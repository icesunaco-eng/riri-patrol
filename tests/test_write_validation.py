"""Exercise practice/live writers with REAL validators and only offline transports."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from riri.errors import SafetyError, NotionConflict, RemoteError, TimeBudgetExceeded
from riri.eventlog import LocalLogs
from riri.notion import NotionClient, LOG_COLUMNS
from riri.slack import SlackClient
from riri.writer import PracticeWriter, LiveWriter
from test_api import Fake, code_response, append_response, page, row, who

OLD='状態：未着手\n本文：before & <example>'
NEW=OLD.replace('未着手','対応中')
V1='2099-01-01T00:00:00Z'
V2='2099-01-01T00:00:01Z'
V3='2099-01-01T00:00:02Z'

def version(text, at=V1):
    return dict(code_response('b',text),last_edited_time=at)

def req(text=OLD): return SimpleNamespace(id='id',block_id='b',text=text)

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.mkdtemp(prefix="riri-write-validation-")
        self.logs=LocalLogs(self.temp, secrets=('custom-private-token',), verbose=True)
        self.clock=lambda:datetime(2099,1,1,tzinfo=timezone.utc)
    def writer(self, writer_type, responses=(), slack_responses=()):
        self.transport=Fake(*responses)
        self.slack_transport=Fake(who(),*slack_responses)
        slack=SlackClient('xoxb-test-only','B123','U123',self.slack_transport)
        slack.auth_test()
        return writer_type(slack,NotionClient('ntn_fake',self.transport),self.logs,self.clock)
    def audit(self):
        return [json.loads(x) for x in (Path(self.temp)/'logs/notion-audit.jsonl').read_text().splitlines()]
    def test_practice_invalid_text_and_channel_use_live_validator(self):
        for writer_type in (PracticeWriter,LiveWriter):
            w=self.writer(writer_type)
            for channel,text in [('bad','hello'),('C123','<@U123>'),('C123','ntn_fake')]:
                with self.assertRaises(SafetyError):w.slack_post(channel,text,'key')
            self.assertEqual(len(self.slack_transport.calls),1)
    def test_practice_slack_does_not_send(self):
        w=self.writer(PracticeWriter)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(w.slack_post('C123','hello\nrp:abcdef','key'),'practice-key')
        self.assertEqual(len(self.slack_transport.calls),1)
    def test_live_slack_preserves_returned_timestamp(self):
        w=self.writer(LiveWriter,slack_responses=({'ok':True,'ts':'123.456'},))
        self.assertEqual(w.slack_post('C123','hello\nrp:abcdef','key'),'123.456')
        self.assertEqual(len(self.slack_transport.calls),2)
    def test_practice_status_rereads_validates_and_audits_without_patch(self):
        w=self.writer(PracticeWriter,[version(OLD)])
        with contextlib.redirect_stdout(io.StringIO()): w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET'])
        entries=self.audit()
        self.assertEqual([x['phase'] for x in entries],['before','practice_validated'])
        self.assertEqual(entries[0]['before_text'],OLD)
        self.assertEqual(entries[0]['before_last_edited_time'],V1)
    def test_practice_stale_text_is_conflict_before_write(self):
        w=self.writer(PracticeWriter,[version(OLD+'changed')])
        with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET'])
    def test_observed_edit_version_conflict_before_write(self):
        w=self.writer(PracticeWriter,[page([version(OLD)]),version(OLD,V2)])
        w.notion.read_blocks('page')
        with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET','GET'])
    def test_live_status_reads_after_patch_and_audits_both_bodies(self):
        w=self.writer(LiveWriter,[version(OLD),version(NEW,V2),version(NEW,V2)])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH','GET'])
        self.assertEqual(output.getvalue(),'')
        entries=self.audit()
        self.assertEqual(entries[-1]['before_text'],OLD)
        self.assertEqual(entries[-1]['after_text'],NEW)
        self.assertEqual(entries[-1]['after_last_edited_time'],V2)
        self.assertTrue(entries[-1]['confirmed'])
    def test_post_write_body_conflict_held_without_repair(self):
        w=self.writer(LiveWriter,[version(OLD),version(NEW,V2),version(NEW+' concurrent edit',V3)])
        with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH','GET'])
        self.assertIn('concurrent edit',self.audit()[-1]['after_text'])
    def test_post_write_version_conflict_even_when_body_equal(self):
        w=self.writer(LiveWriter,[version(OLD),version(NEW,V2),version(NEW,V3)])
        with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
        self.assertEqual(len(self.transport.calls),3)
    def test_missing_prewrite_version_stops_before_patch(self):
        block=version(OLD); del block['last_edited_time']
        w=self.writer(PracticeWriter,[block])
        with self.assertRaises(SafetyError):w.set_status(req(),'対応中')
        self.assertEqual(len(self.transport.calls),1)
    def test_post_read_failure_holds_and_records_unavailable(self):
        w=self.writer(LiveWriter,[version(OLD),version(NEW,V2),OSError('private details')])
        with self.assertRaises(NotionConflict) as e:w.set_status(req(),'対応中')
        self.assertNotIn('private details',str(e.exception))
        self.assertEqual(self.audit()[-1]['phase'],'after_unavailable')
    def test_uncertain_applied_patch_always_holds_even_matching_after_get(self):
        for error in (RemoteError('timeout',uncertain=True), TimeoutError('private timeout'), RuntimeError('unknown private detail')):
            w=self.writer(LiveWriter,[version(OLD),error,version(NEW,V2)])
            with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
            self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH','GET'])
            entry=self.audit()[-1]
            self.assertTrue(entry['patch_uncertain'])
            self.assertFalse(entry['confirmed'])
            self.assertEqual(entry['after_text'],NEW)
    def test_uncertain_patch_records_changed_after_body_without_repair(self):
        w=self.writer(LiveWriter,[version(OLD),RemoteError('timeout',uncertain=True),version(NEW+' changed',V3)])
        with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
        self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH','GET'])
        self.assertEqual(self.audit()[-1]['after_text'],NEW+' changed')
        self.assertEqual(self.audit()[-1]['before_text'],OLD)
    def test_uncertain_patch_with_stop_or_deadline_records_after_unavailable(self):
        for error in (SafetyError('STOP'),TimeBudgetExceeded('deadline')):
            w=self.writer(LiveWriter,[version(OLD),RemoteError('timeout',uncertain=True),error])
            with self.assertRaises(NotionConflict):w.set_status(req(),'対応中')
            entry=self.audit()[-1]
            self.assertEqual(entry['phase'],'after_unavailable')
            self.assertTrue(entry['patch_uncertain'])
            self.assertEqual(entry['before_text'],OLD)
    def test_definite_patch_rejection_is_not_conflict_or_followed_by_read(self):
        rejection=RemoteError('HTTP request failed (403)',uncertain=False)
        w=self.writer(LiveWriter,[version(OLD),rejection])
        with self.assertRaises(RemoteError) as e:w.set_status(req(),'対応中')
        self.assertIs(e.exception,rejection)
        self.assertNotIsInstance(e.exception,NotionConflict)
        self.assertFalse(e.exception.uncertain)
        self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH'])
    def test_practice_append_revalidates_header_and_never_patches(self):
        w=self.writer(PracticeWriter,[page([row('h',LOG_COLUMNS)])])
        w.notion._tables['t']='h'
        with contextlib.redirect_stdout(io.StringIO()):w.append_log('page','t','h',[],[['value']*9])
        self.assertEqual([x[0] for x in self.transport.calls],['GET'])
    def test_practice_append_invalid_header_is_rejected(self):
        w=self.writer(PracticeWriter,[page([row('h',['changed']*9)])])
        w.notion._tables['t']='h'
        with self.assertRaises(SafetyError):w.append_log('page','t','h',[],[['value']*9])
        self.assertEqual([x[0] for x in self.transport.calls],['GET'])
    def test_live_append_same_validator_then_patch(self):
        w=self.writer(LiveWriter,[page([row('h',LOG_COLUMNS)]),append_response([['value']*9])])
        w.notion._tables['t']='h'
        w.append_log('page','t','h',[],[['value']*9])
        self.assertEqual([x[0] for x in self.transport.calls],['GET','PATCH'])
    def test_private_audit_redacts_and_has_owner_only_permissions(self):
        self.logs.audit({'before_text':'custom-private-token ntn_dummy xoxb-dummy','after_text':'safe'})
        path=Path(self.temp)/'logs/notion-audit.jsonl'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        raw=path.read_text()
        self.assertNotIn('custom-private-token',raw)
        self.assertNotIn('ntn_dummy',raw)
        self.assertNotIn('xoxb-dummy',raw)
        self.assertEqual(self.audit()[0]['after_text'],'safe')
    def test_history_unescapes_before_exact_marker_match(self):
        for marker, text in [('rp:abcdef','hello &amp; &lt; &gt;\nrp:abcdef'),
                             ('rp:a&b<c>','rp:a&amp;b&lt;c&gt;')]:
            f=Fake(who(),{'ok':True,'messages':[{'bot_id':'B123','user':'U123','ts':'1.2','text':text}]})
            c=SlackClient('xoxb-test-only','B123','U123',f);c.auth_test()
            self.assertEqual(c.find_post('C123',marker,0),'1.2')
    def test_history_does_not_match_prefix_or_substring(self):
        for text in ('prefix rp:abcdef','rp:abcdef suffix','rp:abcdefgh'):
            f=Fake(who(),{'ok':True,'messages':[{'bot_id':'B123','user':'U123','ts':'1.2','text':text}]})
            c=SlackClient('xoxb-test-only','B123','U123',f);c.auth_test()
            self.assertIsNone(c.find_post('C123','rp:abcdef',0))
