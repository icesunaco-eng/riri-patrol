"""みに起動の土台（riri/mini_run.py）。設計 v1.0 の P1・B1・B3・B6・A3・Z2、完成基準 25/26/29/31/33。"""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from riri.mini_run import (RunStore, _list_ps, end_evidence_counts, receipt_state, unfinished_runs, child_env,
                           run_mates, reap_run_mates)

ROOT = Path(__file__).resolve().parents[1]


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_creates_and_rereads(self):
        self.store.update('r1', lambda r: {**r, 'request_id': '💚1'})
        self.store.update('r1', lambda r: {**r, 'launch_mark': '2026-11-21T00:00:00Z'})
        rec = self.store.read('r1')
        self.assertEqual(rec['request_id'], '💚1')
        self.assertEqual(rec['launch_mark'], '2026-11-21T00:00:00Z')
        self.assertEqual(rec['run_id'], 'r1')
        self.assertEqual([r['run_id'] for r in self.store.all_runs()], ['r1'])

    def test_bad_run_id_is_rejected(self):
        for bad in ['../x', '', 'a/b', 'x' * 81, None]:
            with self.assertRaises(ValueError):
                self.store.update(bad, lambda r: r)

    def test_failed_change_leaves_the_old_record(self):
        self.store.update('r1', lambda r: {'n': 1})
        def boom(_):
            raise RuntimeError('止まった')
        with self.assertRaises(RuntimeError):
            self.store.update('r1', boom)
        self.assertEqual(self.store.read('r1')['n'], 1)
        self.assertEqual(list(self.store.dir.glob('.*.tmp')), [])

    def test_two_writers_at_once_keep_both_changes(self):
        # 完成基準31（B3）：ロックの中で読み直すので、片方の書込みが消えない
        script = textwrap.dedent('''
            import sys, time
            sys.path.insert(0, sys.argv[1])
            from riri.mini_run import RunStore
            key, delay = sys.argv[3], float(sys.argv[4])
            def change(r):
                time.sleep(delay)
                r[key] = True
                return r
            RunStore(sys.argv[2]).update('race', change)
        ''')
        a = subprocess.Popen([sys.executable, '-c', script, str(ROOT), self.tmp.name, '書込み予定', '0.4'])
        time.sleep(0.1)
        b = subprocess.Popen([sys.executable, '-c', script, str(ROOT), self.tmp.name, '結果', '0.0'])
        self.assertEqual((a.wait(10), b.wait(10)), (0, 0))
        rec = self.store.read('race')
        self.assertTrue(rec.get('書込み予定'))
        self.assertTrue(rec.get('結果'))


