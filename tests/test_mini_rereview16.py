"""再レビュー 💚20261125-16 の指摘 T1〜T8。どれも、仮のホーム・偽の Notion・偽の Codex だけで通す
（本物の依頼・本番の Notion・本物の起動は使わない）。"""
import json
import os
from pathlib import Path
from unittest.mock import patch

from riri import mini_lane as lane
from tests.test_mini_lane import Base, FakeMailbox, req


class T1LaunchSnapshotTests(Base):
    """T1：起動の直前に、版・専用ホーム・モデルを1つのスナップショットにまとめて確かめる。"""

    def tick(self, *rs):
        return lane.tick(self.patrol, list(rs), mailbox=FakeMailbox(), popen=self.popen)

    def reserved_run(self):
        r = self.take(req())
        self.tick(r)
        return self.store().all_runs()[0]['run_id']

    def assert_not_launched(self, run, reason):
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], reason)

    def test_second_version_query_that_differs_stops_the_launch(self):
        run = self.reserved_run()
        answers = iter([{'codex': 'codex-cli 0.169.0', 'codex_exit': 0, 'macos': '27.2.1'},
                        {'codex': 'codex-cli 0.170.0', 'codex_exit': 0, 'macos': '27.2.1'}])
        with patch.object(lane, 'versions', lambda cfg: next(answers)):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assert_not_launched(run, 'version mismatch')

    def test_model_changed_while_materials_are_read_again_stops_the_launch(self):
        run = self.reserved_run()
        real_gather = lane.gather
        conf = self.home / '.codex-riri' / 'config.toml'

        def gather_and_change(*a, **k):
            out = real_gather(*a, **k)
            conf.write_text(conf.read_text().replace('model = "m"', 'model = "other"'))
            return out
        with patch.object(lane, 'gather', gather_and_change):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assert_not_launched(run, 'launch snapshot changed')

    def test_doc_bytes_changed_while_materials_are_read_again_stops_the_launch(self):
        run = self.reserved_run()
        real_gather = lane.gather
        conf = self.home / '.codex-riri' / 'config.toml'

        def gather_and_change(*a, **k):
            out = real_gather(*a, **k)
            conf.write_text(conf.read_text().replace('project_doc_max_bytes = 0', 'project_doc_max_bytes = 4096'))
            return out
        with patch.object(lane, 'gather', gather_and_change):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assert_not_launched(run, 'dedicated home')

    def test_empty_config_is_not_the_deployed_home(self):
        run = self.reserved_run()
        (self.home / '.codex-riri' / 'config.toml').write_text('')
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assert_not_launched(run, 'dedicated home')
        self.assertIn('config.toml:model:missing', lane.home_problems(self.cfg))

    def test_dedicated_home_moved_after_the_profile_was_built(self):
        run = self.reserved_run()
        moved = self.home / 'other-codex-home'
        moved.mkdir()
        for name in ('config.toml', 'AGENTS.md'):
            (moved / name).write_text((self.home / '.codex-riri' / name).read_text())
        env = self.root / '.env'
        env.write_text(env.read_text() + 'MINI_CODEX_HOME=' + str(moved) + '\n')
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assert_not_launched(run, 'dedicated home moved')

    def test_launch_keeps_the_snapshot_it_used(self):
        self.reserved_run()
        run = self.run_supervisor()
        rec = self.store().read(run)
        self.assertTrue(rec.get('launch_mark'))
        snap = rec['launch_snapshot']
        self.assertEqual(snap['versions']['codex'], 'codex-cli 0.169.0')
        self.assertEqual(snap['codex_home'], str(self.home / '.codex-riri'))
        self.assertTrue(all(snap['files'].values()))
        self.assertEqual(rec['versions'], snap['versions'])


