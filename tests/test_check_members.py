"""scripts/check_members.py finds contradictions and gaps without importing the other programs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_members', ROOT / 'scripts' / 'check_members.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

ALL = '🤍|💚|💙|🩵|❤️|💛|🧡|💜|🩷'


class CheckMembersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ('miki', 'mamu', 'myu/myu_app'):
            (self.root / name).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        (self.root / rel).write_text(text, encoding='utf-8')

    def run_check(self):
        return check.run(self.root / 'miki', self.root / 'mamu', self.root / 'myu')

    def consistent(self):
        agents = [{'agent_id': 'aisu', 'name': 'あいす', 'heart': '🤍'},
                  {'agent_id': 'noa', 'name': 'のあ', 'heart': '💚'}]
        self.write('miki/registry.json', json.dumps({'agents': agents}, ensure_ascii=False))
        self.write('mamu/mamu_guard.py', "MEMBERS = {'mamu': 'まむ', 'ice': 'あいす'}\n_X = '(?:のあ|みに)'\n")
        self.write('mamu/mamu_relay.py', "import re\nID = re.compile(r'🧡[0-9]{8}-[1-9][0-9]*\\Z')\n"
                   "REL = r'(?:" + ALL + ")[0-9]{8}'\nF = r'(?:やること|依頼ID)'\n")
        self.write('myu/myu_app/riri_link.py', "import re\nID = re.compile(r'(?:" + ALL + ")[0-9]{8}-[0-9]+')\n"
                   "S = r'(?:完了|対応中)'\n")
        self.write('myu/myu_app/slack_reader.py', "class R:\n    HEARTS = {" + ", ".join(
            "'h%d': '%s'" % (i, h) for i, h in enumerate(ALL.split('|'))) + "}\n")

    def test_aliases_and_non_heart_alternations_are_not_errors(self):
        self.consistent()
        report = self.run_check()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.checked, ['みき', 'まむ', 'みゅう'])
        self.assertTrue(any('まむ mamu_guard.py' in w and 'みき' in w for w in report.warnings))

    def test_contradictions_are_errors(self):
        self.consistent()
        self.write('miki/registry.json', json.dumps({'agents': [
            {'agent_id': 'noa', 'name': 'のあ', 'heart': '🩵'},
            {'agent_id': 'mini', 'name': 'みゅう', 'heart': '🩷'}]}, ensure_ascii=False))
        self.write('mamu/mamu_guard.py', "MEMBERS = {'mamu': 'まむ', 'myu': 'みに'}\n")
        self.write('myu/myu_app/riri_link.py', "import re\nID = re.compile(r'(?:🤍|🩵)[0-9]{8}-[0-9]+')\n")
        errors = '\n'.join(self.run_check().errors)
        self.assertIn('のあ」: 色 🩵 が名簿の 💚 と違う', errors)
        self.assertIn("agent_id 'mini'", errors)
        self.assertIn("MEMBERS['myu']: ID 'myu' が名簿の みに と合わない", errors)
        self.assertIn('みゅう riri_link.py ID', errors)

    def test_missing_checkouts_are_reported_as_unchecked(self):
        empty = self.root / 'none'
        report = check.run(empty, empty, None)
        self.assertEqual(report.checked, [])
        self.assertEqual(len(report.skipped), 3)


if __name__ == '__main__':
    unittest.main()
