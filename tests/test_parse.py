import hashlib
import unittest
from riri.errors import RequestIncomplete, StatusMissing
from riri.parse import parse_requests, parse_reports, validate_request, replace_status

REQUEST = '''【りりへの依頼】
依頼 ID：💛20261111-1
依頼した子：うさ
宛先（あいす／かれん）：@あいす @かれん
状態：未着手
やること（一文で）：確認する
手順：
1. 読む
2. 調べる
期限・実行タイミング（いつ）：今日中
'''

def block(text, kind='code', id='b1'):
    # Split rich text so a key and its value span fragments.
    return {'id': id, 'type': kind, kind: {'rich_text': [
        {'plain_text': text[:20]}, {'text': {'content': text[20:]}}]}}

class ParseTests(unittest.TestCase):
    def test_fields_order_and_multiline(self):
        reqs = parse_requests([block(REQUEST), block(REQUEST.replace('-1', '-2'), id='b2')])
        self.assertEqual([r.id for r in reqs], ['💛20261111-1','💛20261111-2'])
        self.assertEqual(reqs[0].fields['手順'], '1. 読む\n2. 調べる')
        self.assertEqual(reqs[0].recipients, ['あいす','かれん'])
        validate_request(reqs[0])

    def test_explicit_overrides_mentions_and_empty_does_not_fallback(self):
        text = REQUEST.replace('確認する', '@みに 確認する')
        self.assertEqual(parse_requests([block(text)])[0].recipients, ['あいす','かれん'])
        text = text.replace('@あいす @かれん', '')
        req = parse_requests([block(text)])[0]
        self.assertEqual(req.recipients, [])
        with self.assertRaises(RequestIncomplete): validate_request(req)

    def test_fallback_and_old_log_lines(self):
        text = '\n'.join(x for x in REQUEST.splitlines() if not x.startswith('宛先'))
        text = text.replace('1. 読む', '1. @みに 読む').replace('2. 調べる', '2. @あいす 調べる\n投稿した：@かれん\n2026-11-10 10:20:49 JST 見回り')
        req = parse_requests([block(text)])[0]
        self.assertEqual(req.recipients, ['みに','あいす'])
        self.assertEqual(req.recipients_source, '@名前')
        self.assertNotIn('投稿した', req.fields['手順'])

    def test_excludes_templates_and_heading_subtree(self):
        blocks = [block(REQUEST.replace('💛20261111-1', '💛YYYYMMDD-1')),
                  block('記入例', 'heading_2'), block(REQUEST),
                  block('詳細', 'heading_3'), block(REQUEST),
                  block('依頼一覧', 'heading_2'), block(REQUEST, id='real')]
        self.assertEqual([r.block_id for r in parse_requests(blocks)], ['real'])

    def test_status_validation_and_exact_replacement(self):
        text = REQUEST.replace('状態：未着手', '  状態 ： 未着手  ').replace('\n','\r\n')
        self.assertEqual(replace_status(text, '対応中'), text.replace('未着手','対応中'))
        for invalid in [REQUEST.replace('未着手', '未着手／対応中／完了'), REQUEST + '状態：完了\n']:
            with self.assertRaises((StatusMissing, RequestIncomplete)):
                req = parse_requests([block(invalid)])[0]
                validate_request(req)
            with self.assertRaises(StatusMissing): replace_status(invalid, '完了')

    def test_multiline_numbered_urls_are_preserved(self):
        text = REQUEST.replace('1. 読む', '1. 開く：https://example.test/path')
        req = parse_requests([block(text)])[0]
        self.assertIn('https://example.test/path', req.fields['手順'])

    def test_reports_all_supported_shapes_and_hash(self):
        text = '依頼 ID：💛20261111-1（※このIDは3回目の使用）\n報告した子：あいす\n結果：完了\nやったこと：確認した'
        for kind, header in [('code',''), ('paragraph',''), ('code','【完了報告】\n')]:
            report = parse_reports([block(header+text, kind)], expected_reporter='あいす')[0]
            self.assertEqual(report.id, '💛20261111-1')
            self.assertEqual(report.digest, hashlib.sha256((header+text).encode()).hexdigest())
        with self.assertRaises(RequestIncomplete):
            parse_reports([block(text)], expected_reporter='かれん')

    def test_truncated_reports_fail_closed_but_templates_do_not(self):
        for text in [
            '【完了報告】',
            '【完了報告】\n依頼ID：💛20261111-1',
            '依頼ID：💛20261111-1\n報告した子：あいす',
            '【完了報告】\n依頼ID：💛20261111-1\n報告した子：あいす\n結果：',
        ]:
            with self.subTest(text=text), self.assertRaises(RequestIncomplete):
                parse_reports([block(text)])
        for text in ['【完了報告】\n依頼ID：\n報告した子：',
                     '【完了報告】\n依頼ID：💛YYYYMMDD-1\n報告した子：あいす',
                     '【完了報告】記入例\n依頼ID：💛20261111-1\n報告した子：あいす']:
            self.assertEqual(parse_reports([block(text)]), [])
        self.assertEqual(parse_reports([block('記入例', 'heading_2'),
                                       block('【完了報告】')]), [])

    def test_duplicate_fields_rejected_after_normalization(self):
        for text in [REQUEST + '依頼ID：💛20261111-2', REQUEST + '宛先：みに']:
            with self.assertRaises(RequestIncomplete): parse_requests([block(text)])
        for duplicate in ['依頼 ID：💛20261111-2', '結果（報告）：一部完了', '報告した子：かれん']:
            text = '依頼ID：💛20261111-1\n報告した子：あいす\n結果：完了\n' + duplicate
            with self.assertRaises(RequestIncomplete): parse_reports([block(text)])

    def test_unknown_fallback_mentions_prevent_partial_dispatch(self):
        base = '\n'.join(x for x in REQUEST.splitlines() if not x.startswith('宛先'))
        for mentions in ['@あいす @知らない 確認', '@あいす @unknown 確認', '@あいす @あいすもどき 確認', '@あいす https://example.test/、@知らない 確認']:
            req = parse_requests([block(base.replace('確認する', mentions))])[0]
            self.assertIn('あいす', req.recipients)
            with self.assertRaises(RequestIncomplete): validate_request(req)
        req = parse_requests([block(base.replace('確認する',
            '@あいす https://example.test/@unknown?q=@nobody user@example.test 確認'))])[0]
        self.assertEqual(req.recipients, ['あいす'])
        validate_request(req)

    def test_invalid_recipient_id_and_report_result(self):
        for text in [REQUEST.replace('@あいす @かれん','@知らない'), REQUEST.replace('💛20261111-1','bad')]:
            with self.assertRaises(RequestIncomplete): validate_request(parse_requests([block(text)])[0])
        self.assertEqual(parse_requests([dict(block(REQUEST), in_trash=True)]), [])
        with self.assertRaises(RequestIncomplete):
            parse_reports([block('依頼ID：💛20261111-1\n報告した子：あいす\n結果：完了かも')])

