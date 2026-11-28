"""再検証 💚20261127-3 の W1・W5：run のプロセスかどうかを、親が先に終わっても消えない記録で決める。

* W5：子が自然に終わる直前（見張り役の最後の一覧のあと）に始まった道具のコマンドが、自分専用のセッションで
  残ると、どの一覧にも出ず「終わった」と書いていた。macOS のプロセスは「生まれたときの親の通し番号」
  （p_puniqueid）を持ち、親が終わって launchd に渡っても消えないので、それで子の子孫をたどる。
* W1：子のグループ番号だけでつながるプロセスは、番号が別のグループに使い回されていることがある。
  通し番号で run のものと分かったときだけ合図し、run のものでないと分かったら数えない。分からないものは
  数えるが合図しない。
* 親が最後の一覧のあとに始まって見られずに終わったプロセスは、たどれない（unsure）。数えるが合図しない。
  run のサンドボックスに聞けるとき（子がそのサンドボックスにいると確かめた）は、それで決める。
"""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from riri import mini_run
from riri import mini_supervisor as ms
from riri.mini_run import Descendants, Mates, RunStore, run_mates, unfinished_runs
from tests import test_mini_lane

DARWIN = sys.platform == 'darwin'


@contextmanager
def world(lineage):
    """模擬のプロセスの世界：通し番号（_lineage）と全部のプロセスの番号（_all_pids）を、本物と同じく launchd（pid 1、
    通し番号 1）がいつもいる形で差し替える（3回目のレビュー：本物の一覧を読み、launchd のいない模擬では見落とした）。"""
    table = {1: (1, 0), **lineage}
    with patch.object(mini_run, '_lineage', lambda pids: {p: table[p] for p in pids if p in table}), \
            patch.object(mini_run, '_all_pids', lambda: set(table)):
        yield

