"""みにの再レビュー R-01〜R-04 の反例（通信なし・偽の起動関数・実Claude起動なし）。

R-01 は Path.glob が列挙エラーを内部で抑制する版（Python 3.14 で確認）に依存するため、
glob 自体ではなく、その内側の列挙層（os.scandir）と stat 層へエラーを注入する。
"""
from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from riri.errors import StopRequested
from riri.eventlog import iso
from riri.instruction_noa import InstructionNoaWake, ROUTINE_HELD
from riri.local_noa import LocalNoaClient
from riri.noa import CHANNEL, NoaRejected, NoaUncertain
from tests import test_instruction_noa as ti
from tests import test_noa_wake as tw
from tests.test_local_noa import FakePopen, MSG
from tests.test_local_noa_review import Base, ExplodingPopen

BOTH = "起動経路を特定できないため"
MAC = "MacのClaude Code"


class R01ListingErrors(Base):
    """Listing/stat failures are never "no match": no spawn, SearchIncomplete."""

    def setUp(self):
        super().setUp()
        self.target = self.transcript("sess-target-01", self.noa, "🤍20261113-6 の続き", 100)

    def assert_incomplete(self):
        with self.assertRaises(NoaRejected) as cm:
            self.client().fire(MSG)
        self.assertEqual(cm.exception.reason, "local_search_incomplete")
        self.assertFalse(self.popen.calls)

    def deny_scandir(self, denied, exc=PermissionError):
        real = os.scandir
        def fake(path="."):
            if os.fspath(path).rstrip("/") == str(denied):
                raise exc(errno.EACCES, "denied")
            return real(path)
        return patch("os.scandir", fake)

    def deny_stat(self, denied, exc=PermissionError, names=("stat", "lstat")):
        patches = []
        for name in names:
            real = getattr(os, name)
            def fake(path, *a, _real=real, **kw):
                if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(denied):
                    raise exc(errno.EACCES, "denied")
                return _real(path, *a, **kw)
            patches.append(patch("os." + name, fake))
        return patches

    def test_baseline_match_is_resumed(self):
        self.assertEqual(self.client().fire(MSG)["session_id"], "sess-target-01")

    def test_subfolder_listing_permission_error_is_incomplete(self):
        with self.deny_scandir(self.projects / "p"):
            self.assert_incomplete()

    def test_root_listing_permission_error_is_incomplete(self):
        with self.deny_scandir(self.projects):
            self.assert_incomplete()

    def test_listing_io_error_is_incomplete(self):
        with self.deny_scandir(self.projects / "p", exc=lambda *_: OSError(errno.EIO, "io")):
            self.assert_incomplete()

    def test_error_in_the_middle_of_a_listing_is_incomplete(self):
        real = os.scandir
        denied = str(self.projects / "p")
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self): raise OSError(errno.EIO, "io")
            def close(self): pass
        def fake(path="."):
            return Broken() if os.fspath(path).rstrip("/") == denied else real(path)
        with patch("os.scandir", fake):
            self.assert_incomplete()

    def test_other_folder_denied_while_match_is_elsewhere_is_incomplete(self):
        # The denied folder could hold a newer match: the found one is not trusted.
        self.transcript("sess-other-0001", self.noa, "無関係", 50, folder="q")
        with self.deny_scandir(self.projects / "q"):
            self.assert_incomplete()

    def test_file_stat_permission_error_is_incomplete(self):
        ps = self.deny_stat(self.target)
        with ps[0], ps[1]:
            self.assert_incomplete()

    def test_folder_stat_permission_error_is_incomplete(self):
        ps = self.deny_stat(self.projects / "p")
        with ps[0], ps[1]:
            self.assert_incomplete()

    def test_root_stat_permission_error_is_incomplete(self):
        ps = self.deny_stat(self.projects)
        with ps[0], ps[1]:
            self.assert_incomplete()

    def test_confirmed_missing_root_starts_new(self):
        self.cfg  # root really does not exist: a confirmed absence, not a failure
        client = LocalNoaClient(self.cfg, guard=lambda: None, clock=self.client().clock,
                                projects_dir=self.projects / "missing", popen=self.popen)
        self.assertEqual(client.fire(MSG)["mode"], "new")

    def test_confirmed_vanished_entries_are_skipped_not_failures(self):
        gone = self.transcript("sess-gone-0001", self.noa, "🤍20261113-6", 900)
        (self.projects / "dangling").symlink_to(self.projects / "nowhere")
        ps = self.deny_stat(gone, exc=FileNotFoundError)
        with ps[0], ps[1]:
            self.assertEqual(self.client().fire(MSG)["session_id"], "sess-target-01")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_real_unlistable_folder_is_incomplete(self):
        folder = self.projects / "p"
        folder.chmod(0o000)
        self.addCleanup(folder.chmod, 0o700)
        self.assert_incomplete()

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_real_unlistable_root_is_incomplete(self):
        self.projects.chmod(0o000)
        self.addCleanup(self.projects.chmod, 0o700)
        self.assert_incomplete()


