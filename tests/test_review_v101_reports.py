"""Pure, offline completion rendering and untrusted field validation."""
import unittest
from unittest.mock import patch
from riri.compose import PROJECT_PAGE_OMITTED, project_page, report_summary
from riri.errors import UnsafeContent
from riri.models import Report

A = '💛20261112-1'
B = '💛20261112-2'
URL = 'https://app.notion.com/p/synthetic-page'


def report(ident=A, person='あいす', summary='内容' * 50):
    return Report(ident, person, '完了', summary, 'synthetic-block', '', 'synthetic-digest')


class CompletionFormatTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_no_excerpt_or_parenthesized_result(self):
        self.assertEqual(report_summary([report()], {A: {'案件ページ': ''}}),
                         f'・あいす：{A} → 完了\n案件ページ：なし')

    def test_empty_none_spelling(self):
        for value in ('', ' ', 'なし', '無し', '\nなし\t'):
            with self.subTest(value=value):
                self.assertEqual(project_page({'案件ページ': value}), '案件ページ：なし')

    def test_missing_request_and_missing_field_are_explicit(self):
        for fields in (None, {}):
            self.assertEqual(project_page(fields), '案件ページ：（依頼の記載なし）')
        self.assertTrue(report_summary([report()], {}).endswith('案件ページ：（依頼の記載なし）'))

    def test_url_only_without_title_lookup(self):
        for url in (URL, 'https://notion.so/synthetic-page'):
            self.assertEqual(project_page({'案件ページ': url}), f'案件ページ：（{url}）')

    def test_name_and_url_forms(self):
        name = 'プロジェクト / ページ'
        for value in (f'{name}（{URL}）', f'{name}({URL})', f'{name} {URL}',
                      f'{name} <{URL}>', f'<{URL}|{name}>'):
            with self.subTest(value=value):
                self.assertEqual(project_page({'案件ページ': value}), f'案件ページ：{name}（{URL}）')

    def test_slack_url_only_label_not_repeated(self):
        for value in (f'<{URL}>', f'<{URL}|{URL}>'):
            self.assertEqual(project_page({'案件ページ': value}), f'案件ページ：（{URL}）')

    def test_github_field_never_rendered(self):
        result = report_summary([report()], {A: {'案件ページ': 'なし', 'GitHubプロジェクト': 'https://github.com/synthetic/repo'}})
        self.assertNotIn('GitHub', result)
        self.assertNotIn('github.com', result)

    def test_same_id_people_share_one_page_line(self):
        result = report_summary([report(), report(person='みに')], {A: {'案件ページ': URL}})
        self.assertEqual(result, f'・あいす：{A} → 完了\n・みに：{A} → 完了\n案件ページ：（{URL}）')

    def test_interleaved_ids_are_grouped_without_mixing_pages(self):
        other = 'https://notion.so/other-synthetic'
        result = report_summary([report(), report(B), report(person='みに')],
                                {A: {'案件ページ': URL}, B: {'案件ページ': other}})
        self.assertEqual(result, f'・あいす：{A} → 完了\n・みに：{A} → 完了\n案件ページ：（{URL}）\n・あいす：{B} → 完了\n案件ページ：（{other}）')

    def test_untrusted_urls_fail_closed(self):
        for value in ('http://notion.so/p', 'https://github.com/project/page',
                      'https://app.notion.com.attacker.invalid/p', 'https://notion.so:443/p',
                      'https://user@notion.so/p', 'https://notion.com/p',
                      'javascript:alert(1)', URL + ' ' + URL,
                      URL + '\n追加の文', '<' + URL + '|label> trailing'):
            with self.subTest(value=value):
                self.assertEqual(project_page({'案件ページ': value}), PROJECT_PAGE_OMITTED)

    def test_source_mentions_and_secrets_rejected(self):
        for value in ('<@U0TESTOTHER>', '&lt;@U0TESTOTHER&gt;', '@channel', 'secret_test', '\x00'):
            with self.subTest(value=value), self.assertRaises(UnsafeContent):
                project_page({'案件ページ': value + ' ' + URL})

    def test_encoded_url_controls_rejected(self):
        for suffix in ('%0Atext', '%253C%2540U0TESTOTHER%253E', '%00'):
            with self.subTest(suffix=suffix), self.assertRaises(UnsafeContent):
                project_page({'案件ページ': URL + suffix})

    def test_hidden_summary_unsafe_tail_not_ignored(self):
        for unsafe in ('<@U0TESTOTHER>', '&lt;@U0TESTOTHER&gt;', 'secret_test'):
            with self.subTest(unsafe=unsafe), self.assertRaises(UnsafeContent):
                report_summary([report(summary='a' * 100 + unsafe)], {A: {'案件ページ': 'なし'}})

    def test_configured_secret_rejected_in_full_summary_or_page(self):
        secret = 'synthetic-sensitive-value'
        with self.assertRaises(UnsafeContent):
            report_summary([report(summary='a' * 100 + secret)], secrets=(secret,))
        with self.assertRaises(UnsafeContent):
            project_page({'案件ページ': secret + ' ' + URL}, secrets=(secret,))

    def test_no_reports_produces_no_page(self):
        self.assertEqual(report_summary([], {A: {'案件ページ': URL}}), '')

    def test_malformed_field_types_fail_closed(self):
        for fields in ('案件ページ', [], {'案件ページ': None}, {'案件ページ': 42}):
            with self.subTest(fields=fields), self.assertRaises(UnsafeContent):
                project_page(fields)

    def test_html_escaped_names_and_query(self):
        self.assertEqual(project_page({'案件ページ': 'A & B ' + URL + '?x=1&y=2'}),
                         '案件ページ：A &amp; B（' + URL + '?x=1&amp;y=2）')


if __name__ == '__main__': unittest.main()
