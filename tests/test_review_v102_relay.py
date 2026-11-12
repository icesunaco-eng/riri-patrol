"""Synthetic offline relay isolation; no patrol run or remote calls."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.errors import AccountMismatch, SafetyError, StopRequested, UnsafeContent
from riri.patrol import Patrol
from riri.relay import queue_instruction_relay
from riri.resolve import held_items
from riri.state import initial_state, StateStore

RID = '💛20990101-102'
RELAY = 'instruction-gdm:' + RID
WARNING = 'relay-unsafe:' + RID


def marker(key):
    return 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]


class RelayIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        cfg = Config(Path(self.temp.name), mode='live', expected_bot_id='BTEST',
                     expected_user_id='UTESTRIRI', ice_mention_user_id='U0TESTICE000',
                     instruction_channel='CTESTINSTRUCTION', report_channel='CTESTREPORT')
        self.p = Patrol(cfg, None, None, None,
                        clock=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.p.state = initial_state('live')
        self.p.state['requests'][RID] = {'status': 'posted', 'unchanged': 'synthetic'}
        self.source = {'status': 'posted', 'channel': cfg.instruction_channel,
                       'marker': marker('instruction:' + RID),
                       'text': 'synthetic instruction\n' + marker('instruction:' + RID)}
        self.p.state['outbox']['instruction:' + RID] = self.source

    def invoke(self):
        return queue_instruction_relay(self.p, RID, self.source)

    def unsafe(self):
        self.source['text'] = '<@UUNTRUSTED> synthetic-private-body\n' + self.source['marker']

    def test_valid_source_exact_copy_and_deterministic_marker(self):
        item = self.invoke()
        self.assertEqual(item['status'], 'queued')
        self.assertEqual(item['kind'], 'instruction_relay')
        self.assertEqual(item['text'], '<@U0TESTICE000>\nsynthetic instruction\n' + marker(RELAY))
        self.assertNotIn(WARNING, self.p.state['outbox'])

    def test_unsafe_source_held_without_rewriting_source_or_request(self):
        self.unsafe()
        source, request = copy.deepcopy(self.source), copy.deepcopy(self.p.state['requests'][RID])
        item = self.invoke()
        self.assertEqual((item['status'], item['hold_reason']), ('held', 'unsafe_content'))
        self.assertEqual(item['marker'], marker(RELAY))
        self.assertEqual(item['text'].splitlines()[-1], marker(RELAY))
        self.assertEqual(self.source, source)
        self.assertEqual(self.p.state['requests'][RID], request)
        self.assertEqual(self.p.state['log_buffer'], [])
        new = json.dumps({RELAY: item, WARNING: self.p.state['outbox'][WARNING]})
        self.assertNotIn('synthetic-private-body', new)
        self.assertNotIn('UUNTRUSTED', new)

    def test_warning_once_across_repeated_calls_and_reload(self):
        self.unsafe(); self.invoke()
        before = copy.deepcopy(self.p.state['outbox'])
        for _ in range(3): self.invoke()
        self.p.state = StateStore(self.p.config.root, 'live').load()
        self.invoke()
        self.assertEqual(self.p.state['outbox'], before)
        self.assertTrue(self.p.state['outbox'][WARNING]['text'].startswith('<@U0TESTICE000>\n'))

    def test_crash_after_hold_saved_recovers_only_warning(self):
        self.unsafe()
        original = self.p.queue
        def fail_warning(key, *args, **kwargs):
            if key == WARNING: raise StopRequested('synthetic interruption')
            return original(key, *args, **kwargs)
        with patch.object(self.p, 'queue', side_effect=fail_warning):
            with self.assertRaises(StopRequested): self.invoke()
        self.assertEqual(self.p.state['outbox'][RELAY]['status'], 'held')
        self.assertNotIn(WARNING, self.p.state['outbox'])
        self.p.state = self.p.store.load()
        self.invoke()
        self.assertIn(WARNING, self.p.state['outbox'])

    def test_all_existing_relay_statuses_are_immutable(self):
        for status in ('queued', 'sending', 'posted', 'held', 'cancelled', 'blocked'):
            with self.subTest(status=status):
                prior = {'status': status, 'kind': 'notice', 'text': 'legacy exact text',
                         'marker': 'legacy-marker', 'retry_permitted_once': True}
                self.p.state['outbox'][RELAY] = copy.deepcopy(prior)
                self.unsafe()
                self.assertEqual(self.invoke(), prior)
                self.assertNotIn(WARNING, self.p.state['outbox'])

    def test_uncertain_warning_never_requeued_or_rewritten(self):
        self.unsafe(); self.invoke()
        for status in ('sending', 'held', 'posted', 'cancelled'):
            self.p.state['outbox'][WARNING]['status'] = status
            prior = copy.deepcopy(self.p.state['outbox'][WARNING])
            self.invoke()
            self.assertEqual(self.p.state['outbox'][WARNING], prior)

    def test_missing_or_forged_source_marker_is_isolated(self):
        for changed in ({'marker': 'rp:forged'}, {'text': 'no final marker'},
                        {'text': None}, {'text': 'body\n' + self.source['marker'] + '\n'}):
            with self.subTest(changed=changed):
                old = copy.deepcopy(self.source)
                self.source.update(changed)
                self.assertEqual(self.invoke()['hold_reason'], 'unsafe_content')
                self.p.state['outbox'].pop(RELAY); self.p.state['outbox'].pop(WARNING)
                self.source.clear(); self.source.update(old)

    def test_unposted_or_wrong_destination_never_queued(self):
        for status in ('queued', 'sending', 'held', 'cancelled'):
            self.source['status'] = status
            self.assertIsNone(self.invoke())
        self.source['status'] = 'posted'; self.source['channel'] = 'COTHER'
        self.assertIsNone(self.invoke())
        self.assertNotIn(RELAY, self.p.state['outbox'])

    def test_only_unsafe_content_is_caught(self):
        for exception in (AccountMismatch('synthetic'), SafetyError('synthetic'), StopRequested('synthetic'), OSError('synthetic')):
            with self.subTest(exception=type(exception).__name__):
                with patch.object(self.p, 'queue', side_effect=exception):
                    with self.assertRaises(type(exception)): self.invoke()
                self.assertNotIn(RELAY, self.p.state['outbox'])

    def test_other_work_can_queue_after_quarantine(self):
        self.unsafe(); self.invoke()
        self.p.queue('independent', self.p.config.report_channel, 'unrelated safe notice')
        self.assertEqual(self.p.state['outbox']['independent']['status'], 'queued')

    def test_unsafe_relay_resolution_is_cancel_only(self):
        self.unsafe(); self.invoke()
        pending = held_items(self.p.state)
        match = [x for x in pending if x.get('key') == RELAY or x.get('id') == RELAY]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]['allowed_outcomes'], ['cancel'])
