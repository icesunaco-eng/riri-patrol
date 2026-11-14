"""Real wake call sites: spawned is not a verified response or completed work."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import subprocess
from unittest import TestCase
from unittest.mock import patch

from riri.instruction_noa import InstructionNoaWake
from riri.local_noa import LocalNoaClient
from tests import test_local_noa as fixtures


REF = "logs/noa-local/20261114T050000000000Z-01234567"
RUN = "0123456789abcdef0123456789abcdef"
INPUT_HASH = "a" * 64


class TrackedLocal(LocalNoaClient):
    def validate(self):
        pass

    def fire(self, text):
        self.on_attempt({"transport": "local", "ref": REF, "run_id": RUN, "input_sha256": INPUT_HASH})
        return {"type": "local_session", "mode": "new", "session_id": None,
                "pid": 4242, "run_id": RUN}


class ExecutionTests(TestCase):
    setUp = fixtures.LocalIntegrationTests.setUp
    new_patrol = fixtures.LocalIntegrationTests.new_patrol
    begin = fixtures.LocalIntegrationTests.begin
    restart = fixtures.LocalIntegrationTests.restart
    source = fixtures.LocalIntegrationTests.source

    def test_new_spawn_is_distinct_from_session_verified(self):
        source = self.source()
        self.p.config = replace(self.p.config, noa_transport="local")
        client = TrackedLocal(self.p.config, guard=self.p.check_time, clock=self.clock)
        self.begin()
        InstructionNoaWake(self.p, client).run()
        item = source["noa_instruction_wake"]
        self.assertEqual(item["reason"], "local_spawned")
        self.assertEqual(item["local_run_id"], RUN)
        self.assertNotEqual(item.get("execution", {}).get("outcome"), "task_completed")

    def test_legacy_done_is_not_rewritten(self):
        source = self.source()
        item = source["noa_instruction_wake"]
        item.update(status="done", attempt_at="2099-01-01T00:00:00+00:00",
                    transport="local", local_ref=REF, reason="session_verified")
        before = deepcopy(item)
        self.begin()
        InstructionNoaWake(self.p, self.client).run()
        self.assertEqual(item, before)

    def test_result_failure_is_recorded_and_notice_deduplicated_without_refire(self):
        source = self.source()
        item = source["noa_instruction_wake"]
        item.update(status="done", attempt_at="2099-01-01T00:00:00+00:00",
                    transport="local", local_ref=REF, local_run_id=RUN, local_input_sha256=INPUT_HASH,
                    reason="local_spawned")
        observation = {"state": "exited", "outcome": "rate_limited", "run_id": RUN,
                       "input_sha256": INPUT_HASH, "exit_code": 1, "partial_work_possible": True}
        with patch("riri.local_outcomes.observe", return_value=observation):
            for _ in range(3):
                self.begin()
                InstructionNoaWake(self.p, self.client).run()
        self.assertEqual(item["execution"]["outcome"], "rate_limited")
        self.assertEqual(item["status"], "done")
        self.assertFalse(self.client.calls)
        notices = [v for k, v in self.p.state["outbox"].items()
                   if k.startswith("noa-local-outcome:")]
        self.assertEqual(len(notices), 1)
        self.assertIn("自動再送しません", notices[0]["text"])

    def test_mismatched_receipt_cannot_claim_cli_success(self):
        source = self.source()
        item = source["noa_instruction_wake"]
        item.update(status="done", attempt_at="2099-01-01T00:00:00+00:00",
                    transport="local", local_ref=REF, local_run_id=RUN, local_input_sha256=INPUT_HASH,
                    reason="local_spawned")
        with patch("riri.local_outcomes.observe", return_value={
                "state": "exited", "outcome": "cli_exited", "run_id": "other-run-123"}):
            self.begin()
            InstructionNoaWake(self.p, self.client).run()
        self.assertEqual(item["execution"]["state"], "unknown")
        self.assertNotEqual(item["execution"]["outcome"], "cli_exited")

    def test_full_local_quota_exit_is_observed_without_refire(self):
        # The real detached supervisor runs only this synthetic executable.
        # No Claude binary, remote client or real account is involved.
        from riri.local_outcomes import observe
        t = Path(self.tmp.name)
        noa = t / "noa-fixture"
        noa.mkdir()
        executable = t / "fake-cli"
        result = {"type": "result", "is_error": True, "subtype": "error_during_execution",
                  "errors": ["You've hit your 5-hour limit · resets 4:50pm (Asia/Tokyo)"]}
        executable.write_text("#!" + sys.executable + "\nimport json,sys\nprint(" +
                              repr(json.dumps(result)) + ")\nsys.exit(1)\n")
        executable.chmod(0o700)
        self.p.config = replace(self.p.config, noa_transport="local", noa_local_dir=str(noa),
                                noa_claude_bin=str(executable), noa_local_permission_mode="plan")
        children = []
        def spawn(*args, **kwargs):
            child = subprocess.Popen(*args, **kwargs)
            children.append(child)
            return child
        client = LocalNoaClient(self.p.config, guard=self.p.check_time,
                                clock=lambda: datetime.now(timezone.utc),
                                projects_dir=t / "no-transcripts", popen=spawn)
        source = self.source()
        self.begin()
        InstructionNoaWake(self.p, client).run()
        item = source["noa_instruction_wake"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observation = observe(self.p.config.root, item["local_ref"])
            if observation["state"] == "exited":
                break
            time.sleep(0.01)
        self.assertEqual(observation["outcome"], "rate_limited")
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.begin()
        InstructionNoaWake(self.p, client).run()
        self.assertEqual(item["reason"], "local_spawned")
        self.assertEqual(item["execution"]["outcome"], "rate_limited")
        self.assertEqual(len([x for x in (self.p.config.root / "logs/noa-local").glob("*.manifest.json")]), 1)
        self.assertNotEqual(self.p.state["requests"][source["request_id"]].get("status"), "done")
