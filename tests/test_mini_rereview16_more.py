"""再レビュー 💚20261125-16 の T1〜T8 と同じ種類の残り（のあの詰め・2026-11-25 夜）。
仮のホーム・偽の Notion・偽の Codex だけで通す（本物の依頼・本番の Notion・本物の起動は使わない）。"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from riri import mini_lane as lane
from riri import mini_supervisor
from riri.errors import StopRequested
from tests.test_mini_lane import Base, FakeMailbox, PatrolHookTests, req, request_text

MODEL = 'gpt-6.2-sol'


def mention_block(text, words, page_id, block_id='request-1'):
    """依頼のコードブロック。words の所だけ Notion のページへのメンション（表示はページの題名）。"""
    head, tail = text.split(words, 1)
    rich = [{'type': 'text', 'text': {'content': head}, 'plain_text': head},
            {'type': 'mention', 'mention': {'type': 'page', 'page': {'id': page_id}}, 'plain_text': words},
            {'type': 'text', 'text': {'content': tail}, 'plain_text': tail}]
    return {'id': block_id, 'type': 'code', 'code': {'rich_text': rich, 'language': 'plain text'}}


class Helpers(Base):
    def tick(self, *rs, mailbox=None):
        return lane.tick(self.patrol, list(rs), mailbox=mailbox or FakeMailbox(), popen=self.popen)

    def spawned_run(self):
        r = self.take(req())
        self.tick(r)
        return self.store().all_runs()[0]['run_id']

    def plant(self, run_id, record):
        self.store().dir.mkdir(parents=True, exist_ok=True)
        (self.store().dir / (run_id + '.json')).write_text(json.dumps({'run_id': run_id, **record}, ensure_ascii=False))


class T7SameKindTests(Helpers):
    """T7 と同じ種類：結果の判定のどの道でも、STOP・時間切れのラベルは終了の証拠で決まる。"""

    def ended(self, reason):
        self.spawned_run()
        run = self.run_supervisor()
        return self.store().update(run, lambda x: {**x, 'outcome': None,
                                                   'end_evidence': {**x['end_evidence'], 'reason': reason}})

    def test_stop_label_when_the_output_is_too_large(self):
        rec = self.ended('stop')
        with patch.object(lane, 'STDOUT_LIMIT', 10):
            self.assertEqual(lane.outcome(self.cfg, rec)['label'], '一部完了（STOPで中断）')

    def test_timeout_label_when_the_output_cannot_be_read(self):
        rec = self.ended('timeout')
        Path(rec['stdout_path']).unlink()
        self.assertEqual(lane.outcome(self.cfg, rec)['label'], 'できなかった（時間切れ）')


class T6SameKindTests(Helpers):
    """T6 と同じ種類：見張り役が「wait」以外の理由で起こさなかったときも数える（0に戻さない）。"""

    def test_any_refusal_of_the_supervisor_is_counted(self):
        r = self.take(req())
        for _ in range(lane.RECHECK_WAIT_HOLD + 2):
            self.tick(r)
            rec = self.store().all_runs()[0]
            if rec['state'] != 'spawning':
                break
            # 見張り役の側でだけ見える理由で、毎回起こさずに終わる
            self.store().update(rec['run_id'], lambda x: {**x, 'not_started': {
                'reason': 'version mismatch', 'detail': '', 'at': lane.iso()}})
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'held')
        self.assertIn('version mismatch', rec['held_reason'])
        self.assertIn('mini-held:' + rec['run_id'], self.patrol.state['outbox'])


class T1SameKindTests(Helpers):
    """T1 と同じ種類：確かめたモデルだけで起こし、設定は起動の印の直前に読み直す。"""

    def test_another_model_is_not_the_deployed_home(self):
        conf = self.home / '.codex-riri' / 'config.toml'
        self.assertEqual(lane.home_problems(self.cfg), [])
        conf.write_text(conf.read_text().replace('model = "' + MODEL + '"', 'model = "other-model"'))
        self.assertIn('config.toml:model', lane.home_problems(self.cfg))

    def test_the_launch_pins_the_verified_model(self):
        run = self.spawned_run()
        seen = {}

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            seen.update(kw)
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda cfg, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 0)
        self.assertEqual(seen.get('model'), MODEL)

    def test_lane_switched_off_while_the_materials_are_read_again_stops_the_launch(self):
        run = self.spawned_run()
        real = lane.gather
        env = self.root / '.env'

        def gather_and_switch_off(*a, **k):
            out = real(*a, **k)
            env.write_text(env.read_text().replace('MINI_TRANSPORT=local', 'MINI_TRANSPORT=off'))
            return out
        with patch.object(lane, 'gather', gather_and_switch_off):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'lane switched off')


class T2SameKindTests(Helpers):
    """T2 と同じ種類：状態ごとに要る項目がない記録も「壊れた記録」。1件の失敗でほかの run を止めない。"""

    def test_record_without_request_id_does_not_stop_the_reports(self):
        r1 = self.take(req())
        self.tick(r1)
        good = self.run_supervisor()
        self.plant('mini-' + 'c' * 20, {'block_id': 'b', 'state': 'launched', 'launch_mark': 't'})
        box = FakeMailbox()
        out = self.tick(r1, mailbox=box)                                  # KeyError で止まらない
        self.assertEqual(out.get('broken'), 1)
        self.assertEqual(box.appends, 1)
        self.assertEqual(self.store().read(good)['report_state'], 'received')

    def test_required_fields_per_state(self):
        base = {'request_id': '💚x', 'block_id': 'b'}
        bad = [{**base, 'state': 'weird'}, {**base, 'state': 'reserved', 'launch_mark': 't'},
               {**base, 'state': 'finished'}, {**base, 'state': 'spawning'},
               {**base, 'state': 'spawning', 'spawn': {'pid': None, 'at': 'not a time'}},
               {'block_id': 'b', 'state': 'reserved'}, {**base, 'request_id': '', 'state': 'reserved'}]
        for record in bad:
            with self.subTest(record):
                self.assertIsNotNone(lane.lane_record_problem(record))
        good = [{**base, 'state': 'reserved'}, {**base, 'state': 'spawning', 'spawn': {'pid': None, 'at': lane.iso()}},
                {**base, 'state': 'launched', 'launch_mark': 't'}, {**base, 'state': 'finished', 'launch_mark': 't'},
                {**base, 'state': 'held'}, {**base, 'state': 'dropped'}]
        for record in good:
            with self.subTest(record):
                self.assertIsNone(lane.lane_record_problem(record))

    def test_a_reserved_record_with_a_launch_mark_is_never_launched_again(self):
        # 起動の印があるのに予約のままの記録は、もう一度起こされてしまう形なので「壊れた記録」
        run = 'mini-' + 'd' * 20
        self.plant(run, {'request_id': '💚x', 'block_id': 'b', 'state': 'reserved', 'launch_mark': 't',
                         'end_evidence': {'kind': 'launch_failed', 'error': 'OSError', 'at': 't'}})
        r = self.take(req())
        self.tick(r)
        self.assertEqual(self.spawned, [])

    def test_one_failing_hold_does_not_stop_the_others(self):
        for n in ('e', 'f'):
            self.plant('mini-' + n * 20, {'request_id': '💚' + n, 'block_id': n, 'state': 'launched',
                                          'launch_mark': 't'})
        real = lane.hold_and_notify
        calls = []

        def flaky(patrol, store, run, *a, **k):
            calls.append(run)
            if len(calls) == 1:
                raise RuntimeError('disk')
            return real(patrol, store, run, *a, **k)
        with patch.object(lane, 'hold_and_notify', flaky):
            self.tick()
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.store().read('mini-' + 'f' * 20).get('hold_notified'))


class T4SameKindTests(Helpers):
    """T4 と同じ種類：リンク先が分からないまま進めない。「使うもの」のメンション先も資料として読む。"""

    def test_unknown_request_links_do_not_count_as_no_links(self):
        record = {'block_id': 'never-read', 'fields': {}}
        with self.assertRaises(ValueError):
            lane.gather(self.cfg, record, request_text(), self.notion)

    def test_a_page_mentioned_in_use_is_a_material(self):
        from riri.parse import parse_requests
        other = '1' * 32
        self.notion.pages[other] = [{'id': 'u1', 'type': 'paragraph',
                                     'paragraph': {'rich_text': [{'type': 'text', 'plain_text': '承認済みの要件',
                                                                  'text': {'content': '承認済みの要件'}}]}}]
        text = request_text().replace('GitHubプロジェクト：なし\n', 'GitHubプロジェクト：なし\n使うもの：要件のページ\n')
        block = mention_block(text, '要件のページ', other)
        first = parse_requests([block])[0]
        self.assertEqual(first.fields['使うもの'], '要件のページ')         # 文字だけでは URL がない
        lane.intake(self.patrol, first)
        self.notion.requests = [block]
        self.tick(first)
        rec = self.store().all_runs()[0]
        self.assertIn('use_0', rec['materials'])
        self.assertIn('承認済みの要件', (Path(rec['run_dir']) / 'input' / 'use_0.txt').read_text())


class PatrolStopTests(Base):
    """STOP と巡回の時間切れは、みにの受け口の外側（巡回本体）でも飲み込まない。"""
    run_patrol = PatrolHookTests.run_patrol
    setup_patrol = PatrolHookTests.setup_patrol

    def test_stop_inside_the_mini_lane_stops_the_patrol(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ')
        with patch.object(lane, 'tick', side_effect=StopRequested('stopped')), \
                patch('riri.project_ops.return_results', side_effect=AssertionError('STOP のあとに続けた')):
            out = self.run_patrol(cfg, notion, slack, clock)
        self.assertEqual(out['status'], 'stopped')
