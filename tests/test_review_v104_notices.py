"""Log notice episodes only. Synthetic transports; no token/auth changes."""
import copy
import unittest
from unittest.mock import patch
from riri.errors import RemoteError
from riri.eventlog import iso, new_event, notion_rows
from riri.state import StateStore
import test_review_v103_flow as fixtures

class LogNoticeEpisodeTests(unittest.TestCase):
    setUp = fixtures.LogIsolationFlowTests.setUp
    tearDown = fixtures.LogIsolationFlowTests.tearDown
    run_once = fixtures.LogIsolationFlowTests.run_once
    state = fixtures.LogIsolationFlowTests.state
    seed = fixtures.LogIsolationFlowTests.seed

    def fail(self):
        self.notion.append_error = RemoteError('synthetic known refusal', uncertain=False)
        self.assertEqual(self.run_once(live=True, flush=True)['status'], 'held')

    def recover(self):
        self.notion.append_error = None
        self.clock.advance(181 * 60)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')

    def add_event(self):
        state = self.state(True)
        state['log_buffer'].append(new_event(self.clock(), self.clock(), 'synthetic event'))
        StateStore(self.root, 'live').save(state)

    def test_three_episodes_each_notify_once_with_unique_markers(self):
        self.seed()
        for n in range(3):
            if n:
                self.add_event()
            self.fail()
            for _ in range(3):
                self.clock.advance(60)
                self.assertEqual(self.run_once(live=True)['status'], 'held')
            self.recover()
        notices = {k:v for k,v in self.state(True)['outbox'].items() if k.startswith('log-sync-failed')}
        self.assertEqual(set(notices), {'log-sync-failed', 'log-sync-failed:2', 'log-sync-failed:3'})
        self.assertEqual(len({x['marker'] for x in notices.values()}), 3)
        for item in notices.values():
            self.assertEqual(item['reason'], 'log_sync_confirmed')
            self.assertEqual(sum(item['marker'] in t.splitlines() for _,t,_ in self.slack.posts), 1)

    def test_continuous_failure_after_cooldown_does_not_create_episode(self):
        self.seed()
        self.fail()
        old = copy.deepcopy(self.state(True)['outbox'])
        self.clock.advance(181 * 60)
        self.fail()
        self.assertEqual(self.state(True)['outbox'], old)
        self.assertEqual(len(self.slack.posts), 1)

    def test_held_notification_is_not_resent_or_replaced(self):
        self.seed()
        self.fail()
        state = self.state(True)
        state['outbox']['log-sync-failed']['status'] = 'held'
        StateStore(self.root, 'live').save(state)
        self.clock.advance(181 * 60)
        self.fail()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.state(True)['outbox']['log-sync-failed']['status'], 'held')

    def test_new_hold_text_describes_readonly_next_patrol(self):
        self.seed()
        self.notion.append_error = RemoteError('synthetic unknown', uncertain=True)
        self.assertEqual(self.run_once(live=True, flush=True)['status'], 'held')
        text = self.slack.posts[0][1]
        self.assertIn('次の見回りで照合します', text)
        self.assertNotIn('たかむねの確認を待ちます', text)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.clock.advance(60)
            self.assertEqual(self.run_once(live=True)['status'], 'held')
            self.assertEqual(append.call_count, 0)
        self.assertEqual(len(self.slack.posts), 1)

    def test_old_hold_text_and_marker_not_rewritten(self):
        self.seed(pending=True)
        state = self.state(True)
        state['outbox']['log-hold:synthetic-batch'] = {
            'status': 'posted', 'channel': self.notion.config.report_channel,
            'text': 'old wording\nrp:oldmarker', 'marker': 'rp:oldmarker',
            'kind': 'notice', 'created_at': iso(self.clock()), 'ts': '1.000'}
        original = copy.deepcopy(state['outbox']['log-hold:synthetic-batch'])
        StateStore(self.root, 'live').save(state)
        self.assertEqual(self.run_once(live=True)['status'], 'held')
        self.assertEqual(self.state(True)['outbox']['log-hold:synthetic-batch'], original)
        self.assertEqual(self.slack.posts, [])

    def test_recovery_does_not_add_log_events_or_immediate_recovery_notice(self):
        events = self.seed(pending=True)
        self.assertEqual(self.run_once(live=True)['status'], 'held')
        self.notion.rows.extend(notion_rows(events))
        self.clock.advance(60)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertEqual(self.state(True)['log_buffer'], [])
        self.assertEqual(len(self.slack.posts), 1)
