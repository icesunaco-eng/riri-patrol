"""みにの見張り役と正式な結果（riri/mini_supervisor.py）。設計 v1.0 の 4-3・4-4・5-1、完成基準 8/10/13/17/25/29。

本物の Codex は使わず、同じ形の JSONL を出す偽の codex で試す。
"""
import json
import signal
import os
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

from riri.mini_run import RunStore, run_mates, unfinished_runs
from riri.mini_supervisor import build_argv, supervise, formal_result, grade, final_signal, find_rollout, thread_id_of

FAKE = textwrap.dedent('''
    import json, subprocess, sys, time
    mode, run_id, last = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "noread":
        time.sleep(1.5)        # 入力を読まずに、しばらくして自然に終わる
        sys.exit(0)
    sys.stdin.read()
    answer = {"run_id": run_id, "final": True, "result": "完了", "summary": "ダミー"}
    def ev(**k): print(json.dumps(k, ensure_ascii=False), flush=True)
    ev(type="thread.started", thread_id="t-1")
    if mode == "grandchild":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    if mode == "sleep":
        time.sleep(60)
    if mode == "platform_session":
        # Codex のように、道具のコマンドを自分専用のセッションで動かす（Apple のプログラム）
        p = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        open(last + ".pid", "w").write(str(p.pid))
        time.sleep(1.5)
    if mode == "platform_session_wait":
        # 道具のコマンドを自分専用のセッションで起こして、子はまだ働いている
        p = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        open(last + ".pid", "w").write(str(p.pid))
        time.sleep(30)
    if mode == "platform_bg":
        # 短く動いたシェルが、背景に Apple のプログラムを残してすぐ終わる
        p = subprocess.Popen(["/bin/sh", "-c", "sleep 1; /bin/sleep 60 & echo $! > " + last + ".pid"], start_new_session=True)
        p.wait()
        time.sleep(1.0)
    ev(type="item.completed", item={"type": "agent_message", "text": json.dumps(answer, ensure_ascii=False)})
    ev(type="turn.completed")
    open(last, "w").write(json.dumps(answer, ensure_ascii=False))
    sys.exit(3 if mode == "fail" else 0)
''')


def validate(answer):
    return isinstance(answer.get('result'), str) and isinstance(answer.get('summary'), str)


class SuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = RunStore(self.dir)
        self.run_id = 'test-' + uuid.uuid4().hex[:12]
        (self.dir / 'fake.py').write_text(FAKE, encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, mode, timeout=20, stop=lambda: False, argv=None):
        last = self.dir / 'last.json'
        argv = argv or [sys.executable, str(self.dir / 'fake.py'), mode, self.run_id, str(last)]
        rec = supervise(self.store, self.run_id, argv, os.environ, '依頼文'.encode(),
                        self.dir / 'out.jsonl', self.dir / 'err.txt',
                        timeout=timeout, stop_check=stop, grace=3, poll=0.05,
                        extra_on_launch={'request_id': '💚1', 'block_id': 'b1', 'normalized_hash': 'h1'})
        return rec, last

    def test_normal_run_has_end_evidence_and_formal_answer(self):
        rec, last = self.go('ok')
        self.assertTrue(rec['launch_mark'])
        self.assertEqual(rec['end_evidence']['exit_code'], 0)
        self.assertTrue(rec['end_evidence']['mates_empty'])
        res = formal_result((self.dir / 'out.jsonl').read_text(), last.read_text(), self.run_id, validate, 0)
        self.assertTrue(res['formal'], res['reason'])
        self.assertEqual(unfinished_runs(self.store.all_runs()), [])

    def test_the_child_is_kept_with_its_start(self):
        rec, _ = self.go('ok')
        self.assertTrue(rec['child']['start'])          # 番号と一緒に子を名指す（保留の知らせ・record-end）

    def test_stop_before_launch_writes_no_launch_mark(self):
        rec = supervise(self.store, self.run_id, ['/nonexistent'], os.environ, b'', self.dir / 'o', self.dir / 'e',
                        timeout=5, stop_check=lambda: True)
        self.assertNotIn('launch_mark', rec)
        self.assertEqual(rec['not_started']['reason'], 'stop')

    def test_spawn_failure_is_end_evidence(self):
        # 完成基準17：起動失敗は「できなかった」になり、ロックを止めない
        rec, _ = self.go('ok', argv=[str(self.dir / 'no-such-codex')])
        self.assertEqual(rec['end_evidence']['kind'], 'launch_failed')
        self.assertEqual(unfinished_runs(self.store.all_runs()), [])

    def test_grandchild_in_another_session_is_reaped_before_end_evidence(self):
        # 完成基準25・29（A2・B1）
        rec, _ = self.go('grandchild')
        self.assertTrue(rec['end_evidence']['mates_empty'])
        self.assertEqual(run_mates(self.run_id, None), set())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS の Apple のプログラムで確かめる')
    def test_platform_program_in_its_own_session_is_reaped(self):
        # 「80% まで止まらずに」の回：Codex は道具のコマンドを自分専用のセッションで動かす。macOS 27 では
        # Apple のプログラム（/bin/sleep など）の環境変数は ps -E に出ないので、目印でも見つからず、
        # 子が終わったあとも動き続けていた（本物の STOP で sleep 120 が残った）
        self.check_reaped('platform_session')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS の Apple のプログラムで確かめる')
    def test_background_program_left_by_a_short_shell_is_reaped(self):
        self.check_reaped('platform_bg', sandboxed=True)

    def check_reaped(self, mode, sandboxed=False):
        last = self.dir / 'last.json'
        argv = [sys.executable, str(self.dir / 'fake.py'), mode, self.run_id, str(last)]
        if sandboxed:
            prof = self.dir / 'allow.sb'
            prof.write_text('(version 1)\n(allow default)\n', encoding='utf-8')
            argv = ['/usr/bin/sandbox-exec', '-f', str(prof)] + argv
        rec, _ = self.go(mode, argv=argv)
        left = int((self.dir / 'last.json.pid').read_text())
        try:
            alive = True
            os.kill(left, 0)
        except ProcessLookupError:
            alive = False
        finally:
            if alive:
                os.kill(left, 9)
        self.assertFalse(alive, 'the program the run started is still running')
        self.assertTrue(rec['end_evidence']['mates_empty'])

    def test_timeout_stops_the_group(self):
        rec, _ = self.go('sleep', timeout=0.5)
        self.assertEqual(rec['exit']['reason'], 'timeout')
        self.assertTrue(rec['end_evidence']['mates_empty'])

    def test_stop_during_run(self):
        started = time.monotonic()
        rec, _ = self.go('sleep', stop=lambda: time.monotonic() - started > 0.5)
        self.assertEqual(rec['exit']['reason'], 'stop')

    def test_stop_and_time_limit_apply_while_ps_does_not_return(self):
        # 「80% まで止まらずに」の回：見張り役は子の動いている間 0.5 秒ごとに ps を呼ぶ（B1 の子孫の記録）。
        # ps が返ってこないと、ループごと止まって STOP も時間の上限も効かなかった（上限なしで呼んでいた）
        import subprocess as sp
        from unittest.mock import patch
        from riri import mini_run
        real = sp.run
        stuck = [True]
        until = [0.0]

        def run(cmd, *a, **k):
            if time.monotonic() > until[0]:
                stuck[0] = False                         # 上限を過ぎたあとの刈り取りでは ps が返る
            if stuck[0] and cmd[:1] == ['ps'] and 'pid=,ppid=,lstart=' in cmd:
                if 'timeout' not in k:
                    time.sleep(30)                       # 上限がなければ返ってこない
                time.sleep(k['timeout'])
                raise sp.TimeoutExpired(cmd, k['timeout'])
            return real(cmd, *a, **k)
        for kind in ('stop', 'timeout'):
            started = time.monotonic()
            stop = (lambda: time.monotonic() - started > 0.5) if kind == 'stop' else (lambda: False)
            with patch.object(mini_run, 'PS_TIMEOUT', 0.3), patch.object(mini_run.subprocess, 'run', run):
                stuck[0] = True
                last = self.dir / 'last.json'
                argv = [sys.executable, str(self.dir / 'fake.py'), 'sleep', self.run_id, str(last)]

                def check():
                    if stop():
                        stuck[0] = False                 # 止めたあとの刈り取りでは ps が返る
                        return True
                    return False
                limit = 60 if kind == 'stop' else 1.0
                until[0] = started + limit
                rec = supervise(self.store, self.run_id, argv, os.environ, b'x', self.dir / 'out.jsonl',
                                self.dir / 'err.txt', timeout=limit, stop_check=check, grace=3, poll=0.05)
            took = time.monotonic() - started
            self.assertEqual(rec['exit']['reason'], kind)
            self.assertLess(took, 8)
            self.run_id = 'test-' + uuid.uuid4().hex[:12]

    def test_the_child_is_not_left_running_when_the_watcher_cannot_go_on(self):
        # 「80% まで止まらずに」の回：子を起こした直後の記録の書込み（容量不足・権限）や STOP の確かめ方が
        # 例外になると、見張り役だけ落ちて、子は時間の上限も STOP も効かないまま動き続けた
        from unittest.mock import patch
        from riri import mini_run
        real = mini_run.RunStore.update
        for where in ('child', 'stop'):
            self.run_id = 'test-' + uuid.uuid4().hex[:12]
            pids = []

            def update(store, run_id, change):
                new = change(dict(store.read(run_id) or {}))
                if 'child' in new and where == 'child':
                    pids.append(new['child']['pid'])
                    raise OSError(28, 'No space left on device')
                return real(store, run_id, change)
            started = time.monotonic()

            def stop():
                if where == 'stop' and time.monotonic() - started > 0.5:
                    raise PermissionError(13, 'Permission denied')
                return False
            last = self.dir / 'last.json'
            mode = 'grandchild' if where == 'child' else 'sleep'     # 孫を残す／子が動き続ける
            argv = [sys.executable, str(self.dir / 'fake.py'), mode, self.run_id, str(last)]
            with patch.object(mini_run.RunStore, 'update', update):
                with self.assertRaises(OSError, msg=where):
                    supervise(self.store, self.run_id, argv, os.environ, b'x', self.dir / 'out.jsonl',
                              self.dir / 'err.txt', timeout=60, stop_check=stop, grace=2, poll=0.05)
            rec = self.store.read(self.run_id)
            self.assertTrue(rec['launch_mark'])                   # 起動の印はある
            self.assertNotIn('end_evidence', rec)                 # 終了の証拠はない → 保留になる
            self.assertEqual(run_mates(self.run_id, None), set(), where)   # 孫も含めて残っていない
            if where == 'child':
                with self.assertRaises(ProcessLookupError):
                    os.kill(pids[0], 0)

    def test_the_reap_goes_on_through_a_failed_listing_and_a_sigterm(self):
        # 「80% まで止まらずに」の回（見張り役の呼び出しを1回ずつ失敗させる総当たり）：子が終わったあとの刈り取りで
        # 一覧が1回失敗する（ps が返らない・失敗する）・SIGTERM が来ると、見張り役が落ちて、孫が見張りなしで残った。
        # どの OS でも同じに確かめるため、刈り取りそのものを1回だけ失敗させる
        import subprocess as sp
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        real = ms.reap_run_mates
        for kind in ('timeout', 'systemexit'):
            self.run_id = 'test-' + uuid.uuid4().hex[:12]
            calls = []

            def reap(*a, **k):
                calls.append(1)
                if len(calls) == 1:
                    if kind == 'timeout':
                        raise sp.TimeoutExpired(['ps'], 5)
                    raise SystemExit(143)
                return real(*a, **k)
            with patch.object(ms, 'reap_run_mates', reap):
                if kind == 'systemexit':
                    with self.assertRaises(SystemExit):
                        self.go('grandchild')
                    rec = self.store.read(self.run_id)
                else:
                    rec, _ = self.go('grandchild')
            self.assertEqual(len(calls), 2, kind)                          # やり直した
            self.assertTrue(rec['end_evidence']['mates_empty'], kind)      # 刈り取りを終えて証拠を書いた
            self.assertEqual(run_mates(self.run_id, None), set(), kind)

    def test_a_write_that_fails_once_is_written_again(self):
        # 「80% まで止まらずに」の回・11（厳しい判定の総当たり）：子の記録・終了の証拠の書込みが1回失敗すると、
        # うまくいった run が保留になり、人の記録で「できなかった」になっていた
        from unittest.mock import patch
        from riri import mini_run
        real = mini_run.RunStore.update
        for key in ('child', 'end_evidence'):
            self.run_id = 'test-' + uuid.uuid4().hex[:12]
            failed = []

            def update(store, run_id, change):
                new = change(dict(store.read(run_id) or {}))
                if key in new and not (store.read(run_id) or {}).get(key) and not failed:
                    failed.append(1)
                    raise OSError(28, 'No space left on device')
                return real(store, run_id, change)
            with patch.object(mini_run.RunStore, 'update', update):
                rec, _ = self.go('grandchild')
            self.assertEqual(failed, [1], key)
            self.assertTrue(rec['end_evidence']['mates_empty'], key)
            self.assertEqual(run_mates(self.run_id, None), set(), key)

    def test_ps_is_called_with_a_time_limit(self):
        import subprocess as sp
        from unittest.mock import patch
        from riri import mini_run
        seen = []
        real = sp.run

        def run(cmd, *a, **k):
            seen.append((cmd[:1], k.get('timeout')))
            return real(cmd, *a, **k)
        with patch.object(mini_run.subprocess, 'run', run):
            mini_run._process_table()
            mini_run.run_mates(self.run_id, None)
        self.assertTrue(seen)
        self.assertTrue(all(t == mini_run.PS_TIMEOUT for c, t in seen if c == ['ps']), seen)

    def big_input_run(self, timeout, stop):
        argv = [sys.executable, str(self.dir / 'fake.py'), 'noread', self.run_id, str(self.dir / 'last.json')]
        started = time.monotonic()
        rec = supervise(self.store, self.run_id, argv, os.environ, b'x' * (4 * 1024 * 1024),
                        self.dir / 'out.jsonl', self.dir / 'err.txt', timeout=timeout, stop_check=stop,
                        grace=3, poll=0.05)
        return rec, time.monotonic() - started

    def test_time_limit_applies_while_the_input_is_still_being_written(self):
        # レビュー R5：入力を読まない子に大きな入力を渡しても、時間の上限が効く
        rec, took = self.big_input_run(0.1, lambda: False)
        self.assertEqual(rec['exit']['reason'], 'timeout')
        self.assertLess(took, 1.2)
        self.assertTrue(rec['end_evidence']['mates_empty'])

    def test_stop_applies_while_the_input_is_still_being_written(self):
        started = time.monotonic()
        rec, took = self.big_input_run(60, lambda: time.monotonic() - started > 0.3)
        self.assertEqual(rec['exit']['reason'], 'stop')
        self.assertLess(took, 1.2)

    def test_mates_that_cannot_be_confirmed_leave_the_run_unfinished(self):
        # 完成基準8・17（Z2）：0を確かめられなければ終了の証拠を書かない
        last = self.dir / 'last.json'
        argv = [sys.executable, str(self.dir / 'fake.py'), 'ok', self.run_id, str(last)]
        rec = supervise(self.store, self.run_id, argv, os.environ, b'', self.dir / 'out.jsonl', self.dir / 'err.txt',
                        timeout=10, stop_check=lambda: False, grace=0.2, poll=0.05,
                        lister=lambda run_id, pgid: {999999})
        self.assertNotIn('end_evidence', rec)
        self.assertIn('end_unconfirmed', rec)
        self.assertEqual([r['run_id'] for r in unfinished_runs(self.store.all_runs())], [self.run_id])


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                       # a zombie answers kill(0): it is not running
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


