"""Building blocks for waking みに (Codex) from りり.  Nothing here is wired in yet.

Design: みに wake design v1.0 (Notion), 共通の決まり P1 and 4-3 / 4-4.

* ``RunStore`` keeps one JSON record per run.  Every change goes through
  ``update``: it takes the run's own lock, re-reads the record inside the lock
  and replaces the file atomically (P1, B3).
* ``receipt_state`` and ``unfinished_runs`` derive "already handled" and
  "still running" from the run records themselves, so there is no second
  ledger that can disagree with them (A3, B6, Z2).
* ``run_mates`` / ``reap_run_mates`` find every process that belongs to a run:
  members of the child's process group plus any process carrying the
  ``RIRI_MINI_RUN`` marker in its environment, even after it moved to another
  group or session (A2, B1).  Only a confirmed empty set counts as the end.
"""
from contextlib import contextmanager
from pathlib import Path
import copy
import fcntl
import json
import os
import stat
import re
import signal
import subprocess
import sys
import time

MARKER = 'RIRI_MINI_RUN'
_RUN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def _check_run_id(run_id):
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError('run ID の形式が不正です')
    return run_id


# Fields that, when present, must have the shape their writers give them (re-review
# 💚20261125-16 T2).  A record that reads as JSON but breaks these cannot show that
# nothing is running, so it counts as broken: no new launch while it is there, and the
# readable runs are still settled and reported.
RECORD_DICTS = ('child', 'supervisor', 'spawn', 'end_evidence', 'end_unconfirmed', 'not_started',
                'exit', 'plan', 'materials')


def record_problem(record):
    """Why a run record that reads as an object is not a consistent run record, or None."""
    state = record.get('state')
    if state is not None and not isinstance(state, str):
        return 'state'
    for name in RECORD_DICTS:
        if record.get(name) is not None and not isinstance(record[name], dict):
            return name
    mark = record.get('launch_mark')
    if mark is not None and not (isinstance(mark, str) and mark):
        return 'launch_mark'
    if state == 'launched' and not mark:
        return 'launched without launch_mark'   # the supervisor writes both in one update
    for name in ('child', 'supervisor', 'spawn'):
        pid = (record.get(name) or {}).get('pid')
        if pid is not None and type(pid) is not int:
            return name + '.pid'
    for name in ('child', 'supervisor', 'spawn'):
        start = (record.get(name) or {}).get('start')
        if start is not None and not isinstance(start, str):
            return name + '.start'
    if record.get('child') and not mark:
        return 'child without launch_mark'      # the child is started only after the mark
    # Re-review 💚20261125-20 U2: the writers' fields must fit together.  The supervisor writes
    # end evidence only after the mark; a launch failure only when no child was started; a
    # watched end and run mates still alive only after the child.
    ev = record.get('end_evidence') if isinstance(record.get('end_evidence'), dict) else None
    if ev is not None and not mark:
        return 'end_evidence without launch_mark'
    if ev is not None and ev.get('kind') == 'launch_failed' and record.get('child'):
        return 'launch_failed with a child'
    if ev is not None and ev.get('kind') == 'watched':
        if not record.get('child'):
            return 'watched without a child'
        if record.get('end_unconfirmed') is not None:
            return 'watched with unconfirmed end'   # only a later manual resolution preserves that history
    if record.get('end_unconfirmed') and not record.get('child'):
        return 'end_unconfirmed without a child'
    return None


class RunStore:
    """One record file per run under ``<root>/state/mini/runs`` (P1).

    ``check`` (optional) says why a record lacks what its user needs (the lane's required
    fields per state), or None; ``scan`` counts such records as broken too."""

    def __init__(self, root, check=None):
        self.dir = Path(root) / 'state' / 'mini' / 'runs'
        self.check = check

    def problem(self, record):
        """Why a record that reads as an object is still not a usable run record, or None."""
        found = record_problem(record)
        if found or self.check is None:
            return found
        try:
            return self.check(record)
        except Exception as exc:   # noqa: BLE001 - a check that cannot decide counts as broken
            return 'check:' + type(exc).__name__

    def _path(self, run_id):
        return self.dir / f'{_check_run_id(run_id)}.json'

    @contextmanager
    def _locked(self, run_id):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / f'{_check_run_id(run_id)}.lock').open('a', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self, run_id):
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def update(self, run_id, change):
        """Re-read the record under the run's lock, apply ``change`` and replace it.

        ``change`` gets a copy of the current record (``{}`` for a new run) and
        returns the new record.  Writers never work from a stale copy (B3), and
        a reader sees either the old or the new file, never half of it.
        """
        with self._locked(run_id):
            current = self.read(run_id) or {}
            new = change(copy.deepcopy(current))
            if not isinstance(new, dict):
                raise TypeError('run の記録は dict で返してください')
            new['run_id'] = run_id
            path = self._path(run_id)
            tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
            data = json.dumps(new, ensure_ascii=False, sort_keys=True, indent=1)
            try:
                with tmp.open('w', encoding='utf-8') as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return new

    def scan(self):
        """``(records, broken)``: the records that read as a run, and the names of the
        record files that do not (not JSON, not an object, or another run's ID).

        One broken file must not stop the work on every other run; callers that need
        the whole picture (whether anything is unfinished) treat ``broken`` as unknown."""
        if not self.dir.exists():
            return [], []
        runs, broken = [], []
        for path in sorted(self.dir.glob('*.json')):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                broken.append(path.name)
                continue
            if not isinstance(record, dict) or record.get('run_id') != path.stem or self.problem(record):
                broken.append(path.name)
                continue
            runs.append(record)
        return runs, broken

    def all_runs(self):
        """Every record, or ``ValueError`` when any record file cannot be read as a run."""
        runs, broken = self.scan()
        if broken:
            raise ValueError('読めない run の記録がある：' + ', '.join(broken[:5]))
        return runs


