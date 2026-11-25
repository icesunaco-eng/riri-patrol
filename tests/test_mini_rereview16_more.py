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
from tests import test_mini_lane
from riri.parse import parse_requests
from tests.test_mini_lane import Base, FakeMailbox, req, request_text

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


class FailurePathTests(Helpers):
    """足した仕組みそのものが失敗したとき（直しが働かない道）も、止まる側で、理由が残る。"""

    def test_settings_that_cannot_be_read_again_stop_the_launch_with_a_reason(self):
        from riri.config import Config
        run = self.spawned_run()
        real = Config.load.__func__
        calls = []

        def load(cls, root):
            calls.append(root)
            if len(calls) == 2:                     # 起動の印の直前の読み直しだけが失敗する
                raise PermissionError('denied')
            return real(cls, root)
        with patch.object(Config, 'load', classmethod(load)):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual((rec['not_started']['reason'], rec['not_started']['detail']),
                         ('settings unreadable', 'PermissionError'))
        self.tick(self.store().read(run) and req())               # 次の巡回で数えて、もう一度試す
        self.assertEqual(self.store().read(run)['recheck_waits'], 1)

    def test_a_use_page_that_cannot_be_read_makes_the_run_report_only(self):
        from riri.parse import parse_requests
        text = request_text().replace('GitHubプロジェクト：なし\n', 'GitHubプロジェクト：なし\n使うもの：要件のページ\n')
        block = mention_block(text, '要件のページ', '2' * 32)
        real = self.notion.read_blocks

        def read_blocks(page_id):
            if page_id == '2' * 32:
                from riri.errors import RemoteError
                raise RemoteError('Notion API rejected response')        # 本物の連携が読めないページ（404 など）
            return real(page_id)
        self.notion.read_blocks = read_blocks
        first = parse_requests([block])[0]
        lane.intake(self.patrol, first)
        self.notion.requests = [block]
        self.tick(first)
        rec = self.store().all_runs()[0]
        self.assertIn('use_0', rec['missing_always'])
        self.assertEqual(rec['plan']['row'], 1)                    # 報告だけ（いつも必須の資料が欠けた）


class SilentRepeatTests(Helpers):
    """T6・T2 と同じ種類：1件の失敗を黙って毎回繰り返さない・ほかの予約を止めない。"""

    def test_a_handoff_that_keeps_failing_is_told_once(self):
        self.spawned_run()
        run = self.run_supervisor()
        real = self.patrol.queue

        def queue(key, *a, **k):
            if key.startswith('mini-handoff:'):
                raise RuntimeError('slack queue broken')
            return real(key, *a, **k)
        self.patrol.queue = queue
        for _ in range(lane.ANNOUNCE_FAIL_NOTICE + 2):
            self.tick(req())
        notices = [k for k in self.patrol.state['outbox'] if k.startswith('mini-announce:')]
        self.assertEqual(notices, ['mini-announce:' + run])                    # 1回だけ
        self.assertNotEqual(self.store().read(run).get('handoff_state'), 'done')   # やり直しは続ける

    def test_an_unexpected_error_in_the_recheck_holds_only_that_reservation(self):
        a, b = req(number=1), req(number=2)
        self.take(a, b)
        real = lane.check_request_safe

        def check(cfg, r):
            if r.id == a.id:
                raise TypeError('unexpected')
            return real(cfg, r)
        with patch.object(lane, 'check_request_safe', check):
            self.tick(a, b)
        states = {r['request_id']: r['state'] for r in self.store().all_runs()}
        self.assertEqual(states, {a.id: 'held', b.id: 'spawning'})
        self.assertEqual(len([k for k in self.patrol.state['outbox'] if k.startswith('mini-held:')]), 1)

    def test_riri_state_of_another_shape_does_not_crash(self):
        r = req(recipients='うさ、みに')
        self.take(r)
        self.patrol.state['requests'] = {r.id: ['not', 'a', 'dict']}
        self.tick(r)                                                          # 例外にならない
        self.patrol.state['requests'] = ['not', 'a', 'dict']
        self.tick(r)
        self.assertEqual(len(self.store().all_runs()), 1)


