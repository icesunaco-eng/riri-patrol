"""PR #4 再レビューの残条件（通信なし・偽の起動関数・実Claude起動なし）。

1. 履歴ルートが「存在するがディレクトリではない」ときは探索不完全（新規起動しない）。
2. 経路の保存（on_attempt）中に STOP／時間切れが来たら、Popen せず未送信として戻す。
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from riri.errors import StopRequested, TimeBudgetExceeded
from riri.instruction_noa import InstructionNoaWake, ROUTINE_HELD
from riri.local_noa import LocalNoaClient
from riri.noa import NoaRejected, NoaUncertain
from tests.test_local_noa import FakePopen, MSG
from tests.test_local_noa_review import Base, ExplodingPopen
from tests.test_local_noa_review_r import HumanBase, InstructionBase, MAC


class RootKind(Base):
    def fire_with_root(self, root):
        client = LocalNoaClient(self.cfg, guard=lambda: None, clock=self.client().clock,
                                projects_dir=root, popen=self.popen)
        return client.fire(MSG)

    def assert_incomplete(self, root):
        with self.assertRaises(NoaRejected) as cm:
            self.fire_with_root(root)
        self.assertEqual(cm.exception.reason, "local_search_incomplete")
        self.assertFalse(self.popen.calls)

    def test_regular_file_root_is_incomplete(self):
        root = Path(self.tmp.name) / "projects-file"
        root.write_text("not a directory")
        self.assert_incomplete(root)

    def test_symlink_to_regular_file_root_is_incomplete(self):
        target = Path(self.tmp.name) / "plain"
        target.write_text("x")
        root = Path(self.tmp.name) / "projects-link"
        root.symlink_to(target)
        self.assert_incomplete(root)

    def test_fifo_root_is_incomplete(self):
        root = Path(self.tmp.name) / "projects-fifo"
        os.mkfifo(root)
        self.assert_incomplete(root)

    def test_root_below_a_regular_file_is_incomplete(self):
        parent = Path(self.tmp.name) / "plain"
        parent.write_text("x")
        self.assert_incomplete(parent / "projects")

    def test_confirmed_missing_root_still_starts_new(self):
        self.assertEqual(self.fire_with_root(Path(self.tmp.name) / "missing")["mode"], "new")

    def test_dangling_symlink_root_is_confirmed_absent(self):
        root = Path(self.tmp.name) / "projects-dangling"
        root.symlink_to(Path(self.tmp.name) / "nowhere")
        self.assertEqual(self.fire_with_root(root)["mode"], "new")

    def test_stray_regular_file_inside_a_real_root_is_not_a_failure(self):
        (self.projects / "notes.jsonl").write_text("x")  # root/* that is not a folder
        self.transcript("sess-target-01", self.noa, "🤍20261113-6", 100)
        self.assertEqual(self.fire_with_root(self.projects)["session_id"], "sess-target-01")


class ArmedGuard:
    """Passes until armed; then every check raises (STOP or time budget)."""
    def __init__(self, patrol, exc):
        self.p, self.exc, self.armed, self.calls_after = patrol, exc, False, 0
    def __call__(self):
        self.p.check_time()
        if self.armed:
            self.calls_after += 1
            raise self.exc("armed")


def arm_during_record(guard):
    """The stop arrives while the route record is being saved."""
    original = InstructionNoaWake._record_attempt
    def record(wake, rec):
        original(wake, rec)
        guard.armed = True
    return patch.object(InstructionNoaWake, "_record_attempt", record)


class StopWhileRecording:
    EXC = (StopRequested, TimeBudgetExceeded)

    def setup_item(self):
        raise NotImplementedError

    def stored(self):
        raise NotImplementedError

    def assert_not_sent(self, item):
        raise NotImplementedError

    def interrupted(self, exc):
        item = self.setup_item()
        popen = FakePopen()
        guard = ArmedGuard(self.p, exc)
        client = self.use_local(popen, guard)
        with arm_during_record(guard):
            self.begin()
            with self.assertRaises(exc) as cm:
                InstructionNoaWake(self.p, client).run()
        self.assertTrue(cm.exception.not_sent)
        self.assertFalse(getattr(cm.exception, "uncertain", False))
        self.assertGreaterEqual(guard.calls_after, 1)
        self.assertFalse(popen.calls)
        self.assert_not_sent(item)
        self.restart()  # what is on disk agrees
        self.assert_not_sent(self.stored())
        return popen

    def test_stop_or_timeout_while_recording_is_not_sent(self):
        for exc in self.EXC:
            with self.subTest(exc=exc.__name__):
                tw_setup = type(self).setUp
                tw_setup(self)
                self.interrupted(exc)

    def test_after_the_stop_it_is_delivered_once_on_the_new_route(self):
        for exc in self.EXC:
            with self.subTest(exc=exc.__name__):
                type(self).setUp(self)
                popen = self.interrupted(exc)
                self.clock.advance(1)
                self.client.responses = [NoaUncertain()]
                self.run_with(self.use_routine())
                item = self.stored()
                self.assertEqual((item["status"], item["transport"]), ("held", "routine"))
                self.assertNotIn("local_ref", item)
                for _ in range(2):  # held is never resent on either route
                    self.clock.advance(1)
                    self.run_with(self.use_routine())
                    self.run_with(self.use_local(popen))
                self.assertEqual(len(self.client.calls), 1)
                self.assertFalse(popen.calls)
                self.assertNotIn(MAC, self.notice_text())

    def test_after_the_stop_local_delivers_exactly_once(self):
        popen = self.interrupted(StopRequested)
        self.clock.advance(1)
        for _ in range(3):
            self.run_with(self.use_local(popen))
        item = self.stored()
        self.assertEqual((item["status"], item["transport"]), ("done", "local"))
        self.assertEqual(len(popen.calls), 1)

    def test_unknown_outcome_after_recording_is_still_held_and_never_resent(self):
        self.setup_item()
        self.run_with(self.use_local(ExplodingPopen()))
        item = self.stored()
        self.assertEqual((item["status"], item["transport"]), ("held", "local"))
        popen = FakePopen()
        for _ in range(3):
            self.clock.advance(1)
            self.run_with(self.use_local(popen))
        self.assertFalse(popen.calls)
        self.assertEqual(self.stored()["status"], "held")


class HumanStopWhileRecording(StopWhileRecording, HumanBase):
    def setup_item(self):
        self.message(1)
        return None

    def stored(self):
        return self.items()[0]

    def assert_not_sent(self, item):
        item = item or self.items()[0]
        self.assertEqual((item["status"], item["attempt_count"]), ("queued", 0))
        self.assertNotIn("transport", item)
        self.assertNotIn("local_ref", item)
        self.assertEqual(self.p.state["noa_wake"]["attempts"], [])

    def notice_text(self):
        text = "\n".join(self.human_notices())
        self.assertIn(ROUTINE_HELD, text)
        return text


class InstructionStopWhileRecording(StopWhileRecording, InstructionBase):
    def setup_item(self):
        self.src = self.source()
        return self.src["noa_instruction_wake"]

    def stored(self):
        return self.p.state["outbox"]["instruction:" + self.src["request_id"]]["noa_instruction_wake"]

    def assert_not_sent(self, item):
        self.assertEqual((item["status"], item["attempt_at"]), ("queued", None))
        self.assertNotIn("transport", item)
        self.assertNotIn("local_ref", item)

    def notice_text(self):
        return self.instruction_notice(self.src)


class DirectClient(Base):
    def test_guard_runs_after_the_route_record_and_before_spawn(self):
        order = []
        state = {"stop": False}
        def guard():
            order.append("guard")
            if state["stop"]:
                raise StopRequested("stop")
        client = self.client(guard)
        def on_attempt(record):
            order.append("record")
            state["stop"] = True
        client.on_attempt = on_attempt
        with self.assertRaises(StopRequested) as cm:
            client.fire(MSG)
        self.assertTrue(cm.exception.not_sent)
        self.assertEqual(order[-2:], ["record", "guard"])
        self.assertFalse(self.popen.calls)

    def test_stop_raised_by_the_record_itself_is_not_sent(self):
        client = self.client()
        def on_attempt(record):
            raise TimeBudgetExceeded("late")
        client.on_attempt = on_attempt
        with self.assertRaises(TimeBudgetExceeded) as cm:
            client.fire(MSG)
        self.assertTrue(cm.exception.not_sent)
        self.assertFalse(self.popen.calls)


if __name__ == "__main__":
    unittest.main()