def read_plain(path, limit=None, *, owner_only=True, head=False):
    """(bytes, None) for a plain file (of this user unless owner_only=False), read through one descriptor opened without
    following a link (re-review 💚20261125-20 U1: the file that is checked is the file that is
    read), or (None, problem).  Never waits on a FIFO (O_NONBLOCK) and reads at most ``limit``
    bytes: みに can write the conversation files and out/last.json (and, at stage 2, its case
    folder), and a FIFO there stopped every patrol until its deadline (the 「どんどん詰めて」 round).

    ``head=True``: a longer file is not refused; its first ``limit`` bytes come back as
    (bytes, 'cut'), so a reader that only needs the beginning (the conversation ID of a long
    output) still gets it and knows that the rest was not read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, 'missing'
    except OSError:
        return None, 'not a plain file'          # a link (ELOOP) or anything else that cannot be opened
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (owner_only and info.st_uid != os.getuid()):
            return None, 'not a plain file'
        if limit is not None and info.st_size > limit and not head:
            return None, 'too large'
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b''.join(chunks), None
            total += len(chunk)
            if limit is not None and total > limit:
                if head:
                    chunks.append(chunk)
                    return b''.join(chunks)[:limit], 'cut'
                return None, 'too large'         # it grew while being read
            chunks.append(chunk)
    except OSError:
        return None, 'unreadable'
    finally:
        os.close(fd)


def receipt_state(runs, request_id, block_id, normalized_hash):
    """Decide what a request block is, from the run records alone (A3, B6).

    ``'new'``       no launched run has this request ID and block ID
    ``'processed'`` a launched run has the same request, block and body
    ``'changed'``   a launched run has the same request and block but another
                    body: hold it and tell a person, never launch again
    """
    same = [r for r in runs if r.get('launch_mark')
            and r.get('request_id') == request_id and r.get('block_id') == block_id]
    if not same:
        return 'new'
    if any(r.get('normalized_hash') == normalized_hash for r in same):
        return 'processed'
    return 'changed'


MANUAL_END_BY = ('たかむね',)    # who may record the end of a run by hand (Z2)


def end_evidence_counts(record):
    """End evidence that counts (Z2): the watcher's with every field it writes (exit
    code, reason, time and an empty run-mate set), a launch failure, or a person's
    record made after the run mates were checked.  Anything less is no evidence."""
    ev = record.get('end_evidence') if isinstance(record, dict) else None
    if not isinstance(ev, dict):
        return False
    stamped = isinstance(ev.get('at'), str) and bool(ev.get('at'))
    if ev.get('kind') == 'watched':
        return (record.get('end_unconfirmed') is None and ev.get('mates_empty') is True
                and type(ev.get('exit_code')) is int and stamped
                and ev.get('reason') in ('exited', 'stop', 'timeout'))
    if ev.get('kind') == 'launch_failed':
        return stamped and isinstance(ev.get('error'), str) and bool(ev.get('error'))
    if ev.get('kind') == 'manual':
        return ev.get('by') in MANUAL_END_BY and ev.get('mates_checked') is True and stamped
    return False


def unfinished_runs(runs):
    """Runs with a launch mark but no evidence that they ended (Z2).

    Lock files and missing PIDs are not evidence: only ``end_evidence`` that counts
    (``end_evidence_counts``), written by the watcher or by a person.
    """
    return [r for r in runs if r.get('launch_mark') and not end_evidence_counts(r)]


def child_env(base, run_id):
    """Environment for the child: the marker is inherited by every descendant."""
    env = dict(base)
    env[MARKER] = _check_run_id(run_id)
    return env


def _list_linux(run_id, pgid):
    token = f'{MARKER}={_check_run_id(run_id)}'.encode()
    mates = set()
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if (pgid is not None and os.getpgid(pid) == pgid) or _same_session(pid, pgid):
                mates.add(pid)
                continue
            with open(f'/proc/{pid}/environ', 'rb') as handle:
                if token in handle.read().split(b'\0'):
                    mates.add(pid)
        except (OSError, ProcessLookupError):
            continue
    return mates


def _same_session(pid, leader):
    """The child is started as a session leader (its PID is the session ID).  A process it
    puts in another group (job control: ``set -m``, a background ``&``) stays in that session;
    only setsid() leaves it."""
    if leader is None:
        return False
    try:
        return os.getsid(pid) == leader
    except OSError:
        return False


class Mates(set):
    """PIDs of a run's processes, with the start time (ps's lstart text) each was listed with,
    in ``starts``.  The reap signals a PID only while the process with that number still has
    that start (re-review 💚20261126-4 V5: a number listed as a run mate can belong to another
    process by the time of the signal).  A start of None is not known for sure: that PID still
    counts as alive (no end evidence), but gets no signal; the next listing decides it."""

    def __init__(self, pids=(), starts=None, *, group_leader_start=None, weak=()):
        super().__init__(pids)
        self.starts = dict(starts or {})
        self.group_leader_start = group_leader_start
        # PIDs tied to the run by the child's group or session number alone (no marker): the
        # number can name another process's group by then (re-review 💚20261127-3 W1).
        self.weak = set(weak)
        self.unsure = set()       # counted but not known to be the run's (run_mates)
        # A process in the table ended before the run's sandbox could be asked: what it started
        # may only show in the next listing, so this listing cannot show the run empty.
        self.recheck = False
        self.waiting = []         # [{'pid', 'ppid', 'state'}] of what made the listing unsettled
        self.lost = False         # the run's folder could not be asked for this listing (record-end refuses)


_ARGMAX = []           # kern.argmax, read once


def _environ_of(pid):
    """The environment strings of a process, read from the kernel apart from its arguments
    (sysctl KERN_PROCARGS2, the record ps itself reads), as a list of bytes; None when it
    cannot be read (the process ended, or is not this user's).  macOS 27 gives no environment
    for Apple's own programs (/bin/sleep, /bin/sh ...): an empty list (measured 2026-11-26)."""
    import ctypes
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    lib.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    if not _ARGMAX:
        value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        if lib.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), 'kern.argmax')          # CTL_KERN, KERN_ARGMAX
        _ARGMAX.append(value.value)
    buf, size = ctypes.create_string_buffer(_ARGMAX[0]), ctypes.c_size_t(_ARGMAX[0])
    if lib.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(size), None, 0) != 0:
        return None                                                   # CTL_KERN, KERN_PROCARGS2
    return _environ_strings(buf.raw[:size.value])


def _environ_strings(data):
    """The environment part of a KERN_PROCARGS2 record: argc, the executable path and its
    padding, argc arguments, then the environment up to the first empty string."""
    if len(data) < 4:
        return None
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    end = rest.find(b'\0')
    if end < 0:
        return None
    pos = end
    while pos < len(rest) and rest[pos] == 0:
        pos += 1
    strings = rest[pos:].split(b'\0')
    env = []
    for item in strings[argc:]:
        if not item:
            break
        env.append(item)
    return env


def _ps_env():
    """ps's environment: the start (lstart) is written in the C locale and in UTC, so a start
    the watcher keeps and one a person's command reads later (a terminal in another language or
    time zone) compare as the same process (measured 2026-11-26 on macOS 27: ja_JP gives
    「木 11/26 13:45:46 2026」, C gives "Thu Nov 26 13:45:46 2026", and TZ moves the hour)."""
    return {**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'}


def process_start(pid):
    """ps's lstart of one process (C locale, UTC), or None when it is not running or cannot be
    read."""
    try:
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(int(pid))], capture_output=True,
                             text=True, timeout=PS_TIMEOUT, env=_ps_env())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _ps_groups():
    """{pid: (pgid, start)} of this user's processes; ``start`` is ps's lstart text.  A line
    whose group or start cannot be read is kept with None there: it may be a run mate, so it is
    counted, and a mate without a known start gets no signal."""
    out = subprocess.run(['ps', '-ww', '-x', '-o', 'pid=,pgid=,lstart='], capture_output=True,
                         text=True, check=True, timeout=PS_TIMEOUT, env=_ps_env()).stdout
    rows = {}
    for line in out.split('\n'):
        parts = line.split(None, 2)
        if not parts or not parts[0].isdigit():
            continue
        group = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        start = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        rows[int(parts[0])] = (group, start)
    return rows


