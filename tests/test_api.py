import io
import json
import unittest
import urllib.error
from riri.errors import AccountMismatch, RemoteError, SafetyError, UncertainPost, TimeBudgetExceeded, NotionConflict, sanitize
from riri.http import Transport, NoRedirect
from riri.slack import SlackClient
from riri.notion import NotionClient, LOG_COLUMNS, rich_text

class Fake:
    def __init__(self, *results): self.results, self.calls = list(results), []
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        value = self.results.pop(0)
        if isinstance(value, Exception): raise value
        return value

def rt(text): return [{'type': 'text', 'text': {'content': text}}]
def page(rows, more=False, cursor=None): return {'results': rows, 'has_more': more, 'next_cursor': cursor}
def row(id, values): return {'id': id, 'type': 'table_row', 'table_row': {'cells': [rt(x) for x in values]}}
def code_response(id, text): return {'object':'block','id':id,'type':'code','in_trash':False,'last_edited_time':'2099-01-01T00:00:00Z','code':{'rich_text':rt(text)}}
def append_response(values): return {'object':'list','has_more':False,'results':[dict(row('new-'+str(i),v),object='block',in_trash=False) for i,v in enumerate(values)]}
def who(): return {'ok': True, 'bot_id': 'B123', 'user_id': 'U123'}

class SlackTests(unittest.TestCase):
    def client(self, *results):
        fake = Fake(who(), *results)
        client = SlackClient('xoxb-test-only', 'B123', 'U123', fake)
        client.auth_test()
        return client, fake
    def test_account_guards(self):
        for token, bot, user, response in [('xoxp-no','B123','U123',who()), ('xoxb-no','','U123',who()),
                ('xoxb-no','B123','U0C5V11S37X',who()), ('xoxb-no','B123','U123',{'ok':True,'user_id':'U123'}),
                ('xoxb-no','B123','U123',{'ok':True,'bot_id':'B999','user_id':'U123'}),
                ('xoxb-no','B123','U123',{'ok':True,'bot_id':'B123','user_id':'U999'})]:
            c = SlackClient(token,bot,user,Fake(response))
            with self.assertRaises(AccountMismatch): c.auth_test()
            with self.assertRaises(AccountMismatch): c.post('C123','hello')
    def test_mutated_config_invalidates_verification(self):
        c, f = self.client(); c.token = 'xoxb-new'
        with self.assertRaises(AccountMismatch): c.post('C123','hello')
        self.assertEqual(len(f.calls), 1)
    def test_post_safe_minimal_payload(self):
        c,f=self.client({'ok':True,'ts':'123.456'})
        self.assertEqual(c.post('C123','@あいす\n依頼ID：💛20261110-1'), '123.456')
        payload=f.calls[-1][2]['payload']
        self.assertFalse(payload['link_names']); self.assertNotIn('username',payload)
    def test_unsafe_text(self):
        for text in ('<@U123>', '<!subteam^S123>', '@everyone', 'ntn_token', 'xoxb-abcd'):
            c,f=self.client()
            with self.assertRaises(SafetyError): c.post('C123',text)
            self.assertEqual(len(f.calls),1)
    def test_uncertain_no_retry(self):
        c,f=self.client(RemoteError('failed',uncertain=True))
        with self.assertRaises(UncertainPost): c.post('C123','hello')
        self.assertEqual(len(f.calls),2)
    def test_malformed_or_ambiguous_success_is_uncertain(self):
        for response in ({}, {'ok':True}, {'ok':False,'error':'internal_error'}, {'ok':False,'error':'future_unknown_error'}):
            c,f=self.client(response)
            with self.assertRaises(UncertainPost): c.post('C123','hello')
            self.assertEqual(len(f.calls),2)
    def test_explicit_rejection_is_not_uncertain(self):
        for code in ('not_in_channel', 'invalid_auth', 'missing_scope', 'channel_not_found', 'msg_too_long'):
            c,f=self.client({'ok':False,'error':code})
            with self.assertRaises(RemoteError) as e: c.post('C123','hello')
            self.assertFalse(e.exception.uncertain)
            self.assertEqual(len(f.calls),2)
    def test_paginate_and_exact_identity_marker(self):
        msg={'bot_id':'B123','user':'U123','text':'依頼ID：abc-extra','ts':'1.1'}
        c,f=self.client({'ok':True,'messages':[msg], 'has_more':True,'response_metadata':{'next_cursor':'next'}},
            {'ok':True,'messages':[dict(msg,text='依頼ID：abc', ts='2.2')]})
        self.assertEqual(c.find_post('C123','依頼ID：abc',0),'2.2')
        self.assertIn('cursor=next', f.calls[-1][1])
    def test_partial_history_never_resolves_match(self):
        c,f=self.client({'ok':True,'messages':[{'bot_id':'B123','user':'U123','text':'id','ts':'1.2'}], 'has_more':True})
        with self.assertRaises(RemoteError): c.find_post('C123','id',0)
    def test_other_bot_does_not_match(self):
        c,f=self.client({'ok':True,'messages':[{'bot_id':'B999','user':'U123','text':'id','ts':'1.2'}]})
        self.assertIsNone(c.find_post('C123','id',0))

