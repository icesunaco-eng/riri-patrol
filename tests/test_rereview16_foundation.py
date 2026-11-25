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
# みにの再現と同じ形：本文（手順）の中に、行の頭が「状態：」の行がある（QUOTED は行の途中なので、
# 直す前の版でも書けてしまい、T3 の再現にならなかった）
QUOTED_LINE = QUOTED.replace('1. 前の報告の「状態：失敗」を読む\n', '1. 前の報告を読む\n状態：失敗（証拠の行）\n')


class T3StatusLineTests(unittest.TestCase):
    """T3：受付（P3）と Notion への書込みが、同じ「状態」の行を選ぶ。1件書けなくても巡回は止まらない。"""

    def test_parse_and_the_writer_pick_the_same_line(self):
        from riri.notion import NotionClient
        from riri.parse import locate_status_line, parse_requests
        from tests.test_patrol_flow import block
        r = parse_requests([block(QUOTED_LINE, 'request-1')])[0]
        self.assertEqual(r.status, '未着手')
        seen = []

        class Transport:
            def request(self, method, url, **kw):
                seen.append(method)
                return {'object': 'block', 'id': 'request-1', 'type': 'code', 'in_trash': False,
                        'last_edited_time': '2026-11-25T00:00:00.000Z',
                        'code': {'rich_text': [{'type': 'text', 'text': {'content': QUOTED_LINE}, 'plain_text': QUOTED_LINE}]}}
        NotionClient('t', Transport()).set_status('request-1', QUOTED_LINE, '対応中', dry_run=True)
        self.assertEqual(seen, ['GET'])                                    # 書き込みは試さない（dry_run）
        from riri.notion import status_matches
        self.assertEqual([m.start() for m in status_matches(QUOTED_LINE)], [locate_status_line(QUOTED_LINE).start()])


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


class SameKindFoundationTests(unittest.TestCase):
    """T2・T3・T1 と同じ種類の残り（のあの詰め・2026-11-25 夜）。"""

    def test_writer_takes_exactly_the_p3_line(self):
        # P3 は「状態：」の行だけ（「状 態：」は本文）。書き込み側の正規表現は間に空白を許すので、
        # 受付は通るのに書き込みだけが2行と数えて止まっていた
        from riri.notion import status_matches
        from riri.parse import locate_status_line, parse_requests, validate_request
        from tests.test_patrol_flow import block
        text = QUOTED_LINE.replace('依頼した子：うさ\n', '依頼した子：うさ\n状 態：メモ（本文）\n', 1)
        r = parse_requests([block(text, 'request-1')])[0]
        validate_request(r)
        self.assertEqual(r.status, '未着手')
        self.assertEqual([m.start() for m in status_matches(text)], [locate_status_line(text).start()])

    def test_store_takes_a_check_for_the_lanes_own_fields(self):
        import tempfile
        from pathlib import Path
        from riri.mini_run import RunStore
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(Path(d), check=lambda r: None if 'request_id' in r else 'request_id')
            store.update('mini-' + 'a' * 20, lambda r: {'state': 'reserved', 'request_id': 'x'})
            store.update('mini-' + 'b' * 20, lambda r: {'state': 'reserved'})
            runs, broken = store.scan()
            self.assertEqual(([r['run_id'] for r in runs], broken), (['mini-' + 'a' * 20], ['mini-' + 'b' * 20 + '.json']))
            boom = RunStore(Path(d), check=lambda r: 1 / 0)               # 決められない検査は「壊れている」
            self.assertEqual(len(boom.scan()[1]), 2)

    def test_argv_pins_the_model(self):
        from riri.mini_supervisor import build_argv
        argv = build_argv('c', 's', 'p', 'w', [], 'j', 'l', resume_id='t-1', model='gpt-6.2-sol')
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.2-sol')
        self.assertLess(argv.index('-m'), argv.index('resume'))
        self.assertNotIn('-m', build_argv('c', 's', 'p', 'w', [], 'j', 'l'))


