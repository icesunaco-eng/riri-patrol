"""Offline U-02/U-04/U-05/U-06 core completion and recovery regressions."""
from dataclasses import replace
from unittest.mock import patch
import unittest

import test_patrol_flow as flows
from riri.errors import RemoteError, StopRequested, TimeBudgetExceeded
from riri.eventlog import iso
from riri.noa import NoaWake
from riri.patrol import Patrol
from riri.state import StateStore


class ReviewV08Patrol(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state

    def deadline(self):
        exc = TimeBudgetExceeded('offline boundary')
        exc.not_sent = True
        return exc

    def expire(self):
        with patch.object(self.notion, 'read_blocks', side_effect=self.deadline()):
            result = self.run_once(live=True)
        self.clock.advance()
        return result

    def core_error(self):
        with patch.object(self.notion, 'read_blocks', side_effect=RemoteError('offline failure')):
            self.assertEqual(self.run_once(live=True)['status'], 'error')
        self.clock.advance()

    def recoveries(self):
        return [x for x in self.state(True)['outbox'].values()
                if x['kind'] in ('recovery', 'deadline_recovery')]

    def test_noa_auth_failure_six_healthy_core_runs_recover_once(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.run_once(live=True)
        state = self.state(True)
        state['noa_wake']['failure_reason'] = 'authentication'
        state['noa_wake']['failure_serial'] = 1
        state['noa_wake']['failure_episodes'] = {'authentication': {
            'serial': 1, 'success_since': None, 'last_success': None}}
        StateStore(self.root, 'live').save(state)
        self.core_error()
        with patch.object(NoaWake, 'run', return_value=None):
            for _ in range(6):
                self.run_once(live=True)
                self.clock.advance()
        self.assertIsNone(self.state(True)['last_error'])
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)
        self.assertEqual(self.state(True)['noa_wake']['failure_reason'], 'authentication')

    def test_long_pause_first_deadline_quiet_then_episode_time_threshold(self):
        self.run_once(live=True)
        completed = self.state(True)['last_run_at']
        self.clock.advance(8 * 3600)
        self.expire()
        self.assertEqual(self.state(True)['last_run_at'], completed)
        self.assertIsNone(self.state(True)['deadline_state']['alert_key'])
        self.clock.advance(1500)
        self.expire()
        self.assertEqual(self.state(True)['deadline_state']['consecutive'], 2)
        self.assertIsNotNone(self.state(True)['deadline_state']['alert_key'])

    def test_checkpoint_before_optional_stop_and_deadline(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        for exc, status in ((self.deadline(), 'deadline'), (StopRequested('stop'), 'stopped')):
            self.clock.advance()
            expected = iso(self.clock())
            def optional(_):
                self.clock.advance(30)
                raise exc
            with patch.object(Patrol, 'optional_noa', side_effect=optional):
                self.assertEqual(self.run_once(live=True)['status'], status)
            state = self.state(True)
            self.assertEqual(state['last_run_at'], expected)
            self.assertEqual(state['deadline_state']['consecutive'], 0)
            self.assertIsNone(state['last_error'])

    def test_post_core_unknown_deadline_not_reclassified(self):
        exc = TimeBudgetExceeded('uncertain response')
        exc.uncertain = True
        with patch.object(Patrol, 'optional_noa', side_effect=exc):
            self.assertEqual(self.run_once(live=True)['status'], 'error')
        self.assertIsNotNone(self.state(True)['last_run_at'])
        self.assertEqual(self.state(True)['core_error_serial'], 0)
        self.assertIsNone(self.state(True)['last_error'])

    def prepare_combined(self):
        self.core_error()
        for _ in range(3): self.expire()
        self.run_once(live=True)
        self.assertEqual(len(self.recoveries()), 1)
        item = self.recoveries()[0]
        self.assertEqual(item['kind'], 'deadline_recovery')
        self.assertEqual(item['recovery_error_serial'], self.state(True)['core_error_serial'])
        self.assertEqual(item['status'], 'queued')

    def test_error_then_deadlines_single_durable_recovery(self):
        self.prepare_combined()
        for _ in range(4): self.run_once(live=True)
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)
        self.assertIsNone(self.state(True)['last_error'])

    def test_combined_recovery_crash_reconciles_without_duplicate(self):
        self.prepare_combined()
        state = self.state(True)
        item = next(x for x in state['outbox'].values() if x['kind'] == 'deadline_recovery')
        item['status'] = 'sending'
        self.slack.history.append((item['channel'], item['text'], '999.1'))
        StateStore(self.root, 'live').save(state)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.recoveries()), 1)
        self.assertEqual(self.recoveries()[0]['status'], 'posted')
        self.assertIsNone(self.state(True)['last_error'])
        self.assertFalse(any('直りました' in x[1] for x in self.slack.posts))

    def test_combined_uncertain_absent_stays_held_without_ack(self):
        self.prepare_combined()
        self.slack.error = RemoteError('lost response', uncertain=True)
        for _ in range(4): self.run_once(live=True)
        self.assertEqual(len(self.recoveries()), 1)
        self.assertEqual(self.recoveries()[0]['status'], 'held')
        self.assertIsNotNone(self.state(True)['last_error'])
        self.assertFalse(any('直りました' in x[1] for x in self.slack.posts))

    def test_combined_uncertain_found_ack_once(self):
        self.prepare_combined()
        original = self.slack.post
        def lost_response(channel, text, **kwargs):
            result = original(channel, text, **kwargs)
            if '直りました' in text:
                raise RemoteError('lost response', uncertain=True)
            return result
        with patch.object(self.slack, 'post', side_effect=lost_response):
            self.run_once(live=True)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.recoveries()), 1)
        self.assertIsNone(self.state(True)['last_error'])
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)

    def test_noa_new_hold_after_checkpoint_blocks_queued_recovery(self):
        self.core_error()
        def held(_):
            # Existing notice uncertainty must retain the same recovery gate.
            state = self.state(True)
            state['outbox']['noa-component:test'] = {'kind': 'noa_notice', 'status': 'held'}
            StateStore(self.root, 'live').save(state)
        with patch.object(Patrol, 'optional_noa', side_effect=held):
            self.run_once(live=True)
        for _ in range(4): self.run_once(live=True)
        self.assertIsNotNone(self.state(True)['last_error'])
        self.assertFalse(any('直りました' in x[1] for x in self.slack.posts))

    def test_new_error_retires_only_known_unsent_stale_recovery(self):
        self.core_error()
        self.run_once(live=True)
        self.assertEqual(self.recoveries()[0]['status'], 'queued')
        # Auth fails before outbox drain, so the first recovery is definitely unsent.
        with patch.object(self.slack, 'auth_test', side_effect=RemoteError('offline auth')):
            self.assertEqual(self.run_once(live=True)['status'], 'error')
        self.assertEqual(self.recoveries()[0]['status'], 'cancelled')
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)
        self.assertIsNone(self.state(True)['last_error'])

    def test_legacy_unversioned_queued_recovery_is_not_sent(self):
        self.run_once(live=True)
        state = self.state(True)
        state['last_error'] = 'RemoteError'
        state['outbox']['recovery:legacy'] = {
            'kind': 'recovery', 'status': 'queued', 'text': '直りました old',
            'channel': self.cfg.report_channel, 'marker': 'legacy'}
        StateStore(self.root, 'live').save(state)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['recovery:legacy']['status'], 'cancelled')
        self.assertFalse(any('old' in x[1] for x in self.slack.posts))
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)

    def test_stale_summary_hold_notice_frozen_while_noa_off(self):
        self.run_once(live=True)
        state = self.state(True)
        for status in ('queued', 'sending'):
            state['outbox']['stale-hold:' + status] = {
                'kind': 'hold_notice', 'status': status, 'held_key': 'noa-stale:1',
                'text': 'check summary', 'channel': self.cfg.report_channel,
                'marker': 'rp:stale-' + status}
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.slack.find_calls, 0)
        self.assertEqual(self.slack.posts, [])
        for status in ('queued', 'sending'):
            self.assertEqual(self.state(True)['outbox']['stale-hold:' + status]['status'], status)

    def test_post_cap_queued_generic_merges_with_deadline_completion(self):
        self.core_error()
        self.run_once(live=True)
        self.cfg = replace(self.cfg, max_posts=0)
        for _ in range(3): self.expire()
        self.run_once(live=True)
        recoveries = self.recoveries()
        self.assertEqual([x['kind'] for x in recoveries if x['status'] == 'queued'], ['deadline_recovery'])
        self.assertEqual([x['reason'] for x in recoveries if x['status'] == 'cancelled'], ['merged_deadline_recovery'])
        self.cfg = replace(self.cfg, max_posts=20)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)
        self.assertIsNone(self.state(True)['last_error'])

    def test_held_alert_keeps_combined_recovery_unacknowledged_until_resolved(self):
        self.core_error()
        for _ in range(3): self.expire()
        self.slack.error = RemoteError('alert response lost', uncertain=True)
        self.run_once(live=True)
        self.assertEqual(len(self.recoveries()), 1)
        self.assertIn('recovery_error_serial', self.recoveries()[0])
        for _ in range(2): self.run_once(live=True)
        self.assertIsNotNone(self.state(True)['last_error'])
        self.assertFalse(any('直りました' in x[1] for x in self.slack.posts))
        state = self.state(True)
        alert = next(x for x in state['outbox'].values() if x['kind'] == 'deadline_alert')
        alert['status'] = 'posted'  # Simulated explicit operator resolution.
        StateStore(self.root, 'live').save(state)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('直りました' in x[1] for x in self.slack.posts), 1)
        self.assertIsNone(self.state(True)['last_error'])

    def test_invalid_serial_fails_closed(self):
        self.run_once(live=True)
        state = self.state(True)
        for value in (-1, True, '1'):
            state['core_error_serial'] = value
            StateStore(self.root, 'live').save(state)
            self.assertEqual(self.run_once(live=True)['status'], 'state_error')