class NotionTests(unittest.TestCase):
    def test_paginated_trashed_filtered(self):
        f=Fake(page([{'id':'one'}],True,'cursor'),page([{'id':'two'},{'id':'trash','in_trash':True}]))
        self.assertEqual([x['id'] for x in NotionClient('ntn_fake',f).read_blocks('page')], ['one','two'])
    def test_missing_cursor(self):
        with self.assertRaises(RemoteError): NotionClient('ntn_fake',Fake(page([],True))).read_blocks('p')
    def test_status_preserves_all_other_text_and_styles(self):
        original='【りりへの依頼】\r\n状態：未着手\r\n本文： unchanged\n'
        parts=rt('【りりへの依頼】\r\n状態：未')+rt('着手\r\n本文： unchanged\n')
        parts[0]['annotations']={'bold':True}
        f=Fake(dict(code_response('block',original),code={'rich_text':parts}), code_response('block',original.replace('未着手','対応中')), code_response('block',original.replace('未着手','対応中')))
        NotionClient('ntn_fake',f).set_status('block',original,'対応中')
        updated=f.calls[-2][2]['payload']['code']['rich_text']
        self.assertEqual(rich_text(updated),original.replace('未着手','対応中'))
        self.assertTrue(updated[0]['annotations']['bold'])
    def test_stale_status_refuses(self):
        f=Fake(code_response('b','状態：対応中'))
        with self.assertRaises(RemoteError): NotionClient('ntn_fake',f).set_status('b','状態：未着手','対応中')
        self.assertEqual(len(f.calls),1)
    def test_status_2000_chunk_limit(self):
        original='状態：未着手\n'+'x'*4000
        f=Fake(code_response('b',original),code_response('b',original.replace('未着手','対応中')),code_response('b',original.replace('未着手','対応中')))
        NotionClient('ntn_fake',f).set_status('b',original,'対応中')
        self.assertTrue(all(len(x['text']['content'])<=2000 for x in f.calls[-2][2]['payload']['code']['rich_text']))
    def test_table_and_position(self):
        heading={'type':'heading_1','heading_1':{'rich_text':rt('ログ一覧（新しい順）')}}
        table={'id':'table','type':'table','table':{'table_width':9,'has_column_header':True}}
        header=row('head',LOG_COLUMNS)
        f=Fake(page([heading,table]),page([header]),page([header]),append_response([['x']*9]))
        c=NotionClient('ntn_fake',f)
        self.assertEqual(c.find_log_table('page')[:2],('table','head'))
        c.append_rows('table','head',[['x']*9])
        payload=f.calls[-1][2]['payload']
        self.assertEqual(payload['position'],{'type':'after_block','after_block':{'id':'head'}})
        self.assertNotIn('after',payload)
    def test_table_wrong_columns(self):
        heading={'type':'heading_1','heading_1':{'rich_text':rt('ログ一覧（新しい順）')}}
        table={'id':'t','type':'table','table':{'table_width':9,'has_column_header':True}}
        c=NotionClient('ntn_fake',Fake(page([heading,table]),page([row('h',['wrong']*9)])))
        with self.assertRaises(SafetyError): c.find_log_table('page')
    def test_status_ambiguous_success_is_uncertain(self):
        for response in ({}, {'object':'block'}, code_response('wrong','状態：対応中'),
                         code_response('b','状態：未着手'), dict(code_response('b','状態：対応中'),in_trash=True),
                         dict(code_response('b','状態：対応中'),code={'rich_text':None})):
            f=Fake(code_response('b','状態：未着手'),response,code_response('b','状態：対応中'))
            with self.assertRaises(RemoteError) as e:
                NotionClient('ntn_fake',f).set_status('b','状態：未着手','対応中')
            self.assertTrue(e.exception.uncertain)
            self.assertEqual(len(f.calls),3)
    def test_append_ambiguous_success_is_uncertain(self):
        responses=[{}, {'object':'list'}, append_response([]), append_response([['wrong']*9]),
                   append_response([['x']*9,['x']*9]), {'object':'list','has_more':False,'results':[None]}]
        for response in responses:
            f=Fake(page([row('h',LOG_COLUMNS)]),response)
            c=NotionClient('ntn_fake',f); c._tables['t']='h'
            with self.assertRaises(RemoteError) as e: c.append_rows('t','h',[['x']*9])
            self.assertTrue(e.exception.uncertain)
            self.assertEqual(len(f.calls),2)
    def test_status_accepts_normalized_uuid_identity(self):
        id='123456781234123412341234567890ab'
        hyphenated='12345678-1234-1234-1234-1234567890ab'
        f=Fake(code_response(hyphenated,'状態：未着手'),code_response(hyphenated,'状態：対応中'),code_response(hyphenated,'状態：対応中'))
        NotionClient('ntn_fake',f).set_status(id,'状態：未着手','対応中')
    def test_unverified_table_rejected(self):
        with self.assertRaises(SafetyError): NotionClient('ntn_fake',Fake()).append_rows('t','h',[['x']*9])

