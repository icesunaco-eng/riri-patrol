"""Offline R/Q review regressions. Reuse fixtures without rediscovering base tests."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from riri.errors import RemoteError, StopRequested, TimeBudgetExceeded
from riri.eventlog import new_event, slack_evidence
from riri.resolve import held_items
from riri.state import StateStore
from riri.status import practice_cleanup_items
from test_patrol_flow import FlowTests, request, report


class ReviewV05Flows(unittest.TestCase):
    setUp = FlowTests.setUp
    tearDown = FlowTests.tearDown
    run_once = FlowTests.run_once
    state = FlowTests.state
    add_request = FlowTests.add_request
    add_report = FlowTests.add_report

    def test_practice_posted_and_held_are_listed_before_done(self):
        self.add_request(); self.run_once()
        entry = practice_cleanup_items(self.state())[0]
        self.assertEqual(entry['practice_status'], '指示済み')
        self.add_request(number=2, task='@channel'); self.run_once()
        self.assertEqual({x['practice_status'] for x in practice_cleanup_items(self.state())}, {'指示済み','保留'})

    def assert_reappearance_notified_once(self, temporary_block):
        self.add_request(task='@channel'); self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page] = temporary_block
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'source_removed')
        self.notion.blocks[self.cfg.requests_page] = [request(task='安全な依頼')]
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('取りさげた依頼IDがもう一度' in text for _,text,_ in self.slack.posts), 1)
        self.assertFalse([x for x in self.slack.posts if x[0] == self.cfg.instruction_channel])
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'source_removed')
        self.add_request(number=2); self.run_once(live=True)
        self.assertEqual(sum(ch == self.cfg.instruction_channel for ch,_,_ in self.slack.posts), 1)

    def test_removed_then_readded_id_gets_fixed_notice(self):
        self.assert_reappearance_notified_once([])

    def test_temporarily_unparsable_then_fixed_id_gets_fixed_notice(self):
        source = request(task='@channel')
        source['code']['rich_text'][0]['text']['content'] = source['code']['rich_text'][0]['text']['content'].replace('依頼ID：💛20990101-1', '依頼ID：')
        self.assert_reappearance_notified_once([source])

    def test_quarantined_report_status_has_request_and_author(self):
        self.run_once(live=True); self.add_report(name='あいす')
        page = self.cfg.pages['あいす'].split('/')[-1]
        self.notion.blocks[page][0]['paragraph']['rich_text'][0]['text']['content'] += ' @channel'
        self.run_once(live=True)
        entry = next(x for x in held_items(self.state(True)) if x['type']=='report')
        self.assertIn('💛20990101-1', entry['request']); self.assertIn('あいす', entry['request'])
        self.assertNotIn('@channel', json.dumps(entry,ensure_ascii=False))

    def test_unsafe_nonreport_notice_includes_its_request_id(self):
        self.run_once(live=True)
        state=self.state(True)
        state['outbox']['bad']={'status':'queued','kind':'self','channel':self.cfg.report_channel,
                                'text':'@channel','request_id':'💛20990101-2'}
        StateStore(self.root,'live').save(state)
        self.run_once(live=True)
        notice=next(text for _,text,_ in self.slack.posts if '安全チェックに通らない投稿文' in text)
        self.assertIn('依頼ID：💛20990101-2',notice);self.assertNotIn('@channel',notice)

    def test_log_progress_action_and_evidence_are_distinct(self):
        self.add_request(); self.run_once(live=True); self.add_report(); self.run_once(live=True)
        rows=[e['row'] for e in self.state(True)['log_buffer']]
        picked=next(row for row in rows if row[5]=='状態を対応中にした')
        self.assertEqual(picked[2],'💛20990101-1 を拾った')
        posted=next(row for row in rows if row[5]=='Slackに投稿した')
        self.assertNotEqual(posted[2],posted[5])
        self.assertEqual(posted[7],'1970-01-01 09:00 JST ts 1.000001')
        done=next(row for row in rows if row[5]=='完了にした')
        self.assertEqual(done[7],'https://app.notion.com/p/'+self.cfg.requests_page)
        self.assertIn('宛先全員の完了報告',done[2])

    def test_practice_log_never_invents_slack_timestamp(self):
        self.add_request();self.run_once()
        row=next(e['row'] for e in self.state()['log_buffer'] if e['row'][5]=='Slackに投稿した')
        self.assertEqual(row[7],'練習（実投稿なし）')
        self.assertEqual(slack_evidence(self.clock(),None),'手動確認（ts未確認）')

    def test_recovered_post_evidence_uses_slack_ts_not_recovery_time(self):
        posted_at=self.clock().timestamp()
        self.clock.advance(10800)
        self.assertEqual(slack_evidence(self.clock(),str(posted_at)),
                         '2099-01-01 09:00 JST ts '+str(posted_at))
        self.assertTrue(slack_evidence(self.clock(),'9'*400+'.1').startswith('投稿時刻未確認 ts '))

    def test_account_mismatch_continues_due_notion_logs_only(self):
        self.add_request(); self.slack.reject_account=True
        for seconds in (0,300,3300,3600,3600):
            self.clock.advance(seconds)
            self.assertEqual(self.run_once(live=True)['status'],'account_mismatch')
        self.assertEqual(self.slack.posts,[]);self.assertEqual(self.slack.find_calls,0)
        self.assertEqual(self.notion.reads,[])
        self.assertEqual(self.notion.writes,[('log',4)])
        rows=[r['table_row']['cells'] for r in self.notion.rows]
        self.assertEqual(len(rows),4)
        self.assertEqual(self.state(True)['log_buffer'],[])
        self.assertTrue(all(e[2][0]['text']['content']=='アカウント違いで停止' for e in rows))
        self.assertTrue(all(e[5][0]['text']['content']=='依頼処理・Slack操作を停止' for e in rows))

    def test_missing_expected_ids_accumulates_and_flushes_without_slack_auth(self):
        self.cfg=replace(self.cfg,expected_bot_id='',expected_user_id='')
        for seconds in (0,3600,3600,3600):
            self.clock.advance(seconds);self.run_once(live=True)
        self.assertEqual(self.slack.auth_calls,0);self.assertEqual(self.slack.posts,[])
        self.assertEqual(self.notion.reads,[]);self.assertEqual(self.notion.writes,[('log',4)])

    def test_account_mismatch_practice_validates_but_does_not_write(self):
        self.slack.reject_account=True
        self.run_once();self.clock.advance(10800);self.run_once()
        self.assertEqual(self.slack.posts,[]);self.assertEqual(self.notion.writes,[])
        records=[json.loads(x) for x in (self.root/'logs/practice.jsonl').read_text().splitlines()]
        self.assertTrue(any(r['operation']=='notion_log' for r in records))

    def test_account_mismatch_log_failure_preserves_buffer_and_never_posts(self):
        self.slack.reject_account=True;self.run_once(live=True);self.clock.advance(10800)
        self.notion.append_error=RemoteError('unknown',uncertain=True)
        self.assertEqual(self.run_once(live=True)['status'],'account_mismatch')
        state=self.state(True)
        self.assertIsNotNone(state['log_batch']);self.assertTrue(state['log_buffer'])
        self.assertEqual(self.slack.posts,[])
        self.run_once(live=True)
        self.assertEqual(self.slack.posts,[]);self.assertEqual(self.notion.writes,[])

    def test_stop_before_account_mismatch_flush_preserves_events(self):
        self.slack.reject_account=True;self.run_once(live=True);self.clock.advance(10800)
        class Notices:
            def append(inner, value): (self.root/'STOP').touch()
        self.notices=Notices()
        self.assertEqual(self.run_once(live=True)['status'],'account_mismatch')
        self.assertEqual(self.notion.writes,[]);self.assertEqual(self.slack.posts,[])

    def test_proven_pre_send_stop_does_not_count_post(self):
        self.add_request();error=StopRequested('guard');error.not_sent=True;self.slack.error=error
        result=self.run_once(live=True)
        self.assertEqual(result['status'],'stopped');self.assertEqual(result['posts'],0)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'queued')

    def test_pre_send_deadline_queues_without_history_then_sends_once_next_run(self):
        self.add_request();error=TimeBudgetExceeded('guard');error.not_sent=True;self.slack.error=error
        self.run_once(live=True)
        item=self.state(True)['outbox']['instruction:💛20990101-1']
        self.assertEqual(item['status'],'queued');self.assertEqual(item['last_attempt_result'],'deadline_before_send')
        self.assertEqual(self.slack.find_calls,0)
        for _ in range(3):self.run_once(live=True)
        self.assertEqual(sum(ch==self.cfg.instruction_channel for ch,_,_ in self.slack.posts),1)

    def test_deadline_without_boundary_evidence_holds(self):
        self.add_request();self.slack.error=TimeBudgetExceeded('during IO')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'held')

    def test_log_flush_reuses_discovery_rows_and_deduplicates(self):
        self.add_request();self.run_once(live=True)
        with patch.object(self.notion,'read_log_rows',side_effect=AssertionError('redundant full read')):
            self.run_once(live=True,flush=True)
        before=copy.deepcopy(self.notion.rows)
        self.run_once(live=True,flush=True)
        self.assertEqual(self.notion.rows,before)

    def test_corrupt_event_line_still_stops_without_deleting_evidence(self):
        self.run_once(live=True)
        path=self.root/'logs/events.jsonl';path.write_text('{broken\n')
        self.add_request();result=self.run_once(live=True)
        self.assertEqual(result['status'],'error')
        self.assertTrue(path.read_text().startswith('{broken\n'))
        self.assertFalse([x for x in self.slack.posts if x[0]==self.cfg.instruction_channel])


del FlowTests
