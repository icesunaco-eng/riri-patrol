"""U-11: isolate malformed source reports without weakening write safety."""
import json
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
from riri.errors import RemoteError, RequestIncomplete, StopRequested, TimeBudgetExceeded
from riri.parse import parse_reports
from riri.patrol import Patrol


def text_of(block):
    return block[block['type']]['rich_text'][0]['text']['content']


class ReviewU11Reports(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def put(self, text, name='のあ', block_id='malformed-report'):
        page = self.cfg.pages[name].split('/')[-1]
        self.notion.blocks[page].insert(0, flows.block(text, block_id, 'paragraph'))

    def bad(self, **kwargs):
        self.put('【完了報告】\n依頼ID：:cat:20261111-T\n報告した子：のあ\n結果：完了', **kwargs)

    def format_notices(self, live=True):
        return [v for k, v in self.state(live)['outbox'].items() if k.startswith('invalid-report:')]

    def test_invalid_id_continues_request_and_other_page_completion(self):
        self.add_request()
        self.run_once(live=True)
        self.add_report()
        self.add_request(2)
        self.bad()
        result = self.run_once(live=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['instructions'], 1)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')
        self.assertEqual(len(self.format_notices()), 1)
        self.assertEqual(self.format_notices()[0]['channel'], self.cfg.report_channel)
        self.assertIn('この報告の書き方を確認してね', self.format_notices()[0]['text'])
        self.assertIsNone(self.state(True)['last_error'])
        self.assertNotIn('malformed-report', self.state(True)['seen_reports'])

    def test_same_page_valid_summaries_survive_bad_report_in_middle(self):
        self.run_once(live=True)
        self.add_report(name='のあ', number=1, id='good-old')
        self.bad()
        self.add_report(name='のあ', number=2, id='good-new')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(set(self.state(True)['seen_reports']), {'good-old', 'good-new'})

    def test_notice_once_across_new_patrol_instances(self):
        self.bad()
        for _ in range(5):
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(len(self.format_notices()), 1)
        self.assertEqual(sum('この報告の書き方を確認してね' in p[1] for p in self.slack.posts), 1)
        self.assertEqual(len([e for e in self.state(True)['log_buffer']
                              if '完了報告の形式を確認' in str(e)]), 1)

    def test_corrected_report_processes_and_reverted_version_does_not_renotify(self):
        self.bad(); self.run_once(live=True)
        page = self.cfg.pages['のあ'].split('/')[-1]
        original = self.notion.blocks[page][0]
        self.notion.blocks[page][0] = flows.report(name='のあ', id='malformed-report')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertIn('malformed-report', self.state(True)['seen_reports'])
        self.notion.blocks[page][0] = original
        self.run_once(live=True)
        self.assertEqual(len(self.format_notices()), 1)

    def test_changed_invalid_version_gets_one_notice_and_identical_blocks_distinct(self):
        self.bad(); self.bad(block_id='another-report')
        self.run_once(live=True)
        self.assertEqual(len(self.format_notices()), 2)
        page = self.cfg.pages['のあ'].split('/')[-1]
        self.notion.blocks[page][0] = flows.block('【完了報告】', 'another-report', 'paragraph')
        self.run_once(live=True); self.run_once(live=True)
        self.assertEqual(len(self.format_notices()), 3)

    def test_truncated_duplicate_mismatched_result_and_reporter_are_isolated(self):
        base = text_of(flows.report(name='のあ'))
        bad = ['【完了報告】', base.replace('結果：完了', '結果：不明'),
               base.replace('報告した子：のあ', '報告した子：うさ'),
               base + '\n結果：一部完了', base + '\n依頼 ID：💛20990101-2']
        for n, text in enumerate(bad): self.put(text, block_id=f'bad-{n}')
        self.add_request(3)
        for _ in range(4): self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(len(self.format_notices()), len(bad))
        self.assertEqual(self.state(True)['requests']['💛20990101-3']['status'], 'posted')
        self.assertFalse(self.state(True)['seen_reports'])

    def test_unrelated_prose_and_templates_never_notify(self):
        for n, text in enumerate(['説明\n結果：例A\n結果：例B',
                                  '【完了報告】記入例\n依頼ID：bad\n結果：完了\n結果：完了',
                                  '【完了報告】\n依頼ID：\n報告した子：のあ',
                                  '【完了報告】\n依頼ID：💛YYYYMMDD-1\n報告した子：のあ']):
            self.put(text, block_id=f'template-{n}')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertFalse(self.format_notices())

    def test_untrusted_content_is_not_copied_to_notice_or_event(self):
        payload = '<!channel> suspicious-body @everyone'
        self.put(f'【完了報告】\n依頼ID：{payload}\n報告した子：{payload}\n結果：{payload}')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertNotIn(payload, json.dumps(self.format_notices(), ensure_ascii=False))
        self.assertNotIn(payload, json.dumps(self.state(True)['log_buffer'], ensure_ascii=False))
        self.assertIn('のあ', self.format_notices()[0]['text'])

    def test_malformed_newest_known_id_does_not_reveal_older_complete(self):
        self.add_request(); self.run_once(live=True)
        self.add_report()
        self.put(text_of(flows.report()).replace('結果：完了', '結果：不明'), name='うさ')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'posted')
        self.assertIn('report-1', self.state(True)['seen_reports'])
        page = self.cfg.pages['うさ'].split('/')[-1]
        self.notion.blocks[page][0] = flows.report(id='malformed-report')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')

    def test_unknown_id_isolated_without_blocking_known_outcomes(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        self.add_request(2); self.run_once(live=True)
        self.add_report(name='のあ'); self.add_report(number=2)
        self.bad()
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'done')

    def test_known_invalid_id_does_not_redispatch_same_request(self):
        self.add_request()
        self.add_request(2)
        self.put(text_of(flows.report()).replace('結果：完了', '結果：不明'), name='うさ')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertNotIn('💛20990101-1', self.state(True)['requests'])
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')

    def test_prepared_related_instruction_waits_without_starving_other_request(self):
        self.add_request()
        exc = TimeBudgetExceeded('before send'); exc.not_sent = True
        self.slack.error = exc
        self.assertEqual(self.run_once(live=True)['status'], 'deadline')
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'], 'queued')
        self.put(text_of(flows.report()).replace('結果：完了', '結果：不明'), name='うさ')
        self.add_request(2)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'], 'queued')
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')

    def test_duplicate_request_id_fields_guard_every_labeled_valid_id(self):
        for n in (1, 2, 3): self.add_request(n)
        self.put(text_of(flows.report()) + '\n依頼 ID：💛20990101-2', name='うさ')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(set(self.state(True)['requests']), {'💛20990101-3'})

    def test_mixed_duplicate_ids_guard_only_identifiable_candidates(self):
        self.add_request(2, recipients='@のあ'); self.run_once(live=True)
        self.add_report(name='のあ', number=2)
        page = self.cfg.pages['のあ'].split('/')[-1]
        good = self.notion.blocks[page][0]
        base = text_of(flows.report(name='のあ')).replace('結果：完了', '結果：不明')
        for invalid_id in ('💛20990101-2x', '', ':cat:20261111-T'):
            self.notion.blocks[page] = [flows.block(base + '\n依頼 ID：' + invalid_id, 'bad'), good]
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'],
                             'posted' if invalid_id == '💛20990101-2x' else 'done')
        self.notion.blocks[page] = [good]
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'done')

    def test_reporter_spoof_cannot_block_unrelated_page_completion(self):
        self.add_request(); self.run_once(live=True)
        self.add_report()
        # The source page is のあ, regardless of the untrusted reporter field.
        self.put(text_of(flows.report(name='うさ')))
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
        self.assertIn('報告した子：のあ', self.format_notices()[0]['text'])

    def test_valid_notion_block_link_identifies_malformed_source(self):
        block_id = '12345678-1234-1234-1234-123456789abc'
        self.bad(block_id=block_id)
        self.run_once(live=True)
        self.assertIn('https://app.notion.com/p/' + block_id.replace('-', ''), self.format_notices()[0]['text'])

    def test_notice_uncertain_absent_never_reposts(self):
        self.bad()
        self.slack.error = RemoteError('lost response', uncertain=True)
        self.run_once(live=True)
        for _ in range(4): self.run_once(live=True)
        self.assertEqual(len(self.format_notices()), 1)
        self.assertEqual(self.format_notices()[0]['status'], 'held')
        self.assertFalse(any('この報告の書き方を確認してね' in p[1] for p in self.slack.posts))

    def test_notice_uncertain_found_reconciles_once(self):
        self.bad()
        original = self.slack.post
        def lost(channel, text, **kwargs):
            original(channel, text, **kwargs)
            raise RemoteError('lost response', uncertain=True)
        with patch.object(self.slack, 'post', side_effect=lost): self.run_once(live=True)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(self.format_notices()[0]['status'], 'posted')
        self.assertEqual(sum('この報告の書き方を確認してね' in p[1] for p in self.slack.posts), 1)

    def test_crash_after_notice_intent_does_not_duplicate(self):
        self.bad()
        original = Patrol.queue
        def crash(patrol, key, *args, **kwargs):
            item = original(patrol, key, *args, **kwargs)
            if key.startswith('invalid-report:'): raise StopRequested('after durable intent')
            return item
        with patch.object(Patrol, 'queue', crash):
            self.assertEqual(self.run_once(live=True)['status'], 'stopped')
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.format_notices()), 1)
        self.assertEqual(sum('この報告の書き方を確認してね' in p[1] for p in self.slack.posts), 1)

    def test_practice_only_records_virtual_notice(self):
        self.bad()
        for _ in range(3): self.assertEqual(self.run_once()['status'], 'ok')
        self.assertFalse(self.slack.posts)
        self.assertFalse(self.notion.writes)
        self.assertEqual(len(self.format_notices(live=False)), 1)

    def test_transport_and_stop_errors_are_not_swallowed_as_bad_reports(self):
        page = self.cfg.pages['のあ'].split('/')[-1]
        original = self.notion.read_blocks
        for exc, status in [(RemoteError('offline failure'), 'error'),
                            (StopRequested('offline stop'), 'stopped'),
                            (TimeBudgetExceeded('offline deadline'), 'deadline')]:
            exc.not_sent = True
            def fail(target):
                if target == page: raise exc
                return original(target)
            with patch.object(self.notion, 'read_blocks', side_effect=fail):
                self.assertEqual(self.run_once(live=True)['status'], status)
            self.assertFalse(self.format_notices())


class ReviewU11ReportParsing(unittest.TestCase):
    def test_opt_in_isolation_preserves_valid_neighbors_and_strict_default(self):
        bad = flows.block('【完了報告】\n依頼ID：bad', 'bad')
        with self.assertRaises(RequestIncomplete): parse_reports([bad])
        problems = []
        parsed = parse_reports([flows.report(id='one'), bad, flows.report(2, id='two')],
                               expected_reporter='うさ', on_invalid=lambda *args: problems.append(args))
        self.assertEqual([r.block_id for r in parsed], ['one', 'two'])
        self.assertEqual(len(problems), 1)

    def test_unexpected_parser_fault_and_handler_fault_propagate(self):
        with patch('riri.parse.fields_from_text', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                parse_reports([flows.report()], on_invalid=lambda *args: None)
        with self.assertRaises(StopRequested):
            parse_reports([flows.block('【完了報告】')],
                          on_invalid=lambda *args: (_ for _ in ()).throw(StopRequested('stop')))


if __name__ == '__main__':
    unittest.main()
