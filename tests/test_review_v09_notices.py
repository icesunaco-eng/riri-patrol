"""V-01 / V-05: accurately describe source-format holds without changing them."""
from datetime import datetime, timezone
import copy
import unittest

import test_patrol_flow as flows
import test_review_u11_reports as u11
from riri.eventlog import new_event
from riri.errors import RequestIncomplete
from riri.patrol import Patrol


class ReviewV09Notices(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report
    put = u11.ReviewU11Reports.put
    format_notices = u11.ReviewU11Reports.format_notices

    def test_unknown_id_explains_isolation_once_and_known_outcomes_continue(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        self.add_request(2); self.run_once(live=True)
        self.add_report(name='のあ',id='known-noa-report'); self.add_report(number=2,id='known-usa-report')
        self.put('【完了報告】\n依頼ID：:cat:20261111-T\n報告した子：のあ\n結果：完了')
        for _ in range(3): self.assertEqual(self.run_once(live=True)['status'], 'ok')
        notices = self.format_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn('報告1件を要確認として隔離しました', notices[0]['text'])
        self.assertIn('原本と履歴は保持します', notices[0]['text'])
        self.assertIn('対象IDと報告者が明確な別の報告は処理を続けます', notices[0]['text'])
        self.assertNotIn('ページの報告での自動完了は待ちます', notices[0]['text'])
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'done')
        self.assertEqual(sum('隔離しました' in p[1] for p in self.slack.posts), 1)

    def test_known_id_keeps_individual_notice_without_page_hold_claim(self):
        self.put(u11.text_of(flows.report(name='のあ')).replace('結果：完了', '結果：不明'))
        self.run_once(live=True)
        text = self.format_notices()[0]['text']
        self.assertIn('1件だけを飛ばし', text)
        self.assertNotIn('ページの報告での自動完了は待ちます', text)

    def test_mixed_duplicate_id_uses_unknown_notice(self):
        self.put(u11.text_of(flows.report(name='のあ')) + '\n依頼 ID：bad')
        self.run_once(live=True)
        self.assertIn('読み取れた候補IDの自動完了は待ちます', self.format_notices()[0]['text'])
        self.assertNotIn('ページの報告での自動完了は待ちます', self.format_notices()[0]['text'])

    def test_unknown_notice_uses_source_page_not_untrusted_reporter(self):
        self.put('【完了報告】\n依頼ID：bad\n報告した子：<!channel>\n結果：完了', name='かれん')
        self.run_once(live=True)
        text = self.format_notices()[0]['text']
        self.assertIn('報告した子：かれん', text)
        self.assertIn('報告1件を要確認として隔離しました', text)
        self.assertNotIn('<!channel>', text)

    def test_format_problem_event_uses_readable_columns_without_claiming_delivery(self):
        self.put('【完了報告】')
        self.run_once(live=True)
        row = next(e['row'] for e in self.state(True)['log_buffer']
                   if 'RequestIncomplete' in e['row'][8])
        self.assertEqual(row[2], '完了報告の形式が不正')
        self.assertEqual(row[5], '完了報告の形式を確認する通知を用意した')
        self.assertEqual(row[4], 'のあ')
        self.assertEqual(row[3], '')
        self.assertEqual(row[7], '')

    def test_event_mapping_keeps_existing_explicit_progress_override(self):
        now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        row = new_event(now, now, '完了報告の形式を確認', progress='明示された進捗')['row']
        self.assertEqual(row[2], '明示された進捗')
        self.assertEqual(row[5], '完了報告の形式を確認')

    def test_preexisting_notice_states_and_text_are_not_rewritten_or_renotified(self):
        text = '【完了報告】'
        self.put(text); self.run_once(live=True)
        original = self.state(True)
        key = next(k for k in original['outbox'] if k.startswith('invalid-report:'))
        for status in ('queued', 'sending', 'held', 'posted'):
            with self.subTest(status=status):
                before = copy.deepcopy(original)
                before['outbox'][key].update(status=status, text='v0.8で保存済みの文面')
                patrol = Patrol(self.cfg, self.slack, self.notion, None, clock=self.clock)
                patrol.state = copy.deepcopy(before)
                patrol.invalid_report_ids = set()
                patrol.invalid_report_outcomes = set()
                patrol.invalid_reporters = set()
                patrol.report_problem('のあ', {'id': 'malformed-report'}, text,
                                      RequestIncomplete('missing fields'))
                self.assertEqual(patrol.state, before)
                self.assertEqual(patrol.invalid_reporters, set())


if __name__ == '__main__':
    unittest.main()
