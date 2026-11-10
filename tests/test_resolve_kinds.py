"""Local-only manual resolution tests. Temporary artifacts intentionally retained."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from riri.errors import SafetyError
from riri.eventlog import iso, new_event
from riri.lock import patrol_lock
from riri.patrol import Patrol
from riri.resolve import held_items, resolve_held, resolve_request
from riri.state import StateStore


class ResolveKindsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='riri-resolve-v03-'))
        self.cfg = SimpleNamespace(root=self.root, mode='practice', secrets=())
        self.store = StateStore(self.root, 'practice')
        self.state = self.store.load()
        self.now = datetime(2099, 1, 1, tzinfo=timezone.utc)

    def put_notice(self, kind='notice', **extra):
        item = dict(status='held', kind=kind, marker='rp:' + 'a'*24, text='private body', **extra)
        self.state['outbox']['target'] = item
        return item

    def resolve(self, kind='notice', identifier='target', outcome='posted', **kwargs):
        self.store.save(self.state)
        result = resolve_held(self.cfg, kind, identifier, outcome, clock=lambda: self.now, **kwargs)
        self.state = self.store.load()
        return result

    def report(self):
        return dict(block_id='b1', digest='v1', id='r1', reporter='test', result='完了')

    def test_notice_posted_matches_automatic_ack_side_effects(self):
        variants = [('reports', {'reports': [self.report()]}), ('self', {'request_id': 'r1'}),
                    ('maintenance', {'size_notice': True, 'buffer_notice': True}),
                    ('error', {'error_name': 'NotionError'}), ('recovery', {}), ('notice', {}),
                    ('hold_notice', {})]
        for kind, metadata in variants:
            with self.subTest(kind=kind):
                self.setUp()
                self.state['last_error'] = 'NotionError'
                self.state['errors']['NotionError'] = {'count': 3, 'last_notified_at': 'old'}
                self.put_notice(kind, **metadata)
                automatic = deepcopy(self.state)
                p = object.__new__(Patrol)
                p.state, p.config = automatic, self.cfg
                p.clock, p.started_at = lambda: self.now, self.now
                p.events_added = 0
                p.save = lambda: None
                p.logs = SimpleNamespace(event=lambda e: None)
                p.acknowledge('target', automatic['outbox']['target'], '123.456')
                self.resolve()
                for field in ('seen_reports', 'seen_report_versions', 'riri_notified', 'errors',
                              'last_error', 'last_log_size_notice_at', 'buffer_warning_sent'):
                    self.assertEqual(self.state[field], automatic[field], field)
                manual_events = self.state['log_buffer'][:-1]
                self.assertEqual([e['row'][2:8] for e in manual_events],
                                 [e['row'][2:8] for e in automatic['log_buffer']])
                self.assertIsNone(self.state['outbox']['target']['ts'])
                self.assertTrue(self.state['outbox']['target']['manually_confirmed'])

    def test_cancel_reports_consumes_versions_without_post_events(self):
        self.put_notice('reports', reports=[self.report()])
        self.resolve(outcome='cancel')
        self.assertEqual(self.state['seen_report_versions'], {'b1': ['v1']})
        self.assertEqual(self.state['seen_reports'], {'b1': 'v1'})
        self.assertEqual(len(self.state['log_buffer']), 1)
        self.assertEqual(self.state['outbox']['target']['status'], 'cancelled')

    def test_cancel_self_consumes_notice_but_maintenance_error_recovery_keep_state(self):
        self.put_notice('self', request_id='r1')
        self.resolve(outcome='cancel')
        self.assertEqual(self.state['riri_notified'], ['r1'])
        for kind, extra in [('maintenance', dict(size_notice=True, buffer_notice=True)),
                            ('error', dict(error_name='E')), ('recovery', {})]:
            self.put_notice(kind, **extra)
            self.state['last_error'] = 'E'
            self.resolve(outcome='cancel')
            self.assertEqual(self.state['errors'], {})
            self.assertEqual(self.state['last_error'], 'E')
            self.assertIsNone(self.state['last_log_size_notice_at'])
            self.assertFalse(self.state['buffer_warning_sent'])

    def notion(self, target):
        self.state['requests']['r1'] = {'status': 'held', 'hold_reason': 'notion_conflict',
            'notion_write_intent': {'status': target}, 'snapshot': {'text': 'original body'},
            'block_id': 'original-block', 'draft': 'original draft'}

    def test_notion_checked_preserves_original_and_restores_stage(self):
        for target, expected in [('対応中', 'prepared'), ('完了', 'posted')]:
            self.notion(target)
            self.resolve('notion', 'r1', 'checked')
            info = self.state['requests']['r1']
            self.assertEqual(info['status'], expected)
            self.assertEqual(info['snapshot'], {'text': 'original body'})
            self.assertEqual(info['block_id'], 'original-block')
            self.assertEqual(info['draft'], 'original draft')
            self.assertNotIn('notion_write_intent', info)
            self.assertNotIn('hold_reason', info)

    def test_notion_cancel_cancels_associated_instruction(self):
        self.notion('対応中')
        self.state['outbox']['instruction:r1'] = {'kind': 'instruction', 'status': 'queued'}
        self.resolve('notion', 'r1', 'cancel')
        self.assertEqual(self.state['requests']['r1']['status'], 'cancelled')
        self.assertEqual(self.state['outbox']['instruction:r1']['status'], 'cancelled')

    def test_notion_checked_only_releases_matching_conflict_hold(self):
        self.notion('対応中')
        self.state['outbox']['instruction:r1'] = {'kind': 'instruction', 'status': 'held',
                                                   'hold_reason': 'notion_conflict'}
        self.resolve('notion', 'r1', 'checked')
        self.assertEqual(self.state['outbox']['instruction:r1']['status'], 'blocked')

    def test_log_written_only_removes_batch_events_keeps_durable_audit(self):
        a, b = (new_event(self.now, self.now, x) for x in ('a', 'b'))
        self.state['log_buffer'] = [a, b]
        self.state['log_batch'] = {'status': 'sending', 'batch_id': 'batch1', 'event_ids': [a['event_id']]}
        self.resolve('log', 'batch1', 'written')
        self.assertIsNone(self.state['log_batch'])
        self.assertEqual(self.state['log_buffer'][0], b)
        self.assertEqual(self.state['last_log_flush_at'], iso(self.now))
        self.assertEqual(self.state['resolutions'][0]['outcome'], 'written')

    def test_log_not_written_preserves_all_events(self):
        a = new_event(self.now, self.now, 'a')
        self.state['log_buffer'] = [a]
        self.state['log_batch'] = {'status': 'held', 'batch_id': 'batch1', 'event_ids': [a['event_id']]}
        self.resolve('log', 'batch1', 'not_written')
        self.assertIsNone(self.state['log_batch'])
        self.assertEqual(self.state['log_buffer'][0], a)
        self.assertIsNone(self.state['last_log_flush_at'])

    def test_invalid_nonheld_unknown_ids_and_repeat_do_not_mutate(self):
        self.put_notice()
        self.notion('invalid target')
        self.state['log_batch'] = {'status': 'sending', 'batch_id': 'b', 'event_ids': []}
        self.store.save(self.state)
        for kind, identifier, outcome in [('bad','target','posted'), ('notice','target','not_posted'),
             ('notice','missing','posted'), ('notion','r1','checked'), ('log','wrong','written'),
             ('instruction','target','posted')]:
            before = self.store.path.read_bytes()
            with self.assertRaises(SafetyError):
                resolve_held(self.cfg, kind, identifier, outcome)
            self.assertEqual(before, self.store.path.read_bytes())
        self.resolve()
        before = self.store.path.read_bytes()
        with self.assertRaises(SafetyError):
            resolve_held(self.cfg, 'notice', 'target', 'posted')
        self.assertEqual(before, self.store.path.read_bytes())

    def test_lock_precedes_load(self):
        with patrol_lock(self.root), patch.object(StateStore, 'load', side_effect=AssertionError):
            self.assertEqual(resolve_held(self.cfg, 'notice', 'target', 'posted')['status'], 'locked')

    def test_all_episode_queued_hold_notices_cancel_not_independent_holds(self):
        self.state['requests']['r1'] = {'status': 'held'}
        self.state['outbox']['instruction:r1'] = {'kind': 'instruction', 'status': 'held'}
        for key, status in [('hold:instruction:r1', 'queued'), ('episode:1', 'queued'), ('episode:2', 'held')]:
            self.state['outbox'][key] = {'kind': 'hold_notice', 'held_key': 'instruction:r1', 'status': status}
        self.store.save(self.state)
        resolve_request(self.cfg, 'r1', 'not_posted', clock=lambda: self.now)
        self.state = self.store.load()
        self.assertEqual(self.state['outbox']['hold:instruction:r1']['status'], 'cancelled')
        self.assertEqual(self.state['outbox']['episode:1']['status'], 'cancelled')
        self.assertEqual(self.state['outbox']['episode:2']['status'], 'held')

    def test_status_projection_excludes_bodies_and_untrusted_reason_marker(self):
        self.put_notice(hold_reason='private reason', marker_unused='private')
        self.state['outbox']['target']['marker'] = 'private marker'
        self.notion('対応中')
        self.state['log_batch'] = {'status': 'sending', 'batch_id': 'batch1', 'event_ids': ['secret']}
        before = deepcopy(self.state)
        result = held_items(self.state)
        self.assertEqual(self.state, before)
        self.assertEqual({r['type'] for r in result}, {'notice', 'notion', 'log'})
        encoded = json.dumps(result)
        for forbidden in ('private', 'original', 'secret', 'event_ids', 'snapshot', 'text'):
            self.assertNotIn(forbidden, encoded)

    def test_failed_local_log_append_keeps_atomic_resolution_intent(self):
        self.put_notice('reports', reports=[self.report()])
        with self.assertRaises(OSError):
            self.resolve(logs=SimpleNamespace(event=lambda ev: (_ for _ in ()).throw(OSError())))
        saved = self.store.load()
        self.assertEqual(saved['outbox']['target']['status'], 'posted')
        self.assertEqual(saved['seen_report_versions'], {'b1': ['v1']})
        self.assertEqual(len(saved['log_buffer']), 2)
        self.assertEqual(len(saved['resolutions']), 1)

    def test_unsafe_sources_are_listed_but_cannot_be_manually_released(self):
        self.state['requests']['r1'] = {'status': 'held', 'hold_reason': 'unsafe_content',
                                       'snapshot': {'text': 'private source'}}
        self.state['outbox']['instruction:r1'] = {'kind': 'instruction', 'status': 'held'}
        self.state['quarantined_reports'] = {'b1:v1': {'status': 'held', 'reason': 'unsafe_content',
                                                     'block_id': 'b1', 'digest': 'v1'}}
        items = held_items(self.state)
        self.assertEqual({r['type'] for r in items}, {'request', 'report'})
        self.assertTrue(all(r['allowed_outcomes'] == ['source_edit_required'] for r in items))
        self.assertNotIn('private source', json.dumps(items))
        self.store.save(self.state)
        original = self.store.path.read_bytes()
        for kind, identifier, outcome in [('request', 'r1', 'posted'), ('report', 'b1:v1', 'posted'),
                                          ('instruction', 'r1', 'posted')]:
            with self.assertRaises(SafetyError):
                resolve_held(self.cfg, kind, identifier, outcome)
            self.assertEqual(original, self.store.path.read_bytes())
