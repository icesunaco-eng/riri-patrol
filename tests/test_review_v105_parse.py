"""AB result annotations normalize without changing the source fingerprint."""
import hashlib
import unittest

from riri.errors import RequestIncomplete
from riri.parse import parse_reports, validate_report
from tests.test_parse import block


class V105ReportResultTests(unittest.TestCase):
    def parse(self, result):
        text = ('【完了報告】\n依頼ID：🤍20261112-1\n報告した子：うさ\n'
                f'結果：{result}\nやったこと：レビューした')
        return text, parse_reports([block(text)], expected_reporter='うさ')

    def test_exact_outcomes(self):
        for outcome in ('完了', '一部完了', 'できなかった'):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.parse(outcome)[1][0].result, outcome)

    def test_parenthetical_annotations(self):
        for outcome in ('完了', '一部完了', 'できなかった'):
            for tail in ('（レビューの完了。本番開始の承認ではない）', '(補足)', '（', '('):
                with self.subTest(outcome=outcome, tail=tail):
                    self.assertEqual(self.parse(outcome + tail)[1][0].result, outcome)

    def test_whitespace_annotations(self):
        for outcome in ('完了', '一部完了', 'できなかった'):
            for separator in (' ', '\t', '　', '\n'):
                with self.subTest(outcome=outcome, separator=separator):
                    self.assertEqual(self.parse(outcome + separator + '補足')[1][0].result, outcome)

    def test_invalid_suffixes_remain_invalid(self):
        for result in ('完了してない', '完了？', '完了かも', '完了。', '完了/未完了',
                       '一部完了してない', '一部完了？', 'できなかったかも', 'できなかった？', '', '完'):
            with self.subTest(result=result), self.assertRaises(RequestIncomplete):
                self.parse(result)

    def test_source_and_digest_unchanged(self):
        text, reports = self.parse('完了（レビューの完了。Y書の完成確認・本番開始の承認ではない）')
        self.assertEqual(reports[0].result, '完了')
        self.assertEqual(reports[0].text, text)
        self.assertEqual(reports[0].digest, hashlib.sha256(text.encode()).hexdigest())

    def test_validation_does_not_modify_input_fields(self):
        fields = {'依頼ID': '🤍20261112-1', '報告した子': 'うさ', '結果': '完了（補足）'}
        original = dict(fields)
        self.assertEqual(validate_report(fields, 'うさ'), ('🤍20261112-1', 'うさ'))
        self.assertEqual(fields, original)

    def test_identity_validation_still_applies(self):
        fields = {'依頼ID': '🤍20261112-1', '報告した子': 'うさ', '結果': '完了（補足）'}
        with self.assertRaises(RequestIncomplete):
            validate_report(fields, 'みに')
        fields['依頼ID'] = 'bad'
        with self.assertRaises(RequestIncomplete):
            validate_report(fields, 'うさ')


if __name__ == '__main__':
    unittest.main()