class SigtermWindowTests(unittest.TestCase):
    """再検証 💚20261126-4 の V6：見張り役の SIGTERM は、子を起こした直後（Popen の中では子はあるのに PID が
    分からない）・子が終わってから刈り取るまで・片付けの途中に来ると、片付けを飛ばして見張り役だけ終わり、子や
    仲間が残った。本物の SIGTERM を自分に送り、見張り役のプロセスと同じ受け口（SystemExit に変える）で確かめる。"""

    def setUp(self):
        import signal
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = RunStore(self.dir)
        self.run_id = 'test-' + uuid.uuid4().hex[:12]
        (self.dir / 'fake.py').write_text(FAKE, encoding='utf-8')
        self.old = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):          # mini_lane.main の supervise と同じ
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)
        self.pids = []

    def tearDown(self):
        import signal
        signal.signal(signal.SIGTERM, self.old)
        for pid in self.pids + sorted(run_mates(self.run_id, None)):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.tmp.cleanup()

    def go(self, mode, timeout=30, stop=lambda: False):
        last = self.dir / 'last.json'
        argv = [sys.executable, str(self.dir / 'fake.py'), mode, self.run_id, str(last)]
        return supervise(self.store, self.run_id, argv, os.environ, b'x', self.dir / 'out.jsonl',
                         self.dir / 'err.txt', timeout=timeout, stop_check=stop, grace=2, poll=0.05)

    def popen_then_sigterm(self, before):
        import signal
        import subprocess as sp
        real = sp.Popen

        def popen(*a, **k):
            if before:
                os.kill(os.getpid(), signal.SIGTERM)       # Popen の途中（子ができる前後）に来る
            p = real(*a, **k)
            self.pids.append(p.pid)
            if not before:
                os.kill(os.getpid(), signal.SIGTERM)       # 子ができて、見張り役が PID を受け取る前に来る
            return p
        return popen

    def test_sigterm_at_the_spawn_stops_the_child_and_writes_no_end_evidence(self):
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        from riri.mini_lane import open_runs
        for before in (False, True):
            with self.subTest(before=before):
                self.run_id = 'test-' + uuid.uuid4().hex[:12]
                self.pids = []
                with patch.object(ms.subprocess, 'Popen', self.popen_then_sigterm(before)):
                    with self.assertRaises(SystemExit) as cm:
                        self.go('sleep')
                self.assertEqual(cm.exception.code, 128 + 15)
                self.assertTrue(self.pids)
                self.assertFalse(_alive(self.pids[0]), '子が見張りなしで残った')
                self.assertEqual(run_mates(self.run_id, None), set())
                rec = self.store.read(self.run_id)
                self.assertTrue(rec['launch_mark'])
                self.assertEqual(rec['child']['pid'], self.pids[0])      # 人の record-end が子を探せる
                self.assertNotIn('end_evidence', rec)                    # 終わりは見届けていない
                self.assertEqual([r['run_id'] for r in open_runs([rec])], [self.run_id])   # 後続は起きない

    def test_minis_v6_steps_leave_no_child_and_no_mark(self):
        # みにの V6 の再現手順そのまま：子孫の追跡器を作るところで SIGTERM と同じ例外を入れる。追跡器は起動の印
        # より前に作るので、子は起きず、印も残らない（次の巡回でやり直せる。人の手は要らない）
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        spawned = []
        with patch.object(ms, 'Descendants', side_effect=SystemExit(143)), \
                patch.object(ms.subprocess, 'Popen', lambda *a, **k: spawned.append(a)):
            with self.assertRaises(SystemExit):
                self.go('sleep')
        self.assertEqual(spawned, [])
        self.assertNotIn('launch_mark', self.store.read(self.run_id) or {})

    def test_sigterm_during_the_launch_mark_spawns_nothing(self):
        import signal
        from unittest.mock import patch
        from riri import mini_run
        from riri import mini_supervisor as ms
        real = mini_run.RunStore.update
        spawned = []

        def update(store, run_id, change):
            new = change(dict(store.read(run_id) or {}))
            if 'launch_mark' in new and 'child' not in new:
                os.kill(os.getpid(), signal.SIGTERM)       # 起動の印を書いている途中に来る
            return real(store, run_id, change)
        with patch.object(mini_run.RunStore, 'update', update), \
                patch.object(ms.subprocess, 'Popen', lambda *a, **k: spawned.append(a)):
            with self.assertRaises(SystemExit) as cm:
                self.go('sleep')
        self.assertEqual(cm.exception.code, 128 + 15)
        self.assertEqual(spawned, [])                      # 子は起こさない
        rec = self.store.read(self.run_id)
        self.assertTrue(rec['launch_mark'])                # 印は書けた → 終了の証拠なし＝保留（後続は起きない）
        self.assertNotIn('end_evidence', rec)

    def test_sigterm_between_the_childs_end_and_the_reap_still_reaps(self):
        import signal
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        real = ms._reap

        def reap(*a, **k):
            os.kill(os.getpid(), signal.SIGTERM)           # 子が終わり、刈り取りに入る前に来る
            return real(*a, **k)
        with patch.object(ms, '_reap', reap):
            with self.assertRaises(SystemExit):
                self.go('grandchild')                      # 孫を別のセッションに残して子は終わる
        self.assertEqual(run_mates(self.run_id, None), set(), '孫が見張りなしで残った')
        rec = self.store.read(self.run_id)
        self.assertTrue(rec['end_evidence']['mates_empty'])   # 0 を確かめてから書き、それから終わる

    def test_sigterm_while_the_end_evidence_is_written_lets_it_be_written(self):
        import signal
        from unittest.mock import patch
        from riri import mini_run
        real = mini_run.RunStore.update

        def update(store, run_id, change):
            new = change(dict(store.read(run_id) or {}))
            if 'exit' in new:
                os.kill(os.getpid(), signal.SIGTERM)       # 終了の記録を書いている途中に来る
            return real(store, run_id, change)
        with patch.object(mini_run.RunStore, 'update', update):
            with self.assertRaises(SystemExit):
                self.go('grandchild')
        self.assertEqual(run_mates(self.run_id, None), set())
        self.assertTrue(self.store.read(self.run_id)['end_evidence']['mates_empty'])

    def test_sigterm_while_the_child_runs_stops_it_promptly(self):
        import signal
        import threading
        started = time.monotonic()
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        with self.assertRaises(SystemExit):
            self.go('sleep')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(run_mates(self.run_id, None), set())
        self.assertNotIn('end_evidence', self.store.read(self.run_id))

    def test_a_second_sigterm_during_the_clean_up_does_not_cut_it_short(self):
        import signal
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        real = ms._stop_group
        calls = []

        def stop_group(child, grace, *rest):
            calls.append(child.pid)
            os.kill(os.getpid(), signal.SIGTERM)           # 片付けの途中にもう一度来る
            return real(child, grace, *rest)
        started = time.monotonic()

        def stop():
            if time.monotonic() - started > 0.5:
                os.kill(os.getpid(), signal.SIGTERM)       # 1回目
            return False
        with patch.object(ms, '_stop_group', stop_group):
            with self.assertRaises(SystemExit):
                self.go('sleep', stop=stop)
        self.assertTrue(calls)
        self.assertFalse(_alive(calls[0]))
        self.assertEqual(run_mates(self.run_id, None), set())

    def test_the_group_of_a_child_already_waited_for_gets_no_signal(self):
        # V5 と同じ種類（のあが同じ穴を探して見つけた）：待ち終わった子の番号は、グループの仲間がいなければ別の
        # プロセスに渡りうる。そのあとは、合図の前に1つずつ確かめる刈り取りに任せる
        from unittest.mock import Mock, patch
        from riri import mini_supervisor as ms
        child = Mock(pid=424242, returncode=0)
        with patch.object(ms.os, 'killpg') as killpg:
            ms._stop_group(child, 0.1)
        killpg.assert_not_called()
        child = Mock(pid=424242, returncode=None)
        child.wait.side_effect = lambda timeout: setattr(child, 'returncode', -15)
        with patch.object(ms.os, 'killpg') as killpg:
            ms._stop_group(child, 0.1)
        # 生きている（待っていない）子のグループには送る（SIGTERM と、止めていたときのための SIGCONT）
        self.assertEqual([c.args[1] for c in killpg.call_args_list], [signal.SIGTERM, signal.SIGCONT])

    def test_a_handler_not_set_from_python_is_put_back_as_the_default(self):
        import signal
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        hold = ms._SigtermHold()
        with patch.object(ms.signal, 'getsignal', lambda sig: None):
            hold.start()
        hold.release(deliver=False)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        hold.start()
        hold.noted = signal.SIGTERM
        with self.assertRaises(SystemExit) as cm:   # 既定の動き（すぐ終わる）の代わりに、片付けを通して終わる
            hold.act()
        self.assertEqual(cm.exception.code, 128 + signal.SIGTERM)
        hold.release(deliver=False)

    def test_a_sigterm_noted_while_acting_on_none_is_kept(self):
        # 書き留めた合図を受けるとき、何もなければ書き戻さない（その間に来た合図を消さない）
        import signal
        from riri import mini_supervisor as ms
        hold = ms._SigtermHold()
        hold.start()
        try:
            hold.act()                            # 何も書き留めていない：何もしない
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            self.assertEqual(hold.noted, signal.SIGTERM)
            with self.assertRaises(SystemExit):
                hold.act()
            self.assertIsNone(hold.noted)
        finally:
            hold.release(deliver=False)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS の Apple のプログラム（環境の目印が見えない）で確かめる')
    def test_a_command_started_after_the_last_look_is_still_reaped(self):
        # 本物の Codex で見つけた（2026-11-27）：SIGTERM で止めるときは最後の一覧を取らずに子を止めていたので、
        # ふだんの一覧（0.5 秒ごと）のあとに始まった道具のコマンド（自分のセッションの /bin/sleep）が残った。
        # 止める前に子のグループを一時停止して最後の一覧を取る。ここではふだんの一覧を取らせずに確かめる
        import signal
        import threading
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        for how in ('sigterm', 'stop'):
            with self.subTest(how=how):
                self.run_id = 'test-' + uuid.uuid4().hex[:12]
                pidfile = self.dir / 'last.json.pid'
                if pidfile.exists():
                    pidfile.unlink()
                told = {'stop': False}

                def press():
                    for _ in range(200):
                        if pidfile.exists() and pidfile.read_text():
                            break
                        time.sleep(0.05)
                    if how == 'sigterm':
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        told['stop'] = True
                threading.Thread(target=press, daemon=True).start()
                with patch.object(ms, 'TRACK_EVERY', 10 ** 9):
                    if how == 'sigterm':
                        with self.assertRaises(SystemExit):
                            self.go('platform_session_wait')
                    else:
                        rec = self.go('platform_session_wait', stop=lambda: told['stop'])
                        self.assertEqual(rec['exit']['reason'], 'stop')
                        self.assertTrue(rec['end_evidence']['mates_empty'])
                left = int(pidfile.read_text())
                self.pids.append(left)
                time.sleep(0.2)
                self.assertFalse(_alive(left), how + ': 道具のコマンドが残った')
                self.assertEqual(run_mates(self.run_id, None), set())

    def test_a_failed_last_look_does_not_leave_the_group_stopped(self):
        from unittest.mock import Mock, patch
        from riri import mini_supervisor as ms
        child = Mock(pid=424242, returncode=None)
        child.wait.side_effect = lambda timeout: setattr(child, 'returncode', -15)

        def look():
            raise TypeError('an odd record')
        with patch.object(ms.os, 'killpg') as killpg:
            ms._stop_group(child, 0.1, look)
        self.assertEqual([c.args[1] for c in killpg.call_args_list],
                         [signal.SIGSTOP, signal.SIGTERM, signal.SIGCONT])

    def test_an_ignored_sigterm_is_still_ignored(self):
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        started = time.monotonic()

        def stop():
            if time.monotonic() - started > 0.3:
                os.kill(os.getpid(), signal.SIGTERM)
            return time.monotonic() - started > 0.8
        rec = self.go('sleep', stop=stop)
        self.assertEqual(rec['exit']['reason'], 'stop')
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)   # 受け口は元に戻る


class StopEvidenceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = RunStore(self.root)
        self.child = Mock(pid=424242, returncode=None)
        self.child.poll.return_value = None

        def wait(timeout=None):
            self.child.returncode = -signal.SIGTERM
            return self.child.returncode
        self.child.wait.side_effect = wait

    def go(self, *, stop_check=lambda: False, timeout=5):
        return supervise(self.store, 'run', ['fake-codex'], {}, b'', self.root / 'stdout', self.root / 'stderr',
                         timeout=timeout, stop_check=stop_check, lister=lambda run, pgid: set())

    def test_output_close_failure_after_spawn_leaves_no_launch_failure_evidence(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from riri import mini_supervisor as ms

        @contextmanager
        def broken_close(path, mode):
            with open(path, mode) as handle:
                yield handle
            if Path(path).name == 'stdout':
                raise OSError('failed to close output')

        with patch.object(ms, 'open', broken_close, create=True), \
                patch.object(ms.subprocess, 'Popen', return_value=self.child), \
                patch.object(ms, 'process_start', return_value='child start'), \
                patch.object(ms.Descendants, 'observe', return_value=False), \
                patch.object(ms.os, 'killpg') as killpg:
            with self.assertRaisesRegex(OSError, 'failed to close'):
                self.go()
        record = self.store.read('run')
        self.assertEqual(record['child']['pid'], self.child.pid)
        self.assertNotIn('end_evidence', record)
        self.assertIn(signal.SIGTERM, [call.args[1] for call in killpg.call_args_list])

    def test_a_failed_frozen_look_keeps_end_unconfirmed(self):
        from unittest.mock import patch
        from riri import mini_supervisor as ms
        stop_check = iter((False, True)).__next__
        with patch.object(ms.subprocess, 'Popen', return_value=self.child), \
                patch.object(ms, 'process_start', return_value='child start'), \
                patch.object(ms.Descendants, 'observe', side_effect=(False, OSError('process table unavailable'))), \
                patch.object(ms, 'TRACK_EVERY', 0), patch.object(ms.os, 'killpg') as killpg:
            record = self.go(stop_check=stop_check)
        self.assertEqual(record['exit']['reason'], 'stop')
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'final process observation unavailable')
        self.assertEqual([call.args[1] for call in killpg.call_args_list],
                         [signal.SIGSTOP, signal.SIGTERM, signal.SIGCONT])

    def test_a_successful_frozen_look_keeps_normal_stop_evidence(self):
        from unittest.mock import patch
        from riri import mini_supervisor as ms

        def observe(tracker):
            self.assertEqual(tracker.root_start, 'child start')
            return False        # complete look, no descendants added

        from riri import mini_run
        stop_check = iter((False, True)).__next__
        # 模擬の子にも通し番号を持たせる（本物の子は読める。読めないと、どの止め方でも保留：再検証 W5 のあと）
        with patch.object(ms.subprocess, 'Popen', return_value=self.child), \
                patch.object(ms, 'process_start', return_value='child start'), \
                patch.object(mini_run, '_lineage', lambda pids: {p: (5000, 1) for p in pids if p == self.child.pid}), \
                patch.object(ms.Descendants, 'observe', observe), patch.object(ms.os, 'killpg'):
            record = self.go(stop_check=stop_check)
        self.assertEqual(record['end_evidence']['reason'], 'stop')
        self.assertNotIn('end_unconfirmed', record)


