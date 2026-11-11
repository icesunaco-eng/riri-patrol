"""v0.7 Noa review regressions; mock transports and temporary state only."""
import copy
import hashlib
import json
from dataclasses import replace
import unittest
from unittest.mock import patch

from riri.errors import AccountMismatch, SafetyError, StopRequested, TimeBudgetExceeded
from riri.noa import (CHANNEL, LEGACY_KEYS, NoaClient, NoaWake, NoaRateLimit, NoaRejected,
                      NoaUncertain, migrate_state, validate_state)
from tests import test_noa_wake as fixtures


class ReviewNoaTests(unittest.TestCase):
    setUp = fixtures.WakeTests.setUp
    new_patrol = fixtures.WakeTests.new_patrol
    begin = fixtures.WakeTests.begin
    message = fixtures.WakeTests.message
    run_wake = fixtures.WakeTests.run_wake
    items = fixtures.WakeTests.items
    restart = fixtures.WakeTests.restart

    def off(self):
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.run_wake()

    def on(self):
        self.p.config = self.cfg
        self.run_wake()

    def notices(self):
        return [v['text'] for k, v in self.p.state['outbox'].items() if k.startswith('noa-')]

    def test_off_persists_idempotently_without_network(self):
        self.off()
        s = copy.deepcopy(self.p.state['noa_wake'])
        self.assertFalse(s['enabled'])
        self.assertIsNotNone(s['off_at'])
        self.clock.advance(3)
        self.off()
        self.assertEqual(s, self.p.store.load()['noa_wake'])
        self.assertFalse(self.slack.reads)
        self.assertFalse(self.client.calls)

    def test_off_on_retires_queue_and_retry_but_preserves_attempts(self):
        self.message(1)
        self.message(2)
        self.client.responses = [NoaRateLimit(0)]
        self.run_wake()
        self.assertEqual([i['status'] for i in self.items()], ['retry', 'queued'])
        attempts = self.p.state['noa_wake']['attempts'][:]
        self.off()
        self.assertTrue(all(i['reason'] == 'disabled_before_send' for i in self.items()))
        self.clock.advance(2)
        self.message(3)  # OFF-era post must not be queued later.
        self.clock.advance(2)
        self.on()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(attempts, self.p.state['noa_wake']['attempts'])
        self.assertEqual(len(self.items()), 2)
        self.restart()
        self.clock.advance(1)
        self.message(4)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 2)
        validate_state(self.p.state['noa_wake'])

    def test_off_preserves_ambiguous_and_done_records(self):
        self.message(1)
        self.message(2)
        self.message(3)
        self.run_wake()
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        item = self.items()[2]
        item.update(status='sending', attempt_count=1)
        self.p.state['noa_wake']['attempts'].append(self.clock().isoformat())
        before = copy.deepcopy(self.p.state['noa_wake'])
        self.off()
        self.assertEqual([i['status'] for i in self.items()], ['done', 'held', 'sending'])
        self.clock.advance(2)
        self.on()
        self.assertEqual([i['status'] for i in self.items()], ['done', 'held', 'held'])
        self.assertEqual(before['attempts'], self.p.state['noa_wake']['attempts'])
        self.run_wake()
        self.assertEqual(len(self.client.calls), 2)
        validate_state(self.p.store.load()['noa_wake'])

    def test_legacy_migration_keeps_dedup_quota_and_rearms_conservatively(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        s = self.p.state['noa_wake']
        legacy = {k: copy.deepcopy(v) for k,v in s.items() if k in LEGACY_KEYS}
        original = copy.deepcopy(legacy)
        migrate_state(legacy)
        for k in LEGACY_KEYS:
            self.assertEqual(legacy[k], original[k])
        self.assertFalse(legacy['enabled'])
        self.p.state['noa_wake'] = legacy
        self.clock.advance(3)
        self.run_wake()
        self.assertGreater(float(legacy['cutoff']), float(original['cutoff']))
        self.assertEqual([i['status'] for i in self.items()], ['done', 'rejected'])
        self.assertEqual(legacy['attempts'], original['attempts'])
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)

    def test_corrupt_legacy_is_never_repaired(self):
        for state in ({'cutoff': None, 'watermark': None, 'items': {}, 'attempts':['bad']},
                      {'cutoff': None, 'watermark': None, 'items': {}, 'attempts':[], 'enabled':False}):
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                migrate_state(state)
            self.assertEqual(before, state)

    def test_quota_survives_off_on(self):
        for _ in range(10):
            self.message()
            self.run_wake()
            self.clock.advance(1)
        self.assertEqual(len(self.client.calls), 10)
        self.off()
        self.clock.advance(1)
        self.on()
        self.clock.advance(1)
        self.message()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 10)
        self.assertEqual(self.items()[-1]['status'], 'queued')
        validate_state(self.p.store.load()['noa_wake'])

    def test_fullwidth_mention_keeps_original_payload_and_known_sender(self):
        text = '前文 ＠のあ ＡＢＣ① のままお願いします'
        self.message(text=text, user='U0C5V11S37X')
        self.run_wake()
        self.assertIn(text, self.client.calls[0])
        self.assertIn('たかむね（U0C5V11S37X）', self.client.calls[0])
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)

    def test_distinct_held_rejection_secret_source_notices(self):
        self.message(1)
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        self.message(2)
        self.client.responses = [NoaRejected()]
        self.run_wake()
        self.message(3, text='＠のあ unit-token')
        self.run_wake()
        self.message(4)
        self.message(5)
        self.run_wake()
        self.slack.messages[-1]['text'] = '@のあ changed'
        self.run_wake()
        texts = self.notices()
        self.assertTrue(any('保留' in t and '実行履歴とセッションを確認' in t for t in texts))
        self.assertTrue(any('要求が拒否' in t for t in texts))
        self.assertTrue(any('認証情報を除いた新しい指示' in t for t in texts))
        self.assertTrue(any('変更または削除' in t for t in texts))
        self.assertIn('https://test.slack.com/archives/', texts[0])
        combined = json.dumps(self.p.state, ensure_ascii=False)
        self.assertNotIn('unit-token', combined)
        self.assertNotIn('private source body', combined)
        for event in self.p.state['log_buffer']:
            self.assertRegex(event['row'][8], '[ぁ-んァ-ン一-龥]')

    def test_configuration_failure_coalesces_until_verified_success(self):
        with patch.object(self.client, 'validate', side_effect=SafetyError('unit-token')):
            for n in range(1,4):
                self.message(n)
                self.run_wake()
        self.assertEqual(len(self.notices()), 1)
        self.assertIn('URL・認証設定', self.notices()[0])
        self.assertFalse(self.client.calls)
        self.restart()
        self.message(4)
        self.run_wake()
        self.assertIsNone(self.p.state['noa_wake']['failure_reason'])
        with patch.object(self.client, 'validate', side_effect=SafetyError('unit-token')):
            self.message(5)
            self.run_wake()
        self.assertEqual(len(self.notices()), 2)

    def test_authentication_failure_has_one_episode_no_silent_retry(self):
        for n in range(1,4):
            self.message(n)
            self.client.responses = [NoaRejected('authentication')]
            self.run_wake()
        self.assertEqual(len(self.client.calls), 3)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(len(self.notices()), 1)
        self.assertIn('認証・権限', self.notices()[0])
        self.assertIn('https://test.slack.com/archives/', self.notices()[0])

    def test_component_failure_is_fixed_and_recovers_after_valid_scan(self):
        wake = NoaWake(self.p, self.client)
        wake.component_failure('unit-token https://unsafe.example/raw')
        wake.component_failure()
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn('unit-token', self.notices()[0])
        self.off()
        self.assertEqual(self.p.state['noa_wake']['failure_reason'], 'component_unavailable')
        self.on()
        self.run_wake()
        self.assertIsNone(self.p.state['noa_wake']['failure_reason'])
        NoaWake(self.p, self.client).component_failure()
        self.assertEqual(len(self.notices()), 2)

    def test_invalid_stored_link_fails_closed(self):
        self.message()
        self.run_wake()
        self.items()[0]['source_link'] = 'https://evil.example/token'
        with self.assertRaises(ValueError):
            validate_state(self.p.state['noa_wake'])

    def test_account_mismatch_propagates_and_holds_possible_send(self):
        self.message()
        self.client.responses = [AccountMismatch('mismatch')]
        with self.assertRaises(AccountMismatch):
            self.run_wake()
        self.assertEqual(self.items()[0]['status'], 'held')

    def test_off_retirement_event_and_status_are_one_durable_save(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        self.off()
        state = self.p.store.load()
        self.assertEqual(list(state['noa_wake']['items'].values())[1]['reason'], 'disabled_before_send')
        self.assertTrue(any('OFF切替のため未送信分の起動を取りやめた' in e['row']
                            and '起こさなかった' in e['row'] for e in state['log_buffer']))
        self.assertFalse(self.notices())

    def test_missing_source_has_actionable_notice_without_new_permalink_call(self):
        self.message(1)
        pending = self.message(2)
        self.run_wake()
        self.slack.messages.remove(pending)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(self.slack.links), 1)
        self.assertIn('変更または削除', self.notices()[0])
        self.assertIn(pending['ts'], self.notices()[0])

    def test_fullwidth_practice_has_no_fire_or_permalink(self):
        self.p.config = replace(self.cfg, mode='practice')
        self.message(text='＠のあ 練習')
        self.run_wake()
        self.assertEqual(self.items()[0]['status'], 'simulated')
        self.assertFalse(self.client.calls)
        self.assertFalse(self.slack.links)

    def test_pending_before_new_cutoff_fails_closed(self):
        self.message(1)
        self.message(2)
        self.run_wake()
        self.off()
        self.clock.advance(2)
        self.on()
        self.items()[1].update(status='queued', attempt_count=0)
        with self.assertRaises(ValueError):
            validate_state(self.p.state['noa_wake'])

    def test_component_failure_after_history_does_not_reopen_same_episode(self):
        self.message()
        with patch.object(self.slack, 'permalink', side_effect=RuntimeError('unit-token')):
            for _ in range(3):
                wake = NoaWake(self.p, self.client)
                with self.assertRaises(RuntimeError):
                    wake.run()
                wake.component_failure()
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.client.calls)

    def test_transport_post_response_boundaries_keep_original_exception(self):
        for boundary in (StopRequested, TimeBudgetExceeded):
            with self.subTest(boundary=boundary.__name__):
                opener = fixtures.Opener()
                error = boundary('local boundary')
                checks = []
                def guard():
                    checks.append(1)
                    if len(checks) == 2:
                        raise error
                client = NoaClient(fixtures.URL, 'unit-token', guard=guard,
                                   clock=self.clock, opener=opener)
                with self.assertRaises(boundary) as caught:
                    client.fire('mock payload')
                self.assertIs(caught.exception, error)
                self.assertTrue(error.uncertain)
                self.assertFalse(error.not_sent)
                self.assertEqual(len(opener.calls), 1)

    def test_post_response_boundary_holds_then_propagates_without_notice_or_next_send(self):
        for boundary in (StopRequested, TimeBudgetExceeded):
            with self.subTest(boundary=boundary.__name__):
                self.clock.advance(1)
                message = self.message()
                opener = fixtures.Opener()
                checks = []
                def guard():
                    checks.append(1)
                    if len(checks) == 2:
                        raise boundary('local boundary')
                client = NoaClient(fixtures.URL, 'unit-token', guard=guard,
                                   clock=self.clock, opener=opener)
                before = len(self.notices())
                with self.assertRaises(boundary):
                    NoaWake(self.p, client).run()
                item = self.p.store.load()['noa_wake']['items'][CHANNEL + ':' + message['ts']]
                self.assertEqual(item['status'], 'held')
                self.assertEqual(item['attempt_count'], 1)
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(len(self.notices()), before)
                # Isolate the second subcase from startup notification of first HOLD.
                self.p.state['noa_wake']['items'][CHANNEL + ':' + message['ts']]['status'] = 'reviewed'

    def test_terminal_failure_notice_intent_survives_crash_before_queue(self):
        for reason in ('invalid_configuration', 'authentication'):
            with self.subTest(reason=reason):
                self.clock.advance(1)
                message = self.message()
                if reason == 'authentication':
                    self.client.responses = [NoaRejected('authentication')]
                original_validate = self.client.validate
                if reason == 'invalid_configuration':
                    self.client.validate = lambda: (_ for _ in ()).throw(SafetyError('bad config'))
                try:
                    with patch.object(self.p.logs, 'event', side_effect=RuntimeError('crash after durable terminal')):
                        with self.assertRaises(RuntimeError):
                            self.run_wake()
                finally:
                    self.client.validate = original_validate
                saved = self.p.store.load()['noa_wake']
                item = saved['items'][CHANNEL + ':' + message['ts']]
                self.assertEqual(item['status'], 'rejected')
                self.assertIn('notice_episode', item)
                key = 'noa-component:' + str(item['notice_episode'])
                self.assertNotIn(key, self.p.state['outbox'])
                self.restart()
                NoaWake(self.p, self.client).notices()
                self.assertIn(key, self.p.store.load()['outbox'])
                before = len(self.notices())
                # Reconstructing notice intent never reopens a recovered episode.
                self.p.state['noa_wake']['failure_reason'] = None
                NoaWake(self.p, self.client).notices()
                self.assertIsNone(self.p.state['noa_wake']['failure_reason'])
                self.assertEqual(len(self.notices()), before)

    def test_corrupt_notice_episode_is_rejected(self):
        self.message()
        self.client.responses = [NoaRejected('authentication')]
        self.run_wake()
        self.items()[0]['notice_episode'] = 999
        with self.assertRaises(ValueError):
            validate_state(self.p.state['noa_wake'])