if __name__ == '__main__': unittest.main()


MINI_REQUEST = '''【りりへの依頼】
依頼ID：💚20261121-90
宛先：@みに
依頼した子：のあ

状態：未着手
案件ページ（NotionのURL）：https://app.notion.com/p/3ecf1dff94dc8151a598ddf9297ea322
やること（一文で）：ダミー
手順：
1. 前回の証拠を確かめる
状態：失敗（証拠の行）
期限・実行タイミング（いますぐ／毎日〇時／5分おき など）：いますぐ
'''


class StatusFieldP3Tests(unittest.TestCase):
    """設計 v1.0 の P3・B4・完成基準27/32：状態欄は案件ページより前の1行だけ。"""

    def test_evidence_status_line_is_body_not_a_duplicate_field(self):
        req = parse_requests([block(MINI_REQUEST)])[0]
        self.assertEqual(req.status, '未着手')
        self.assertIn('状態：失敗（証拠の行）', req.fields['手順'])
        validate_request(req)

    def test_replace_status_changes_only_the_status_field(self):
        out = replace_status(MINI_REQUEST, '対応中')
        lines = [x for x in out.splitlines() if x.startswith('状態')]
        self.assertEqual(lines, ['状態：対応中', '状態：失敗（証拠の行）'])
        self.assertEqual(out.replace('状態：対応中', '状態：未着手', 1), MINI_REQUEST)

    def test_blank_line_before_status_does_not_hide_it(self):
        # 「依頼した子」と「状態」の間に空行がある（MINI_REQUEST 自体がそう）
        self.assertEqual(parse_requests([block(MINI_REQUEST)])[0].status, '未着手')

    def test_two_status_lines_before_project_page_are_rejected(self):
        text = MINI_REQUEST.replace('状態：未着手\n', '状態：未着手\n状態：対応中\n', 1)
        with self.assertRaises(StatusMissing):
            replace_status(text, '完了')
        with self.assertRaises(RequestIncomplete):
            parse_requests([block(text)])

    def test_no_status_before_project_page_is_not_guessed_from_body(self):
        text = MINI_REQUEST.replace('状態：未着手\n', '', 1)
        req = parse_requests([block(text)])[0]
        self.assertEqual(req.status, '')
        with self.assertRaises(StatusMissing):
            validate_request(req)
        with self.assertRaises(StatusMissing):
            replace_status(text, '完了')

    def test_request_without_project_page_line_keeps_old_behaviour(self):
        reqs = parse_requests([block(REQUEST)])
        self.assertEqual(reqs[0].status, '未着手')
        self.assertIn('状態：完了', replace_status(REQUEST, '完了'))
