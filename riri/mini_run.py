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


class RunStore:
    """One record file per run under ``<root>/state/mini/runs`` (P1)."""

    def __init__(self, root):
        self.dir = Path(root) / 'state' / 'mini' / 'runs'

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
            if not isinstance(record, dict) or record.get('run_id') != path.stem:
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
        return (ev.get('mates_empty') is True and type(ev.get('exit_code')) is int and stamped
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


def _marker_in(environ_text, run_id):
    token = f'{MARKER}={run_id}'
    return re.search(rf'(?:^|[\s\x00]){re.escape(token)}(?=$|[\s\x00])', environ_text) is not None


def _list_linux(run_id, pgid):
    mates = set()
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if pgid is not None and os.getpgid(pid) == pgid:
                mates.add(pid)
                continue
            with open(f'/proc/{pid}/environ', 'rb') as handle:
                if _marker_in(handle.read().decode('utf-8', 'replace'), run_id):
                    mates.add(pid)
        except (OSError, ProcessLookupError):
            continue
    return mates


def _list_ps(run_id, pgid):
    out = subprocess.run(['ps', '-E', '-ww', '-x', '-o', 'pid=,pgid=,command='],
                         capture_output=True, text=True, check=True).stdout
    mates = set()
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid, group = int(parts[0]), int(parts[1]) if parts[1].isdigit() else None
        rest = parts[2] if len(parts) > 2 else ''
        if (pgid is not None and group == pgid) or _marker_in(rest, run_id):
            mates.add(pid)
    return mates


def run_mates(run_id, pgid=None):
    """PIDs of processes that belong to the run (B1).

    Same-user processes only.  A child that deliberately execs with a cleared
    environment *and* leaves the group cannot be seen; the design records that
    as a residual risk accepted under decision 5.
    """
    _check_run_id(run_id)
    lister = _list_linux if os.path.isdir('/proc') and sys.platform.startswith('linux') else _list_ps
    mates = lister(run_id, pgid)
    mates.discard(os.getpid())
    return mates


def reap_run_mates(run_id, pgid=None, grace=30.0, poll=0.2, lister=run_mates, kill=os.kill):
    """Stop every run mate and confirm the set is empty.

    Sends SIGTERM, waits up to ``grace`` seconds, then SIGKILL.  Returns True
    only when an empty set was observed; the caller must treat False as "no
    end evidence" and hold the run (4-4).
    """
    def alive():
        return lister(run_id, pgid)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = alive()
        if not pids:
            return True
        for pid in pids:
            try:
                kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        deadline = time.monotonic() + (grace if sig == signal.SIGTERM else max(grace / 3, 1.0))
        while time.monotonic() < deadline:
            if not alive():
                return True
            time.sleep(poll)
    return not alive()