def events(*evs):
    return '\n'.join(json.dumps(e, ensure_ascii=False) for e in evs)


class FormalResultTests(unittest.TestCase):
    ANS = {'run_id': 'r1', 'final': True, 'result': '完了', 'summary': 's'}

    def msg(self, ans):
        return {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(ans, ensure_ascii=False)}}

    def check(self, out, last=None, code=0):
        return formal_result(out, last, 'r1', validate, code)

    def test_all_five_checks(self):
        out = events({'type': 'thread.started'}, self.msg(self.ANS), {'type': 'turn.completed'})
        self.assertTrue(self.check(out, json.dumps(self.ANS))['formal'])

    def test_each_check_fails(self):
        # 完成基準10（X2・Y2）
        good = events({'type': 'thread.started'}, self.msg(self.ANS), {'type': 'turn.completed'})
        self.assertFalse(self.check(good, code=1)['formal'])
        self.assertFalse(self.check(events(self.msg(self.ANS)))['formal'])  # turn.completed なし
        tool_after = events(self.msg(self.ANS), {'type': 'item.completed', 'item': {'type': 'command_execution'}},
                            {'type': 'turn.completed'})
        self.assertFalse(self.check(tool_after)['formal'])
        self.assertFalse(self.check(events(self.msg({**self.ANS, 'run_id': 'r2'}), {'type': 'turn.completed'}))['formal'])
        self.assertFalse(self.check(events(self.msg({**self.ANS, 'final': False}), {'type': 'turn.completed'}))['formal'])
        self.assertFalse(self.check(good, json.dumps({**self.ANS, 'summary': '別'}))['formal'])
        self.assertFalse(self.check('not json\n' + good)['formal'])

    def test_item_after_the_last_turn_is_not_formal(self):
        # レビュー M4：A → turn.completed → B は、どちらも正式にしない
        other = {**self.ANS, 'summary': '別の本文'}
        out = events({'type': 'thread.started', 'thread_id': 't'}, self.msg(self.ANS), {'type': 'turn.completed'},
                     self.msg(other))
        res = self.check(out)
        self.assertFalse(res['formal'])
        self.assertIn('turn.completed のあと', res['reason'])
        tool = events(self.msg(self.ANS), {'type': 'turn.completed'}, {'type': 'item.started', 'item': {'type': 'command_execution'}})
        self.assertFalse(self.check(tool)['formal'])

    def test_odd_lines_are_not_formal_and_do_not_crash(self):
        # 再検証 💚20261125-8 S5：null・配列・type のない行で落ちずに「正式ではない」にする
        good = events({'type': 'thread.started'}, self.msg(self.ANS), {'type': 'turn.completed'})
        for odd in ('null', '[]', '1', '{"no_type": 1}', '"text"'):
            with self.subTest(odd):
                res = self.check(odd + '\n' + good)
                self.assertFalse(res['formal'])
                self.assertIn('形の違う行', res['reason'])

    def test_formal_text_is_the_answer_itself(self):
        out = events({'type': 'thread.started', 'thread_id': 't'}, self.msg(self.ANS), {'type': 'turn.completed'})
        res = self.check(out)
        self.assertEqual(res['text'], json.dumps(self.ANS, ensure_ascii=False))
        self.assertIsNone(self.check(out, code=1)['text'])

    def test_last_turn_is_used(self):
        first = {**self.ANS, 'summary': '途中'}
        out = events(self.msg(first), {'type': 'turn.completed'}, self.msg(self.ANS), {'type': 'turn.completed'})
        res = self.check(out)
        self.assertEqual(res['answer']['summary'], 's')

    def test_grade_never_completes_automatically_before_x2(self):
        self.assertEqual(grade(self.ANS, final_signal_confirmed=False, artifacts_confirmed=True),
                         '一部完了（最終の裏付けなし）')
        self.assertEqual(grade(self.ANS, final_signal_confirmed=True, artifacts_confirmed=False),
                         '一部完了（成果物を確かめられない）')
        self.assertEqual(grade(self.ANS, final_signal_confirmed=True, artifacts_confirmed=True), '完了')
        self.assertEqual(grade({**self.ANS, 'result': 'できなかった'}, final_signal_confirmed=False,
                               artifacts_confirmed=False), 'できなかった')


