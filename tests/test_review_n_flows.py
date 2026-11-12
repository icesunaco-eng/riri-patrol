"""End-to-end regressions for review N-01 through N-06; network forbidden."""
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
from unittest.mock import patch
import contextlib

import patrol as cli
from riri.errors import NotionConflict, RemoteError, StopRequested, TimeBudgetExceeded
from riri.eventlog import notion_rows, row_ids, new_event
from riri.llm import DraftService
from riri.notion import NotionClient
from riri.patrol import Patrol
from riri.resolve import resolve_held, resolve_request, held_items
from riri.slack import SlackClient
from riri.state import StateStore
from test_api import code_response, page, row, who
from test_patrol_flow import FlowTests, request, report


class ReviewNFlows(FlowTests):
    def test_practice_completes_without_mutating_remote_fixture(self):
        self.cfg = replace(self.cfg, log_size_notice_bytes=1)
        self.add_request(recipients='@うさ @あいす')
        original = copy.deepcopy(self.notion.blocks[self.cfg.requests_page])
        self.assertEqual(self.run_once()['status'],'ok')
        self.assertEqual(self.state()['requests']['💛20990101-1']['practice_status'],'対応中')
        self.add_report(name='うさ',id='r-usa');self.add_report(name='あいす',id='r-aisu')
        self.clock.advance(10800)
        self.assertEqual(self.run_once()['status'],'ok')
        info=self.state()['requests']['💛20990101-1']
        self.assertEqual((info['status'],info['practice_status']),('done','完了'))
        self.assertEqual(self.notion.blocks[self.cfg.requests_page],original)
        self.assertEqual(self.notion.writes,[]);self.assertEqual(self.slack.posts,[])
        logs=[json.loads(x) for x in (self.root/'logs/practice.jsonl').read_text().splitlines()]
        self.assertTrue(any(x['operation']=='notion_log' for x in logs))
        self.assertTrue(any('整理するか' in x.get('text','') for x in logs))
        self.assertEqual(self.run_once()['status'],'ok')

    def test_real_notion_client_practice_completion_through_engine(self):
        original=request(recipients='@うさ @あいす')
        body=original['code']['rich_text'][0]['text']['content']
        before=dict(code_response(original['id'],body),last_edited_time='2099-01-01T00:00:00Z')
        pages={self.cfg.requests_page:[before]}
        pages.update({url.split('/')[-1]:[] for url in self.cfg.pages.values()})
        class OfflineAPI:
            calls=[]
            def request(inner,method,url,**kwargs):
                inner.calls.append((method,url))
                if 'slack.com' in url:
                    if 'auth.test' in url:return who()
                    if 'conversations.history' in url:return {'ok':True,'messages':[]}
                    raise AssertionError('no Slack remote write')
                if method!='GET':raise AssertionError('no Notion remote write')
                block_id=url.split('/blocks/')[1].split('/')[0].split('?')[0]
                if '/children' in url:return page(copy.deepcopy(pages[block_id]))
                self.assertEqual(block_id,original['id']);return copy.deepcopy(before)
        transport=OfflineAPI()
        cfg=replace(self.cfg,expected_bot_id='B123',expected_user_id='U123',slack_token=('xoxb' + '-test-only'))
        def run():
            return Patrol(cfg,SlackClient(cfg.slack_token,'B123','U123',transport,
                report_channel=cfg.report_channel, ice_mention_user_id=cfg.ice_mention_user_id),
                NotionClient('ntn_fake',transport),DraftService(transport,enabled=False),clock=self.clock).run()
        self.assertEqual(run()['status'],'ok')
        for name in ('うさ','あいす'):
            pages[cfg.pages[name].split('/')[-1]]=[report(name=name,id='report-'+name)]
        self.assertEqual(run()['status'],'ok')
        self.assertEqual(self.state()['requests']['💛20990101-1']['status'],'done')
        self.assertEqual(before['code']['rich_text'][0]['text']['content'],body)
        self.assertTrue(all(method=='GET' for method,url in transport.calls))

    def test_old_practice_state_revalidates_virtual_step_without_source_edit(self):
        self.add_request();self.run_once()
        state=self.state();state['requests']['💛20990101-1'].pop('practice_status')
        StateStore(self.root,'practice').save(state)
        original=copy.deepcopy(self.notion.blocks[self.cfg.requests_page])
        self.add_report()
        self.assertEqual(self.run_once()['status'],'ok')
        self.assertEqual(self.state()['requests']['💛20990101-1']['practice_status'],'完了')
        self.assertEqual(self.notion.blocks[self.cfg.requests_page],original)
        self.assertEqual(self.notion.writes,[])

    def test_reheld_retry_gets_one_new_notice_and_consumes_permission(self):
        self.add_request();self.slack.error=RemoteError('unknown',uncertain=True)
        self.run_once(live=True)
        resolve_request(replace(self.cfg,mode='live'),'💛20990101-1','not_posted',clock=self.clock)
        self.slack.error=RemoteError('unknown again',uncertain=True)
        self.run_once(live=True)
        state=self.state(True);item=state['outbox']['instruction:💛20990101-1']
        self.assertNotIn('retry_permitted_once',item)
        self.assertNotIn('retry_permitted_once',state['requests']['💛20990101-1'])
        self.assertEqual(item['hold_episode'],2)
        for _ in range(3):self.run_once(live=True)
        notices=[x for x in self.state(True)['outbox'].values() if x['kind']=='hold_notice' and x.get('held_key')=='instruction:💛20990101-1']
        self.assertEqual(len(notices),2)
        second=[x for x in notices if x['hold_episode']==2][0]
        self.assertEqual(second['status'],'posted')
        self.assertEqual(sum(second['marker'] in text for _,text,_ in self.slack.posts),1)

    def test_completed_unsafe_source_does_not_block_new_request(self):
        self.add_request(1,status='完了',task='secret_manager')
        self.add_request(2)
        self.assertEqual(self.run_once(live=True)['instructions'],1)
        self.assertIn('💛20990101-2',self.slack.posts[0][1])

    def test_one_unsafe_candidate_is_held_and_other_candidate_runs(self):
        self.add_request(1,task='@channel secret_manager');self.add_request(2)
        self.assertEqual(self.run_once(live=True)['instructions'],1)
        state=self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['hold_reason'],'unsafe_content')
        self.assertEqual(state['requests']['💛20990101-2']['status'],'posted')
        for _ in range(2):self.run_once(live=True)
        self.assertEqual(sum('投稿文の安全チェック' in text for _,text,_ in self.slack.posts),1)
        self.assertFalse(any('@channel' in text or 'secret_manager' in text for _,text,_ in self.slack.posts))
        self.notion.blocks[self.cfg.requests_page]=[request(1),request(2,status='対応中')]
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'],'posted')

    def test_unsafe_newest_report_isolated_never_uses_old_complete(self):
        self.add_request();self.run_once(live=True)
        self.add_report(id='old')
        bad=report(id='new');bad['paragraph']['rich_text'][0]['text']['content']+=' @channel'
        self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]].insert(0,bad)
        self.add_report(name='あいす',number=2,id='safe-other')
        self.run_once(live=True)
        state=self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'],'posted')
        self.assertIn('safe-other',state['seen_reports'])
        self.assertTrue(any(x['type']=='report' for x in held_items(state)))
        self.assertFalse(any('@channel' in text for _,text,_ in self.slack.posts))
        page_id=self.cfg.pages['うさ'].split('/')[-1]
        self.notion.blocks[page_id][0]=report(id='new')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'],'done')
        self.assertFalse([x for x in held_items(self.state(True)) if x['type']=='report'])

    def test_stop_during_llm_exits_without_notion_or_later_work(self):
        self.add_request()
        class LLM:
            def draft(inner,*args):
                (self.root/'STOP').touch()
                raise StopRequested('STOP')
        cfg=replace(self.cfg,mode='live')
        runner=Patrol(cfg,self.slack,self.notion,LLM(),clock=self.clock)
        with patch.object(runner,'maintenance',side_effect=AssertionError('not after STOP')):
            self.assertEqual(runner.run()['status'],'stopped')
        self.assertEqual(self.slack.posts,[]);self.assertEqual(self.notion.writes,[])
        self.assertEqual(self.state(True)['requests'],{})

    def test_unsafe_report_reversion_becomes_visible_held_again(self):
        self.run_once(live=True)
        page_id=self.cfg.pages['うさ'].split('/')[-1]
        bad=report(id='revert-report');bad['paragraph']['rich_text'][0]['text']['content']+=' @channel'
        self.notion.blocks[page_id]=[bad]
        self.assertEqual(self.run_once(live=True)['status'],'held')
        self.notion.blocks[page_id]=[report(id='revert-report')]
        self.assertEqual(self.run_once(live=True)['status'],'ok')
        self.notion.blocks[page_id]=[bad]
        self.assertEqual(self.run_once(live=True)['status'],'held')
        self.assertTrue(any(x['type']=='report' for x in held_items(self.state(True))))
        self.assertEqual(sum('安全チェックに通らない完了報告' in text for _,text,_ in self.slack.posts),1)

    def test_deadline_during_llm_does_not_prepare_request(self):
        self.add_request()
        class LLM:
            def draft(inner,*args):raise TimeBudgetExceeded('deadline')
        cfg=replace(self.cfg,mode='live')
        runner=Patrol(cfg,self.slack,self.notion,LLM(),clock=self.clock)
        with patch.object(runner,'maintenance',side_effect=AssertionError('not after deadline')):
            self.assertEqual(runner.run()['error'],'TimeBudgetExceeded')
        self.assertEqual(self.notion.writes,[]);self.assertEqual(self.state(True)['requests'],{})

    def test_stop_after_possible_notion_write_holds_and_notifies_on_next_run(self):
        self.add_request()
        error=StopRequested('STOP after PATCH');error.uncertain=True
        with patch.object(self.notion,'set_status',side_effect=error):
            result=self.run_once(live=True)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.slack.posts,[])
        info=self.state(True)['requests']['💛20990101-1']
        self.assertEqual(info['status'],'held');self.assertIn('notion_write_intent',info)
        self.run_once(live=True)
        self.assertFalse([x for x in self.slack.posts if x[0]==self.cfg.instruction_channel])
        self.assertTrue(any('Notion更新を確認できない' in text for _,text,_ in self.slack.posts))

    def test_stop_before_notion_write_does_not_falsely_hold_as_written(self):
        self.add_request()
        with patch.object(self.notion,'set_status',side_effect=StopRequested('STOP before PATCH')):
            self.assertEqual(self.run_once(live=True)['status'],'stopped')
        info=self.state(True)['requests']['💛20990101-1']
        self.assertEqual(info['status'],'prepared');self.assertNotIn('notion_write_intent',info)
        self.assertEqual(self.slack.posts,[]);self.assertEqual(self.notion.writes,[])

    def test_notion_checked_revalidates_before_unblocking_instruction(self):
        self.add_request();self.slack.error=RemoteError('definite rejection',uncertain=False)
        self.run_once(live=True)
        state=self.state(True);info=state['requests']['💛20990101-1'];item=state['outbox']['instruction:💛20990101-1']
        info.update(status='held',hold_reason='notion_conflict',notion_write_intent={'status':'対応中'})
        item.update(status='held',hold_reason='notion_conflict')
        StateStore(self.root,'live').save(state)
        resolve_held(replace(self.cfg,mode='live'),'notion','💛20990101-1','checked',clock=self.clock)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'blocked')
        self.notion.blocks[self.cfg.requests_page]=[request(status='未着手')]
        self.run_once(live=True)
        self.assertEqual(sum(ch==self.cfg.instruction_channel for ch,_,_ in self.slack.posts),1)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'],'posted')

    def test_log_partial_resolution_preserves_missing_events_and_deduplicates(self):
        self.run_once(live=True)
        state=self.state(True)
        events=[new_event(self.clock(),self.clock(),'test',str(n)) for n in range(2)]
        state['log_buffer']=events
        state['log_batch']={'batch_id':'partial','event_ids':[x['event_id'] for x in events],'status':'held'}
        StateStore(self.root,'live').save(state)
        self.notion.rows=notion_rows(events[:1])
        resolve_held(replace(self.cfg,mode='live'),'log','partial','not_written',clock=self.clock)
        self.run_once(live=True,flush=True)
        all_ids=[next(iter(row_ids([r]))) for r in self.notion.rows]
        for e in events:self.assertEqual(all_ids.count(e['event_id']),1)
        self.assertEqual(self.state(True)['log_buffer'],[])

    def test_cancelled_error_notice_does_not_block_future_error_notification(self):
        self.slack.error=RemoteError('unknown',uncertain=True)
        self.notion.read_error=RemoteError('read failed')
        self.run_once(live=True)
        held=[(k,v) for k,v in self.state(True)['outbox'].items() if v['kind']=='error' and v['status']=='held']
        self.assertEqual(len(held),1)
        resolve_held(replace(self.cfg,mode='live'),'notice',held[0][0],'cancel',clock=self.clock)
        self.clock.advance(3600);self.run_once(live=True)
        self.assertTrue(any(v['kind']=='error' and v['status']=='posted' for v in self.state(True)['outbox'].values()))

    def test_status_lists_all_holds_without_network_or_raw_bodies(self):
        self.add_request(task='secret_manager');self.run_once(live=True)
        (self.root/'.env').write_text('RIRI_MODE=live\nEXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\n')
        with patch.object(cli,'Transport',side_effect=AssertionError('offline')):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):self.assertEqual(cli.main(['--root',str(self.root),'--status']),0)
        status=json.loads(output.getvalue())
        self.assertEqual(status['held'][0]['type'],'request')
        self.assertNotIn('secret_manager',output.getvalue())


for _name in tuple(dir(FlowTests)):
    if _name.startswith('test_') and _name not in ReviewNFlows.__dict__:
        setattr(ReviewNFlows,_name,None)
del FlowTests
