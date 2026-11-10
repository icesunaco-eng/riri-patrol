"""Practice-only virtual transitions use real Notion validation, never remote PATCH."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from riri.errors import NotionConflict, SafetyError
from riri.eventlog import LocalLogs
from riri.notion import NotionClient
from riri.writer import PracticeWriter, LiveWriter
from test_api import Fake, code_response, page

ORIGINAL = '【りりへの依頼】\n状態：未着手\n手順：\n1. 内容を確認\n2. 報告する\n'
V1 = '2099-01-01T00:00:00Z'
V2 = '2099-01-01T00:00:01Z'


def block(text=ORIGINAL, version=V1):
    return dict(code_response('b', text), last_edited_time=version)


def request():
    return SimpleNamespace(id='req', block_id='b', text=ORIGINAL, last_edited_time=V1)


class PracticeVirtualTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='riri-practice-virtual-'))
        self.logs = LocalLogs(self.root)
        self.clock = lambda: datetime(2099, 1, 1, tzinfo=timezone.utc)

    def writer(self, responses, cls=PracticeWriter):
        self.transport = Fake(*responses)
        self.notion = NotionClient('ntn_fake', self.transport)
        return cls(None, self.notion, self.logs, self.clock)

    def entries(self, filename):
        return [json.loads(line) for line in (self.root / 'logs' / filename).read_text().splitlines()]

    def test_advance_then_complete_with_unchanged_remote_and_zero_patch(self):
        writer = self.writer([block(), block()])
        writer.set_status(request(), '対応中')
        # The engine persists this only after the preceding preview succeeded.
        writer.set_status(request(), '完了', practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET', 'GET'])
        previews = self.entries('practice.jsonl')
        self.assertEqual([p['status'] for p in previews], ['対応中', '完了'])
        after = self.entries('notion-audit.jsonl')[-1]
        self.assertEqual(after['phase'], 'practice_validated')
        self.assertEqual(after['before_text'], ORIGINAL)
        self.assertEqual(after['expected_text'], ORIGINAL.replace('未着手', '完了'))
        self.assertEqual(after['practice_status'], '対応中')

    def test_persisted_context_works_with_fresh_client(self):
        first = self.writer([block()])
        first.set_status(request(), '対応中')
        second = self.writer([block()])
        second.set_status(request(), '完了', practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_without_virtual_context_direct_completion_is_rejected(self):
        writer = self.writer([block()])
        with self.assertRaises(SafetyError):
            writer.set_status(request(), '完了')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_changed_non_status_text_cannot_use_virtual_context(self):
        writer = self.writer([block(ORIGINAL.replace('報告する', '送信する'))])
        with self.assertRaises(NotionConflict):
            writer.set_status(request(), '完了', practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_changed_remote_status_also_fails_full_snapshot_comparison(self):
        writer = self.writer([block(ORIGINAL.replace('未着手', '対応中'))])
        with self.assertRaises(NotionConflict):
            writer.set_status(request(), '完了', practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_changed_edit_version_rejected_even_with_identical_text(self):
        writer = self.writer([block(version=V2)])
        with self.assertRaises(NotionConflict):
            writer.set_status(request(), '完了', practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_cached_edit_version_comparison_remains_active(self):
        writer = self.writer([page([block()]), block(version=V2)])
        self.notion.read_blocks('page')
        with self.assertRaises(NotionConflict):
            self.notion.set_status('b', ORIGINAL, '完了', dry_run=True, practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET', 'GET'])

    def test_live_writer_rejects_any_virtual_context_before_read(self):
        for value in ('未着手', '対応中', '完了', ''):
            writer = self.writer([], LiveWriter)
            with self.assertRaises(SafetyError):
                writer.set_status(request(), '完了', practice_status=value)
            self.assertEqual(self.transport.calls, [])

    def test_direct_live_client_cannot_bypass_virtual_guard(self):
        self.writer([])
        with self.assertRaises(SafetyError):
            self.notion.set_status('b', ORIGINAL, '完了', practice_status='対応中')
        self.assertEqual(self.transport.calls, [])

    def test_invalid_virtual_state_rejected_before_read(self):
        for value in ('posted', '', True, 42):
            writer = self.writer([])
            with self.assertRaises(SafetyError):
                writer.set_status(request(), '完了', practice_status=value)
            self.assertEqual(self.transport.calls, [])

    def test_virtual_state_cannot_regress_behind_real_state(self):
        text = ORIGINAL.replace('未着手', '完了')
        self.writer([block(text)])
        with self.assertRaises(SafetyError):
            self.notion.set_status('b', text, '完了', dry_run=True, practice_status='対応中')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])

    def test_completed_virtual_state_cannot_go_back_to_in_progress(self):
        writer = self.writer([block()])
        with self.assertRaises(SafetyError):
            writer.set_status(request(), '対応中', practice_status='完了')
        self.assertEqual([c[0] for c in self.transport.calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
