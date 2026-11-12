"""U-05: local recovery resolution preserves newer and uncertain error state."""
import unittest

from riri.errors import SafetyError
import test_resolve_kinds as resolve_fixtures


class ReviewV08Resolve(unittest.TestCase):
    setUp = resolve_fixtures.ResolveKindsTests.setUp
    put_notice = resolve_fixtures.ResolveKindsTests.put_notice
    resolve = resolve_fixtures.ResolveKindsTests.resolve

    def test_current_generic_and_combined_notice_acknowledge_current_error(self):
        for kind in ("recovery", "deadline_recovery"):
            with self.subTest(kind=kind):
                self.setUp()
                self.state["core_error_serial"] = 3
                self.state["last_error"] = "RemoteError"
                self.put_notice(kind, recovery_error_serial=3)
                self.resolve()
                self.assertIsNone(self.state["last_error"])
                self.assertTrue(self.state["outbox"]["target"]["manually_confirmed"])
                self.assertIsNone(self.state["outbox"]["target"]["ts"])

    def test_stale_and_legacy_notice_ack_never_clear_newer_error(self):
        for kind in ("recovery", "deadline_recovery"):
            for metadata in ({"recovery_error_serial": 2}, {}):
                with self.subTest(kind=kind, metadata=metadata):
                    self.setUp()
                    self.state["core_error_serial"] = 3
                    self.state["last_error"] = "NewError"
                    self.put_notice(kind, **metadata)
                    self.resolve()
                    self.assertEqual(self.state["last_error"], "NewError")

    def test_cancel_or_retry_does_not_claim_recovery_delivery(self):
        for kind in ("recovery", "deadline_recovery"):
            for outcome in ("cancel", "not_posted"):
                with self.subTest(kind=kind, outcome=outcome):
                    self.setUp()
                    self.state["core_error_serial"] = 1
                    self.state["last_error"] = "RemoteError"
                    self.put_notice(kind, recovery_error_serial=1)
                    self.resolve(outcome=outcome)
                    self.assertEqual(self.state["last_error"], "RemoteError")

    def test_future_recovery_serial_rejected_without_reset(self):
        self.state["core_error_serial"] = 2
        self.state["last_error"] = "RemoteError"
        self.put_notice("recovery", recovery_error_serial=3)
        with self.assertRaises(SafetyError):
            self.resolve()
        self.assertEqual(self.state["last_error"], "RemoteError")
