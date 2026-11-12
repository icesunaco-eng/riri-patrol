"""v1.0.5 transport contract; synthetic offline fixtures only."""
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from test_api import Fake
from riri.slack import SlackClient, UserTokenUnavailable, AUTOMATED_REPORT_LABEL
from riri.errors import SafetyError, UncertainPost

BOT={'ok':True,'user_id':'UBOT','bot_id':'BTEST','team_id':'TTEST','app_id':'ATEST'}
USER={'ok':True,'user_id':'UOWNER','team_id':'TTEST'}
MARK='rp:'+'b'*24
TEXT='<@UICE>\nautomated test\n'+MARK

class Slack105Tests(unittest.TestCase):
    def setUp(self):
        for name,value in [('REPORT_OWNER_ID','UOWNER'),('USER_REPORT_CHANNEL','CREPORT')]:
            p=patch('riri.slack.'+name,value);p.start();self.addCleanup(p.stop)
    def client(self,*responses,token=('xoxp' + '-offline-only'),who=USER):
        t=Fake(BOT,*([who] if token else []),*responses)
        c=SlackClient(('xoxb' + '-offline-only'),'BTEST','UBOT',t,report_channel='CREPORT',ice_mention_user_id='UICE',report_user_token=token)
        c.auth_test();return c,t
    def test_kind_routing(self):
        for kind in ('instruction','instruction_relay','reports','notice','recovery','unsafe_notice'):
            c,t=self.client({'ok':True,'ts':'1.1'})
            c.post('CREPORT',TEXT,kind=kind)
            is_user=kind in ('instruction','instruction_relay','reports')
            self.assertEqual(t.calls[-1][2]['headers']['Authorization'],'Bearer '+('xoxp' if is_user else 'xoxb')+'-offline-only')
            self.assertEqual(AUTOMATED_REPORT_LABEL in t.calls[-1][2]['payload']['text'],is_user)
    def test_empty_token_all_kinds_bot(self):
        for kind in ('instruction','instruction_relay','reports','notice'):
            c,t=self.client({'ok':True,'ts':'1.1'},token='')
            c.post('CREPORT',TEXT,kind=kind)
            self.assertEqual(t.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-offline-only'))
    def test_user_failure_only_blocks_user_work(self):
        for who in ({'ok':False,'error':'token_revoked'},dict(USER,user_id='UOTHER'),dict(USER,team_id='TOTHER')):
            c,t=self.client({'ok':True,'ts':'1.1'},{'ok':True,'ts':'2.1'},who=who)
            self.assertTrue(c.verified);self.assertFalse(c.report_user_verified)
            with self.assertRaises(UserTokenUnavailable) as ctx:c.post('CREPORT',TEXT,kind='reports')
            self.assertTrue(ctx.exception.not_sent);self.assertFalse(ctx.exception.uncertain)
            self.assertEqual(len(t.calls),2)
            c.post('CREPORT',TEXT,kind='notice');c.post('CINSTRUCTION','test',kind='instruction')
            self.assertTrue(all(x[2]['headers']['Authorization']==('Bearer ' + 'xoxb' + '-offline-only') for x in t.calls[2:]))
    def test_owner_app_receipt_with_token_removed(self):
        c,t=self.client({'ok':True,'messages':[{'user':'UOWNER','bot_id':'BAPPLICATION','app_id':'ATEST','text':TEXT,'ts':'1.1'}]},token='')
        self.assertEqual(c.find_post('CREPORT',MARK,0),'1.1')
        self.assertEqual(c.last_found_owner_post,('CREPORT',MARK,'1.1'))
    def test_known_other_app_receipt_rejected(self):
        c,t=self.client({'ok':True,'messages':[{'user':'UOWNER','bot_id':'BAPPLICATION','app_id':'AOTHER','text':TEXT,'ts':'1.1'}]})
        self.assertIsNone(c.find_post('CREPORT',MARK,0))
        self.assertIsNone(c.last_found_owner_post)
    def test_unknown_app_still_owner_exact_marker(self):
        c,t=self.client({'ok':True,'messages':[{'user':'UOWNER','bot_id':'BAPPLICATION','text':TEXT,'ts':'1.1'}]})
        self.assertEqual(c.find_post('CREPORT',MARK,0),'1.1')
        self.assertEqual(c.last_found_owner_post,('CREPORT',MARK,'1.1'))
    def test_preflight_safety_failure_is_definitely_unsent(self):
        c,t=self.client()
        with self.assertRaises(SafetyError) as ctx:c.post('CREPORT','<@UOTHER>\n'+MARK,kind='reports')
        self.assertTrue(ctx.exception.not_sent);self.assertEqual(len(t.calls),2)
    def test_probe_history_one_bot_only(self):
        c,t=self.client({'ok':True,'messages':[],'has_more':True})
        self.assertTrue(c.probe_report_history())
        self.assertEqual(len(t.calls),3)
        call=t.calls[-1]
        self.assertEqual(call[2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-offline-only'))
        self.assertEqual(parse_qs(urlsplit(call[1]).query),{'channel':['CREPORT'],'limit':['1']})
    def test_probe_history_failure_propagates(self):
        from riri.errors import RemoteError
        c,t=self.client({'ok':False,'error':'missing_scope'})
        with self.assertRaises(RemoteError):c.probe_report_history()
    def test_post_after_send_without_timestamp_remains_unknown(self):
        c,t=self.client({'ok':True})
        with self.assertRaises(UncertainPost) as ctx:c.post('CREPORT',TEXT,kind='reports')
        self.assertFalse(getattr(ctx.exception,'not_sent',False))
    def test_revoked_after_auth_blocks_later_user_posts_without_fallback(self):
        c,t=self.client({'ok':False,'error':'token_revoked'},{'ok':True,'ts':'1.1'})
        with self.assertRaises(UserTokenUnavailable) as ctx:c.post('CREPORT',TEXT,kind='reports')
        self.assertTrue(ctx.exception.not_sent);self.assertFalse(c.report_user_verified)
        with self.assertRaises(UserTokenUnavailable):c.post('CREPORT',TEXT,kind='reports')
        self.assertEqual(len(t.calls),3)
        c.post('CREPORT',TEXT,kind='notice')
        self.assertEqual(t.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-offline-only'))