class DerivedStateTests(unittest.TestCase):
    RUNS = [
        {'run_id': 'a', 'request_id': '💚1', 'block_id': 'b1', 'normalized_hash': 'h1',
         'launch_mark': 't', 'end_evidence': {'kind': 'watched', 'exit_code': 1, 'reason': 'exited',
                                              'mates_empty': True, 'at': '2026-11-25T00:00:00+00:00'}},
        {'run_id': 'b', 'request_id': '💚2', 'block_id': 'b2', 'normalized_hash': 'h2'},
        {'run_id': 'c', 'request_id': '💚3', 'block_id': 'b3', 'normalized_hash': 'h3',
         'launch_mark': 't'},
    ]

    def test_receipt_state_from_records(self):
        # 完成基準26・33（A3・B6）
        self.assertEqual(receipt_state(self.RUNS, '💚1', 'b1', 'h1'), 'processed')
        self.assertEqual(receipt_state(self.RUNS, '💚1', 'b1', 'h9'), 'changed')
        self.assertEqual(receipt_state(self.RUNS, '💚2', 'b2', 'h2'), 'new')  # 起動の印なし
        self.assertEqual(receipt_state(self.RUNS, '💚9', 'b9', 'h9'), 'new')

    def test_unfinished_runs_need_end_evidence(self):
        # Z2：起動の印があって終了の証拠がないものだけ
        self.assertEqual([r['run_id'] for r in unfinished_runs(self.RUNS)], ['c'])

    def test_incomplete_end_evidence_is_still_unfinished(self):
        # 再検証 💚20261125-8 S7 と同じ種類：足りない証拠は終わりではない
        for ev in ({'exit_code': 0}, {'kind': 'watched', 'exit_code': 0, 'mates_empty': True},
                   {'kind': 'manual', 'by': 'のあ', 'mates_checked': True, 'at': 'x'}, 'done'):
            with self.subTest(ev):
                self.assertEqual(len(unfinished_runs([{'run_id': 'x', 'launch_mark': 't', 'end_evidence': ev}])), 1)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)
        self.store.update('good', lambda r: {'state': 'reserved'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_broken_files_are_named_and_the_rest_are_read(self):
        for name, content in (('bad', '{壊れた'), ('list', '[1]'), ('other', '{"run_id": "good"}')):
            (self.store.dir / (name + '.json')).write_text(content, encoding='utf-8')
        runs, broken = self.store.scan()
        self.assertEqual([r['run_id'] for r in runs], ['good'])
        self.assertEqual(broken, ['bad.json', 'list.json', 'other.json'])
        with self.assertRaises(ValueError):
            self.store.all_runs()                       # 全体が要る所は「分からない」として扱う

    def test_clean_store(self):
        self.assertEqual(self.store.scan()[1], [])
        self.assertEqual([r['run_id'] for r in self.store.all_runs()], ['good'])

    def test_watched_end_conflicts_with_live_mates_but_manual_resolution_does_not(self):
        stamp = '2026-11-25T00:00:00+00:00'
        record = self.store.update('finished', lambda r: {
            'state': 'finished', 'launch_mark': stamp, 'child': {'pid': 101, 'pgid': 101},
            'end_unconfirmed': {'reason': 'run mates still alive', 'at': stamp},
            'end_evidence': {'kind': 'watched', 'exit_code': 0, 'reason': 'exited',
                             'mates_empty': True, 'at': stamp},
        })
        self.assertEqual(self.store.scan()[1], ['finished.json'])
        self.assertFalse(end_evidence_counts(record))

        resolved = self.store.update('finished', lambda r: {
            **r, 'end_evidence': {'kind': 'manual', 'by': 'たかむね', 'mates_checked': True, 'at': stamp},
        })
        self.assertEqual(self.store.scan()[1], [])
        self.assertTrue(end_evidence_counts(resolved))


class RunMatesTests(unittest.TestCase):
    def test_macos_changed_arguments_do_not_turn_a_marker_into_a_signal(self):
        # 再検証 💚20261126-4 の V5（みにの試験を、のあが仲間の探し方の作り直しに合わせて書き直した）：前は ps -E
        # （環境つき）と ps（引数だけ）の2つの一覧を比べて、引数の後ろを環境と見ていた。2つの間に番号か引数が変わると、
        # 関係ないプロセスの「引数の中の目印」を環境と取り違えた。今は環境をカーネルから引数と別に読む（_environ_of）。
        run = 'test-arguments'
        groups = {123456: (101, 'Thu Nov 26 12:00:00 2026')}
        kill = Mock()
        # 目印の文字は引数にだけある（printf RIRI_MINI_RUN=...）。環境には目印がない
        with patch('riri.mini_run._ps_groups', lambda: groups), \
                patch('riri.mini_run._environ_of', lambda pid: [b'HOME=/Users/x']), \
                patch('riri.mini_run._same_session', lambda pid, leader: False):
            self.assertEqual(_list_ps(run, None), set())
            self.assertTrue(reap_run_mates(run, lister=_list_ps, kill=kill))
        kill.assert_not_called()
        with patch('riri.mini_run._ps_groups', lambda: groups), \
                patch('riri.mini_run._environ_of', lambda pid: []), \
                patch('riri.mini_run._same_session', lambda pid, leader: False):
            self.assertEqual(_list_ps(run, 101), {123456})  # the group independently places it in the run

    def test_the_marker_is_read_from_the_environment_part_only(self):
        # V5：KERN_PROCARGS2 の記録（argc・実行ファイル・詰め物・引数・環境・空の文字列・そのあとの apple の文字列）
        from riri.mini_run import _environ_strings
        def record(args, env, apple=()):
            body = b'/usr/bin/python3\0\0\0\0' + b''.join(a + b'\0' for a in args) + b''.join(e + b'\0' for e in env)
            body += b'\0' + b''.join(a + b'\0' for a in apple)
            return len(args).to_bytes(4, sys.byteorder) + body
        mark = b'RIRI_MINI_RUN=test-arguments'
        # 新しい引数が古い引数の頭と同じ（みにの2つ目の例）でも、引数の中の目印は環境に入らない
        self.assertEqual(_environ_strings(record([b'python3', b'x', mark], [b'HOME=/x'])), [b'HOME=/x'])
        self.assertEqual(_environ_strings(record([b'python3', b'', b'a'], [mark, b'B=1'])), [mark, b'B=1'])
        self.assertEqual(_environ_strings(record([b'sleep', b'5'], [], [mark])), [])   # 環境のあとの文字列は環境ではない
        self.assertIsNone(_environ_strings(b'\x01\x00'))

    def test_minis_second_example_gives_no_signal_either(self):
        # V5 のみにの2つ目の例（新しい引数が古い引数の頭と同じ）を、今の作りの入口から通す：関係ないプロセスの
        # 引数の後ろに目印の文字があっても、カーネルの記録では引数で、環境ではない。仲間にならず、合図もない
        from riri.mini_run import _environ_strings
        run = 'test-arguments'
        rec = (3).to_bytes(4, sys.byteorder) + b'/usr/bin/printf\0\0\0' + b'printf\0x\0RIRI_MINI_RUN=' + run.encode() \
            + b'\0HOME=/Users/x\0\0'
        groups = {123456: (777, 'Thu Nov 26 12:00:00 2026')}
        kill = Mock()
        with patch('riri.mini_run._ps_groups', lambda: groups), \
                patch('riri.mini_run._environ_of', lambda pid: _environ_strings(rec)), \
                patch('riri.mini_run._same_session', lambda pid, leader: False):
            self.assertEqual(_list_ps(run, 101), set())
            self.assertTrue(reap_run_mates(run, 101, lister=_list_ps, kill=kill))
        kill.assert_not_called()

    def test_ps_reads_starts_in_the_c_locale_and_utc(self):
        # 同じ種類を探して見つけた：ps の始まった時刻（lstart）は言語と時間帯で書き方が変わる（ja_JP「木 11/26 …」、
        # C「Thu Nov 26 …」）。見張り役が記録した時刻と、人の record-end（別の言語のターミナル）が読む時刻が食い違い、
        # 仲間を見落として終わりを記録しうる。どの ps も C・UTC で読む
        from riri import mini_run
        seen = []

        def ps(argv, **k):
            seen.append(k.get('env') or {})
            return subprocess.CompletedProcess(argv, 0, stdout='')
        with patch.object(mini_run.subprocess, 'run', ps):
            mini_run._ps_groups()
            mini_run._process_table()
            mini_run.process_start(os.getpid())
        self.assertEqual(len(seen), 3)
        for env in seen:
            self.assertEqual((env.get('LC_ALL'), env.get('TZ')), ('C', 'UTC'))

    def test_a_start_reads_the_same_in_any_language_and_time_zone(self):
        import re
        from riri import mini_run
        got = []
        for lang, tz in (('ja_JP.UTF-8', 'Asia/Tokyo'), ('fr_FR.UTF-8', 'America/New_York'), ('C', 'UTC')):
            with patch.dict(os.environ, {'LANG': lang, 'LC_ALL': lang, 'LC_TIME': lang, 'TZ': tz}):
                got.append((mini_run.process_start(os.getpid()), mini_run._process_table()[os.getpid()][1],
                            mini_run._ps_groups()[os.getpid()][1]))
        self.assertEqual(len({x for row in got for x in row}), 1, got)
        self.assertRegex(got[0][0], r'^[A-Z][a-z]{2} [A-Z][a-z]{2} +[0-9]+ [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}$')

    def test_a_tracked_program_is_still_found_from_a_terminal_in_another_language(self):
        # 同じ穴を本物のプロセスで：見張り役（C の言語）が子孫として記録した Apple のプログラム（自分のセッションの
        # /bin/sleep。環境の目印は見えない）を、見張り役が落ちたあとで、日本語のターミナルの record-end が探す。
        # 時刻の書き方が食い違うと見落とし、仲間が残っているのに終わりを記録しえた
        from riri.mini_run import Descendants
        run_id = 'test-' + uuid.uuid4().hex[:12]
        code = ('import subprocess, time\n'
                'p = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)\n'
                'print(p.pid, flush=True)\n'
                'time.sleep(60)\n')
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True,
                                 env=child_env(os.environ, run_id), start_new_session=True)
        left = int(child.stdout.readline())
        try:
            with patch.dict(os.environ, {'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC'}):
                tracker = Descendants(child.pid)
                tracker.observe()
                saved = tracker.as_record()                    # 見張り役が run の記録に残すもの
            self.assertIn(str(left), saved['pids'])
            child.kill()                                       # 見張り役も子も落ちて、sleep だけ残る
            child.wait()
            with patch.dict(os.environ, {'LANG': 'ja_JP.UTF-8', 'LC_ALL': 'ja_JP.UTF-8', 'LC_TIME': 'ja_JP.UTF-8',
                                         'TZ': 'Asia/Tokyo'}):
                found = run_mates(run_id, child.pid, Descendants.from_record(child.pid, saved))
            self.assertIn(left, found)
        finally:
            child.stdout.close()
            for pid in (left, child.pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            try:
                child.wait(timeout=5)
            except Exception:   # noqa: BLE001
                pass

    def test_a_ps_line_without_a_group_or_start_is_kept(self):
        from riri import mini_run
        out = '  101   101 Thu Nov 26 12:00:00 2026\n  102 x\n  103\n'
        with patch.object(mini_run.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=out)):
            self.assertEqual(mini_run._ps_groups(), {101: (101, 'Thu Nov 26 12:00:00 2026'), 102: (None, None),
                                                     103: (None, None)})

    def test_a_start_of_another_shape_is_a_broken_record(self):
        from riri.mini_run import record_problem
        self.assertEqual(record_problem({'supervisor': {'pid': 5, 'start': 7}}), 'supervisor.start')
        self.assertEqual(record_problem({'spawn': {'pid': 5, 'start': ['x']}}), 'spawn.start')
        self.assertIsNone(record_problem({'spawn': {'pid': 5, 'start': None}}))

    def test_a_number_that_changed_hands_before_the_signal_gets_no_signal(self):
        # V5：仲間として数えた番号が、合図の前に別のプロセスのものになった（始まった時刻が違う）ときは合図しない。
        # 終わりも見届けられないので、刈り取りは「空」と言わない（終了の証拠なし＝保留）
        from riri.mini_run import Mates
        listed = Mates({4242}, {4242: 'Thu Nov 26 12:00:00 2026'})
        kill = Mock()
        with patch('riri.mini_run._process_table', lambda: {4242: (1, 'Thu Nov 26 12:00:07 2026')}):
            self.assertFalse(reap_run_mates('test-arguments', lister=lambda r, g: listed, kill=kill,
                                            grace=0.2, poll=0.05))
        kill.assert_not_called()
        kill = Mock()
        with patch('riri.mini_run._process_table', lambda: {}):     # もういない
            self.assertFalse(reap_run_mates('test-arguments', lister=lambda r, g: listed, kill=kill,
                                            grace=0.2, poll=0.05))
        kill.assert_not_called()
        kill = Mock()
        with patch('riri.mini_run._process_table', lambda: {4242: (1, 'Thu Nov 26 12:00:00 2026')}):
            reap_run_mates('test-arguments', lister=lambda r, g: listed, kill=kill, grace=0.2, poll=0.05)
        self.assertEqual(kill.call_args_list[0].args, (4242, signal.SIGTERM))   # 同じプロセスには合図する

    def test_two_looks_that_disagree_on_a_start_give_no_signal(self):
        # V5：一覧（_list_ps）と同じ回の表（_process_table）で、同じ番号の始まった時刻が違う＝間で持ち主が変わった。
        # どちらのプロセスか分からないので、数えるが（終了の証拠なし）合図しない
        from riri import mini_run
        from riri.mini_run import Mates, still_the_same
        listed = Mates({4242}, {4242: 'Thu Nov 26 12:00:00 2026'})
        table = {4242: (1, 'Thu Nov 26 12:00:03 2026')}
        lister = mini_run._list_linux if os.path.isdir('/proc') and sys.platform.startswith('linux') else mini_run._list_ps
        with patch.object(mini_run, lister.__name__, lambda r, g: listed), \
                patch.object(mini_run, '_process_table', lambda: table):
            mates = run_mates('test-arguments', None)
            self.assertEqual(mates, {4242})
            self.assertIsNone(mates.starts[4242])
            self.assertEqual(still_the_same(mates), [])

    def test_a_marker_only_in_the_arguments_of_a_real_process_is_no_mate(self):
        # V5 を本物のプロセスで：環境に目印がある子だけが仲間。引数に目印の文字があるだけ（人の grep など）は仲間でない
        run_id = 'test-' + uuid.uuid4().hex[:12]
        sleeper = [sys.executable, '-c', 'import time; time.sleep(30)']
        mate = subprocess.Popen(sleeper, env=child_env(os.environ, run_id), start_new_session=True)
        other = subprocess.Popen(sleeper + ['RIRI_MINI_RUN=' + run_id], start_new_session=True)
        try:
            time.sleep(0.4)
            found = run_mates(run_id, None)
            self.assertIn(mate.pid, found)
            self.assertNotIn(other.pid, found)
            self.assertTrue(found.starts.get(mate.pid))
            self.assertTrue(reap_run_mates(run_id, grace=2, poll=0.05))
            self.assertIsNotNone(mate.wait(timeout=5))
            self.assertIsNone(other.poll())                 # 関係ないプロセスには合図していない
        finally:
            for p in (mate, other):
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def test_grandchild_in_another_session_is_found_and_stopped(self):
        # 完成基準29・25（B1・A2）：本体が終了0でも、別セッションの孫が残っていれば仲間
        run_id = 'test-' + uuid.uuid4().hex[:12]
        parent = textwrap.dedent('''
            import subprocess, sys
            subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             start_new_session=True)
        ''')
        child = subprocess.Popen([sys.executable, '-c', parent],
                                 env=child_env(os.environ, run_id), start_new_session=True)
        self.assertEqual(child.wait(10), 0)
        deadline = time.monotonic() + 5
        mates = set()
        while time.monotonic() < deadline and not mates:
            mates = run_mates(run_id, child.pid)
            time.sleep(0.1)
        self.assertTrue(mates, '別セッションの孫が見つからない')
        self.assertTrue(reap_run_mates(run_id, child.pid, grace=5))
        self.assertEqual(run_mates(run_id, child.pid), set())

    def test_unrelated_process_is_not_a_mate(self):
        run_id = 'test-' + uuid.uuid4().hex[:12]
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 env=child_env(os.environ, run_id + 'x'), start_new_session=True)
        try:
            time.sleep(0.3)
            self.assertNotIn(other.pid, run_mates(run_id, None))
        finally:
            other.kill()
            other.wait(5)

    def test_reap_reports_false_when_mates_never_go_away(self):
        sent = []
        ok = reap_run_mates('r', 1, grace=0.3, poll=0.05,
                            lister=lambda run_id, pgid: {4242},
                            kill=lambda pid, sig: sent.append(sig))
        self.assertFalse(ok)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])

    def test_reap_reports_false_on_permission_error(self):
        def kill(pid, sig):
            raise PermissionError
        self.assertFalse(reap_run_mates('r', 1, grace=0.1, lister=lambda r, p: {1}, kill=kill))

    def test_reap_true_when_already_empty(self):
        self.assertTrue(reap_run_mates('r', 1, lister=lambda r, p: set(), kill=lambda p, s: None))


if __name__ == '__main__':
    unittest.main()
