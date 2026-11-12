import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import test_patrol_flow as flows
from riri.errors import SafetyError

class V113Flows(unittest.TestCase):
    setUp=flows.FlowTests.setUp
    tearDown=flows.FlowTests.tearDown
    run_once=flows.FlowTests.run_once
    state=flows.FlowTests.state
    add_request=flows.FlowTests.add_request

    def test_remove_failed_token_closes_disabled_reinstall_alerts_again(self):
        self.cfg=replace(self.cfg,slack_report_user_token='dummy-token')
        self.slack.report_user_verified=False
        self.slack.wants_report_user=lambda ch,kind: bool(self.cfg.slack_report_user_token) and kind in ('instruction_relay','reports')
        post=self.slack.post
        self.slack.post=lambda ch,text,**kw:post(ch,text,dry_run=kw.get('dry_run',False))
        self.add_request(recipients='@みに')
        self.run_once(live=True)
        self.cfg=replace(self.cfg,slack_report_user_token='')
        self.clock.advance();self.run_once(live=True)
        first=self.state(True)['outbox']['report-user-failed:1']
        self.assertEqual(first['resolution'],'token_removed')
        from tests.test_review_v10_state_compatibility import historical_reader
        historical=historical_reader()(self.root,'live')
        old_state=historical.load()
        self.assertEqual(old_state['outbox']['report-user-failed:1']['resolution'],'token_removed')
        historical.save(old_state)
        self.cfg=replace(self.cfg,slack_report_user_token='dummy-token')
        self.clock.advance();self.run_once(live=True)
        notices={k:v for k,v in self.state(True)['outbox'].items() if k.startswith('report-user-failed:')}
        self.assertEqual(len(notices),2)
        self.assertNotIn('resolved_at',notices['report-user-failed:2'])
        self.assertEqual(sum('代理投稿用のトークン' in p[1] for p in self.slack.posts),2)

    def test_relay_preflight_notice_identifies_destination_not_reporter(self):
        self.slack.wants_report_user=lambda *a:False
        self.add_request(recipients='@みに')
        post=self.slack.post
        def preflight(ch,text,**kw):
            if kw.get('kind')=='instruction_relay':
                error=SafetyError('preflight');error.not_sent=True;raise error
            return post(ch,text,dry_run=kw.get('dry_run',False))
        self.slack.post=preflight
        self.run_once(live=True)
        notices=[v for k,v in self.state(True)['outbox'].items() if k.startswith('preflight:')]
        self.assertTrue(notices)
        for n in notices:
            self.assertIn('💛20990101-1',n['text'])
            self.assertIn('宛先：たかむねりりあいす',n['text'])
            self.assertNotIn('報告した子',n['text'])

    def test_test_sources_do_not_store_token_shaped_literals(self):
        import re
        for p in Path(__file__).parent.glob('test_*.py'):
            self.assertIsNone(re.search('xox'+'[bp]-',p.read_text()),str(p))

    def test_docs_distinguish_internal_check_and_real_post_confirmation(self):
        root=Path(__file__).resolve().parents[1]
        docs=(root/'docs/upgrade-v1.md').read_text()
        self.assertIn('第二段階',docs)
        self.assertIn('chat:write',docs)
        self.assertIn('うさ', (root/'docs/verification.md').read_text())
