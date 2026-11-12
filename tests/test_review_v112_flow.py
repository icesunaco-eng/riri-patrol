import unittest
from dataclasses import replace
from unittest.mock import patch
import test_patrol_flow as flows
from riri.errors import SafetyError, RemoteError
from riri.slack import UserTokenUnavailable
from riri.state import StateStore
from riri.patrol import Patrol
import test_review_v103_flow as logflows

class V112Flows(unittest.TestCase):
    setUp=flows.FlowTests.setUp
    tearDown=flows.FlowTests.tearDown
    run_once=flows.FlowTests.run_once
    state=flows.FlowTests.state
    add_request=flows.FlowTests.add_request

    def test_auth_success_missing_scope_twelve_runs_one_episode(self):
        self.cfg=replace(self.cfg,slack_report_user_token='dummy-owner-token')
        self.slack.report_user_verified=True
        self.slack.wants_report_user=lambda ch,kind: ch==self.cfg.report_channel and kind in ('instruction_relay','reports')
        auth=self.slack.auth_test
        def auth_ok():
            self.slack.report_user_verified=True
            return auth()
        self.slack.auth_test=auth_ok
        post=self.slack.post
        def denied(ch,text,**kw):
            if self.slack.wants_report_user(ch,kw.get('kind')):
                self.slack.report_user_verified=False
                raise UserTokenUnavailable()
            return post(ch,text,dry_run=kw.get('dry_run',False))
        self.slack.post=denied
        self.add_request(recipients='@みに')
        for _ in range(12):
            self.run_once(live=True);self.clock.advance(300)
        s=self.state(True)
        self.assertEqual(len([k for k in s['outbox'] if k.startswith('report-user-failed:')]),1)
        self.assertEqual(sum('代理投稿を待機' in str(e) for e in s['log_buffer']),1)
        self.assertNotIn('resolved_at',s['outbox']['report-user-failed:1'])
        self.slack.post=lambda ch,text,**kw:post(ch,text,dry_run=kw.get('dry_run',False))
        self.run_once(live=True)
        self.assertIn('resolved_at',self.state(True)['outbox']['report-user-failed:1'])

    def test_preflight_failure_does_not_consume_post_budget_and_has_reference(self):
        self.add_request()
        error=SafetyError('preflight');error.not_sent=True
        self.slack.error=error
        observed=[]
        original=Patrol.drain
        def drain(p,*a,**kw):
            result=original(p,*a,**kw)
            observed.append((p.post_count,len(self.slack.posts)))
            return result
        with patch.object(Patrol,'drain',drain):
            self.run_once(live=True)
        self.assertTrue(all(count==posts for count,posts in observed))
        notice=self.state(True)['outbox']['preflight:instruction:💛20990101-1']
        self.assertIn('💛20990101-1',notice['text'])

    def test_notion_prewrite_remote_error_waits_normal_interval(self):
        logflows.LogIsolationFlowTests.seed(self)
        error=RemoteError('prewrite read unavailable',uncertain=False);error.not_sent=True
        self.notion.append_error=error
        with patch.object(self.notion,'append_rows',wraps=self.notion.append_rows) as append:
            self.run_once(live=True,flush=True)
            self.assertIsNone(self.state(True)['log_batch'])
            self.notion.append_error=None
            self.clock.advance(300);self.run_once(live=True)
            self.assertEqual(append.call_count,1)
            self.clock.advance(175*60);self.run_once(live=True)
            self.assertEqual(append.call_count,2)
            self.assertEqual(len(self.notion.rows),2)

    def test_ack_episode_close_is_atomic_with_posted_even_if_save_crashes(self):
        import copy
        from types import SimpleNamespace
        from riri.state import initial_state
        p=Patrol.__new__(Patrol)
        p.config=self.cfg;p.clock=self.clock;p.slack=SimpleNamespace(report_user_verified=True)
        p.state=initial_state('live');p.started_at=self.clock()
        p.state['outbox']['report-user-failed:1']={'status':'posted','created_at':self.clock().isoformat(),'last_failure_at':self.clock().isoformat()}
        item={'kind':'notice','status':'sending'}
        p.state['outbox']['test']=item
        snapshots=[]
        def interrupted_save():
            snapshots.append(copy.deepcopy(p.state))
            raise RuntimeError('simulated crash')
        p.save=interrupted_save
        with self.assertRaises(RuntimeError):
            p.acknowledge('test',item,'1.0',owner_post_confirmed=True,owner_success_at=self.clock())
        self.assertEqual(len(snapshots),1)
        self.assertEqual(snapshots[0]['outbox']['test']['status'],'posted')
        self.assertIn('resolved_at',snapshots[0]['outbox']['report-user-failed:1'])

    def test_reconcile_only_exact_owner_evidence_closes_episode(self):
        from types import SimpleNamespace
        from riri.state import initial_state
        for owner in (False,True):
            p=Patrol.__new__(Patrol)
            p.config=replace(self.cfg,mode='live');p.clock=self.clock;p.started_at=self.clock()
            p.state=initial_state('live');p.events_added=0
            p.state['outbox']['report-user-failed:1']={'status':'posted','created_at':self.clock().isoformat(),'last_failure_at':self.clock().isoformat()}
            item={'kind':'notice','status':'sending','channel':self.cfg.report_channel,'marker':'rp:abc'}
            p.state['outbox']['test']=item
            ts=str(int(self.clock().timestamp()))+'.000001'
            self.clock.advance(1)
            proof=(item['channel'],item['marker'],ts) if owner else None
            p.slack=SimpleNamespace(report_user_verified=False,find_post=lambda *a:ts,last_found_owner_post=proof)
            p.check_time=lambda:None;p.save=lambda:None
            p.reconcile(keys={'test'})
            self.assertEqual(item['status'],'posted')
            self.assertEqual('resolved_at' in p.state['outbox']['report-user-failed:1'],owner)

    def test_old_or_invalid_success_cannot_clear_new_failure(self):
        from types import SimpleNamespace
        from datetime import timedelta
        from riri.state import initial_state
        p=Patrol.__new__(Patrol)
        p.config=self.cfg;p.clock=self.clock;p.slack=SimpleNamespace(report_user_verified=False)
        p.state=initial_state('live');p.save=lambda:None
        notice={'status':'queued','created_at':(self.clock()-timedelta(hours=2)).isoformat(),
                'last_failure_log_at':(self.clock()-timedelta(minutes=30)).isoformat(),
                'last_failure_at':self.clock().isoformat()}
        p.state['outbox']['report-user-failed:1']=notice
        from tests.test_review_v10_state_compatibility import historical_reader
        old=historical_reader()(self.root,'live')
        old.save(p.state)
        self.assertEqual(old.load()['outbox']['report-user-failed:1'],notice)
        self.assertEqual(StateStore(self.root,'live').load()['outbox']['report-user-failed:1'],notice)
        for success in (None,self.clock()-timedelta(seconds=1),self.clock().replace(tzinfo=None)):
            p.report_user_health(post_succeeded=True,success_at=success)
            self.assertNotIn('resolved_at',notice)
            self.assertEqual(notice['status'],'queued')
        p.report_user_health(post_succeeded=True,success_at=self.clock()+timedelta(seconds=1))
        self.assertIn('resolved_at',notice)
        self.assertEqual(notice['status'],'cancelled')