def _list_ps(run_id, pgid):
    """Run mates on macOS: the child's group, the child's session, or the marker in the
    environment.  The environment is read from the kernel apart from the arguments
    (``_environ_of``), so a process that merely has the marker text in its arguments (a
    person's ``grep``) is never a mate, and nothing depends on two listings agreeing
    (re-review 💚20261126-4 V5: the environment listing of ps -E and the plain one were
    compared, and a process whose number or arguments changed between the two could be taken
    for a mate).  macOS 27 does not give the environment of Apple's own programs (/bin/sleep,
    /bin/sh ...), so the session and Descendants are what find those when they left the group
    (measured 2026-11-25)."""
    token = f'{MARKER}={_check_run_id(run_id)}'.encode()
    groups = _ps_groups()
    found = Mates(group_leader_start=groups.get(pgid, (None, None))[1])
    for pid, (group, start) in groups.items():
        env = _environ_of(pid)
        marked = bool(env) and token in env
        if (pgid is not None and group == pgid) or _same_session(pid, pgid):
            found.add(pid)
            if not marked:
                found.weak.add(pid)       # the number alone ties it to the run (W1)
        elif marked:
            found.add(pid)
        else:
            continue
        found.starts[pid] = start
    return found



# The watcher looks at the process table every TRACK_EVERY while the child runs, between its
# STOP and time-limit checks: a ps that does not return must not keep those from applying (the
# same kind as review R5; the 「80% まで止まらずに」 round).  A look that times out is skipped
# (subprocess.TimeoutExpired); the reap that cannot list the run mates leaves no end evidence.
PS_TIMEOUT = 5.0


def _sandboxed(pid):
    """True / False whether a process runs in a sandbox, None when it cannot be told (not macOS).

    Every process the run starts inherits the run's outer sandbox and cannot leave it, whatever
    session or group it moves to (sandbox_check with no filter; measured 2026-11-25 on macOS 27:
    a setsid grandchild reparented to launchd answers 1, an ordinary process 0)."""
    if sys.platform != 'darwin':
        return None
    try:
        import ctypes
        lib = ctypes.CDLL('/usr/lib/libsandbox.1.dylib')
        result = lib.sandbox_check(ctypes.c_int(pid), None, ctypes.c_int(0))
    except (OSError, AttributeError):
        return None
    return {1: True, 0: False}.get(result)


_SANDBOX_FILTER_PATH = 1
_SANDBOX_CHECK = []


def _sandbox_check():
    """libsandbox's sandbox_check and the filter type for a path that logs no report, set up
    once.  The path is the variadic argument: the pid, the operation and the filter type are
    declared and the path is passed beyond them, so ctypes passes it the way arm64 passes
    variadic arguments (on the stack).  Declared as four fixed arguments, every process gave
    the same answer (measured 2026-11-28).  SANDBOX_CHECK_NO_REPORT is the library's exported
    constant (0x40000000 on macOS 27), read from it rather than written here."""
    if not _SANDBOX_CHECK:
        import ctypes
        lib = ctypes.CDLL('/usr/lib/libsandbox.1.dylib')
        check = lib.sandbox_check
        check.restype = ctypes.c_int
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        no_report = ctypes.c_uint32.in_dll(lib, 'SANDBOX_CHECK_NO_REPORT').value
        _SANDBOX_CHECK.append((check, ctypes.c_char_p, _SANDBOX_FILTER_PATH | no_report))
    return _SANDBOX_CHECK[0]


def sandbox_probes(run_dir):
    """The folders of a run that tell its sandbox, or None when they are not all there as
    folders (not links): (work, out, input).

    profile.sb lets every process of the run write into <run>/work and <run>/out and keeps all
    of them out of <run>/input.  libsandbox answers "denied" for a path that does not exist,
    so the folders must be there (freeze makes them before the launch).  A run can remove or
    replace work and out itself; the sandbox then cannot be asked (the watcher holds the run)."""
    if sys.platform != 'darwin' or not isinstance(run_dir, str) or not run_dir:
        return None
    if _proc_state(os.getpid()) != 'alive':
        return None                  # the process states cannot be read here: the answers cannot be weighed
    base = os.path.realpath(run_dir)
    paths = tuple(os.path.join(base, name) for name in ('work', 'out', 'input'))
    ids = _folder_ids(paths, base)
    if ids is None:
        return None
    return Probes((os.fsencode(path) for path in paths), paths=paths, ids=ids, base=base)


class Probes(tuple):
    """The run's folders as libsandbox is asked about them, with the folders' identities (device
    and inode) when they were taken: an answer counts only while the same folders are still there
    (third re-review: a run that removes and makes its out folder again gave partial answers)."""

    def __new__(cls, items, paths=(), ids=None, base=None):
        made = super().__new__(cls, items)
        made.paths, made.ids, made.base = tuple(paths), ids, base
        return made

    def steady(self):
        return self.ids is None or _folder_ids(self.paths, self.base) == self.ids


