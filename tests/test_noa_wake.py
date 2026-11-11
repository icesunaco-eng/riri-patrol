"""Mock contract tests only. No live Slack/routine account or credential used."""
from dataclasses import replace
from datetime import timedelta
import io
import hashlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from riri.config import Config
from riri.errors import RemoteError, SafetyError, StopRequested
from riri.noa import (CHANNEL, NoaClient, NoaRateLimit, NoaRejected, NoaUncertain,
                      NoaWake, session_evidence, validate_endpoint)
from riri.patrol import Patrol
from riri.slack import SlackClient
from riri.state import StateStore, initial_state
from tests.test_patrol_flow import Clock, FakeSlack, FakeNotion

URL = 'https://api.anthropic.com/v1/claude_code/routines/routine_test/fire'
RESULT = {'type':'routine_fire', 'claude_code_session_id': 'session_test', 'claude_code_session_url': 'https://claude.ai/code/session_test'}


class SourceSlack(FakeSlack):
    def __init__(self):
        super().__init__()
        del self.history
        self.messages, self.reads, self.links = [], [], []
        self.fail = None
    def history(self, channel, oldest):
        self.reads.append((channel, oldest))
        if self.fail:
            raise self.fail
        return self.messages[:]
    def permalink(self, channel, ts):
        self.links.append(ts)
        return 'https://test.slack.com/archives/' + channel + '/p' + ts.replace('.', '')
    def post(self, channel, text, *, dry_run=False):
        ts = str(len(self.posts) + 1) + '.000001'
        self.posts.append((channel, text, ts))
        return ts


