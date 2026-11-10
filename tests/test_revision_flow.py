"""v2.2 regression tests. All transports are mocks and artifacts are retained."""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import patrol as cli
from riri.errors import NotionConflict, RemoteError, SafetyError, TimeBudgetExceeded
from riri.lock import patrol_lock
from riri.resolve import resolve_request
from riri.state import StateStore
from test_patrol_flow import FlowTests, request


class RevisionFlowTests(FlowTests):
    # Reuse only fixtures/helpers, not inherited test methods.
    def make_held(self):
        self.add_request()
        self.slack.error = RemoteError('unknown write', uncertain=True)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'held')

    def resolve(self, result):
        return resolve_request(replace(self.cfg, mode='live'), '💛20990101-1', result, clock=self.clock)

    def test_resolve_not_posted_authorizes_one_retry_in_progress(self):
        self.make_held()
        before = len(self.slack.posts)
        self.assertEqual(self.resolve('not_posted')['status'], 'resolved')
        self.assertEqual(len(self.slack.posts), before)
        self.assertTrue(self.state(True)['requests']['💛20990101-1']['retry_permitted_once'])
        self.assertIn('対応中', self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'])
        for _ in range(3): self.run_once(live=True)
        info = self.state(True)['requests']['💛20990101-1']
        self.assertNotIn('retry_permitted_once', info)
        self.assertEqual(info['status'], 'posted')
        self.assertEqual(sum(ch == self.cfg.instruction_channel for ch, _, _ in self.slack.posts), 1)
        self.assertEqual(info['slack_ts'], self.state(True)['outbox']['instruction:💛20990101-1']['ts'])

    def test_resolve_posted_never_sends_instruction(self):
        self.make_held()
        self.resolve('posted')
        for _ in range(3): self.run_once(live=True)
        self.assertFalse([x for x in self.slack.posts if x[0] == self.cfg.instruction_channel])
        info = self.state(True)['requests']['💛20990101-1']
        self.assertTrue(info['manually_confirmed'])
        self.assertIsNone(info['slack_ts'])  # No invented Slack timestamp.
        events = [json.loads(x) for x in (self.root/'logs/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(x['row'][2] == '保留を手動解決' for x in events))

    def test_resolve_rejects_nonheld_unknown_and_repeated(self):
        self.add_request(); self.run_once(live=True)
        original = (self.root/'state/state.json').read_bytes()
        for rid, outcome in [('missing','posted'),('💛20990101-1','posted'),('💛20990101-1','bad')]:
            with self.assertRaises(SafetyError):
                resolve_request(replace(self.cfg, mode='live'), rid, outcome)
            self.assertEqual((self.root/'state/state.json').read_bytes(), original)

    def test_resolve_acquires_same_patrol_lock_before_state_access(self):
        self.make_held()
        original = (self.root/'state/state.json').read_bytes()
        with patrol_lock(self.root):
            with patch.object(StateStore, 'load', side_effect=AssertionError('must not read state')):
                self.assertEqual(self.resolve('not_posted')['status'], 'locked')
        self.assertEqual((self.root/'state/state.json').read_bytes(), original)

    def test_retry_uncertainty_holds_again_without_repeating(self):
        self.make_held(); self.resolve('not_posted')
        self.slack.error = RemoteError('unknown retry', uncertain=True)
        self.run_once(live=True)
        for _ in range(4): self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'held')
        self.assertFalse([x for x in self.slack.posts if x[0] == self.cfg.instruction_channel])

    def test_held_is_manual_even_if_matching_history_appears_later(self):
        self.make_held()
        item = self.state(True)['outbox']['instruction:💛20990101-1']
        self.slack.history.append((item['channel'], item['text'], '99.99'))
        calls = self.slack.find_calls
        self.run_once(live=True)
        self.assertEqual(self.slack.find_calls, calls)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'held')

    def test_ascii_marker_and_returned_timestamp_persist(self):
        self.add_request(); self.run_once(live=True)
        item = self.state(True)['outbox']['instruction:💛20990101-1']
        self.assertRegex(item['marker'], r'^rp:[a-z0-9]{24}$')
        self.assertEqual(item['text'].splitlines()[-1], item['marker'])
        self.assertEqual(item['ts'], self.slack.posts[0][2])

    def test_size_notice_three_hour_batch_weekly_limit_no_deletion(self):
        self.cfg = replace(self.cfg, log_size_notice_bytes=1)
        self.run_once(live=True)
        kept = self.root/'logs/old.log'; kept.write_text('keep me')
        self.clock.advance(10799); self.run_once(live=True)
        self.assertFalse([x for x in self.slack.posts if '整理するか' in x[1]])
        self.clock.advance(1); result = self.run_once(live=True)
        notices = [x for x in self.slack.posts if '整理するか' in x[1]]
        self.assertEqual(len(notices), 1); self.assertIn('3時間ごとのまとめ報告', notices[0][1])
        self.assertLessEqual(result['posts'], 4)
        stamp = self.state(True)['last_log_size_notice_at']
        self.clock.advance(7*86400-1); self.run_once(live=True)
        self.assertEqual(self.state(True)['last_log_size_notice_at'], stamp)
        self.clock.advance(10800); self.run_once(live=True)
        self.assertEqual(sum('整理するか' in x[1] for x in self.slack.posts), 2)
        self.assertEqual(kept.read_text(), 'keep me')

    def test_aged_small_logs_do_not_trigger_size_notice(self):
        self.run_once(live=True); self.clock.advance(365*86400); self.run_once(live=True)
        self.assertFalse([x for x in self.slack.posts if '整理するか' in x[1]])

    def test_size_notice_respects_four_post_limit_and_eventual_delivery(self):
        self.cfg = replace(self.cfg, log_size_notice_bytes=1)
        self.run_once(live=True); self.clock.advance(10800)
        self.add_request(1, recipients='@りり')
        self.add_request(2, recipients='@りり')
        self.add_request(3, recipients='@りり')
        self.add_request(4, recipients='@りり')
        self.assertEqual(self.run_once(live=True)['posts'], 4)
        self.assertIsNone(self.state(True)['last_log_size_notice_at'])
        self.assertTrue(any(x['kind']=='maintenance' and x['status']=='queued' for x in self.state(True)['outbox'].values()))
        self.run_once(live=True)
        self.assertEqual(sum('整理するか' in x[1] for x in self.slack.posts), 1)

    def test_notion_conflict_is_held_and_not_resolved_as_slack(self):
        self.add_request()
        with patch.object(self.notion, 'set_status', side_effect=NotionConflict()):
            result = self.run_once(live=True)
        self.assertEqual(result['error'], 'NotionConflict')
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'held')
        with self.assertRaises(SafetyError): self.resolve('not_posted')
        self.run_once(live=True)
        self.assertFalse([x for x in self.slack.posts if x[0] == self.cfg.instruction_channel])
        self.assertFalse([x for x in self.slack.posts if '直りました' in x[1]])

    def test_unexpected_error_redacted_logged_and_fixed_notification(self):
        self.cfg = replace(self.cfg, slack_token='fake-private-credential')
        notices = []
        with patch.object(self.notion, 'read_blocks', side_effect=RuntimeError('fake-private-credential')):
            result = self.run_once(live=True, unexpected_notifier=lambda: notices.append(1))
        self.assertEqual(result['error'], 'RuntimeError'); self.assertEqual(notices, [1])
        text = (self.root/'logs/patrol.log').read_text()
        self.assertIn('[REDACTED]', text); self.assertNotIn('fake-private-credential', text)
        self.assertFalse(any('fake-private-credential' in p[1] for p in self.slack.posts))

    def test_live_mode_has_one_source_log_line_each_run(self):
        for _ in range(2): self.run_once(live=True)
        self.assertEqual((self.root/'logs/patrol.log').read_text().splitlines().count('本番（.envより）'), 2)

    def test_cli_resolve_constructs_no_transport_and_is_offline(self):
        self.make_held()
        (self.root/'.env').write_text('RIRI_MODE=live\nEXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\n')
        with patch.object(cli, 'Transport', side_effect=AssertionError('no transport')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--root',str(self.root),'--resolve','💛20990101-1','not_posted']),0)
        self.assertTrue(self.state(True)['requests']['💛20990101-1']['retry_permitted_once'])

    def test_cli_final_catch_sanitizes_unknown_setup_error(self):
        with patch.object(cli.Config, 'load', side_effect=RuntimeError('xoxb-fake-test-credential')):
            with patch.object(cli.notify, 'unexpected_error') as notice:
                stderr=io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli.main(['--root',str(self.root)]),2)
                notice.assert_called_once()
        self.assertNotIn('xoxb-',stderr.getvalue())
        self.assertNotIn('xoxb-',(self.root/'logs/patrol.log').read_text())

    def test_old_python_notifies_and_stops_before_main_imports(self):
        with patch('sys.version_info',(3,10,9)), patch('riri.notify.python_too_old') as notice:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(cli.__file__)), run_name='__main__')
        self.assertEqual(stopped.exception.code,2);notice.assert_called_once()

    def test_history_timeout_unexpected_and_stop_fail_durably_held(self):
        self.add_request()
        for error in (TimeBudgetExceeded('deadline'), AttributeError('malformed'), SafetyError('STOP')):
            self.slack.error = RemoteError('post outcome unknown', uncertain=True)
            with patch.object(self.slack, 'find_post', side_effect=error):
                self.run_once(live=True)
            item = self.state(True)['outbox']['instruction:💛20990101-1']
            self.assertEqual(item['status'], 'held')
            self.slack.history.append((item['channel'], item['text'], '99.1'))
            with patch.object(self.slack, 'find_post', side_effect=AssertionError('held must not query')):
                self.run_once(live=True)
            self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'held')
            self.resolve('not_posted')

    def test_hard_crash_during_notion_write_requires_manual_recovery(self):
        self.add_request()
        def crash(block_id, original_text, status, **kwargs):
            self.notion.blocks[self.cfg.requests_page] = [request(status='対応中')]
            raise KeyboardInterrupt('simulated power loss')
        with patch.object(self.notion, 'set_status', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): self.run_once(live=True)
        self.assertIn('notion_write_intent',self.state(True)['requests']['💛20990101-1'])
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'],'held')
        self.assertFalse([x for x in self.slack.posts if x[0] == self.cfg.instruction_channel])

    def test_stop_log_failure_is_caught_without_traceback(self):
        (self.root/'STOP').touch()
        with patch.object(cli.LocalLogs,'operational',side_effect=OSError('full disk')):
            with patch.object(cli.notify,'unexpected_error') as notice:
                stderr=io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli.main(['--root',str(self.root)]),2)
                notice.assert_called_once()
        self.assertNotIn('Traceback',stderr.getvalue())


# Do not duplicate the 25 original FlowTests when unittest discovers this module.
for _name in tuple(dir(FlowTests)):
    if _name.startswith('test_') and _name not in RevisionFlowTests.__dict__:
        setattr(RevisionFlowTests, _name, None)
del FlowTests
