"""Y-01: safe plain-text page mentions and unsupported-link omission."""
import unittest
from unittest.mock import patch
from riri.compose import (PROJECT_PAGE_LIMIT, PROJECT_PAGE_OMITTED,
                          ProjectPageUnsafe, project_page, report_summary)
from riri.errors import UnsafeContent
from tests.test_review_v101_reports import A, report


class ProjectPageV102Tests(unittest.TestCase):
    def setUp(self):
        self.net = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def test_plain_text_page_mention_title(self):
        for title in ('Y書 v0.2', 'MF完全自動化 / Y書 v0.2', 'ページ名だけ'):
            self.assertEqual(project_page({'案件ページ': title}), '案件ページ：' + title)

    def test_title_escaped_and_whitespace_normalized(self):
        self.assertEqual(project_page({'案件ページ': ' A & B <draft>\n 次の行 '}),
                         '案件ページ：A &amp; B &lt;draft&gt; 次の行')

    def test_www_notion_https_is_exactly_allowed(self):
        url = 'https://www.notion.so/synthetic-page'
        for value in (url, '<' + url + '>', '名前（' + url + '）'):
            name = '名前' if value.startswith('名前') else ''
            self.assertEqual(project_page({'案件ページ': value}), '案件ページ：' + name + '（' + url + '）')

    def test_www_notion_nonhttps_or_spoof_is_omitted(self):
        for value in ('http://www.notion.so/p', 'https://www.notion.so.evil.invalid/p',
                      'https://www.notion.so@evil.invalid/p', 'https://www.notion.so:443/p'):
            self.assertEqual(project_page({'案件ページ': value}), PROJECT_PAGE_OMITTED)

    def test_external_and_unknown_urls_omitted_without_source(self):
        for value in ('https://outside.invalid/private', 'javascript:alert(1)',
                      'www.notion.so/page', 'https//notion.so/page',
                      'https%3A%2F%2Foutside.invalid%2Fprivate',
                      '<https://notion.so/a|name> trailing', 'https://notion.so/a https://notion.so/b'):
            self.assertEqual(project_page({'案件ページ': value}), PROJECT_PAGE_OMITTED)

    def test_safe_malformed_url_characters_omitted(self):
        for suffix in ('%5Cevil', '%20', '<ordinary>', '\\more'):
            self.assertEqual(project_page({'案件ページ': 'https://notion.so/a' + suffix}), PROJECT_PAGE_OMITTED)

    def test_raw_unsafe_external_url_still_held(self):
        for unsafe in ('<@U0TESTOTHER>', '@channel', 'secret_test', '\x00', '\x1b'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ProjectPageUnsafe):
                project_page({'案件ページ': 'https://outside.invalid/' + unsafe})

    def test_encoded_unsafe_external_url_still_held(self):
        for unsafe in ('%3C%40U0TESTOTHER%3E', '%2540channel', '%73ecret_test',
                       '%00', '%0A', '&#x3c;@U0TESTOTHER&#x3e;', '%E2%80%AE'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ProjectPageUnsafe):
                project_page({'案件ページ': 'https://outside.invalid/' + unsafe})

    def test_title_unsafe_and_encoded_unsafe_still_held(self):
        for unsafe in ('<@U0TESTOTHER>', '&lt;@U0TESTOTHER&gt;', '%3C%40U0TESTOTHER%3E',
                       'secret_test', '\x07', '\u2066'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ProjectPageUnsafe):
                project_page({'案件ページ': 'タイトル ' + unsafe})

    def test_supplied_secret_raw_and_encoded_rejected_before_omission(self):
        secret = 'synthetic-sensitive-value'
        for value in ('https://outside.invalid/' + secret,
                      'https://outside.invalid/%73ynthetic-sensitive-value'):
            with self.assertRaises(ProjectPageUnsafe) as caught:
                project_page({'案件ページ': value}, secrets=(secret,))
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn('outside.invalid', str(caught.exception))

    def test_oversized_source_and_escaped_render_held(self):
        for value in ('a' * (PROJECT_PAGE_LIMIT + 1), '&' * 400,
                      'https://outside.invalid/' + 'a' * PROJECT_PAGE_LIMIT):
            with self.assertRaises(ProjectPageUnsafe):
                project_page({'案件ページ': value})

    def test_ordinary_line_wrapping_remains_safe(self):
        self.assertEqual(project_page({'案件ページ': '名前\nA &amp; B'}),
                         '案件ページ：名前 A &amp;amp; B')

    def test_page_fault_distinct_from_summary_fault(self):
        with self.assertRaises(ProjectPageUnsafe):
            report_summary([report()], {A: {'案件ページ': '<@U0TESTOTHER>'}})
        with self.assertRaises(UnsafeContent) as caught:
            report_summary([report(summary='<@U0TESTOTHER>')], {A: {'案件ページ': 'Y書 v0.2'}})
        self.assertNotIsInstance(caught.exception, ProjectPageUnsafe)

    def test_reports_not_blocked_by_supported_titles_or_safe_omission(self):
        for page, expected in (('Y書 v0.2', '案件ページ：Y書 v0.2'),
                               ('https://outside.invalid/p', PROJECT_PAGE_OMITTED),
                               ('https://www.notion.so/p', '案件ページ：（https://www.notion.so/p）')):
            self.assertEqual(report_summary([report()], {A: {'案件ページ': page}}),
                             f'・あいす：{A} → 完了\n作業場所URL（あいす）：未記載（任意）\n' + expected)


if __name__ == '__main__': unittest.main()