class T2BrokenRunRecordTests(Base):
    """T2：JSON としては読めても、中身が食い違う run の記録は「壊れた記録」。新しく起こさず、ほかの run の報告は続ける。"""

    def tick(self, *rs):
        return lane.tick(self.patrol, list(rs), mailbox=FakeMailbox(), popen=self.popen)

    def plant(self, run_id, record):
        path = self.store().dir
        path.mkdir(parents=True, exist_ok=True)
        (path / (run_id + '.json')).write_text(json.dumps({'run_id': run_id, **record}, ensure_ascii=False))

    def test_launched_without_launch_mark_blocks_a_new_launch(self):
        self.plant('mini-' + 'a' * 20, {'request_id': '💚x', 'block_id': 'b', 'state': 'launched'})
        runs, broken = self.store().scan()
        self.assertEqual(broken, ['mini-' + 'a' * 20 + '.json'])
        r = self.take(req())
        self.tick(r)
        self.assertEqual(self.spawned, [])

    def test_child_given_as_text_does_not_crash_and_others_are_reported(self):
        # 先に正常な run を1つ最後まで通して、報告を待つ状態にする
        r1 = self.take(req())
        self.tick(r1)
        good = self.run_supervisor()
        self.plant('mini-' + 'b' * 20, {'request_id': '💚y', 'block_id': 'b2', 'state': 'launched',
                                        'launch_mark': '2026-11-25T00:00:00+09:00', 'child': '123',
                                        'end_unconfirmed': {'reason': 'x', 'at': 'y'}})
        box = FakeMailbox()
        out = lane.tick(self.patrol, [r1], mailbox=box, popen=self.popen)   # AttributeError で止まらない
        self.assertEqual(out.get('broken'), 1)
        self.assertEqual(box.appends, 1)                                    # 正常な run の報告は書く
        self.assertEqual(self.store().read(good)['report_state'], 'received')


def linked_block(text, url, block_id='request-1'):
    """依頼のコードブロック。「設計」の2文字だけ url へのリンク（表示は同じ）。"""
    head, tail = text.split('設計', 1)
    rich = [{'type': 'text', 'text': {'content': head}, 'plain_text': head},
            {'type': 'text', 'text': {'content': '設計', 'link': {'url': url}}, 'plain_text': '設計', 'href': url},
            {'type': 'text', 'text': {'content': tail}, 'plain_text': tail}]
    return {'id': block_id, 'type': 'code', 'code': {'rich_text': rich, 'language': 'plain text'}}


class T4RequestLinkTests(Base):
    """T4：依頼のブロックの中のリンク先だけが変わっても、起こす直前の照合で止まる。写しにリンク先が残る。
    Y7 の版ハッシュは設計どおりの区切りで、既知の値と一致する。"""

    def tick(self, *rs):
        return lane.tick(self.patrol, list(rs), mailbox=FakeMailbox(), popen=self.popen)

    def test_link_target_changed_under_the_same_words_stops_the_launch(self):
        from riri.parse import parse_requests
        from tests.test_mini_lane import request_text
        text = request_text()
        first = parse_requests([linked_block(text, 'https://example.com/a')])[0]
        lane.intake(self.patrol, first)
        self.notion.requests = [linked_block(text, 'https://example.com/a')]
        self.tick(first)
        run = self.store().all_runs()[0]['run_id']
        shown = (Path(self.store().read(run)['run_dir']) / 'input' / 'request.txt').read_text()
        self.assertIn('https://example.com/a', shown)                     # 写しにリンク先がある
        self.notion.requests = [linked_block(text, 'https://example.com/b')]   # 表示はそのまま、リンク先だけ変える
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'request recheck: materials')

    def test_same_link_launches(self):
        from riri.parse import parse_requests
        from tests.test_mini_lane import request_text
        text = request_text()
        first = parse_requests([linked_block(text, 'https://example.com/a')])[0]
        lane.intake(self.patrol, first)
        self.notion.requests = [linked_block(text, 'https://example.com/a')]
        self.tick(first)
        run = self.run_supervisor()
        self.assertTrue(self.store().read(run).get('launch_mark'))

    def test_y7_known_value(self):
        class N:
            def read_blocks(self, page_id):
                return [{'type': 'paragraph', 'id': 'x',
                         'paragraph': {'rich_text': [{'type': 'text', 'plain_text': '証拠', 'text': {'content': '証拠'}}]}}]
        self.assertTrue(lane._page_text(N(), 'p')[2].startswith('79b38b72'))