def _folder_ids(paths, base=None):
    """(device, inode) of each folder, and the run folder's own inode, modification and change
    times: moving work or out away and back keeps their inodes, but always changes the run
    folder's times (fourth review of the unknown-answer change, measured on macOS 27)."""
    ids = []
    if base is not None:
        try:
            info = os.lstat(base)
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        ids.append((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None              # missing, a link or a file
        ids.append((info.st_dev, info.st_ino))
    return tuple(ids)


def run_sandbox_state(pid, probes):
    """Whether a process runs in the run's sandbox: 'run', 'none' (in no sandbox), 'other'
    (another sandbox) or None (cannot be told: not macOS, the process is gone, no probes).

    Every process the run starts inherits the run's sandbox and cannot leave it, nor enter
    another one (sandbox_apply inside a sandbox is refused), whatever session, group or parent
    it ends up with and whatever its environment (re-review 💚20261127-3 W1 and W5).  Only this
    run's sandbox lets a process write into this run's work and out folders and not into its
    input folder (measured 2026-11-28 on macOS 27 over the 635 processes of this user: no other
    process gave that answer; a command the run left behind in a session of its own,
    reparented to launchd, did)."""
    if probes is None:
        return None
    # libsandbox answers "denied" everywhere for a process that ended, a zombie, and one that is
    # ending (P_WEXIT: measured 2026-11-28, about 35 ms after a SIGKILL while p_stat still says
    # running, once even a partial answer): an answer counts only from a process that runs
    # before and after it was asked (third re-review of the lineage change).
    if _proc_state(pid) != 'alive':
        return None
    try:
        check, path, kind = _sandbox_check()
        boxed = check(int(pid), None, 0)
        answers = (tuple(check(int(pid), b'file-write-data', kind, path(probe)) for probe in probes)
                   if boxed == 1 else ())
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    if _proc_state(pid) != 'alive':
        return None
    if boxed == 0:
        return 'none'
    if boxed != 1:
        return None
    if answers == (0, 0, 1):
        return 'run'
    if answers in ((0, 1, 1), (1, 0, 1)):
        return None              # only one of work and out: the run's own, with a folder going or coming
    if all(answer in (0, 1) for answer in answers):
        return 'other'
    return None


class Table(dict):
    """{pid: (ppid, start)}, with ``groups`` {pid: pgid} from the same ps."""

    def __init__(self, rows=(), groups=None):
        super().__init__(rows)
        self.groups = dict(groups or {})


def _process_table():
    """{pid: (ppid, start)} of this user's processes; ``start`` is ps's lstart text.  The process
    group each was in at that moment is kept too (``groups``): one that ends before anything else
    can be read of it is still told by its group's leader (fourth review: orphans handed to
    launchd on a busy Mac)."""
    out = subprocess.run(['ps', '-ww', '-x', '-o', 'pid=,ppid=,pgid=,lstart='], capture_output=True,
                         text=True, check=True, timeout=PS_TIMEOUT, env=_ps_env()).stdout
    rows, groups = {}, {}
    for line in out.split('\n'):
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[3].strip())
            if parts[2].isdigit():
                groups[int(parts[0])] = int(parts[2])
    return Table(rows, groups)


_BOOT = []


def boot_time():
    """When this Mac started (kern.boottime, seconds), or None.  Birth numbers start again at each
    start, so ones kept from another start name other processes."""
    if not _BOOT:
        value = None
        try:
            import ctypes

            class Timeval(ctypes.Structure):
                _fields_ = [('sec', ctypes.c_long), ('usec', ctypes.c_int32)]
            tv, size = Timeval(), ctypes.c_size_t(ctypes.sizeof(Timeval))
            lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            if lib.sysctlbyname(b'kern.boottime', ctypes.byref(tv), ctypes.byref(size), None, 0) == 0 and tv.sec > 0:
                value = int(tv.sec)
        except (OSError, AttributeError):
            pass
        _BOOT.append(value)
    return _BOOT[0]


_LIBPROC = []
_PROC_PIDUNIQIDENTIFIERINFO = 17
LAUNCHD_BIRTH = 1           # launchd's unique number (the first process; measured 2026-11-28)
PID_MAX = 99999             # macOS hands out process numbers in turn up to this, skipping those in use
_REUSE_GAP = []


def reuse_gap():
    """How many processes must start before a number can be handed out again: macOS gives
    numbers in turn up to PID_MAX and skips the ones in use as a process, a group or a session
    number (each at most kern.maxproc, 8000 on this Mac), and every process takes one birth
    number.  0 when kern.maxproc cannot be read (the proof is then not used)."""
    if not _REUSE_GAP:
        gap = 0
        try:
            import ctypes
            value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
            lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            if lib.sysctlbyname(b'kern.maxproc', ctypes.byref(value), ctypes.byref(size), None, 0) == 0:
                gap = max(0, PID_MAX - 100 - 3 * value.value - 1000)    # 1000: margin
        except (OSError, AttributeError):
            pass
        _REUSE_GAP.append(gap)
    return _REUSE_GAP[0]


def _lineage(pids):
    """{pid: (birth, parent_birth)} of the processes of ``pids`` that can be read, macOS only
    ({} elsewhere).  ``birth`` is the kernel's unique number of the process (p_uniqueid: handed
    out in the order processes start and never again) and ``parent_birth`` the unique number of
    the process that started it (p_puniqueid).  The second stays when that parent ends and the
    process is handed to launchd (re-review 💚20261127-3 W1 / W5: a record of belonging that
    outlives the parent).  Measured 2026-11-28 on macOS 27: a /bin/sleep left in a session of its
    own by a short process, reparented to launchd, still named that process's number.  A
    process that execs after it was handed to launchd names launchd (1) from then on."""
    if sys.platform != 'darwin':
        return {}
    try:
        if not _LIBPROC:
            import ctypes

            class Info(ctypes.Structure):          # struct proc_uniqidentifierinfo
                _fields_ = [('uuid', ctypes.c_uint8 * 16), ('uniqueid', ctypes.c_uint64),
                            ('puniqueid', ctypes.c_uint64), ('idversion', ctypes.c_int32),
                            ('orig_ppidversion', ctypes.c_int32), ('reserve2', ctypes.c_uint64),
                            ('reserve3', ctypes.c_uint64)]
            info = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidinfo
            info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            info.restype = ctypes.c_int
            _LIBPROC.append((info, Info, ctypes))
        pidinfo, Info, ctypes = _LIBPROC[0]
    except (OSError, AttributeError):
        return {}
    out = {}
    for pid in pids:
        data = Info()
        try:
            got = pidinfo(int(pid), _PROC_PIDUNIQIDENTIFIERINFO, 0, ctypes.byref(data), ctypes.sizeof(data))
        except (TypeError, ValueError, OverflowError, ctypes.ArgumentError):
            continue
        if got == ctypes.sizeof(data) and data.uniqueid:
            out[int(pid)] = (int(data.uniqueid), int(data.puniqueid))
    return out


def _all_pids():
    """The numbers of every running process (any user), or None when they cannot be listed."""
    if sys.platform != 'darwin':
        return None
    try:
        import ctypes
        listing = ctypes.CDLL('/usr/lib/libproc.dylib').proc_listallpids
        listing.argtypes = [ctypes.c_void_p, ctypes.c_int]
        listing.restype = ctypes.c_int
        size = listing(None, 0)
        if size <= 0:
            return None
        buf = (ctypes.c_int * (size + 256))()
        got = listing(buf, ctypes.sizeof(buf))
    except (OSError, AttributeError):
        return None
    if got <= 0:
        return None
    return {pid for pid in buf[:min(got, len(buf))] if pid > 0}


_KINFO = []
_SZOMB = 5
_P_WEXIT = 0x2000


def _proc_state(pid):
    """'alive', 'exiting', 'zombie' or 'gone' for a process number, from the kernel's record of it
    (sysctl KERN_PROC_PID, what ps reads), or None when it cannot be told (not macOS).  Measured
    2026-11-28 on macOS 27: p_stat 2 for a running process, 5 for a zombie, no record for one
    that is gone, and P_WEXIT (0x2000) in p_flag for about 35 ms while a large process ends after
    a SIGKILL (p_stat still 2); it does not depend on reading the birth numbers."""
    if sys.platform != 'darwin':
        return None
    try:
        import ctypes
        if not _KINFO:
            lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            lib.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
            _KINFO.append(lib)
        lib = _KINFO[0]
        buf, size = ctypes.create_string_buffer(1024), ctypes.c_size_t(1024)
        if lib.sysctl((ctypes.c_int * 4)(1, 14, 1, int(pid)), 4, buf, ctypes.byref(size), None, 0) != 0:
            return None                      # CTL_KERN, KERN_PROC, KERN_PROC_PID
    except (OSError, AttributeError, ValueError, TypeError, OverflowError):
        return None
    if size.value == 0:
        return 'gone'
    raw = buf.raw[:size.value]
    if len(raw) < 44 or int.from_bytes(raw[40:44], sys.byteorder) != int(pid):
        return None                          # extern_proc: p_flag at 32, p_stat at 36, p_pid at 40
    if raw[36] == _SZOMB:
        return 'zombie'
    return 'exiting' if int.from_bytes(raw[32:36], sys.byteorder) & _P_WEXIT else 'alive'


class Descendants:
    """Every process started under the child, kept by PID and start time, and the sessions
    they lead (B1 on macOS 27, the 「80% まで止まらずに」 round).

    Codex runs each tool command in a session of its own, and macOS 27 hides the environment
    (the run marker) of Apple's own programs, so a command like /bin/sleep that outlives
    Codex was found neither by group, session nor marker.  The watcher observes the process
    tree while the child is alive, so a command is known while it is still Codex's child; a
    program it leaves behind stays in the command's session.  A session counts only for a
    process that runs in a sandbox, and not while another process with the leader's number
    runs (reused).

    On macOS every process also carries the birth number of the process that started it
    (_lineage), which stays when that one ends.  The run's processes are followed by that
    number from the child's own, also when they were started after the last look and their
    parent ended meanwhile, as when the child itself ended by itself (re-review 💚20261127-3
    W5), and a process that merely has the child's group number is told from one of the run
    (W1).  A process whose parent started after the last look and ended unseen cannot be
    followed: it is ``unsure`` (counted, never signalled), unless its session says whose it is.
    The run's sandbox (``calibrated``) then tells it.  Residual where the run's sandbox cannot be
    asked: a process that makes a session of its own and execs only after its parent ended (the
    kernel then names launchd as its parent), and, without birth numbers (not macOS), an Apple
    program that leaves its session before it is seen."""

    def __init__(self, root, pids=None, sessions=None, root_start=None, *, run_dir=None,
                 calibrated=False, births=None, unsure=None, doubt=None):
        self.root = root
        self.root_start = root_start          # the child's own start, once seen
        self.pids = dict(pids or {})          # pid -> start
        self.sessions = set(sessions or ())
        self.run_dir = run_dir                # the run folder whose sandbox tells the run's processes
        # The child itself was seen in the run's sandbox (run_sandbox_state 'run'), so the
        # sandbox answers for this run on this machine: from then on it finds every process of
        # the run, whatever session, group or parent, and tells one that is not.
        self.calibrated = calibrated
        self.births = set(births or ())       # macOS: birth numbers of the run's processes
        self.unsure = dict(unsure or {})      # pid -> start of the processes that cannot be followed
        self.doubt = set(doubt or ())         # their birth numbers (their children are unsure too)
        self.foreign = set()                  # the watcher's: birth numbers it followed and found not the run's
        self.session_maker = {}               # session -> birth number of the run process that made it
        self.birth_of = {}                    # pid -> birth number of the run's processes seen running
        self.boxed = False                    # the child was seen in a sandbox (any): so is every process of the run
        self.live = False                     # the watcher's own (not one rebuilt from a record)
        self.lost_sandbox = False             # the run's sandbox could not be asked once after it was confirmed
        self.ask_sandbox = False              # a person's record-end: ask the run's folder even unconfirmed
        self.boot = None                      # the Mac's start the birth numbers belong to
        self.seen_run = {}                    # pid -> start of processes the run's sandbox told the run's
        self.last_births = {}                 # pid -> birth number read at the latest listing
        # The child was waited for: its number can belong to another process from then on, so it
        # is no starting point for the parentage walk (review of afc97e6: the reap took the
        # children of a process that got the child's number for the run's).
        self.root_done = False
        # True: followed by birth number; False: the child's could not be read (macOS); None:
        # not tried (not macOS, or a record without them).
        self.lineage = True if self.births else None
        self.mark = None                      # the watcher's: highest birth number its last look read

    def start_lineage(self):
        """The watcher reads the child's birth number right after the spawn, before the child can
        start anything: every later process of the run descends from it."""
        if sys.platform != 'darwin' or self.root is None:
            return
        got = _lineage([self.root]).get(self.root)
        if got is None:
            self.lineage = False
            return
        self.births.add(got[0])
        self.mark = got[0]
        self.lineage = True
        self.live = True
        self.session_maker[self.root] = got[0]
        self.boot = boot_time()

    def reused(self, number, rows):
        """True when ``number`` (a group or session ID this run used) now belongs to another
        process: one with that PID runs with a different start time.  An ID is never handed out
        again while a group or session of that ID still has a member (POSIX), so a reused one
        holds no process of this run and must name none."""
        known = self.root_start if number == self.root else self.pids.get(number)
        current = rows.get(number)
        return bool(known and current and current[1] != known)

    def observe(self, rows=None):
        """Add what is under the root now.  Returns True when something new was learnt.

        With birth numbers (macOS) the run's processes are followed by them alone (follow): a
        parent's number names only that process, while a PID can belong to another process by
        then.  Without them, by the parentage walk from the child while it runs."""
        rows = _process_table() if rows is None else rows
        here = (not self.root_done and self.root in rows
                and (self.root_start is None or rows[self.root][1] == self.root_start))
        if self.root_start is None and here:
            self.root_start = rows[self.root][1]
        live = {pid for pid, start in self.pids.items() if rows.get(pid, (None, None))[1] == start}
        frontier = ({self.root} if here else set()) | live
        new = False
        grew = not self.lineage
        while grew:
            grew = False
            for pid, (ppid, start) in rows.items():
                if ppid in frontier and pid not in frontier:
                    frontier.add(pid)
                    grew = True
                    if self.pids.get(pid) != start:
                        self.pids[pid] = start
                        new = True
        new = self.follow(rows, live=self.live) or new
        known = {pid for pid, start in self.pids.items() if rows.get(pid, (None, None))[1] == start}
        for pid in frontier | known:
            try:
                sid = os.getsid(pid)
            except OSError:
                continue
            if sid == pid and pid in self.birth_of:
                self.session_maker.setdefault(sid, self.birth_of[pid])
            if pid != self.root and sid not in self.sessions:
                self.sessions.add(sid)
                new = True
        return new

    def follow(self, rows, live=False):
        """Follow the birth numbers (macOS): every running process started by one of the run's
        is the run's.  A process started after the watcher's last look whose parent is not known
        (it started after that look too and ended unseen, or was never read, or the kernel names
        launchd because it execed after its parent ended) cannot be followed and is ``unsure``,
        unless its session tells (a session holds only processes started under the one that
        made it), or it runs in no sandbox while the child ran in one.  ``live``: the watcher,
        which also keeps what it found not the run's and moves its mark.  A person's record-end
        uses the watcher's last mark the same way.  Returns True when something new was learnt."""
        if not self.lineage:
            return False
        lin = _lineage(rows)
        self.last_births = {pid: birth for pid, (birth, _) in lin.items()}
        for pid, start in self.pids.items():
            if pid in lin and rows.get(pid, (None, None))[1] == start:
                self.births.add(lin[pid][0])
                self.birth_of[pid] = lin[pid][0]
        running = {birth: pid for pid, (birth, _) in lin.items()}
        judge = self.mark is not None
        others = {}
        if judge:
            listed = _all_pids()
            others = _lineage(listed - set(lin)) if listed else {}    # other users' (a parent can be one)
        # launchd always runs: a process the kernel names it the parent of is judged by its session
        # (third review of the lineage change: with launchd among the running parents it never was)
        alive = (set(running) | {b for b, _ in others.values()}) - {0, LAUNCHD_BIRTH}
        makers = {**others, **lin}
        new = False
        for birth, pid in sorted(running.items()):        # a parent is always born before its child
            if birth in self.births:
                continue
            parent = lin[pid][1]
            ours, doubt = parent in self.births, parent in self.doubt
            if not ours and not doubt and judge and birth > self.mark and parent not in alive:
                if parent <= LAUNCHD_BIRTH:
                    # Started by launchd, or handed to launchd before its exec (the kernel then
                    # names launchd as the parent; measured 2026-11-28): its session tells.
                    ours, doubt = self._by_session(pid, birth, rows, makers, own_session_unsure=False)
                elif parent > self.mark or (live and parent not in self.foreign):
                    # Its parent started after the last look and ended unseen, or was not read.
                    ours, doubt = self._by_session(pid, birth, rows, makers, own_session_unsure=True)
                if doubt and self.boxed and _sandboxed(pid) is False:
                    doubt = False          # every process of the run is in the child's sandbox
            start = rows.get(pid, (None, None))[1]
            if ours:
                self.births.add(birth)
                self.birth_of[pid] = birth
                self.unsure.pop(pid, None)
                if start and self.pids.get(pid) != start:
                    self.pids[pid] = start
                    new = True
            elif doubt:
                self.doubt.add(birth)
                if start and self.unsure.get(pid) != start:
                    self.unsure[pid] = start
                    new = True
            elif live:
                self.foreign.add(birth)
        if live and running:
            self.mark = max(self.mark, max(running))
        return new

    def _by_session(self, pid, birth, rows, lin, own_session_unsure):
        """(ours, unsure) for a new process whose parent is not known, by its session.  A session
        holds only processes started under the one that made it (it cannot be joined), and its
        number is not handed out again while it has a member: while its maker runs, the maker
        tells; the child's session and the run's sessions hold only the run's; launchd's holds
        none.  One that made a session of its own: ``own_session_unsure`` (its parent ended
        unseen) or not the run's (started by launchd, as launchd's jobs are, or a process that
        made its own session and was handed to launchd before its exec, which the run's sandbox
        then tells when it can be asked)."""
        try:
            sid = os.getsid(pid)
        except OSError:
            return False, True
        if sid == pid:
            return False, own_session_unsure
        if sid <= 1:
            return False, False
        maker = lin.get(sid)
        if maker is not None:
            return maker[0] in self.births, maker[0] in self.doubt
        # The maker ended: the number alone does not say whose the session is.  Once the run's
        # session had no member left, the number could go to another process and its session
        # (review of afc97e6, the same kind as W1).  It can only after nearly every other number
        # was handed out since it was handed to the session's maker: for one of the run's
        # sessions (a run process was seen in it), made at the earliest when the child started,
        # a process that started fewer processes (birth numbers) after the maker (or the child,
        # when the maker was not seen) is still in the run's session.
        if (sid == self.root or sid in self.sessions) and not self.reused(sid, rows):
            origin = self.session_maker.get(sid, min(self.births) if self.births else None)
            if origin is not None and birth - origin < reuse_gap():
                return True, False
        return False, True

    def told(self, pid, ours):
        """The run's sandbox told whose an unsure process is: its birth number moves from ``doubt``
        (whose children are unsure too) to the run's or to the others."""
        got = _lineage([pid]).get(pid)
        if got is None:
            return
        self.doubt.discard(got[0])
        (self.births if ours else self.foreign).add(got[0])

    def first_birth(self):
        """The child's birth number (the smallest of the run's), or 0 when not known."""
        return min(self.births) if self.births else 0

    def foreign_birth(self, birth):
        """True when the watcher followed the process of ``birth`` and found it not the run's."""
        return birth in self.foreign and birth not in self.births and birth not in self.doubt

    def probes(self):
        """The run's sandbox probes when the sandbox is known to answer for this run, else None.
        Once they could not be had after the sandbox was confirmed, that is remembered: what was
        reaped meanwhile was reaped without them (fourth review: a run that removed out and made
        it again)."""
        if not self.calibrated:
            return None
        found = sandbox_probes(self.run_dir)
        if found is None:
            self.lost_sandbox = True
        return found

    def calibrate(self):
        """See whether the child runs in the run's sandbox (and in a sandbox at all).  True when
        something is newly known.  Only while the child runs.  The run's answer (work and out
        writable, input not) never comes from a process that ended (libsandbox denies everything
        then), so it needs nothing more; "in some sandbox" does come from one that ended, so it
        counts only when the child's birth number reads the same before and after."""
        if self.root is None or self.root_done or (self.calibrated and self.boxed):
            return False
        found = False
        if not self.calibrated and run_sandbox_state(self.root, sandbox_probes(self.run_dir)) == 'run':
            self.calibrated = found = True
        if not self.boxed:
            before = _lineage([self.root]).get(self.root)
            if before is not None and (not self.births or before[0] in self.births) \
                    and _sandboxed(self.root) is True and _lineage([self.root]).get(self.root) == before:
                self.boxed = found = True
        return found

    def unfollowed(self):
        """macOS: the child's birth number could not be read and the run's sandbox cannot be
        asked, so what the child started after the last look is not followed (W5), however the
        child ended (by itself, STOP, the time limit)."""
        return self.lineage is False and self.probes() is None

    def sandbox_lost(self):
        """The child was seen in the run's sandbox, but the sandbox could not be asked at some point
        (the run removed or replaced its work, out or input folder)."""
        return self.calibrated and (self.probes() is None or self.lost_sandbox)

    def alive_parts(self, rows=None):
        """(known, in_sessions, unsure): known descendants still running (the same PID and start),
        the other sandboxed processes in their sessions (tied by the session number), and the
        processes that could not be followed and still run."""
        rows = _process_table() if rows is None else rows
        found = {pid for pid, start in self.pids.items() if rows.get(pid, (None, None))[1] == start}
        doubt = {pid for pid, start in self.unsure.items() if rows.get(pid, (None, None))[1] == start} - found
        members = set()
        sessions = {sid for sid in self.sessions if not self.reused(sid, rows)}
        if sessions:
            for pid in rows:
                if pid in found or pid == os.getpid():
                    continue
                try:
                    sid = os.getsid(pid)
                except OSError:
                    continue
                if sid in sessions and _sandboxed(pid):
                    members.add(pid)
        return found, members - doubt, doubt

    def alive(self, rows=None):
        """PIDs of known descendants still running, sandboxed processes in their sessions, and the
        processes that could not be followed."""
        found, members, doubt = self.alive_parts(rows)
        return found | members | doubt

    def as_record(self):
        return {'pids': {str(pid): start for pid, start in sorted(self.pids.items())},
                'sessions': sorted(self.sessions), 'root_start': self.root_start,
                'calibrated': self.calibrated, 'births': sorted(self.births),
                'unsure': {str(pid): start for pid, start in sorted(self.unsure.items())},
                'doubt': sorted(self.doubt), 'boxed': self.boxed, 'mark': self.mark, 'boot': self.boot}

    @classmethod
    def from_record(cls, root, data, run_dir=None):
        """From a run record; parts that are not of the expected shape are left out."""
        data = data if isinstance(data, dict) else {}

        def by_pid(value):
            value = value if isinstance(value, dict) else {}
            return {int(k): v for k, v in value.items() if isinstance(k, str) and k.isdigit()
                    and isinstance(v, str)}
        def numbers(value, low):
            return {x for x in (value if isinstance(value, list) else []) if type(x) is int and x > low}
        root_start = data.get('root_start') if isinstance(data.get('root_start'), str) else None
        tracked = cls(root, by_pid(data.get('pids')), numbers(data.get('sessions'), 1), root_start,
                      run_dir=run_dir, calibrated=data.get('calibrated') is True,
                      births=numbers(data.get('births'), 0), unsure=by_pid(data.get('unsure')),
                      doubt=numbers(data.get('doubt'), 0))
        tracked.root_done = True          # from a record: the watcher and its child are gone
        tracked.boxed = data.get('boxed') is True
        boot = data.get('boot')
        tracked.boot = boot if type(boot) is int else None
        now = boot_time()
        if tracked.lineage and tracked.boot is not None and now is not None and tracked.boot != now:
            # Kept from another start of the Mac: the numbers name other processes now, and no
            # process of the run outlived that start
            tracked.births, tracked.doubt, tracked.lineage = set(), set(), None
        mark = data.get('mark')
        if tracked.lineage and type(mark) is int and mark > 0:
            tracked.mark = mark           # the watcher's last look: what started later is judged
        return tracked


def run_mates(run_id, pgid=None, tracked=None):
    """PIDs of processes that belong to the run (B1).

    Same-user processes only: the child's group, the child's session (the child is a
    session leader) and the marker in the environment, and with ``tracked`` the processes the
    watcher followed (Descendants).  On macOS a process tied to the run by the group or session
    number alone is signalled only when its birth number says it is the run's; one the watcher
    followed and found not to be the run's is left out (W1), and one that cannot be told is
    counted but never signalled.  When the run's sandbox answers for the run (``calibrated``)
    every process in it is a run mate.  Without birth numbers (not macOS), a process that left
    the session (setsid) is seen only by the marker; one that execs with a cleared environment
    after leaving the session cannot be seen (the residual risk of decision 5).
    """
    _check_run_id(run_id)
    lister = _list_linux if os.path.isdir('/proc') and sys.platform.startswith('linux') else _list_ps
    rows = _process_table()       # the start of every mate (V5), and Descendants' look
    if tracked is not None and pgid is not None and tracked.reused(pgid, rows):
        pgid = None       # the child's group and session ID now belong to another process
    if tracked is not None:
        # What was started since the watcher's last look, also after the child ended (W5); a
        # person's record-end only follows the birth numbers the watcher kept.
        if tracked.live:
            tracked.observe(rows)
        else:
            tracked.follow(rows)
    found = lister(run_id, pgid)
    group_leader_start = getattr(found, 'group_leader_start', None)
    if tracked is not None and pgid is not None and group_leader_start is not None:
        # The group snapshot is newer than the parentage table above.  The same number
        # can still name a different group by then, even if the old child was absent
        # from the first table.  Drop the numeric match and keep independent markers.
        group_leader = {pgid: (None, group_leader_start)}
        if tracked.reused(pgid, group_leader):
            rows = {**rows, **group_leader}    # the tracked-session lookup must drop it too
            found = lister(run_id, None)
    listed = getattr(found, 'starts', {})
    weak = set(getattr(found, 'weak', ()))
    mates = set(found)
    unsure = set()
    recheck = lost = False
    known = set()
    waiting = []
    if tracked is not None:
        known, in_sessions, doubt = tracked.alive_parts(rows)
        mates |= known | in_sessions | doubt
        weak = (weak | in_sessions) - known
        unsure |= doubt
        if tracked.lineage is not None and (weak - unsure):
            # macOS: the number alone does not say whose a process is (W1).
            lin = _lineage(weak)
            for pid in weak - unsure:
                got = lin.get(pid)
                if got is not None and got[0] in tracked.births:
                    continue                             # the run's, by birth
                if got is not None and tracked.foreign_birth(got[0]):
                    mates.discard(pid)                   # the watcher followed it: not the run's
                else:
                    unsure.add(pid)                      # cannot be told: counted, never signalled
        if tracked.boxed:
            for pid in list(unsure):
                if _sandboxed(pid) is False:             # the child ran in a sandbox, this one in none
                    unsure.discard(pid)
                    mates.discard(pid)
                    tracked.unsure.pop(pid, None)
        probes = tracked.probes()
        adding = False
        if probes is None and tracked.ask_sandbox:
            # A person's record-end in a run never confirmed in its sandbox (the watcher was lost
            # before): the run's folder still tells the run's processes, which are added; nothing
            # is left out by it (second re-review 💚20261128-3, the second severe finding).
            probes, adding = sandbox_probes(tracked.run_dir), True
            lost = probes is None
        if probes is not None:
            # The run's sandbox answers for this run (the child was seen in it), and no process of
            # the run can leave it or enter another: every process in it is the run's, wherever it
            # went; any other is not, whatever ties it to the run (a number, a birth number, the
            # marker).  An answer that could not be had is no "not the run's" (second re-review
            # 💚20261128-3, the first severe finding): see below.
            states = {pid: run_sandbox_state(pid, probes) for pid in set(rows) | mates}
            groups = getattr(rows, 'groups', {})
            if not getattr(probes, 'steady', lambda: True)():
                # The folders went or changed while the sandbox was asked: no answer of this
                # listing counts, and the run's sandbox cannot be relied on any more.
                states, lost, recheck = {}, True, True
                if tracked.calibrated:
                    tracked.lost_sandbox = True
            me = os.getpid()

            def maybe_ours(pid):
                # One that ended (or is ending) before it could be told: a child is in its parent's
                # sandbox, so a parent that runs and is told not the run's clears it; the
                # watcher's own ps, too; and one the watcher followed by birth and found not the
                # run's (never told the run's at an earlier listing), or born before the child.
                # Otherwise what it started may show only in the next listing (reviews: without
                # these, a busy Mac never settled).
                if pid == tracked.root and not tracked.root_done:
                    return True
                if pid in known or tracked.seen_run.get(pid) == rows.get(pid, (None, None))[1]:
                    return True
                ppid = rows.get(pid, (None, None))[0]
                if ppid == me:
                    return False
                birth = tracked.last_births.get(pid)
                if birth is not None and (birth in tracked.foreign or birth < tracked.first_birth()):
                    return False
                if states.get(ppid) in ('none', 'other'):
                    return False
                # Its group's leader, alive and told not the run's: a group lies within one session,
                # and a session holds only processes started under the one that made it.
                leader = groups.get(pid)
                if leader not in (None, pid) and states.get(leader) in ('none', 'other'):
                    return False
                return True

            def wait_for(pid, kind):
                waiting.append({'pid': pid, 'ppid': rows.get(pid, (None, None))[0], 'state': kind})
            for pid, state in states.items():
                if state == 'run':
                    mates.add(pid)
                    unsure.discard(pid)
                    start = rows.get(pid, (None, None))[1]
                    if start:
                        tracked.seen_run[pid] = start
                    if pid in tracked.unsure and start:
                        tracked.pids[pid] = tracked.unsure.pop(pid)   # told: the run's (the record too)
                        tracked.told(pid, True)
                    continue
                if state is None:
                    kind = _proc_state(pid)
                    if kind == 'alive':
                        mates.add(pid)                   # runs, but cannot be told: counted, never signalled
                        unsure.add(pid)
                        continue
                    if pid in mates:
                        if kind in ('zombie', 'gone') and not adding:
                            mates.discard(pid)           # cannot start anything, nor be signalled
                            unsure.discard(pid)
                        elif kind not in ('zombie', 'gone'):
                            unsure.add(pid)              # ending: counted until it is gone
                    if kind is None or maybe_ours(pid):
                        recheck = True
                        wait_for(pid, kind)
                    continue
                if pid in mates and not adding:          # 'none' or 'other'
                    if state == 'other' and pid in known:
                        unsure.add(pid)                  # followed as the run's: an 'other' is no proof
                    else:
                        mates.discard(pid)
                        unsure.discard(pid)
                        if tracked.unsure.pop(pid, None) is not None:  # told: not the run's
                            tracked.told(pid, False)
    mates.discard(os.getpid())
    result = Mates(mates)
    result.unsure = unsure & mates            # counted but not known to be the run's (record-end names them)
    result.recheck = recheck
    result.waiting = waiting[:5]              # what the listing waits for (record-end and the hold name them)
    result.lost = lost                        # the run's sandbox could not be asked for this listing
    for pid in mates:
        seen = rows.get(pid, (None, None))[1]
        start = listed.get(pid, seen)
        # Two looks that disagree on a number's start: it changed hands in between, and which
        # process is meant is not known (counted, never signalled).
        result.starts[pid] = start if (seen is None or start == seen) and pid not in unsure else None
    return result


def still_the_same(pids):
    """The PIDs of ``pids`` that may be signalled now: each still names the process it was
    listed as (the same start), looked at right before the signal (re-review 💚20261126-4 V5).
    A PID listed without a start of its own (a list from elsewhere) is kept as it is; one whose
    start is not known, or differs now, or is gone, is not signalled.  ps's lstart is to the
    second, and macOS hands out process numbers in turn up to 99999, so a number does not come
    round again within the same second."""
    starts = getattr(pids, 'starts', None)
    if not starts:
        return sorted(pids)
    now = _process_table()
    keep = []
    for pid in sorted(pids):
        if pid in starts:
            want = starts[pid]
            if want is None or now.get(pid, (None, None))[1] != want:
                continue
        keep.append(pid)
    return keep


def reap_run_mates(run_id, pgid=None, grace=30.0, poll=0.2, lister=run_mates, kill=os.kill, report=None):
    """Stop every run mate and confirm the set is empty.

    Sends SIGTERM, waits up to ``grace`` seconds, then SIGKILL.  Returns True
    only when an empty set was observed; the caller must treat False as "no
    end evidence" and hold the run (4-4).
    """
    def alive():
        return lister(run_id, pgid)

    def settled(pids):
        # Empty, and nothing in the listing ended before it could be told (Mates.recheck).
        if report is not None:
            report['unsettled'] = not pids and bool(getattr(pids, 'recheck', False))
            report['waiting'] = list(getattr(pids, 'waiting', []))[:5]
        return not pids and not getattr(pids, 'recheck', False)

    def send(pids, sig, sent):
        # Each run mate gets the round's signal once, also one first known while waiting (one that
        # could not be told at first, or started meanwhile).
        for pid in still_the_same(pids):
            if pid in sent:
                continue
            sent.add(pid)
            try:
                kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        return True

    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = alive()
        if settled(pids):
            return True
        sent = set()
        if not send(pids, sig, sent):
            return False
        deadline = time.monotonic() + (grace if sig == signal.SIGTERM else max(grace / 3, 1.0))
        while time.monotonic() < deadline:
            pids = alive()
            if settled(pids):
                return True
            if set(pids) - sent and not send(pids, sig, sent):
                return False
            time.sleep(poll)
    return settled(alive())
