"""Pinned v0.8/current state round trips; synthetic data only, no operational run.

The historical reader is shipped as inert source, not obtained from Git at runtime.
Its relative imports reuse dependencies only after exact historical hash checks.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest

from riri.errors import SafetyError
from riri.state import StateStore, initial_state
from riri.noa import CHANNEL, migrate_state

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'v08_state'
AT = '2099-01-01T00:00:00+00:00'
LATER = '2099-01-01T00:05:00+00:00'


def historical_reader():
    manifest = json.loads((FIXTURE / 'manifest.json').read_text(encoding='utf-8'))
    source = (FIXTURE / manifest['fixture']).read_bytes()
    expected = manifest['files']['riri/state.py']
    if hashlib.sha256(source).hexdigest() != expected['sha256']:
        raise AssertionError('Historical reader fixture changed')
    blob = hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    if blob != expected['git_blob']:
        raise AssertionError('Historical reader Git blob mismatch')
    for name, record in manifest['files'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != record['sha256']:
            raise AssertionError('Historical dependency or current schema changed: ' + name)
    module = types.ModuleType('riri._compat_v08_state')
    module.__package__ = 'riri'
    exec(compile(source, 'pinned-v0.8/state.py', 'exec'), module.__dict__)
    return module.StateStore


def rich_state(mode):
    """All records are invented; equality is the preservation oracle."""
    state = initial_state(mode)
    migrate_state(state['noa_wake'])
    state.update(first_run=False, last_run_at=LATER, core_error_serial=3,
                 last_error='RemoteError', last_log_flush_at=AT, last_summary_at=AT,
                 last_account_mismatch_event_at=AT, last_log_size_notice_at=AT,
                 buffer_warning_sent=True)
    state['deadline_state'] = {'consecutive': 3, 'episode': 2, 'first_at': AT,
                               'alert_key': 'deadline-alert:2'}
    for index, status in enumerate(('prepared', 'status_changing', 'queued', 'sending', 'posted', 'held', 'done', 'skipped')):
        key = 'synthetic-request-' + str(index)
        state['requests'][key] = {
            'status': status, 'block_id': 'synthetic-block-' + str(index),
            'snapshot': {'id': key, 'requester': 'offline fixture', 'text': 'synthetic only'},
            'first_seen': AT, 'source_status_observed_at': LATER,
            'status_intent': {'from': '未着手', 'to': '対応中'},
            'retry_consumed_at': AT, 'retry_permitted_once': status == 'queued',
            'posted_event': status == 'posted'}
    for index, status in enumerate(('queued', 'sending', 'posted', 'held', 'cancelled', 'blocked')):
        key = 'synthetic-outbox-' + str(index)
        state['outbox'][key] = {
            'kind': 'instruction', 'status': status, 'request_id': 'synthetic-request-' + str(index),
            'marker': 'rp:synthetic-' + str(index), 'text': 'synthetic only',
            'channel': 'CSYNTHETIC', 'created_at': AT, 'attempted_at': LATER,
            'hold_episode': 2, 'retry_consumed_at': AT, 'retry_permitted_once': status == 'queued'}
    state['outbox']['deadline-alert:2'] = {'kind': 'deadline_alert', 'status': 'held',
        'marker': 'rp:synthetic-alert', 'channel': 'CSYNTHETIC', 'text': 'synthetic only'}
    state['outbox']['deadline-recovery:1'] = {'kind': 'deadline_recovery', 'status': 'sending',
        'marker': 'rp:synthetic-recovery', 'channel': 'CSYNTHETIC', 'text': 'synthetic only',
        'alert_key': 'deadline-alert:1', 'recovery_error_serial': 2}
    state['seen_reports'] = {'synthetic-report': 'b' * 64}
    state['seen_report_versions'] = {'synthetic-report': ['a' * 64, 'b' * 64]}
    state['riri_notified'] = ['synthetic-request-7']
    state['incomplete_notified'] = {'synthetic-incomplete': AT}
    state['errors'] = {'RemoteError': {'last_notified_at': AT, 'count': 3}}
    state['retention_notified'] = ['synthetic-retention']
    state['quarantined_reports'] = {'synthetic-unsafe': {'status': 'held', 'digest': 'c' * 64}}
    state['resolutions'] = [{'target': 'synthetic-old-hold', 'outcome': 'posted', 'at': AT}]
    state['log_buffer'] = [{'event_id': 'synthetic-event', 'at': AT, 'started_at': AT,
                            'action': 'synthetic fixture', 'result': '一部完了', 'memo': 'not a real event'}]
    state['log_batch'] = {'batch_id': 'synthetic-batch', 'event_ids': ['synthetic-event'], 'status': 'sending'}
    noa = state['noa_wake']
    noa.update(enabled=True, origin_cutoff='4070908800.000000', cutoff='4070908800.000000',
               watermark='4070908900.000000', off_at='4070908799.000000', last_run_at=LATER,
               failure_reason='authentication', failure_serial=3)
    for index, (status, attempts) in enumerate((('queued', 0), ('retry', 1), ('sending', 2),
            ('done', 2), ('held', 2), ('rejected', 1), ('simulated', 0), ('reviewed', 2))):
        ts = '40709088' + str(10 + index) + '.000001'
        noa['items'][CHANNEL + ':' + ts] = {
            'status': status, 'channel': CHANNEL, 'ts': ts, 'attempt_count': attempts,
            'source_digest': ('%064x' % (index + 1)), 'source_user': 'USYNTHETIC',
            'retry_at': LATER, 'reason': 'synthetic only', 'attempted_at': AT,
            'retry_consumed_at': AT, 'hold_episode': 2}
        noa['attempts'].extend([AT] * attempts)
    retired = CHANNEL + ':4070908820.000001'
    noa['items'][retired] = {'status': 'rejected', 'channel': CHANNEL, 'ts': '4070908820.000001',
        'attempt_count': 0, 'source_digest': 'd' * 64, 'source_user': 'USYNTHETIC',
        'reason': 'disabled_before_send'}
    noa['stale_windows'] = [
        {'start': '4070908800.000000', 'end': '4070908830.000000', 'retired': [retired], 'count': 2},
        {'start': '4070908840.000000', 'end': None, 'retired': [], 'count': None}]
    noa['failure_episodes'] = {
        'configuration': {'serial': 1, 'success_since': AT, 'last_success': LATER},
        'authentication': {'serial': 2, 'success_since': None, 'last_success': None},
        'component_unavailable': {'serial': 3, 'success_since': AT, 'last_success': LATER}}
    return state


class StateCompatibilityV08V10(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.LegacyStore = historical_reader()

    def test_pinned_reader_is_distinct_and_exact_original(self):
        self.assertIsNot(self.LegacyStore, StateStore)
        self.assertEqual(self.LegacyStore.__module__, 'riri._compat_v08_state')
        self.assertEqual(json.loads((FIXTURE / 'manifest.json').read_text())['source_version'], 'v0.8')

    def test_bidirectional_roundtrip_preserves_every_field_and_local_logs(self):
        for mode in ('live', 'practice'):
            for first, second in ((self.LegacyStore, StateStore), (StateStore, self.LegacyStore)):
                with self.subTest(mode=mode, first=first.__module__), tempfile.TemporaryDirectory() as root:
                    root = Path(root)
                    logs = root / 'logs'
                    logs.mkdir()
                    sentinels = {'events.jsonl': b'{"synthetic":"append-only event"}\n',
                                 'practice.jsonl': b'{"synthetic":"preview marker"}\n'}
                    for name, contents in sentinels.items(): (logs / name).write_bytes(contents)
                    original = rich_state(mode)
                    writer, reader = first(root, mode), second(root, mode)
                    writer.save(copy.deepcopy(original))
                    written = writer.path.read_bytes()
                    loaded = reader.load()
                    self.assertEqual(loaded, original)
                    self.assertEqual(writer.path.read_bytes(), written, 'read must not rewrite state')
                    reader.save(loaded)
                    self.assertEqual(writer.load(), original)
                    self.assertEqual(reader.path.read_bytes(), written, 'same schema round trip must be byte-stable')
                    for name, contents in sentinels.items(): self.assertEqual((logs / name).read_bytes(), contents)
                    self.assertEqual(len(loaded['noa_wake']['attempts']), 10)
                    self.assertEqual(loaded['noa_wake']['items'], original['noa_wake']['items'])
                    self.assertEqual(set(p.name for p in (root / 'state').iterdir()), {writer.path.name})

    def test_repeat_cross_version_cycles_do_not_consume_retry_or_reset_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            original = rich_state('live')
            StateStore(root, 'live').save(original)
            for reader in (self.LegacyStore, StateStore) * 3:
                store = reader(root, 'live')
                state = store.load()
                store.save(state)
                self.assertEqual(state, original)

    def test_both_modes_stay_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            live, practice = rich_state('live'), rich_state('practice')
            practice['first_run'] = True
            self.LegacyStore(root, 'live').save(live)
            StateStore(root, 'practice').save(practice)
            self.assertEqual(StateStore(root, 'live').load(), live)
            self.assertEqual(self.LegacyStore(root, 'practice').load(), practice)

    def test_supported_additive_migration_equal_and_preserves_uncertainty(self):
        original = rich_state('live')
        del original['core_error_serial']
        # Older additive inputs lack recovery serial metadata, not uncertainty records.
        del original['outbox']['deadline-recovery:1']['recovery_error_serial']
        noa = original['noa_wake']
        for key in ('last_run_at', 'stale_windows', 'failure_episodes'): del noa[key]
        outcomes = []
        for reader in (self.LegacyStore, StateStore):
            with tempfile.TemporaryDirectory() as root:
                store = reader(root, 'live')
                store.save(copy.deepcopy(original))
                loaded = store.load()
                self.assertEqual(loaded['outbox'], original['outbox'])
                self.assertEqual(loaded['noa_wake']['items'], original['noa_wake']['items'])
                self.assertEqual(loaded['noa_wake']['attempts'], original['noa_wake']['attempts'])
                self.assertEqual(loaded['log_batch'], original['log_batch'])
                self.assertIsNone(loaded['noa_wake']['last_run_at'])
                outcomes.append(loaded)
        self.assertEqual(*outcomes)

    def test_corrupt_records_fail_closed_without_modifying_state_or_logs(self):
        def mutate(path, value):
            state = rich_state('live')
            target = state
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            return json.dumps(state, ensure_ascii=False).encode()
        invalid = [b'{not JSON', mutate(['version'], 999), mutate(['mode'], 'practice'),
            mutate(['core_error_serial'], -1), mutate(['deadline_state', 'first_at'], 'bad-time'),
            mutate(['outbox', 'synthetic-outbox-1', 'status'], 'unsent-guessed'),
            mutate(['outbox', 'deadline-recovery:1', 'recovery_error_serial'], 4),
            mutate(['noa_wake', 'attempts'], []), mutate(['noa_wake', 'last_run_at'], '2099-01-01'),
            mutate(['noa_wake', 'failure_episodes', 'authentication', 'serial'], 4),
            mutate(['noa_wake', 'stale_windows', 0, 'count'], 0)]
        for reader in (self.LegacyStore, StateStore):
            for number, raw in enumerate(invalid):
                with self.subTest(reader=reader.__module__, corruption=number), tempfile.TemporaryDirectory() as root:
                    store = reader(root, 'live')
                    store.path.parent.mkdir(parents=True)
                    store.path.write_bytes(raw)
                    log = Path(root) / 'untouched-synthetic-log.jsonl'
                    log.write_bytes(b'synthetic append-only sentinel\n')
                    with self.assertRaises(SafetyError): store.load()
                    self.assertEqual(store.path.read_bytes(), raw)
                    self.assertEqual(log.read_bytes(), b'synthetic append-only sentinel\n')

    def test_missing_state_has_same_initial_semantics_not_backup_restore(self):
        for mode in ('live', 'practice'):
            with tempfile.TemporaryDirectory() as root:
                backup = Path(root) / 'old-backup.json'
                backup.write_text(json.dumps(rich_state(mode)))
                self.assertEqual(self.LegacyStore(root, mode).load(), StateStore(root, mode).load())
                self.assertFalse(StateStore(root, mode).path.exists())
                self.assertEqual(json.loads(backup.read_text()), rich_state(mode))
