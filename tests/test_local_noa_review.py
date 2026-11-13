"""みにのレビュー L-01〜L-03 の反例と、local経路の安全境界（通信なし・偽の起動関数）。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from riri import local_noa
from riri.errors import StopRequested
from riri.instruction_noa import InstructionNoaWake, ROUTINE_HELD, LOCAL_INCOMPLETE
from riri.local_noa import LocalNoaClient, match_keys
from riri.noa import NoaRejected, NoaUncertain
from tests import test_instruction_noa as ti
from tests import test_noa_wake as tw
from tests.test_local_noa import FakePopen, MSG, clock

PROJECT = "416f1dff94dc815bb595ca8fe52527b5"
DASHED = "416f1dff-94dc-815b-b595-ca8fe52527b5"


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.noa = t / "のあ"; self.noa.mkdir()
        self.other = t / "other"; self.other.mkdir()
        self.projects = t / "projects"; (self.projects / "p").mkdir(parents=True)
        self.bin = t / "claude"; self.bin.write_text("#!/bin/sh\n"); self.bin.chmod(0o700)
        self.root = t / "riri"; self.root.mkdir()
        from riri.config import Config
        self.cfg = Config(root=self.root, noa_transport="local", noa_local_dir=str(self.noa),
                          noa_claude_bin=str(self.bin), noa_local_permission_mode="bypassPermissions")
        self.popen = FakePopen()

    def transcript(self, sid, cwd, body, mtime, folder="p", ascii_escape=False):
        (self.projects / folder).mkdir(exist_ok=True)
        p = self.projects / folder / (sid + ".jsonl")
        rows = [{"type": "summary", "summary": "x"},
                {"type": "user", "cwd": str(cwd), "sessionId": sid, "message": {"content": body}}]
        p.write_text("\n".join(json.dumps(r, ensure_ascii=ascii_escape) for r in rows) + "\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))
        return p

    def client(self, guard=lambda: None):
        return LocalNoaClient(self.cfg, guard=guard, clock=clock, projects_dir=self.projects, popen=self.popen)


class L01SearchCompleteness(Base):
    def test_400_newer_other_cwd_do_not_hide_the_match(self):
        self.transcript("sess-target-01", self.noa, "🤍20261113-2 の続き", 100)
        for i in range(400):
            self.transcript("sess-other-%04d" % i, self.other, "🤍20261113-2", 1000 + i, folder="o")
        result = self.client().fire(MSG)
        self.assertEqual((result["mode"], result["session_id"]), ("resume", "sess-target-01"))

    def test_candidate_limit_is_incomplete_not_new(self):
        with patch.object(local_noa, "MAX_CANDIDATES", 3):
            self.transcript("sess-target-01", self.noa, "🤍20261113-2", 100)
            for i in range(3):
                self.transcript("sess-noa-%04d" % i, self.noa, "無関係", 1000 + i)
            with self.assertRaises(NoaRejected) as cm:
                self.client().fire(MSG)
        self.assertEqual(cm.exception.reason, "local_search_incomplete")
        self.assertFalse(self.popen.calls)

    def test_scan_limit_is_incomplete(self):
        with patch.object(local_noa, "MAX_SCAN_FILES", 2):
            for i in range(3):
                self.transcript("sess-other-%04d" % i, self.other, "x", 1000 + i, folder="o")
            with self.assertRaises(NoaRejected):
                self.client().fire(MSG)
        self.assertFalse(self.popen.calls)

    def test_unreadable_transcript_is_incomplete(self):
        self.transcript("sess-target-01", self.noa, "🤍20261113-2", 100)
        bad = self.transcript("sess-broken-01", self.noa, "?", 200)
        real_open = open
        def fake_open(path, *a, **kw):
            if str(path) == str(bad):
                raise PermissionError("denied")
            return real_open(path, *a, **kw)
        with patch("builtins.open", fake_open):
            with self.assertRaises(NoaRejected) as cm:
                self.client().fire(MSG)
        self.assertEqual(cm.exception.reason, "local_search_incomplete")
        self.assertFalse(self.popen.calls)

    def test_complete_search_without_match_starts_new(self):
        self.transcript("sess-noa-0001", self.noa, "無関係", 100)
        self.assertEqual(self.client().fire(MSG)["mode"], "new")

    def test_stop_during_search_is_not_sent(self):
        self.transcript("sess-noa-0001", self.noa, "無関係", 100)
        calls = []
        def guard():
            calls.append(1)
            if len(calls) >= 2:  # first guard passes, stop arrives during the search
                raise StopRequested("stop")
        with self.assertRaises(StopRequested) as cm:
            self.client(guard).fire(MSG)
        self.assertTrue(cm.exception.not_sent)
        self.assertFalse(self.popen.calls)


class L02NotionIds(Base):
    def msg(self, project):
        return "依頼ID：無し\n案件ページ：https://app.notion.com/p/" + project + "\n本文"

    def test_compact_input_matches_dashed_history(self):
        self.transcript("sess-proj-0001", self.noa, "案件ページ：https://app.notion.com/p/" + DASHED, 100)
        self.assertEqual(self.client().fire(self.msg(PROJECT))["session_id"], "sess-proj-0001")

    def test_dashed_input_matches_compact_history(self):
        self.transcript("sess-proj-0001", self.noa, "https://app.notion.com/p/Title-" + PROJECT, 100)
        self.assertEqual(self.client().fire(self.msg(DASHED))["session_id"], "sess-proj-0001")

    def test_both_dashed_and_uppercase(self):
        self.transcript("sess-proj-0001", self.noa, DASHED.upper(), 100)
        self.assertEqual(self.client().fire(self.msg(DASHED))["session_id"], "sess-proj-0001")

    def test_input_keys_are_normalized_and_slug_safe(self):
        self.assertEqual(match_keys(self.msg("MF-deadbeef-" + PROJECT)), [PROJECT])
        self.assertEqual(match_keys(self.msg(DASHED.upper())), [PROJECT])

    def test_longer_hex_is_not_a_match(self):
        self.transcript("sess-proj-0001", self.noa, PROJECT + "0", 100)
        self.assertEqual(self.client().fire(self.msg(PROJECT))["mode"], "new")

    def test_request_id_emoji_and_digit_boundaries_kept(self):
        self.transcript("sess-a-000001", self.noa, "💛20261113-2 と 🤍20261113-20 と 🤍120261113-2", 100)
        self.assertEqual(self.client().fire("依頼ID：🤍20261113-2")["mode"], "new")
        p = self.transcript("sess-b-000001", self.noa, "🤍20261113-2", 200, ascii_escape=True)
        self.assertIn("\\ud83e\\udd0d20261113-2", p.read_text())  # stored JSON-escaped
        self.assertEqual(self.client().fire("依頼ID：🤍20261113-2")["session_id"], "sess-b-000001")


class ExplodingPopen(FakePopen):
    """Spawn outcome unknown (e.g. interrupted after the attempt was recorded)."""
    def __call__(self, args, **kw):
        raise RuntimeError("unknown")


class L03HumanPath(unittest.TestCase):
    setUp = tw.WakeTests.setUp
    new_patrol = tw.WakeTests.new_patrol
    begin = tw.WakeTests.begin
    message = tw.WakeTests.message
    items = tw.WakeTests.items
    restart = tw.WakeTests.restart

    def local(self, popen, guard=None):
        t = Path(self.tmp.name)
        noa = t / "のあ"; noa.mkdir(exist_ok=True)
        exe = t / "claude"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o700)
        self.cfg = replace(self.cfg, noa_transport="local", noa_local_dir=str(noa),
                           noa_claude_bin=str(exe), noa_local_permission_mode="bypassPermissions")
        self.p.config = self.cfg
        return LocalNoaClient(self.cfg, guard=guard or self.p.check_time, clock=self.clock,
                              projects_dir=t / "none", popen=popen)

    def notices(self):
        return [x["text"] for x in self.p.state["outbox"].values() if x.get("kind") == "noa_notice"]

    def test_local_unknown_outcome_points_to_mac_not_routine(self):
        client = self.local(ExplodingPopen())
        self.message(1)
        self.begin(); InstructionNoaWake(self.p, client).run()
        item = self.items()[0]
        self.assertEqual((item["status"], item["transport"]), ("held", "local"))
        self.assertTrue(item["local_ref"].startswith("logs/noa-local/"))
        text = "\n".join(self.notices())
        self.assertIn("MacのClaude Code", text)
        self.assertIn(item["local_ref"], text)
        self.assertNotIn("routine", text)

    def test_guidance_survives_transport_change(self):
        client = self.local(ExplodingPopen())
        self.message(1)
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.p.config = replace(self.cfg, noa_transport="routine")
        wake = InstructionNoaWake(self.p, tw.MockFire())
        self.assertIn("MacのClaude Code", wake._held_guidance(self.items()[0]))

    def test_unknown_route_is_not_asserted(self):
        self.local(FakePopen())
        wake = InstructionNoaWake(self.p, tw.MockFire())
        guidance = wake._held_guidance({"status": "held"})
        self.assertIn("routine", guidance)
        self.assertIn("MacのClaude Code", guidance)

    def test_sending_interrupted_becomes_held_local_and_never_resends(self):
        popen = FakePopen()
        client = self.local(popen)
        self.message(1)
        def crash(record):
            InstructionNoaWake._record_attempt(wake, record)
            raise KeyboardInterrupt()  # process dies after the durable route record
        wake = InstructionNoaWake(self.p, client)
        client.on_attempt = crash
        self.begin()
        with self.assertRaises(KeyboardInterrupt):
            wake.run()
        self.assertEqual(self.items()[0]["status"], "sending")
        self.restart()
        client2 = self.local(popen)
        for _ in range(3):
            self.begin(); InstructionNoaWake(self.p, client2).run()
        self.assertEqual(self.items()[0]["status"], "held")
        self.assertEqual(self.items()[0]["transport"], "local")
        self.assertFalse(popen.calls)
        self.assertIn("MacのClaude Code", "\n".join(self.notices()))

    def test_routine_held_text_unchanged(self):
        self.client.responses = [NoaUncertain()]
        self.message(1)
        self.begin(); InstructionNoaWake(self.p, self.client).run()
        item = self.items()[0]
        self.assertEqual((item["status"], item["transport"]), ("held", "routine"))
        self.assertTrue(any(ROUTINE_HELD in t for t in self.notices()))

    def test_stop_just_before_spawn_is_not_sent(self):
        popen = FakePopen()
        stop = {"on": False}
        def guard():
            self.p.check_time()
            if stop["on"]:
                raise StopRequested("stop")
        client = self.local(popen, guard)
        self.message(1)
        stop["on"] = True
        self.begin()
        with self.assertRaises(StopRequested):
            InstructionNoaWake(self.p, client).run()
        self.assertEqual(self.items()[0]["status"], "queued")
        self.assertFalse(popen.calls)

    def test_practice_off_and_secret_never_spawn(self):
        popen = FakePopen()
        client = self.local(popen)
        self.message(1, text="@のあ unit-token")
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual(self.items()[0]["status"], "rejected")
        self.p.config = replace(self.cfg, mode="practice")
        self.message(2)
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.message(3)
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertFalse(popen.calls)

    def test_two_deliveries_in_same_tick_get_distinct_logs(self):
        popen = FakePopen()
        client = self.local(popen)
        client.fire("a"); client.fire("b")
        self.assertEqual(len(list((Path(self.cfg.root) / "logs" / "noa-local").iterdir())), 4)

    def test_search_incomplete_rejects_with_specific_notice(self):
        popen = FakePopen()
        client = self.local(popen)
        client.projects_dir = Path(self.tmp.name)
        with patch.object(local_noa, "find_session", side_effect=local_noa.SearchIncomplete()):
            self.message(1)
            self.begin(); InstructionNoaWake(self.p, client).run()
            self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual((self.items()[0]["status"], self.items()[0]["reason"]), ("rejected", "local_search_incomplete"))
        self.assertFalse(popen.calls)
        self.assertTrue(any(LOCAL_INCOMPLETE in t for t in self.notices()))


class L03InstructionPath(unittest.TestCase):
    setUp = ti.InstructionWakeTests.setUp
    new_patrol = ti.InstructionWakeTests.new_patrol
    begin = ti.InstructionWakeTests.begin
    restart = ti.InstructionWakeTests.restart
    source = ti.InstructionWakeTests.source
    local = L03HumanPath.local

    def notice(self, rid):
        return self.p.state["outbox"]["noa-instruction:" + rid]["text"]

    def test_local_held_notice_points_to_mac(self):
        client = self.local(ExplodingPopen())
        src = self.source()
        self.begin(); InstructionNoaWake(self.p, client).run()
        wake = src["noa_instruction_wake"]
        self.assertEqual((wake["status"], wake["transport"]), ("held", "local"))
        self.assertIn("確認先：MacのClaude Code", self.notice(src["request_id"]))
        self.assertIn(wake["local_ref"], self.notice(src["request_id"]))

    def test_routine_held_notice_text_unchanged(self):
        self.client.responses = [NoaUncertain()]
        src = self.source()
        self.begin(); InstructionNoaWake(self.p, self.client).run()
        self.assertEqual(src["noa_instruction_wake"]["transport"], "routine")
        self.assertIn("\nりりからの指示による、のあの起動を確認できません。自動再送しません。参照：rp:",
                      self.notice(src["request_id"]))

    def test_stop_practice_off_secret_and_search_incomplete(self):
        popen = FakePopen()
        stop = {"on": True}
        def guard():
            self.p.check_time()
            if stop["on"]:
                raise StopRequested("stop")
        client = self.local(popen, guard)
        src = self.source(1)
        self.begin()
        with self.assertRaises(StopRequested):
            InstructionNoaWake(self.p, client).run()
        self.assertEqual((src["noa_instruction_wake"]["status"], src["noa_instruction_wake"]["attempt_at"]), ("queued", None))
        stop["on"] = False
        with patch.object(local_noa, "find_session", side_effect=local_noa.SearchIncomplete()):
            self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual(src["noa_instruction_wake"]["reason"], "local_search_incomplete")
        self.assertIn(LOCAL_INCOMPLETE, self.notice(src["request_id"]))
        self.p.config = replace(self.p.config, mode="practice")
        s2 = self.source(2)
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual(s2["noa_instruction_wake"]["status"], "simulated")
        self.p.config = replace(self.p.config, mode="live")
        s3 = self.source(3)
        s3["text"] = s3["text"] + " unit-token"; self.p.save()
        self.begin(); InstructionNoaWake(self.p, client).run()
        self.assertEqual(s3["noa_instruction_wake"]["reason"], "invalid_source")
        self.assertFalse(popen.calls)


class RoutineDefault(unittest.TestCase):
    def test_defaults(self):
        import tempfile
        from riri.config import Config
        from riri.instruction_noa import RecordingClient
        from riri.noa import NoaClient
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(d)
            self.assertEqual((cfg.noa_transport, cfg.noa_wake_enabled), ("routine", False))

        class P:
            config = Config(root=Path("."))
            state = {"noa_wake": {"cutoff": None, "watermark": None, "items": {}, "attempts": []}, "outbox": {}}
            check_time = staticmethod(lambda: None)
            clock = staticmethod(clock)
        wake = InstructionNoaWake(P)
        self.assertIsInstance(wake.client, RecordingClient)
        self.assertIsInstance(wake.client.inner, NoaClient)


if __name__ == "__main__":
    unittest.main()
