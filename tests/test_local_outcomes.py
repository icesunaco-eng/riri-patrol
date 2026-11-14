"""Run-bound observations; isolated fake programs only, never Claude/network."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest

from riri.config import Config
from riri.local_noa import LocalNoaClient
from riri.local_outcomes import observe, classify_output


class OutcomeTests(unittest.TestCase):
    def test_success_is_cli_exit_and_quoted_quota_is_not_failure(self):
        raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "Documentation quotes: You've hit your 5-hour limit"}).encode()
        self.assertEqual(classify_output(raw, 0)["outcome"], "cli_exited")

    def test_explicit_quota_error_is_observed(self):
        raw = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "errors": ["You've hit your 5-hour limit · resets 4:50pm"]}).encode()
        self.assertEqual(classify_output(raw, 1)["outcome"], "rate_limited")

    def test_json_error_result_string_and_auth_error(self):
        raw = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "You've hit your 5-hour limit"}).encode()
        self.assertEqual(classify_output(raw, 1)["outcome"], "rate_limited")
        raw = json.dumps({"type": "result", "is_error": True, "error": {"type": "authentication_error"}}).encode()
        self.assertEqual(classify_output(raw, 1)["outcome"], "authentication_failed")

    def test_nonjson_success_cannot_claim_validated_result(self):
        self.assertEqual(classify_output(b"not a JSON result", 0)["outcome"], "unknown")

    def test_plain_error_requires_failure_exit_and_only_diagnostic_lines(self):
        text = b"You've hit your 5-hour limit\n"
        self.assertEqual(classify_output(text, 1)["outcome"], "rate_limited")
        self.assertEqual(classify_output(text, 0)["outcome"], "unknown")
        self.assertEqual(classify_output(b"User asks:\n" + text, 1)["outcome"], "cli_failed")

    def test_legacy_and_bad_reference_are_safe(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(observe(d, "logs/noa-local/20261114T000000000000Z-12345678")["state"], "legacy_unobserved")
            self.assertEqual(observe(d, "../../.env")["state"], "unknown")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "riri"; self.root.mkdir()
        self.cwd = Path(self.tmp.name) / "noa"; self.cwd.mkdir()
        self.fake = Path(self.tmp.name) / "fake-cli"
        self.fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                             "data=sys.stdin.read()\n"
                             "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'received'}))\n")
        self.fake.chmod(0o700)
        self.children = []
        self.before_spawn = lambda: None
        self.addCleanup(self.reap)
        self.cfg = replace(Config(root=self.root), noa_transport="local", noa_local_dir=str(self.cwd),
                           noa_claude_bin=str(self.fake), noa_local_permission_mode="default")

    def reap(self):
        for child in self.children:
            child.wait(timeout=10)

    def spawn(self, *args, **kwargs):
        self.before_spawn()
        child = subprocess.Popen(*args, **kwargs)
        self.children.append(child)
        return child

    def fire(self, message="private test input"):
        self.attempt_records = []
        client = LocalNoaClient(self.cfg, guard=lambda: None,
                                clock=lambda: datetime.now(timezone.utc), projects_dir=self.root / "none", popen=self.spawn,
                                on_attempt=self.attempt_records.append)
        result = client.fire(message)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            item = observe(self.root, result["local_ref"])
            if item["state"] == "exited":
                return result, item
            time.sleep(.025)
        self.fail("fake worker did not finish: " + repr(item))

    def test_input_hash_is_anchored_by_callback_before_supervisor_spawn(self):
        message = "bound private input"
        def before_spawn():
            self.assertEqual(len(self.attempt_records), 1)
            self.assertEqual(self.attempt_records[0]["input_sha256"],
                             hashlib.sha256(message.encode()).hexdigest())
        self.before_spawn = before_spawn
        result, item = self.fire(message)
        self.assertEqual(self.attempt_records[0]["run_id"], result["run_id"])
        self.assertEqual(self.attempt_records[0]["input_sha256"], item["input_sha256"])

    def test_detached_worker_records_bound_exit_and_private_files(self):
        result, item = self.fire()
        self.assertEqual(item["outcome"], "cli_exited")
        self.assertEqual(item["exit_code"], 0)
        self.assertEqual(item["run_id"], result["run_id"])
        self.assertTrue(item["partial_work_possible"])
        self.assertNotIn("result", item)
        for p in (self.root / "logs" / "noa-local").iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_receipt_mismatch_and_input_change_are_unknown(self):
        result, _ = self.fire()
        receipt = self.root / (result["local_ref"] + ".result.json")
        data = json.loads(receipt.read_text()); data["run_id"] = "other-run"
        receipt.write_text(json.dumps(data))
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")
        data["run_id"] = result["run_id"]; receipt.write_text(json.dumps(data))
        (self.root / (result["local_ref"] + ".in.txt")).write_text("different")
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")

    def test_invalid_final_receipt_lifecycle_is_unknown(self):
        result, _ = self.fire()
        receipt = self.root / (result["local_ref"] + ".result.json")
        original = json.loads(receipt.read_text())
        for change in ({"outcome": "pending"}, {"exit_code": None}, {"exit_code": 1},
                       {"started_at": "not-a-date"}, {"completed_at": "2000-01-01T00:00:00+00:00"},
                       {"output_sha256": "0" * 64}, {"partial_work_possible": False}):
            receipt.write_text(json.dumps({**original, **change}))
            self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown", change)

    def test_stop_between_patrol_and_worker_does_not_start_cli(self):
        marker = self.cwd / "executed"
        self.fake.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\nPath('executed').touch()\n")
        self.before_spawn = lambda: (self.root / "STOP").touch()
        _, item = self.fire()
        self.assertEqual(item["outcome"], "stopped_before_start")
        self.assertFalse(item["partial_work_possible"])
        self.assertIsNone(item["exit_code"])
        self.assertFalse(marker.exists())

    def test_second_supervisor_never_replays_finished_run(self):
        from riri.local_worker import run
        result, _ = self.fire()
        self.assertEqual(run(self.root / (result["local_ref"] + ".manifest.json")), 2)
        self.assertEqual(observe(self.root, result["local_ref"])["outcome"], "cli_exited")

    def test_new_manifest_without_receipt_is_unknown_not_legacy_or_running(self):
        from types import SimpleNamespace
        client = LocalNoaClient(self.cfg, guard=lambda: None, clock=lambda: datetime.now(timezone.utc),
                                projects_dir=self.root / "none", popen=lambda *a, **k: SimpleNamespace(pid=4242))
        result = client.fire("isolated fake input")
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")

    def test_output_symlink_and_oversized_receipt_are_unknown(self):
        result, _ = self.fire()
        output = self.root / (result["local_ref"] + ".out.log")
        other = self.cwd / "outside"; other.write_text(output.read_text()); other.chmod(0o600)
        output.unlink(); output.symlink_to(other)
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")
        receipt = self.root / (result["local_ref"] + ".result.json")
        receipt.write_text(" " * 70000)
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")

    def test_lost_running_supervisor_is_unknown(self):
        from riri.local_outcomes import atomic_json
        result, _ = self.fire()
        receipt = self.root / (result["local_ref"] + ".result.json")
        data = json.loads(receipt.read_text())
        data.update(state="running", outcome="pending")
        data.pop("completed_at"); data.pop("exit_code")
        atomic_json(receipt, data)
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")

    def test_receipt_symlink_is_never_followed(self):
        result, _ = self.fire()
        receipt = self.root / (result["local_ref"] + ".result.json")
        target = Path(self.tmp.name) / "outside.json"; target.write_text(receipt.read_text()); target.chmod(0o600)
        receipt.unlink(); receipt.symlink_to(target)
        self.assertEqual(observe(self.root, result["local_ref"])["state"], "unknown")

    def test_failed_cli_is_not_a_successful_task_or_retry(self):
        self.fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                             "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'errors':[\"You've hit your 5-hour limit\"]}))\n"
                             "sys.exit(1)\n")
        result, item = self.fire()
        self.assertEqual(item["outcome"], "rate_limited")
        self.assertEqual(item["exit_code"], 1)
        self.assertEqual(len(list((self.root / "logs" / "noa-local").glob("*.manifest.json"))), 1)