class T3RealWriteTests(unittest.TestCase):
    """T3（みにの再現と同じ形）：本物の NotionClient.set_status が GET→PATCH→読み戻しまで通り、
    状態欄だけが変わって証拠の「状態：」の行は残る。本物の Patrol でも、その依頼と次の依頼が進む。"""

    def setUp(self):
        from unittest.mock import patch
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def test_real_client_writes_only_the_status_field(self):
        from riri.notion import NotionClient
        from riri.parse import replace_status
        from tests.test_api import Fake, code_response
        expected = replace_status(QUOTED_LINE, '対応中')
        fake = Fake(code_response('request-1', QUOTED_LINE), code_response('request-1', expected),
                    code_response('request-1', expected))
        NotionClient('synthetic-notion-token', fake).set_status('request-1', QUOTED_LINE, '対応中')
        self.assertEqual([c[0] for c in fake.calls], ['GET', 'PATCH', 'GET'])
        sent = ''.join(x['text']['content'] for x in fake.calls[1][2]['payload']['code']['rich_text'])
        self.assertEqual(sent, expected)
        self.assertIn('状態：対応中', sent)
        self.assertIn('\n状態：失敗（証拠の行）\n', sent)                                   # 証拠の行は残る

    def test_real_patrol_takes_the_quoted_request_and_the_next(self):
        from tests import test_patrol_flow as flow
        from tests.test_patrol_flow import block
        f = flow.FlowTests('test_live_posts_exactly_once_oldest_first_one_per_run')
        f.setUp()
        self.addCleanup(f.tearDown)
        f.notion.blocks[f.cfg.requests_page].insert(0, block(QUOTED_LINE, 'request-1'))
        f.add_request(2)
        for _ in range(3):
            f.run_once(live=True)
            f.clock.advance()
        state = f.state(live=True)
        self.assertIn(('status', 'request-1', '対応中'), f.notion.writes)      # 書けた
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'posted')
        self.assertTrue(any('依頼ID：💛20990101-2' in p[1] for p in f.slack.posts))   # 次の依頼も進む
        self.assertFalse([k for k in state['outbox'] if k.startswith('status-unwritable:')])


class BlockTransport:
    """「りりへの依頼」のブロックを返す偽の通信。本物の NotionClient.set_status の
    GET・PATCH・読み戻しを、そのまま偽の Notion のブロックに当てる。"""

    def __init__(self, fake):
        self.fake, self.calls, self.version = fake, [], 0

    def request(self, method, url, headers=None, payload=None, timeout=None):
        bid = url.rsplit('/', 1)[-1]
        self.calls.append((method, bid))
        blk = next(b for b in self.fake.blocks[self.fake.config.requests_page] if b['id'] == bid)
        if method == 'PATCH':
            text = ''.join(x['text']['content'] for x in payload['code']['rich_text'])
            blk['code']['rich_text'] = [{'text': {'content': text}}]
            self.version += 1
        text = ''.join(x['text']['content'] for x in blk['code']['rich_text'])
        return {'object': 'block', 'id': bid, 'type': 'code', 'in_trash': False,
                'last_edited_time': '2099-01-01T00:00:%02d.000Z' % self.version,
                'code': {'rich_text': [{'type': 'text', 'text': {'content': text}}]}}


class T3ReviewerRunTests(unittest.TestCase):
    """T3 をみにの手順どおりに：本物の Patrol の中で本物の NotionClient.set_status を動かし、3巡回する
    （みにの再現では GET 3回・PATCH 0回で、無関係の2件目も受け付けられなかった）。"""

    def setUp(self):
        from tests import test_patrol_flow as flow
        self.f = flow.FlowTests('test_live_posts_exactly_once_oldest_first_one_per_run')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_three_patrols_with_the_real_client(self):
        from riri.notion import NotionClient
        from tests.test_patrol_flow import block
        f = self.f
        transport = BlockTransport(f.notion)
        f.notion.set_status = NotionClient('synthetic-notion-token', transport).set_status
        f.notion.blocks[f.cfg.requests_page].insert(0, block(QUOTED_LINE, 'request-1'))
        f.add_request(2)
        for _ in range(3):
            f.run_once(live=True)
            f.clock.advance()
        methods = [m for m, _ in transport.calls]
        self.assertGreaterEqual(methods.count('PATCH'), 2)                 # 2件とも 対応中 を書けた
        state = f.state(live=True)
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'posted')
        self.assertEqual(state['requests']['💛20990101-2']['status'], 'posted')
        text = ''.join(x['text']['content'] for x in f.notion.blocks[f.cfg.requests_page][1]['code']['rich_text'])
        self.assertIn('状態：対応中\n案件ページ', text)
        self.assertIn('\n状態：失敗（証拠の行）\n', text)                    # 証拠の行は残る