FAKE = textwrap.dedent('''
    import subprocess, sys, time
    mode, pidfile, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])
    def wait_exec(path):
        for _ in range(200):
            try:
                pid = open(path).read().strip()
                if pid and subprocess.run(["ps", "-o", "comm=", "-p", pid], capture_output=True,
                                          text=True).stdout.strip() == "/bin/sleep":
                    return
            except OSError:
                pass
            time.sleep(0.025)
    sys.stdin.read()
    time.sleep(wait)          # 見張り役の一覧のあとまで待つ
    if mode == "direct":
        # 道具のコマンドを自分専用のセッションで起こして、子はすぐ自然に終わる（W5 そのもの）
        p = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        open(pidfile, "w").write(str(p.pid))
    elif mode == "via_shell":
        # 短いシェルが背景に Apple のプログラムを残して終わり、子もすぐ終わる（親が見られずに終わる）
        subprocess.run(["/bin/sh", "-c", "/bin/sleep 60 & echo $! > " + pidfile], start_new_session=True)
        time.sleep(0.05)
    elif mode in ("launchd_own", "launchd_group"):
        # シェルが背景を残してすぐ終わり、背景はシェルが終わってから exec する：カーネルは親を launchd と書く
        # （3回目のレビュー：シェルの「cmd &」でよく起きる形）。own は自分のセッション、group は子のセッションのまま
        subprocess.run(["/bin/sh", "-c", "(sleep 0.4; exec /bin/sleep 60) & echo $! > " + pidfile],
                       start_new_session=(mode == "launchd_own"))
        wait_exec(pidfile)       # 背景が exec するまで（親の番号が launchd になる）
    elif mode == "launchd_own_wait":
        subprocess.run(["/bin/sh", "-c", "(sleep 0.4; exec /bin/sleep 60) & echo $! > " + pidfile], start_new_session=True)
        wait_exec(pidfile)
        time.sleep(30)           # 子は止められるまで動く（STOP・時間切れ）
    sys.exit(0)
''')


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(DARWIN, 'macOS の通し番号とサンドボックスで確かめる')
class NaturalEndTests(unittest.TestCase):
    """本物のプロセスで：子が自然に終わる直前に始まったコマンド（W5）。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(os.path.realpath(temporary.name))
        self.store = RunStore(self.dir)
        self.run_id = 'test-' + uuid.uuid4().hex[:12]
        (self.dir / 'fake.py').write_text(FAKE, encoding='utf-8')
        self.run_dir = self.dir / 'run'
        for sub in ('work', 'out', 'input'):
            (self.run_dir / sub).mkdir(parents=True)
        self.pidfile = self.run_dir / 'work' / 'left.pid'
        self.addCleanup(self.kill_left)

    def kill_left(self):
        try:
            pid = int(self.pidfile.read_text())
        except (OSError, ValueError):
            return
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)

    def go(self, mode, wait, *, sandbox=False, run_dir=None):
        argv = [sys.executable, '-B', str(self.dir / 'fake.py'), mode, str(self.pidfile), str(wait)]
        if sandbox:
            profile = self.dir / 'run.sb'
            profile.write_text('(version 1)\n(allow default)\n(deny file-write*)\n'
                               f'(allow file-write* (subpath "{self.run_dir}/work") (subpath "{self.run_dir}/out") (regex #"^/dev/"))\n'
                               f'(deny file-write* (subpath "{self.run_dir}/input"))\n', encoding='utf-8')
            argv = ['/usr/bin/sandbox-exec', '-f', str(profile)] + argv
        return ms.supervise(self.store, self.run_id, argv, os.environ, b'', self.dir / 'out', self.dir / 'err',
                            timeout=20, stop_check=lambda: False, grace=1, poll=0.05,
                            run_dir=None if run_dir is None else str(run_dir))

    def left(self):
        return int(self.pidfile.read_text())

    def test_a_command_started_after_the_last_look_is_followed_and_reaped(self):
        # 一覧は始まってすぐの1回だけ。コマンドは子が親のうちに見られず、子が終わると launchd に渡る
        with patch.object(ms, 'TRACK_EVERY', 1000):
            record = self.go('direct', 0.6)
        left = self.left()
        time.sleep(0.2)
        self.assertFalse(_alive(left), '子が最後に起こしたコマンドが残った')
        self.assertEqual(record['end_evidence']['kind'], 'watched')
        self.assertTrue(record['end_evidence']['mates_empty'])
        self.assertIn(str(left), record['tracked']['pids'])      # record-end にも残る

    def test_a_program_whose_parent_ended_unseen_is_held_not_signalled(self):
        # シェルは最後の一覧のあとに始まって見られずに終わった：残った sleep は通し番号でたどれない。
        # run のものかもしれないので数えて保留にするが、関係ないプロセスかもしれないので合図しない
        with patch.object(ms, 'TRACK_EVERY', 1000):
            record = self.go('via_shell', 0.6)
        left = self.left()
        self.assertTrue(_alive(left), 'たどれないプロセスに合図した')
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'run mates still alive')
        self.assertIn(str(left), record['tracked']['unsure'])
        self.assertEqual(len(unfinished_runs(self.store.all_runs())), 1)

    def test_in_the_runs_sandbox_the_program_left_by_a_shell_is_reaped(self):
        # 本番と同じく子が run のサンドボックスにいるときは、サンドボックスがそのプロセスを run のものと決める
        with patch.object(ms, 'TRACK_EVERY', 0.3):
            record = self.go('via_shell', 0.8, sandbox=True, run_dir=self.run_dir)
        left = self.left()
        time.sleep(0.2)
        self.assertFalse(_alive(left), 'run のサンドボックスにいるプロセスが残った')
        self.assertTrue(record['tracked']['calibrated'])
        self.assertEqual(record['end_evidence']['kind'], 'watched')

    def test_an_unrelated_orphan_does_not_hold_a_run_whose_child_is_in_a_sandbox(self):
        # 2回目のレビュー：run と関係ない短いシェルが、見張りの最中に背景の sleep を残して終わる。run のサンドボックス
        # に聞けなくても（run_dir なし）、子が何かのサンドボックスにいれば、サンドボックスの外のそれは run のものでない
        profile = self.dir / 'loose.sb'
        profile.write_text('(version 1)\n(allow default)\n', encoding='utf-8')
        child = self.dir / 'child.py'
        child.write_text('import sys, time\nsys.stdin.read()\ntime.sleep(2.5)\n', encoding='utf-8')
        left = []

        def unrelated():
            time.sleep(1.0)
            out = subprocess.run(['/bin/sh', '-c', '(sleep 0.3; exec /bin/sleep 30) >/dev/null 2>&1 & echo $!'],
                                 capture_output=True, text=True, start_new_session=True).stdout.strip()
            left.append(int(out))
        import threading
        thread = threading.Thread(target=unrelated)
        thread.start()
        try:
            record = ms.supervise(self.store, self.run_id, ['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable,
                                                            '-B', str(child)], os.environ, b'', self.dir / 'out',
                                  self.dir / 'err', timeout=20, stop_check=lambda: False, grace=1, poll=0.05)
        finally:
            thread.join()
            for pid in left:
                alive = _alive(pid)
                if alive:
                    os.kill(pid, signal.SIGKILL)
        self.assertTrue(alive, '関係ないプロセスに合図した')
        self.assertTrue(record['tracked']['boxed'])
        self.assertEqual(record['end_evidence']['kind'], 'watched')
        self.assertNotIn(str(left[0]), record['tracked']['unsure'])

    def launchd_parent(self, left):
        self.assertEqual(mini_run._lineage([left])[left][1], mini_run.LAUNCHD_BIRTH)   # 場面どおり（親は launchd）

    def test_a_program_named_launchds_child_in_a_session_of_its_own_is_held(self):
        # 3回目のレビューの重大：launchd がいつも「生きている親」に数えられ、この形を判定しなかった（watched と書き、
        # sleep が残った）。自然な終わり・STOP・時間切れのどれでも、数えて保留にし、合図しない
        for mode, stop, timeout in (('launchd_own', None, 20), ('launchd_own_wait', 2.0, 20),
                                    ('launchd_own_wait', None, 2.0)):
            with self.subTest(mode=mode, stop=stop, timeout=timeout):
                self.run_id = 'test-' + uuid.uuid4().hex[:12]
                started = time.monotonic()
                argv = [sys.executable, '-B', str(self.dir / 'fake.py'), mode, str(self.pidfile), '0.6']
                with patch.object(ms, 'TRACK_EVERY', 1000):
                    record = ms.supervise(self.store, self.run_id, argv, os.environ, b'', self.dir / 'out',
                                          self.dir / 'err', timeout=timeout, grace=1, poll=0.05,
                                          stop_check=lambda: stop is not None and time.monotonic() - started > stop)
                left = self.left()
                self.launchd_parent(left)
                self.assertTrue(_alive(left), 'たどれないプロセスに合図した')
                self.assertNotIn('end_evidence', record)
                self.assertEqual(record['end_unconfirmed']['reason'], 'run mates still alive')
                self.assertIn(str(left), record['tracked']['unsure'])
                self.kill_left()

    def test_a_program_named_launchds_child_in_the_childs_session_is_reaped(self):
        # W1 の「本来の残存コマンドは回収する」：子のセッションに残ったもの（b9aa94f も回収していた）
        with patch.object(ms, 'TRACK_EVERY', 1000):
            record = self.go('launchd_group', 0.6)
        left = self.left()
        time.sleep(0.2)
        self.assertFalse(_alive(left), '子のセッションに残ったコマンドが残った')
        self.assertEqual(record['end_evidence']['kind'], 'watched')

    def test_in_the_runs_sandbox_a_program_named_launchds_child_is_reaped(self):
        with patch.object(ms, 'TRACK_EVERY', 0.3):
            record = self.go('launchd_own', 0.8, sandbox=True, run_dir=self.run_dir)
        left = self.left()
        time.sleep(0.2)
        self.assertFalse(_alive(left))
        self.assertTrue(record['tracked']['calibrated'])
        self.assertEqual(record['end_evidence']['kind'], 'watched')

    def test_the_runs_sandbox_is_confirmed_without_birth_numbers(self):
        # 4回目のレビュー：run の答え（work・out に書けて input に書けない）は終わったプロセスからは出ないので、
        # 通し番号が読めなくても較正する。較正すれば残ったコマンドはサンドボックスで見つかる
        with patch.object(mini_run, '_lineage', lambda pids: {}), patch.object(ms, 'TRACK_EVERY', 0.3):
            record = self.go('via_shell', 0.8, sandbox=True, run_dir=self.run_dir)
        left = self.left()
        time.sleep(0.2)
        self.assertFalse(_alive(left))
        self.assertTrue(record['tracked']['calibrated'])
        self.assertEqual(record['end_evidence']['kind'], 'watched')

    def test_a_child_started_in_a_sandbox_never_seen_in_the_runs_is_held(self):
        # 4回目のレビュー：run のサンドボックスで起こしたのに、子がそこにいると一度も確かめられなかった（別の決まりの
        # サンドボックス）：自分でセッションを作って親が終わってから exec したものは何でも見つからないので、保留
        profile = self.dir / 'loose.sb'
        profile.write_text('(version 1)\n(allow default)\n', encoding='utf-8')
        argv = ['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable, '-B', str(self.dir / 'fake.py'), 'ok',
                str(self.pidfile), '0.5']
        record = ms.supervise(self.store, self.run_id, argv, os.environ, b'', self.dir / 'out', self.dir / 'err',
                              timeout=20, stop_check=lambda: False, grace=1, poll=0.05, run_dir=str(self.run_dir))
        self.assertTrue(record['tracked']['boxed'])
        self.assertFalse(record['tracked']['calibrated'])
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'run sandbox not confirmed')

    def test_without_the_childs_birth_number_a_natural_end_is_not_confirmed(self):
        # 子の通し番号が読めない（ほかの道がない）ときは、最後の一覧のあとを追えないので終わりを確かめない
        with patch.object(mini_run, '_lineage', lambda pids: {}):
            record = self.go('direct', 0.2)
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'process lineage unavailable')


@unittest.skipUnless(DARWIN, 'macOS のサンドボックスで確かめる')
class SandboxStateTests(unittest.TestCase):
    """run のサンドボックスかどうかの見分け（libsandbox の sandbox_check）を本物で。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(os.path.realpath(temporary.name))
        for sub in ('work', 'out', 'input'):
            (self.dir / 'run' / sub).mkdir(parents=True)
        self.probes = mini_run.sandbox_probes(str(self.dir / 'run'))
        self.procs = []
        self.addCleanup(self.stop)

    def stop(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def start(self, profile=None):
        argv = ['/bin/sleep', '30']
        if profile is not None:
            path = self.dir / f'{len(self.procs)}.sb'
            path.write_text(profile, encoding='utf-8')
            argv = ['/usr/bin/sandbox-exec', '-f', str(path)] + argv
        p = subprocess.Popen(argv)
        self.procs.append(p)
        for _ in range(100):                     # sandbox-exec が sleep に exec するまで待つ
            comm = subprocess.run(['ps', '-o', 'comm=', '-p', str(p.pid)], capture_output=True, text=True).stdout.strip()
            if comm == '/bin/sleep':
                break
            time.sleep(0.05)
        return p.pid

    def test_the_runs_sandbox_another_sandbox_and_none(self):
        run = self.start('(version 1)\n(allow default)\n(deny file-write*)\n'
                         f'(allow file-write* (subpath "{self.dir}/run/work") (subpath "{self.dir}/run/out"))\n')
        work_only = self.start('(version 1)\n(allow default)\n(deny file-write*)\n'
                               f'(allow file-write* (subpath "{self.dir}/run/work"))\n')   # work だけを作業場所にした別の道具
        other_run = self.dir / 'other'
        for sub in ('work', 'out', 'input'):
            (other_run / sub).mkdir(parents=True)
        other = self.start('(version 1)\n(allow default)\n(deny file-write*)\n'
                           f'(allow file-write* (subpath "{other_run}/work"))\n')
        loose = self.start('(version 1)\n(allow default)\n')
        plain = self.start()
        self.assertEqual(mini_run.run_sandbox_state(run, self.probes), 'run')
        self.assertIsNone(mini_run.run_sandbox_state(work_only, self.probes))       # work だけ：run のフォルダが揺れた形と同じ
        self.assertEqual(mini_run.run_sandbox_state(other, self.probes), 'other')     # 別の run
        self.assertEqual(mini_run.run_sandbox_state(loose, self.probes), 'other')     # どこにでも書ける
        self.assertEqual(mini_run.run_sandbox_state(plain, self.probes), 'none')
        self.assertEqual(mini_run.run_sandbox_state(os.getpid(), self.probes), 'none')

    def test_a_process_that_ended_cannot_be_told(self):
        # libsandbox はいないプロセスにも「全部だめ」と答える：別のサンドボックスとは言わない
        p = subprocess.Popen(['/usr/bin/true'])
        p.wait()
        self.assertIsNone(mini_run.run_sandbox_state(p.pid, self.probes))

    def test_without_the_folders_nothing_is_told(self):
        self.assertIsNone(mini_run.sandbox_probes(str(self.dir / 'missing')))
        self.assertIsNone(mini_run.run_sandbox_state(os.getpid(), None))
        (self.dir / 'run' / 'out').rmdir()
        self.assertIsNone(mini_run.sandbox_probes(str(self.dir / 'run')))     # run が消した
        (self.dir / 'run' / 'out').symlink_to(self.dir / 'run' / 'input')
        self.assertIsNone(mini_run.sandbox_probes(str(self.dir / 'run')))     # run が差し替えた

    def lineage_after_the_parent_ended(self, code):
        pidfile = self.dir / 'run' / 'work' / 'left.pid'
        parent = subprocess.Popen([sys.executable, '-c', code, str(pidfile)], start_new_session=True)
        parent_birth = mini_run._lineage([parent.pid])[parent.pid][0]
        parent.wait()
        left = None
        for _ in range(100):                     # 期限つきで待つ（決まった秒数に頼らない）
            try:
                left = int(pidfile.read_text())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        self.addCleanup(lambda: left and _alive(left) and os.kill(left, signal.SIGKILL))
        return parent_birth, left

    def wait_for_exec(self, pid):
        for _ in range(100):
            comm = subprocess.run(['ps', '-o', 'comm=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            if comm == '/bin/sleep':
                return
            time.sleep(0.05)
        self.fail('exec が終わらなかった')

    def test_the_birth_number_outlives_the_parent(self):
        # 親が終わって launchd に渡っても、生まれたときの親の通し番号は残る（親のうちに exec した）
        parent_birth, left = self.lineage_after_the_parent_ended(textwrap.dedent('''
            import subprocess, sys, time
            p = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
            open(sys.argv[1], "w").write(str(p.pid))
            time.sleep(0.2)
        '''))
        self.wait_for_exec(left)
        birth, parent = mini_run._lineage([left])[left]
        self.assertEqual(parent, parent_birth)
        self.assertGreater(birth, parent_birth)

    def test_an_exec_after_the_parent_ended_names_launchd(self):
        # カーネルの癖：launchd に渡ってから exec すると、親の番号は launchd（1）になる。だからセッションでも見る
        parent_birth, left = self.lineage_after_the_parent_ended(textwrap.dedent('''
            import os, sys, time
            if os.fork() == 0:
                open(sys.argv[1], "w").write(str(os.getpid()))
                time.sleep(0.5)
                os.execv("/bin/sleep", ["/bin/sleep", "30"])
            os._exit(0)
        '''))
        self.wait_for_exec(left)
        self.assertEqual(mini_run._lineage([left])[left][1], mini_run.LAUNCHD_BIRTH)
        self.assertNotEqual(os.getsid(left), left)          # セッションは親（もういない）が作ったもののまま


class NumberOnlyTests(unittest.TestCase):
    """W1 を模擬で：子のグループ・セッションの番号だけでつながるプロセス。"""

    OLD, NEW = 'Thu Nov 26 12:00:00 2026', 'Thu Nov 26 12:00:07 2026'

    def tracker(self):
        t = Descendants(100, root_start=self.OLD)
        t.births, t.lineage, t.mark, t.live = {1000}, True, 1050, True
        return t

    def mates(self, tracker, lineage, listed, rows):
        lister = Mates(listed, {pid: self.NEW for pid in listed}, weak=listed)
        with patch.object(mini_run, '_list_ps', lambda run_id, pgid: lister), \
                patch.object(mini_run, '_list_linux', lambda run_id, pgid: lister), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                world(lineage), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid), \
                patch.object(mini_run, '_sandboxed', lambda pid: False):
            return run_mates('test-number', 100, tracker)

    def test_the_run_is_signalled_another_group_is_left_and_the_unknown_is_only_counted(self):
        rows = {201: (1, self.NEW), 202: (1, self.NEW), 203: (1, self.NEW)}
        lineage = {201: (1010, 1000),       # 子が起こした：run のもの
                   202: (1020, 900)}        # 子の前からある別の家系：番号が使い回された別のグループ
        found = self.mates(self.tracker(), lineage, {201, 202, 203}, rows)     # 203 は読めない
        self.assertEqual(found, {201, 203})
        self.assertEqual(found.starts[201], self.NEW)
        self.assertIsNone(found.starts[203])               # 数えるが合図しない
        with patch.object(mini_run, '_process_table', lambda: rows):
            self.assertEqual(mini_run.still_the_same(found), [201])

    def test_new_leader_ended_and_only_members_left(self):
        # みにの W1 の残り：新しいグループの頭も終わって、同じ番号の仲間だけが残る
        rows = {301: (1, self.NEW), 302: (1, self.NEW)}
        lineage = {301: (1030, 1025), 302: (1031, 1025)}   # 頭（1025）は子の家系でなく、もういない
        found = self.mates(self.tracker(), lineage, {301, 302}, rows)
        self.assertEqual(found, set())                      # 合図も数えもしない

    def test_without_the_childs_birth_number_the_number_alone_gets_no_signal(self):
        t = Descendants(100, root_start=self.OLD)
        t.lineage = False                                   # macOS で子の通し番号が読めなかった
        rows = {201: (1, self.NEW)}
        found = self.mates(t, {}, {201}, rows)
        self.assertEqual(found, {201})
        self.assertIsNone(found.starts[201])

    def test_a_person_record_end_counts_what_it_cannot_tell(self):
        t = Descendants.from_record(100, {'pids': {}, 'sessions': [], 'births': [1000]})
        self.assertIsNone(t.mark)                           # 人の record-end：見張り役の一覧はない
        rows = {202: (1, self.NEW)}
        found = self.mates(t, {202: (1020, 900)}, {202}, rows)
        self.assertEqual(found, {202})                      # 分からないので数える（record-end は断る）

    def test_without_birth_numbers_linux_keeps_the_number(self):
        t = Descendants(100, root_start=self.OLD)           # lineage None：通し番号を試していない
        rows = {201: (1, self.NEW)}
        found = self.mates(t, {}, {201}, rows)
        self.assertEqual(found.starts[201], self.NEW)


class FollowTests(unittest.TestCase):
    """通し番号のたどり方（模擬）。"""

    S = 'Thu Nov 26 12:00:00 2026'

    def follow(self, tracker, rows, lineage, sids):
        with world(lineage), \
                patch.object(mini_run.os, 'getsid', lambda pid: sids[pid]), \
                patch.object(mini_run, 'reuse_gap', lambda: 90899):
            return tracker.follow(rows, live=True)

    def tracker(self):
        t = Descendants(100)
        t.births, t.lineage, t.mark = {1000}, True, 1050
        t.sessions = {100}
        return t

    def test_children_of_the_run_are_the_runs_even_after_their_parent_ended(self):
        t = self.tracker()
        rows = {401: (1, self.S), 402: (1, self.S)}
        self.follow(t, rows, {401: (1060, 1000), 402: (1061, 1060)}, {401: 401, 402: 401})
        self.assertEqual(set(t.pids), {401, 402})
        self.assertEqual(t.mark, 1061)

    def test_an_orphan_of_another_terminal_is_not_unsure(self):
        # 関係ない端末の短いシェルの背景プロセス：親は見られずに終わったが、セッションの頭（端末）が生きていて run でない
        t = self.tracker()
        rows = {500: (1, self.S), 501: (1, self.S)}
        self.follow(t, rows, {500: (10, 1), 501: (1070, 1065)}, {500: 500, 501: 500})
        self.assertEqual((t.pids, t.unsure), ({}, {}))

    def test_an_orphan_in_one_of_the_runs_sessions_is_the_runs_only_while_its_number_cannot_be_reused(self):
        # 子のセッション（頭＝子はもういない）。番号が使い回されるには、そのセッションを作ったプロセス（ここでは子）が
        # 生まれてから、ほぼ全部の番号が配られる（通し番号で数えて reuse_gap 以上）必要がある
        t = self.tracker()
        rows = {601: (1, self.S)}
        self.follow(t, rows, {601: (1080, 1075)}, {601: 100})
        self.assertIn(601, t.pids)
        # 子が生まれてから多くのプロセスが生まれた（長い run の間に番号が一巡しうる）、または run のセッションと
        # 分かっていない番号なら決めない（2回目のレビュー：最後に見た一覧から数えるのは起点の誤り）
        for birth, sid in ((1000 + 90899 + 5, 100), (1080, 777)):
            t = self.tracker()
            t.mark = birth - 10
            self.follow(t, {601: (1, self.S)}, {601: (birth, birth - 2)}, {601: sid})
            self.assertNotIn(601, t.pids)
            self.assertIn(601, t.unsure)
        # 記録したセッションは、それを作った run のプロセスの通し番号から数える
        t = self.tracker()
        t.sessions, t.session_maker = {555}, {555: 50000}
        t.mark = 50000 + 90899 - 20
        self.follow(t, {602: (1, self.S)}, {602: (50000 + 90899 - 5, 50000 + 90899 - 6)}, {602: 555})
        self.assertIn(602, t.pids)

    def test_an_orphan_that_cannot_be_told_is_unsure_and_so_are_its_children(self):
        t = self.tracker()
        rows = {701: (1, self.S)}
        self.follow(t, rows, {701: (1090, 1085)}, {701: 701})       # 自分でセッションを作った
        self.assertEqual(t.unsure, {701: self.S})
        rows = {701: (1, self.S), 702: (701, self.S)}
        self.follow(t, rows, {701: (1090, 1085), 702: (1100, 1090)}, {701: 701, 702: 701})
        self.assertEqual(set(t.unsure), {701, 702})

    def test_a_process_named_launchds_child_is_judged_by_its_session(self):
        # launchd はいつも生きている（world に入れてある）。それでも親が launchd のものはセッションで決める
        for sid, expect in ((901, 'foreign'), (1, 'foreign'), (100, 'ours'), (555, 'unsure')):
            t = self.tracker()
            self.follow(t, {901: (1, self.S)}, {901: (1100, 1)}, {901: sid})
            got = 'ours' if 901 in t.pids else 'unsure' if 901 in t.unsure else 'foreign'
            self.assertEqual(got, expect, sid)

    def test_a_session_whose_maker_runs_tells(self):
        # セッションの頭が生きていれば、その頭が run のものかで決まる
        for maker, expect in (((1070, 1000), 'ours'), ((1040, 900), 'foreign')):    # run の子／最後の一覧より前からある別の家系
            t = self.tracker()
            self.follow(t, {950: (1, self.S), 951: (1, self.S)}, {950: maker, 951: (1100, 1095)}, {950: 950, 951: 950})
            got = 'ours' if 951 in t.pids else 'unsure' if 951 in t.unsure else 'foreign'
            self.assertEqual(got, expect)

    def test_what_the_sandbox_told_also_settles_the_children(self):
        # 4回目のレビュー：サンドボックスが「run のものでない」と決めたら、その通し番号を doubt から外す（子まで unsure にしない）
        t = self.tracker()
        t.unsure, t.doubt = {901: self.S}, {1110}
        t.calibrated, t.run_dir = True, '/run'
        rows = {901: (1, self.S)}
        with world({901: (1110, 1105)}), \
                patch.object(mini_run, 'sandbox_probes', lambda run_dir: (b'w', b'o', b'i')), \
                patch.object(mini_run, 'run_sandbox_state', lambda pid, probes: 'none'), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                patch.object(mini_run, '_list_ps', lambda run_id, pgid: Mates()), \
                patch.object(mini_run, '_list_linux', lambda run_id, pgid: Mates()), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid), \
                patch.object(mini_run, '_sandboxed', lambda pid: False):
            self.assertEqual(run_mates('test-told', 100, t), set())
        self.assertEqual((t.unsure, t.doubt, 1110 in t.foreign), ({}, set(), True))

    def test_a_sandbox_that_could_not_be_asked_once_stays_lost(self):
        # 4回目のレビュー：片付けの間に out が消え、あとで作り直されても、聞けなかったことは覚えている
        t = self.tracker()
        t.calibrated, t.run_dir = True, '/run'
        answers = iter([None, (b'w', b'o', b'i')])
        with patch.object(mini_run, 'sandbox_probes', lambda run_dir: next(answers)):
            self.assertIsNone(t.probes())
            self.assertTrue(t.sandbox_lost())

    def test_birth_numbers_from_another_start_of_the_mac_are_not_used(self):
        record = {'pids': {}, 'sessions': [], 'births': [1000], 'doubt': [1200], 'mark': 1300}
        with patch.object(mini_run, 'boot_time', lambda: 5000):
            other = Descendants.from_record(100, {**record, 'boot': 4000})
            same = Descendants.from_record(100, {**record, 'boot': 5000})
            unknown = Descendants.from_record(100, record)
        self.assertEqual((other.lineage, other.births, other.doubt, other.mark), (None, set(), set(), None))
        self.assertEqual((same.lineage, same.births, same.mark), (True, {1000}, 1300))
        self.assertEqual((unknown.lineage, unknown.births), (True, {1000}))      # 分からないときは使う（数えるだけ）

    def test_processes_older_than_the_last_look_are_not_unsure(self):
        t = self.tracker()
        rows = {801: (1, self.S)}
        self.follow(t, rows, {801: (1040, 1035)}, {801: 801})       # 最後の一覧より前に生まれた
        self.assertEqual(t.unsure, {})

    def test_unsure_is_told_by_the_runs_sandbox(self):
        t = self.tracker()
        t.unsure = {901: self.S, 902: self.S}
        t.doubt = {1110, 1111}
        t.calibrated, t.run_dir = True, '/run'
        rows = {901: (1, self.S), 902: (1, self.S)}
        states = {901: 'run', 902: 'none'}
        with patch.object(mini_run, 'sandbox_probes', lambda run_dir: (b'w', b'i')), \
                patch.object(mini_run, 'run_sandbox_state', lambda pid, probes: states.get(pid, 'none')), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                patch.object(mini_run, '_lineage', lambda pids: {}), \
                patch.object(mini_run, '_list_ps', lambda run_id, pgid: Mates()), \
                patch.object(mini_run, '_list_linux', lambda run_id, pgid: Mates()), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid), \
                patch.object(mini_run, '_sandboxed', lambda pid: True):
            found = run_mates('test-follow', 100, t)
        self.assertEqual(found, {901})
        self.assertEqual(found.starts[901], self.S)        # run のサンドボックス：合図してよい
        self.assertEqual((t.unsure, t.pids.get(901)), ({}, self.S))     # 記録にも、決まったほうで残る

    def test_the_record_keeps_births_unsure_and_the_sandbox_answer(self):
        t = self.tracker()
        t.unsure, t.calibrated, t.pids = {701: self.S}, True, {401: self.S}
        t.boxed = True
        back = Descendants.from_record(100, t.as_record(), run_dir='/run')
        self.assertEqual((back.births, back.unsure, back.calibrated, back.run_dir, back.lineage, back.mark,
                          back.boxed, back.live, back.root_done),
                         ({1000}, {701: self.S}, True, '/run', True, 1050, True, False, True))
        self.assertTrue(Descendants.from_record(100, {'births': ['x', 0, 5]}).births == {5})