def not_sent_stop():
    exc = StopRequested("stop")
    exc.not_sent = True
    return exc


class Mixin:
    """Helpers shared by the human-mention and generated-instruction fixtures."""

    def local_cfg(self):
        t = Path(self.tmp.name)
        noa = t / "のあ"; noa.mkdir(exist_ok=True)
        exe = t / "claude"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o700)
        return replace(self.p.config, noa_transport="local", noa_local_dir=str(noa),
                       noa_claude_bin=str(exe), noa_local_permission_mode="bypassPermissions")

    def use_local(self, popen, guard=None):
        self.p.config = self.local_cfg()
        return LocalNoaClient(self.p.config, guard=guard or self.p.check_time, clock=self.clock,
                              projects_dir=Path(self.tmp.name) / "none", popen=popen)

    def use_routine(self):
        self.p.config = replace(self.p.config, noa_transport="routine")
        return self.client

    def run_with(self, client):
        self.begin()
        InstructionNoaWake(self.p, client).run()

    def human_notices(self):
        return [x["text"] for k, x in self.p.state["outbox"].items() if k.startswith("noa-notice:")]

    def old_sending_human(self, number=1):
        """A human item left in sending by a version that recorded no route."""
        m = self.message(number)
        key = CHANNEL + ":" + m["ts"]
        self.p.state["noa_wake"]["items"][key] = dict(
            channel=CHANNEL, ts=m["ts"], status="sending", attempt_count=1, source_user=m["user"],
            source_digest=hashlib.sha256(m["text"].encode()).hexdigest())
        self.p.state["noa_wake"]["attempts"].append(iso(self.clock()))
        self.p.save()
        return self.p.state["noa_wake"]["items"][key]

    def old_instruction(self, n, status):
        src = self.source(n)
        src["noa_instruction_wake"] = {"status": status, "attempt_at": iso(self.clock())}
        if status == "held":
            src["noa_instruction_wake"]["reason"] = "interrupted_send"
        self.p.save()
        return src

    def instruction_notice(self, src):
        return self.p.state["outbox"]["noa-instruction:" + src["request_id"]]["text"]


class HumanBase(Mixin, unittest.TestCase):
    setUp = tw.WakeTests.setUp
    new_patrol = tw.WakeTests.new_patrol
    begin = tw.WakeTests.begin
    message = tw.WakeTests.message
    items = tw.WakeTests.items
    restart = tw.WakeTests.restart


class InstructionBase(Mixin, unittest.TestCase):
    setUp = ti.InstructionWakeTests.setUp
    new_patrol = ti.InstructionWakeTests.new_patrol
    begin = ti.InstructionWakeTests.begin
    message = ti.InstructionWakeTests.message
    restart = ti.InstructionWakeTests.restart
    source = ti.InstructionWakeTests.source


class R02MissingTransport(HumanBase):
    """A held record without a transport is route-unknown under either setting."""

    def check_human(self, client):
        item = self.old_sending_human()
        self.run_with(client)
        self.assertEqual(item["status"], "held")
        self.assertNotIn("transport", item)
        text = "\n".join(self.human_notices())
        self.assertIn(BOTH, text)
        self.assertIn("routine", text)
        self.assertIn(MAC, text)
        self.assertNotIn(ROUTINE_HELD, text)

    def test_human_missing_transport_under_routine_setting(self):
        self.check_human(self.use_routine())

    def test_human_missing_transport_under_local_setting(self):
        self.check_human(self.use_local(FakePopen()))

    def test_guidance_function_for_both_settings(self):
        for client in (self.use_routine(), self.use_local(FakePopen())):
            wake = InstructionNoaWake(self.p, client)
            self.assertIn(BOTH, wake._held_guidance({"status": "held"}))
            self.assertIsNone(wake._held_guidance({"status": "held", "transport": "routine"}))
            self.assertIn(MAC, wake._held_guidance({"status": "held", "transport": "local"}))
            self.assertNotIn("routine", wake._held_guidance({"status": "held", "transport": "local"}))

    def test_recorded_routine_keeps_the_original_text(self):
        self.client.responses = [NoaUncertain()]
        self.message(1)
        self.run_with(self.use_routine())
        self.assertEqual(self.items()[0]["transport"], "routine")
        self.assertTrue(any(ROUTINE_HELD + "\n" in t for t in self.human_notices()))


