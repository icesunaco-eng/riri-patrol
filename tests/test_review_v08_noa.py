"""No real API/credentials: durable stale fences and stable failure episodes."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from riri.errors import SafetyError, StopRequested, TimeBudgetExceeded
from riri.noa import (CHANNEL, LEGACY_KEYS, LIFECYCLE_KEYS, NoaWake, NoaRateLimit,
                      NoaRejected, NoaUncertain, migrate_state, validate_state)
from tests import test_noa_wake as fixtures


class ReviewNoaV08Tests(unittest.TestCase):
    setUp = fixtures.WakeTests.setUp
    new_patrol = fixtures.WakeTests.new_patrol
    begin = fixtures.WakeTests.begin
    message = fixtures.WakeTests.message
    run_wake = fixtures.WakeTests.run_wake
    items = fixtures.WakeTests.items
    restart = fixtures.WakeTests.restart

    def notices(self, prefix='noa-stale:'):
        return [v for k, v in self.p.state['outbox'].items() if k.startswith(prefix)]

    def failure(self, reason='component_unavailable'):
        NoaWake(self.p, self.client).component_failure(reason)

    def test_stop_one_day_twenty_messages_count_once_never_fire(self):
        self.cfg.stop.touch()
        for i in range(20):
            self.clock.advance(4320)
            self.message(i)
            self.assertEqual(self.p.run()['status'], 'stopped')
        self.assertFalse(self.slack.reads)
        self.assertFalse(self.client.calls)
        self.cfg.stop.unlink()
        self.clock.advance(1)  # Fixture message microseconds precede the resume fence.
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertIn('起こさなかった @のあ が20件', self.notices()[0]['text'])
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.client.calls)
        self.clock.advance(1)
        self.message(21)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)

    def test_on_twelve_hour_mac_downtime_with_twelve_unseen_posts(self):
        for i in range(12):
            self.clock.advance(3600)
            self.message(i)
        self.clock.advance(1)
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertIn('が12件', self.notices()[0]['text'])
        self.assertEqual(self.items(), [])
        validate_state(self.p.store.load()['noa_wake'])

    def test_queue_retry_counted_once_alongside_unseen_preserves_quota(self):
        self.message(1)
        self.message(2)
        self.client.responses = [NoaRateLimit(0)]
        self.run_wake()
        attempts = copy.deepcopy(self.p.state['noa_wake']['attempts'])
        self.clock.advance(901)
        for n in range(3, 13):
            self.message(n)
        self.clock.advance(1)
        self.run_wake()
        self.assertEqual([i['status'] for i in self.items()], ['rejected', 'rejected'])
        self.assertEqual(attempts, self.p.state['noa_wake']['attempts'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn('が12件', self.notices()[0]['text'])
        self.run_wake()
        self.assertEqual(len(self.notices()), 1)

    def test_known_done_held_sending_excluded_from_skipped_total(self):
        for n in range(1, 5):
            self.message(n)
        self.run_wake()
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        self.items()[2].update(status='sending', attempt_count=1)
        self.p.state['noa_wake']['attempts'].append(self.clock().isoformat())
        attempts = self.p.state['noa_wake']['attempts'][:]
        self.clock.advance(901)
        self.run_wake()
        self.assertEqual([i['status'] for i in self.items()], ['done', 'held', 'held', 'rejected'])
        self.assertEqual(self.p.state['noa_wake']['attempts'], attempts)
        self.assertIn('が1件', self.notices()[0]['text'])

    def test_observed_off_counts_after_on_with_no_off_reads(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        before_reads = len(self.slack.reads)
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.run_wake()
        self.clock.advance(20)
        for n in range(3, 8):
            self.message(n)
        self.run_wake()
        self.assertEqual(len(self.slack.reads), before_reads)
        self.assertFalse(self.notices())
        self.clock.advance(1)
        self.p.config = self.cfg
        self.run_wake()
        self.assertIn('が6件', self.notices()[0]['text'])
        self.assertEqual(len(self.client.calls), 1)

    def test_unobserved_off_is_caught_as_gap(self):
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.clock.advance(901)
        self.message()
        self.clock.advance(1)
        self.p.config = self.cfg
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertIn('が1件', self.notices()[0]['text'])

    def test_partial_count_read_never_claims_guessed_total(self):
        self.clock.advance(901)
        for n in range(20):
            self.message(n)
        self.clock.advance(1)
        self.slack.fail = SafetyError('partial page failure')
        with self.assertRaises(SafetyError):
            self.run_wake()
        saved = self.p.store.load()['noa_wake']
        self.assertIsNone(saved['stale_windows'][0]['count'])
        self.assertFalse(self.notices())
        self.assertFalse(self.client.calls)
        self.slack.fail = None
        self.restart()
        self.run_wake()
        self.assertIn('が20件', self.notices()[0]['text'])

    def test_count_save_before_outbox_queue_survives_crash(self):
        self.message()
        self.clock.advance(901)
        with patch.object(self.p, 'queue', side_effect=RuntimeError('crash before queue')):
            with self.assertRaises(RuntimeError):
                self.run_wake()
        self.assertEqual(self.p.store.load()['noa_wake']['stale_windows'][0]['count'], 1)
        self.assertFalse(self.notices())
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.client.calls)

    def test_uncertain_stale_notice_is_never_requeued_as_new_intent(self):
        self.message()
        self.clock.advance(901)
        self.run_wake()
        self.notices()[0].update(status='held', hold_reason='uncertain')
        self.p.save()
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices()[0]['status'], 'held')

    def test_bad_stale_window_does_not_starve_fresh_wake(self):
        self.message()
        self.clock.advance(901)
        self.slack.fail = TimeBudgetExceeded('old scan budget')
        with self.assertRaises(TimeBudgetExceeded):
            self.run_wake()
        old = self.p.state['noa_wake']['stale_windows'][0]['start']
        self.slack.fail = None
        history = self.slack.history
        def selective_history(channel, oldest):
            if oldest == old:
                raise TimeBudgetExceeded('old scan budget')
            return history(channel, oldest)
        self.clock.advance(1)
        self.message(2)
        with patch.object(self.slack, 'history', side_effect=selective_history):
            with self.assertRaises(TimeBudgetExceeded):
                self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(self.notices())
        self.assertEqual(self.items()[0]['status'], 'done')

    def test_bot_duplicates_and_non_mentions_do_not_inflate_count(self):
        message = self.message()
        self.slack.messages.append(copy.deepcopy(message))
        self.message(2, bot_id='BOTHER')
        self.message(3, text='normal message')
        self.message(4, user='URIRI')
        self.message(5, text='＠のあ fullwidth')
        self.clock.advance(901)
        self.run_wake()
        self.assertIn('が2件', self.notices()[0]['text'])

    def test_legacy_v07_unknown_timestamp_rearms_without_purging(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        state = self.p.state['noa_wake']
        old = {k: copy.deepcopy(v) for k, v in state.items() if k in LEGACY_KEYS | LIFECYCLE_KEYS}
        self.p.state['noa_wake'] = old
        self.clock.advance(1)
        self.message(3)
        self.clock.advance(1)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual([i['status'] for i in self.items()], ['done', 'rejected'])
        self.assertEqual(old['attempts'], state['attempts'])
        self.assertIn('が2件', self.notices()[0]['text'])
        validate_state(self.p.store.load()['noa_wake'])

    def test_component_flapping_eight_alternations_one_notice(self):
        for _ in range(8):
            self.failure()
            self.clock.advance(300)
            self.run_wake()
            self.restart()
        self.assertEqual(len(self.notices('noa-component:')), 1)

    def test_reason_specific_configuration_auth_flapping(self):
        for n in range(8):
            self.clock.advance(1)
            self.message()
            if n % 2:
                self.client.responses = [NoaRejected('authentication')]
                self.run_wake()
            else:
                with patch.object(self.client, 'validate', side_effect=SafetyError('bad config')):
                    self.run_wake()
            self.clock.advance(300)
            self.message()
            self.run_wake()
            self.restart()
        # One durable notification per reason, not one per alternation.
        self.assertEqual(len(self.notices('noa-component:')), 2)

    def test_component_rearms_after_hour_of_continuous_real_scans(self):
        self.failure()
        self.run_wake()
        for _ in range(12):
            self.clock.advance(300)
            self.run_wake()
        self.failure()
        self.assertEqual(len(self.notices('noa-component:')), 2)

    def test_auth_and_config_rearm_only_after_hour_verified_fires(self):
        self.failure('configuration')
        self.failure('authentication')
        for _ in range(7):
            self.message()
            self.run_wake()
            self.clock.advance(600)
        self.failure('configuration')
        self.failure('authentication')
        self.assertEqual(len(self.notices('noa-component:')), 4)

    def test_walltime_or_empty_scans_cannot_rearm_auth(self):
        self.failure('authentication')
        self.message()
        self.run_wake()
        for _ in range(13):
            self.clock.advance(300)
            self.run_wake()
        self.failure('authentication')
        self.assertEqual(len(self.notices('noa-component:')), 1)

    def test_downtime_does_not_count_as_hour_success(self):
        self.failure()
        self.run_wake()
        self.clock.advance(3601)
        self.run_wake()
        self.run_wake()
        self.failure()
        self.assertEqual(len(self.notices('noa-component:')), 1)

    def test_partial_failure_interrupts_success_window(self):
        self.failure()
        self.run_wake()
        for _ in range(5):
            self.clock.advance(600)
            self.run_wake()
        self.clock.advance(300)
        self.message()
        self.client.responses = [NoaRateLimit(0)]
        self.run_wake()
        self.clock.advance(300)
        self.run_wake()
        self.failure()
        self.assertEqual(len(self.notices('noa-component:')), 1)

    def test_component_notice_general_intent_survives_crash(self):
        with patch.object(self.p, 'queue', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.failure()
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.notices('noa-component:')), 1)

    def test_backward_clock_never_moves_fence_back_or_rearms_failure(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        before = self.p.state['noa_wake']['watermark']
        self.failure()
        self.run_wake()
        self.clock.advance(-120)
        self.run_wake()
        self.assertGreaterEqual(float(self.p.state['noa_wake']['cutoff']), float(before))
        self.assertEqual(len(self.client.calls), 2)
        self.failure()
        self.assertEqual(len(self.notices('noa-component:')), 1)
        validate_state(self.p.store.load()['noa_wake'])

    def test_corrupt_recovery_state_fails_closed_without_repair(self):
        valid = copy.deepcopy(self.p.state['noa_wake'])
        bad_values = [('last_run_at', 'not a date'), ('stale_windows', [{}]),
                      ('failure_episodes', {'authentication': {'serial': 99}})]
        for key, value in bad_values:
            state = copy.deepcopy(valid)
            state[key] = value
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                migrate_state(state)
            self.assertEqual(state, before)
