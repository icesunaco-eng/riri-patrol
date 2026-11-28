"""Review checks for mini material references and the design-version digest."""
import unittest

from riri import mini_lane as lane
from riri.errors import RemoteError
from riri.parse import parse_requests
from tests.test_mini_lane import Base, request_text
from tests import test_mini_rereview20 as rereview


class DesignHashHeadingTests(unittest.TestCase):
    def test_only_the_exact_review_heading_stops_the_digest(self):
        def digest(requirement, review):
            block = rereview.DesignHashTests.blk
            page = [
                block('heading_2', '独立検証の記録を付ける条件', 'conditions'),
                block('paragraph', requirement, 'requirement'),
                block('heading_2', '独立検証の記録', 'review'),
                block('paragraph', review, 'record'),
            ]
            return lane.design_hash_of(rereview.DesignHashTests.Pages({lane.DESIGN_PAGE: page}))

        self.assertNotEqual(digest('条件A', '記録A'), digest('条件B', '記録A'))
        self.assertEqual(digest('条件A', '記録A'), digest('条件A', '記録B'))


class UseMaterialReferenceTests(Base):
    def gather_with_use(self, value, *, link=None):
        text = request_text() + '使うもの：' + value + '\n'
        head, tail = text.rsplit(value, 1)
        req = parse_requests([rereview.rich_block([(head, None), (value, link), (tail, None)])])[0]
        record = {'block_id': req.block_id, 'fields': req.fields}
        materials, _, missing, _ = lane.gather(self.cfg, record, req.text, self.notion)
        return materials, missing

    def test_external_links_with_a_hex_suffix_are_not_notion_materials(self):
        fake_id = 'f' * 32
        reads = []
        read_blocks = self.notion.read_blocks

        def read(page_id):
            reads.append(page_id)
            if page_id == fake_id:
                raise RemoteError('Not found')
            return read_blocks(page_id)

        self.notion.read_blocks = read
        url = 'https://example.com/' + fake_id
        for value, link in [(url, None), ('外部の資料', url)]:
            with self.subTest(link=link):
                materials, missing = self.gather_with_use(value, link=link)
                self.assertNotIn(fake_id, reads)
                self.assertNotIn('use_0', materials)
                self.assertNotIn('use_0', missing)

    def test_notion_links_without_page_ids_count_as_missing(self):
        url = 'https://www.notion.so/team/Spec-With-No-Id'
        for value, link in [(url, None), ('要件のページ', url)]:
            with self.subTest(link=link):
                _, missing = self.gather_with_use(value, link=link)
                self.assertIn('use_0', missing)

    def test_supported_notion_hosts_load_materials(self):
        page_id = 'a' * 32
        self.notion.pages[page_id] = []
        for host in ('app.notion.com', 'notion.so', 'www.notion.so'):
            with self.subTest(host=host):
                url = f'https://{host}/team/Spec-{page_id}?pvs=4'
                materials, missing = self.gather_with_use('要件のページ', link=url)
                self.assertIn('use_0', materials)
                self.assertNotIn('use_0', missing)
