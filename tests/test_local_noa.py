"""Local Claude Code delivery (NOA_TRANSPORT=local): offline, no real CLI is run."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from riri.config import Config
from riri.errors import SafetyError, StopRequested
from riri.local_noa import LocalNoaClient, find_session, match_keys, local_evidence
from riri.noa import NoaRejected, NoaUncertain, NoaClient, session_evidence as routine_evidence
from riri.local_noa import any_evidence
from riri.instruction_noa import InstructionNoaWake


def session_evidence(result):
    return any_evidence(result, routine_evidence)

MSG = ("りりからの依頼（依頼ID: 🤍20261113-6）\nメッセージ:\n@のあ\n【りりからのお願い】\n"
       "依頼ID：🤍20261113-6\n報告先：https://app.notion.com/p/413f1dff94dc8120bfa6d9ba516c56bb\n"
       "案件ページ：https://app.notion.com/p/416f1dff94dc815bb595ca8fe52527b5\n"
       "くわしい内容：🤍20261113-5の再依頼（修正元は🤍20261113-2）\nSlack: https://x.slack.com/archives/C/p1")


class Child:
    pid = 4242


class FakePopen:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, args, **kw):
        if self.fail:
            raise OSError("no such file")
        self.calls.append((args, kw, kw["stdin"].read().decode("utf-8")))
        return Child()


def clock():
    return datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc)


class LocalNoaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.noa = t / "のあ"; self.noa.mkdir()
        self.other = t / "other"; self.other.mkdir()
        self.projects = t / "projects"; (self.projects / "p1").mkdir(parents=True)
        self.bin = t / "claude"; self.bin.write_text("#!/bin/sh\n"); self.bin.chmod(0o700)
        self.root = t / "riri"; self.root.mkdir()
        self.cfg = replace(Config(root=self.root), noa_transport="local", noa_local_dir=str(self.noa),
                           noa_claude_bin=str(self.bin), noa_local_permission_mode="acceptEdits")
        self.popen = FakePopen()

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, sid, cwd, body, mtime, ascii_escape=False):
        rows = [{"type": "user", "cwd": str(cwd), "sessionId": sid,
                 "message": {"content": body}}]
        p = self.projects / "p1" / (sid + ".jsonl")
        p.write_text("\n".join(json.dumps(r, ensure_ascii=ascii_escape) for r in rows) + "\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))
        return p

    def client(self, guard=lambda: None):
        return LocalNoaClient(self.cfg, guard=guard, clock=clock, projects_dir=self.projects, popen=self.popen)

    def test_match_keys_keep_emoji_and_project_only(self):
        keys = match_keys(MSG)
        self.assertEqual(keys[:3], ["🤍20261113-6", "🤍20261113-5", "🤍20261113-2"])
        self.assertIn("416f1dff94dc815bb595ca8fe52527b5", keys)
        self.assertNotIn("413f1dff94dc8120bfa6d9ba516c56bb", keys)  # report page is in every request

    def test_resumes_most_recent_related_session_in_noa_dir(self):
        self.transcript("sess-old-0001", self.noa, "🤍20261113-2 を修正", 100)
        self.transcript("sess-new-0002", self.noa, "前回 🤍20261113-2 の続き", 200, ascii_escape=True)
        self.transcript("sess-unrelated", self.noa, "💛20261113-2 は別件", 300)
        self.transcript("sess-otherdir1", self.other, "🤍20261113-2", 400)
        result = self.client().fire(MSG)
        self.assertEqual(result["mode"], "resume")
        self.assertEqual(result["session_id"], "sess-new-0002")
        args, kw, stdin = self.popen.calls[0]
        self.assertEqual(args, [str(self.bin), "-p", "--permission-mode", "acceptEdits", "--resume", "sess-new-0002"])
        self.assertEqual(kw["cwd"], os.path.realpath(self.noa))
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(stdin, MSG)
        self.assertEqual(session_evidence(result), "local:resume:sess-new-0002")

    def test_project_page_match_resumes(self):
        self.transcript("sess-proj-001", self.noa, "案件 416f1dff94dc815bb595ca8fe52527b5", 100)
        self.assertEqual(self.client().fire(MSG)["session_id"], "sess-proj-001")

    def test_no_match_starts_new_session(self):
        self.transcript("sess-unrelated", self.noa, "💛20261113-2 と 20261113-20", 100)
        result = self.client().fire(MSG)
        self.assertEqual(result["mode"], "new")
        self.assertNotIn("--resume", self.popen.calls[0][0])
        self.assertEqual(session_evidence(result), "local:new:pid4242")

    def test_child_env_has_no_riri_secrets(self):
        os.environ["SLACK_BOT_TOKEN"] = "unit-" + "secret-value"
        try:
            self.client().fire(MSG)
        finally:
            del os.environ["SLACK_BOT_TOKEN"]
        env = self.popen.calls[0][1]["env"]
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        self.assertLessEqual(set(env), {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL"})

    def test_message_and_log_are_private_files(self):
        self.client().fire(MSG)
        files = list((self.root / "logs" / "noa-local").iterdir())
        self.assertEqual(len(files), 2)
        for f in files:
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_invalid_config_refuses_before_spawn(self):
        for change in ({"noa_local_dir": "relative/のあ"}, {"noa_local_dir": str(self.noa / "missing")},
                       {"noa_claude_bin": "claude"}, {"noa_local_permission_mode": ""},
                       {"noa_local_permission_mode": "dontAsk"}):
            self.cfg = replace(self.cfg, **change)
            with self.assertRaises(SafetyError):
                self.client().fire(MSG)
            self.setUp_cfg_reset()
        self.assertFalse(self.popen.calls)

    def setUp_cfg_reset(self):
        self.cfg = replace(self.cfg, noa_local_dir=str(self.noa), noa_claude_bin=str(self.bin),
                           noa_local_permission_mode="acceptEdits")

    def test_bypass_mode_is_allowed_when_chosen(self):
        self.cfg = replace(self.cfg, noa_local_permission_mode="bypassPermissions")
        self.client().fire(MSG)
        self.assertEqual(self.popen.calls[0][0][2:4], ["--permission-mode", "bypassPermissions"])

    def test_stop_before_spawn_is_not_sent(self):
        def guard():
            raise StopRequested("stop")
        with self.assertRaises(StopRequested) as cm:
            self.client(guard).fire(MSG)
        self.assertTrue(cm.exception.not_sent)
        self.assertFalse(self.popen.calls)

    def test_spawn_failure_is_rejection(self):
        self.popen = FakePopen(fail=True)
        with self.assertRaises(NoaRejected) as cm:
            self.client().fire(MSG)
        self.assertEqual(cm.exception.reason, "local_spawn_failed")

    def test_bad_local_result_is_uncertain(self):
        for bad in ({"type": "local_session", "mode": "resume", "session_id": "../x", "pid": 1},
                    {"type": "local_session", "mode": "new", "pid": 0},
                    {"type": "local_session", "mode": "other", "pid": 1}):
            with self.assertRaises(NoaUncertain):
                session_evidence(bad)
        self.assertIsNone(local_evidence(None))

    def test_routine_evidence_unchanged_and_restored(self):
        import riri.noa as noa
        original = noa.session_evidence
        from riri.local_noa import local_evidence_installed
        with local_evidence_installed(True):
            self.assertEqual(noa.session_evidence({"type": "local_session", "mode": "new", "pid": 7}), "local:new:pid7")
        self.assertIs(noa.session_evidence, original)
        with local_evidence_installed(False):
            self.assertIs(noa.session_evidence, original)
        with self.assertRaises(NoaUncertain):
            original({"type": "local_session", "mode": "new", "pid": 7})

    def test_config_load_defaults_to_routine(self):
        (self.root / ".env").write_text("NOA_TRANSPORT=local\nNOA_LOCAL_DIR=/x/のあ\nNOA_LOCAL_PERMISSION_MODE=plan\n", encoding="utf-8")
        cfg = Config.load(self.root)
        self.assertEqual((cfg.noa_transport, cfg.noa_local_dir, cfg.noa_local_permission_mode), ("local", "/x/のあ", "plan"))
        (self.root / ".env").write_text("NOA_TRANSPORT=weird\n", encoding="utf-8")
        self.assertEqual(Config.load(self.root).noa_transport, "routine")


if __name__ == "__main__":
    unittest.main()


from tests import test_instruction_noa as ti


class LocalIntegrationTests(unittest.TestCase):
    """Generated-instruction path end to end with the local client (fake spawn)."""
    setUp = ti.InstructionWakeTests.setUp
    new_patrol = ti.InstructionWakeTests.new_patrol
    begin = ti.InstructionWakeTests.begin
    restart = ti.InstructionWakeTests.restart
    source = ti.InstructionWakeTests.source

    def test_generated_instruction_delivered_locally_once(self):
        t = Path(self.tmp.name)
        noa = t / "のあ"; noa.mkdir()
        exe = t / "claude"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o700)
        self.p.config = replace(self.p.config, noa_transport="local", noa_local_dir=str(noa),
                                noa_claude_bin=str(exe), noa_local_permission_mode="default")
        popen = FakePopen()
        client = LocalNoaClient(self.p.config, guard=self.p.check_time, clock=self.clock,
                                projects_dir=t / "none", popen=popen)
        src = self.source()
        self.begin(); ti.InstructionNoaWake(self.p, client).run()
        self.begin(); ti.InstructionNoaWake(self.p, client).run()
        self.assertEqual(len(popen.calls), 1)
        self.assertIn(src["text"], popen.calls[0][2])
        self.assertEqual(src["noa_instruction_wake"]["status"], "done")
        self.assertEqual(src["noa_instruction_wake"]["evidence"], "local:new:pid4242")


from tests import test_noa_wake as tw


class LocalHumanMentionTests(unittest.TestCase):
    """Human @のあ path (riri/noa.py, pinned) also delivers locally with local evidence."""
    setUp = tw.WakeTests.setUp
    new_patrol = tw.WakeTests.new_patrol
    begin = tw.WakeTests.begin
    message = tw.WakeTests.message
    items = tw.WakeTests.items

    def test_human_mention_delivered_locally(self):
        t = Path(self.tmp.name)
        noa = t / "のあ"; noa.mkdir()
        exe = t / "claude"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o700)
        self.p.config = replace(self.p.config, noa_transport="local", noa_local_dir=str(noa),
                                noa_claude_bin=str(exe), noa_local_permission_mode="default")
        popen = FakePopen()
        client = LocalNoaClient(self.p.config, guard=self.p.check_time, clock=self.clock,
                                projects_dir=t / "none", popen=popen)
        self.message(1)
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(self.items()[0]["status"], "done")
        import riri.noa as noa_mod
        self.assertIs(noa_mod.session_evidence, routine_evidence)
