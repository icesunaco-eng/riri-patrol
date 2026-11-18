"""Optional session URLs through real patrol flow; synthetic offline clients only."""
import copy
import hashlib
import unittest
from dataclasses import replace

import test_patrol_flow as flows
from riri.parse import parse_reports, replace_status
from riri.state import StateStore
from riri.eventlog import iso
from riri.llm import DraftService
from riri.patrol import Patrol

RID = '💛20990101-1'


class OptionalWorkURLFlows(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request

    def put(self, url=None, *, block_id='optional-report', result='完了', extra=''):
        text = ('【完了報告】\n依頼ID：' + RID + '\n報告した子：のあ\n結果：' + result
            + '\nやったこと：模擬作業を確認した')
        if url is not None:
            text += '\n作業場所URL：' + url
        text += extra
        block = flows.block(text, block_id, 'paragraph')
        self.notion.blocks[self.cfg.pages['のあ'].split('/')[-1]].insert(0, block)
        return block

    def start_request(self):
        self.add_request(recipients='@のあ')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')

    def test_new_report_without_work_session_url_completes(self):
        self.start_request()
        self.put(url=None)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')

    def assert_dual_report_posted_once(self, block_id='optional-report'):
        state = self.state(True)
        items = [item for item in state['outbox'].values() if item.get('kind') == 'reports'
            and any(r['block_id'] == block_id for r in item.get('reports', []))]
        self.assertEqual(len(items), 2)
        self.assertEqual({item['channel'] for item in items},
            {self.cfg.instruction_channel, self.cfg.report_channel})
        self.assertTrue(all(item['status'] == 'posted' and item['ts'] for item in items))
        self.assertEqual(len({item['marker'] for item in items}), 2)
        self.assertIn(block_id, state['seen_reports'])
        return copy.deepcopy(items)

    def finish_with_optional_value(self, value):
        self.start_request()
        source = self.put(value)
        original = copy.deepcopy(source)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        self.assertIn(('status', 'request-1', '完了'), self.notion.writes)
        receipts = self.assert_dual_report_posted_once()
        self.assertEqual(source, original)
        posts = copy.deepcopy(self.slack.posts)
        for _ in range(3):
            self.clock.advance()
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.slack.posts, posts)
        self.assertEqual(self.assert_dual_report_posted_once(), receipts)
        self.assertFalse(any(item.get('kind') == 'work_url_notice'
            for item in self.state(True)['outbox'].values()))

    def test_missing_url_keeps_two_report_receipts_and_does_not_replay(self):
        self.finish_with_optional_value(None)

    def test_blank_url_completes_and_does_not_replay(self):
        self.finish_with_optional_value('')

    def test_unavailable_local_session_url_completes_and_does_not_replay(self):
        self.finish_with_optional_value('未取得（ローカルCLIでURL未提供）')

    def test_supported_supplied_url_is_still_shared_in_both_rooms(self):
        self.start_request()
        url = 'https://claude.ai/chat/22222222-2222-4222-8222-222222222222'
        self.put(url)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        items = self.assert_dual_report_posted_once()
        self.assertTrue(all(url in item['text'] for item in items))

    def legacy_waiting_report(self):
        """Make real delivery receipts, then seed the precise old URL gate state."""
        self.start_request()
        source = self.put(None)
        self.run_once(live=True)
        report = parse_reports([source], expected_reporter='のあ')[0]
        state = self.state(True)
        self.assertEqual(state['seen_reports'][report.block_id], report.digest)
        self.assert_dual_report_posted_once()
        state['requests'][RID]['status'] = 'posted'
        current = self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']
        current['content'] = replace_status(current['content'], '対応中')
        key = report.block_id + ':' + report.digest
        state['work_url_policy'] = {'version': 1, 'activated_at': iso(self.clock()),
            'legacy_versions': [], 'pending_versions': {key: {
                'block_id': report.block_id, 'digest': report.digest, 'request_id': RID,
                'reporter': 'のあ', 'status': 'pending', 'reason': 'missing', 'at': iso(self.clock())}}}
        StateStore(self.root, 'live').save(state)
        return source, report, key

    def test_upgrade_releases_previous_pending_report_without_edit_or_repost(self):
        source, report, key = self.legacy_waiting_report()
        original = copy.deepcopy(source)
        previous = self.state(True)
        receipts = self.assert_dual_report_posted_once()
        posts = copy.deepcopy(self.slack.posts)
        for _ in range(2):
            self.clock.advance()
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
        state = self.state(True)
        self.assertEqual(state['requests'][RID]['status'], 'done')
        self.assertEqual(state['seen_report_versions'][report.block_id],
            previous['seen_report_versions'][report.block_id])
        self.assertEqual(self.slack.posts, posts)
        self.assertEqual(self.assert_dual_report_posted_once(), receipts)
        self.assertEqual(source, original)
        self.assertNotEqual(state['work_url_policy']['pending_versions'][key]['status'], 'pending')
        for field in ('block_id', 'digest', 'request_id', 'reporter', 'reason', 'at'):
            self.assertEqual(state['work_url_policy']['pending_versions'][key][field],
                previous['work_url_policy']['pending_versions'][key][field])

    def test_upgrade_cancels_known_unsent_obsolete_notice_without_rewriting_it(self):
        _, _, key = self.legacy_waiting_report()
        state = self.state(True)
        notice_key = 'work-url:' + hashlib.sha256(key.encode()).hexdigest()
        marker = 'rp:' + hashlib.sha256(notice_key.encode()).hexdigest()[:24]
        notice = {'status': 'queued', 'channel': self.cfg.report_channel,
            'kind': 'work_url_notice', 'request_id': RID, 'reporter': 'のあ',
            'created_at': iso(self.clock()), 'marker': marker,
            'text': '<@' + self.cfg.ice_mention_user_id + '>\n'
                + '作業場所URLの追記待ち（旧方針の模擬通知）\n' + marker}
        state['outbox'][notice_key] = copy.deepcopy(notice)
        StateStore(self.root, 'live').save(state)
        posts = copy.deepcopy(self.slack.posts)
        self.clock.advance()
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        state = self.state(True)
        self.assertEqual(state['requests'][RID]['status'], 'done')
        self.assertEqual(state['outbox'][notice_key]['status'], 'cancelled')
        self.assertEqual(state['outbox'][notice_key]['text'], notice['text'])
        self.assertEqual(state['outbox'][notice_key]['marker'], marker)
        self.assertEqual(self.slack.posts, posts)

    def test_upgrade_preserves_existing_held_and_posted_notice_evidence(self):
        self.legacy_waiting_report()
        state = self.state(True)
        saved = {}
        for status in ('held', 'posted'):
            key = 'work-url:synthetic-' + status
            marker = 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]
            item = {'status': status, 'channel': self.cfg.report_channel, 'kind': 'work_url_notice',
                'request_id': RID, 'reporter': 'のあ', 'created_at': iso(self.clock()),
                'text': '<@' + self.cfg.ice_mention_user_id + '>\n旧通知の証拠\n' + marker,
                'marker': marker}
            if status == 'posted':
                item.update(ts='1795000000.000001', posted_at=iso(self.clock()))
            else:
                item.update(held_at=iso(self.clock()), hold_episode=1)
            state['outbox'][key] = copy.deepcopy(item)
            saved[key] = item
        StateStore(self.root, 'live').save(state)
        posts = copy.deepcopy(self.slack.posts)
        self.clock.advance()
        # A prior unknown notification send remains held globally; it does not
        # invalidate this report outcome or prevent its request from completing.
        self.assertEqual(self.run_once(live=True)['status'], 'held')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        for key, item in saved.items():
            self.assertEqual(self.state(True)['outbox'][key], item)
        self.assertEqual(self.slack.posts, posts)

    def test_optional_policy_does_not_cancel_or_resend_a_notice_already_sending(self):
        source, report, _ = self.legacy_waiting_report()
        state = self.state(True)
        key = 'work-url:synthetic-sending'
        marker = 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]
        notice = {'status': 'sending', 'channel': self.cfg.report_channel, 'kind': 'work_url_notice',
            'request_id': RID, 'reporter': 'のあ', 'created_at': iso(self.clock()),
            'attempted_at': iso(self.clock()), 'marker': marker,
            'text': '<@' + self.cfg.ice_mention_user_id + '>\n送信結果未確定の旧通知\n' + marker}
        state['outbox'][key] = copy.deepcopy(notice)
        StateStore(self.root, 'live').save(state)
        cfg = replace(self.cfg, mode='live')
        draft = DraftService(flows.OfflineTransport(), enabled=False, report_pages=cfg.pages)
        patrol = Patrol(cfg, self.slack, self.notion, draft, clock=self.clock)
        patrol.state = copy.deepcopy(state)
        posts = copy.deepcopy(self.slack.posts)
        report_before = copy.deepcopy(source)
        # Exercise the migration boundary without unrelated in-flight-send
        # reconciliation, which may legitimately change sending to held/posted.
        patrol.apply_work_url_policy([report])
        self.assertEqual(patrol.state['outbox'][key], notice)
        self.assertEqual(self.state(True)['outbox'][key], notice)
        self.assertEqual(self.slack.posts, posts)
        self.assertEqual(source, report_before)

    def test_corrupt_migration_history_stops_without_fabricating_an_optional_baseline(self):
        self.start_request()
        self.put(None)
        state = self.state(True)
        state['work_url_policy']['pending_versions'] = {'corrupt-version': {'status': 'unknown'}}
        before = copy.deepcopy(state['work_url_policy'])
        StateStore(self.root, 'live').save(state)
        result = self.run_once(live=True)
        self.assertEqual(result['error'], 'SafetyError')
        self.assertEqual(self.state(True)['work_url_policy'], before)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')
        self.assertNotIn(('status', 'request-1', '完了'), self.notion.writes)

    def test_optional_url_does_not_upgrade_partial_work_result_to_complete(self):
        self.start_request()
        self.put(None, result='一部完了')
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')
        self.assertNotIn(('status', 'request-1', '完了'), self.notion.writes)
        self.assert_dual_report_posted_once()

    def older_seen_completion_cannot_override_newer_missing_url_result(self, result):
        self.add_request(recipients='@のあ @かれん')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        previous = self.put('https://claude.ai/chat/22222222-2222-4222-8222-222222222222',
            block_id='older-complete', result='完了')
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')
        original = copy.deepcopy(previous)
        old_receipts = self.assert_dual_report_posted_once('older-complete')
        self.put(None, block_id='newer-result', result=result)
        other_page = self.cfg.pages['かれん'].split('/')[-1]
        self.notion.blocks[other_page].insert(0, flows.report(name='かれん', id='karen-complete'))
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')
        self.assertNotIn(('status', 'request-1', '完了'), self.notion.writes)
        self.assert_dual_report_posted_once('newer-result')
        self.assert_dual_report_posted_once('karen-complete')
        self.assertEqual(self.assert_dual_report_posted_once('older-complete'), old_receipts)
        self.assertEqual(previous, original)
        posts = copy.deepcopy(self.slack.posts)
        for _ in range(2):
            self.clock.advance()
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.assertEqual(self.state(True)['requests'][RID]['status'], 'posted')
        self.assertEqual(self.slack.posts, posts)

    def test_newer_missing_url_partial_result_wins_over_older_seen_complete(self):
        self.older_seen_completion_cannot_override_newer_missing_url_result('一部完了')

    def test_newer_missing_url_failed_result_wins_over_older_seen_complete(self):
        self.older_seen_completion_cannot_override_newer_missing_url_result('できなかった')

    def test_unsupported_url_is_omitted_but_does_not_invalidate_completion(self):
        self.start_request()
        value = 'https://github.com/example/repo'
        self.put(value)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        items = self.assert_dual_report_posted_once()
        self.assertTrue(all(value not in item['text'] for item in items))
        self.assertTrue(all('→ 完了' in item['text'] for item in items))

    def test_unsafe_url_is_omitted_but_does_not_invalidate_completion(self):
        self.start_request()
        value = 'https://user:pass@chatgpt.com/c/11111111-1111-4111-8111-111111111111'
        self.put(value)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        items = self.assert_dual_report_posted_once()
        self.assertTrue(all(value not in item['text'] and 'user:pass' not in item['text'] for item in items))
        self.assertFalse(any('user:pass' in text for _, text, _ in self.slack.posts))

    def test_duplicate_url_fields_omit_only_url_and_still_complete(self):
        self.start_request()
        first = 'https://chatgpt.com/c/11111111-1111-4111-8111-111111111111'
        second = 'https://claude.ai/chat/22222222-2222-4222-8222-222222222222'
        self.put(first, extra='\n作業場所URL：' + second)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests'][RID]['status'], 'done')
        items = self.assert_dual_report_posted_once()
        self.assertTrue(all(first not in item['text'] and second not in item['text'] for item in items))


if __name__ == '__main__':
    unittest.main()
