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
from pathlib import Path
from unittest.mock import Mock, patch

from riri import mini_run
from riri import mini_supervisor as ms
from riri.mini_run import Descendants, Mates, RunStore, run_mates, unfinished_runs
from tests import test_mini_lane

DARWIN = sys.platform == 'darwin'

FAKE = textwrap.dedent('''
    import subprocess, sys, time
    mode, pidfile, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])
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
        time.sleep(0.3)
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
        self.assertEqual(mini_run.run_sandbox_state(work_only, self.probes), 'other')
        self.assertEqual(mini_run.run_sandbox_state(other, self.probes), 'other')     # 別の run
        self.assertEqual(mini_run.run_sandbox_state(loose, self.probes), 'other')     # どこにでも書ける
        self.assertEqual(mini_run.run_sandbox_state(plain, self.probes), 'none')
        self.assertEqual(mini_run.run_sandbox_state(os.getpid(), self.probes), 'none')

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
        t.births, t.lineage, t.mark = {1000}, True, 1050
        return t

    def mates(self, tracker, lineage, listed, rows):
        lister = Mates(listed, {pid: self.NEW for pid in listed}, weak=listed)
        with patch.object(mini_run, '_list_ps', lambda run_id, pgid: lister), \
                patch.object(mini_run, '_list_linux', lambda run_id, pgid: lister), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                patch.object(mini_run, '_lineage', lambda pids: {p: lineage[p] for p in pids if p in lineage}), \
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
        with patch.object(mini_run, '_lineage', lambda pids: {p: lineage[p] for p in pids if p in lineage}), \
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
        # 子のセッション（頭はもういない）。run のプロセスがそのセッションにいたのを最近の一覧で見た：番号が
        # 使い回されるには、ほぼ全部の番号が配られる（通し番号で数えて reuse_gap 以上）必要がある
        t = self.tracker()
        t.session_seen = {100: 1040}
        rows = {601: (1, self.S)}
        self.follow(t, rows, {601: (1080, 1075)}, {601: 100})
        self.assertIn(601, t.pids)
        # 番号だけ（そのセッションに run のプロセスを見たことがない、または見てから多くのプロセスが生まれた）なら
        # 決めない（レビュー：番号の使い回しで、新しい頭も終わり仲間だけ残る W1 そのもの）
        for seen in ({}, {100: 1080 - 90899 - 1}):
            t = self.tracker()
            t.session_seen = dict(seen)
            self.follow(t, rows, {601: (1080, 1075)}, {601: 100})
            self.assertNotIn(601, t.pids)
            self.assertIn(601, t.unsure)

    def test_an_orphan_that_cannot_be_told_is_unsure_and_so_are_its_children(self):
        t = self.tracker()
        rows = {701: (1, self.S)}
        self.follow(t, rows, {701: (1090, 1085)}, {701: 701})       # 自分でセッションを作った
        self.assertEqual(t.unsure, {701: self.S})
        rows = {701: (1, self.S), 702: (701, self.S)}
        self.follow(t, rows, {701: (1090, 1085), 702: (1100, 1090)}, {701: 701, 702: 701})
        self.assertEqual(set(t.unsure), {701, 702})

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
        back = Descendants.from_record(100, t.as_record(), run_dir='/run')
        self.assertEqual((back.births, back.unsure, back.calibrated, back.run_dir, back.lineage, back.mark),
                         ({1000}, {701: self.S}, True, '/run', True, None))
        self.assertTrue(Descendants.from_record(100, {'births': ['x', 0, 5]}).births == {5})


class LaneTests(unittest.TestCase):
    def test_a_broken_tracked_record_is_refused(self):
        from riri import mini_lane as lane
        base = {'pids': {}, 'sessions': []}
        record = {'request_id': '💚1', 'block_id': 'b1', 'state': 'reserved'}
        for bad in ({'births': 'x'}, {'births': [0]}, {'births': [True]}, {'unsure': []},
                    {'unsure': {'a': 's'}}, {'unsure': {'1': ''}}, {'calibrated': 'yes'},
                    {'doubt': 'x'}, {'doubt': [0]}, {'doubt': [False]}):
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
            t.births, t.lineage, t.mark = {1000}, True, 1050
        return t

    def mates(self, tracker, rows, lineage, sids, listed=(), states=None, listed_rows=None):
        listed = set(listed)
        lister = Mates(listed, {p: (listed_rows or rows)[p][1] for p in listed}, weak=listed)
        if states is not None:
            tracker.calibrated, tracker.run_dir = True, '/run'
        with patch.object(mini_run, '_list_ps', lambda r, g: lister), \
                patch.object(mini_run, '_list_linux', lambda r, g: lister), \
                patch.object(mini_run, '_process_table', lambda: rows), \
                patch.object(mini_run, '_lineage', lambda pids: {p: lineage[p] for p in pids if p in lineage}), \
                patch.object(mini_run.os, 'getsid', lambda pid: sids[pid]), \
                patch.object(mini_run, '_sandboxed', lambda pid: False), \
                patch.object(mini_run, 'reuse_gap', lambda: 90899), \
                patch.object(mini_run, 'sandbox_probes', lambda d: (b'w', b'o', b'i')), \
                patch.object(mini_run, 'run_sandbox_state', lambda pid, pr: (states or {}).get(pid, 'none')):
            found = run_mates('test-review', 100, tracker)
            return found, mini_run.still_the_same(found)

    def test_the_reap_does_not_walk_from_the_number_of_a_child_that_was_waited_for(self):
        # 子 100 は wait 済み。番号 100 を関係ない X が使い、X の子 401 がいる：401 を run のものにしない
        rows = {100: (1, self.NEW), 401: (100, self.NEW)}
        lineage = {100: (1080, 900), 401: (1081, 1080)}
        for t in (self.watcher(), self.watcher(lineage=False)):
            for states in (None, {}):
                found, signalled = self.mates(t, rows, lineage, {100: 100, 401: 100}, states=states)
                self.assertEqual((found, signalled), (set(), []))
                self.assertNotIn(401, t.pids)

    def test_a_session_number_alone_does_not_make_a_process_the_runs(self):
        # 番号を使い回した X（setsid）が仲間を残して終わった：子の番号のセッション・記録済みのセッション
        for sid, sessions in ((100, set()), (555, {555})):
            t = self.watcher()
            t.sessions = sessions
            rows = {301: (1, self.NEW)}
            found, signalled = self.mates(t, rows, {301: (1061, 1060)}, {301: sid})
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
        self.assertEqual(found, {401, 404})          # サンドボックスにいないもの・別のサンドボックスは外す
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
        with patch.object(mini_run, '_lineage', lambda pids: {p: (1090, 1085) for p in pids if p == 701}), \
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
