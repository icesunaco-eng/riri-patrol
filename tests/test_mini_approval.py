"""書き手の確認（P2）と完了承認（5-3・B2）。設計 v1.0 の完成基準 18/21/24/30。

Notion の代わりに、作成者・最終編集者・本文を持つ偽のブロックで試す。
2026-11-21 のテスト用ページで確かめた実物の動き（最終編集時刻は分単位で変わらない、
最終編集者は書き換えた連携になる）を、偽のブロックでもそのまま再現している。
"""
import tempfile
import unittest

from riri.mini_run import RunStore
from riri.mini_approval import parse_approval, writer_of, check_approval, approve, recover

TAKAMUNE, NOA, RIRI = 'u-takamune', 'u-noa', 'bot-riri'
AUTHORS = {TAKAMUNE: 'たかむね', NOA: 'のあ'}
TEXT = '''【完了承認】
依頼ID：💚20261121-90
版：第2版
本文ハッシュ：h2
承認した人：たかむね
確かめたこと：ダミー
'''
CURRENT = {'request_id': '💚20261121-90', 'version': 2, 'body_hash': 'h2', 'approvers': {'たかむね', 'のあ'}}


class FakeNotion:
    def __init__(self):
        self.block = {'id': 'b1', 'created_by': {'id': TAKAMUNE}, 'last_edited_by': {'id': TAKAMUNE},
                      'last_edited_time': '2026-11-21T06:01:00.000Z', 'text': TEXT}
        self.status = '対応中'
        self.notices = []
        self.on_set = None

    def get_block(self, _id):
        return dict(self.block)

    def set_status(self, value):
        self.status = value
        hook, self.on_set = self.on_set, None
        if hook:
            hook(self)

    def deps(self):
        return dict(get_block=self.get_block, block_text=lambda b: b['text'], set_status=self.set_status,
                    read_status=lambda: self.status, notify=self.notices.append,
                    author_map=AUTHORS, riri_ids={RIRI}, current=CURRENT)


def rewrite(editor, text):
    def go(n):
        n.block = {**n.block, 'last_edited_by': {'id': editor}, 'text': text}  # 時刻は同じ分のまま
    return go


class WriterTests(unittest.TestCase):
    def test_p2_needs_both_creator_and_last_editor(self):
        # 完成基準18・24
        b = {'created_by': {'id': TAKAMUNE}, 'last_edited_by': {'id': TAKAMUNE}}
        self.assertEqual(writer_of(b, AUTHORS, {RIRI}), 'たかむね')
        self.assertIsNone(writer_of({**b, 'last_edited_by': {'id': NOA}}, AUTHORS, {RIRI}))
        self.assertIsNone(writer_of({**b, 'created_by': {'id': 'u-unknown'}}, AUTHORS, {RIRI}))
        self.assertIsNone(writer_of({'created_by': {'id': TAKAMUNE}}, AUTHORS, {RIRI}))

    def test_riri_exception_only_for_request_status(self):
        b = {'created_by': {'id': TAKAMUNE}, 'last_edited_by': {'id': RIRI}}
        self.assertEqual(writer_of(b, AUTHORS, {RIRI}, status_only_change=True, is_request_block=True), 'たかむね')
        self.assertIsNone(writer_of(b, AUTHORS, {RIRI}, status_only_change=False, is_request_block=True))
        # 【完了承認】には例外を使わない
        self.assertIsNone(writer_of(b, AUTHORS, {RIRI}, status_only_change=True, is_request_block=False))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)
        self.n = FakeNotion()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        self.assertEqual(parse_approval(TEXT)['version'], 2)
        self.assertIsNone(parse_approval(TEXT.replace('第2版', '2')))
        self.assertIsNone(parse_approval(TEXT + '版：第3版\n'))
        self.assertIsNone(parse_approval('【りりへの依頼】\n'))

    def test_valid_approval_confirms(self):
        # 完成基準21
        self.assertEqual(approve(self.store, 'r1', block_id='b1', **self.n.deps()), 'confirmed')
        self.assertEqual(self.n.status, '完了')
        self.assertEqual(self.store.read('r1')['approval']['state'], 'confirmed')

    def test_wrong_version_or_writer_is_invalid_before_any_change(self):
        n = self.n
        n.block['text'] = TEXT.replace('第2版', '第1版')
        self.assertTrue(approve(self.store, 'r1', block_id='b1', **n.deps()).startswith('invalid'))
        n.block['text'] = TEXT
        n.block['last_edited_by'] = {'id': NOA}
        self.assertTrue(approve(self.store, 'r2', block_id='b1', **n.deps()).startswith('invalid'))
        self.assertEqual(n.status, '対応中')

    def test_rewritten_right_after_status_change_is_reverted_once(self):
        # 完成基準30（B2）：別の人が書き換え → 読み戻しで対応中に戻る
        self.n.on_set = rewrite(RIRI, TEXT.replace('ダミー', '書き換えた'))
        self.assertEqual(approve(self.store, 'r1', block_id='b1', **self.n.deps()), 'invalid_reverted')
        self.assertEqual(self.n.status, '対応中')
        self.assertEqual(len(self.n.notices), 1)

    def test_restored_text_by_someone_else_is_still_caught(self):
        # 本文は同じでも最終編集者が違う（実物のテスト用ページと同じ動き）
        self.n.on_set = rewrite(RIRI, TEXT)
        self.assertEqual(approve(self.store, 'r1', block_id='b1', **self.n.deps()), 'invalid_reverted')

    def test_restart_while_pending_redoes_the_check_and_notifies_once(self):
        n = self.n
        # 「これから完了にする」を書いて状態を変えた直後に止まった、という形を作る
        def crash(_):
            raise SystemExit
        n.on_set = lambda n2: (rewrite(RIRI, TEXT)(n2), crash(n2))
        with self.assertRaises(SystemExit):
            approve(self.store, 'r1', block_id='b1', **n.deps())
        self.assertEqual(self.store.read('r1')['approval']['state'], 'pending')
        self.assertEqual(n.status, '完了')
        self.assertEqual(recover(self.store, 'r1', **n.deps()), 'invalid_reverted')
        self.assertEqual(n.status, '対応中')
        self.assertEqual(recover(self.store, 'r1', **n.deps()), 'invalid_reverted')
        self.assertEqual(len(n.notices), 1)


if __name__ == '__main__':
    unittest.main()