class MockFire:
    def __init__(self):
        self.calls, self.responses = [], []
        self.callback = None
    def validate(self):
        pass
    def fire(self, text):
        self.calls.append(text)
        if self.callback:
            self.callback()
        response = self.responses.pop(0) if self.responses else RESULT
        if isinstance(response, BaseException):
            raise response
        return response


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.cfg = Config(Path(self.tmp.name), mode='live', expected_user_id='URIRI', expected_bot_id='BRIRI',
                          noa_wake_enabled=True, noa_fire_url=URL, noa_fire_token='unit-token')
        self.slack, self.client = SourceSlack(), MockFire()
        self.p = self.new_patrol()
        self.p.state = initial_state('live')
        self.begin()
        NoaWake(self.p, self.client).run()
        self.clock.advance(1)
    def new_patrol(self):
        return Patrol(self.cfg, self.slack, FakeNotion(self.cfg), None, clock=self.clock, monotonic=lambda: 0,
                      noa_client=self.client)
    def begin(self):
        self.p.started_at = self.clock()
        self.p.started_mono = 0
        self.p.events_added = self.p.post_count = self.p.instruction_count = 0
    def message(self, number=1, **kw):
        ts = str(int(self.clock().timestamp())) + '.' + f'{number:06d}'
        m = dict(ts=ts, user='UHUMAN', text='@のあ private source body')
        m.update(kw)
        self.slack.messages.append(m)
        return m
    def run_wake(self):
        self.begin()
        NoaWake(self.p, self.client).run()
    def items(self):
        return list(self.p.state['noa_wake']['items'].values())
    def restart(self):
        self.p = self.new_patrol()
        self.p.state = self.p.store.load()
        self.begin()
    def test_queued_source_edit_does_not_change_authorized_payload(self):
        self.message(1)
        second = self.message(2)
        self.run_wake()
        second['text'] = '@のあ edited body'
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.items()[-1]['status'], 'rejected')
    def test_known_secrets_in_source_never_forwarded(self):
        self.message(text='@のあ unit-token')
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertEqual(self.items()[0]['status'], 'rejected')
    def test_sending_intent_is_durable_when_fire_begins(self):
        self.message()
        def check():
            state = self.p.store.load()['noa_wake']
            self.assertEqual(next(iter(state['items'].values()))['status'], 'sending')
            self.assertEqual(len(state['attempts']), 1)
        self.client.callback = check
        self.run_wake()
    def test_successful_retry_still_counts_hourly_quota(self):
        self.message()
        self.client.responses = [NoaRateLimit(0), RESULT]
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.items()[0]['status'], 'done')
        self.assertEqual(len(self.p.state['noa_wake']['attempts']), 2)
    def test_conflicting_duplicate_history_cannot_advance_watermark(self):
        msg = self.message()
        self.slack.messages.append(dict(msg, text='@のあ conflicting content'))
        before = copy.deepcopy(self.p.state['noa_wake'])
        with self.assertRaises(SafetyError):
            self.run_wake()
        self.assertEqual(before, self.p.state['noa_wake'])
        self.assertFalse(self.client.calls)
    def test_invalid_quota_and_dedup_state_fail_closed(self):
        self.message()
        self.run_wake()
        baseline = copy.deepcopy(self.p.state)
        changes = [lambda s,i: i.update(status='retry', attempt_count=2, retry_at=self.clock().isoformat()),
                   lambda s,i: i.update(status='queued', attempt_count=2),
                   lambda s,i: i.update(status='sending', attempt_count=0),
                   lambda s,i: i.update(status='done', attempt_count=0),
                   lambda s,i: s.update(watermark='0.000001'),
                   lambda s,i: s.update(attempts=[]),
                   lambda s,i: i.update(attempt_count=True)]
        for change in changes:
            self.p.state = copy.deepcopy(baseline)
            s = self.p.state['noa_wake']; item = next(iter(s['items'].values()))
            change(s, item)
            self.p.save()
            with self.assertRaises(SafetyError): self.p.store.load()
    def test_secret_pattern_source_never_forwarded(self):
        self.message(text='@のあ xoxb-never-forward-this')
        self.run_wake()
        self.assertFalse(self.client.calls)
    def test_disabling_feature_defers_pending_noa_notice(self):
        self.message()
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.p.drain()
        self.assertFalse(self.slack.posts)
        self.assertEqual(next(iter(self.p.state['outbox'].values()))['status'],'queued')
    def test_status_and_manual_review_are_local_terminal_and_keep_dedup(self):
        from riri.resolve import held_items, resolve_held
        self.message()
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        projected = [i for i in held_items(self.p.state) if i['type']=='noa']
        self.assertEqual(len(projected),1)
        self.assertEqual(projected[0]['allowed_outcomes'],['reviewed'])
        self.assertEqual(projected[0]['attempt_count'],1)
        key = projected[0]['id']
        before_attempts = self.p.state['noa_wake']['attempts'][:]
        outcome = resolve_held(self.cfg, 'noa', key, 'reviewed', clock=self.clock)
        self.assertEqual(outcome['posts'],0)
        self.restart()
        self.assertEqual(self.items()[0]['status'],'reviewed')
        self.assertEqual(self.p.state['noa_wake']['attempts'], before_attempts)
        self.assertEqual(next(iter(self.p.state['outbox'].values()))['status'],'cancelled')
        self.assertFalse([i for i in held_items(self.p.state) if i['type']=='noa'])
        self.run_wake()
        self.assertEqual(len(self.client.calls),1)
        with self.assertRaises(SafetyError): resolve_held(self.cfg, 'noa', key, 'not_posted',clock=self.clock)
        with self.assertRaises(SafetyError): resolve_held(self.cfg, 'noa', key, 'reviewed',clock=self.clock)
    def test_off_freezes_sending_noa_notice_and_hold_descendants(self):
        self.message()
        self.client.responses = [NoaUncertain()]
        self.run_wake()
        key, notice = next(iter(self.p.state['outbox'].items()))
        notice['status'] = 'sending'
        self.p.queue('hold:'+key+':1', self.cfg.report_channel, 'safe hold notice',
                     'hold_notice', held_key=key)
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.p.save()
        self.assertEqual(self.p.run()['status'], 'held')
        self.assertEqual(self.slack.find_calls, 0)
        self.assertFalse(self.slack.posts)
        self.assertEqual(self.p.state['outbox'][key]['status'],'sending')
    def test_activation_has_no_history_or_backlog(self):
        self.assertEqual(self.slack.reads, [])
        self.message(ts='1.000001')
        self.run_wake()
        self.assertEqual(self.client.calls, [])
    def test_success_payload_and_atomic_dedup_after_restart(self):
        msg = self.message()
        self.run_wake()
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn('UHUMAN', self.client.calls[0])
        self.assertIn('JST', self.client.calls[0])
        self.assertIn(msg['text'], self.client.calls[0])
        self.assertIn('/p' + msg['ts'].replace('.', ''), self.client.calls[0])
        row = self.p.state['log_buffer'][-1]['row']
        self.assertEqual(row[2], '@のあ のメッセージを拾った')
        self.assertEqual(row[5], 'のあを起こした')
        self.assertEqual(row[7], RESULT['claude_code_session_url'])
        for path in self.cfg.root.rglob('*'):
            if path.is_file():
                raw = path.read_text()
                self.assertNotIn('private source body', raw)
                self.assertNotIn('unit-token', raw)
                self.assertNotIn(URL, raw)
    def test_skip_all_bot_metadata_and_self(self):
        for i, opts in enumerate([{'user':'U0CFH9JRCKV'}, {'user':'URIRI'}, {'bot_id':''},
                                  {'subtype':''}, {'bot_profile':{}}, {'user':None}, {'text':'のあ'}], 1):
            self.message(i, **opts)
        self.run_wake()
        self.assertEqual(self.items(), [])
    def test_one_attempt_per_run_and_ten_per_hour(self):
        for i in range(1, 13):
            self.message(i)
        for i in range(12):
            self.run_wake()
            self.assertLessEqual(len(self.client.calls), min(i + 1, 10))
        self.assertEqual(len(self.client.calls), 10)
        self.assertEqual(sum(i['status']=='queued' for i in self.items()), 2)
        self.clock.advance(3601)
        self.run_wake()
        self.assertEqual(len(self.client.calls), 11)
    def test_429_retry_only_later_once_and_one_notification(self):
        self.message()
        self.client.responses = [NoaRateLimit(60), NoaRateLimit(1)]
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.items()[0]['status'], 'retry')
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)
        self.clock.advance(60)
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.items()[0]['status'], 'rejected')
        self.run_wake()
        self.assertEqual(len(self.p.state['outbox']), 1)
        self.assertEqual(len(self.p.state['noa_wake']['attempts']), 2)
    def test_timeout_malformed_and_server_failure_never_resend(self):
        for result in [TimeoutError('unit-token'), NoaUncertain(), {},
                       {'claude_code_session_id':'s', 'claude_code_session_url':'https://evil.test/s'}]:
            with self.subTest(result=type(result).__name__):
                self.message(len(self.slack.messages)+1)
                self.client.responses = [result]
                before = len(self.client.calls)
                self.run_wake()
                self.restart()
                self.run_wake()
                self.assertEqual(len(self.client.calls), before + 1)
                self.assertEqual(self.items()[-1]['status'], 'held')
    def test_crash_after_sending_persist_holds_even_without_call(self):
        m = self.message()
        self.p.state['noa_wake']['items'][CHANNEL+':'+m['ts']] = dict(channel=CHANNEL, ts=m['ts'],status='sending',attempt_count=1, source_user=m['user'], source_digest=hashlib.sha256(m['text'].encode()).hexdigest())
        self.p.state['noa_wake']['attempts'].append(self.clock().isoformat())
        self.p.save()
        self.restart()
        self.run_wake()
        self.assertEqual(self.items()[0]['status'], 'held')
        self.assertFalse(self.client.calls)
        self.assertEqual(len(self.p.state['outbox']), 1)
    def test_stop_before_scan_and_just_before_open(self):
        self.message()
        self.cfg.stop.touch()
        with self.assertRaises(StopRequested):
            self.run_wake()
        self.assertFalse(self.client.calls)
        self.cfg.stop.unlink()
        def check():
            self.cfg.stop.touch()
            self.p.check_time()
        self.client = NoaClient(URL, 'unit-token', guard=check, clock=self.clock)
        self.p.noa_client = self.client
        with self.assertRaises(StopRequested):
            self.run_wake()
        self.assertEqual(self.items()[0]['status'], 'queued')
        self.assertEqual(self.p.state['noa_wake']['attempts'], [])
    def test_stop_during_send_is_held(self):
        self.message()
        self.client.responses = [StopRequested('stop')]
        with self.assertRaises(StopRequested):
            self.run_wake()
        self.assertEqual(self.items()[0]['status'], 'held')
    def test_practice_never_fires_or_logs_body(self):
        self.cfg = replace(self.cfg, mode='practice')
        self.p = self.new_patrol()
        self.p.state = initial_state('practice')
        self.run_wake()
        self.clock.advance(1)
        self.message()
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertFalse(self.slack.links)
        self.assertEqual(self.items()[0]['status'], 'simulated')
        self.assertEqual(self.p.state['noa_wake']['attempts'], [])
        self.assertNotIn('private source body', (self.cfg.root/'logs/practice.jsonl').read_text())
    def test_off_has_no_reads_or_state_changes(self):
        self.message()
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        before = json.dumps(self.p.state)
        self.run_wake()
        self.assertEqual(before, json.dumps(self.p.state))
        self.assertFalse(self.slack.reads)
    def test_partial_history_preserves_watermark_and_pending(self):
        self.message(1); self.message(2)
        self.run_wake()
        before = json.dumps(self.p.state['noa_wake'])
        self.slack.fail = RemoteError('partial history')
        with self.assertRaises(RemoteError):
            self.run_wake()
        self.assertEqual(before, json.dumps(self.p.state['noa_wake']))
        self.slack.fail = None
        self.restart()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 2)
    def test_invalid_state_does_not_reset_dedup(self):
        self.p.state['noa_wake']['attempts'] = ['bad']
        self.p.save()
        with self.assertRaises(SafetyError):
            self.p.store.load()
    def test_full_patrol_hook_after_auth_and_stop_precheck(self):
        self.message()
        self.p.save()
        self.assertEqual(self.p.run()['status'], 'ok')
        self.assertEqual(len(self.client.calls), 1)
        self.cfg.stop.touch()
        before = len(self.slack.reads)
        self.assertEqual(self.p.run()['status'], 'stopped')
        self.assertEqual(len(self.slack.reads), before)


