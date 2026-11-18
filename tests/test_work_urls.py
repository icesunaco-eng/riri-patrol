"""Work-session URL policy through parsing, rendering and patrol completion."""
import copy
import unittest

import test_patrol_flow as flows
from riri.compose import instruction, report_summary
from riri.parse import parse_reports
from riri.state import StateStore

URL = 'https://chatgpt.com/c/11111111-1111-4111-8111-111111111111'
CLAUDE = 'https://claude.ai/chat/22222222-2222-4222-8222-222222222222'


def report_text(number=1, url=URL, extra=''):
    text = ('【完了報告】\n依頼ID：💛20990101-' + str(number) +
            '\n報告した子：のあ\n結果：完了\nやったこと：模擬作業を確認した')
    if url is not None:
        text += '\n作業場所URL：' + url
    return text + extra


class WorkURLPolicy(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request

    def put(self, number=1, url=URL, block_id='work-report', extra=''):
        page = self.cfg.pages['のあ'].split('/')[-1]
        block = flows.block(report_text(number, url, extra), block_id, 'paragraph')
        self.notion.blocks[page].insert(0, block)
        return block

    def parse(self, value):
        return parse_reports([flows.block(report_text(url=value), 'parse-work')], 'のあ')[0]

    def test_supported_chat_and_execution_urls_are_preserved_exactly(self):
        values = [URL, CLAUDE, 'codex://threads/01a1d861-9989-7441-92c3-e3d681781071',
                  'https://claude.ai/code/cse_014TphtbcTnu4TXfpVw9h6L9',
                  'https://grok.com/c/33333333-3333-4333-8333-333333333333']
        for value in values:
            with self.subTest(value=value):
                report = self.parse(value)
                self.assertEqual(report.work_url, value)
                self.assertEqual(report.work_url_status, 'valid')
                self.assertIn(value, report_summary([report]))

    def test_unsafe_or_unsupported_urls_keep_result_but_are_never_forwarded(self):
        for value in ('javascript:alert(1)', 'file:///tmp/chat', 'http://localhost:123/chat',
                      'https://user:pass@chatgpt.com/c/11111111-1111-4111-8111-111111111111',
                      URL + '?access_token=private', URL + '#fragment', URL + '|<!channel>',
                      'codex://threads/new', 'codex://threads/01a1d861-9989-7441-92c3-e3d681781071?mode=edit',
                      'https://claude.ai/code/22222222-2222-4222-8222-222222222222',
                      'https://chatgpt.com.evil.example/c/11111111-1111-4111-8111-111111111111',
                      'https://github.com/example/repo', 'https://chatgpt.com/',
                      URL + '%0a<!channel>'):
            with self.subTest(value=value):
                report = self.parse(value)
                self.assertEqual(report.result, '完了')
                self.assertEqual(report.work_url, '')
                text = report_summary([report])
                self.assertIn('未記載', text)
                self.assertNotIn(value, text)

    def test_duplicate_url_fields_isolate_only_url_not_whole_report(self):
        report = parse_reports([flows.block(report_text(extra='\n作業場所URL：' + CLAUDE), 'duplicate-url')], 'のあ')[0]
        self.assertEqual(report.result, '完了')
        self.assertEqual(report.work_url, '')
        self.assertNotIn(URL, report_summary([report]))

    def test_new_instructions_recommend_actual_work_session_url(self):
        request = flows.request(recipients='@のあ')
        from riri.parse import parse_requests
        req = parse_requests([request])[0]
        text = instruction(req, req.recipients)
        self.assertIn('作業場所URL：', text)
        self.assertIn('チャット', text)
        self.assertIn('可能なら記入', text)
        self.assertIn('取得できなくても完了できます', text)
        self.assertNotIn('必ず記入', text)
        self.assertNotIn('追記待ち', text)
        self.assertLessEqual(len(text), 1500)

    def test_new_missing_url_completes_and_reports_result_once(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        self.add_request(2, recipients='@のあ'); self.run_once(live=True)
        self.put(1, None)
        self.put(2, CLAUDE, 'other-report')
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(state['requests']['💛20990101-2']['status'], 'done')
        summary = '\n'.join(i['text'] for i in state['outbox'].values() if i['kind'] == 'reports')
        self.assertIn('💛20990101-1 → 完了', summary)
        self.assertIn('未記載（任意）', summary)
        self.assertIn(CLAUDE, summary)
        report_items = {key: copy.deepcopy(item) for key, item in state['outbox'].items()
                        if item['kind'] == 'reports'}
        for _ in range(3): self.run_once(live=True)
        restarted = self.state(True)
        self.assertFalse(any(k.startswith('work-url:') for k in restarted['outbox']))
        self.assertFalse(restarted['work_url_policy']['pending_versions'])
        self.assertEqual(report_items, {key: item for key, item in restarted['outbox'].items()
                                       if item['kind'] == 'reports'})

    def test_url_added_to_same_report_is_a_new_version_after_optional_completion(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        block = self.put(url=None)
        self.run_once(live=True)
        before = self.state(True)
        self.assertEqual(before['requests']['💛20990101-1']['status'], 'done')
        first_digest = before['seen_reports']['work-report']
        first_deliveries = copy.deepcopy(before['report_deliveries'])
        block['paragraph']['rich_text'][0]['text']['content'] += '\n作業場所URL：' + CLAUDE
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertNotEqual(state['seen_reports']['work-report'], first_digest)
        self.assertEqual(len(state['seen_report_versions']['work-report']), 2)
        self.assertEqual(len(state['report_deliveries']), len(first_deliveries) + 1)
        for key, delivery in first_deliveries.items():
            self.assertEqual(state['report_deliveries'][key], delivery)
        self.assertFalse(state['work_url_policy']['pending_versions'])
        self.run_once(live=True)
        self.assertEqual(self.state(True)['report_deliveries'], state['report_deliveries'])

    def test_newer_valid_report_is_delivered_without_deleting_old_optional_report(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        old = self.put(url=None, block_id='old-missing')
        old_source = copy.deepcopy(old)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
        self.put(url=CLAUDE, block_id='new-valid')
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(old, old_source)
        self.assertIn('old-missing', state['seen_reports'])
        self.assertIn('new-valid', state['seen_reports'])
        self.assertEqual(len(state['report_deliveries']), 2)
        self.assertFalse(state['work_url_policy']['pending_versions'])

    def test_first_activation_baselines_only_existing_versions_without_rewriting_them(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        state = self.state(True); state.pop('work_url_policy', None)
        StateStore(self.root, 'live').save(state)
        block = self.put(url=None)
        before = copy.deepcopy(block)
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(before, block)
        self.assertTrue(state['work_url_policy']['legacy_versions'])
        summary = '\n'.join(i['text'] for i in state['outbox'].values() if i['kind'] == 'reports')
        self.assertIn('未記載（旧報告）', summary)

    def test_restart_does_not_expand_legacy_baseline(self):
        self.add_request(recipients='@のあ'); self.run_once(live=True)
        before = copy.deepcopy(self.state(True)['work_url_policy']['legacy_versions'])
        self.put(url=None)
        for _ in range(3): self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['work_url_policy']['legacy_versions'], before)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertFalse(state['work_url_policy']['pending_versions'])

    def test_existing_outbox_text_is_never_recomposed(self):
        self.run_once(live=True)
        state = self.state(True)
        state['outbox']['legacy-summary'] = {'status':'posted', 'kind':'reports',
            'channel':self.cfg.report_channel, 'text':'既存の保存済み報告本文', 'reports':[]}
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['legacy-summary']['text'], '既存の保存済み報告本文')

    def test_updated_legacy_missing_url_is_new_optional_version_without_expanding_baseline(self):
        self.add_request(recipients='@のあ @かれん'); self.run_once(live=True)
        state = self.state(True); state.pop('work_url_policy', None)
        StateStore(self.root, 'live').save(state)
        block = self.put(url=None)
        self.run_once(live=True)
        baseline = copy.deepcopy(self.state(True)['work_url_policy']['legacy_versions'])
        block['paragraph']['rich_text'][0]['text']['content'] += '\nブロッカー：追加確認'
        page = self.cfg.pages['かれん'].split('/')[-1]
        self.notion.blocks[page].insert(0, flows.report(name='かれん', id='karen-valid'))
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual(state['work_url_policy']['legacy_versions'], baseline)
        self.assertEqual(len(state['seen_report_versions']['work-report']), 2)
        self.assertFalse(state['work_url_policy']['pending_versions'])
        self.assertFalse(any(k.startswith('work-url:') for k in state['outbox']))

    def test_unavailable_reason_is_retained_in_source_without_becoming_a_url(self):
        report = parse_reports([flows.block(report_text(url='未取得（ローカルのみ）',
            extra='\n作業場所URLが取得できない理由：ローカルCLIでURL未提供'), 'unavailable')], 'のあ')[0]
        self.assertEqual(report.work_url, '')
        self.assertIn('ローカルCLIでURL未提供', report.text)
        self.assertIn('未記載', report_summary([report]))

    def test_broken_cutover_record_is_not_reset_to_another_baseline(self):
        self.run_once(live=True)
        state = self.state(True)
        state['work_url_policy']['legacy_versions'] = 'corrupt'
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['work_url_policy']['legacy_versions'], 'corrupt')

    def test_batching_keeps_every_valid_url_without_truncation(self):
        self.run_once(live=True)
        values=[]
        for number in range(1, 36):
            self.add_request(number,recipients='@のあ',status='完了')
            value='https://chatgpt.com/c/00000000-0000-4000-8000-' + f'{number:012d}'
            values.append(value)
            self.put(number, value, 'url-report-' + str(number))
        self.run_once(live=True)
        items = [i for i in self.state(True)['outbox'].values() if i['kind'] == 'reports']
        self.assertGreater(len(items), 2)
        for channel in (self.cfg.instruction_channel,self.cfg.report_channel):
            deliveries=[item for item in items if item['channel']==channel]
            self.assertGreater(len(deliveries),1)
            self.assertTrue(all(item['status']=='posted' and item['ts'] for item in deliveries))
            text='\n'.join(item['text'] for item in deliveries)
            for value in values: self.assertEqual(text.count(value),1)
            self.assertTrue(all(len(item['text'])<=2850 for item in deliveries))
        self.assertEqual({item['channel'] for item in items},
                         {self.cfg.instruction_channel,self.cfg.report_channel})


if __name__ == '__main__':
    unittest.main()
