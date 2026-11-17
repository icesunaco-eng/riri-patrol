"""Handoff guards at the real patrol call sites; synthetic clients, no network."""
import copy
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
from riri.errors import TimeBudgetExceeded


TARGET = 'https://app.notion.com/p/416f1dff94dc815bb595ca8fe52527b5'


class HandoffGuards(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_report = flows.FlowTests.add_report

    def add_request(self, number=1, *, target=True, recipients='@のあ', **kwargs):
        req = flows.request(number, recipients=recipients, **kwargs)
        if target:
            req['code']['rich_text'][0]['text']['content'] += '\n案件ページ：' + TARGET
        self.notion.blocks[self.cfg.requests_page].insert(0, req)
        return req

    def put_report(self, text, block_id='unknown-report'):
        page = self.cfg.pages['のあ'].split('/')[-1]
        self.notion.blocks[page].insert(0, flows.block(text, block_id, 'paragraph'))

    def start_and_complete(self, number=1):
        self.add_request(number)
        self.run_once(live=True)
        self.add_report(name='のあ', number=number)

    def unknown_report(self):
        return ('【完了報告】\n依頼ID：不明（起動データが空だったため）\n'
                '報告した子：のあ\n結果：できなかった\nやったこと：内容不明のため停止')

    def test_unknown_id_is_quarantined_without_blocking_distinct_known_completion(self):
        self.start_and_complete()
        self.put_report(self.unknown_report())
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        state = self.state(True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        records = [r for r in state['quarantined_reports'].values() if r.get('block_id') == 'unknown-report']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'isolated')
        self.assertTrue(records[0]['needs_review'])
        self.assertEqual(records[0]['reason'], 'unidentified_report')
        self.assertEqual(records[0]['request_id'], '')
        self.assertEqual(records[0]['reporter'], 'のあ')
        self.assertNotIn('text', records[0])
        self.run_once(live=True)
        self.assertEqual(self.state(True)['quarantined_reports'], state['quarantined_reports'])

    def test_fixed_unknown_report_preserves_quarantine_history(self):
        self.start_and_complete()
        self.put_report(self.unknown_report())
        self.run_once(live=True)
        page = self.cfg.pages['のあ'].split('/')[-1]
        self.notion.blocks[page][0] = flows.report(name='のあ', id='unknown-report', number=2)
        self.run_once(live=True)
        records = list(self.state(True)['quarantined_reports'].values())
        self.assertEqual(records[0]['status'], 'superseded')
        self.assertIn('superseded_at', records[0])

    def test_partly_identifiable_bad_id_blocks_only_its_candidate(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2, task='別の作業を確認する'); self.run_once(live=True)
        self.add_report(name='のあ', number=1, id='report-one')
        self.add_report(name='のあ', number=2, id='report-two')
        self.put_report('【完了報告】\n依頼ID：💛20990101-1x\n報告した子：のあ\n結果：不明')
        self.run_once(live=True)
        state = self.state(True)['requests']
        self.assertEqual(state['💛20990101-1']['status'], 'posted')
        self.assertEqual(state['💛20990101-2']['status'], 'done')

    def test_mismatched_reporter_still_blocks_its_known_id(self):
        self.start_and_complete()
        self.put_report('【完了報告】\n依頼ID：💛20990101-1\n報告した子：うさ\n結果：完了')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'posted')

    def test_conflicting_duplicate_id_fields_block_both_candidates(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2, task='別の作業を確認する'); self.run_once(live=True)
        self.add_report(name='のあ', number=1, id='report-one')
        self.add_report(name='のあ', number=2, id='report-two')
        self.put_report('【完了報告】\n依頼ID：💛20990101-1\n依頼ID：💛20990101-2\n報告した子：のあ\n結果：完了')
        self.run_once(live=True)
        self.assertEqual([v['status'] for v in self.state(True)['requests'].values()], ['posted', 'posted'])

    def test_same_target_execution_later_id_is_held_before_dispatch(self):
        self.add_request(1); self.run_once(live=True)
        later = self.add_request(2)
        original_text = copy.deepcopy(later)
        self.run_once(live=True)
        state = self.state(True)
        duplicate = state['requests']['💛20990101-2']
        self.assertEqual(duplicate['status'], 'held')
        self.assertEqual(duplicate['hold_reason'], 'content_duplicate')
        self.assertEqual(duplicate['duplicate_of'], '💛20990101-1')
        self.assertNotIn('instruction:💛20990101-2', state['outbox'])
        self.assertEqual(later, original_text)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(sum('内容が同じ' in p[1] for p in self.slack.posts), 1)

    def test_completed_earlier_execution_does_not_block_new_id(self):
        self.start_and_complete(); self.run_once(live=True)
        self.add_request(2)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')

    def test_identical_minimal_template_without_target_is_not_held(self):
        self.add_request(1, target=False); self.run_once(live=True)
        self.add_request(2, target=False); self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')

    def test_different_execution_or_recipients_is_not_a_duplicate(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2, task='異なる作業を確認する'); self.run_once(live=True)
        self.add_request(3, recipients='@うさ'); self.run_once(live=True)
        self.assertEqual([v['status'] for v in self.state(True)['requests'].values()], ['posted'] * 3)

    def test_existing_posted_duplicates_are_never_changed_or_resent(self):
        self.add_request(1); self.run_once(live=True)
        # A pre-upgrade duplicate already has a posted record. Preserve it.
        self.add_request(2)
        with patch('riri.patrol.older_active_duplicate', return_value=None):
            self.run_once(live=True)
        state = self.state(True)
        before = copy.deepcopy(state['outbox'])
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox'], before)
        self.assertEqual([v['status'] for v in self.state(True)['requests'].values()], ['posted'] * 2)

    def test_placeholder_resources_do_not_claim_an_identifiable_target(self):
        for value in ('なし', '未確認', '架空データのみ', 'なし（変更しない）', '未確認（後で確認）'):
            with self.subTest(value=value):
                probe = HandoffGuards()
                probe.setUp()
                try:
                    for number in (1, 2):
                        req = probe.add_request(number, target=False)
                        req['code']['rich_text'][0]['text']['content'] += '\n使うもの：' + value
                        probe.run_once(live=True)
                    self.assertEqual(probe.state(True)['requests']['💛20990101-2']['status'], 'posted')
                finally:
                    probe.tearDown()

    def test_queued_instruction_rechecks_fresh_source_before_send(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2, task='別の作業を確認する')
        error = TimeBudgetExceeded('known unsent'); error.not_sent = True
        self.slack.error = error
        self.assertEqual(self.run_once(live=True)['status'], 'deadline')
        before = sum(ch == self.cfg.instruction_channel for ch, _, _ in self.slack.posts)
        original_read = self.notion.read_blocks
        calls = 0
        def change_after_initial_read(page):
            nonlocal calls
            if page == self.cfg.requests_page:
                calls += 1
                if calls == 2:
                    source = self.notion.blocks[page][0]['code']['rich_text'][0]['text']
                    source['content'] = source['content'].replace('別の作業を確認する', '手順を確認する')
            return original_read(page)
        with patch.object(self.notion, 'read_blocks', side_effect=change_after_initial_read):
            self.run_once(live=True)
        state = self.state(True)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(state['requests']['💛20990101-2']['hold_reason'], 'content_duplicate')
        self.assertEqual(state['outbox']['instruction:💛20990101-2']['status'], 'held')
        self.assertEqual(sum(ch == self.cfg.instruction_channel for ch, _, _ in self.slack.posts), before)

    def test_duplicate_hold_never_autoresumes_after_original_completes(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2); self.run_once(live=True)
        self.add_report(name='のあ', number=1)
        self.run_once(live=True)
        self.add_request(3); self.run_once(live=True)
        state = self.state(True)['requests']
        self.assertEqual(state['💛20990101-1']['status'], 'done')
        self.assertEqual(state['💛20990101-2']['status'], 'held')
        self.assertEqual(state['💛20990101-3']['status'], 'posted')

    def test_existing_posted_duplicates_can_complete_from_their_own_reports(self):
        self.add_request(1); self.run_once(live=True)
        self.add_request(2)
        with patch('riri.patrol.older_active_duplicate', return_value=None):
            self.run_once(live=True)
        markers={item['marker'] for item in self.state(True)['outbox'].values()
                 if item['kind']=='instruction'}
        before=sum(ch==self.cfg.instruction_channel and bool(markers.intersection(text.splitlines()))
                   for ch,text,_ in self.slack.posts)
        self.add_report(name='のあ', number=1, id='old-one-report')
        self.add_report(name='のあ', number=2, id='old-two-report')
        self.run_once(live=True)
        self.assertEqual([v['status'] for v in self.state(True)['requests'].values()], ['done', 'done'])
        self.assertEqual(sum(ch==self.cfg.instruction_channel and bool(markers.intersection(text.splitlines()))
                             for ch,text,_ in self.slack.posts),before)
        reports=[item for item in self.state(True)['outbox'].values() if item['kind']=='reports']
        self.assertEqual({item['channel'] for item in reports},
                         {self.cfg.instruction_channel,self.cfg.report_channel})

    def test_completed_source_does_not_block_even_if_local_observation_is_stale(self):
        original = self.add_request(1); self.run_once(live=True)
        source = original['code']['rich_text'][0]['text']
        source['content'] = source['content'].replace('状態：対応中', '状態：完了')
        self.add_request(2); self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'posted')


if __name__ == '__main__':
    unittest.main()
