"""Y-01/Y-02 record compatibility with pinned v1.0.1 reader, offline."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from riri.state import StateStore, initial_state
from tests.test_review_v10_state_compatibility import historical_reader, rich_state

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'tests/fixtures/v101_state/manifest.json'


class V102Compatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest=json.loads(MANIFEST.read_text())
        # v1.0.4 changes config/Slack only: pin their historical bytes,
        # while persistence, Noa and all other dependencies remain exact.
        for name, info in cls.manifest['unchanged_files'].items():
            b=((MANIFEST.parent/(Path(name).name+".txt")) if name in {"riri/config.py", "riri/slack.py"} else ROOT/name).read_bytes()
            if hashlib.sha256(b).hexdigest()!=info['sha256']:
                raise AssertionError('v1.0.1 pinned dependency changed: '+name)
            if hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()!=info['git_blob']:
                raise AssertionError('v1.0.1 blob mismatch')
        b=(MANIFEST.parent/cls.manifest['state_fixture']).read_bytes()
        if hashlib.sha256(b).hexdigest()!=cls.manifest['unchanged_files']['riri/state.py']['sha256']:
            raise AssertionError('Historical state reader differs from v1.0.1')
        cls.OldStore=historical_reader()

    def test_pinned_v101_state_noa_identity_and_transport_unchanged(self):
        self.assertEqual(self.manifest['source_version'],'v1.0.1')
        self.assertEqual(len(self.manifest['unchanged_files']),6)
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.OldStore(root,'live').load(),initial_state('live'))

    def test_v101_v102_bidirectional_relay_hold_warning_and_quotas(self):
        for mode in ('live','practice'):
            for reverse in (False,True):
                with self.subTest(mode=mode,reverse=reverse),tempfile.TemporaryDirectory() as root:
                    data=rich_state(mode)
                    data['outbox']['instruction-gdm:synthetic']={
                        'status':'held','kind':'notice','hold_reason':'unsafe_content',
                        'text':'<@U0TESTICE000>\nsafe placeholder\nrp:synthetic-relay',
                        'channel':'CSYNTHETICREPORT','marker':'rp:synthetic-relay',
                        'request_id':'synthetic','hold_episode':1}
                    data['outbox']['relay-unsafe:synthetic']={
                        'status':'sending','kind':'hold_notice','held_key':'instruction-gdm:synthetic',
                        'text':'<@U0TESTICE000>\nfixed warning\nrp:synthetic-warning',
                        'channel':'CSYNTHETICREPORT','marker':'rp:synthetic-warning','hold_episode':1}
                    readers=[self.OldStore(root,mode),StateStore(root,mode)]
                    if reverse:readers.reverse()
                    readers[0].save(data); raw=readers[0].path.read_bytes()
                    log=Path(root)/'logs';log.mkdir();(log/'events.jsonl').write_bytes(b'synthetic sentinel\n')
                    for reader in readers*2:
                        loaded=reader.load();self.assertEqual(loaded,data)
                        reader.save(loaded);self.assertEqual(reader.path.read_bytes(),raw)
                    self.assertEqual((log/'events.jsonl').read_bytes(),b'synthetic sentinel\n')

    def test_old_quarantine_and_all_outbox_states_remain_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            data=rich_state('live')
            data['quarantined_reports']['legacy-format-or-danger']={
                'status':'held','reason':'unsafe_content','block_id':'synthetic','digest':'a'*64}
            old,new=self.OldStore(root,'live'),StateStore(root,'live')
            old.save(data);raw=old.path.read_bytes()
            snapshot=copy.deepcopy(data)
            new.save(new.load())
            self.assertEqual(old.load(),snapshot)
            self.assertEqual(new.path.read_bytes(),raw)