class R02MissingTransportInstruction(InstructionBase):
    def check(self, client, status):
        src = self.old_instruction(1, status)
        self.run_with(client)
        self.assertEqual(src["noa_instruction_wake"]["status"], "held")
        self.assertNotIn("transport", src["noa_instruction_wake"])
        text = self.instruction_notice(src)
        self.assertIn("確認先：" + BOTH, text)
        self.assertIn("routine", text)
        self.assertIn(MAC, text)

    def test_old_held_under_routine_setting(self):
        self.check(self.use_routine(), "held")

    def test_old_held_under_local_setting(self):
        self.check(self.use_local(FakePopen()), "held")

    def test_old_sending_under_routine_setting(self):
        self.check(self.use_routine(), "sending")

    def test_old_sending_under_local_setting(self):
        self.check(self.use_local(FakePopen()), "sending")

    def test_recorded_routine_keeps_the_original_text(self):
        self.client.responses = [NoaUncertain()]
        src = self.source()
        self.run_with(self.use_routine())
        self.assertEqual(src["noa_instruction_wake"]["transport"], "routine")
        self.assertIn("\nりりからの指示による、のあの起動を確認できません。自動再送しません。参照：rp:",
                      self.instruction_notice(src))


class StopGuard:
    def __init__(self, patrol):
        self.p, self.on = patrol, False
    def __call__(self):
        self.p.check_time()
        if self.on:
            raise StopRequested("stop")


