"""P-01..07 regression scenarios; no external services or credentials."""
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from riri.errors import RemoteError, StopRequested
from riri.patrol import Patrol
from riri.resolve import held_items, resolve_held
from riri.state import StateStore
from riri.status import practice_cleanup_items
from test_patrol_flow import FlowTests, request, report

class ReviewPFlows(FlowTests):
    def test_practice_cleanup_lists_saved_observation_not_live_claim(self):
        self.add_request();self.run_once();self.add_report();self.run_once()
        cleanup=practice_cleanup_items(self.state())
        self.assertEqual(len(cleanup),1)
        self.assertEqual(cleanup[0]['last_observed_notion_status'],'未着手')
        self.assertIsNotNone(cleanup[0]['observed_at'])
        self.assertTrue(cleanup[0]['needs_manual_verification'])
        self.notion.blocks[self.cfg.requests_page]=[request(status='完了')]
        self.run_once()
        self.assertEqual(practice_cleanup_items(self.state()),[])

    def test_old_practice_cleanup_is_marked_unverified(self):
        state=StateStore(self.root,'practice').load()
        state['requests']['💛20990101-1']={'status':'done','snapshot':{'status':'未着手'}}
        entry=practice_cleanup_items(state)[0]
        self.assertIsNone(entry['observed_at']);self.assertTrue(entry['needs_manual_verification'])

    def test_practice_cleanup_does_not_quote_untrusted_status_text(self):
        state=StateStore(self.root,'practice').load()
        state['requests']['💛20990101-1']={'status':'done','source_status_observed':'@channel secret_example'}
        self.assertEqual(practice_cleanup_items(state)[0]['last_observed_notion_status'],'不明（要確認）')

    def test_report_summary_retry_posts_once_then_records_report_events(self):
        self.run_once(live=True);self.add_report()
        self.slack.error=RemoteError('unknown summary',uncertain=True)
        self.run_once(live=True)
        state=self.state(True)
        key=next(k for k,v in state['outbox'].items() if v['kind']=='reports')
        self.assertEqual(state['outbox'][key]['status'],'held')
        self.assertNotIn('report-1',state['seen_reports'])
        resolve_held(replace(self.cfg,mode='live'),'notice',key,'not_posted',clock=self.clock)
        for _ in range(3):self.run_once(live=True)
        state=self.state(True);item=state['outbox'][key]
        self.assertEqual(item['status'],'posted');self.assertNotIn('retry_permitted_once',item)
        self.assertEqual(sum(item['marker'] in text for _,text,_ in self.slack.posts),1)
        self.assertIn('report-1',state['seen_reports'])
        events=state['log_buffer']
        self.assertEqual(sum(e['row'][5]=='たかむねりりあいすに報告' for e in events),1)

    def test_report_retry_reholds_and_new_notice_exists(self):
        self.run_once(live=True);self.add_report()
        self.slack.error=RemoteError('unknown summary',uncertain=True);self.run_once(live=True)
        key=next(k for k,v in self.state(True)['outbox'].items() if v['kind']=='reports')
        resolve_held(replace(self.cfg,mode='live'),'notice',key,'not_posted',clock=self.clock)
        self.slack.error=RemoteError('unknown again',uncertain=True);self.run_once(live=True)
        item=self.state(True)['outbox'][key]
        self.assertEqual(item['status'],'held');self.assertNotIn('retry_permitted_once',item)
        self.assertEqual(item['hold_episode'],2)
        self.assertTrue(any(x['kind']=='hold_notice' and x.get('held_key')==key and x.get('hold_episode')==2
                            for x in self.state(True)['outbox'].values()))

    def test_proven_pre_send_stop_returns_queued_without_history_or_hold(self):
        self.add_request()
        exc=StopRequested('pre-send STOP');exc.not_sent=True
        self.slack.error=exc
        result=self.run_once(live=True)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.slack.find_calls,0);self.assertEqual(self.slack.posts,[])
        item=self.state(True)['outbox']['instruction:💛20990101-1']
        self.assertEqual(item['status'],'queued')
        self.assertFalse(held_items(self.state(True)))
        self.run_once(live=True)
        self.assertEqual(sum(ch==self.cfg.instruction_channel for ch,_,_ in self.slack.posts),1)

    def test_stop_without_pre_send_proof_still_holds(self):
        self.add_request();self.slack.error=StopRequested('no boundary proof')
        self.assertEqual(self.run_once(live=True)['status'],'stopped')
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'held')
        self.assertEqual(self.slack.posts,[])

    def test_uncertain_stop_overrides_not_sent_tag(self):
        self.add_request();exc=StopRequested('uncertain');exc.not_sent=True;exc.uncertain=True
        self.slack.error=exc
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'held')

    def test_quarantine_notices_include_safe_ids_and_reporter_only(self):
        self.add_request(task='@channel');self.run_once(live=True)
        notice=next(text for _,text,_ in self.slack.posts if '投稿文の安全チェック' in text)
        self.assertIn('依頼ID：💛20990101-1',notice);self.assertNotIn('@channel',notice)
        self.add_report(name='あいす',id='bad-report')
        p=self.cfg.pages['あいす'].split('/')[-1]
        self.notion.blocks[p][0]['paragraph']['rich_text'][0]['text']['content']+=' @everyone'
        self.run_once(live=True)
        notice=next(text for _,text,_ in self.slack.posts if '安全チェックに通らない完了報告' in text)
        self.assertIn('依頼ID：💛20990101-1',notice);self.assertIn('報告した子：あいす',notice)
        self.assertNotIn('@everyone',notice)

    def test_known_unsent_unsafe_instruction_edit_in_progress_is_not_stranded(self):
        self.add_request();self.slack.error=RemoteError('definite reject',uncertain=False)
        self.run_once(live=True)
        state=self.state(True);state['outbox']['instruction:💛20990101-1']['text']='@channel'
        StateStore(self.root,'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['hold_reason'],'unsafe_content')
        self.notion.blocks[self.cfg.requests_page]=[request(status='対応中',task='修正した安全な依頼')]
        self.run_once(live=True)
        state=self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'],'posted')
        self.assertEqual(sum(ch==self.cfg.instruction_channel for ch,_,_ in self.slack.posts),1)
        self.assertFalse(held_items(state))

    def test_removed_unsafe_source_clears_hold_without_post_or_erasing_history(self):
        self.add_request(task='@channel');self.run_once(live=True)
        self.assertTrue(held_items(self.state(True)))
        self.notion.blocks[self.cfg.requests_page]=[]
        self.run_once(live=True)
        state=self.state(True)
        self.assertFalse(held_items(state))
        self.assertEqual(state['requests']['💛20990101-1']['status'],'source_removed')
        self.assertTrue(any(e['row'][5]=='自動投稿せず記録を保持した' for e in state['log_buffer']))
        self.assertFalse([x for x in self.slack.posts if x[0]==self.cfg.instruction_channel])

    def test_completed_corrected_unsafe_instruction_cancels_instead_of_stalling(self):
        self.add_request();self.slack.error=RemoteError('definite reject',uncertain=False)
        self.run_once(live=True)
        state=self.state(True);state['outbox']['instruction:💛20990101-1']['text']='@channel'
        StateStore(self.root,'live').save(state);self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page]=[request(status='完了',task='修正')]
        self.run_once(live=True)
        self.assertFalse(held_items(self.state(True)))
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1']['status'],'cancelled')
        self.assertFalse([x for x in self.slack.posts if x[0]==self.cfg.instruction_channel])

    def test_corrected_unsafe_instruction_to_riri_only_cancels_old_instruction(self):
        self.add_request();self.slack.error=RemoteError('definite reject',uncertain=False)
        self.run_once(live=True)
        state=self.state(True);state['outbox']['instruction:💛20990101-1']['text']='@channel'
        StateStore(self.root,'live').save(state);self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page]=[request(status='対応中',recipients='@りり',task='修正')]
        for _ in range(2):self.run_once(live=True)
        state=self.state(True)
        self.assertFalse(held_items(state))
        self.assertEqual(state['requests']['💛20990101-1']['status'],'skipped')
        self.assertEqual(state['outbox']['instruction:💛20990101-1']['status'],'cancelled')
        self.assertEqual(state['outbox']['instruction:💛20990101-1']['text'],'@channel')
        self.assertEqual(sum('はりり自身あての依頼' in text for _,text,_ in self.slack.posts),1)
        self.assertFalse([x for x in self.slack.posts if x[0]==self.cfg.instruction_channel])

    def test_incomplete_correction_remains_held_and_source_removal_is_audited(self):
        self.add_request(task='@channel');self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page]=[request(task='')]
        self.run_once(live=True)
        self.assertTrue(held_items(self.state(True)))
        self.notion.blocks[self.cfg.requests_page]=[];self.run_once(live=True)
        state=self.state(True)
        self.assertFalse(held_items(state))
        self.assertEqual(state['requests']['💛20990101-1']['status'],'source_removed')
        self.assertTrue(any(e['row'][5]=='自動投稿せず記録を保持した' for e in state['log_buffer']))

    def test_incomplete_then_valid_correction_retries_without_stranding(self):
        self.add_request(task='@channel');self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page]=[request(task='')];self.run_once(live=True)
        self.notion.blocks[self.cfg.requests_page]=[request(task='安全な修正')]
        for _ in range(2):self.run_once(live=True)
        self.assertFalse(held_items(self.state(True)))
        self.assertEqual(sum(ch==self.cfg.instruction_channel for ch,_,_ in self.slack.posts),1)

    def test_legacy_pending_correction_removed_source_clears_hold(self):
        self.add_request(task='@channel');self.run_once(live=True)
        state=self.state(True)
        state['requests']['💛20990101-1'].update(status='retry_after_edit',hold_reason='')
        StateStore(self.root,'live').save(state)
        self.notion.blocks[self.cfg.requests_page]=[];self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'],'source_removed')

for _name in tuple(dir(FlowTests)):
    if _name.startswith('test_') and _name not in ReviewPFlows.__dict__:setattr(ReviewPFlows,_name,None)
del FlowTests

