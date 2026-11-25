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