class R03Human(HumanBase):
    """Known-not-sent STOP -> transport changed -> unknown outcome, both directions."""

    def test_routine_stop_then_local_unknown_points_to_mac(self):
        self.client.responses = [not_sent_stop()]
        self.message(1)
        with self.assertRaises(StopRequested):
            self.run_with(self.use_routine())
        item = self.items()[0]
        self.assertEqual(item["status"], "queued")
        self.assertNotIn("transport", item)
        self.assertNotIn("local_ref", item)
        self.run_with(self.use_local(ExplodingPopen()))
        self.assertEqual((item["status"], item["transport"]), ("held", "local"))
        self.assertTrue(item["local_ref"].startswith("logs/noa-local/"))
        text = "\n".join(self.human_notices())
        self.assertIn(MAC, text)
        self.assertIn(item["local_ref"], text)
        self.assertNotIn("routine", text)
        self.assertEqual(len(self.client.calls), 1)  # the stopped routine call only; never resent

    def test_local_stop_then_routine_unknown_keeps_no_local_ref(self):
        guard = StopGuard(self.p)
        popen = FakePopen()
        client = self.use_local(popen, guard)
        self.message(1)
        guard.on = True
        with self.assertRaises(StopRequested):
            self.run_with(client)
        item = self.items()[0]
        self.assertEqual(item["status"], "queued")
        self.assertNotIn("transport", item)
        self.assertNotIn("local_ref", item)
        self.client.responses = [NoaUncertain()]
        self.run_with(self.use_routine())
        self.assertEqual((item["status"], item["transport"]), ("held", "routine"))
        self.assertNotIn("local_ref", item)
        text = "\n".join(self.human_notices())
        self.assertIn(ROUTINE_HELD, text)
        self.assertNotIn(MAC, text)
        self.assertFalse(popen.calls)

    def test_stale_local_route_on_waiting_item_is_replaced_by_routine_attempt(self):
        # State as an earlier version could leave it: queued, but with a local route.
        self.message(1)
        self.client.responses = [not_sent_stop()]
        with self.assertRaises(StopRequested):
            self.run_with(self.use_routine())
        item = self.items()[0]
        item.update(transport="local", local_ref="logs/noa-local/OLD"); self.p.save()
        self.client.responses = [NoaUncertain()]
        self.run_with(self.use_routine())
        self.assertEqual((item["status"], item["transport"]), ("held", "routine"))
        self.assertNotIn("local_ref", item)
        self.assertNotIn("OLD", "\n".join(self.human_notices()))

    def test_stale_route_is_gone_before_the_next_attempt_can_be_interrupted(self):
        # Crash after the sending fence but before this attempt's route is recorded:
        # the earlier attempt's route must not survive as if it were this one's.
        self.message(1)
        self.client.responses = [not_sent_stop()]
        with self.assertRaises(StopRequested):
            self.run_with(self.use_routine())
        item = self.items()[0]
        item.update(transport="routine"); self.p.save()  # as left by the fixed-version bug
        client = self.use_local(FakePopen())
        self.begin()
        wake = InstructionNoaWake(self.p, client)
        with patch.object(InstructionNoaWake, "_record_attempt", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                wake.run()
        self.restart()
        item = self.items()[0]
        self.assertEqual(item["status"], "sending")
        self.assertNotIn("transport", item)
        self.run_with(self.use_local(FakePopen()))
        item = self.items()[0]
        self.assertEqual(item["status"], "held")
        self.assertNotIn("transport", item)
        self.assertIn(BOTH, "\n".join(self.human_notices()))

    def test_rate_limited_retry_does_not_carry_the_old_route(self):
        from riri.noa import NoaRateLimit
        self.client.responses = [NoaRateLimit(1)]
        self.message(1)
        self.run_with(self.use_routine())
        item = self.items()[0]
        self.assertEqual(item["status"], "retry")
        self.assertNotIn("transport", item)
        self.clock.advance(5)
        self.run_with(self.use_local(ExplodingPopen()))
        self.assertEqual((item["status"], item["transport"]), ("held", "local"))
        self.assertIn("local_ref", item)


class R03Instruction(InstructionBase):
    def test_routine_stop_then_local_unknown_points_to_mac(self):
        self.client.responses = [not_sent_stop()]
        src = self.source()
        wake = src["noa_instruction_wake"]
        with self.assertRaises(StopRequested):
            self.run_with(self.use_routine())
        self.assertEqual((wake["status"], wake["attempt_at"]), ("queued", None))
        self.assertNotIn("transport", wake)
        self.run_with(self.use_local(ExplodingPopen()))
        self.assertEqual((wake["status"], wake["transport"]), ("held", "local"))
        self.assertTrue(wake["local_ref"].startswith("logs/noa-local/"))
        text = self.instruction_notice(src)
        self.assertIn("確認先：" + MAC, text)
        self.assertIn(wake["local_ref"], text)
        self.assertNotIn("routine", text)
        self.assertEqual(len(self.client.calls), 1)

    def test_local_stop_then_routine_unknown_keeps_no_local_ref(self):
        guard = StopGuard(self.p)
        popen = FakePopen()
        client = self.use_local(popen, guard)
        src = self.source()
        wake = src["noa_instruction_wake"]
        guard.on = True
        with self.assertRaises(StopRequested):
            self.run_with(client)
        self.assertEqual((wake["status"], wake["attempt_at"]), ("queued", None))
        self.assertNotIn("transport", wake)
        self.client.responses = [NoaUncertain()]
        self.run_with(self.use_routine())
        self.assertEqual((wake["status"], wake["transport"]), ("held", "routine"))
        self.assertNotIn("local_ref", wake)
        text = self.instruction_notice(src)
        self.assertNotIn("確認先", text)
        self.assertNotIn(MAC, text)
        self.assertFalse(popen.calls)

    def test_stale_routes_on_queued_item_are_replaced_in_both_directions(self):
        a = self.source(1)["noa_instruction_wake"]
        a.update(transport="local", local_ref="logs/noa-local/OLD"); self.p.save()
        self.client.responses = [NoaUncertain()]
        self.run_with(self.use_routine())
        self.assertEqual((a["status"], a["transport"]), ("held", "routine"))
        self.assertNotIn("local_ref", a)
        self.clock.advance(1)
        b = self.source(2)["noa_instruction_wake"]
        b.update(transport="routine"); self.p.save()
        self.run_with(self.use_local(ExplodingPopen()))
        self.assertEqual((b["status"], b["transport"]), ("held", "local"))
        self.assertNotEqual(b["local_ref"], "logs/noa-local/OLD")

    def test_stale_route_is_cleared_in_the_same_write_as_the_sending_fence(self):
        src = self.source()
        wake = src["noa_instruction_wake"]
        wake.update(transport="routine"); self.p.save()
        client = self.use_local(FakePopen())
        self.begin()
        runner = InstructionNoaWake(self.p, client)
        with patch.object(InstructionNoaWake, "_record_attempt", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.run()
        self.restart()
        wake = self.p.state["outbox"]["instruction:" + src["request_id"]]["noa_instruction_wake"]
        self.assertEqual(wake["status"], "sending")
        self.assertNotIn("transport", wake)


class R04OnlyTheAttemptedItem(InstructionBase):
    """B's route/log reference is recorded on B only; an old sending A is held
    separately and its route is never guessed."""

    def assert_a_untouched(self, a):
        self.assertEqual(a["status"], "held")
        self.assertNotIn("transport", a)
        self.assertNotIn("local_ref", a)

    def test_old_human_sending_a_and_new_instruction_b_local(self):
        a = self.old_sending_human(7)
        b_src = self.source(1)
        b = b_src["noa_instruction_wake"]
        popen = FakePopen()
        self.run_with(self.use_local(popen))
        self.assertEqual(len(popen.calls), 1)
        self.assertIn(b_src["request_id"], popen.calls[0][2])
        self.assertEqual((b["status"], b["transport"]), ("done", "local"))
        self.assertTrue(b["local_ref"].startswith("logs/noa-local/"))
        self.assert_a_untouched(a)
        self.clock.advance(1)
        self.run_with(self.use_local(popen))
        self.assert_a_untouched(a)
        text = "\n".join(self.human_notices())
        self.assertIn(BOTH, text)
        self.assertNotIn(b["local_ref"], text)
        self.assertEqual(len(popen.calls), 1)  # A is never sent again

    def test_old_human_sending_a_and_new_instruction_b_unknown_outcome(self):
        a = self.old_sending_human(7)
        b = self.source(1)["noa_instruction_wake"]
        self.run_with(self.use_local(ExplodingPopen()))
        self.assertEqual((b["status"], b["transport"]), ("held", "local"))
        self.assert_a_untouched(a)

    def test_old_human_sending_a_and_new_instruction_b_routine(self):
        a = self.old_sending_human(7)
        b = self.source(1)["noa_instruction_wake"]
        self.run_with(self.use_routine())
        self.assertEqual((b["status"], b["transport"]), ("done", "routine"))
        self.assert_a_untouched(a)
        self.assertEqual(len(self.client.calls), 1)

    def test_queued_b_before_old_sending_a_in_the_instruction_ledger_local(self):
        b_src = self.source(1)
        a_src = self.old_instruction(2, "sending")
        self.assertEqual([k for k in self.p.state["outbox"] if k.startswith("instruction:")],
                         ["instruction:" + b_src["request_id"], "instruction:" + a_src["request_id"]])
        popen = FakePopen()
        self.run_with(self.use_local(popen))
        a, b = a_src["noa_instruction_wake"], b_src["noa_instruction_wake"]
        self.assertEqual((b["status"], b["transport"]), ("done", "local"))
        self.assertIn("local_ref", b)
        self.assert_a_untouched(a)
        self.assertEqual(len(popen.calls), 1)
        self.assertNotIn(a_src["request_id"], popen.calls[0][2])
        self.clock.advance(1)
        self.run_with(self.use_local(popen))
        self.assertIn("確認先：" + BOTH, self.instruction_notice(a_src))
        self.assertNotIn(b["local_ref"], self.instruction_notice(a_src))
        self.assertEqual(len(popen.calls), 1)

    def test_queued_b_before_old_sending_a_in_the_instruction_ledger_routine(self):
        b_src = self.source(1)
        a_src = self.old_instruction(2, "sending")
        self.run_with(self.use_routine())
        self.assertEqual(b_src["noa_instruction_wake"]["transport"], "routine")
        self.assert_a_untouched(a_src["noa_instruction_wake"])
        self.assertEqual(len(self.client.calls), 1)

    def test_human_attempt_binds_only_to_its_own_message(self):
        # A held instruction with no route stays untouched while a human mention is sent.
        a_src = self.old_instruction(1, "held")
        self.message(3)
        popen = FakePopen()
        self.run_with(self.use_local(popen))
        human = list(self.p.state["noa_wake"]["items"].values())[0]
        self.assertEqual((human["status"], human["transport"]), ("done", "local"))
        self.assert_a_untouched(a_src["noa_instruction_wake"])

    def test_unidentified_attempt_records_nothing(self):
        a = self.old_sending_human(7)
        b = self.source(1)["noa_instruction_wake"]
        wake = InstructionNoaWake(self.p, self.use_local(FakePopen()))
        wake._record_attempt({"transport": "local", "ref": "logs/noa-local/X"})
        wake.client.fire("誰のものでもない本文")
        self.assertNotIn("transport", a)
        self.assertNotIn("transport", b)


if __name__ == "__main__":
    unittest.main()
