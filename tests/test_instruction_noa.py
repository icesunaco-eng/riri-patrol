"""Generated instruction wakes: offline fixtures and no real credentials."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from riri.instruction_noa import InstructionNoaWake, validate_instruction_wakes
from riri.noa import CHANNEL, NoaRateLimit, NoaUncertain, NoaRejected
from riri.errors import StopRequested, SafetyError
from riri.eventlog import iso
from riri.state import initial_state
from riri.llm import DraftService
from tests import test_noa_wake as f
from tests.test_patrol_flow import request
from tests.test_review_v10_state_compatibility import historical_reader


class InstructionWakeTests(unittest.TestCase):
    setUp = f.WakeTests.setUp
    new_patrol = f.WakeTests.new_patrol
    begin = f.WakeTests.begin
    message = f.WakeTests.message
    restart = f.WakeTests.restart

    def run_wake(self):
        self.begin()
        InstructionNoaWake(self.p, self.client).run()

    def source(self, n=1, status='posted', recipients=None):
        rid = '💛20990101-' + str(n)
        self.p.state['requests'][rid] = {'recipients': recipients or ['のあ']}
        source = self.p.queue('instruction:' + rid, CHANNEL,
            '@のあ\n【りりからのお願い】\n依頼ID：' + rid + '\n安全な試験本文',
            'instruction', request_id=rid,
            noa_instruction_wake={'status':'queued', 'attempt_at':None})
        source.update(status=status, posted_at=iso(self.clock()), ts=str(int(self.clock().timestamp())) + f'.{n:06d}')
        self.p.save()
        return source

    def test_confirmed_generated_body_and_link_once_per_request(self):
        source = self.source()
        self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'done')
        self.assertIn(source['text'], self.client.calls[0])
        self.assertIn('/p' + source['ts'].replace('.', ''), self.client.calls[0])
        self.restart(); self.run_wake(); self.run_wake()
        self.assertEqual(len(self.client.calls), 1)

    def test_not_posted_queued_sending_held_blocked_cancelled_no_fire(self):
        for n, status in enumerate(('queued','sending','held','blocked','cancelled'), 1):
            self.source(n, status)
        self.run_wake()
        self.assertFalse(self.client.calls)

    def test_post_becomes_confirmed_then_fires(self):
        source = self.source(status='held')
        self.run_wake()
        source['status'] = 'posted'; self.p.save()
        self.run_wake()
        self.assertEqual(len(self.client.calls), 1)

    def test_activation_preserves_new_unconfirmed_original_until_confirmation(self):
        for status in ('queued', 'sending', 'held'):
            with self.subTest(status=status):
                self.p.state = initial_state('live')
                old = self.source(1, 'queued')
                old.pop('posted_at')
                self.clock.advance(1)
                self.begin()
                fresh = self.source(2, status)
                fresh.pop('posted_at')
                self.p.save()
                InstructionNoaWake(self.p, self.client).run()
                self.assertEqual(old['noa_instruction_wake']['status'], 'disabled')
                self.assertEqual(fresh['noa_instruction_wake']['status'], 'queued')
                before = len(self.client.calls)
                self.restart()
                fresh = self.p.state['outbox']['instruction:💛20990101-2']
                self.run_wake()
                self.assertEqual(len(self.client.calls), before)
                fresh.update(status='posted', posted_at=iso(self.clock()))
                self.p.save()
                self.run_wake()
                self.assertEqual(len(self.client.calls), before + 1)
                self.restart(); self.run_wake()
                self.assertEqual(len(self.client.calls), before + 1)

    def test_reports_other_bots_and_historical_posts_never_opt_in(self):
        source = self.source()
        del source['noa_instruction_wake']
        self.p.queue('reports:example', CHANNEL, 'のあ 完了', 'reports')
        self.message(text='@のあ bot text', user='UBOT', bot_id='BBOT')
        self.message(2, text='@のあ own text', user=self.cfg.expected_user_id)
        self.run_wake()
        self.assertFalse(self.client.calls)

    def test_recipient_without_noa_rejected(self):
        source = self.source(recipients=['みに'])
        self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'rejected')
        self.assertFalse(self.client.calls)

    def test_off_retires_intent_no_replay_on_reenable(self):
        source = self.source()
        self.p.config = replace(self.cfg, noa_wake_enabled=False)
        self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'disabled')
        self.p.config = self.cfg
        self.clock.advance(1); self.run_wake()
        self.assertFalse(self.client.calls)

    def test_practice_never_fires(self):
        source = self.source()
        self.p.config = replace(self.cfg, mode='practice')
        self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'simulated')
        self.assertFalse(self.client.calls)

    def test_stop_before_attempt_preserves_no_external_calls(self):
        self.source(); self.cfg.stop.touch()
        with self.assertRaises(StopRequested): self.run_wake()
        self.assertFalse(self.client.calls)

    def test_stop_just_before_fire_known_unsent_can_resume(self):
        source = self.source()
        error=StopRequested('synthetic'); error.not_sent=True
        self.client.responses=[error]
        with self.assertRaises(StopRequested): self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'queued')
        self.assertIsNone(source['noa_instruction_wake']['attempt_at'])
        self.run_wake()
        self.assertEqual(source['noa_instruction_wake']['status'], 'done')

    def test_crash_after_sending_fence_never_retries(self):
        source = self.source()
        self.client.responses=[KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): self.run_wake()
        self.restart(); self.run_wake()
        intent=self.p.state['outbox']['instruction:'+source['request_id']]['noa_instruction_wake']
        self.assertEqual(intent['status'], 'held')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len([k for k in self.p.state['outbox'] if k.startswith('noa-instruction:')]),1)

    def test_durable_sending_before_transport(self):
        source=self.source()
        def check():
            stored=self.p.store.load()['outbox']['instruction:'+source['request_id']]['noa_instruction_wake']
            self.assertEqual(stored['status'],'sending')
            self.assertIsNotNone(stored['attempt_at'])
        self.client.callback=check
        self.run_wake()

    def test_uncertain_429_and_rejected_never_retry(self):
        for n, error in enumerate((NoaUncertain(), NoaRateLimit(0), NoaRejected()),1):
            source=self.source(n)
            self.client.responses=[error]
            self.run_wake(); self.restart(); self.run_wake()
            self.assertEqual(len(self.client.calls),n)

    def test_shared_one_fire_per_run_with_human(self):
        self.source(); self.message(99)
        self.run_wake()
        self.assertEqual(len(self.client.calls),1)
        self.run_wake()
        self.assertEqual(len(self.client.calls),2)
        self.assertIn('private source body',self.client.calls[-1])

    def test_shared_hourly_quota_both_directions(self):
        for n in range(1,11): self.source(n)
        for _ in range(10): self.run_wake()
        self.message(99); self.source(11)
        self.run_wake()
        self.assertEqual(len(self.client.calls),10)
        self.clock.advance(3601); self.source(12); self.run_wake()
        self.assertEqual(len(self.client.calls),11)
        self.assertIn('20990101-12',self.client.calls[-1])
        self.assertEqual(self.p.state['outbox']['instruction:💛20990101-11']['noa_instruction_wake']['status'],'disabled')

    def test_human_attempts_also_limit_generated(self):
        for n in range(1,11):
            self.message(n); self.run_wake()
        self.source(99); self.run_wake()
        self.assertEqual(len(self.client.calls),10)

    def test_repost_same_request_id_does_not_reset_fence(self):
        source=self.source(); self.run_wake()
        source.update(status='posted',ts='5000000000.123456')
        self.run_wake()
        self.assertEqual(len(self.client.calls),1)

    def test_secret_not_forwarded(self):
        source=self.source(); source['text']+=' unit-token'
        self.run_wake()
        self.assertFalse(self.client.calls)
        self.assertEqual(source['noa_instruction_wake']['status'],'rejected')

    def test_invalid_permalink_prevents_fire(self):
        self.source()
        self.slack.permalink=lambda *a:'https://evil.example/'
        with self.assertRaises(SafetyError):self.run_wake()
        self.assertFalse(self.client.calls)

    def test_optional_metadata_reads_in_historical_store(self):
        self.source(); self.run_wake()
        old=historical_reader()(self.cfg.root,'live').load()
        self.assertEqual(old,self.p.store.load())

    def test_malformed_metadata_fails_closed(self):
        source=self.source(); source['noa_instruction_wake']['status']='done'
        with self.assertRaises(ValueError): validate_instruction_wakes(self.p.state['outbox'])
        self.p.save()
        self.assertEqual(self.p.run()['status'],'state_error')
        self.assertFalse(self.client.calls)

    def test_full_patrol_new_instruction_and_held_projection(self):
        self.p.draft=DraftService(None,enabled=False)
        self.p.notion.blocks[self.cfg.requests_page]=[request(recipients='@のあ')]
        self.client.responses=[NoaUncertain()]
        result=self.p.run()
        self.assertEqual(result['status'],'held')
        source=self.p.state['outbox']['instruction:💛20990101-1']
        self.assertEqual(source['status'],'posted')
        self.assertEqual(source['noa_instruction_wake']['status'],'held')
        self.assertEqual(len(self.client.calls),1)
        self.assertTrue(self.p.recovery_unresolved())

    def test_new_instruction_when_off_has_no_intent(self):
        self.p.config=replace(self.cfg,noa_wake_enabled=False)
        self.p.draft=DraftService(None,enabled=False)
        self.p.notion.blocks[self.cfg.requests_page]=[request(recipients='@のあ')]
        self.p.run()
        source=self.p.state['outbox']['instruction:💛20990101-1']
        self.assertNotIn('noa_instruction_wake',source)
        self.assertFalse(self.client.calls)