class Crash(BaseException):
    """プロセスがその場で止まったことの代わり（except Exception では捕まらない）。"""


class T5To8Tests(Base):
    def tick(self, *rs, popen=None):
        return lane.tick(self.patrol, list(rs), mailbox=FakeMailbox(), popen=popen or self.popen)

    # ---- T5
    def test_t5_hold_and_its_notice_are_saved_together(self):
        r = self.take(req())
        self.tick(r)
        run = self.store().all_runs()[0]['run_id']
        record = self.store().read(run)
        with patch.object(lane, 'notify', side_effect=Crash()):
            with self.assertRaises(Crash):
                lane.hold_report(self.cfg, self.store(), record, self.patrol, lane.now(), '理由')
        rec = self.store().read(run)
        self.assertEqual(rec['report_state'], 'held')
        self.assertEqual(rec['notices']['mini-report-hold:' + run]['state'], 'intent')   # 予定も残っている
        lane.resend_notices(self.patrol, self.store())                                   # 次の巡回で知らせる
        self.assertIn('mini-report-hold:' + run, self.patrol.state['outbox'])

    def test_t5_same_for_the_other_holds(self):
        r = self.take(req())
        self.tick(r)
        record = self.store().all_runs()[0]
        with patch.object(lane, 'notify', side_effect=Crash()):
            with self.assertRaises(Crash):
                lane.gate_failed(self.patrol, self.store(), record, 'hold', '理由', lane.now())
        rec = self.store().read(record['run_id'])
        self.assertEqual(rec['state'], 'held')
        self.assertEqual(rec['notices']['mini-held:' + record['run_id']]['state'], 'intent')

    # ---- T6
    def test_t6_supervisor_that_cannot_start_is_counted_and_the_next_goes_on(self):
        a, b = req(number=1), req(number=2)
        self.take(a, b)
        tried = []

        def popen(argv, *x, **k):
            tried.append(argv[-1])
            oldest = sorted(self.store().all_runs(), key=lambda r: r.get('reserved_at', ''))[0]['run_id']
            if argv[-1] == oldest:
                raise OSError('cannot start')
            return self.popen(argv, *x, **k)
        for _ in range(6):
            self.tick(a, b, popen=popen)
            if any(r.get('state') == 'spawning' for r in self.store().all_runs()):
                break
        states = sorted(r['state'] for r in self.store().all_runs())
        self.assertEqual(states, ['held', 'spawning'])                  # 最古は保留、次の予約が進む
        held = [r for r in self.store().all_runs() if r['state'] == 'held'][0]
        self.assertEqual(held['spawn_failures'], lane.SPAWN_FAIL_HOLD)
        self.assertIn('mini-held:' + held['run_id'], self.patrol.state['outbox'])   # 1回知らせる

    # ---- T7
    def test_t7_stop_keeps_the_interruption_even_when_the_answer_says_could_not(self):
        r = self.take(req())
        self.tick(r)
        run = self.run_supervisor()
        rec = self.store().read(run)
        for path in [Path(rec['stdout_path']), Path(rec['run_dir']) / 'out' / 'last.json',
                     *(self.home / '.codex-riri' / 'sessions').rglob('*.jsonl')]:
            path.write_text(path.read_text().replace('完了', 'できなかった'))
        rec = self.store().update(run, lambda x: {**x, 'outcome': None,
                                                   'end_evidence': {**x['end_evidence'], 'reason': 'stop'}})
        self.assertEqual(lane.outcome(self.cfg, rec)['label'], '一部完了（STOPで中断）')

    # ---- T8
    def test_t8_cancelled_shared_request_is_dropped_without_a_hold(self):
        r = req(recipients='うさ、みに')
        self.take(r)
        self.patrol.state['requests'][r.id] = {'status': 'cancelled', 'block_id': r.block_id}
        self.give(req(recipients='うさ、みに', status='対応中'))           # 人が原本を対応中に戻した
        self.tick(r)
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'dropped')
        self.assertEqual(self.spawned, [])
        self.assertFalse([k for k in self.patrol.state['outbox'] if k.startswith('mini-held:')])
