"""Actual Patrol + SlackClient + disk state, with synthetic HTTP only."""
import copy
from dataclasses import replace
import unittest
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
import test_patrol_flow as flows
from riri.slack import SlackClient
from riri.state import StateStore, initial_state
from riri.eventlog import iso
from riri.errors import RemoteError

class SimulatedCrash(BaseException): pass

class Transport:
    def __init__(self): self.posts=[]; self.hide_history=False; self.crash=False; self.uncertain=False; self.calls=[]
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,copy.deepcopy(kwargs)))
        user=kwargs['headers']['Authorization'].startswith(('Bearer ' + 'xoxp' + '-'))
        endpoint=urlsplit(url).path.rsplit('/',1)[-1]
        if endpoint=='auth.test':
            return {'ok':True,'user_id':'UOWNER' if user else 'UTEST','team_id':'TTEST',**({} if user else {'bot_id':'BTEST'})}
        if endpoint=='chat.postMessage':
            data=kwargs['payload']
            ts=str(len(self.posts)+1)+'.000001'
            self.posts.append(dict(channel=data['channel'],text=data['text'],ts=ts,
                user='UOWNER' if user else 'UTEST',**({'bot_id':'BAPP','app_id':'ATEST'} if user else {'bot_id':'BTEST'})))
            if self.crash: self.crash=False; raise SimulatedCrash()
            if self.uncertain: self.uncertain=False; raise RemoteError('synthetic timeout',uncertain=True)
            return {'ok':True,'ts':ts}
        if endpoint=='conversations.history':
            if user: raise AssertionError('user token must never read')
            channel=parse_qs(urlsplit(url).query)['channel'][0]
            return {'ok':True,'messages':[] if self.hide_history else [p for p in self.posts if p['channel']==channel]}
        raise AssertionError('unexpected endpoint')

class UserPatrolIntegrationTests(unittest.TestCase):
    tearDown=flows.FlowTests.tearDown
    run_once=flows.FlowTests.run_once
    state=flows.FlowTests.state
    def setUp(self):
        flows.FlowTests.setUp(self)
        for name,value in [('REPORT_OWNER_ID','UOWNER'),('USER_REPORT_CHANNEL','CREPORT')]:
            p=patch('riri.slack.'+name,value);p.start();self.addCleanup(p.stop)
        self.transport=Transport()
        self.cfg=replace(self.cfg,report_channel='CREPORT',slack_report_user_token=('xoxp' + '-synthetic-only'))
        self.notion.config=self.cfg
        self.client()
        state=initial_state('live')
        state.update(first_run=False,last_log_flush_at=iso(self.clock()),last_summary_at=iso(self.clock()))
        for n in range(5):
            marker='rp:'+format(n,'024x')
            state['outbox']['synthetic-'+str(n)]={'status':'queued','channel':'CREPORT',
                'text':'<@U0TESTICE000>\nsynthetic report\n'+marker,'marker':marker,
                'kind':'instruction_relay','created_at':iso(self.clock())}
        StateStore(self.root,'live').save(state)
    def client(self, token=('xoxp' + '-synthetic-only')):
        self.cfg=replace(self.cfg,slack_report_user_token=token)
        self.slack=SlackClient(('xoxb' + '-synthetic-only'),'BTEST','UTEST',self.transport,
            report_channel='CREPORT',ice_mention_user_id='U0TESTICE000',report_user_token=token)

    def check_crash_restart(self, token):
        self.transport.crash=True
        with self.assertRaises(SimulatedCrash): self.run_once(live=True)
        state=self.state(True)
        self.assertEqual(state['outbox']['synthetic-0']['status'],'sending')
        self.assertEqual(len(self.transport.posts),1)
        self.client(token)
        self.clock.advance(60)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['synthetic-0']['status'],'posted')
        marker=state['outbox']['synthetic-0']['marker']
        self.assertEqual(sum(marker in p['text'].splitlines() for p in self.transport.posts),1)
        self.assertLessEqual(len(self.transport.posts),5)
        self.clock.advance(60)
        self.run_once(live=True)
        for n in range(5):
            marker=state['outbox']['synthetic-'+str(n)]['marker']
            self.assertEqual(sum(marker in p['text'].splitlines() for p in self.transport.posts),1)

    def test_post_applied_then_process_crash_restart_reconciles_owner(self):
        self.check_crash_restart(('xoxp' + '-synthetic-only'))

    def test_post_applied_then_crash_token_removed_reconciles_using_bot(self):
        self.check_crash_restart('')

    def test_unknown_response_remains_held_across_restart_never_resends(self):
        self.transport.uncertain=True
        self.transport.hide_history=True
        self.run_once(live=True)
        state=self.state(True)
        self.assertEqual(state['outbox']['synthetic-0']['status'],'held')
        marker=state['outbox']['synthetic-0']['marker']
        self.client()
        for _ in range(3):
            self.clock.advance(60);self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['synthetic-0']['status'],'held')
        self.assertEqual(sum(marker in p['text'].splitlines() for p in self.transport.posts),1)

    def test_user_posts_obey_per_run_quota_and_restart(self):
        first=self.run_once(live=True)
        self.assertEqual(first['posts'],4)
        self.assertEqual(len(self.transport.posts),4)
        self.assertEqual(self.state(True)['outbox']['synthetic-4']['status'],'queued')
        self.client()
        self.clock.advance(60)
        second=self.run_once(live=True)
        self.assertEqual(second['posts'],1)
        self.assertEqual(len(self.transport.posts),5)