class LaneTests(unittest.TestCase):
    def test_child_and_initial_birth_are_saved_before_the_first_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            store = RunStore(path)
            written = []
            save = store.update

            def capture(run_id, change):
                record = save(run_id, change)
                written.append(record)
                return record

            child = Mock(pid=401, returncode=0)
            child.poll.return_value = child.wait.return_value = 0
            with patch.object(store, 'update', capture), \
                    patch.object(ms.subprocess, 'Popen', return_value=child), \
                    patch.object(ms, 'process_start', return_value='child start'), \
                    patch.object(mini_run.sys, 'platform', 'darwin'), \
                    patch.object(mini_run, 'boot_time', return_value=5070), \
                    world({child.pid: (1000, 1)}):
                ms.supervise(store, 'test-initial-birth', ['fake-codex'], {}, b'', path / 'stdout', path / 'stderr',
                             timeout=2, stop_check=lambda: False, lister=lambda run_id, pgid: set())
            spawn = next(record for record in written if record.get('child'))
            self.assertEqual(spawn['child']['pid'], 401)
            self.assertEqual(spawn['tracked']['births'], [1000])
            self.assertEqual(spawn['tracked']['mark'], 1000)
            self.assertEqual(spawn['tracked']['boot'], 5070)

    def test_a_broken_tracked_record_is_refused(self):
        from riri import mini_lane as lane
        base = {'pids': {}, 'sessions': []}
        record = {'request_id': '💚1', 'block_id': 'b1', 'state': 'reserved'}
        for bad in ({'births': 'x'}, {'births': [0]}, {'births': [True]}, {'unsure': []},
                    {'unsure': {'a': 's'}}, {'unsure': {'1': ''}}, {'calibrated': 'yes'},
                    {'doubt': 'x'}, {'doubt': [0]}, {'doubt': [False]}, {'boxed': 1}, {'mark': 0},
                    {'mark': 'x'}, {'boot': -1}, {'boot': 1.5}):
            with self.subTest(bad=bad):
                self.assertEqual(lane.lane_record_problem({**record, 'tracked': {**base, **bad}}), 'tracked')
        self.assertIsNone(lane.lane_record_problem({**record, 'tracked': {
            **base, 'births': [5], 'unsure': {'1': 's'}, 'calibrated': True}}))
        self.assertIsNone(lane.lane_record_problem({**record, 'tracked': base}))     # 前の版の記録もそのまま読める



