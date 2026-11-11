"""T-01 / T-03 offline regression: optional isolation and durable deadlines."""
from dataclasses import replace
from unittest.mock import patch
import unittest

from riri.errors import AccountMismatch, RemoteError, SafetyError, StopRequested, TimeBudgetExceeded
from riri.noa import NoaWake
from riri.patrol import Patrol
from riri.state import StateStore
import test_patrol_flow as flows


class ReviewV07Patrol(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def deadline(self):
        error = TimeBudgetExceeded('pre-I/O limit')
        error.not_sent = True
        return error

    def expire(self):
        with patch.object(self.notion, 'read_blocks', side_effect=self.deadline()):
            result = self.run_once(live=True)
        self.clock.advance()
        return result

    def notices_of(self, kind):
        return [v for v in self.state(True)['outbox'].values() if v['kind'] == kind]

    def test_single_deadline_stays_quiet_and_preserves_last_complete(self):
        self.run_once(live=True)
        before = self.state(True)['last_run_at']
        self.clock.advance()
        self.assertEqual(self.expire()['status'], 'deadline')
        self.assertEqual(self.state(True)['last_run_at'], before)
        self.run_once(live=True)
        self.run_once(live=True)
        self.assertEqual(self.slack.posts, [])
        self.assertIsNone(self.state(True)['last_error'])

    def test_36_deadlines_restart_priority_one_alert_then_real_recovery(self):
        for i in range(36):
            self.assertEqual(self.expire()['status'], 'deadline')
            self.assertEqual(len(self.slack.posts), int(i >= 3))
        state = self.state(True)
        self.assertEqual(state['deadline_state']['consecutive'], 36)
        self.assertIsNone(state['last_run_at'])
        self.assertEqual(len(self.notices_of('deadline_alert')), 1)
        self.assertEqual(self.notices_of('deadline_recovery'), [])
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['deadline_state']['consecutive'], 0)
        self.assertEqual(len(self.notices_of('deadline_recovery')), 1)
        self.run_once(live=True)
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 2)
        self.assertIn('時間切れが続いています', self.slack.posts[0][1])
        self.assertIn('直りました', self.slack.posts[1][1])
        self.assertFalse(any(x['kind'] in ('error', 'recovery') for x in self.state(True)['outbox'].values()))

    def test_thirty_minutes_since_complete_alerts_before_third_deadline(self):
        self.run_once(live=True)
        self.clock.advance(1800)
        self.expire()
        self.assertEqual(len(self.notices_of('deadline_alert')), 1)
        self.assertEqual(len(self.slack.posts), 0)

    def test_priority_notification_precedes_failing_heavy_read(self):
        for _ in range(3): self.expire()
        def read(_):
            self.assertEqual(len(self.slack.posts), 1)
            raise self.deadline()
        with patch.object(self.notion, 'read_blocks', side_effect=read):
            self.assertEqual(self.run_once(live=True)['status'], 'deadline')

    def test_stop_and_account_mismatch_never_deliver_pending_alert(self):
        for _ in range(3): self.expire()
        before = self.state(True)['deadline_state'].copy()
        self.cfg.stop.touch()
        self.assertEqual(self.run_once(live=True)['status'], 'stopped')
        self.cfg.stop.unlink()
        self.slack.reject_account = True
        self.assertEqual(self.run_once(live=True)['status'], 'account_mismatch')
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.state(True)['deadline_state'], before)
        self.assertEqual(self.notices_of('deadline_recovery'), [])

    def test_mid_run_stop_does_not_queue_recovery(self):
        for _ in range(3): self.expire()
        with patch.object(Patrol, 'flush', side_effect=StopRequested('stopped')):
            self.assertEqual(self.run_once(live=True)['status'], 'stopped')
        self.assertEqual(self.notices_of('deadline_recovery'), [])
        self.assertIsNone(self.state(True)['last_run_at'])

    def test_uncertain_notification_found_once_never_reposts(self):
        for _ in range(3): self.expire()
        original = self.slack.post
        def post(channel, text, **kwargs):
            original(channel, text, **kwargs)
            raise RemoteError('response missing', uncertain=True)
        with patch.object(self.slack, 'post', side_effect=post): self.expire()
        for _ in range(4): self.expire()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.notices_of('deadline_alert')[0]['status'], 'posted')

    def test_uncertain_notification_absent_is_held_not_retried(self):
        for _ in range(3): self.expire()
        self.slack.error = RemoteError('response missing', uncertain=True)
        self.expire()
        for _ in range(4): self.expire()
        self.assertEqual(self.notices_of('deadline_alert')[0]['status'], 'held')
        self.assertFalse(any('時間切れが続いています' in p[1] for p in self.slack.posts))
        self.run_once(live=True)
        self.run_once(live=True)
        self.assertFalse(any('直りました' in p[1] for p in self.slack.posts))

    def test_restart_sending_alert_reconciles_marker_without_duplicate(self):
        for _ in range(3): self.expire()
        state = self.state(True)
        item = next(v for v in state['outbox'].values() if v['kind'] == 'deadline_alert')
        item['status'] = 'sending'
        self.slack.history.append((item['channel'], item['text'], '100.000001'))
        StateStore(self.root, 'live').save(state)
        self.expire()
        self.assertEqual(self.notices_of('deadline_alert')[0]['status'], 'posted')
        self.assertEqual(self.slack.posts, [])

    def test_recovery_then_future_episode_gets_new_once_only_keys(self):
        for _ in range(3): self.expire()
        self.run_once(live=True)
        self.run_once(live=True)
        for _ in range(5): self.expire()
        self.run_once(live=True)
        self.run_once(live=True)
        self.assertEqual(len(self.notices_of('deadline_alert')), 2)
        self.assertEqual(len(self.notices_of('deadline_recovery')), 2)
        self.assertEqual(len(self.slack.posts), 4)

    def test_noa_failures_do_not_block_requests_reports_or_status(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.add_request()
        for error in (RemoteError('history failure'), SafetyError('permalink failure'), ValueError('config failure')):
            with patch.object(NoaWake, 'run', side_effect=error):
                self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'posted')
        self.add_report(name='うさ')
        with patch.object(NoaWake, 'run', side_effect=RemoteError('history failure')):
            self.run_once(live=True)
        self.assertIn(('status', 'request-1', '完了'), self.notion.writes)
        notices = self.notices_of('noa_notice')
        self.assertEqual(len(notices), 1)
        self.assertIsNotNone(self.state(True)['last_run_at'])
        self.assertIsNone(self.state(True)['last_error'])

    def test_noa_safety_boundaries_propagate_without_failure_notice(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        for error, status in ((StopRequested('stop'), 'stopped'),
                              (AccountMismatch('identity'), 'account_mismatch'),
                              (self.deadline(), 'deadline')):
            with patch.object(NoaWake, 'run', side_effect=error):
                self.assertEqual(self.run_once(live=True)['status'], status)
            self.assertEqual(self.notices_of('noa_notice'), [])
            self.assertIsNone(self.state(True)['last_run_at'])

    def test_noa_off_transition_persists_before_core_deadline(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.run_once(live=True)
        self.assertTrue(self.state(True)['noa_wake']['enabled'])
        self.cfg = replace(self.cfg, noa_wake_enabled=False)
        self.expire()
        self.assertFalse(self.state(True)['noa_wake']['enabled'])

    def test_legacy_state_migrates_without_resetting_outbox_or_quota(self):
        self.run_once(live=True)
        state = self.state(True)
        state.pop('deadline_state')
        state['noa_wake'] = {k: state['noa_wake'][k] for k in ('cutoff', 'watermark', 'items', 'attempts')}
        state['outbox']['saved'] = {'status': 'held', 'kind': 'notice'}
        StateStore(self.root, 'live').save(state)
        migrated = self.state(True)
        self.assertEqual(migrated['outbox']['saved'], state['outbox']['saved'])
        self.assertEqual(migrated['deadline_state']['consecutive'], 0)
        self.assertFalse(migrated['noa_wake']['enabled'])

    def test_expired_entry_budget_never_resets_to_send_pending_alert(self):
        for _ in range(3): self.expire()
        before_auth = self.slack.auth_calls
        ticks = iter([0, self.cfg.run_timeout])
        self.assertEqual(self.run_once(live=True, monotonic=lambda: next(ticks))['status'], 'deadline')
        self.assertEqual(self.slack.auth_calls, before_auth)
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.state(True)['deadline_state']['consecutive'], 4)

    def test_completed_core_resolves_deadline_even_if_optional_noa_fails(self):
        for _ in range(3): self.expire()
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        with patch.object(NoaWake, 'run', side_effect=RemoteError('history unavailable')):
            self.run_once(live=True)
        self.assertIsNotNone(self.state(True)['last_run_at'])
        self.assertEqual(self.state(True)['deadline_state']['consecutive'], 0)
        self.assertEqual(len(self.notices_of('deadline_recovery')), 1)
        self.assertIn('依頼・報告', self.notices_of('deadline_recovery')[0]['text'])

    def test_noa_failure_preserves_generic_error_without_false_recovery(self):
        self.run_once(live=True)
        state = self.state(True)
        state['last_error'] = 'RemoteError'
        StateStore(self.root, 'live').save(state)
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        with patch.object(NoaWake, 'run', side_effect=RemoteError('history unavailable')):
            self.run_once(live=True)
        self.assertEqual(self.state(True)['last_error'], 'RemoteError')
        self.assertEqual(self.notices_of('recovery'), [])

    def test_component_hold_notice_is_frozen_when_noa_off(self):
        self.run_once(live=True)
        state = self.state(True)
        for status in ('queued', 'sending'):
            state['outbox']['component-hold-' + status] = {
                'kind': 'hold_notice', 'status': status, 'held_key': 'noa-component:1',
                'text': 'component hold', 'channel': self.cfg.report_channel, 'marker': 'rp:component-' + status}
        StateStore(self.root, 'live').save(state)
        self.add_request()
        self.assertEqual(self.run_once(live=True)['instructions'], 1)
        self.assertEqual(self.slack.find_calls, 0)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.state(True)['outbox']['component-hold-queued']['status'], 'queued')
        self.assertEqual(self.state(True)['outbox']['component-hold-sending']['status'], 'sending')

    def test_noa_failure_notice_queue_error_does_not_abort_core(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.add_request()
        with patch.object(NoaWake, 'run', side_effect=RemoteError('history unavailable')):
            with patch.object(NoaWake, 'component_failure', side_effect=OSError('disk unavailable')):
                result = self.run_once(live=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['instructions'], 1)
        self.assertIsNotNone(self.state(True)['last_run_at'])

    def test_deadline_state_invalid_dates_and_episodes_fail_closed(self):
        self.run_once(live=True)
        original = self.state(True)
        invalid = [
            {'consecutive': 1, 'episode': 1, 'first_at': 'bad', 'alert_key': None},
            {'consecutive': 1, 'episode': 1, 'first_at': '2099-01-01T00:00:00', 'alert_key': None},
            {'consecutive': 1, 'episode': 0, 'first_at': '2099-01-01T00:00:00+00:00', 'alert_key': None},
            {'consecutive': 0, 'episode': 1, 'first_at': None, 'alert_key': 'deadline-alert:1'},
            {'consecutive': 3, 'episode': 2, 'first_at': '2099-01-01T00:00:00+00:00', 'alert_key': 'deadline-alert:1'},
        ]
        for deadline in invalid:
            with self.subTest(deadline=deadline):
                original['deadline_state'] = deadline
                StateStore(self.root, 'live').save(original)
                self.assertEqual(self.run_once(live=True)['status'], 'state_error')

    def test_off_transition_survives_auth_failure_and_initial_budget_expiry(self):
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.run_once(live=True)
        initial_cutoff = self.state(True)['noa_wake']['cutoff']
        self.clock.advance()
        self.cfg = replace(self.cfg, noa_wake_enabled=False)
        self.slack.reject_account = True
        self.assertEqual(self.run_once(live=True)['status'], 'account_mismatch')
        self.assertFalse(self.state(True)['noa_wake']['enabled'])
        self.slack.reject_account = False
        self.cfg = replace(self.cfg, noa_wake_enabled=True)
        self.clock.advance()
        self.run_once(live=True)
        self.assertNotEqual(self.state(True)['noa_wake']['cutoff'], initial_cutoff)
        self.cfg = replace(self.cfg, noa_wake_enabled=False)
        ticks = iter([0, self.cfg.run_timeout])
        self.assertEqual(self.run_once(live=True, monotonic=lambda: next(ticks))['status'], 'deadline')
        self.assertFalse(self.state(True)['noa_wake']['enabled'])


    def test_thirty_minute_threshold_uses_completion_not_start(self):
        # A nearly four-minute successful run must not consume the grace period.
        with patch.object(Patrol, 'flush', side_effect=lambda *_: self.clock.advance(239)):
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
        from riri.eventlog import iso
        completed_at = iso(self.clock())
        self.assertEqual(self.state(True)['last_run_at'], completed_at)
        self.clock.advance(26 * 60)
        with patch.object(self.notion, 'read_blocks', side_effect=self.deadline()):
            self.assertEqual(self.run_once(live=True)['status'], 'deadline')
        self.assertEqual(self.notices_of('deadline_alert'), [])
        self.assertEqual(self.state(True)['last_run_at'], completed_at)
        self.clock.advance(4 * 60)
        with patch.object(self.notion, 'read_blocks', side_effect=self.deadline()):
            self.assertEqual(self.run_once(live=True)['status'], 'deadline')
        self.assertEqual(len(self.notices_of('deadline_alert')), 1)
        self.assertEqual(self.state(True)['deadline_state']['consecutive'], 2)
