"""Core/log isolation regressions using synthetic clients; network forbidden."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
from riri.errors import AccountMismatch, RemoteError, StopRequested, TimeBudgetExceeded
from riri.eventlog import iso, new_event, notion_rows, row_ids
from riri.resolve import resolve_held
from riri.state import StateStore, initial_state


class LogIsolationFlowTests(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state
    add_request = flows.FlowTests.add_request
    add_report = flows.FlowTests.add_report

    def seed(self, *, pending=False, extra=False):
        events = [new_event(self.clock(), self.clock(), 'synthetic log event ' + str(n)) for n in range(2)]
        state = initial_state('live')
        state.update(first_run=False, log_buffer=copy.deepcopy(events),
                     last_log_flush_at=iso(self.clock()), last_summary_at=iso(self.clock()))
        if pending:
            state['log_batch'] = {'batch_id': 'synthetic-batch',
                                  'event_ids': [ev['event_id'] for ev in events], 'status': 'sending'}
        if extra:
            state['log_buffer'].append(new_event(self.clock(), self.clock(), 'later independent event'))
        StateStore(self.root, 'live').save(state)
        return events

    def assert_core_progress(self, result, status='held'):
        self.assertEqual(result['status'], status)
        state = self.state(True)
        self.assertEqual(state['last_run_at'], iso(self.clock()))
        self.assertEqual(state['core_error_serial'], 0)
        self.assertIsNone(state['last_error'])
        self.assertEqual(state['errors'], {})
        self.assertFalse(any(item['kind'] in ('error', 'recovery') for item in state['outbox'].values()))
        return state

    def test_uncertain_append_finishes_core_and_drains_notice_same_run(self):
        events = self.seed()
        self.notion.append_error = RemoteError('synthetic uncertain append', uncertain=True)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            result = self.run_once(live=True, flush=True)
            state = self.assert_core_progress(result)
            self.assertEqual(append.call_count, 1)
        self.assertEqual(set(state['log_batch']['event_ids']), {ev['event_id'] for ev in events})
        notices = [x for k, x in state['outbox'].items() if k.startswith('log-hold:')]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['status'], 'posted')
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(len(state['log_buffer']), 2)

    def test_zero_rows_multi_run_never_reappend_or_multiply_error_events(self):
        self.seed()
        self.notion.append_error = RemoteError('unknown', uncertain=True)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assert_core_progress(self.run_once(live=True, flush=True))
            original = copy.deepcopy(self.state(True)['log_batch'])
            buffer_ids = {e['event_id'] for e in self.state(True)['log_buffer']}
            for _ in range(3):
                self.clock.advance()
                state = self.assert_core_progress(self.run_once(live=True, flush=True))
                self.assertEqual(state['log_batch'], original)
                self.assertEqual({e['event_id'] for e in state['log_buffer']}, buffer_ids)
            self.assertEqual(append.call_count, 1)
        self.assertEqual(len(self.slack.posts), 1)

    def test_partial_rows_reconcile_before_interval_and_never_append(self):
        events = self.seed(pending=True)
        self.notion.rows = notion_rows(events[:1])
        with patch.object(self.notion, 'find_log_table', wraps=self.notion.find_log_table) as read, \
             patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            for _ in range(3):
                self.clock.advance(60)
                self.assert_core_progress(self.run_once(live=True))
            self.assertEqual(read.call_count, 3)
            self.assertEqual(append.call_count, 0)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(len(self.state(True)['log_buffer']), 2)

    def test_unknown_then_all_ids_visible_auto_clears_without_new_append(self):
        events = self.seed()
        self.notion.append_error = RemoteError('unknown', uncertain=True)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assert_core_progress(self.run_once(live=True, flush=True))
            self.clock.advance(60)
            self.assert_core_progress(self.run_once(live=True))
            state = self.state(True)
            extra = new_event(self.clock(), self.clock(), 'later independent event')
            state['log_buffer'].append(extra)
            StateStore(self.root, 'live').save(state)
            self.notion.rows = notion_rows(events)
            self.clock.advance(60)
            final = self.assert_core_progress(self.run_once(live=True, flush=True), 'ok')
            self.assertIsNone(final['log_batch'])
            self.assertEqual([e['event_id'] for e in final['log_buffer']], [extra['event_id']])
            self.assertEqual(final['last_log_flush_at'], iso(self.clock()))
            self.assertEqual(append.call_count, 1)

    def test_pending_full_visible_even_empty_buffer_is_reconciled(self):
        events = self.seed(pending=True)
        state = self.state(True); state['log_buffer'] = []
        StateStore(self.root, 'live').save(state)
        self.notion.rows = notion_rows(events)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.clock.advance(60)
            state = self.assert_core_progress(self.run_once(live=True), 'ok')
            self.assertIsNone(state['log_batch'])
            self.assertEqual(state['last_log_flush_at'], iso(self.clock()))
            self.assertEqual(append.call_count, 0)

    def test_pending_read_failure_retains_batch_and_finishes_core(self):
        self.seed(pending=True)
        original = copy.deepcopy(self.state(True)['log_batch'])
        with patch.object(self.notion, 'find_log_table', side_effect=RemoteError('Incomplete table pagination')), \
             patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            for _ in range(2):
                self.clock.advance(60)
                state = self.assert_core_progress(self.run_once(live=True))
                self.assertEqual(state['log_batch'], original)
            self.assertEqual(append.call_count, 0)
        self.assertEqual(len(self.slack.posts), 1)

    def test_pending_log_notice_unknown_is_not_replayed(self):
        self.seed(pending=True)
        self.slack.error = RemoteError('synthetic post uncertainty', uncertain=True)
        self.assertEqual(self.run_once(live=True)['status'], 'error')
        state = self.state(True)
        serial = state['core_error_serial']
        key = next(k for k in state['outbox'] if k.startswith('log-hold:'))
        self.assertEqual(state['outbox'][key]['status'], 'held')
        before = copy.deepcopy(state['outbox'][key])
        for _ in range(2):
            self.clock.advance(60)
            self.assertEqual(self.run_once(live=True)['status'], 'held')
            self.assertEqual(self.state(True)['last_run_at'], iso(self.clock()))
            self.assertEqual(self.state(True)['core_error_serial'], serial)
        self.assertEqual(self.state(True)['outbox'][key], before)
        self.assertFalse(any('Notionログ' in text for _, text, _ in self.slack.posts))

    def test_definite_append_rejection_isolated_with_backoff_and_stable_notice(self):
        self.seed()
        self.notion.append_error = RemoteError('synthetic prewrite rejection', uncertain=False)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assert_core_progress(self.run_once(live=True, flush=True), 'held')
            state = self.state(True)
            self.assertIsNone(state['log_batch'])
            self.assertEqual(state['last_log_flush_at'], iso(self.clock()))
            keys = set(state['outbox'])
            self.assertEqual(len(keys), 1)
            self.clock.advance(60)
            self.assert_core_progress(self.run_once(live=True), 'held')
            self.assertEqual(append.call_count, 1)
            self.clock.advance(181 * 60)
            self.assert_core_progress(self.run_once(live=True), 'held')
            self.assertEqual(append.call_count, 2)
            self.assertEqual(set(self.state(True)['outbox']), keys)
        self.assertEqual(len(self.slack.posts), 1)

    def test_manual_not_written_only_missing_ids_are_written(self):
        events = self.seed(pending=True)
        self.notion.rows = notion_rows(events[:1])
        self.assert_core_progress(self.run_once(live=True))
        result = resolve_held(replace(self.cfg, mode='live'), 'log', 'synthetic-batch',
                              'not_written', clock=self.clock)
        self.assertEqual(result['status'], 'resolved')
        written = []
        original = self.notion.append_rows
        def capture(table, header, rows, **kwargs):
            written.extend(copy.deepcopy(rows))
            return original(table, header, rows, **kwargs)
        with patch.object(self.notion, 'append_rows', side_effect=capture):
            self.assert_core_progress(self.run_once(live=True, flush=True), 'ok')
        self.assertNotIn(events[0]['event_id'], row_ids(written))
        self.assertIn(events[1]['event_id'], row_ids(written))
        self.assertIsNone(self.state(True)['log_batch'])

    def test_known_beforewrite_stop_keeps_events_without_uncertain_batch(self):
        events = self.seed()
        error = StopRequested('synthetic prewrite stop'); error.not_sent = True
        self.notion.append_error = error
        result = self.run_once(live=True, flush=True)
        self.assertEqual(result['status'], 'stopped')
        state = self.state(True)
        self.assertIsNone(state['log_batch'])
        self.assertIsNone(state['last_run_at'])
        self.assertEqual({e['event_id'] for e in state['log_buffer']}, {e['event_id'] for e in events})
        self.assertEqual(self.slack.posts, [])

    def test_known_beforewrite_deadline_propagates_without_unknown_batch(self):
        self.seed()
        error = TimeBudgetExceeded('synthetic prewrite deadline'); error.not_sent = True
        self.notion.append_error = error
        result = self.run_once(live=True, flush=True)
        self.assertEqual(result['status'], 'deadline')
        state = self.state(True)
        self.assertIsNone(state['log_batch'])
        self.assertIsNone(state['last_run_at'])
        self.assertEqual(state['deadline_state']['consecutive'], 1)
        self.assertEqual(self.slack.posts, [])

    def test_stop_after_actual_write_preserves_batch_for_later_reconciliation(self):
        events = self.seed()
        original = self.notion.append_rows
        def write_then_stop(*args, **kwargs):
            original(*args, **kwargs)
            raise StopRequested('synthetic postwrite stop')
        with patch.object(self.notion, 'append_rows', side_effect=write_then_stop):
            self.assertEqual(self.run_once(live=True, flush=True)['status'], 'stopped')
        self.assertIsNotNone(self.state(True)['log_batch'])
        self.assertIsNone(self.state(True)['last_run_at'])
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.clock.advance(60)
            self.assert_core_progress(self.run_once(live=True), 'ok')
            self.assertEqual(append.call_count, 0)
        self.assertEqual(row_ids(self.notion.rows), {e['event_id'] for e in events})

    def test_deadline_after_actual_write_is_not_swallowed_and_next_run_reconciles(self):
        self.seed()
        original = self.notion.append_rows
        def write_then_deadline(*args, **kwargs):
            original(*args, **kwargs)
            error = TimeBudgetExceeded('synthetic postwrite deadline')
            error.uncertain = True
            raise error
        with patch.object(self.notion, 'append_rows', side_effect=write_then_deadline):
            result = self.run_once(live=True, flush=True)
            self.assertEqual(result['error'], 'TimeBudgetExceeded')
        state = self.state(True)
        self.assertIsNotNone(state['log_batch'])
        self.assertIsNone(state['last_run_at'])
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.clock.advance(60)
            result = self.run_once(live=True)
            self.assertEqual(result['status'], 'ok')
            self.assertIsNone(self.state(True)['log_batch'])
            self.assertEqual(self.state(True)['last_run_at'], iso(self.clock()))
            self.assertEqual(append.call_count, 0)

    def test_pending_log_does_not_block_other_instruction_or_completion(self):
        self.seed(pending=True)
        self.add_request(recipients='@うさ')
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            first = self.run_once(live=True)
            self.assert_core_progress(first)
            self.assertEqual(first['instructions'], 1)
            self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'posted')
            self.add_report(name='うさ')
            self.clock.advance(60)
            self.assert_core_progress(self.run_once(live=True))
            self.assertEqual(self.state(True)['requests']['💛20990101-1']['status'], 'done')
            self.assertTrue(any('→ 完了' in text for _, text, _ in self.slack.posts))
            self.assertEqual(append.call_count, 0)
        self.assertEqual(sum('Notionログ' in text for _, text, _ in self.slack.posts), 1)

    def test_log_find_account_mismatch_is_not_retried_by_slack_fallback(self):
        self.seed(pending=True)
        original = copy.deepcopy(self.state(True)['log_batch'])
        error = AccountMismatch('synthetic Notion identity mismatch')
        with patch.object(self.notion, 'find_log_table', side_effect=error) as find, \
             patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            result = self.run_once(live=True, flush=True)
            self.assertEqual(result['status'], 'account_mismatch')
            self.assertEqual(find.call_count, 1)
            self.assertEqual(append.call_count, 0)
        self.assertTrue(error.log_account_mismatch)
        self.assertEqual(self.state(True)['log_batch'], original)
        self.assertIsNone(self.state(True)['last_run_at'])
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(len(self.notices), 1)

    def test_log_append_account_mismatch_is_not_retried_by_slack_fallback(self):
        self.seed()
        error = AccountMismatch('synthetic Notion append identity mismatch')
        with patch.object(self.notion, 'find_log_table', wraps=self.notion.find_log_table) as find, \
             patch.object(self.notion, 'append_rows', side_effect=error) as append:
            result = self.run_once(live=True, flush=True)
            self.assertEqual(result['status'], 'account_mismatch')
            self.assertEqual(find.call_count, 1)
            self.assertEqual(append.call_count, 1)
        self.assertTrue(error.log_account_mismatch)
        self.assertIsNotNone(self.state(True)['log_batch'])
        self.assertIsNone(self.state(True)['last_run_at'])
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(len(self.notices), 1)

    def test_old_core_error_recovery_waits_through_log_failure_and_backoff(self):
        self.seed()
        state = self.state(True)
        state['last_error'] = 'RemoteError'
        state['core_error_serial'] = 1
        state['errors'] = {'RemoteError': {'count': 1, 'last_notified_at': iso(self.clock())}}
        StateStore(self.root, 'live').save(state)
        self.notion.append_error = RemoteError('known not written', uncertain=False)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assertEqual(self.run_once(live=True, flush=True)['status'], 'held')
            failed = self.state(True)
            self.assertIsNone(failed['log_batch'])
            self.assertEqual(failed['last_error'], 'RemoteError')
            self.assertFalse(any(item['kind'] == 'recovery' for item in failed['outbox'].values()))
            notice = copy.deepcopy(failed['outbox']['log-sync-failed'])
            for _ in range(2):
                self.clock.advance(60)
                self.assertEqual(self.run_once(live=True)['status'], 'held')
                waiting = self.state(True)
                self.assertEqual(waiting['last_run_at'], iso(self.clock()))
                self.assertEqual(waiting['core_error_serial'], 1)
                self.assertFalse(any(item['kind'] == 'recovery' for item in waiting['outbox'].values()))
                self.assertFalse(any('直りました' in text for _, text, _ in self.slack.posts))
            self.assertEqual(append.call_count, 1)
            self.notion.append_error = None
            self.clock.advance(181 * 60)
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            healthy = self.state(True)
            self.assertEqual(append.call_count, 2)
            self.assertFalse(healthy['log_buffer'])
            self.assertEqual(healthy['outbox']['log-sync-failed'], dict(notice, reason='log_sync_confirmed'))
            recovery = [item for item in healthy['outbox'].values() if item['kind'] == 'recovery']
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]['status'], 'queued')
            self.assertFalse(any('直りました' in text for _, text, _ in self.slack.posts))
            self.clock.advance(60)
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.assertIsNone(self.state(True)['last_error'])
            self.assertEqual(sum('直りました' in text for _, text, _ in self.slack.posts), 1)
            self.assertEqual(self.state(True)['outbox']['log-sync-failed'], dict(notice, reason='log_sync_confirmed'))

    def test_confirmed_log_notice_does_not_hold_new_buffer_and_rearms_without_resend(self):
        self.seed()
        self.notion.append_error = RemoteError('synthetic known refusal', uncertain=False)
        self.assertEqual(self.run_once(live=True, flush=True)['status'], 'held')
        original_notice = copy.deepcopy(self.state(True)['outbox']['log-sync-failed'])
        self.assertEqual(original_notice['status'], 'posted')
        self.assertEqual(original_notice['reason'], 'log_sync_pending')
        self.notion.append_error = None
        self.clock.advance(181 * 60)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.assertFalse(self.state(True)['log_buffer'])
        confirmed_notice = dict(original_notice, reason='log_sync_confirmed')
        self.assertEqual(self.state(True)['outbox']['log-sync-failed'], confirmed_notice)
        # A normal new request produces legitimate buffered audit events. The
        # historical posted failure notice is not evidence of a current failure.
        self.add_request(recipients='@うさ')
        self.clock.advance(60)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.assertTrue(self.state(True)['log_buffer'])
            self.clock.advance(60)
            self.assertEqual(self.run_once(live=True)['status'], 'ok')
            self.assertEqual(append.call_count, 0)
        self.assertEqual(self.state(True)['outbox']['log-sync-failed'], confirmed_notice)
        # A later definite failure keeps the old notification intact and creates
        # a separate episode, exactly once (v1.0.4 corrected contract).
        self.notion.append_error = RemoteError('synthetic second refusal', uncertain=False)
        self.clock.advance(181 * 60)
        self.assertEqual(self.run_once(live=True)['status'], 'held')
        self.assertEqual(self.state(True)['outbox']['log-sync-failed'], confirmed_notice)
        self.assertEqual(self.state(True)['outbox']['log-sync-failed:2']['status'], 'posted')
        self.clock.advance(60)
        self.assertEqual(self.run_once(live=True)['status'], 'held')
        marker = original_notice['marker']
        self.assertEqual(sum(marker in text.splitlines() for _, text, _ in self.slack.posts), 1)
        self.assertEqual(self.state(True)['core_error_serial'], 0)

    def assert_atomic_failure_save_and_restart(self, *, already_posted):
        target = iso(self.clock())
        key = 'log-sync-failed:2' if already_posted else 'log-sync-failed'
        snapshots = []
        original_save = StateStore.save
        def observe_then_stop(store, state):
            original_save(store, state)
            snapshot = copy.deepcopy(state)
            snapshots.append(snapshot)
            if snapshot['last_log_flush_at'] == target:
                raise StopRequested('synthetic crash immediately after durable cooldown save')
        self.notion.append_error = RemoteError('synthetic known prewrite refusal', uncertain=False)
        with patch.object(StateStore, 'save', new=observe_then_stop):
            result = self.run_once(live=True, flush=True)
        self.assertEqual(result['status'], 'stopped')
        changed = [s for s in snapshots if s['last_log_flush_at'] == target]
        self.assertEqual(len(changed), 1)
        # Every saved snapshot carrying the new cooldown already carries the
        # failure marker. This checks disk snapshots, not just final RAM state.
        for state in changed:
            notice = state['outbox'].get(key)
            self.assertIsNotNone(notice)
            self.assertEqual(notice['reason'], 'log_sync_pending')
            self.assertEqual(notice['status'], 'queued')
        durable = self.state(True)
        self.assertEqual(durable['last_log_flush_at'], target)
        self.assertEqual(durable['outbox'][key]['reason'], 'log_sync_pending')
        marker = durable['outbox'][key]['marker']
        self.clock.advance(60)
        with patch.object(self.notion, 'append_rows', wraps=self.notion.append_rows) as append:
            self.assertEqual(self.run_once(live=True)['status'], 'held')
            self.assertEqual(append.call_count, 0)
        self.assertEqual(self.state(True)['last_run_at'], iso(self.clock()))
        self.assertEqual(sum(marker in text.splitlines() for _, text, _ in self.slack.posts), 1)

    def test_first_failure_cooldown_and_pending_notice_saved_atomically(self):
        self.seed()
        self.clock.advance(60)
        self.assert_atomic_failure_save_and_restart(already_posted=False)

    def test_recurring_failure_cooldown_and_rearmed_notice_saved_atomically(self):
        self.seed()
        self.notion.append_error = RemoteError('synthetic initial refusal', uncertain=False)
        self.assertEqual(self.run_once(live=True, flush=True)['status'], 'held')
        self.notion.append_error = None
        self.clock.advance(181 * 60)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        confirmed = copy.deepcopy(self.state(True)['outbox']['log-sync-failed'])
        self.assertEqual(confirmed['reason'], 'log_sync_confirmed')
        self.add_request(recipients='@うさ')
        self.clock.advance(60)
        self.assertEqual(self.run_once(live=True)['status'], 'ok')
        self.clock.advance(181 * 60)
        self.assert_atomic_failure_save_and_restart(already_posted=True)
        self.assertEqual(self.state(True)['outbox']['log-sync-failed'],
                         confirmed)