class ReviewTests(unittest.TestCase):
    """afc97e6 のレビューで見つかった道（模擬）。"""

    OLD, NEW = 'Thu Nov 26 12:00:00 2026', 'Thu Nov 26 12:00:07 2026'

    def watcher(self, lineage=True):
        t = Descendants(100, root_start=self.OLD)            # 子 PID 100（もう wait 済み）
        t.root_done = True
        if lineage:
            t.births, t.lineage, t.mark, t.live = {1000}, True, 1050, True
        return t

    def mates(self, tracker, rows, lineage, sids, listed=(), states=None, listed_rows=None):
        listed = set(listed)
        lister = Mates(listed, {p: (listed_rows or rows)[p][1] for p in listed}, weak=listed)
        if states is not None:
            tracker.calibrated, tracker.run_dir = True, '/run'
        with patch.object(mini_run, '_list_ps', lambda r, g: lister), \
                patch.object(mini_run, '_list_linux', lambda r, g: lister), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                world(lineage), \
                patch.object(mini_run.os, 'getsid', lambda pid: sids[pid]), \
                patch.object(mini_run, '_sandboxed', lambda pid: False), \
                patch.object(mini_run, 'reuse_gap', lambda: 90899), \
                patch.object(mini_run, 'sandbox_probes', lambda d: (b'w', b'o', b'i')), \
                patch.object(mini_run, '_proc_state', lambda pid: 'alive'), \
                patch.object(mini_run, 'run_sandbox_state', lambda pid, pr: (states or {}).get(pid, 'none')):
            found = run_mates('test-review', 100, tracker)
            return found, mini_run.still_the_same(found)

    def test_the_reap_does_not_walk_from_the_number_of_a_child_that_was_waited_for(self):
        # 子 100 は wait 済み。番号 100 を関係ない X が使い、X の子 401 がいる：401 を run のものにしない
        rows = {100: (1, self.NEW), 401: (100, self.NEW)}
        lineage = {100: (1080, 900), 401: (1081, 1080)}
        for t in (self.watcher(), self.watcher(lineage=False)):
            if t.lineage:
                t.foreign.add(900)        # X の親：見張り役が前の一覧で読んで、run のものでないと分かっていた
            for states in (None, {}):
                found, signalled = self.mates(t, rows, lineage, {100: 100, 401: 100}, states=states)
                self.assertEqual((found, signalled), (set(), []))
                self.assertNotIn(401, t.pids)
        # X の親を読んだことがない（模擬）：分からないので数えるが、合図しない
        t = self.watcher()
        found, signalled = self.mates(t, rows, lineage, {100: 100, 401: 100})
        self.assertEqual((signalled, found.unsure), ([], {100, 401}))
        self.assertNotIn(401, t.pids)

    def test_a_session_number_alone_does_not_make_a_process_the_runs(self):
        # 番号を使い回した X（setsid）が仲間を残して終わった：子の番号のセッション・記録済みのセッション。
        # 長い run（子が生まれてから番号が一巡しうる数のプロセスが生まれた）なので、番号では決められない
        late = 1000 + 90899 + 100
        for sid, sessions in ((100, set()), (555, {555})):
            t = self.watcher()
            t.sessions, t.mark = sessions, late - 50
            rows = {301: (1, self.NEW)}
            found, signalled = self.mates(t, rows, {301: (late, late - 1)}, {301: sid})
            self.assertEqual(found, {301})          # 分からないので数える
            self.assertEqual(signalled, [])         # 合図しない
            self.assertEqual(found.unsure, {301})

    def test_with_the_runs_sandbox_only_its_processes_are_signalled(self):
        t = self.watcher()
        t.pids = {401: self.NEW, 402: self.NEW, 403: self.NEW}   # たどれたもの（番号の取り違えがあったとしても）
        rows = {401: (1, self.NEW), 402: (1, self.NEW), 403: (1, self.NEW), 404: (1, self.NEW)}
        lineage = {401: (1060, 1000), 402: (1061, 1000), 403: (1062, 1000), 404: (1063, 1)}
        states = {401: 'run', 402: 'none', 403: 'other', 404: 'run'}
        found, signalled = self.mates(t, rows, lineage, {p: p for p in rows}, states=states)
        # サンドボックスにいないものは外す。たどれた run のものが「別のサンドボックス」と出ても証拠にしない（数えて合図しない）
        self.assertEqual(found, {401, 403, 404})
        self.assertEqual(found.unsure, {403})
        self.assertEqual(signalled, [401, 404])

    def test_with_the_runs_sandbox_a_process_that_cannot_be_told_is_only_counted(self):
        t = self.watcher()
        t.pids = {401: self.NEW}
        rows = {401: (1, self.NEW)}
        found, signalled = self.mates(t, rows, {401: (1060, 1000)}, {401: 401}, states={401: None})
        self.assertEqual((found, signalled, found.unsure), ({401}, [], {401}))

    def test_a_person_record_end_counts_the_children_of_an_unsure_process(self):
        live = self.watcher()
        live.root_done = False
        with world({701: (1090, 1085)}), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid):
            live.follow({701: (1, self.NEW)}, live=True)
        self.assertEqual(live.unsure, {701: self.NEW})
        back = Descendants.from_record(100, live.as_record())       # 701 は 702 を起こして終わった
        self.assertEqual(back.doubt, {1090})
        found, signalled = self.mates(back, {702: (1, self.NEW)}, {702: (1100, 1090)}, {702: 702})
        self.assertEqual((found, signalled), ({702}, []))

    def test_a_weak_process_the_watcher_did_not_read_is_not_dropped(self):
        # mark より前に生まれていても、見張り役が読めなかったもの（run のものでないと分かっていない）は外さない
        # 201 は表（ps）に出ず、グループの一覧にだけ出た：見張り役のたどりは読んでいない
        t = self.watcher()
        lister_rows = {201: (1, self.NEW)}
        found, signalled = self.mates(t, {}, {201: (1020, 900)}, {201: 201}, listed={201}, listed_rows=lister_rows)
        self.assertEqual((found, signalled), ({201}, []))
        t.foreign.add(1020)                                          # たどって run のものでないと分かった
        found, signalled = self.mates(t, {}, {201: (1020, 900)}, {201: 201}, listed={201}, listed_rows=lister_rows)
        self.assertEqual(found, set())
        # 表に出ていれば、その場でたどって決める（子の前からある家系なので run のものでない）
        found, signalled = self.mates(self.watcher(), lister_rows, {201: (1020, 900)}, {201: 201}, listed={201})
        self.assertEqual(found, set())

    def test_the_second_reviews_long_run_number_reuse(self):
        # 2回目のレビューの例：子（通し番号 1000）の run の間に約9万5千回 fork があり、最後の一覧の mark は 96000。
        # 子が終わったあと、関係ない L が子の番号 100 をもらって setsid し、M を残して終わった
        t = self.watcher()
        t.mark = 1000 + 95000
        rows = {701: (1, self.NEW)}
        found, signalled = self.mates(t, rows, {701: (t.mark + 5, t.mark + 3)}, {701: 100}, listed={701})
        self.assertEqual((found, signalled, found.unsure), ({701}, [], {701}))

    def test_when_the_child_ran_in_a_sandbox_an_orphan_in_none_is_not_the_runs(self):
        # 2回目のレビュー：run のサンドボックスに聞けない（較正なし）でも、子が何かのサンドボックスにいたなら、
        # サンドボックスの外の孤児（関係ない端末の背景のプロセスなど）は run のものでない
        for boxed, sandboxed, expected in ((True, False, set()), (True, True, {301}), (False, False, {301})):
            t = self.watcher()
            t.boxed = boxed
            rows = {301: (1, self.NEW)}
            with patch.object(mini_run, '_sandboxed', lambda pid: sandboxed):
                lister = Mates()
                with patch.object(mini_run, '_list_ps', lambda r, g: lister), \
                        patch.object(mini_run, '_list_linux', lambda r, g: lister), \
                        patch.object(mini_run, '_process_table', lambda: rows), \
                        world({301: (1061, 1060)}), \
                        patch.object(mini_run.os, 'getsid', lambda pid: pid):
                    found = run_mates('test-boxed', 100, t)
            self.assertEqual(found, expected, (boxed, sandboxed))
            self.assertEqual(found.unsure, expected)
            self.assertTrue(all(found.starts[pid] is None for pid in found))      # 数えても合図しない

    def test_a_person_record_end_judges_what_started_after_the_watchers_last_look(self):
        # 2回目のレビュー：保留のあと、残っていた R が短い C を起こし、C は O を自分のセッションに残して終わった。
        # サンドボックスに聞けない（run が out を消した）。record-end も、見張り役の最後の一覧より後に生まれて親が
        # 分からないものは「分からないもの」として数える
        record = {'pids': {}, 'sessions': [], 'births': [1000, 1200], 'calibrated': True, 'boxed': True, 'mark': 1250}
        t = Descendants.from_record(100, record, run_dir='/nonexistent-run-folder')
        rows = {801: (1, self.NEW)}
        with patch.object(mini_run, '_list_ps', lambda r, g: Mates()), \
                patch.object(mini_run, '_list_linux', lambda r, g: Mates()), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                world({801: (1301, 1300)}), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid), \
                patch.object(mini_run, '_sandboxed', lambda pid: True):
            found = run_mates('test-record-end', 100, t)
        self.assertEqual((found, found.unsure), ({801}, {801}))


