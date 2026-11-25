"""再レビュー 💚20261125-16 の T2（run の記録の形）と T3（状態の行の選び方・1件書けなくても巡回は止まらない）。
#22 の範囲（mini_run・parse の P3・notion・patrol）だけで通す。"""
import unittest

from riri.mini_run import record_problem


class T2RecordShapeTests(unittest.TestCase):
    def test_wrong_shapes_are_broken(self):
        from riri.mini_run import record_problem
        for bad in ({'state': 1}, {'launch_mark': True}, {'launch_mark': ''}, {'spawn': 'x'},
                    {'supervisor': {'pid': '12'}}, {'end_evidence': []}, {'child': {'pid': 1}},
                    {'state': 'launched'}):
            with self.subTest(bad):
                self.assertIsNotNone(record_problem(bad))
        for good in ({'state': 'reserved'}, {'state': 'spawning', 'spawn': {'pid': None, 'at': 'x'}},
                     {'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 5, 'pgid': 5, 'at': 'x'}}):
            with self.subTest(good):
                self.assertIsNone(record_problem(good))


    def test_scan_counts_a_launched_record_without_mark_as_broken(self):
        import json
        import tempfile
        from pathlib import Path
        from riri.mini_run import RunStore
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(Path(d))
            store.update('mini-' + 'c' * 20, lambda r: {'state': 'reserved'})
            store.dir.joinpath('mini-' + 'a' * 20 + '.json').write_text(
                json.dumps({'run_id': 'mini-' + 'a' * 20, 'state': 'launched'}))
            runs, broken = store.scan()
            self.assertEqual([r['run_id'] for r in runs], ['mini-' + 'c' * 20])   # 読める run は使える
            self.assertEqual(broken, ['mini-' + 'a' * 20 + '.json'])
            with self.assertRaises(ValueError):
                store.all_runs()


QUOTED = ('【りりへの依頼】\n依頼ID：💛20990101-1\n依頼した子：うさ\n宛先：@うさ\n状態：未着手\n'
          '案件ページ：https://app.notion.com/p/' + '1' * 32 + '\n'
          'やること：手順を確認する\n手順：\n1. 前の報告の「状態：失敗」を読む\n'
          '期限・実行タイミング：今日中\nGitHubプロジェクト：なし\n')


class T3StatusLineTests(unittest.TestCase):
    """T3：受付（P3）と Notion への書込みが、同じ「状態」の行を選ぶ。1件書けなくても巡回は止まらない。"""

    def test_parse_and_the_writer_pick_the_same_line(self):
        from riri.notion import NotionClient
        from riri.parse import locate_status_line, parse_requests
        from tests.test_patrol_flow import block
        r = parse_requests([block(QUOTED, 'request-1')])[0]
        self.assertEqual(r.status, '未着手')
        seen = []

        class Transport:
            def request(self, method, url, **kw):
                seen.append(method)
                return {'object': 'block', 'id': 'request-1', 'type': 'code', 'in_trash': False,
                        'last_edited_time': '2026-11-25T00:00:00.000Z',
                        'code': {'rich_text': [{'type': 'text', 'text': {'content': QUOTED}, 'plain_text': QUOTED}]}}
        NotionClient('t', Transport()).set_status('request-1', QUOTED, '対応中', dry_run=True)
        self.assertEqual(seen, ['GET'])                                    # 書き込みは試さない（dry_run）
        from riri.notion import status_matches
        self.assertEqual([m.start() for m in status_matches(QUOTED)], [locate_status_line(QUOTED).start()])


class T3PatrolGoesOnTests(unittest.TestCase):
    def setUp(self):
        from tests import test_patrol_flow as flow
        self.flow = flow.FlowTests('test_live_posts_exactly_once_oldest_first_one_per_run')
        self.flow.setUp()

    def tearDown(self):
        self.flow.tearDown()

    def test_one_unwritable_request_is_held_and_the_next_goes_on(self):
        from riri.errors import StatusMissing
        f = self.flow
        real = f.notion.set_status

        def set_status(block_id, *a, **k):
            if block_id == 'request-1':
                raise StatusMissing('Status line is missing or ambiguous')
            return real(block_id, *a, **k)
        f.notion.set_status = set_status
        f.add_request(1)
        f.add_request(2)
        for _ in range(3):
            f.run_once(live=True)
            f.clock.advance()
        state = f.state(live=True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'held')
        self.assertEqual(state['requests']['💛20990101-1']['hold_reason'], 'status_unwritable')
        self.assertTrue(any('💛20990101-2' in p[1] for p in f.slack.posts))       # 2件目は届く
        self.assertFalse(any('依頼ID：💛20990101-1' in p[1] for p in f.slack.posts))
        notices = [k for k in state['outbox'] if k.startswith('status-unwritable:')]
        self.assertEqual(notices, ['status-unwritable:💛20990101-1'])           # 知らせは1回
