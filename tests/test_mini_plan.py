"""作業場所・資料・続き（riri/mini_plan.py）。設計 v1.0 の 4-1-3・4-2・4-3、完成基準 1/6/7/9/22/28。"""
import os
import tempfile
import unittest
from pathlib import Path

from riri.mini_plan import page_id_of, find_case, workplace, materials_changed, continuation_check

PID = '3ecf1dff94dc8151a598ddf9297ea322'
URL = f'https://app.notion.com/p/{PID}'


class PageIdTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(page_id_of(URL), PID)
        self.assertEqual(page_id_of(f'https://www.notion.so/Title-{PID.upper()}?pvs=4'), PID)
        self.assertEqual(page_id_of('https://app.notion.com/p/3ecf1dff-94dc-8151-a598-ddf9297ea322/'), PID)
        self.assertIsNone(page_id_of('https://example.com/x'))
        self.assertIsNone(page_id_of(None))


class FindCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mini = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def case(self, group, name, text):
        d = self.mini / group / name
        d.mkdir(parents=True)
        (d / 'AGENTS.md').write_text(text, encoding='utf-8')
        return d

    def test_rows_of_completion_1(self):
        # 完成基準1：一致・受付別名・独立監視・0件・2件・シンボリックリンク・読めない
        a = self.case('案件', 'A', f'NotionページID: {PID}\n')
        self.assertEqual(find_case(self.mini, URL)['folder'], a)
        self.assertEqual(find_case(self.mini, 'https://app.notion.com/p/' + 'f' * 32)['kind'], 'none')
        self.case('独立監視', 'B', f'受付別名ID: https://www.notion.so/x-{PID}\n')
        self.assertEqual(find_case(self.mini, URL)['kind'], 'many')

    def test_alias_only(self):
        b = self.case('独立監視', 'B', f'受付別名ID：{PID}\n')
        self.assertEqual(find_case(self.mini, URL)['folder'], b)

    def test_mention_in_other_lines_is_ignored(self):
        self.case('案件', 'A', f'メモ：{PID} はここに書いても数えない\n')
        self.assertEqual(find_case(self.mini, URL)['kind'], 'none')

    def test_symlink_and_unreadable(self):
        real = self.case('案件', 'A', f'NotionページID: {PID}\n')
        os.symlink(real, self.mini / '案件' / 'L')
        self.assertEqual(find_case(self.mini, URL)['kind'], 'unreadable')
        os.unlink(self.mini / '案件' / 'L')
        (self.mini / '案件' / 'C').mkdir()
        (self.mini / '案件' / 'C' / 'AGENTS.md').write_bytes(b'\xff\xfe')
        self.assertEqual(find_case(self.mini, URL)['kind'], 'unreadable')


class WorkplaceTests(unittest.TestCase):
    ONE = {'kind': 'one', 'folder': '/m/案件/A', 'reason': ''}

    def wp(self, **k):
        base = dict(always_ok=True, case=self.ONE, stage=2, write_ok=True, case_folder_real='/m/案件/A',
                    write_roots=['/m/案件/A', '/w/ok'], targets=['/w/ok/x', '/w/ng'])
        base.update(k)
        return workplace(**base)

    def test_each_row_and_priority(self):
        # 完成基準6
        self.assertEqual(self.wp(always_ok=False)['row'], 1)
        self.assertEqual(self.wp(always_ok=False, case={'kind': 'many', 'folder': None})['row'], 1)  # 重なり→上が勝つ
        self.assertEqual(self.wp(case={'kind': 'none', 'folder': None, 'reason': 'x'})['row'], 2)
        self.assertEqual(self.wp(stage=1)['row'], 3)
        r4 = self.wp()
        self.assertEqual((r4['row'], r4['write'], r4['blocked_targets']), (4, ['/m/案件/A', '/w/ok/x'], ['/w/ng']))
        self.assertEqual(self.wp(write_roots=['/w/ok'])['reason'], '許可外')
        self.assertEqual(self.wp(write_ok=False)['reason'], '資料不足')
        self.assertEqual(self.wp(stage=0)['row'], 6)

    def test_prefix_is_not_a_root(self):
        self.assertEqual(self.wp(write_roots=['/m/案件/AB'], case_folder_real='/m/案件/A')['row'], 5)


class MaterialsTests(unittest.TestCase):
    def test_any_single_change_is_found(self):
        # 完成基準7
        fixed = {'request': 'h1', 'case_page': 'h2', 'rules': 'h3', 'targets': 'h4'}
        self.assertEqual(materials_changed(fixed, dict(fixed)), [])
        for name in fixed:
            self.assertEqual(materials_changed(fixed, {**fixed, name: 'x'}), [name])
        self.assertEqual(materials_changed(fixed, {k: v for k, v in fixed.items() if k != 'rules'}), ['rules'])


class ContinuationTests(unittest.TestCase):
    PLAN = {'read': ['/a'], 'write': [], 'row': 3, 'stage': 1, 'fingerprint': 'fp'}
    EVIDENCE = {'kind': 'watched', 'exit_code': 0, 'reason': 'exited', 'mates_empty': True,
                'at': '2026-11-25T00:00:00+00:00'}
    ORIG = {'request_id': '💚1', 'thread_id': 't-1', 'end_evidence': EVIDENCE,
            'normalized_hash': 'h', 'original_hash_now': 'h', 'plan': PLAN}

    def chk(self, **k):
        base = dict(original=self.ORIG, block_count=1, block_hash='c', writer_ok=True,
                    now_plan=self.PLAN, consumed=set(), block_id='blk')
        base.update(k)
        return continuation_check(**base)

    def test_ok_and_each_failure(self):
        # 完成基準9・22・28
        self.assertEqual(self.chk(), (True, ''))
        self.assertFalse(self.chk(original={**self.ORIG, 'thread_id': None})[0])          # 会話のない失敗
        self.assertFalse(self.chk(original={**self.ORIG, 'original_hash_now': 'z'})[0])   # 元が書き換わった
        self.assertFalse(self.chk(block_count=2)[0])
        self.assertFalse(self.chk(writer_ok=False)[0])
        self.assertFalse(self.chk(now_plan={**self.PLAN, 'fingerprint': 'other'})[0])
        self.assertFalse(self.chk(consumed={('💚1', 'blk')})[0])                          # 同じ続きを二度

    def test_incomplete_end_evidence_is_no_end(self):
        # 再検証 💚20261125-8 S7 と同じ種類：足りない終了の証拠では続きを始めない
        for ev in ({'exit_code': 0}, {**self.EVIDENCE, 'mates_empty': False}, {**self.EVIDENCE, 'at': ''},
                   {'kind': 'manual', 'by': 'のあ', 'mates_checked': True, 'at': 'x'}):
            with self.subTest(ev):
                self.assertFalse(self.chk(original={**self.ORIG, 'end_evidence': ev})[0])


if __name__ == '__main__':
    unittest.main()
