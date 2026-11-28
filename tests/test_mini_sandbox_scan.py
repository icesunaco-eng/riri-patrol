"""Keep late W5 candidates until a sandbox decision or confirmed disappearance."""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from riri import mini_run
from riri.mini_run import Descendants, Mates, run_mates
from tests import test_mini_lineage


class InconclusiveSandboxScanTests(unittest.TestCase):
    PARENT = 401
    CHILD = 402
    FIRST = 'Sat Nov 28 12:00:01 2026'
    LATER = 'Sat Nov 28 12:00:02 2026'

    @contextmanager
    def scan(self, tables, answers):
        tracker = Descendants(100, run_dir='/dummy-run', calibrated=True, births={1000})
        tracker.mark = 1000
        tracker.root_done = True
        lineage = {self.PARENT: (1010, 1), self.CHILD: (1011, 1)}
        with test_mini_lineage.world(lineage), \
                patch.object(mini_run, '_process_table', side_effect=tables) as table, \
                patch.object(mini_run, '_list_ps', return_value=Mates()), \
                patch.object(mini_run, '_list_linux', return_value=Mates()), \
                patch.object(mini_run, 'sandbox_probes', return_value=(b'w', b'o', b'i')), \
                patch.object(mini_run, 'run_sandbox_state', side_effect=answers) as state, \
                patch.object(mini_run.os, 'getsid', side_effect=lambda pid: pid):
            yield tracker, table, state

    def test_alive_unknown_is_held_until_a_later_probe_confirms_it(self):
        row = {self.PARENT: (1, self.FIRST)}
        with self.scan([row, row, row], [None, 'run']) as (tracker, _, state):
            first = run_mates('test-w5', 100, tracker)
            self.assertEqual(first, {self.PARENT})
            self.assertEqual(first.unsure, {self.PARENT})
            self.assertIsNone(first.starts[self.PARENT])
            again = run_mates('test-w5', 100, tracker)
            self.assertEqual(again, {self.PARENT})
            self.assertEqual(again.starts[self.PARENT], self.FIRST)
            self.assertEqual(state.call_count, 2)

    def test_an_uncertain_process_that_has_gone_does_not_hold_the_run(self):
        with self.scan([{self.PARENT: (1, self.FIRST)}, {}], [None]) as (tracker, table, _):
            self.assertEqual(run_mates('test-w5', 100, tracker), set())
            self.assertEqual(table.call_count, 2)

    def test_a_new_member_is_checked_even_when_the_first_pid_exited(self):
        old = {self.PARENT: (1, self.FIRST)}
        new = {self.CHILD: (1, self.LATER)}
        with self.scan([old, new], [None, 'run']) as (tracker, _, state):
            found = run_mates('test-w5', 100, tracker)
            self.assertEqual(found, {self.CHILD})
            self.assertEqual(found.starts[self.CHILD], self.LATER)
            self.assertEqual([call.args[0] for call in state.call_args_list], [self.PARENT, self.CHILD])

    def test_confirmed_non_sandbox_process_does_not_hold_or_need_a_second_table(self):
        with self.scan([{self.PARENT: (1, self.FIRST)}], ['none']) as (tracker, table, _):
            self.assertEqual(run_mates('test-w5', 100, tracker), set())
            self.assertEqual(table.call_count, 1)