class Response:
    status = 200
    def __init__(self, data=RESULT):
        self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, n): return json.dumps(self.data).encode()


class Opener:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []
    def open(self, req, timeout):
        self.calls.append(req)
        if isinstance(self.response, Exception): raise self.response
        return self.response


class ClientTests(unittest.TestCase):
    def client(self, opener, guard=lambda: None):
        return NoaClient(URL, 'unit-token', guard=guard, clock=Clock(), opener=opener)
    def test_deadline_after_open_is_uncertain(self):
        from riri.errors import TimeBudgetExceeded
        calls=[]
        def guard():
            calls.append(1)
            if len(calls)==2: raise TimeBudgetExceeded('expired')
        with self.assertRaises(NoaUncertain):
            self.client(Opener(), guard).fire('sample')
    def test_exact_headers_and_json_contract(self):
        opener = Opener()
        result = self.client(opener).fire('sample')
        req = opener.calls[0]
        headers = dict((k.lower(), v) for k, v in req.header_items())
        self.assertEqual(headers['authorization'], 'Bearer unit-token')
        self.assertEqual(headers['anthropic-beta'], 'experimental-cc-routine-2026-04-01')
        self.assertEqual(headers['anthropic-version'], '2023-06-01')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(json.loads(req.data), {'text':'sample'})
        self.assertEqual(session_evidence(result), RESULT['claude_code_session_url'])
    def test_malicious_endpoints_fail_closed(self):
        for url in ['http://api.anthropic.com/v1/claude_code/routines/x/fire', URL+'?x=1', URL+'#x',
                    URL.replace('api.anthropic.com','api.anthropic.com.evil.test'),
                    URL.replace('api.anthropic.com','unit-token@api.anthropic.com'),
                    URL.replace('api.anthropic.com','api.anthropic.com:443'),
                    URL.replace('routine_test','..'), URL.replace('routine_test','%2e%2e'),
                    'https://localhost/fire', 'https://api.anthropic.com/v1/messages']:
            with self.subTest(url=url), self.assertRaises(SafetyError):
                validate_endpoint(url)
    def test_http_outcomes_no_immediate_retry_or_leaking_errors(self):
        for code, cls in [(429, NoaRateLimit), (400, NoaRejected), (302, NoaRejected), (500, NoaUncertain), (529, NoaUncertain)]:
            opener = Opener(urllib.error.HTTPError(URL, code, 'unit-token', {'Retry-After':'45'}, io.BytesIO(b'unit-token')))
            with self.subTest(code=code), self.assertRaises(cls) as caught:
                self.client(opener).fire('sample')
            self.assertNotIn('unit-token', str(caught.exception))
            self.assertEqual(len(opener.calls), 1)
    def test_invalid_retry_after_rejects(self):
        for value in ['nan', '-1', None, 'inf', 'bad']:
            opener = Opener(urllib.error.HTTPError(URL,429,'error', {'Retry-After':value},None))
            with self.subTest(value=value), self.assertRaises(NoaRejected):
                self.client(opener).fire('sample')
    def test_missing_or_mismatched_session_identity_holds(self):
        for result in [{}, {'claude_code_session_url':'https://claude.ai/code/session_test'},
                       {'claude_code_session_id':'different', 'claude_code_session_url':RESULT['claude_code_session_url']},
                       {'claude_code_session_id':'s', 'claude_code_session_url':'https://claude.ai/code/s?token=unit-token'}]:
            with self.subTest(result=result), self.assertRaises(NoaUncertain):
                self.client(Opener(Response(result))).fire('sample')
    def test_connection_error_is_sanitized_uncertainty(self):
        with self.assertRaises(NoaUncertain) as caught:
            self.client(Opener(OSError('unit-token'))).fire('sample')
        self.assertNotIn('unit-token', str(caught.exception))
    def test_default_opener_disables_redirects(self):
        from riri.http import NoRedirect
        client = NoaClient(URL,'unit-token',guard=lambda:None,clock=Clock())
        handlers = [h for h in client.opener.handlers if isinstance(h, NoRedirect)]
        self.assertEqual(len(handlers), 1)
        self.assertIsNone(handlers[0].redirect_request(None,None,302,'',{},'https://evil.test'))
    def test_config_defaults_and_env_only(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config.load(root, environ={'NOA_WAKE_ENABLED':'true', 'NOA_FIRE_TOKEN':'unit-token'})
            self.assertFalse(cfg.noa_wake_enabled)
            self.assertEqual(cfg.noa_fire_token,'')
            Path(root,'.env').write_text('NOA_WAKE_ENABLED=true\nNOA_FIRE_TOKEN=unit-token\nNOA_FIRE_URL='+URL)
            cfg = Config.load(root)
            self.assertTrue(cfg.noa_wake_enabled)
            self.assertIn('unit-token',cfg.secrets)
            self.assertNotIn('unit-token',repr(cfg))
    def test_history_pagination_failure_and_cursor_loop(self):
        class Transport:
            def __init__(self, results): self.results=results
            def request(self,*a,**kw): return self.results.pop(0)
        for second in [{'ok':True,'messages':[], 'has_more':True},
                       {'ok':True,'messages':[], 'response_metadata':{'next_cursor':'same'}}]:
            transport=Transport([{'ok':True,'messages':[{'ts':'1.000001','text':'@のあ'}],
                                 'response_metadata':{'next_cursor':'same'}}, second])
            slack=SlackClient('xoxb-unit','BUNIT','UUNIT',transport)
            slack.verified=True; slack._identity=('xoxb-unit','BUNIT','UUNIT')
            with self.assertRaises(RemoteError): slack.history(CHANNEL,'0.000001')
