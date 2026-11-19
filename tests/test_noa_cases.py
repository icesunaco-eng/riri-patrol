"""Noa's case folders (2026-11-19, たかむね): start local runs in 案件/<name>/.

Offline: no real CLI runs. Temporary paths are resolved first because macOS
/var is a symlink to /private/var and transcripts record the real cwd.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from riri.config import Config
from riri.local_noa import LocalNoaClient
from riri.noa_cases import case_dir, canonical_ids, requested_ids, Unclear
from tests.test_local_noa import FakePopen, MSG, clock

MF = "416f1dff94dc815bb595ca8fe52527b5"          # 案件ページ in MSG
KEIBA = "417f1dff94dc8174b62cdb6b7c3de202"
REPORT = "413f1dff94dc8120bfa6d9ba516c56bb"       # 報告先 in MSG, never a case key


def rules(ids, extra=""):
    lines = ["# 案件", "", "## 正本"] + ["- Notion：https://app.notion.com/p/" + i for i in ids]
    return "\n".join(lines + ["", "## 守ること", extra, ""])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(os.path.realpath(self.tmp.name))
        self.noa = t / "のあ"; self.noa.mkdir()
        self.cases = self.noa / "案件"; self.cases.mkdir()
        self.projects = t / "projects"; (self.projects / "p1").mkdir(parents=True)
        self.bin = t / "claude"; self.bin.write_text("#!/bin/sh\n"); self.bin.chmod(0o700)
        self.root = t / "riri"; self.root.mkdir()
        self.cfg = replace(Config(root=self.root), noa_transport="local", noa_local_dir=str(self.noa),
                           noa_claude_bin=str(self.bin), noa_local_permission_mode="acceptEdits")
        self.popen = FakePopen()

    def tearDown(self):
        for d, _, files in os.walk(self.tmp.name):
            os.chmod(d, 0o700)
            for f in files:
                p = os.path.join(d, f)
                if not os.path.islink(p):
                    os.chmod(p, 0o600)
        self.tmp.cleanup()

    def case(self, name, ids, extra=""):
        d = self.cases / name
        d.mkdir()
        (d / "CLAUDE.md").write_text(rules(ids, extra), encoding="utf-8")
        return d

    def transcript(self, sid, cwd, body, mtime):
        row = {"type": "user", "cwd": str(cwd), "sessionId": sid, "message": {"content": body}}
        p = self.projects / "p1" / (sid + ".jsonl")
        p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))

    def fire(self, text=MSG):
        client = LocalNoaClient(self.cfg, guard=lambda: None, clock=clock,
                                projects_dir=self.projects, popen=self.popen)
        result = client.fire(text)
        args, kw, _ = self.popen.calls[-1]
        manifest = json.loads(Path(args[2]).read_text())
        self.assertEqual(manifest["cwd"], kw["cwd"])
        return result, kw["cwd"]


class CasePicking(Base):
    def test_only_the_project_line_counts(self):
        self.assertEqual(requested_ids(MSG), {MF})
        self.assertNotIn(REPORT, requested_ids(MSG))

    def test_one_match_is_the_case_folder(self):
        mf = self.case("MF完全自動化", [MF])
        self.case("競馬", [KEIBA])
        self.assertEqual(case_dir(str(self.noa), MSG), str(mf))

    def test_dashed_id_in_rules_matches_compact_request(self):
        dashed = "-".join((MF[:8], MF[8:12], MF[12:16], MF[16:20], MF[20:]))
        mf = self.case("MF", [dashed.upper()])
        self.assertEqual(case_dir(str(self.noa), MSG), str(mf))

    def test_id_outside_the_canonical_section_does_not_match(self):
        self.case("ヒロインズ運用", [KEIBA], extra="- 参考：https://app.notion.com/p/" + MF)
        self.assertIsNone(case_dir(str(self.noa), MSG))

    def test_two_matches_do_not_guess(self):
        self.case("A", [MF]); self.case("B", [MF])
        self.assertIsNone(case_dir(str(self.noa), MSG))

    def test_no_case_root_or_no_project_line(self):
        self.cases.rmdir()
        self.assertIsNone(case_dir(str(self.noa), MSG))
        self.cases.mkdir(); self.case("MF", [MF])
        self.assertIsNone(case_dir(str(self.noa), MSG.replace("案件ページ", "参考")))

    def test_symlinked_folder_is_never_a_case(self):
        body = self.noa.parent / "live-body"; body.mkdir()
        (body / "CLAUDE.md").write_text(rules([MF]), encoding="utf-8")
        (self.cases / "リンク").symlink_to(body)
        self.assertIsNone(case_dir(str(self.noa), MSG))

    def test_symlinked_or_unreadable_rules_are_unclear(self):
        d = self.cases / "MF"; d.mkdir()
        target = self.noa.parent / "rules.md"; target.write_text(rules([MF]), encoding="utf-8")
        (d / "CLAUDE.md").symlink_to(target)
        self.assertIsNone(case_dir(str(self.noa), MSG))
        with self.assertRaises(Unclear):
            canonical_ids(str(d / "CLAUDE.md"))
        (d / "CLAUDE.md").unlink()
        (d / "CLAUDE.md").write_text(rules([MF]), encoding="utf-8")
        os.chmod(d / "CLAUDE.md", 0)
        if not os.access(d / "CLAUDE.md", os.R_OK):  # root can still read it
            self.assertIsNone(case_dir(str(self.noa), MSG))

    def test_folder_without_rules_is_skipped(self):
        (self.cases / "メモだけ").mkdir()
        mf = self.case("MF", [MF])
        self.assertEqual(case_dir(str(self.noa), MSG), str(mf))

    def test_oversized_rules_are_unclear(self):
        self.case("MF", [MF], extra="x" * (64 * 1024))
        self.assertIsNone(case_dir(str(self.noa), MSG))


class LaunchFolder(Base):
    def test_new_work_starts_in_the_case_folder(self):
        mf = self.case("MF完全自動化", [MF])
        result, cwd = self.fire()
        self.assertEqual((result["mode"], cwd), ("new", str(mf)))

    def test_no_case_keeps_noa_dir(self):
        self.case("競馬", [KEIBA])
        result, cwd = self.fire()
        self.assertEqual((result["mode"], cwd), ("new", str(self.noa)))

    def test_case_session_is_resumed_in_the_case_folder(self):
        mf = self.case("MF", [MF])
        self.transcript("sess-base-0001", self.noa, "🤍20261113-6", 300)
        self.transcript("sess-case-0001", mf, "🤍20261113-6", 100)
        result, cwd = self.fire()
        self.assertEqual((result["session_id"], cwd), ("sess-case-0001", str(mf)))

    def test_older_session_in_noa_dir_still_resumes_there(self):
        self.case("MF", [MF])
        self.transcript("sess-base-0001", self.noa, "🤍20261113-6 の作業", 100)
        result, cwd = self.fire()
        self.assertEqual((result["mode"], result["session_id"], cwd),
                         ("resume", "sess-base-0001", str(self.noa)))

    def test_unrelated_case_session_is_not_resumed(self):
        mf = self.case("MF", [MF])
        self.transcript("sess-case-0002", mf, "💛20261113-6 は別件", 100)
        result, cwd = self.fire()
        self.assertEqual((result["mode"], cwd), ("new", str(mf)))


if __name__ == "__main__":
    unittest.main()
