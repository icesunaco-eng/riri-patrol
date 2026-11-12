"""Actual v1.0 readers, unchanged persisted bytes, no network or live data."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest

from riri.errors import UnsafeContent
from riri.state import StateStore, initial_state
from tests.test_review_v10_state_compatibility import historical_reader, rich_state

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/v10_state'


class V101CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURE / 'manifest.json').read_text())
        cls.OldStore = historical_reader()  # v1.0 uses the exact same source.
        for name, record in cls.manifest['files'].items():
            raw = ((FIXTURE / 'compose.py.txt') if name == 'riri/compose.py' else ROOT / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record['sha256']:
                raise AssertionError('Pinned v1.0 source differs: ' + name)
            if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() != record['git_blob']:
                raise AssertionError('Pinned v1.0 blob differs')
        source = (FIXTURE / cls.manifest['state_fixture']).read_bytes()
        if hashlib.sha256(source).hexdigest() != cls.manifest['files']['riri/state.py']['sha256']:
            raise AssertionError('v1.0 state fixture differs')

    def fixture(self, mode):
        data = rich_state(mode)
        for n, status in enumerate(('queued', 'sending', 'held', 'posted')):
            key = 'instruction-gdm:synthetic-' + str(n)
            data['outbox'][key] = {'kind':'notice', 'status':status, 'request_id':'synthetic-'+str(n),
                'text':'<@U0TESTICE000>\nsynthetic instruction\nrp:synthetic-relay-'+str(n),
                'channel':'CSYNTHETICREPORT', 'marker':'rp:synthetic-relay-'+str(n)}
        data['outbox']['synthetic-outbox-0']['relay_to_report'] = True
        return data

    def test_v10_reader_proven_identical_without_new_required_state_keys(self):
        self.assertEqual(self.manifest['source_version'], 'v1.0')
        self.assertEqual(set(initial_state('live')), set(self.OldStore('/tmp/unused-v101-fixture','live').load()))
        self.assertFalse(any('mention' in x for x in initial_state('live')))

    def test_v10_to_v101_preserves_legacy_outbox_bytes_all_statuses(self):
        for mode in ('live','practice'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                original = rich_state(mode)
                old, new = self.OldStore(root, mode), StateStore(root, mode)
                old.save(original)
                raw = old.path.read_bytes()
                loaded = new.load()
                self.assertEqual(loaded, original)
                new.save(loaded)
                self.assertEqual(raw, new.path.read_bytes())

    def test_v101_to_v10_keeps_mentions_relays_uncertainty_and_logs(self):
        for mode in ('live','practice'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                original = self.fixture(mode)
                old, new = self.OldStore(root, mode), StateStore(root, mode)
                logs = Path(root)/'logs'; logs.mkdir()
                (logs/'events.jsonl').write_bytes(b'{"synthetic":"keep"}\n')
                new.save(original); raw = new.path.read_bytes()
                for reader in (old, new, old, new):
                    loaded = reader.load()
                    self.assertEqual(loaded, original)
                    reader.save(loaded)
                    self.assertEqual(reader.path.read_bytes(), raw)
                self.assertEqual((logs/'events.jsonl').read_bytes(), b'{"synthetic":"keep"}\n')

    def test_rollback_is_schema_not_legacy_mention_send_compatibility(self):
        module = types.ModuleType('riri._compat_v10_compose'); module.__package__ = 'riri'
        source = (FIXTURE/'compose.py.txt').read_bytes()
        exec(compile(source, 'pinned-v1.0/compose.py', 'exec'), module.__dict__)
        module.assert_safe('legacy body\nrp:synthetic')
        with self.assertRaises(UnsafeContent):
            module.assert_safe('<@U0TESTICE000>\nnew body\nrp:synthetic')
        # This is intentional: never remove the mention/marker or rewrite outbox to bypass it.
