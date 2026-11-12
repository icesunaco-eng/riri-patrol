"""Synthetic owner-authorized transport tests. No real IDs or credentials."""
import unittest
from pathlib import Path
from unittest.mock import patch
from riri.slack import SlackClient, AUTOMATED_REPORT_LABEL, UserTokenUnavailable
from riri.errors import AccountMismatch, SafetyError, UncertainPost, RemoteError
from riri.config import Config
from test_api import Fake

BOT = {'ok':True, 'bot_id':'BTEST', 'user_id':'UBOT', 'team_id':'TTEST'}
USER = {'ok':True, 'user_id':'UOWNER', 'team_id':'TTEST'}
MARK = 'rp:' + 'a'*24
TEXT = '<@UICE>\nsynthetic report\n' + MARK

class UserReportsTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch('riri.slack.REPORT_OWNER_ID','UOWNER'),
                        patch('riri.slack.USER_REPORT_CHANNEL','CREPORT')]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def client(self, *results, token=('xoxp' + '-synthetic-only'), user=USER):
        fake = Fake(BOT, *([user] if token else []), *results)
        client = SlackClient(('xoxb' + '-synthetic-only'),'BTEST','UBOT',fake,
            report_channel='CREPORT',ice_mention_user_id='UICE',report_user_token=token)
        client.auth_test()
        return client,fake

    def test_only_report_posts_use_user_token_and_provenance(self):
        c,f=self.client({'ok':True,'ts':'1.1'}, {'ok':True,'ts':'2.2'})
        c.post('CREPORT',TEXT,kind='reports'); c.post('CINSTRUCTIONS','synthetic instruction')
        self.assertEqual(f.calls[-2][2]['headers']['Authorization'],('Bearer ' + 'xoxp' + '-synthetic-only'))
        self.assertEqual(f.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-synthetic-only'))
        text=f.calls[-2][2]['payload']['text']
        self.assertEqual(text.splitlines()[0],'<@UICE>')
        self.assertIn(AUTOMATED_REPORT_LABEL,text)
        self.assertEqual(text.splitlines()[-1], MARK)

    def test_empty_variable_keeps_bot_behavior(self):
        c,f=self.client({'ok':True,'ts':'1.1'},token='')
        c.post('CREPORT',TEXT,kind='reports')
        self.assertEqual(len(f.calls),2)
        self.assertEqual(f.calls[-1][2]['payload']['text'],TEXT)
        self.assertEqual(f.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-synthetic-only'))

    def test_wrong_user_or_workspace_blocks_all_posts(self):
        # v105: only user-designated work blocks; verified bot work continues.
        for who in (dict(USER,user_id='UOTHER'),dict(USER,team_id='TOTHER')):
            f=Fake(BOT,who,{'ok':True,'ts':'1.1'})
            c=SlackClient(('xoxb' + '-synthetic-only'),'BTEST','UBOT',f,report_channel='CREPORT',
                ice_mention_user_id='UICE',report_user_token=('xoxp' + '-synthetic-only'))
            c.auth_test()
            self.assertTrue(c.verified)
            self.assertFalse(c.report_user_verified)
            with self.assertRaises(UserTokenUnavailable):c.post('CREPORT',TEXT,kind='reports')
            self.assertEqual(len(f.calls),2)
            c.post('CINSTRUCTIONS','hello')
            self.assertEqual(f.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-synthetic-only'))

    def test_forbidden_user_methods_never_reach_transport(self):
        c,f=self.client()
        for method in ('conversations.history','chat.update','chat.delete','chat.getPermalink'):
            with self.assertRaises(SafetyError):
                c._request(method,{'channel':'CREPORT'},use_report_user=True)
        self.assertEqual(len(f.calls),2)

    def test_other_user_destinations_rejected_before_send(self):
        c,f=self.client()
        for channel in ('COTHER','DOTHER','GOTHER'):
            with self.assertRaises(SafetyError):
                c._request('chat.postMessage',{'channel':channel,'text':TEXT},use_report_user=True)
        self.assertEqual(len(f.calls),2)

    def test_mutated_user_token_invalidates_all_posts(self):
        c,f=self.client()
        c.report_user_token=('xoxp' + '-changed')
        with self.assertRaises(AccountMismatch):c.post('CREPORT',TEXT,kind='reports')
        with self.assertRaises(AccountMismatch):c.post('CINSTRUCTIONS','hello')
        self.assertEqual(len(f.calls),2)

    def test_history_always_uses_bot_and_marks_automated_owner_posts(self):
        c,f=self.client({'ok':True,'messages':[{'user':'UOWNER','text':TEXT,'ts':'1.1'}]})
        messages=c.history('CREPORT',0)
        self.assertEqual(f.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-synthetic-only'))
        self.assertEqual(messages[0]['subtype'],'riri_patrol')
        self.assertEqual(messages[0]['text'],TEXT)

    def test_marked_owner_posts_excluded_by_existing_noa_filter(self):
        from types import SimpleNamespace
        from riri.noa import NoaWake
        # Test actual existing eligibility predicate without modifying Noa.
        c,f=self.client({'ok':True,'messages':[{'user':'UOWNER','text':'@のあ\n'+MARK,'ts':'1.1'}]})
        messages=c.history('CREPORT',0)
        instance=object.__new__(NoaWake)
        instance.p=SimpleNamespace(config=SimpleNamespace(expected_user_id='UBOT'))
        self.assertFalse(instance._eligible(messages[0]))

    def test_unknown_send_is_not_retried(self):
        c,f=self.client(RemoteError('synthetic',uncertain=True))
        with self.assertRaises(UncertainPost):c.post('CREPORT',TEXT,kind='reports')
        self.assertEqual(len(f.calls),3)

    def test_restart_without_token_can_find_old_owner_post(self):
        c,f=self.client({'ok':True,'messages':[{'user':'UOWNER','text':TEXT,'ts':'1.1'}]},token='')
        self.assertEqual(c.find_post('CREPORT',MARK,0),'1.1')
        self.assertEqual(f.calls[-1][2]['headers']['Authorization'],('Bearer ' + 'xoxb' + '-synthetic-only'))

    def test_owner_post_other_channel_or_nonexact_marker_not_a_receipt(self):
        for channel,text in (('COTHER',TEXT),('CREPORT',TEXT+'extra')):
            c,f=self.client({'ok':True,'messages':[{'user':'UOWNER','text':text,'ts':'1.1'}]})
            self.assertIsNone(c.find_post(channel,MARK,0))

    def test_missing_marker_or_mention_blocks_user_post(self):
        for text in ('<@UICE>\nhello', 'hello\n'+MARK):
            c,f=self.client()
            with self.assertRaises(SafetyError):c.post('CREPORT',text,kind='reports')
            self.assertEqual(len(f.calls),2)

    def test_dry_run_verifies_both_but_posts_nothing(self):
        c,f=self.client()
        self.assertIsNone(c.post('CREPORT',TEXT,dry_run=True,kind='reports'))
        self.assertTrue(c.verified)
        self.assertEqual(len(f.calls),2)

    def test_new_secret_in_redaction_and_not_repr(self):
        cfg=Config(root=Path('/tmp'),slack_report_user_token=('xoxp' + '-synthetic-only'))
        self.assertIn(('xoxp' + '-synthetic-only'),cfg.secrets)
        self.assertNotIn(('xoxp' + '-synthetic-only'),repr(cfg))

    def test_mismatching_fixed_report_channel_rejected(self):
        f=Fake(BOT)
        c=SlackClient(('xoxb' + '-synthetic-only'),'BTEST','UBOT',f,report_channel='COTHER',
            ice_mention_user_id='UICE',report_user_token=('xoxp' + '-synthetic-only'))
        c.auth_test()
        self.assertTrue(c.verified)
        self.assertFalse(c.report_user_verified)
        self.assertEqual(len(f.calls),1)
