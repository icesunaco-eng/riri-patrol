"""A known-unsent duplicate is not an uncertain Slack post to retry."""
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest import TestCase

from riri.config import Config
from riri.errors import SafetyError
from riri.resolve import held_items, resolve_held
from riri.state import initial_state, StateStore


class DuplicateResolutionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(Config(Path(self.tmp.name)), mode="live")
        self.state = initial_state("live")
        self.rid = "🤍20990101-2"
        self.state["requests"][self.rid] = {"status": "held", "hold_reason": "content_duplicate",
                                             "duplicate_of": "🤍20990101-1"}
        self.state["outbox"]["instruction:" + self.rid] = {
            "status": "held", "kind": "instruction", "hold_reason": "content_duplicate"}
        StateStore(self.cfg.root, "live").save(self.state)
        self.state = StateStore(self.cfg.root, "live").load()

    def test_duplicate_is_visible_without_posted_not_posted_resolution(self):
        item = held_items(self.state)[0]
        self.assertEqual(item["reason"], "content_duplicate")
        self.assertNotIn("not_posted", item["allowed_outcomes"])

    def test_generic_resolution_cannot_release_duplicate(self):
        for outcome in ("posted", "not_posted"):
            with self.subTest(outcome=outcome), self.assertRaises(SafetyError):
                resolve_held(self.cfg, "instruction", self.rid, outcome)
        self.assertEqual(StateStore(self.cfg.root, "live").load(), self.state)