class NoticeAdviceTests(Helpers):
    """知らせの案内どおりにやれば片付く（案内に書いたコマンドそのもので）。"""

    def test_lost_run_notice_gives_the_record_end_command_that_clears_it(self):
        from riri.errors import SafetyError
        run = 'mini-' + 'e' * 20
        self.plant(run, {'request_id': '💚e', 'block_id': 'e', 'state': 'launched', 'launch_mark': lane.iso(),
                         'child': {'pid': 999999, 'pgid': 999999, 'at': 't'}})
        self.tick()
        text = self.patrol.state['outbox']['mini-hold:' + run]['text']
        args = text.split('python3 -m riri.mini_lane ')[1].split(' で記録')[0].split()
        self.assertEqual(args, ['record-end', run, '--by', 'たかむね'])
        lane.record_end(self.cfg, args[1], args[3], lister=lambda r, pgid: set())   # 案内どおり
        self.assertTrue(lane.ended(self.store().read(run)))
        r = self.take(req())
        self.tick(r)
        self.assertEqual(len(self.spawned), 1)                               # 次を起こせる


class BrokenNoticeAdviceTests(Helpers):
    def test_broken_record_notice_says_where_and_moving_it_away_resumes(self):
        self.allow_broken = True
        r = self.take(req())
        bad = self.store().dir / ('mini-' + 'f' * 20 + '.json')
        self.store().dir.mkdir(parents=True, exist_ok=True)
        bad.write_text('{壊れた', encoding='utf-8')
        self.tick(r)
        self.assertEqual(self.spawned, [])
        text = [v['text'] for k, v in self.patrol.state['outbox'].items() if k.startswith('mini-broken:')][0]
        self.assertIn('state/mini/runs', text)
        bad.rename(self.root / 'moved-away.json')                         # 案内どおり、別の場所へ移す
        self.tick(r)
        self.assertEqual(len(self.spawned), 1)


class RecordFuzzTests(Helpers):
    """T2 と同じ種類を総当たりで：run の記録のどの項目に型の違う値が入っても、巡回（tick）から例外が漏れない
    （漏れると巡回全体がエラーになる。数えるもの・時刻の型の違いで実際に漏れていた）。"""
    ODD = [None, True, 1.5, '1', '', [], {}, 10 ** 30, 'not-a-time']
    EXTRA = ['x4_mismatch', 'x4_changed', 'recheck_waits', 'recheck_wait_detail', 'spawn_failures', 'read_timeouts',
             'announce_failures', 'reserved_at', 'report_checked_at', 'report_attempt_at', 'report_try_at',
             'report_first_attempt_at', 'report_state', 'handoff_state', 'hold_notified', 'shared', 'materials',
             'plan', 'missing_always', 'missing_write', 'notices', 'outcome', 'report_body', 'report_sha256',
             'end_evidence', 'end_unconfirmed', 'not_started', 'supervisor', 'spawn', 'child', 'launch_mark',
             'fields', 'requester', 'recipients', 'normalized_hash', 'stage', 'state', 'codex_home', 'x4_notified']

    def leaks(self, bases, reqs):
        import copy
        found = []
        for base in bases:
            for key in sorted(set(base) | set(self.EXTRA)):
                if key == 'run_id':
                    continue
                for odd in self.ODD:
                    saved = {p.name: p.read_text() for p in self.store().dir.glob('*.json')}
                    rec = copy.deepcopy(base)
                    rec[key] = odd
                    (self.store().dir / (base['run_id'] + '.json')).write_text(json.dumps(rec, ensure_ascii=False))
                    try:
                        self.tick(*reqs)
                    except Exception as exc:   # noqa: BLE001
                        found.append((base['state'], key, repr(odd), type(exc).__name__))
                    finally:
                        for p in self.store().dir.glob('*.json'):
                            p.unlink()
                        for n, t in saved.items():
                            (self.store().dir / n).write_text(t)
        return found

    def test_no_exception_leaks_from_any_field(self):
        self.allow_broken = True
        r1 = self.take(req())
        self.tick(r1)
        self.run_supervisor()
        r2 = self.take(req(number=2))
        self.tick(r1, r2)
        self.assertEqual(self.leaks(self.store().all_runs(), [r1, r2]), [])       # 終わった run・起動中の run
        for p in self.store().dir.glob('*.json'):
            p.unlink()
        r3, r4 = self.take(req(number=3)), self.take(req(number=4))
        self.assertEqual(self.leaks(self.store().all_runs(), [r3, r4]), [])       # 予約だけ（列の先まで進む）