class BuildArgvTests(unittest.TestCase):
    def test_flags_before_resume(self):
        argv = build_argv('/c/codex', '/usr/bin/sandbox-exec', '/p.sb', '/run/work', ['/a', '/b'],
                          '/run/schema.json', '/run/out/last.json', resume_id='t-1')
        self.assertEqual(argv[:4], ['/usr/bin/sandbox-exec', '-f', '/p.sb', '/c/codex'])
        self.assertEqual(argv[-3:], ['resume', 't-1', '-'])
        self.assertLess(argv.index('--json'), argv.index('resume'))
        self.assertEqual(argv.count('--add-dir'), 2)
        self.assertIn('project_doc_max_bytes=0', argv)
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertEqual(build_argv('c', 's', 'p', 'w', [], 'j', 'l')[-1], '-')

    def test_apps_and_plugins_are_switched_off(self):
        # 2026-11-25 の実測：0.163 は外部アプリ・プラグインが既定で有効だった
        argv = build_argv('c', 's', 'p', 'w', [], 'j', 'l', resume_id='t-1')
        off = {argv[i + 1] for i, a in enumerate(argv) if a == '--disable'}
        self.assertTrue({'hooks', 'apps', 'plugins', 'remote_plugin', 'image_generation', 'multi_agent_v2'} <= off)
        self.assertNotIn('multi_agent', off)  # 0.169 で非推奨。毎回警告が出る
        self.assertIn('agents.enabled=false', argv)
        self.assertLess(max(i for i, a in enumerate(argv) if a == '--disable'), argv.index('resume'))


