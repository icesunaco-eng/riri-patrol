"""Per-request completion review must not abort unrelated patrol progress."""
import copy
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
from riri.eventlog import iso
from riri.errors import NotionConflict, StopRequested, TimeBudgetExceeded
from riri.parse import replace_status
from riri.state import StateStore

RID1 = '💛20990101-1'
RID2 = '💛20990101-2'


class CompletionIsolationFlows(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def accepted_pair(self, second_recipients='@のあ'):
        for number in (1, 2):
            self.add_request(number, recipients=second_recipients if number == 2 else '@のあ',
                task='模擬確認' + str(number))
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.clock.advance()
        self.original_first = copy.deepcopy(next(b for b in self.notion.blocks[self.cfg.requests_page]
            if b['id'] == 'request-1'))

    def complete_pair(self):
        self.add_report(name='のあ', number=1, id='report-first')
        self.add_report(name='のあ', number=2, id='report-second')

    def alter_first(self, mode):
        blocks = self.notion.blocks[self.cfg.requests_page]
        first = next(b for b in blocks if b['id'] == 'request-1')
        if mode == 'missing':
            blocks.remove(first)
        elif mode == 'replaced':
            first['id'] = 'replacement-first'
        elif mode == 'status':
            text = first['code']['rich_text'][0]['text']
            text['content'] = replace_status(text['content'], '未着手')
        elif mode == 'missing_status':
            text = first['code']['rich_text'][0]['text']
            text['content'] = text['content'].replace('状態：対応中\n', '')
        elif mode == 'invalid_status':
            text = first['code']['rich_text'][0]['text']
            text['content'] = text['content'].replace('状態：対応中', '状態：中止')
        else:
            first['code']['rich_text'][0]['text']['content'] += '\n手順追記：受付後の変更'

    def assert_isolated(self, mode, reason):
        self.accepted_pair()
        before = self.state(True)
        snapshot = copy.deepcopy(before['requests'][RID1]['snapshot'])
        instruction = copy.deepcopy(before['outbox']['instruction:' + RID1])
        self.alter_first(mode)
        self.complete_pair()
        self.clock.advance(10800)
        result = self.run_once(live=True, flush=True)
        state = self.state(True)
        self.assertNotIn('error', result)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result.get('completion_reviews'), 1)
        self.assertEqual(state['requests'][RID2]['status'], 'done')
        self.assertEqual(state['requests'][RID1]['status'], 'posted')
        self.assertEqual(state['requests'][RID1]['completion_hold']['status'], 'pending')
        self.assertEqual(state['requests'][RID1]['completion_hold']['reason'], reason)
        self.assertEqual(state['requests'][RID1]['snapshot'], snapshot)
        self.assertEqual(state['outbox']['instruction:' + RID1], instruction)
        self.assertNotIn(('status', 'request-1', '完了'), self.notion.writes)
        self.assertEqual(state['last_run_at'], iso(self.clock()))
        self.assertEqual(state['core_error_serial'], before['core_error_serial'])
        self.assertIsNone(state['last_error'])
        self.assertTrue(any(write[0] == 'log' for write in self.notion.writes))
        posts = copy.deepcopy(self.slack.posts)
        for _ in range(3):
            self.clock.advance()
            again = self.run_once(live=True)
            self.assertNotIn('error', again)
            self.assertEqual(again['status'], 'ok')
            self.assertEqual(again.get('completion_reviews'), 1)
        after = self.state(True)
        self.assertEqual(self.slack.posts, posts)
        self.assertEqual(after['core_error_serial'], before['core_error_serial'])
        self.assertEqual(after['outbox']['instruction:' + RID1], instruction)
        return snapshot, instruction

    def test_changed_first_source_isolated_while_second_completes_and_flush_finishes(self):
        self.assert_isolated('changed', 'source_changed')

    def test_missing_first_source_isolated_while_second_completes(self):
        self.assert_isolated('missing', 'source_missing')

    def test_replaced_first_block_isolated_while_second_completes(self):
        self.assert_isolated('replaced', 'source_changed')

    def test_unexpected_first_status_isolated_while_second_completes(self):
        self.assert_isolated('status', 'source_status_changed')

    def test_missing_current_status_line_is_a_local_completion_review(self):
        self.assert_isolated('missing_status', 'source_status_changed')

    def test_invalid_current_status_line_is_a_local_completion_review(self):
        self.assert_isolated('invalid_status', 'source_status_changed')

    def test_corrupt_accepted_snapshot_status_is_fatal_instead_of_source_review(self):
        self.accepted_pair()
        self.complete_pair()
        state = self.state(True)
        snapshot = state['requests'][RID1]['snapshot']
        snapshot['text'] = snapshot['text'].replace('状態：未着手\n', '')
        damaged = copy.deepcopy(snapshot)
        before = state['last_run_at']
        StateStore(self.root, 'live').save(state)
        self.clock.advance()
        result = self.run_once(live=True)
        after = self.state(True)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error'], 'StatusMissing')
        self.assertEqual(after['requests'][RID1]['snapshot'], damaged)
        self.assertNotIn('completion_hold', after['requests'][RID1])
        self.assertNotEqual(after['requests'][RID2]['status'], 'done')
        self.assertEqual(after['last_run_at'], before)

    def test_completion_notice_is_once_per_pending_episode_and_respects_post_cap(self):
        self.accepted_pair()
        self.cfg = replace(self.cfg, max_posts=1)
        self.alter_first('changed')
        self.complete_pair()
        for _ in range(8):
            self.clock.advance()
            result = self.run_once(live=True)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result.get('completion_reviews'), 1)
            self.assertLessEqual(result['posts'], 1)
        state = self.state(True)
        notices = [item for item in state['outbox'].values() if item['kind'] == 'completion_notice']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['status'], 'posted')
        marker = notices[0]['marker']
        self.assertEqual(sum(marker in text.splitlines() for _, text, _ in self.slack.posts), 1)
        self.assertEqual(state['requests'][RID1]['completion_hold']['episode'], 1)
        self.assertEqual(state['requests'][RID2]['status'], 'done')
        self.assertEqual(state['core_error_serial'], 0)

    def test_status_cli_shows_review_metadata_without_source_body_or_network(self):
        self.assert_isolated('changed', 'source_changed')
        import patrol as cli
        output = io.StringIO()
        path = self.root / 'state/state.json'
        before = path.read_bytes()
        cfg = replace(self.cfg, mode='live')
        with patch.object(cli.Config, 'load', return_value=cfg), \
                patch.object(cli, 'Transport', side_effect=AssertionError('status must be offline')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--root', str(self.root), '--status']), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(len(summary['completion_reviews']), 1)
        review = summary['completion_reviews'][0]
        self.assertIn(RID1, review['request'])
        self.assertEqual(review['reason'], 'source_changed')
        self.assertTrue(review['source_restore_required'])
        self.assertIn('first_observed_at', review)
        self.assertIn('last_observed_at', review)
        self.assertNotIn('模擬確認', output.getvalue())
        self.assertNotIn('受付後の変更', output.getvalue())
        self.assertNotIn('snapshot', output.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_queued_generic_recovery_does_not_claim_success_while_review_pending(self):
        self.assert_isolated('changed', 'source_changed')
        state = self.state(True)
        state['last_error'] = 'SafetyError'
        state['core_error_serial'] = 1
        key = 'recovery:synthetic-existing'
        marker = 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]
        state['outbox'][key] = {'kind': 'recovery', 'status': 'queued',
            'channel': self.cfg.report_channel, 'recovery_error_serial': 1,
            'created_at': iso(self.clock()), 'marker': marker,
            'text': '<@' + self.cfg.ice_mention_user_id + '>\n直りました🍚🔥\n' + marker}
        StateStore(self.root, 'live').save(state)
        for _ in range(3):
            self.clock.advance()
            result = self.run_once(live=True)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result.get('completion_reviews'), 1)
        after = self.state(True)
        self.assertEqual(after['outbox'][key]['status'], 'queued')
        self.assertEqual(after['last_error'], 'SafetyError')
        self.assertFalse(any(marker in text.splitlines() for _, text, _ in self.slack.posts))
        self.assertEqual(sum(item['kind'] == 'recovery' for item in after['outbox'].values()), 1)

    def test_restoring_exact_original_resolves_hold_without_instruction_resend(self):
        snapshot, instruction = self.assert_isolated('changed', 'source_changed')
        requests = self.notion.blocks[self.cfg.requests_page]
        requests[:] = [b for b in requests if b['id'] != 'request-1']
        requests.append(copy.deepcopy(self.original_first))
        self.clock.advance()
        result = self.run_once(live=True)
        state = self.state(True)
        self.assertNotIn('error', result)
        self.assertEqual(state['requests'][RID1]['status'], 'done')
        self.assertEqual(state['requests'][RID1]['completion_hold']['status'], 'resolved')
        self.assertEqual(state['requests'][RID1]['snapshot'], snapshot)
        self.assertEqual(state['outbox']['instruction:' + RID1], instruction)
        self.assertEqual(sum(channel == self.cfg.instruction_channel and '【りりからのお願い】' in text
            and RID1 in text for channel, text, _ in self.slack.posts), 1)
        posts = copy.deepcopy(self.slack.posts)
        self.clock.advance()
        self.run_once(live=True)
        self.assertEqual(self.slack.posts, posts)

    def newer_noncomplete_never_completes_behind_an_isolated_source(self, outcome):
        self.accepted_pair(second_recipients='@のあ @かれん')
        self.alter_first('changed')
        self.add_report(name='のあ', number=1, id='report-first')
        self.add_report(name='のあ', number=2, id='older-second', result='完了')
        self.run_once(live=True)
        prior = self.state(True)
        self.assertIn('older-second', prior['seen_reports'])
        self.assertEqual(prior['requests'][RID2]['status'], 'posted')
        self.add_report(name='のあ', number=2, id='newer-second', result=outcome)
        self.add_report(name='かれん', number=2, id='karen-second', result='完了')
        self.clock.advance()
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['requests'][RID1]['completion_hold']['status'], 'pending')
        self.assertEqual(state['requests'][RID2]['status'], 'posted')
        self.assertNotIn(('status', 'request-2', '完了'), self.notion.writes)
        self.assertIn('newer-second', state['seen_reports'])
        self.assertIn('karen-second', state['seen_reports'])
        self.assertEqual(state['seen_report_versions']['older-second'], prior['seen_report_versions']['older-second'])

    def test_newer_partial_does_not_inherit_older_complete_while_other_source_waits(self):
        self.newer_noncomplete_never_completes_behind_an_isolated_source('一部完了')

    def test_newer_failed_does_not_inherit_older_complete_while_other_source_waits(self):
        self.newer_noncomplete_never_completes_behind_an_isolated_source('できなかった')

    def write_boundary_still_stops(self, error, expected_status, expected_error=None):
        self.accepted_pair()
        self.alter_first('changed')
        self.complete_pair()
        before = self.state(True)['last_run_at']
        original = self.notion.set_status
        attempts = []
        def reject(block_id, original_text, status, **kwargs):
            if block_id == 'request-2' and status == '完了':
                attempts.append(block_id)
                raise error
            return original(block_id, original_text, status, **kwargs)
        self.notion.set_status = reject
        self.clock.advance()
        result = self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(result['status'], expected_status)
        if expected_error:
            self.assertEqual(result['error'], expected_error)
        self.assertEqual(attempts, ['request-2'])
        self.assertNotEqual(state['requests'][RID2]['status'], 'done')
        self.assertNotIn('completion_hold', state['requests'][RID2])
        self.assertEqual(state['last_run_at'], before)
        return state

    def test_stop_during_valid_completion_is_not_swallowed_as_source_review(self):
        error = StopRequested('synthetic STOP before completion write')
        error.not_sent = True
        self.write_boundary_still_stops(error, 'stopped')

    def test_deadline_during_valid_completion_is_not_swallowed_as_source_review(self):
        error = TimeBudgetExceeded('synthetic deadline before completion write')
        error.not_sent = True
        self.write_boundary_still_stops(error, 'deadline')

    def test_notion_conflict_is_held_as_uncertain_write_and_stops_the_run(self):
        state = self.write_boundary_still_stops(NotionConflict('synthetic write conflict'), 'error', 'NotionConflict')
        self.assertEqual(state['requests'][RID2]['status'], 'held')
        self.assertEqual(state['requests'][RID2]['hold_reason'], 'notion_conflict')
        self.assertEqual(state['requests'][RID2]['notion_write_intent']['status'], '完了')


if __name__ == '__main__':
    unittest.main()
