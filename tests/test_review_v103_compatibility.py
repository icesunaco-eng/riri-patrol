"""v1.0.2 exact persistence compatibility, never live state or network."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from riri.state import StateStore
from tests.test_review_v10_state_compatibility import historical_reader, rich_state

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/fixtures/v102_state/manifest.json'


class V103Compatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text())
        # v1.0.4 changes config/Slack only: pin their historical bytes,
        # while persistence, Noa and all other dependencies remain exact.
        for name, info in cls.manifest['unchanged_files'].items():
            value = ((MANIFEST.parent/(Path(name).name+".txt")) if name in {"riri/config.py", "riri/slack.py", "riri/writer.py"} else ROOT/name).read_bytes()
            if hashlib.sha256(value).hexdigest() != info['sha256']:
                raise AssertionError('Pinned v1.0.2 source changed: '+name)
            if hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest() != info['git_blob']:
                raise AssertionError('Pinned v1.0.2 blob changed: '+name)
        fixture = (MANIFEST.parent/cls.manifest['state_fixture']).read_bytes()
        if hashlib.sha256(fixture).hexdigest() != cls.manifest['unchanged_files']['riri/state.py']['sha256']:
            raise AssertionError('Original state reader does not match v1.0.2')
        cls.OldStore = historical_reader()

    def test_pinned_v102_state_noa_and_safety_sources_are_unchanged(self):
        self.assertEqual(self.manifest['source_version'], 'v1.0.2')
        self.assertEqual(len(self.manifest['unchanged_files']), 9)
        self.assertEqual(self.manifest['published_commit'], '5d384fd764f763e2ec521ba0985bb6c93de1689e')

    def test_uncertain_log_batch_notices_and_all_other_state_roundtrip_both_directions(self):
        for mode in ('live', 'practice'):
            for reverse in (False, True):
                with self.subTest(mode=mode, reverse=reverse), tempfile.TemporaryDirectory() as root:
                    state = rich_state(mode)
                    for status in ('queued', 'sending', 'held', 'posted', 'cancelled'):
                        state['outbox']['log-hold:synthetic-'+status] = {
                            'kind': 'notice', 'status': status, 'channel': 'CSYNTHETIC',
                            'text': 'synthetic hold notice\nrp:synthetic-'+status,
                            'marker': 'rp:synthetic-'+status}
                    state['outbox']['log-sync-failed'] = {
                        'kind': 'notice', 'status': 'posted', 'channel': 'CSYNTHETIC',
                        'text': 'synthetic log notice\nrp:synthetic-log', 'marker': 'rp:synthetic-log',
                        'reason': 'log_sync_pending' if reverse else 'log_sync_confirmed'}
                    readers = [self.OldStore(root, mode), StateStore(root, mode)]
                    if reverse: readers.reverse()
                    readers[0].save(state)
                    raw = readers[0].path.read_bytes()
                    log = Path(root)/'logs'; log.mkdir()
                    (log/'events.jsonl').write_bytes(b'synthetic sentinel\n')
                    for reader in readers*2:
                        self.assertEqual(reader.load(), state)
                        reader.save(reader.load())
                        self.assertEqual(reader.path.read_bytes(), raw)
                    self.assertEqual((log/'events.jsonl').read_bytes(), b'synthetic sentinel\n')
