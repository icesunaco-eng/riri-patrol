"""Durable two-room workflows using synthetic, network-free clients."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
from riri.errors import RemoteError
from riri.errors import UnsafeContent
from riri.patrol import Patrol
from riri.resolve import resolve_held
from riri.state import StateStore


class DualFlowTests(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def change_requester(self, number, name):
        for b in self.notion.blocks[self.cfg.requests_page]:
            if b['id'] == 'request-' + str(number):
                entry = b['code']['rich_text'][0]['text']
                entry['content'] = entry['content'].replace('依頼した子：うさ', '依頼した子：' + name)

    def report_posts(self):
        return [p for p in self.slack.posts if '作業場所URL（' in p[1]]

    def test_aisu_both_intents_durable_before_source_ack(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        state = self.state(True)
        src = state['outbox']['instruction:💛20990101-1']
        dst = state['outbox']['instruction-gdm:💛20990101-1']
        self.assertEqual((src['status'], dst['status']), ('posted', 'queued'))
        self.assertEqual(src['delivery_policy'], dst['delivery_policy'])
        self.assertTrue(src['text'].startswith('<@U0TESTICE000>\n'))
        self.assertEqual(src['text'].splitlines()[:-1], dst['text'].splitlines()[:-1])
        self.assertNotEqual(src['marker'], dst['marker'])
        self.assertEqual(state['instruction_deliveries']['💛20990101-1']['status'], 'partial')
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 2)

    def test_edited_source_after_first_room_holds_remaining_only(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        entry = self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']
        entry['content'] = entry['content'].replace('読んで確かめる', '変更後の手順')
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(state['outbox']['instruction-gdm:💛20990101-1']['hold_reason'], 'source_changed')

    def test_requester_is_not_report_author_and_both_rooms_match(self):
        self.add_request(recipients='@あいす')
        self.change_requester(1, 'のあ')
        self.run_once(live=True)
        self.add_report(name='あいす')
        self.run_once(live=True)
        posts = self.report_posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual({p[0] for p in posts}, {self.cfg.instruction_channel, self.cfg.report_channel})
        self.assertTrue(all(p[1].startswith('@のあ\n') for p in posts))
        self.assertTrue(all('<@U0TESTICE000>' not in p[1] for p in posts))
        bundle = next(iter(self.state(True)['report_deliveries'].values()))
        self.assertEqual(bundle['status'], 'posted')
        self.assertEqual(bundle['recipient_receipt'], 'unverified')
        self.assertEqual(bundle['mention_mode'], 'text')
        self.assertEqual(self.state(True)['seen_reports']['report-1'], bundle['reports'][0]['digest'])

    def test_first_report_ack_cannot_consume_second_room_after_restart(self):
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_report(name='あいす')
        self.run_once(live=True)
        state = self.state(True)
        self.assertNotIn('report-1', state['seen_reports'])
        bundle = next(iter(state['report_deliveries'].values()))
        self.assertEqual(bundle['status'], 'partial')
        self.run_once(live=True)
        self.assertEqual(len(self.report_posts()), 2)
        self.assertIn('report-1', self.state(True)['seen_reports'])
        self.run_once(live=True)
        self.assertEqual(len(self.report_posts()), 2)

    def test_requester_unconfirmed_preserves_report_without_wrong_mention(self):
        self.run_once(live=True)
        self.add_report(name='あいす')
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(self.report_posts(), [])
        self.assertTrue(state['report_requester_waiting'])
        self.assertNotIn('report-1', state['seen_reports'])

    def test_different_requesters_never_share_batch(self):
        self.add_request(1, recipients='@うさ')
        self.add_request(2, recipients='@うさ')
        self.change_requester(1, 'のあ')
        self.change_requester(2, 'みに')
        self.run_once(live=True)
        self.run_once(live=True)
        self.add_report(number=1, name='うさ', id='report-a')
        self.add_report(number=2, name='うさ', id='report-b')
        self.run_once(live=True)
        posts = self.report_posts()
        self.assertEqual(len(posts), 4)
        self.assertEqual(sum(p[1].startswith('@のあ\n') for p in posts), 2)
        self.assertEqual(sum(p[1].startswith('@みに\n') for p in posts), 2)
        self.assertTrue(all(not ('20990101-1' in p[1] and '20990101-2' in p[1]) for p in posts))

    def test_legacy_intent_preserves_original_sender_and_text(self):
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        state = self.state(True)
        before = dict(state['outbox']['instruction:💛20990101-1'])
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:💛20990101-1'], before)

    def test_pair_planning_failure_cannot_persist_a_single_instruction(self):
        self.add_request(recipients='@あいす')
        original = Patrol.queue
        def fail_second(patrol, key, *args, **kwargs):
            if key.startswith('instruction-gdm:'):
                raise UnsafeContent('synthetic second intent rejected')
            return original(patrol, key, *args, **kwargs)
        with patch.object(Patrol, 'queue', fail_second):
            self.run_once(live=True)
        state = self.state(True)
        self.assertNotIn('instruction:💛20990101-1', state['outbox'])
        self.assertNotIn('instruction-gdm:💛20990101-1', state['outbox'])
        self.assertFalse(any('【りりからのお願い】' in p[1] for p in self.slack.posts))

    def test_pair_planning_failure_cannot_lose_a_report_after_error_save(self):
        self.add_request()
        self.run_once(live=True)
        self.add_report()
        original = Patrol.queue
        def fail_second(patrol, key, *args, **kwargs):
            if key.startswith('reports:') and key.endswith(':group'):
                raise UnsafeContent('synthetic second intent rejected')
            return original(patrol, key, *args, **kwargs)
        with patch.object(Patrol, 'queue', fail_second):
            self.run_once(live=True)
        self.assertFalse(any(i['kind'] == 'reports' for i in self.state(True)['outbox'].values()))
        self.assertNotIn('report-1', self.state(True)['seen_reports'])
        self.run_once(live=True)
        self.assertEqual(len(self.report_posts()), 2)

    def test_manual_source_confirmation_without_ts_does_not_release_other_room(self):
        self.add_request(recipients='@あいす')
        self.slack.error = RemoteError('synthetic unknown', uncertain=True)
        self.run_once(live=True)
        resolve_held(replace(self.cfg, mode='live'), 'instruction', '💛20990101-1', 'posted', clock=self.clock)
        self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(state['outbox']['instruction-gdm:💛20990101-1']['status'], 'queued')
        self.assertEqual(state['instruction_deliveries']['💛20990101-1']['status'], 'held')


if __name__ == '__main__':
    unittest.main()