class _LanePatrol(unittest.TestCase):
    """まむの受け口の本物の巡回（test_agent_lane.PatrolLaneTest）を借りる部品。試験は持たない。"""

    def setUp(self):
        from tests import test_agent_lane as al
        self.t = al.PatrolLaneTest('test_lane_only_request')
        self.t.setUp()
        self.addCleanup(self.t.tearDown)

    def refuse(self, status_to_refuse, block_id='request-1'):
        from riri.errors import StatusMissing
        real = self.t.notion.set_status

        def set_status(bid, original, status, **k):
            if bid == block_id and status == status_to_refuse:
                raise StatusMissing('Status line is missing or ambiguous')
            return real(bid, original, status, **k)
        self.t.notion.set_status = set_status

    def other_request(self):
        from tests.test_patrol_flow import request
        self.t.notion.blocks[self.t.cfg.requests_page].insert(0, request(2))   # うさ宛ての別の依頼

    def notices(self, state):
        return [k for k in state['outbox'] if k.startswith('status-unwritable:')]

    def run_lane_only_handoff(self):
        from riri.state import StateStore
        t = self.t
        t.run_once()
        t.add()
        self.refuse('対応中')
        self.other_request()
        for _ in range(3):
            t.run_once()
            t.clock.advance()
        state = StateStore(t.root, 'live').load()
        self.assertEqual(state['requests']['🤍20990101-1']['status'], 'held')
        self.assertEqual(state['requests']['🤍20990101-1']['hold_reason'], 'status_unwritable')
        self.assertEqual(self.notices(state), ['status-unwritable:🤍20990101-1'])     # 知らせは1回
        self.assertEqual(state['requests']['💛20990101-2']['status'], 'posted')         # ほかは進む

    def run_completion(self):
        from riri.state import StateStore
        t = self.t
        t.run_once()
        t.add()
        t.run_once(); t.clock.advance()
        t.mamu_report()
        self.refuse('完了')
        self.other_request()
        for _ in range(3):
            t.run_once()
            t.clock.advance()
        state = StateStore(t.root, 'live').load()
        self.assertEqual(state['requests']['🤍20990101-1']['status'], 'held')
        self.assertEqual(state['requests']['🤍20990101-1']['hold_reason'], 'status_unwritable')
        self.assertNotIn(('status', 'request-1', '完了'), t.notion.writes)
        self.assertEqual(self.notices(state), ['status-unwritable:🤍20990101-1'])
        text = state['outbox']['status-unwritable:🤍20990101-1']['text']
        self.assertIn('手で「完了」', text)
        self.assertNotIn('新しい依頼ID', text)                              # 済んだ作業をやり直させない
        self.assertEqual(state['requests']['💛20990101-2']['status'], 'posted')


class T3OtherPathsTests(_LanePatrol):
    """状態を書けない依頼の保留（T3）を、準備のほかの2つの経路でも：
    まむ・みきだけの依頼の受け渡し（lane_only）と、報告がそろったあとの完了。"""

    def test_lane_only_handoff(self):
        self.run_lane_only_handoff()

    def test_completion(self):
        self.run_completion()


class T3ResolveTests(_LanePatrol):
    """状態を書けない保留は、保留の一覧に出て、人が解除できる（解除できないと、回復の知らせがずっと止まる）。"""

    def held(self, state, rid):
        from riri.resolve import held_items
        return [x for x in held_items(state) if x['id'] == rid]

    def test_completion_hold_is_listed_and_can_be_set_done(self):
        from dataclasses import replace
        from riri.errors import SafetyError
        from riri.resolve import resolve_held
        from riri.state import StateStore
        self.run_completion()
        t = self.t
        state = StateStore(t.root, 'live').load()
        self.assertEqual(self.held(state, '🤍20990101-1'),
                         [{'type': 'status', 'id': '🤍20990101-1', 'reason': 'status_unwritable',
                           'allowed_outcomes': ['done', 'cancel']}])
        with self.assertRaises(SafetyError):
            resolve_held(t.cfg, 'status', '💛20990101-2', 'done')            # 保留でない依頼は不可
        out = resolve_held(t.cfg, 'status', '🤍20990101-1', 'done')
        self.assertEqual(out['status'], 'resolved')
        state = StateStore(t.root, 'live').load()
        self.assertEqual(state['requests']['🤍20990101-1']['status'], 'done')
        self.assertFalse(any(x['status'] == 'held' for x in state['requests'].values()))
        self.assertEqual(self.held(state, '🤍20990101-1'), [])

    def test_start_hold_can_only_be_cancelled(self):
        from riri.errors import SafetyError
        from riri.resolve import resolve_held
        from riri.state import StateStore
        self.run_lane_only_handoff()
        t = self.t
        state = StateStore(t.root, 'live').load()
        self.assertEqual(self.held(state, '🤍20990101-1')[0]['allowed_outcomes'], ['cancel'])
        with self.assertRaises(SafetyError):
            resolve_held(t.cfg, 'status', '🤍20990101-1', 'done')            # 始まっていない依頼は完了にしない
        resolve_held(t.cfg, 'status', '🤍20990101-1', 'cancel')
        state = StateStore(t.root, 'live').load()
        self.assertEqual(state['requests']['🤍20990101-1']['status'], 'cancelled')