class AlsoTests(unittest.TestCase):
    """4回目のレビュー：保留の理由が重なったら、両方を記録して知らせる。"""

    def test_mates_left_and_a_lost_sandbox_are_both_recorded(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = Path(temporary.name)
        child = Mock(pid=424242, returncode=None)
        child.poll.side_effect = [None, 0]
        child.wait.return_value = 0
        with patch.object(ms.subprocess, 'Popen', return_value=child), \
                patch.object(ms, 'process_start', return_value='child start'), \
                patch.object(mini_run, '_lineage', lambda pids: {p: (5000, 1) for p in pids if p == child.pid}), \
                patch.object(ms.Descendants, 'observe', lambda tracker: False), \
                patch.object(ms.Descendants, 'sandbox_lost', lambda tracker: True), \
                patch.object(ms.os, 'killpg'):
            record = ms.supervise(RunStore(d), 'run', ['fake-codex'], {}, b'', d / 'stdout', d / 'stderr',
                                  timeout=5, stop_check=lambda: False, grace=0.2, poll=0.01,
                                  lister=lambda run, pgid: {999999})
        self.assertNotIn('end_evidence', record)
        self.assertEqual((record['end_unconfirmed']['reason'], record['end_unconfirmed']['also']),
                         ('run mates still alive', 'run sandbox unavailable'))


@unittest.skipUnless(DARWIN, 'macOS の本物のプロセスで確かめる')
class StopPathTests(unittest.TestCase):
    """どの止め方でも、子の通し番号が読めず run のサンドボックスにも聞けなければ、終わりを確かめない。"""

    def test_a_time_limit_without_the_childs_birth_number_is_not_confirmed(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = Path(os.path.realpath(temporary.name))
        pidfile = d / 'left.pid'
        (d / 'fake.py').write_text(textwrap.dedent('''
            import subprocess, sys, time
            sys.stdin.read()
            time.sleep(0.6)
            subprocess.run(["/bin/sh", "-c", "/bin/sleep 60 >/dev/null 2>&1 & echo $! > " + sys.argv[1]],
                           start_new_session=True)
            time.sleep(30)
        '''), encoding='utf-8')

        def kill_left():
            try:
                pid = int(pidfile.read_text())
            except (OSError, ValueError):
                return
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        self.addCleanup(kill_left)
        with patch.object(ms, 'TRACK_EVERY', 1000), patch.object(mini_run, '_lineage', lambda pids: {}):
            record = ms.supervise(RunStore(d), 'test-' + uuid.uuid4().hex[:12],
                                  [sys.executable, '-B', str(d / 'fake.py'), str(pidfile)], os.environ, b'',
                                  d / 'out', d / 'err', timeout=1.5, stop_check=lambda: False, grace=1, poll=0.05)
        self.assertEqual(record['exit']['reason'], 'timeout')
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'process lineage unavailable')

    def test_a_run_that_removed_its_folder_is_held(self):
        # 子が run のサンドボックスにいると確かめたあとで、run が自分の out を消した：サンドボックスに聞けない
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = Path(os.path.realpath(temporary.name))
        run_dir = d / 'run'
        for sub in ('work', 'out', 'input'):
            (run_dir / sub).mkdir(parents=True)
        profile = d / 'run.sb'
        profile.write_text('(version 1)\n(allow default)\n(deny file-write*)\n'
                           f'(allow file-write* (subpath "{run_dir}/work") (subpath "{run_dir}/out") (regex #"^/dev/"))\n'
                           f'(deny file-write* (subpath "{run_dir}/input"))\n', encoding='utf-8')
        (d / 'fake.py').write_text(textwrap.dedent('''
            import os, sys, time
            sys.stdin.read()
            time.sleep(0.8)
            os.rmdir(sys.argv[1])
        '''), encoding='utf-8')
        with patch.object(ms, 'TRACK_EVERY', 0.2):
            record = ms.supervise(RunStore(d), 'test-' + uuid.uuid4().hex[:12],
                                  ['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable, '-B',
                                   str(d / 'fake.py'), str(run_dir / 'out')], os.environ, b'',
                                  d / 'o', d / 'e', timeout=20, stop_check=lambda: False, grace=1, poll=0.05,
                                  run_dir=str(run_dir))
        self.assertFalse((run_dir / 'out').exists())
        self.assertTrue(record['tracked']['calibrated'])
        self.assertNotIn('end_evidence', record)
        self.assertEqual(record['end_unconfirmed']['reason'], 'run sandbox unavailable')


class UnknownAnswerTests(unittest.TestCase):
    """再検証 💚20261128-3 の重大1：サンドボックスの答えが分からないものを「run のものでない」と同じに扱わない。"""

    S = 'Thu Nov 26 12:00:00 2026'

    def tracker(self):
        t = Descendants(100)
        t.root_done = True
        t.births, t.lineage, t.mark, t.live = {1000}, True, 1050, True
        t.calibrated, t.run_dir = True, '/run'
        return t

    def mates(self, t, rows, states, kinds, lineage=None, lister=None):
        with patch.object(mini_run, 'sandbox_probes', lambda d: (b'w', b'o', b'i')):
            return self.mates_raw(t, rows, states, kinds, lineage, lister)

    def mates_raw(self, t, rows, states, kinds, lineage=None, lister=None):
        lister = lister or Mates()
        with world(lineage or {}), \
                patch.object(mini_run, 'run_sandbox_state', lambda pid, pr: states.get(pid, 'none')), \
                patch.object(mini_run, '_proc_state', lambda pid: kinds.get(pid, 'alive')), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                patch.object(mini_run, '_list_ps', lambda r, g: lister), \
                patch.object(mini_run, '_list_linux', lambda r, g: lister), \
                patch.object(mini_run.os, 'getsid', lambda pid: pid), \
                patch.object(mini_run, '_sandboxed', lambda pid: True):
            return run_mates('test-unknown', 100, t)

    def test_a_process_that_ended_before_it_was_told_makes_the_listing_unsettled(self):
        rows = {401: (1, self.S)}
        found = self.mates(self.tracker(), rows, {401: None}, {401: 'gone'})
        self.assertEqual((found, found.recheck), (set(), True))
        rows = {402: (os.getpid(), self.S)}                         # 見張り役自身が起こした ps：数えない
        found = self.mates(self.tracker(), rows, {402: None}, {402: 'gone'})
        self.assertEqual((found, found.recheck), (set(), False))

    def test_a_running_process_that_cannot_be_told_is_counted_not_signalled(self):
        rows = {403: (1, self.S)}
        found = self.mates(self.tracker(), rows, {403: None}, {403: 'alive'})
        self.assertEqual((found, found.unsure, found.starts[403]), ({403}, {403}, None))

    def test_a_zombie_is_not_counted_and_its_parent_tells_whether_to_look_again(self):
        # 親が生きていて run のものでないゾンビ：数えず、待たない
        t = self.tracker()
        rows = {404: (450, self.S), 450: (1, self.S)}
        found = self.mates(t, rows, {404: None, 450: 'none'}, {404: 'zombie', 450: 'alive'}, lineage={404: (1060, 950)})
        self.assertEqual((found, found.recheck), (set(), False))
        # たどれた run のもののゾンビ：数えないが、ゾンビになる前に何か起こしたかもしれないので次の一覧を見る
        t = self.tracker()
        t.pids = {404: self.S}
        found = self.mates(t, {404: (1, self.S)}, {404: None}, {404: 'zombie'}, lineage={404: (1060, 1000)})
        self.assertEqual((found, found.recheck), (set(), True))
        # launchd が親のゾンビ（launchd がすぐ片付ける）：ゾンビになる前に何か起こしたかもしれないので、次の一覧を見る
        t = self.tracker()
        t.pids = {404: self.S}
        found = self.mates(t, {404: (1, self.S)}, {404: None}, {404: 'zombie'}, lineage={404: (1060, 1000)})
        self.assertEqual((found, found.recheck), (set(), True))

    def test_a_process_ending_before_it_was_told_is_counted_and_looked_at_again(self):
        # 3回目のレビュー：終わりかけ（P_WEXIT）の run のプロセスを「別のサンドボックス」と取らない
        t = self.tracker()
        t.pids = {405: self.S}
        found = self.mates(t, {405: (1, self.S)}, {405: None}, {405: 'exiting'}, lineage={405: (1060, 1000)})
        self.assertEqual((found, found.unsure, found.recheck), ({405}, {405}, True))

    def test_a_vanished_process_whose_parent_is_not_the_runs_does_not_hold_the_listing(self):
        # 3回目のレビュー：忙しい Mac の関係ない短いプロセス。親が生きていて run のものでなければ、待たない
        rows = {406: (460, self.S), 460: (1, self.S)}
        found = self.mates(self.tracker(), rows, {406: None, 460: 'none'}, {406: 'gone', 460: 'alive'})
        self.assertEqual((found, found.recheck), (set(), False))
        found = self.mates(self.tracker(), rows, {406: None, 460: 'run'}, {406: 'gone', 460: 'alive'})
        self.assertEqual((found.recheck, 460 in found), (True, True))           # 親が run のもの：次の一覧を待つ

    def test_a_known_process_that_is_gone_is_not_counted_but_looked_at_again(self):
        t = self.tracker()
        t.pids = {408: self.S}
        found = self.mates(t, {408: (1, self.S)}, {408: None}, {408: 'gone'}, lineage={408: (1060, 1000)})
        self.assertEqual((found, found.recheck, found.waiting[0]['pid']), (set(), True, 408))

    def test_one_told_the_runs_before_is_waited_for_whatever_its_birth(self):
        t = self.tracker()
        t.seen_run = {409: self.S}
        t.foreign.add(1070)
        found = self.mates(t, {409: (1, self.S)}, {409: None}, {409: 'gone'}, lineage={409: (1070, 1)})
        self.assertTrue(found.recheck)

    def test_a_daemon_of_the_run_taken_for_not_the_runs_by_birth_is_still_waited_for(self):
        # 確認のレビュー：run のプロセスが自分のセッションを作り、親が終わってから exec した（デーモン化）。通し番号の
        # 弱い規則では run のものでないとされるので、それだけで待たないと、照会の前に起こした孫を見落とした
        t = self.tracker()
        t.foreign.add(1070)
        found = self.mates(t, {410: (1, self.S)}, {410: None}, {410: 'gone'}, lineage={410: (1070, 1)})
        self.assertTrue(found.recheck)

    def test_one_told_not_the_runs_while_it_ran_is_not_waited_for(self):
        t = self.tracker()
        t.seen_not_run = {413: self.S}
        found = self.mates(t, {413: (1, self.S)}, {413: None}, {413: 'gone'}, lineage={413: (1070, 1)})
        self.assertFalse(found.recheck)

    def test_one_born_before_the_child_is_not_waited_for(self):
        found = self.mates(self.tracker(), {411: (1, self.S)}, {411: None}, {411: 'gone'}, lineage={411: (900, 1)})
        self.assertFalse(found.recheck)
        found = self.mates(self.tracker(), {411: (1, self.S)}, {411: None}, {411: 'gone'}, lineage={411: (1070, 1)})
        self.assertTrue(found.recheck)                                          # 子のあとに生まれた：分からない

    def test_a_group_leader_told_not_the_runs_clears_its_members(self):
        # 忙しい Mac の孤児（親は launchd）：ps の時点のグループの頭が生きていて run のものでなければ、待たない
        rows = mini_run.Table({414: (1, self.S), 470: (1, self.S)}, groups={414: 470, 470: 470})
        found = self.mates(self.tracker(), rows, {414: None, 470: 'none'}, {414: 'gone', 470: 'alive'},
                           lineage={414: (1070, 1)})
        self.assertFalse(found.recheck)
        found = self.mates(self.tracker(), rows, {414: None, 470: 'run'}, {414: 'gone', 470: 'alive'},
                           lineage={414: (1070, 1)})
        self.assertTrue(found.recheck)                                          # 頭が run のもの：待つ

    def test_the_child_itself_not_yet_waited_for_is_waited_for(self):
        t = self.tracker()
        t.root, t.root_done = 412, False
        found = self.mates(t, {412: (os.getpid(), self.S)}, {412: None}, {412: 'zombie'}, lineage={})
        self.assertTrue(found.recheck)

    def test_folders_that_change_while_asked_make_no_answer_count(self):
        # 3回目のレビュー：run が out を消して作り直す。答えを使わず、サンドボックスを失ったものとして扱う
        class Shaky(tuple):
            def steady(self):
                return False
        t = self.tracker()
        t.pids = {407: self.S}
        rows = {407: (1, self.S)}
        with patch.object(mini_run, 'sandbox_probes', lambda d: Shaky((b'w', b'o', b'i'))):
            found = self.mates_raw(t, rows, {407: 'other'}, {407: 'alive'}, lineage={407: (1060, 1000)})
        self.assertEqual((407 in found, found.lost, found.recheck, t.lost_sandbox), (True, True, True, True))

    def test_the_reviews_tool_told_once_unknown_is_reaped_after_all(self):
        # みにの再現：較正済み、親が launchd の道具（自分のセッション）の答えが1回だけ分からない。前は残ったまま
        # watched だった。今は1回目は数えて合図せず、2回目に run と分かって止める
        answers = iter([None, 'run', 'run'])
        state = {'v': None}
        t = self.tracker()
        rows = {501: (1, self.S)}
        kill = Mock()

        def lister(run_id, pgid):
            state['v'] = next(answers, 'run')
            found = self.mates(t, rows, {501: state['v']}, {501: 'alive'}, lineage={501: (1100, 1)})
            if kill.call_count:
                return Mates()                                         # 止めたあとはいない
            return found
        with patch.object(mini_run, '_process_table', lambda: rows):
            self.assertTrue(mini_run.reap_run_mates('test-unknown', 100, grace=0.5, poll=0.01, lister=lister,
                                                    kill=kill))
        self.assertEqual(kill.call_args_list[0].args, (501, signal.SIGTERM))

    def test_the_reap_does_not_take_an_unsettled_empty_listing_for_the_end(self):
        # みにの2つ目の再現：一覧にいたプロセスが次を起こして照会の前に終わった。1回目の一覧は空でも、次の一覧を待つ
        unsettled = Mates()
        unsettled.recheck = True
        listings = iter([unsettled, Mates({601}, {601: self.S}), Mates()])
        kill = Mock()
        with patch.object(mini_run, '_process_table', lambda: {601: (1, self.S)}):
            self.assertTrue(mini_run.reap_run_mates('test-unknown', 100, grace=0.5, poll=0.01,
                                                    lister=lambda r, g: next(listings), kill=kill))
        self.assertEqual(kill.call_args_list[0].args, (601, signal.SIGTERM))
        always = Mates()
        always.recheck = True
        self.assertFalse(mini_run.reap_run_mates('test-unknown', 100, grace=0.1, poll=0.01,
                                                 lister=lambda r, g: always, kill=Mock()))


@unittest.skipUnless(DARWIN, 'macOS の本物のプロセスで確かめる')
class ProcStateTests(unittest.TestCase):
    def test_alive_zombie_and_gone(self):
        self.assertEqual(mini_run._proc_state(os.getpid()), 'alive')
        p = subprocess.Popen(['/usr/bin/true'])
        for _ in range(100):
            if mini_run._proc_state(p.pid) == 'zombie':
                break
            time.sleep(0.02)
        self.assertEqual(mini_run._proc_state(p.pid), 'zombie')
        p.wait()
        self.assertEqual(mini_run._proc_state(p.pid), 'gone')

    def test_another_sandbox_is_told_without_birth_numbers(self):
        # みにの読み直しの案が崩れた所：通し番号が読めないときも、生きている別のサンドボックスは「other」、ゾンビは None
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = Path(os.path.realpath(temporary.name))
        for sub in ('work', 'out', 'input'):
            (d / 'run' / sub).mkdir(parents=True)
        probes = mini_run.sandbox_probes(str(d / 'run'))
        (d / 'p.sb').write_text('(version 1)\n(allow default)\n(deny file-write*)\n', encoding='utf-8')
        other = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(d / 'p.sb'), '/bin/sleep', '30'])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        for _ in range(100):
            if subprocess.run(['ps', '-o', 'comm=', '-p', str(other.pid)], capture_output=True,
                              text=True).stdout.strip() == '/bin/sleep':
                break
            time.sleep(0.05)
        with patch.object(mini_run, '_lineage', lambda pids: {}):
            self.assertEqual(mini_run.run_sandbox_state(other.pid, probes), 'other')
            z = subprocess.Popen(['/usr/bin/true'])
            for _ in range(200):
                if mini_run._proc_state(z.pid) == 'zombie':
                    break
                time.sleep(0.02)
            self.assertIsNone(mini_run.run_sandbox_state(z.pid, probes))
            z.wait()