class RequestParseFuzzTests(Base):
    """全部の依頼の読み取り（parse_requests）は、リンク先・メンション先も取り出すようになった。
    元の読み取りが落ちない形（plain_text がある項目）なら、どんなリンク・メンションの形でも落ちない。"""

    def test_links_and_mentions_of_any_shape_do_not_break_the_parser(self):
        text = request_text()
        odd = [None, True, 1, 1.5, '', 'x', [], {}, ['a'], {'type': []}, {'type': {}}, {'type': 'page'},
               {'type': 'page', 'page': []}, {'type': 'page', 'page': {'id': []}}, {'url': 1}]
        for value in odd:
            for item in ({'type': 'mention', 'mention': value, 'plain_text': 'よ'},
                         {'type': 'text', 'text': {'content': 'よ'}, 'plain_text': 'よ', 'href': value},
                         {'type': 'text', 'text': {'content': 'よ', 'link': value}, 'plain_text': 'よ'},
                         {'type': 'text', 'text': {'content': 'よ', 'link': {'url': value}}, 'plain_text': 'よ'}):
                with self.subTest(item=item):
                    blk = {'id': 'request-1', 'type': 'code', 'code': {'rich_text': [
                        {'type': 'text', 'text': {'content': text}, 'plain_text': text}, item]}}
                    self.assertEqual(len(parse_requests([blk])), 1)


class SupervisorFuzzTests(Helpers):
    """起動に使う項目のどれかが型の違う値・別の値なら、見張り役は起動しない（総当たり）。
    出力先が true だと見張り役が自分の標準出力を開いて閉じ、'1' だとりりのフォルダにファイルを作っていた。"""
    KEYS = ('run_dir', 'stdout_path', 'stderr_path', 'plan', 'materials', 'frozen_sha256', 'prompt_sha256',
            'profile_sha256', 'codex_home', 'fields', 'block_id', 'request_id', 'normalized_hash',
            'missing_always', 'missing_write', 'spawn', 'state')
    ODD = [None, True, 1.5, '1', '', [], {}, 10 ** 30, 'not-a-time', '/tmp/elsewhere']

    def test_no_launch_with_an_odd_launch_field(self):
        import copy
        self.allow_broken = True
        run = self.spawned_run()
        path = self.store().dir / (run + '.json')
        base, saved = self.store().read(run), path.read_text()
        launched = []

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, '-c', 'pass']
        for key in self.KEYS:
            for odd in self.ODD:
                if odd == base.get(key):
                    continue                                   # 元と同じ値は、起動してよい
                rec = copy.deepcopy(base)
                rec[key] = odd
                path.write_text(json.dumps(rec, ensure_ascii=False))
                try:
                    with patch.object(mini_supervisor, 'build_argv', argv):
                        lane.run_supervisor(self.cfg, run, notion=self.notion)
                except Exception:   # noqa: BLE001 - refusing by an exception is also no launch
                    pass
                if json.loads(path.read_text()).get('launch_mark'):
                    launched.append((key, repr(odd)))
                path.write_text(saved)
        self.assertEqual(launched, [])
        self.assertFalse((self.root / '1').exists())


