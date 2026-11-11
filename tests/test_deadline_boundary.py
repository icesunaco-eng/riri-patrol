"""Deadline evidence is only authoritative before an I/O attempt."""
import io
import unittest
import urllib.error
from riri.errors import TimeBudgetExceeded, RemoteError, UncertainPost
from riri.http import Transport
from riri.slack import SlackClient
from test_api import Opener, who

URL='https://slack.com/api/chat.postMessage'

class DeadlineBoundaryTests(unittest.TestCase):
    def test_expired_budget_before_open_is_known_unsent(self):
        opener=Opener()
        with self.assertRaises(TimeBudgetExceeded) as caught:
            Transport(deadline=10,clock=lambda:10,opener=opener).request('POST',URL,payload={})
        self.assertTrue(caught.exception.not_sent);self.assertEqual(opener.calls,[])

    def test_pre_request_guard_deadline_propagates_through_slack(self):
        attempts=[]
        def guard():
            attempts.append(True)
            if len(attempts)==2:raise TimeBudgetExceeded('before write')
        opener=Opener(who());client=SlackClient('xoxb-test-only','B123','U123',Transport(opener=opener,before_request=guard))
        client.auth_test()
        with self.assertRaises(TimeBudgetExceeded) as caught:client.post('C123','hello')
        self.assertTrue(caught.exception.not_sent);self.assertEqual(len(opener.calls),1)

    def test_throttled_notion_write_deadline_is_known_unsent(self):
        opener=Opener();transport=Transport(deadline=.1,clock=lambda:0,opener=opener)
        transport._notion_at=0
        with self.assertRaises(TimeBudgetExceeded) as caught:
            transport.request('PATCH','https://api.notion.com/v1/blocks/a',payload={})
        self.assertTrue(caught.exception.not_sent);self.assertEqual(opener.calls,[])

    def test_rate_limit_retry_wait_deadline_is_known_unsent(self):
        opener=Opener(urllib.error.HTTPError(URL,429,'',{'Retry-After':'10'},None))
        with self.assertRaises(TimeBudgetExceeded) as caught:
            Transport(deadline=2,clock=lambda:0,opener=opener).request('POST',URL,payload={})
        self.assertTrue(caught.exception.not_sent);self.assertEqual(len(opener.calls),1)

    def test_during_open_deadline_is_uncertain_not_retry_permission(self):
        opener=Opener(who(),TimeBudgetExceeded('I/O started'))
        client=SlackClient('xoxb-test-only','B123','U123',Transport(opener=opener));client.auth_test()
        with self.assertRaises(UncertainPost) as caught:client.post('C123','hello')
        self.assertTrue(caught.exception.uncertain);self.assertFalse(getattr(caught.exception,'not_sent',False))

    def test_response_read_deadline_remains_uncertain(self):
        class Response(io.BytesIO):
            def read(self,*args):raise TimeBudgetExceeded('during read')
        class ReadOpener:
            def open(self,*args,**kwargs):return Response()
        with self.assertRaises(RemoteError) as caught:
            Transport(opener=ReadOpener()).request('POST',URL,payload={})
        self.assertTrue(caught.exception.uncertain);self.assertFalse(getattr(caught.exception,'not_sent',False))

    def test_explicit_uncertainty_survives_pre_send_tag(self):
        error=TimeBudgetExceeded('previous uncertainty');error.uncertain=True
        def guard():raise error
        with self.assertRaises(TimeBudgetExceeded) as caught:
            Transport(opener=Opener(),before_request=guard).request('POST',URL,payload={})
        self.assertIs(caught.exception,error);self.assertTrue(error.uncertain);self.assertTrue(error.not_sent)
