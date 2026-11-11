"""T-05: an uncertain notice without a request ID has a meaningful subject."""
from datetime import datetime, timezone
import unittest

from riri.eventlog import new_event


class ReviewV07EventlogTests(unittest.TestCase):
    def test_uncertain_notice_without_request_id_has_subject(self):
        now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        row = new_event(now, now, "投稿結果不明のため保留")["row"]
        self.assertEqual(row[2], "知らせの投稿結果を確認できない")
        self.assertEqual(row[3], "")
        self.assertEqual(row[5], "自動再投稿せず保留にした")

    def test_uncertain_instruction_preserves_request_id(self):
        now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        row = new_event(now, now, "投稿結果不明のため保留", "💛20990101-1")["row"]
        self.assertEqual(row[2], "💛20990101-1 の投稿結果を確認できない")
        self.assertEqual(row[3], "💛20990101-1")
        self.assertEqual(row[5], "自動再投稿せず保留にした")