@unittest.skipUnless(DARWIN, 'macOS の本物のカーネルで確かめる')
class RealKernelTests(unittest.TestCase):
    """3回目のレビューの本物のカーネルの場面：終わりかけ・フォルダの揺れ・忙しい Mac。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(os.path.realpath(temporary.name))
        self.run_dir = self.dir / 'run'
        for sub in ('work', 'out', 'input'):
            (self.run_dir / sub).mkdir(parents=True)
        self.profile = self.dir / 'run.sb'
        self.profile.write_text('(version 1)\n(allow default)\n(deny file-write*)\n'
                                f'(allow file-write* (subpath "{self.run_dir}/work") (subpath "{self.run_dir}/out") '
                                '(regex #"^/dev/"))\n'
                                f'(deny file-write* (subpath "{self.run_dir}/input"))\n', encoding='utf-8')

    def test_a_run_process_that_is_ending_is_never_told_another_sandbox(self):
        probes = mini_run.sandbox_probes(str(self.run_dir))
        big = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(self.profile), sys.executable, '-c',
                                'b = bytearray(400 * 1024 * 1024); import time; time.sleep(30)'])
        self.addCleanup(lambda: (big.poll() is None and big.kill(), big.wait()))
        for _ in range(200):
            if mini_run.run_sandbox_state(big.pid, probes) == 'run':
                break
            time.sleep(0.05)
        time.sleep(1.0)                                      # the memory is in place
        os.kill(big.pid, signal.SIGKILL)
        seen, kinds = set(), set()
        end = time.monotonic() + 2
        while time.monotonic() < end:
            kind = mini_run._proc_state(big.pid)
            kinds.add(kind)
            if kind in ('zombie', 'gone'):
                break
            seen.add(mini_run.run_sandbox_state(big.pid, probes))
        self.assertNotIn('other', seen)
        self.assertNotIn('none', seen)
        self.assertTrue(None in seen or 'exiting' in kinds, (seen, kinds))     # 終わりかけの間に聞けている

    def test_folders_that_went_and_came_back_are_not_the_same(self):
        probes = mini_run.sandbox_probes(str(self.run_dir))
        self.assertTrue(probes.steady())
        (self.run_dir / 'out').rmdir()
        self.assertFalse(probes.steady())
        (self.run_dir / 'out').mkdir()
        self.assertFalse(probes.steady())                    # 作り直したフォルダは別のもの
        self.assertTrue(mini_run.sandbox_probes(str(self.run_dir)).steady())

    def test_folders_moved_away_and_back_with_the_same_inode_are_not_steady(self):
        # 4回目のレビュー：run が work・out を tmp へ動かして戻す（inode は同じ）。run のフォルダの時刻で分かる
        (self.run_dir / 'tmp').mkdir()
        probes = mini_run.sandbox_probes(str(self.run_dir))
        before = [os.lstat(self.run_dir / name).st_ino for name in ('work', 'out')]
        time.sleep(0.01)
        os.rename(self.run_dir / 'work', self.run_dir / 'tmp' / 'w')
        os.rename(self.run_dir / 'out', self.run_dir / 'tmp' / 'o')
        os.rename(self.run_dir / 'tmp' / 'w', self.run_dir / 'work')
        os.rename(self.run_dir / 'tmp' / 'o', self.run_dir / 'out')
        self.assertEqual([os.lstat(self.run_dir / name).st_ino for name in ('work', 'out')], before)
        self.assertFalse(probes.steady())

    def test_a_run_folder_moved_behind_a_link_is_not_asked(self):
        # 確認のレビュー：保留中に置き場を別の場所へ動かしてリンクを置くと、同じパスで別のフォルダに聞いていた
        t = Descendants(100, run_dir=str(self.run_dir))
        t.pin_folder()
        self.assertFalse(t.folder_moved())
        self.assertIsNotNone(t.folder_probes())
        moved = self.dir / 'moved'
        os.rename(self.run_dir, moved)
        os.symlink(moved, self.run_dir)
        self.assertTrue(t.folder_moved())                     # realpath が違う
        self.assertIsNone(t.folder_probes())
        os.unlink(self.run_dir)
        for sub in ('work', 'out', 'input'):
            (self.run_dir / sub).mkdir(parents=True)          # 同じパスに作り直した別のフォルダ
        self.assertTrue(t.folder_moved())                     # inode が違う
        back = Descendants.from_record(100, t.as_record(), run_dir=str(self.run_dir))
        self.assertTrue(back.folder_moved())                  # 記録から作り直しても同じ

    def busy(self, loop):
        churn = subprocess.Popen(['/bin/sh', '-c', loop], start_new_session=True)
        self.addCleanup(lambda: (os.killpg(churn.pid, signal.SIGKILL), churn.wait()))
        time.sleep(0.5)
        child = self.dir / 'child.py'
        child.write_text('import sys, time\nsys.stdin.read()\ntime.sleep(1.2)\n', encoding='utf-8')
        started = time.monotonic()
        with patch.object(ms, 'TRACK_EVERY', 0.3):
            record = ms.supervise(RunStore(self.dir), 'test-' + uuid.uuid4().hex[:12],
                                  ['/usr/bin/sandbox-exec', '-f', str(self.profile), sys.executable, '-B', str(child)],
                                  os.environ, b'', self.dir / 'o', self.dir / 'e', timeout=30, stop_check=lambda: False,
                                  grace=3, poll=0.05, run_dir=str(self.run_dir))
        self.assertTrue(record['tracked']['calibrated'])
        self.assertEqual(record.get('end_evidence', {}).get('kind'), 'watched', record.get('end_unconfirmed'))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_mac_busy_with_orphans_handed_to_launchd_does_not_hold_a_run_that_ended_well(self):
        # 4回目のレビュー：「( cmd & )」の形で、親が launchd の短いプロセスが毎秒十数個生まれては消える
        self.busy('while :; do ( /bin/sleep 0.05 & ); done')

    def test_a_mac_busy_with_nested_short_shells_does_not_hold_a_run_that_ended_well(self):
        self.busy("while :; do /bin/sh -c '/bin/sh -c \"/bin/sleep 0.03\"; true'; done")

    def test_a_busy_mac_does_not_hold_a_run_that_ended_well(self):
        # 3回目のレビュー：関係ない短いプロセスが毎秒数十回生まれては消える。正常に終わった run は watched になる
        churn = subprocess.Popen(['/bin/sh', '-c', 'while :; do /bin/sleep 0.03; done'], start_new_session=True)
        self.addCleanup(lambda: (os.killpg(churn.pid, signal.SIGKILL), churn.wait()))
        child = self.dir / 'child.py'
        child.write_text('import sys, time\nsys.stdin.read()\ntime.sleep(1.2)\n', encoding='utf-8')
        started = time.monotonic()
        with patch.object(ms, 'TRACK_EVERY', 0.3):
            record = ms.supervise(RunStore(self.dir), 'test-' + uuid.uuid4().hex[:12],
                                  ['/usr/bin/sandbox-exec', '-f', str(self.profile), sys.executable, '-B', str(child)],
                                  os.environ, b'', self.dir / 'o', self.dir / 'e', timeout=30, stop_check=lambda: False,
                                  grace=3, poll=0.05, run_dir=str(self.run_dir))
        self.assertTrue(record['tracked']['calibrated'])
        self.assertEqual(record['end_evidence']['kind'], 'watched')
        self.assertLess(time.monotonic() - started, 10)


class HoldNoticeTests(test_mini_lane.Base):
    def test_the_hold_notice_names_a_lineage_that_could_not_be_followed(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'},
                                            'end_unconfirmed': {'reason': 'process lineage unavailable', 'at': 'x'}})
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        self.assertIn('子が最後に起こしたプロセスをたどれず、終了を確かめられない',
                      self.patrol.state['outbox']['mini-hold:' + run]['text'])

    def test_the_hold_notice_names_a_sandbox_that_cannot_be_asked(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'},
                                            'end_unconfirmed': {'reason': 'run sandbox unavailable', 'at': 'x'}})
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        self.assertIn('run のサンドボックスに聞けなくなり', self.patrol.state['outbox']['mini-hold:' + run]['text'])

    def test_the_hold_notice_says_both_reasons(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'},
                                            'end_unconfirmed': {'reason': 'run mates still alive', 'at': 'x',
                                                                'also': 'run sandbox unavailable'}})
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        text = self.patrol.state['outbox']['mini-hold:' + run]['text']
        self.assertIn('run の仲間が残っていて終了を確かめられない。run のサンドボックスにも聞けなくなっている', text)

    def test_the_hold_notice_names_an_unconfirmed_sandbox(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'},
                                            'end_unconfirmed': {'reason': 'run sandbox not confirmed', 'at': 'x'}})
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        self.assertIn('子が run のサンドボックスにいると確かめられず', self.patrol.state['outbox']['mini-hold:' + run]['text'])

    def launched_run(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        folder = lane.run_base(self.cfg) / run
        for sub in ('input', 'work', 'out', 'tmp'):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'}, 'run_dir': str(folder),
                                            'stdout_path': str(self.root / 'none.jsonl')})
        return run, folder

    @unittest.skipUnless(DARWIN, 'macOS のサンドボックスで確かめる')
    def test_record_end_asks_the_runs_folder_and_refuses_without_it(self):
        # 重大2：見張り役が run のサンドボックスを確かめる前に落ちた run でも、run のフォルダに聞いて残りを数える。
        # フォルダがそろっていなければ通さない（作り直せば聞ける）
        from riri import mini_lane as lane
        run, folder = self.launched_run()
        real = os.path.realpath(folder)
        profile = Path(self.tmp.name) / 'run.sb'
        profile.write_text('(version 1)\n(allow default)\n(deny file-write*)\n'
                           f'(allow file-write* (subpath "{real}/work") (subpath "{real}/out") (regex #"^/dev/"))\n'
                           f'(deny file-write* (subpath "{real}/input"))\n', encoding='utf-8')
        left = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/sleep', '30'], start_new_session=True)
        self.addCleanup(lambda: (left.poll() is None and left.kill(), left.wait()))
        for _ in range(100):
            if subprocess.run(['ps', '-o', 'comm=', '-p', str(left.pid)], capture_output=True,
                              text=True).stdout.strip() == '/bin/sleep':
                break
            time.sleep(0.05)
        with self.assertRaisesRegex(Exception, 'run の仲間がまだ残っている（PID ' + str(left.pid)):
            lane.record_end(self.cfg, run, 'たかむね')
        (folder / 'out').rmdir()
        with self.assertRaisesRegex(Exception, 'run のフォルダ（work・out・input）がそろっていない'):
            lane.record_end(self.cfg, run, 'たかむね')
        self.assertNotIn('end_evidence', self.store().read(run))
        (folder / 'out').mkdir()
        left.kill()
        left.wait()
        lane.record_end(self.cfg, run, 'たかむね')
        self.assertEqual(self.store().read(run)['end_evidence']['kind'], 'manual')

    def test_record_end_waits_out_a_listing_that_cannot_settle(self):
        from riri import mini_lane as lane
        from riri import mini_run as mr
        run, folder = self.launched_run()
        unsettled = Mates()
        unsettled.recheck = True
        unsettled.waiting = [{'pid': 777, 'ppid': 1, 'state': 'gone'}]
        with patch.object(mr, 'run_mates', lambda *a, **k: unsettled), patch.object(lane.time, 'sleep', lambda s: None):
            with self.assertRaisesRegex(Exception, '確かめている間に終わるプロセスが続いた.*PID 777（親 1・gone）'):
                lane.record_end(self.cfg, run, 'たかむね')
        self.assertNotIn('end_evidence', self.store().read(run))

    def test_the_hold_notice_names_a_listing_that_did_not_settle(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'},
                                            'end_unconfirmed': {'reason': 'process listing did not settle', 'at': 'x'}})
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        self.assertIn('一覧が落ち着かず', self.patrol.state['outbox']['mini-hold:' + run]['text'])

    def test_record_end_names_mates_before_an_unsettled_listing_and_refuses_a_lost_folder(self):
        from riri import mini_lane as lane
        run, folder = self.launched_run()
        both = Mates({4242}, {4242: 's'})
        both.recheck = True
        with self.assertRaisesRegex(Exception, 'run の仲間がまだ残っている（PID 4242'):
            lane.record_end(self.cfg, run, 'たかむね', lister=lambda r, g: both)
        lost = Mates()
        lost.lost = True
        with self.assertRaisesRegex(Exception, '確かめている間に変わった'):
            lane.record_end(self.cfg, run, 'たかむね', lister=lambda r, g: lost)
        self.assertNotIn('end_evidence', self.store().read(run))

    def test_record_end_names_what_cannot_be_told(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'}})
        listed = Mates({4242, 4243}, {4242: 's', 4243: None})
        listed.unsure = {4243}
        with self.assertRaisesRegex(Exception, '（PID 4242, 4243）。うち run のものか分からないもの：PID 4243'):
            lane.record_end(self.cfg, run, 'たかむね', lister=lambda r, g: listed)

    def test_record_end_refuses_a_calibrated_receipt_with_another_runs_folder(self):
        from riri import mini_lane as lane
        self.take(test_mini_lane.req(number=1))
        store = self.store()
        run = store.all_runs()[0]['run_id']
        base = lane.run_base(self.cfg)
        store.update(run, lambda record: {**record, 'state': 'launched', 'launch_mark': 'x',
                                          'child': {'pid': 800}, 'run_dir': str(base / 'other-run'),
                                          'tracked': {'pids': {}, 'sessions': [], 'calibrated': True}})
        with patch.object(lane, 'supervisor_alive', return_value=False):
            lister = Mock(return_value=set())
            with self.assertRaisesRegex(lane.SafetyError, 'サンドボックスの照会先を確認できない'):
                lane.record_end(self.cfg, run, 'たかむね', lister=lister)
            lister.assert_not_called()
            self.assertNotIn('end_evidence', store.read(run))
            store.update(run, lambda record: {**record, 'run_dir': str(base / run)})
            result = lane.record_end(self.cfg, run, 'たかむね', lister=lister)
        self.assertEqual(result['end_evidence']['kind'], 'manual')

    def test_the_lane_gives_the_watcher_the_run_folder(self):
        from riri import mini_lane as lane
        r1 = test_mini_lane.req(number=1)
        self.take(r1)
        lane.tick(self.patrol, [r1], mailbox=test_mini_lane.FakeMailbox(), popen=self.popen)
        record = [r for r in self.store().all_runs() if r.get('state') == 'spawning'][0]
        got = {}
        with patch.object(ms, 'supervise', lambda *a, **k: got.update(k)):
            self.run_supervisor()
        self.assertEqual(got.get('run_dir'), record['run_dir'])


if __name__ == '__main__':
    unittest.main()
