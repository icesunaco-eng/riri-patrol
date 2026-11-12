"""S-01/S-02 regressions, including the real pre-I/O Transport guard."""
import io
import json
import unittest
from unittest.mock import patch

from riri.errors import TimeBudgetExceeded
from riri.eventlog import new_event
from riri.http import Transport
from riri.slack import SlackClient
from riri.notion import NotionClient
from test_api import code_response
from test_patrol_flow import FlowTests


class ReviewV06SFixes(unittest.TestCase):
    setUp = FlowTests.setUp
    tearDown = FlowTests.tearDown
    run_once = FlowTests.run_once
    state = FlowTests.state
    add_request = FlowTests.add_request
    add_report = FlowTests.add_report

    def test_s01_exact_required_rows(self):
        cases = [
            ('Slackに投稿した', '💛20990101-1 を投稿した', '#りりの指示部屋に投稿'),
            ('完了報告を見つけた', 'うさの完了報告から 💛20990101-1 を拾った', 'たかむねりりあいすに報告'),
            ('完了にした', '💛20990101-1 の宛先全員の完了報告を確認', '状態を完了にした'),
            ('りり自身あてのため飛ばした', 'りり自身あてのため飛ばした', 'りり宛ての除外を報告'),
            ('エラーで停止', 'エラーで停止', '依頼処理を停止'),
        ]
        for old, progress, action in cases:
            with self.subTest(old=old):
                row = new_event(self.clock(), self.clock(), old, '💛20990101-1', 'うさ')['row']
                self.assertEqual(row[2], progress)
                self.assertEqual(row[5], action)

    def test_s01_other_rows_describe_observation_and_operation(self):
        actions = ['投稿結果不明のため保留', '依頼の不足で停止', 'Notion同時編集のため保留',
                   'Notion更新中断のため保留', '取りさげた依頼IDの再登場を確認',
                   '保留の元依頼が見つからない', '安全でない投稿文を保留',
                   '安全でない報告文を保留', '保留を手動解決']
        for action in actions:
            row = new_event(self.clock(), self.clock(), action, '💛20990101-1')['row']
            self.assertNotEqual(row[2], '💛20990101-1 を確認')
            self.assertNotEqual(row[2], row[5])
            self.assertEqual(row[3], '💛20990101-1')

    def test_s01_real_report_ack_uses_reporter_and_accurate_evidence(self):
        self.add_request(); self.run_once(live=True)
        self.add_report(name='うさ'); self.run_once(live=True)
        row = next(e['row'] for e in self.state(True)['log_buffer'] if e['row'][5] == 'たかむねりりあいすに報告')
        self.assertEqual(row[2], 'うさの完了報告から 💛20990101-1 を拾った')
        self.assertIn('ts ', row[7])

    def real_slack(self, fail_after_open=False):
        calls, writes = [], []
        armed = [True]
        def guard():
            # First call is auth.test. The next transport entry is the write.
            if len(calls) == 1 and armed[0] and not fail_after_open:
                armed[0] = False
                raise TimeBudgetExceeded('known pre-I/O boundary')
        class Opener:
            def open(inner, req, timeout):
                calls.append(req.full_url)
                if '/auth.test' in req.full_url:
                    result = {'ok': True, 'bot_id': 'BTEST', 'user_id': 'UTEST'}
                elif '/conversations.history' in req.full_url:
                    result = {'ok': True, 'messages': [], 'has_more': False}
                else:
                    payload = json.loads(req.data)
                    writes.append(payload)
                    if fail_after_open and armed[0]:
                        armed[0] = False
                        raise TimeBudgetExceeded('during socket operation')
                    result = {'ok': True, 'ts': str(len(writes)) + '.000001'}
                return io.BytesIO(json.dumps(result).encode())
        self.slack = SlackClient(('xoxb' + '-test-only'), 'BTEST', 'UTEST',
                                 Transport(opener=Opener(), before_request=guard))
        return calls, writes

    def test_s02_real_transport_pre_send_deadline_no_error_or_recovery_pair(self):
        self.add_request()
        calls, writes = self.real_slack()
        result = self.run_once(live=True)
        self.assertEqual(result, {'status': 'deadline', 'posts': 0})
        self.assertEqual(writes, [])
        state = self.state(True)
        self.assertEqual(set(state['outbox']), {'instruction:💛20990101-1'})
        self.assertEqual(state['outbox']['instruction:💛20990101-1']['status'], 'queued')
        self.assertIsNone(state['last_error'])
        self.assertEqual(state['errors'], {})
        self.assertFalse(any(e['row'][2] == 'エラーで停止' for e in state['log_buffer']))
        self.assertEqual(self.run_once(live=True)['posts'], 1)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['channel'], self.cfg.instruction_channel)
        self.assertFalse(any(x['kind'] in ('error', 'recovery') for x in self.state(True)['outbox'].values()))
        self.assertEqual((self.root/'logs/patrol.log').read_text().count('送信前の時間切れのため終了'), 1)

    def test_s02_time_check_before_any_io_is_quiet_deadline(self):
        ticks = iter([0, 300])
        result = self.run_once(live=True, monotonic=lambda: next(ticks))
        self.assertEqual(result, {'status': 'deadline', 'posts': 0})
        self.assertEqual(self.slack.auth_calls, 0)
        self.assertIsNone(self.state(True)['last_error'])

    def test_s02_real_notion_pre_patch_deadline_has_no_hold_or_notifications(self):
        self.add_request()
        source = self.notion.blocks[self.cfg.requests_page][0]
        text = source['code']['rich_text'][0]['text']['content']
        calls = []
        class Opener:
            def open(inner, req, timeout):
                calls.append(req.method)
                return io.BytesIO(json.dumps(code_response('request-1', text)).encode())
        def guard():
            if calls: raise TimeBudgetExceeded('known pre-PATCH deadline')
        real = NotionClient('ntn_synthetic', Transport(opener=Opener(), before_request=guard, sleep=lambda _: None))
        with patch.object(self.notion, 'set_status', side_effect=real.set_status):
            result = self.run_once(live=True)
        self.assertEqual(result, {'status': 'deadline', 'posts': 0})
        self.assertEqual(calls, ['GET'])
        state = self.state(True)
        self.assertNotEqual(state['requests']['💛20990101-1']['status'], 'held')
        self.assertNotIn('notion_write_intent', state['requests']['💛20990101-1'])
        self.assertFalse(state['outbox'])
        self.assertFalse(state['log_buffer'])
        self.assertIsNone(state['last_error'])
        self.assertEqual(self.run_once(live=True)['posts'], 1)
        self.assertFalse(any(x['kind'] in ('error', 'recovery', 'hold_notice') for x in self.state(True)['outbox'].values()))

    def test_s02_post_patch_verification_deadline_still_holds(self):
        self.add_request()
        error = TimeBudgetExceeded('verification after PATCH')
        error.not_sent = True; error.uncertain = True
        with patch.object(self.notion, 'set_status', side_effect=error):
            self.assertEqual(self.run_once(live=True)['status'], 'error')
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'held')
        self.assertIn('notion_write_intent', state['requests']['💛20990101-1'])

    def test_s02_log_append_pre_send_deadline_keeps_buffer_without_false_hold(self):
        self.add_request(); self.run_once(live=True)
        before = self.state(True)['log_buffer']
        error = TimeBudgetExceeded('known pre-append deadline'); error.not_sent = True
        with patch.object(self.notion, 'append_rows', side_effect=error):
            result = self.run_once(live=True, flush=True)
        self.assertEqual(result, {'status': 'deadline', 'posts': 0})
        state = self.state(True)
        self.assertIsNone(state['log_batch'])
        self.assertEqual(state['log_buffer'], before)
        self.assertIsNone(state['last_error'])
        self.assertFalse(any(x['kind'] in ('error', 'recovery', 'hold_notice') for x in state['outbox'].values()))
        self.assertEqual(self.run_once(live=True, flush=True)['status'], 'ok')
        self.assertFalse(self.state(True)['log_buffer'])
        self.assertFalse(any(k.startswith('log-hold:') for k in self.state(True)['outbox']))

    def test_s02_log_append_unknown_deadline_preserves_uncertain_batch(self):
        self.add_request(); self.run_once(live=True)
        error = TimeBudgetExceeded('after log append'); error.not_sent = True; error.uncertain = True
        with patch.object(self.notion, 'append_rows', side_effect=error):
            self.assertEqual(self.run_once(live=True, flush=True)['status'], 'error')
        self.assertIsNotNone(self.state(True)['log_batch'])
        self.run_once(live=True, flush=True)
        self.assertTrue(any(k.startswith('log-hold:') for k in self.state(True)['outbox']))

    def test_s02_after_open_deadline_still_holds_and_never_retries_instruction(self):
        self.add_request()
        calls, writes = self.real_slack(fail_after_open=True)
        self.assertEqual(self.run_once(live=True)['status'], 'error')
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'], 'held')
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum(x['channel'] == self.cfg.instruction_channel for x in writes), 1)

    def test_s02_not_sent_with_explicit_uncertainty_does_not_quietly_retry(self):
        self.add_request()
        error = TimeBudgetExceeded('ambiguous')
        error.not_sent = True; error.uncertain = True
        self.slack.error = error
        self.assertEqual(self.run_once(live=True)['status'], 'error')
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'], 'held')


del FlowTests
