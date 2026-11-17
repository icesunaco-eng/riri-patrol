"""Y-01/Y-02 end-to-end mock regressions; no network or real state."""
import copy
from dataclasses import replace
from unittest.mock import patch
import unittest

import test_patrol_flow as flows
from riri import compose
from riri.errors import UnsafeContent
from riri.eventlog import iso
from riri.parse import parse_reports, parse_requests
from riri.patrol import Patrol
from riri.state import StateStore

ID = '💛20990101-1'


class V102Flows(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_report = flows.FlowTests.add_report

    def add_page_request(self, value, number=1, recipients='@うさ'):
        blk = flows.request(number, recipients=recipients)
        blk['code']['rich_text'][0]['text']['content'] += '\n案件ページ：' + value
        self.notion.blocks[self.cfg.requests_page].insert(0, blk)
        return blk

    def complete_page(self, value, expected):
        self.add_page_request(value)
        result = self.run_once(live=True)
        self.assertEqual(result['status'], 'ok')
        original = copy.deepcopy(self.state(True)['requests'][ID]['snapshot'])
        self.add_report()
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        messages = [p[1] for p in self.slack.posts if '→ 完了' in p[1]]
        self.assertEqual(len(messages), 2)
        self.assertIn(expected, messages[0])
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'done')
        self.assertEqual(self.state(True)['requests'][ID]['snapshot'], original)
        self.assertEqual(messages[0].splitlines()[-1][:3], 'rp:')
        self.run_once(live=True)
        self.assertEqual(sum('→ 完了' in p[1] for p in self.slack.posts), 2)
        return messages[0]

    def test_title_only_finishes_once(self):
        self.complete_page('Y書 v0.2', '案件ページ：Y書 v0.2')

    def test_www_notion_so_https_finishes_once(self):
        self.complete_page('https://www.notion.so/synthetic', '案件ページ：（https://www.notion.so/synthetic）')

    def test_external_url_omits_and_finishes_once(self):
        text = self.complete_page('https://example.invalid/safe-private-page', compose.PROJECT_PAGE_OMITTED)
        self.assertNotIn('safe-private-page', text)

    def test_unknown_url_form_omits_and_finishes_once(self):
        self.complete_page('リンク https:/notion.so/malformed', compose.PROJECT_PAGE_OMITTED)

    def test_page_mention_plain_text_title_finishes_once(self):
        blk = flows.request()
        prefix = blk['code']['rich_text'][0]['text']['content'] + '\n案件ページ：'
        blk['code']['rich_text'] = [
            {'type':'text', 'plain_text':prefix, 'text':{'content':prefix}},
            {'type':'mention', 'plain_text':'ページメンション相当のタイトル',
             'mention':{'type':'page','page':{'id':'synthetic-only'}}}]
        req = parse_requests([blk])[0]
        self.assertEqual(req.fields['案件ページ'], 'ページメンション相当のタイトル')
        self.complete_page(req.fields['案件ページ'], '案件ページ：ページメンション相当のタイトル')

    def test_title_markup_escaped_not_slack_control(self):
        self.complete_page('Y書 <案> & 詳細', '案件ページ：Y書 &lt;案&gt; &amp; 詳細')

    def test_unsafe_page_stops_before_instruction_and_notion_status_change(self):
        self.add_page_request('<@UUNTRUSTED> unsafe page')
        result = self.run_once(live=True)
        self.assertEqual(result['status'], 'held')
        self.assertEqual(self.notion.writes, [])
        self.assertNotIn('instruction:' + ID, self.state(True)['outbox'])
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'held')
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])
        self.assertNotIn('UUNTRUSTED', self.slack.posts[-1][1])

    def test_external_url_does_not_hide_encoded_danger_before_instruction(self):
        self.add_page_request('https://example.invalid/%3C%40UUNTRUSTED%3E')
        self.run_once(live=True)
        self.assertEqual(self.notion.writes, [])
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])

    def test_configured_secret_page_preflight_is_held_without_echo(self):
        self.cfg = replace(self.cfg, notion_token='synthetic-private-value')
        self.add_page_request('https://example.invalid/synthetic-private-value')
        self.run_once(live=True)
        self.assertEqual(self.notion.writes, [])
        self.assertNotIn('synthetic-private-value', self.slack.posts[-1][1])
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])

    def test_page_control_character_is_held_before_instruction(self):
        self.add_page_request('page\x01title')
        self.run_once(live=True)
        self.assertNotIn('instruction:' + ID, self.state(True)['outbox'])
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])

    def test_page_too_long_is_dedicated_hold_not_summary_truncation(self):
        self.add_page_request('長'*1501)
        self.run_once(live=True)
        self.assertEqual(self.notion.writes, [])
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])
        self.assertNotIn('長'*20, self.slack.posts[-1][1])

    def test_dangerous_saved_page_holds_report_with_correct_cause(self):
        self.add_page_request('initial safe title')
        self.run_once(live=True)
        state = self.state(True)
        state['requests'][ID]['snapshot']['fields']['案件ページ'] = '<@UUNTRUSTED>'
        StateStore(self.root,'live').save(state)
        self.add_report()
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'posted')
        self.assertTrue(self.state(True)['quarantined_reports'])
        self.assertIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])
        self.assertFalse(any('→ 完了' in p[1] for p in self.slack.posts))

    def test_dangerous_report_body_stays_report_cause(self):
        self.add_page_request('safe title')
        self.run_once(live=True)
        self.add_report()
        blk = self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]][0]
        blk['paragraph']['rich_text'][0]['text']['content'] += '<@UUNTRUSTED>'
        self.run_once(live=True)
        self.assertIn('元の報告を確認して', self.slack.posts[-1][1])
        self.assertNotIn('依頼の案件ページ欄を確認して', self.slack.posts[-1][1])
        self.assertEqual(self.state(True)['requests'][ID]['status'],'posted')

    def test_legacy_hold_is_preserved_until_corrected_report_version(self):
        self.add_page_request('https://example.invalid/safe-old-snapshot')
        self.run_once(live=True)
        self.add_report()
        blocks = self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]]
        r = parse_reports(blocks, 'うさ')[0]
        key = r.block_id + ':' + r.digest
        state = self.state(True)
        old = {'block_id':r.block_id,'digest':r.digest,'status':'held','request_id':ID,
               'reporter':'うさ','reason':'unsafe_content','at':iso(self.clock())}
        state['quarantined_reports'][key] = copy.deepcopy(old)
        original = copy.deepcopy(state['requests'][ID]['snapshot'])
        StateStore(self.root,'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['quarantined_reports'][key], old)
        self.assertFalse(any('→ 完了' in p[1] for p in self.slack.posts))
        blocks[0]['paragraph']['rich_text'][0]['text']['content'] += '。訂正版として結果を再確認'
        self.run_once(live=True)
        current = self.state(True)
        self.assertEqual(current['requests'][ID]['status'],'done')
        self.assertEqual(current['requests'][ID]['snapshot'],original)
        self.assertEqual(current['quarantined_reports'][key]['status'],'superseded')
        self.assertEqual(current['quarantined_reports'][key]['reason'],'unsafe_content')
        self.assertEqual(sum('→ 完了' in p[1] for p in self.slack.posts),2)
        self.run_once(live=True)
        self.assertEqual(sum('→ 完了' in p[1] for p in self.slack.posts),2)

    def test_corrected_report_cannot_release_dangerous_snapshot(self):
        self.add_page_request('safe original')
        self.run_once(live=True)
        state=self.state(True)
        state['requests'][ID]['snapshot']['fields']['案件ページ']='https://example.invalid/%3C%40UUNTRUSTED%3E'
        StateStore(self.root,'live').save(state)
        self.add_report(); self.run_once(live=True)
        self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]][0]['paragraph']['rich_text'][0]['text']['content'] += '訂正'
        self.run_once(live=True)
        self.assertFalse(any('→ 完了' in p[1] for p in self.slack.posts))
        self.assertEqual(self.state(True)['requests'][ID]['status'],'posted')

    def test_relay_unsafe_exception_does_not_abort_other_request(self):
        # A mini-only source exercises the preserved legacy relay path.
        self.add_page_request('first page',recipients='@みに')
        with patch.object(Patrol,'ensure_followups'): self.run_once(live=True)
        state = self.state(True)
        item = state['outbox']['instruction:' + ID]
        item['text'] = '<@UUNTRUSTED>\n' + item['text']
        StateStore(self.root,'live').save(state)
        self.add_page_request('next page',number=2)
        result=self.run_once(live=True)
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['instructions'],1)
        current=self.state(True)
        self.assertEqual(current['outbox']['instruction-gdm:'+ID]['hold_reason'],'unsafe_content')
        self.assertEqual(current['outbox']['instruction:💛20990101-2']['status'],'posted')
        self.assertEqual(current['requests'][ID]['status'],'posted')
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('指示の複製を安全確認' in p[1] for p in self.slack.posts),1)
        self.assertFalse(any('UUNTRUSTED' in p[1] for p in self.slack.posts))

    def test_relay_queue_unsafe_exception_isolated_in_real_patrol_path(self):
        # The v2 Aisu pair is atomic; legacy mini-only relays remain supported.
        self.add_page_request('page',recipients='@みに')
        original=Patrol.queue
        def queue(p,key,*args,**kw):
            if key.startswith('instruction-gdm:'): raise UnsafeContent('synthetic private error')
            return original(p,key,*args,**kw)
        with patch.object(Patrol,'queue',queue): result=self.run_once(live=True)
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['instructions'],1)
        self.assertNotIn('error',result)
        self.assertEqual(sum('指示の複製を安全確認' in p[1] for p in self.slack.posts),1)
        self.assertFalse(any('synthetic private error' in p[1] for p in self.slack.posts))

    def test_resumed_prepared_unsafe_page_isolated_with_dedicated_notice(self):
        blk=self.add_page_request('safe initial')
        original=Patrol.queue
        def interrupt(p,key,*args,**kw):
            if key=='instruction:'+ID: raise KeyboardInterrupt('before original queue')
            return original(p,key,*args,**kw)
        with patch.object(Patrol,'queue',interrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][ID]['status'],'prepared')
        blk['code']['rich_text'][0]['text']['content']=blk['code']['rich_text'][0]['text']['content'].replace('safe initial','<@UUNTRUSTED>')
        self.add_page_request('independent safe',number=2)
        result=self.run_once(live=True)
        self.assertEqual(result['status'],'held')
        self.assertNotIn('instruction:'+ID,self.state(True)['outbox'])
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-2']['status'],'posted')
        self.assertTrue(any('依頼の案件ページ欄を確認して' in x[1] for x in self.slack.posts))
