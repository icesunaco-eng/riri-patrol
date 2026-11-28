"""報告の版（riri/mini_report.py）。設計 v1.0 の 5-2・Y5・Z4・X6、完成基準 11/19。"""
import unittest

from riri.mini_report import body_hash, current_report, new_version_text, notice_id

V1 = '''【完了報告】
依頼ID：💚20261121-90
報告した子：みに
結果：一部完了（最終の裏付けなし）
証拠：
状態：失敗（試験ログの行）
'''


class BodyHashTests(unittest.TestCase):
    def test_line_ends_and_trailing_spaces_do_not_matter(self):
        # 完成基準19
        self.assertEqual(body_hash(V1), body_hash(V1.replace('\n', '\r\n')))
        self.assertEqual(body_hash(V1), body_hash('\n\n' + V1.replace('みに', 'みに   ') + '\n\n'))

    def test_inner_spaces_and_evidence_lines_matter(self):
        self.assertNotEqual(body_hash(V1), body_hash(V1.replace('最終の裏付け', '最終の  裏付け')))
        self.assertNotEqual(body_hash(V1), body_hash(V1.replace('状態：失敗', '状態：成功')))

    def test_only_the_top_header_is_dropped(self):
        v2 = new_version_text(V1, 2, 'blk1', body_hash(V1))
        self.assertEqual(body_hash(v2), body_hash(V1))
        # 本文の途中の「版：」は除かない
        self.assertNotEqual(body_hash(V1 + '版：第9版\n'), body_hash(V1))

    def test_known_digest_is_fixed(self):
        # 既知の入力と期待するハッシュを固定する（Y5）
        text = '版：第2版\n前の版：x\n本文ハッシュ：y\n\n【完了報告】\r\n結果：完了  です   \n\n'
        self.assertEqual(body_hash(text), '369ec98685cc00dbf8ff399becde0d881c126fc103356dce9820f3d0ec7c800f')


class ChainTests(unittest.TestCase):
    def v2(self, body=None, prev=None):
        body = body or V1.replace('一部完了（最終の裏付けなし）', 'できなかった')
        return new_version_text(body, 2, 'blk1', prev or body_hash(V1))

    def test_first_version_without_header(self):
        self.assertEqual(current_report([('blk1', V1)])['version'], 1)

    def test_second_version_becomes_current(self):
        # 完成基準11
        res = current_report([('blk1', V1), ('blk2', self.v2())])
        self.assertEqual((res['state'], res['version'], res['block_id']), ('ok', 2, 'blk2'))

    def test_holds(self):
        self.assertEqual(current_report([('blk1', V1), ('blk2', self.v2()), ('blk3', self.v2())])['state'], 'hold')
        v3 = new_version_text(V1, 3, 'blk1', body_hash(V1))
        self.assertEqual(current_report([('blk1', V1), ('blk3', v3)])['reason'], '版が飛んでいる')
        bad = self.v2(prev='0' * 64)
        self.assertIn('前の版', current_report([('blk1', V1), ('blk2', bad)])['reason'])
        edited = self.v2().replace('できなかった', '完了')
        self.assertIn('本文ハッシュ', current_report([('blk1', V1), ('blk2', edited)])['reason'])

    def test_none_and_notice_id(self):
        self.assertEqual(current_report([])['state'], 'none')
        self.assertEqual(notice_id('💚1', 2, 'gdm'), 'mini-report:💚1:v2:gdm')


if __name__ == '__main__':
    unittest.main()