def _ri(payload):
    return json.dumps({'timestamp': 't', 'type': 'response_item', 'payload': payload}, ensure_ascii=False)


def _msg(role, text, phase=None):
    p = {'type': 'message', 'role': role, 'content': [{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': text}]}
    if phase:
        p['phase'] = phase
    return _ri(p)


class FinalSignalTests(unittest.TestCase):
    """2026-11-25 に本物（0.169.0・gpt-6.2-sol）で見た形を縮めたもの。途中の返事は commentary。"""
    ANSWER = '{"run_id":"m6","final":true,"result":"hello\\n"}'

    def rollout(self, *tail):
        head = [_msg('developer', '<permissions instructions>'), _msg('user', '依頼'),
                _ri({'type': 'reasoning', 'summary': []}),
                _msg('assistant', 'これから a.txt を読みます', 'commentary'),
                _ri({'type': 'custom_tool_call', 'name': 'exec', 'input': 'cat a.txt'}),
                _ri({'type': 'custom_tool_call_output', 'output': 'hello'})]
        return '\n'.join(head + list(tail)) + '\n'

    def test_thread_id_must_be_one(self):
        # レビュー M4：会話IDは1つに決まるときだけ使う
        ev = lambda *e: '\n'.join(json.dumps(x) for x in e)
        self.assertEqual(thread_id_of(ev({'type': 'thread.started', 'thread_id': 't-1'})), 't-1')
        self.assertIsNone(thread_id_of(ev({'type': 'thread.started', 'thread_id': 't-1'},
                                          {'type': 'thread.started', 'thread_id': 't-2'})))
        self.assertIsNone(thread_id_of(ev({'type': 'thread.started'})))
        self.assertIsNone(thread_id_of(ev({'type': 'turn.started'})))

    def test_the_final_message_must_be_the_assistants(self):
        # 再検証 💚20261125-8 S2：role のない返事は裏付けにしない
        no_role = _ri({'type': 'message', 'phase': 'final_answer',
                       'content': [{'type': 'output_text', 'text': self.ANSWER}]})
        self.assertFalse(final_signal(self.rollout(no_role), self.ANSWER))
        self.assertTrue(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'final_answer')), self.ANSWER))

    def test_no_answer_text_is_no_signal(self):
        self.assertFalse(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'final_answer')), None))

    def test_final_answer_phase_is_the_signal(self):
        self.assertTrue(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'final_answer')), self.ANSWER))

    def test_commentary_last_or_other_text_or_tool_after_is_not(self):
        self.assertFalse(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'commentary')), self.ANSWER))
        self.assertFalse(final_signal(self.rollout(_msg('assistant', self.ANSWER)), self.ANSWER))
        self.assertFalse(final_signal(self.rollout(_msg('assistant', self.ANSWER + ' ', 'final_answer')), self.ANSWER))
        self.assertFalse(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'final_answer'),
                                                   _ri({'type': 'custom_tool_call', 'input': 'rm x'})), self.ANSWER))
        self.assertFalse(final_signal('not json\n', self.ANSWER))

    def test_trailing_instructions_do_not_hide_the_answer(self):
        self.assertTrue(final_signal(self.rollout(_msg('assistant', self.ANSWER, 'final_answer'),
                                                  _ri({'type': 'reasoning'}), _msg('developer', 'x')), self.ANSWER))

    def test_find_rollout_and_thread(self):
        with tempfile.TemporaryDirectory() as home:
            d = Path(home) / 'sessions' / '2026' / '11' / '25'
            d.mkdir(parents=True)
            (d / 'rollout-2026-11-25T03-05-33-abc-1.jsonl').write_text('x')
            self.assertTrue(find_rollout(home, 'abc-1').endswith('abc-1.jsonl'))
            self.assertIsNone(find_rollout(home, 'zzz'))
            self.assertIsNone(find_rollout(home, '*'))
            (d / 'rollout-2026-11-25T03-06-00-abc-1.jsonl').write_text('y')
            self.assertIsNone(find_rollout(home, 'abc-1'))   # 2つあれば決めない
        self.assertEqual(thread_id_of('{"type":"thread.started","thread_id":"t-9"}\n'), 't-9')


if __name__ == '__main__':
    unittest.main()
