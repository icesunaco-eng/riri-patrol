import unittest
from riri.compose import instruction, report_summary, mini_notice, riri_notice
from riri.errors import SafetyError
from riri.parse import parse_requests, parse_reports
from tests.test_parse import REQUEST, block

class ComposeTests(unittest.TestCase):
    def setUp(self): self.req = parse_requests([block(REQUEST)])[0]
    def test_instruction_preserves_material(self):
        self.req.fields.update({'GitHubプロジェクト':'https://github.com/example/repo', 'やってはいけないこと':'送らない\n消さない'})
        text = instruction(self.req, self.req.recipients)
        for key in ['依頼ID', '宛先', '元の依頼', '何を', 'いつまでに', '報告先', 'GitHubプロジェクト', 'くわしい内容', '期待する結果', 'やってはいけないこと']:
            self.assertIn(key+'：', text)
        self.assertTrue(text.startswith('@あいす @かれん\n'))
        self.assertIn('送らない\n  消さない', text)
        self.assertIn('1. 読む\n  2. 調べる', text)
    def test_unsafe_raw_fields_not_silently_sanitized(self):
        for bad in ['<@U123>', '<!subteam^S123>', '@channel', ('xoxb' + '-secret'), 'ntn_secret', '&lt;@U123&gt;', '\x00']:
            self.req.fields['やること'] = bad
            with self.assertRaises(SafetyError): instruction(self.req, self.req.recipients)
    def test_length_never_truncated(self):
        self.req.fields['手順'] = '大事'*1600
        with self.assertRaises(SafetyError): instruction(self.req, self.req.recipients)
    def test_notices_and_report_summary(self):
        self.assertIn(self.req.id, mini_notice(self.req.id))
        self.assertIn('飛ばします', riri_notice(self.req.id))
        reports = parse_reports([block('依頼ID：💛20261111-1\n報告した子：あいす\n結果：完了\nやったこと：'+'確'*50)])
        self.assertEqual(report_summary(reports), '・あいす：💛20261111-1 → 完了\n作業場所URL（あいす）：未記載（URL追記待ち）\n案件ページ：（依頼の記載なし）')
        reports[0].text += '\nsecret_token'
        # Unrendered historical/source fields are not outgoing message content.
        self.assertNotIn('secret_', report_summary(reports))
        reports[0].summary = 'secret_token'
        with self.assertRaises(SafetyError): report_summary(reports)
if __name__ == '__main__': unittest.main()