class T2SameKindTests(Helpers):
    """T2 と同じ種類：状態ごとに要る項目がない記録も「壊れた記録」。1件の失敗でほかの run を止めない。"""

    def test_record_without_request_id_does_not_stop_the_reports(self):
        self.allow_broken = True     # わざと壊れた記録を置く
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
               {'block_id': 'b', 'state': 'reserved'}, {**base, 'request_id': '', 'state': 'reserved'},
               {**base, 'state': 'spawning', 'spawn': {'pid': None, 'at': lane.iso()}},         # 起動に使う項目がない
               {**base, 'state': 'reserved', 'recheck_waits': '3'}, {**base, 'state': 'reserved', 'reserved_at': 5},
               {**base, 'state': 'reserved', 'shared': 'うさ'}, {**base, 'state': 'launched', 'launch_mark': 't',
                                                                  'stdout_path': True}]
        for record in bad:
            with self.subTest(record):
                self.assertIsNotNone(lane.lane_record_problem(record))
        spawning = {**base, 'state': 'spawning', 'spawn': {'pid': None, 'at': lane.iso()}, 'run_dir': '/r',
                    'stdout_path': '/o', 'stderr_path': '/e', 'prompt_sha256': 'p', 'profile_sha256': 'f',
                    'plan': {'stage': 1, 'write': []}, 'materials': {}, 'frozen_sha256': {}}
        good = [{**base, 'state': 'reserved'}, spawning,
                {**base, 'state': 'launched', 'launch_mark': 't'}, {**base, 'state': 'finished', 'launch_mark': 't'},
                {**base, 'state': 'held'}, {**base, 'state': 'dropped'}]
        for record in good:
            with self.subTest(record):
                self.assertIsNone(lane.lane_record_problem(record))

    def test_a_reserved_record_with_a_launch_mark_is_never_launched_again(self):
        self.allow_broken = True     # わざと壊れた記録を置く
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
    # 関数だけ借りる（クラスを名前で取り込むと、その試験がこのファイルでもう一度流れる）
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol

    def test_stop_inside_the_mini_lane_stops_the_patrol(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ')
        with patch.object(lane, 'tick', side_effect=StopRequested('stopped')), \
                patch('riri.project_ops.return_results', side_effect=AssertionError('STOP のあとに続けた')):
            out = self.run_patrol(cfg, notion, slack, clock)
        self.assertEqual(out['status'], 'stopped')

    def test_a_record_without_request_id_does_not_stop_reports_in_the_real_patrol(self):
        # 本物の入口（Patrol.run）から：壊れた記録があっても、ほかの run の報告は書いて読み戻す
        self.allow_broken = True
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        page = cfg.pages['みに'].split('/')[-1]
        notion.blocks[page] = [{'id': 'hdr', 'type': 'heading_1',
                                'heading_1': {'rich_text': [{'text': {'content': '報告一覧（新しい順）'}}]}}]

        def append(method, block_id, children=False, query=None, payload=None):
            blocks = notion.blocks[block_id]
            at = [b['id'] for b in blocks].index(payload['position']['after_block']['id'])
            for n, child in enumerate(payload['children']):
                blocks.insert(at + 1 + n, {**child, 'id': 'appended-%d' % n})
            return {'object': 'list'}
        notion._request = append
        self.run_patrol(cfg, notion, slack, clock)
        run = spawned[0]

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, run, notion=notion), 0)
        store = lane.store_of(cfg)
        (store.dir / ('mini-' + 'c' * 20 + '.json')).write_text(json.dumps(
            {'run_id': 'mini-' + 'c' * 20, 'block_id': 'b', 'state': 'launched', 'launch_mark': 't'}))
        clock.advance()
        out = self.run_patrol(cfg, notion, slack, clock)
        self.assertNotEqual(out['status'], 'error')
        self.assertEqual(store.read(run)['report_state'], 'received')
        self.assertTrue(any('みにの run の記録に読めないもの' in p[1] for p in slack.posts))

    def test_an_unwritable_status_at_the_handoff_is_held_once_in_the_real_patrol(self):
        # T3 と同じ種類：みにだけの依頼の受け渡し（みにの受け口が lane_only を呼ぶ所）で状態を書けないとき、
        # その依頼だけ保留して1回知らせる。受け口の中で例外を飲み込んで、毎回黙ってやり直さない
        from riri.errors import StatusMissing
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        tried = []
        real = notion.set_status

        def set_status(bid, original, status, **k):
            tried.append(status)
            if status == '対応中':
                raise StatusMissing('Status line is missing or ambiguous')
            return real(bid, original, status, **k)
        notion.set_status = set_status
        self.run_patrol(cfg, notion, slack, clock)
        run = spawned[0]

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, run, notion=notion), 0)
        for _ in range(3):
            clock.advance()
            self.run_patrol(cfg, notion, slack, clock)
        state = json.loads((self.root / 'state' / 'state.json').read_text(encoding='utf-8'))
        info = state['requests']['💛20990101-1']
        self.assertEqual((info['status'], info.get('hold_reason')), ('held', 'status_unwritable'))
        self.assertEqual(tried.count('対応中'), 1)                         # 毎回やり直さない
        self.assertEqual([k for k in state['outbox'] if k.startswith('status-unwritable:')],
                         ['status-unwritable:💛20990101-1'])
        self.assertEqual(lane.store_of(cfg).read(run)['handoff_state'], 'done')

    def test_the_time_limit_inside_the_mini_lane_stays_a_quiet_log(self):
        # のあの直しで一度悪くした所（直す前と同じ入力で比べて見つけた）：時間切れを巡回本体へ返すと、
        # 資料の大きいページのたびに巡回がエラーになって、エラーの知らせが出ていた
        from riri.errors import TimeBudgetExceeded
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ')
        with patch.object(lane, 'tick', side_effect=TimeBudgetExceeded('HTTP time budget exhausted')):
            out = self.run_patrol(cfg, notion, slack, clock)
        self.assertEqual(out['status'], 'ok')
        self.assertFalse(any('エラー' in p[1] for p in slack.posts), [p[1][:60] for p in slack.posts])
