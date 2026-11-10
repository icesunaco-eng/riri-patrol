"""P-03: only pre-I/O STOP is evidence that a Slack send did not happen."""
import io
import unittest
import urllib.error
from riri.errors import StopRequested, UncertainPost, RemoteError
from riri.http import Transport
from riri.slack import SlackClient
from test_api import Opener, who


class SlackStopBoundaryTests(unittest.TestCase):
    def test_before_write_stop_is_same_exception_and_opener_is_never_called(self):
        error=StopRequested('STOP')
        def stop(): raise error
        opener=Opener()
        transport=Transport(opener=opener,before_request=stop)
        with self.assertRaises(StopRequested) as caught:
            transport.request('POST','https://slack.com/api/chat.postMessage',payload={'text':'hello'})
        self.assertIs(caught.exception,error)
        self.assertIs(error.not_sent,True)
        self.assertEqual(opener.calls,[])

    def test_auth_get_then_stop_before_post_never_queries_history(self):
        error=StopRequested('STOP')
        attempts=[]
        def guard():
            attempts.append(True)
            if len(attempts)==2: raise error
        opener=Opener(who())
        transport=Transport(opener=opener,before_request=guard)
        client=SlackClient('xoxb-test-only','B123','U123',transport)
        client.auth_test()
        with self.assertRaises(StopRequested) as caught:client.post('C123','hello\nrp:abc123')
        self.assertIs(caught.exception,error)
        self.assertIs(error.not_sent,True)
        self.assertEqual(len(attempts),2)
        self.assertEqual([r.full_url for r,t in opener.calls],['https://slack.com/api/auth.test'])

    def test_stop_once_open_started_has_no_known_not_sent_flag(self):
        error=StopRequested('STOP during I/O')
        opener=Opener(who(),error)
        client=SlackClient('xoxb-test-only','B123','U123',Transport(opener=opener))
        client.auth_test()
        with self.assertRaises(StopRequested) as caught:client.post('C123','hello')
        self.assertIs(caught.exception,error)
        self.assertFalse(getattr(error,'not_sent',False))
        self.assertEqual(len(opener.calls),2)
        self.assertTrue(opener.calls[-1][0].full_url.endswith('/chat.postMessage'))

    def test_stop_during_response_read_has_no_known_not_sent_flag(self):
        error=StopRequested('STOP during read')
        class ReadStop(io.BytesIO):
            def read(self,*args):raise error
        class ReadOpener:
            def __init__(self):self.calls=[]
            def open(self,request,timeout):
                self.calls.append(request.full_url)
                return ReadStop()
        opener=ReadOpener()
        transport=Transport(opener=opener)
        with self.assertRaises(StopRequested) as caught:
            transport.request('POST','https://slack.com/api/chat.postMessage',payload={})
        self.assertIs(caught.exception,error)
        self.assertFalse(getattr(error,'not_sent',False))
        self.assertEqual(len(opener.calls),1)

    def test_uncertain_stop_attribute_is_preserved(self):
        error=StopRequested('STOP with prior uncertainty')
        error.uncertain=True
        def stop():raise error
        opener=Opener()
        with self.assertRaises(StopRequested) as caught:
            Transport(opener=opener,before_request=stop).request('POST','https://slack.com/api/chat.postMessage',payload={})
        self.assertIs(caught.exception,error)
        self.assertTrue(error.uncertain)
        self.assertTrue(error.not_sent)
        # Caller must let explicit uncertainty take precedence over not_sent.
        self.assertEqual(opener.calls,[])

    def test_stop_before_rate_limited_retry_is_known_not_sent(self):
        error=StopRequested('STOP before retry')
        attempts=[]
        def guard():
            attempts.append(True)
            if len(attempts)==2:raise error
        opener=Opener(urllib.error.HTTPError('url',429,'',{'Retry-After':'0'},None))
        with self.assertRaises(StopRequested) as caught:
            Transport(opener=opener,before_request=guard).request('POST','https://slack.com/api/chat.postMessage',payload={})
        self.assertIs(caught.exception,error)
        self.assertTrue(error.not_sent)
        self.assertEqual(len(opener.calls),1)

    def test_network_timeout_still_has_uncertain_post_classification(self):
        opener=Opener(who(),TimeoutError('synthetic timeout'))
        client=SlackClient('xoxb-test-only','B123','U123',Transport(opener=opener))
        client.auth_test()
        with self.assertRaises(UncertainPost):client.post('C123','hello')
        self.assertEqual(len(opener.calls),2)


if __name__=='__main__':unittest.main()
