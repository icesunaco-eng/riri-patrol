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

from riri.mini_run import (RunStore, receipt_state, unfinished_runs, child_env,
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
         'launch_mark': 't', 'end_evidence': {'exit_code': 1, 'mates_empty': True}},
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


class RunMatesTests(unittest.TestCase):
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
