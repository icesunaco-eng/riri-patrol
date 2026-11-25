"""みにの見張り役と正式な結果（riri/mini_supervisor.py）。設計 v1.0 の 4-3・4-4・5-1、完成基準 8/10/13/17/25/29。

本物の Codex は使わず、同じ形の JSONL を出す偽の codex で試す。
"""
import json
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
        # ps が1回失敗する・SIGTERM が来ると、見張り役が落ちて、孫が見張りなしで残った
        import subprocess as sp
        from unittest.mock import patch
        from riri import mini_run
        real = sp.run
        for kind in ('oserror', 'systemexit'):
            self.run_id = 'test-' + uuid.uuid4().hex[:12]
            state = {'done': False, 'ps': 0}

            def run(cmd, *a, **k):
                if cmd[:1] == ['ps'] and 'pid=,pgid=,args=' in cmd:       # 刈り取りの仲間の一覧（ループの中では呼ばない）
                    state['ps'] += 1
                    if state['ps'] == 1:
                        if kind == 'oserror':
                            raise OSError(12, 'Cannot allocate memory')
                        raise SystemExit(143)
                return real(cmd, *a, **k)
            with patch.object(mini_run.subprocess, 'run', run):
                if kind == 'systemexit':
                    with self.assertRaises(SystemExit):
                        self.go('grandchild')
                    rec = self.store.read(self.run_id)
                else:
                    rec, _ = self.go('grandchild')
            self.assertTrue(rec['end_evidence']['mates_empty'], kind)      # 刈り取りを終えて証拠を書いた
            self.assertEqual(run_mates(self.run_id, None), set(), kind)

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