class Opener:
    def __init__(self,*items): self.items=list(items); self.calls=[]
    def open(self,request,timeout):
        self.calls.append((request,timeout)); item=self.items.pop(0)
        if isinstance(item,Exception): raise item
        return io.BytesIO(json.dumps(item).encode())

class HTTPTests(unittest.TestCase):
    def test_redirect_rejected(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://bad.example'))
        opener=Opener(urllib.error.HTTPError('url',302,'',{},None))
        with self.assertRaises(RemoteError): Transport(opener=opener).request('GET','https://slack.com/api/auth.test')
        self.assertEqual(len(opener.calls),1)
    def test_origin_guard(self):
        for url in ('https://slack.com.evil/api', 'https://slack.com@evil/api', 'http://slack.com/api'):
            with self.assertRaises(SafetyError): Transport(opener=Opener()).request('GET',url)
    def test_uncertain_write_no_retry(self):
        for failure in (TimeoutError('secret-no'),urllib.error.HTTPError('url',503,'',{},None)):
            o=Opener(failure)
            with self.assertRaises(RemoteError) as e: Transport(opener=o).request('PATCH','https://api.notion.com/v1/blocks/t/children',payload={})
            self.assertTrue(e.exception.uncertain); self.assertEqual(len(o.calls),1)
            self.assertNotIn('secret-no',str(e.exception))
    def test_bounded_rate_retry(self):
        error=lambda:urllib.error.HTTPError('url',429,'',{'Retry-After':'2'},None)
        o=Opener(error(),error(),{'ok':True}); sleeps=[]
        self.assertTrue(Transport(opener=o,sleep=sleeps.append).request('GET','https://slack.com/api/auth.test')['ok'])
        self.assertEqual(sleeps,[2,2]); self.assertEqual(len(o.calls),3)
    def test_notion_529_retries_capped(self):
        errors=[urllib.error.HTTPError('url',529,'',{'Retry-After':'0'},None) for _ in range(3)]
        o=Opener(*errors)
        with self.assertRaises(RemoteError) as e:
            Transport(opener=o,sleep=lambda n:None).request('PATCH','https://api.notion.com/v1/blocks/a/children',payload={})
        self.assertFalse(e.exception.uncertain)
        self.assertEqual(len(o.calls),3)
    def test_notion_pacing(self):
        now=[0.0]
        def sleep(n): now[0]+=n
        t=Transport(opener=Opener({},{}),clock=lambda:now[0],sleep=sleep)
        t.request('GET','https://api.notion.com/v1/blocks/a')
        t.request('GET','https://api.notion.com/v1/blocks/a')
        self.assertAlmostEqual(now[0],.35)
    def test_deadline(self):
        with self.assertRaises(TimeBudgetExceeded): Transport(deadline=1,clock=lambda:2,opener=Opener()).request('GET','https://slack.com/api/auth.test')
    def test_timeout_clamped_to_remaining_budget(self):
        o=Opener({})
        Transport(deadline=15,clock=lambda:10,opener=o).request('GET','https://slack.com/api/auth.test',timeout=20)
        self.assertEqual(o.calls[0][1],5)
    def test_retry_after_cannot_exceed_deadline(self):
        o=Opener(urllib.error.HTTPError('url',429,'',{'Retry-After':'10'},None))
        sleeps=[]
        with self.assertRaises(TimeBudgetExceeded):
            Transport(deadline=10,clock=lambda:1,opener=o,sleep=sleeps.append).request('GET','https://slack.com/api/auth.test')
        self.assertEqual(len(o.calls),1)
        self.assertEqual(sleeps,[])
    def test_retry_uses_remaining_budget(self):
        now=[1.0]
        def sleep(n): now[0]+=n
        o=Opener(urllib.error.HTTPError('url',429,'',{'Retry-After':'3'},None),{})
        Transport(deadline=10,clock=lambda:now[0],sleep=sleep,opener=o).request('GET','https://slack.com/api/auth.test')
        self.assertEqual([timeout for _,timeout in o.calls],[9,6])
    def test_notion_pacing_cannot_exceed_deadline(self):
        now=[0.0]
        o=Opener({})
        t=Transport(deadline=.2,clock=lambda:now[0],sleep=lambda n:self.fail('must not sleep'),opener=o)
        t.request('GET','https://api.notion.com/v1/blocks/a')
        with self.assertRaises(TimeBudgetExceeded): t.request('GET','https://api.notion.com/v1/blocks/a')
        self.assertEqual(len(o.calls),1)
    def test_late_read_result_rejected_and_write_uncertain(self):
        for method in ('GET','PATCH'):
            now=[0.0]
            class SlowRead(io.BytesIO):
                def read(self,*args):
                    now[0]=11.0
                    return super().read(*args)
            class SlowOpener:
                def open(self,request,timeout): return SlowRead(b'{}')
            t=Transport(deadline=10,clock=lambda:now[0],opener=SlowOpener())
            with self.assertRaises(RemoteError if method=='PATCH' else TimeBudgetExceeded) as e:
                t.request(method,'https://api.notion.com/v1/blocks/a',payload={} if method=='PATCH' else None)
            if method=='PATCH': self.assertTrue(e.exception.uncertain)
    def test_deadline_interrupt_during_write_is_uncertain(self):
        o=Opener(TimeBudgetExceeded('hard deadline'))
        with self.assertRaises(RemoteError) as e:
            Transport(opener=o).request('POST','https://slack.com/api/chat.postMessage',payload={})
        self.assertTrue(e.exception.uncertain)
        self.assertEqual(len(o.calls),1)
    def test_stop_guard_between_notion_read_and_patch(self):
        calls=[0]
        def guard():
            calls[0]+=1
            if calls[0]>1: raise SafetyError('STOP')
        o=Opener(code_response('b','状態：未着手'))
        t=Transport(opener=o,before_request=guard,sleep=lambda n:None)
        with self.assertRaises(NotionConflict):
            NotionClient('ntn_fake',t).set_status('b','状態：未着手','対応中')
        self.assertEqual(len(o.calls),1)
        self.assertEqual(calls[0],3)
    def test_stop_guard_after_retry_wait(self):
        stop=[False]
        def guard():
            if stop[0]: raise SafetyError('STOP')
        def sleep(n): stop[0]=True
        o=Opener(urllib.error.HTTPError('url',429,'',{'Retry-After':'1'},None))
        with self.assertRaises(SafetyError):
            Transport(opener=o,before_request=guard,sleep=sleep).request('POST','https://slack.com/api/chat.postMessage',payload={})
        self.assertEqual(len(o.calls),1)
    def test_stop_guard_before_initial_attempt(self):
        def guard(): raise SafetyError('STOP')
        o=Opener()
        with self.assertRaises(SafetyError):
            Transport(opener=o,before_request=guard).request('GET','https://slack.com/api/auth.test')
        self.assertEqual(o.calls,[])
    def test_redaction(self):
        self.assertEqual(sanitize('ntn_fake xoxb-fake secret_fake custom',('custom',)), '[REDACTED] [REDACTED] [REDACTED] [REDACTED]')

if __name__=='__main__': unittest.main()
