import hashlib
import json
from dataclasses import replace
import unittest
from unittest.mock import patch
import test_patrol_flow as flows
from riri.state import StateStore, initial_state
from riri.eventlog import iso
from riri.errors import SafetyError

class V105Flows(unittest.TestCase):
    setUp=flows.FlowTests.setUp
    tearDown=flows.FlowTests.tearDown
    run_once=flows.FlowTests.run_once
    state=flows.FlowTests.state
    add_request=flows.FlowTests.add_request
    add_report=flows.FlowTests.add_report

    def test_previously_invalid_now_valid_never_relay_or_complete(self):
        self.add_request()
        self.run_once(live=True)
        b=flows.report(result='完了（レビューのみ）')
        text=b['paragraph']['rich_text'][0]['text']['content']
        fingerprint=hashlib.sha256(json.dumps(['うさ',b['id'],text],ensure_ascii=False).encode()).hexdigest()
        s=self.state(True);s['incomplete_notified']['report:'+fingerprint]=iso(self.clock())
        StateStore(self.root,'live').save(s)
        self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]]=[b]
        for _ in range(3):
            self.clock.advance();self.run_once(live=True)
        s=self.state(True)
        self.assertEqual(s['requests']['💛20990101-1']['status'],'posted')
        self.assertIn(b['id'],s['seen_reports'])
        self.assertFalse(any(v['kind']=='reports' for v in s['outbox'].values()))

    def test_user_failure_bot_jobs_continue_notice_once_and_hourly_log(self):
        self.cfg=replace(self.cfg,slack_report_user_token=('xoxp' + '-synthetic'))
        self.slack.report_user_verified=False
        self.slack.wants_report_user=lambda ch,kind: ch==self.cfg.report_channel and kind in ('instruction_relay','reports')
        old_post=self.slack.post
        self.slack.post=lambda ch,text,**kw:old_post(ch,text,dry_run=kw.get('dry_run',False))
        self.add_request(recipients='@みに')
        for _ in range(6):
            self.run_once(live=True);self.clock.advance(300)
        s=self.state(True)
        self.assertEqual(s['requests']['💛20990101-1']['status'],'posted')
        self.assertEqual(len([k for k in s['outbox'] if k.startswith('report-user-failed:')]),1)
        self.assertEqual(s['outbox']['mini:💛20990101-1']['status'],'queued')
        self.assertEqual(sum('代理投稿を待機' in str(e) for e in s['log_buffer']),1)
        self.slack.report_user_verified=True
        self.run_once(live=True)
        self.slack.report_user_verified=False
        self.clock.advance();self.run_once(live=True)
        self.assertEqual(len([k for k in self.state(True)['outbox'] if k.startswith('report-user-failed:')]),2)

    def test_known_unsent_slack_safety_retries_after_repair_no_manual_resolve(self):
        self.add_request()
        error=SafetyError('preflight');error.not_sent=True
        self.slack.error=error
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'queued')
        self.clock.advance();self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'posted')
        self.assertEqual(sum(p[0]==self.cfg.instruction_channel for p in self.slack.posts),1)

    def test_old_relay_notice_is_classified_without_mutation(self):
        from riri.patrol import Patrol
        item={'kind':'notice'}
        self.assertEqual(Patrol.outgoing_kind('mini:ID',item),'instruction_relay')
        self.assertEqual(Patrol.outgoing_kind('instruction-gdm:ID',item),'instruction_relay')
        self.assertEqual(Patrol.outgoing_kind('error:ID',item),'notice')
        self.assertEqual(item,{'kind':'notice'})

    def test_known_unwritten_log_retries_next_patrol_without_duplicate(self):
        from test_review_v103_flow import LogIsolationFlowTests
        LogIsolationFlowTests.seed(self)
        error=SafetyError('pre-write header changed');error.not_sent=True
        self.notion.append_error=error
        self.run_once(live=True,flush=True)
        self.assertIsNone(self.state(True)['log_batch'])
        self.notion.append_error=None
        self.clock.advance(300)
        self.run_once(live=True)
        self.assertIsNone(self.state(True)['log_batch'])
        self.assertEqual(self.state(True)['log_buffer'],[])
        self.assertEqual(len(self.notion.rows),2)

    def test_notion_header_failure_proves_no_write(self):
        from riri.notion import NotionClient, LOG_COLUMNS
        from tests.test_api import Fake,page,row
        f=Fake(page([row('changed',LOG_COLUMNS)]))
        n=NotionClient('synthetic-only',f);n._tables['table']='header'
        with self.assertRaises(SafetyError) as caught:
            n.append_rows('table','header',[['a']*8+['#e-test']])
        self.assertTrue(caught.exception.not_sent)
        self.assertEqual([call[0] for call in f.calls],['GET'])

    def test_dry_run_history_probe_failure_is_not_success(self):
        self.slack.probe_report_history=lambda: (_ for _ in ()).throw(SafetyError('unreadable'))
        result=self.run_once()
        self.assertNotEqual(result['status'],'ok')
        self.assertEqual(self.slack.posts,[])

    def test_status_with_user_token_is_offline_and_labels_expected_only(self):
        import patrol as cli
        import io, contextlib
        (self.root/'.env').write_text(('RIRI_MODE=live\nEXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\nSLACK_REPORT_USER_TOKEN=' + 'xoxp' + '-synthetic-only\n'))
        output=io.StringIO()
        with patch.object(cli,'Transport',side_effect=AssertionError('no network')), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--root',str(self.root),'--status']),0)
        self.assertIn('未検証',output.getvalue())
        self.assertNotIn(('xoxp' + '-'),output.getvalue())
