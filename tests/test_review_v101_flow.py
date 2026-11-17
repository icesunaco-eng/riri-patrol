"""v1.0.1 integration regressions, synthetic sources and no network."""
import copy
from dataclasses import replace
from unittest.mock import patch
import unittest

import test_patrol_flow as flows
from riri.errors import RemoteError
from riri.llm import DraftService
from riri.patrol import Patrol
from riri.state import StateStore, initial_state

ID = '💛20990101-1'
PREFIX = '<@U0TESTICE000>\n'


class V101FlowTests(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def patrol(self):
        cfg = replace(self.cfg, mode='live')
        p = Patrol(cfg, self.slack, self.notion, DraftService(flows.OfflineTransport(), enabled=False), clock=self.clock)
        p.state = self.state(True)
        p.started_at = self.clock()
        p.started_mono = p.monotonic()
        p.post_count = p.instruction_count = p.events_added = 0
        return p

    def instruction_posts(self):
        return [p for p in self.slack.posts if '【りりからのお願い】' in p[1]]

    def test_ice_and_mini_get_exactly_one_relay_each_request(self):
        self.add_request(recipients='@あいす @みに')
        result = self.run_once(live=True)
        state = self.state(True)
        self.assertEqual(result['instructions'], 1)
        self.assertEqual(result['posts'], 3)
        source = state['outbox']['instruction:' + ID]
        relay = state['outbox']['instruction-gdm:' + ID]
        self.assertEqual(relay['kind'], 'instruction_relay')
        self.assertEqual(relay['request_id'], ID)
        self.assertEqual(relay['text'].splitlines()[:-1], source['text'].splitlines()[:-1])
        self.assertEqual(source['channel'], self.cfg.instruction_channel)
        self.assertEqual(relay['channel'], self.cfg.report_channel)
        self.assertTrue(source['text'].startswith(PREFIX))
        self.assertTrue(relay['text'].startswith(PREFIX))
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.instruction_posts()), 2)
        self.assertEqual(sum('みにへの依頼' in x[1] for x in self.slack.posts), 1)

    def test_karen_noa_usa_only_do_not_relay(self):
        self.add_request(recipients='@かれん @のあ @うさ')
        result = self.run_once(live=True)
        self.assertEqual((result['instructions'], result['posts']), (1, 1))
        self.assertNotIn('instruction-gdm:' + ID, self.state(True)['outbox'])

    def test_post_budget_defers_relay_without_consuming_instruction_budget(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        first = self.run_once(live=True)
        self.assertEqual((first['posts'], first['instructions']), (1, 1))
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'queued')
        second = self.run_once(live=True)
        self.assertEqual((second['posts'], second['instructions']), (1, 0))
        self.assertEqual(len(self.instruction_posts()), 2)

    def test_two_requests_still_at_most_one_original_per_run(self):
        self.add_request(recipients='@あいす')
        self.add_request(2, recipients='@あいす')
        for _ in range(2):
            result = self.run_once(live=True)
            self.assertEqual(result['instructions'], 1)
            self.assertEqual(result['posts'], 2)
        self.assertEqual(len(self.instruction_posts()), 4)

    def test_definitive_rejection_has_no_relay_until_later_success(self):
        self.add_request(recipients='@あいす')
        self.slack.error = RemoteError('known not sent', uncertain=False)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'queued')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'posted')

    def test_uncertain_source_is_held_and_never_relayed(self):
        self.add_request(recipients='@あいす')
        self.slack.error = RemoteError('outcome unknown', uncertain=True)
        self.run_once(live=True)
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:' + ID]['status'], 'held')
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'queued')

    def test_relay_timeout_does_not_repost_source_or_relay(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        self.slack.error = RemoteError('relay unknown', uncertain=True)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'held')
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.instruction_posts()), 1)
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'posted')

    def test_relay_that_reached_slack_is_reconciled_not_repeated(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        self.run_once(live=True)
        original = self.slack.post
        def post(*args, **kw):
            original(*args, **kw)
            raise RemoteError('lost response', uncertain=True)
        with patch.object(self.slack, 'post', side_effect=post): self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'posted')
        self.run_once(live=True)
        self.assertEqual(len(self.instruction_posts()), 2)

    def test_crash_after_source_ack_creates_relay_on_next_run(self):
        self.add_request(recipients='@あいす')
        with patch.object(Patrol, 'ensure_followups', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:' + ID]['status'], 'posted')
        self.assertEqual(self.state(True)['outbox']['instruction-gdm:' + ID]['status'], 'queued')
        self.run_once(live=True)
        self.assertEqual(len(self.instruction_posts()), 2)

    def test_relay_uses_durable_original_not_mutated_draft(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients='@あいす')
        with patch.object(Patrol, 'ensure_followups'): self.run_once(live=True)
        state = self.state(True)
        state['requests'][ID]['draft'] = 'changed draft must not be forwarded'
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertNotIn('changed draft', self.state(True)['outbox']['instruction-gdm:' + ID]['text'])
        self.assertIn('手順を確認する', self.slack.posts[-1][1])

    def test_historical_posted_source_is_not_retroactively_relayed(self):
        self.add_request(recipients='@あいす')
        with patch.object(Patrol, 'ensure_followups'): self.run_once(live=True)
        state = self.state(True)
        # Model an immutable pre-v2 posted source, with no relay opt-in.
        source = state['outbox']['instruction:' + ID]
        source.pop('delivery_policy')
        source['text'] = source['text'][len(PREFIX):]
        state['outbox'].pop('instruction-gdm:' + ID)
        state['instruction_deliveries'].pop(ID)
        old = copy.deepcopy(state['outbox']['instruction:' + ID])
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertNotIn('instruction-gdm:' + ID, self.state(True)['outbox'])
        self.assertEqual(self.state(True)['outbox']['instruction:' + ID], old)

    def test_existing_outbox_every_status_not_reformatted_by_queue(self):
        p = self.patrol()
        for status in ('queued', 'sending', 'held', 'posted', 'cancelled', 'blocked'):
            old = {'status': status, 'channel': self.cfg.report_channel, 'text': 'old text\nrp:old',
                   'marker': 'rp:old', 'kind': 'notice'}
            p.state['outbox'][status] = copy.deepcopy(old)
            self.assertEqual(p.queue(status, self.cfg.report_channel, 'new text'), old)

    def test_all_new_report_channel_notices_have_one_controlled_prefix(self):
        p = self.patrol()
        for kind in ('notice', 'self', 'reports', 'hold_notice', 'unsafe_notice', 'error',
                     'recovery', 'deadline_recovery', 'maintenance', 'noa_notice', 'source_notice'):
            item = p.queue(kind, self.cfg.report_channel, 'synthetic safe body', kind)
            self.assertTrue(item['text'].startswith(PREFIX))
            self.assertEqual(item['text'].count('<@'), 1)
            self.assertEqual(item['text'].splitlines()[-1], item['marker'])

    def test_legacy_notice_is_sent_unchanged_without_added_mention(self):
        self.run_once(live=True)
        state = self.state(True)
        old = 'legacy completion（old summary）\nrp:legacy'
        state['outbox']['legacy'] = {'status': 'queued', 'channel': self.cfg.report_channel,
            'text': old, 'marker': 'rp:legacy', 'kind': 'notice'}
        StateStore(self.root, 'live').save(state)
        self.run_once(live=True)
        self.assertEqual(self.slack.posts[-1][1], old)
        self.assertEqual(self.state(True)['outbox']['legacy']['text'], old)

    def test_invalid_mention_identity_stops_before_auth_or_source_reads(self):
        for value in ('', 'not-id', self.cfg.expected_user_id):
            self.cfg = replace(self.cfg, ice_mention_user_id=value)
            self.assertEqual(self.run_once(live=True)['status'], 'account_mismatch')
        self.assertEqual(self.slack.auth_calls, 0)
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.notion.reads, [])

    def test_snapshot_page_overrides_changed_current_page(self):
        self.add_request()
        self.run_once(live=True)
        state = self.state(True)
        state['requests'][ID]['snapshot']['fields']['案件ページ'] = '保存ページ https://notion.so/snapshot'
        StateStore(self.root, 'live').save(state)
        self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'] += '\n案件ページ：現在ページ https://notion.so/current'
        self.add_report()
        self.run_once(live=True)
        texts = [p[1] for p in self.slack.posts if '→ 完了' in p[1]]
        self.assertEqual(texts, [])
        self.assertTrue(self.state(True)['report_requester_waiting'])
        self.assertNotIn('report-1', self.state(True)['seen_reports'])

    def test_current_page_fallback_when_no_saved_snapshot(self):
        self.run_once(live=True)
        self.add_request(status='完了')
        self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'] += '\n案件ページ：https://app.notion.com/p/current'
        self.add_report()
        self.run_once(live=True)
        self.assertIn('案件ページ：（https://app.notion.com/p/current）', self.slack.posts[-1][1])

    def test_missing_request_field_has_explicit_missing_label(self):
        self.run_once(live=True)
        self.add_report()
        self.run_once(live=True)
        self.assertFalse(any('→ 完了' in post[1] for post in self.slack.posts))
        self.assertTrue(self.state(True)['report_requester_waiting'])

    def test_unsafe_project_page_quarantines_report_without_echo(self):
        self.run_once(live=True)
        self.add_request(status='完了')
        self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'] += '\n案件ページ：https://example.invalid/private-title?unsafe=%3C%40UUNTRUSTED%3E'
        self.add_report()
        self.run_once(live=True)
        self.assertTrue(self.state(True)['quarantined_reports'])
        self.assertFalse(any('→ 完了' in p[1] for p in self.slack.posts))
        self.assertFalse(any('private-title' in p[1] for p in self.slack.posts))
        self.assertTrue(self.slack.posts[-1][1].startswith(PREFIX))

    def test_source_configured_mention_is_rejected_before_system_prefix(self):
        self.add_request(task='<@U0TESTICE000> malicious mention')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'held')
        self.assertFalse(self.instruction_posts())
        self.assertTrue(all(x[1].count('<@') == 1 for x in self.slack.posts))

    def test_hidden_report_summary_mentions_quarantine_without_completion(self):
        self.add_request()
        self.run_once(live=True)
        self.add_report()
        blk = self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]][0]
        blk['paragraph']['rich_text'][0]['text']['content'] += '<@U0TESTICE000>'
        self.run_once(live=True)
        self.assertTrue(self.state(True)['quarantined_reports'])
        self.assertEqual(self.state(True)['requests'][ID]['status'], 'posted')

    def test_stop_prevents_all_auth_and_new_message_intents(self):
        (self.root / 'STOP').touch()
        self.add_request(recipients='@あいす')
        self.assertEqual(self.run_once(live=True)['status'], 'stopped')
        self.assertEqual(self.slack.auth_calls, 0)
        self.assertEqual(self.state(True)['outbox'], {})
        self.assertFalse(self.cfg.noa_wake_enabled)

    def test_identical_instruction_report_channels_stop_before_auth(self):
        self.cfg = replace(self.cfg, report_channel=self.cfg.instruction_channel)
        self.assertEqual(self.run_once(live=True)['status'], 'account_mismatch')
        self.assertEqual(self.slack.auth_calls, 0)
        self.assertFalse(self.slack.posts)

    def test_page_correction_alone_does_not_release_existing_quarantine(self):
        self.run_once(live=True)
        self.add_request(status='対応中')
        blk = self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']
        blk['content'] += '\n案件ページ：https://example.invalid/page?unsafe=%3C%40UUNTRUSTED%3E'
        self.add_report()
        self.run_once(live=True)
        blk['content'] = blk['content'].replace('https://example.invalid/page?unsafe=%3C%40UUNTRUSTED%3E','https://notion.so/fixed')
        self.run_once(live=True)
        self.assertFalse(any('→ 完了' in p[1] for p in self.slack.posts))
        # A corrected report is a new version, never a silent reset of the held record.
        r = self.notion.blocks[self.cfg.pages['うさ'].split('/')[-1]][0]['paragraph']['rich_text'][0]['text']
        r['content'] += '。案件ページの訂正を確認'
        self.run_once(live=True)
        self.assertIn('案件ページ：（https://notion.so/fixed）', self.slack.posts[-1][1])
        self.assertTrue(all(x['status']=='superseded' for x in self.state(True)['quarantined_reports'].values()))

    def test_overlong_page_is_held_without_truncation_or_echo(self):
        self.run_once(live=True)
        self.add_request(status='完了')
        self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'] += '\n案件ページ：' + '長'*2800 + ' https://notion.so/test'
        self.add_report()
        self.run_once(live=True)
        self.assertTrue(self.state(True)['quarantined_reports'])
        self.assertFalse(any('長'*10 in p[1] for p in self.slack.posts))
