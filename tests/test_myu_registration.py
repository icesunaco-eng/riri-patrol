import re
import tempfile
import unittest
from pathlib import Path
from riri.models import RECIPIENTS, REPORT_PAGES
from riri.config import Config
from riri.parse import ID_PATTERN, parse_reports

class MyuRegistrationTests(unittest.TestCase):
    def test_registry_keeps_existing_and_adds_myu(self):
        self.assertEqual(len(RECIPIENTS), 9)
        self.assertEqual(len(REPORT_PAGES), 8)
        self.assertIn("みに", RECIPIENTS)
        self.assertIn("みき", RECIPIENTS)
        self.assertIn("みゅう", RECIPIENTS)
        self.assertTrue(REPORT_PAGES["みゅう"].endswith("41af1dff94dc81029c04de1714325e47"))

    def test_pink_id_and_myu_report_are_parsed(self):
        self.assertIsNotNone(re.fullmatch(ID_PATTERN, "🩷20261117-1"))
        text = "【完了報告】\n依頼ID：🩷20261117-1\n報告した子：みゅう\n結果：完了\nやったこと：登録試験"
        block = {"id":"a"*32,"type":"paragraph","paragraph":{"rich_text":[{"text":{"content":text}}]}}
        self.assertEqual(parse_reports([block],expected_reporter="みゅう")[0].reporter,"みゅう")

    def test_myu_config_override_is_literal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/".env").write_text("NOTION_REPORT_PAGE_MYU="+"b"*32+"\n")
            self.assertEqual(Config.load(root).pages["みゅう"],"https://app.notion.com/p/"+"b"*32)
