"""Watcher for one みに (Codex) run, and how its answer becomes a formal result.

Nothing in patrol calls this yet (it stays off until wiring and MINI_TRANSPORT).
Design: みに wake design v1.0 (Notion), 4-3 / 4-4 / 5 / 5-1.

``supervise`` follows the launch stages of 4-3:

1. STOP is checked right before the launch mark; a stop there means "not started".
2. The launch mark is written to the run record (P1) before anything is spawned.
3. A failed spawn is end evidence on its own ("launch failed").
4. The child runs as the leader of a new session with the RIRI_MINI_RUN marker.
   Timeout or STOP stops the whole group.
5. After the child exits, every run mate must be gone (``reap_run_mates``).
   Only then is ``end_evidence`` written.  If that cannot be confirmed the run
   keeps its launch mark without end evidence, so nothing else starts (Z2).

``formal_result`` implements the five checks of 5-1 and ``grade`` keeps an
answer from becoming "完了" automatically until the final-answer signal (X2)
has been confirmed with the real model.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import signal
import subprocess
import threading
import time

from riri.mini_run import Descendants, child_env, reap_run_mates, run_mates


def _now():
    return datetime.now(timezone.utc).isoformat()


# Switched off on every run, even if the dedicated config.toml is changed.  Measured
# 2026-11-25: Codex CLI 0.163 / 0.169 turn on ChatGPT apps (connectors) and remote
# plugins by default and download plugins into the Codex home on the first run.
# Sub-agents: 0.169 deprecated features.multi_agent; agents.enabled=false is the switch.
DISABLED_FEATURES = ('apps', 'plugins', 'remote_plugin', 'image_generation', 'multi_agent_v2')
DISABLED_FEATURE_ARGS = (tuple(a for f in DISABLED_FEATURES for a in ('--disable', f))
                         + ('-c', 'agents.enabled=false'))


def build_argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json,
               resume_id=None, effort='max', model=None):
    """Argument list of 4-4.  Flags always come before ``resume``.  ``model`` pins the
    verified model on the command line, whatever config.toml says by then (re-review T1)."""
    argv = [sandbox_exec, '-f', str(profile), str(codex_bin), 'exec', '-C', str(workdir)]
    if model:
        argv += ['-m', str(model)]
    for path in write_dirs:
        argv += ['--add-dir', str(path)]
    argv += ['-s', 'danger-full-access', '-c', 'approval_policy="never"',
             '-c', f'model_reasoning_effort="{effort}"', '-c', 'project_doc_max_bytes=0',
             '--disable', 'hooks', *DISABLED_FEATURE_ARGS, '--skip-git-repo-check',
             '--json', '--output-schema', str(schema_path), '-o', str(last_json)]
    if resume_id:
        argv += ['resume', resume_id]
    argv.append('-')
    return argv


def _stop_group(child, grace):
    """SIGTERM the child's whole group, then SIGKILL.  Survivors are reaped later.

    Only while the child is not yet waited for: until then its number (and so its group's)
    cannot be handed to another process.  Once waited for, a group with no member left can
    get the number again, and the reap, which checks every process before its signal, takes
    over (the same kind as re-review 💚20261126-4 V5)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if child.returncode is not None:
            return
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # an exited (zombie-only) group can refuse signals on macOS
        try:
            child.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


TRACK_EVERY = 0.5        # seconds between two looks at the child's process tree
REAP_TRIES = 3           # tries of the reap when listing the processes fails for a moment
WRITE_TRIES = 3          # tries of a run record write the watcher cannot go on without


def _write(store, run_id, change):
    """store.update, tried again when the write fails for a moment (no space, a busy disk): the
    child's record and the end evidence are what the lane needs, and without them a run that
    went well would be held for a person and end as できなかった (the 「80% まで止まらずに」
    round, found by failing each write once).  The last failure is raised."""
    for attempt in range(WRITE_TRIES):
        try:
            return store.update(run_id, change)
        except OSError:
            if attempt == WRITE_TRIES - 1:
                raise
            time.sleep(0.5)


class _SigtermHold:
    """SIGTERM noted instead of raised, for the whole time the watcher has a child (re-review
    💚20261126-4 V6).  Raised at any moment, the interrupt could land between the spawn and the
    clean-up (inside Popen the child exists before its PID is known), between the child's end
    and the reap, or inside ``abandon``, and leave the child or its run mates without the
    watcher.  Noted, it is acted on where the watcher can: the loop stops the child and its run
    mates first (``abandon``), and after the child's end the reap and the record are finished
    first.  Only the main thread can set a handler; elsewhere (a test's thread) nothing is held."""

    def __init__(self):
        self.previous = None
        self.noted = None
        self.active = False

    def start(self):
        if self.active or threading.current_thread() is not threading.main_thread():
            return
        self.previous = signal.getsignal(signal.SIGTERM)
        self.noted = None
        signal.signal(signal.SIGTERM, self._note)
        self.active = True

    def _note(self, signum, frame):
        self.noted = signum

    def act(self):
        """Do now what a noted SIGTERM asks for, still holding: the supervisor's own handler
        raises SystemExit(128 + signal); the default action would end the process at once,
        past the clean-up, so it ends the same way instead; an ignored one does nothing."""
        signum, self.noted = self.noted, None
        if signum is None or self.previous == signal.SIG_IGN:
            return
        if callable(self.previous):
            self.previous(signum, None)
            return
        raise SystemExit(128 + signum)

    def release(self, deliver=True):
        """The handler back; then, with ``deliver``, what a noted SIGTERM asks for (``act``)."""
        if not self.active:
            return
        signal.signal(signal.SIGTERM, self.previous)
        self.active = False
        if deliver:
            self.act()
        self.noted = None


def _reap(run_id, pgid, grace, poll, lister):
    """reap_run_mates, tried again when listing the processes fails for a moment (ps did not
    answer, or failed), and finished even when the watcher is told to end meanwhile (SIGTERM):
    the run mates must not be left running without the watcher (the 「80% まで止まらずに」 round,
    found by failing each of the watcher's calls in turn).  Returns (mates_empty, interrupt);
    when every try fails the run mates are not known to be gone, so there is no end evidence."""
    interrupt = None
    for attempt in range(REAP_TRIES):
        try:
            if attempt:
                time.sleep(1.0)
            return reap_run_mates(run_id, pgid, grace=grace, poll=poll, lister=lister), interrupt
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        except BaseException as exc:   # noqa: BLE001 - SystemExit from SIGTERM: finish the reap first
            interrupt = interrupt or exc
    return False, interrupt


def supervise(store, run_id, argv, base_env, stdin_bytes, stdout_path, stderr_path, *,
              timeout, stop_check, extra_on_launch=None, poll=0.2, grace=30.0,
              lister=run_mates):
    """Run one child under the launch stages of 4-3 and return the run record."""
    if stop_check():
        return store.update(run_id, lambda r: {**r, 'not_started': {'reason': 'stop', 'at': _now()}})

    def mark(record):
        record.update(extra_on_launch or {})
        record['launch_mark'] = _now()
        return record
    store.update(run_id, mark)

    # The processes the child starts, seen while it is alive (Descendants: Codex runs each tool
    # command in a session of its own and macOS hides the marker of Apple's programs).  What
    # is learnt goes into the record too, so a person's record-end can still look for them.
    # Made before the spawn, so that nothing stands between the spawn and the clean-up (V6).
    tracker = Descendants(None)
    if lister is run_mates:
        lister = lambda run, pgid: run_mates(run, pgid, tracker)   # noqa: E731
    child = None
    hold = _SigtermHold()

    def abandon():
        # The watcher cannot go on (a record write or the STOP check failed, an interrupt): it
        # never leaves the child running without it, where neither the time limit nor STOP
        # would apply (the 「80% まで止まらずに」 round).  The record keeps the launch mark
        # without end evidence, so the run is held for a person (4-3).  It runs while SIGTERM
        # is held (V6): a second one meanwhile does not cut it short.
        if child is None:
            return
        try:
            _stop_group(child, min(grace, 30.0))
            child.wait(timeout=max(grace, 1.0))
        except BaseException:   # noqa: BLE001 - the first error is the one raised
            pass
        _reap(run_id, child.pid, grace, poll, lister)

    hold.start()        # V6: SIGTERM is noted from the spawn on, and acted on where the watcher can
    try:
        try:
            with open(stdout_path, 'wb') as out, open(stderr_path, 'wb') as err:
                child = subprocess.Popen(argv, env=child_env(base_env, run_id), stdin=subprocess.PIPE,
                                         stdout=out, stderr=err, start_new_session=True, close_fds=True)
        except OSError as exc:
            result = _write(store, run_id, lambda r: {**r, 'end_evidence': {
                'kind': 'launch_failed', 'error': type(exc).__name__, 'at': _now()}})
            hold.release()      # a SIGTERM that came meanwhile ends the watcher now, after the record
            return result
        tracker.root = child.pid
        _write(store, run_id, lambda r: {**r, 'child': {'pid': child.pid, 'pgid': child.pid, 'at': _now()}})

        def look():
            try:
                if tracker.observe():
                    seen = tracker.as_record()
                    store.update(run_id, lambda r: {**r, 'tracked': seen})
            except (OSError, ValueError, subprocess.SubprocessError):
                pass       # one missed look is made up by the next; the reap still uses the others

        def feed():
            # A child that does not read its input fills the pipe; STOP and the time limit
            # must still apply while this blocks (review R5), so it runs beside the loop.
            try:
                child.stdin.write(stdin_bytes)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    child.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        feeder = threading.Thread(target=feed, name='mini-stdin', daemon=True)
        feeder.start()

        reason = 'exited'
        deadline = time.monotonic() + timeout
        looked = 0.0
        while child.poll() is None:
            hold.act()             # told to end (SIGTERM): abandon below stops the child and its mates first
            if time.monotonic() - looked >= TRACK_EVERY:
                look()
                looked = time.monotonic()
            if stop_check():
                reason = 'stop'
            elif time.monotonic() >= deadline:
                reason = 'timeout'
            if reason != 'exited':
                look()             # the latest commands, before their parent is stopped
                _stop_group(child, min(grace, 30.0))
                break
            time.sleep(poll)
        exit_code = child.wait()
        feeder.join(timeout=5)
    except BaseException:
        abandon()
        hold.release(deliver=False)       # the first interrupt is the one raised
        raise

    try:
        mates_empty, interrupt = _reap(run_id, child.pid, grace, poll, lister)
        digest = _sha256_file(stdout_path)

        def finish(record):
            record['exit'] = {'code': exit_code, 'reason': reason, 'stdout_sha256': digest, 'at': _now()}
            if mates_empty:
                record['end_evidence'] = {'kind': 'watched', 'exit_code': exit_code,
                                          'reason': reason, 'mates_empty': True, 'at': _now()}
            else:
                record['end_unconfirmed'] = {'reason': 'run mates still alive', 'at': _now()}
            return record
        result = _write(store, run_id, finish)
    except BaseException:
        hold.release(deliver=False)
        raise
    hold.release()             # told to end while reaping (SIGTERM): the record is written, then it ends
    if interrupt is not None:
        raise interrupt        # the same for another interrupt the reap went on through
    return result


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _item_type(item):
    return item.get('type') or item.get('item_type')


def json_lines(text):
    """The lines of JSON Lines output: split on "\\n" only.  str.splitlines() also splits on
    \\u2028, \\u2029, \\x85 and other separators that a JSON string may hold unescaped (Python's
    json.dumps(ensure_ascii=False) writes them so; what Codex's own writer does is not measured),
    which broke one event into two unreadable halves (the 「どんどん詰めて」 round: a finished
    run became できなかった)."""
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()                     # like splitlines(): no empty piece after the last "\n"
    return lines

def formal_result(stdout_text, last_json_text, run_id, validate, exit_code):
    """The five checks of 5-1.  Returns ``{'formal': bool, 'answer': ..., 'reason': ...}``."""
    def fail(reason):
        return {'formal': False, 'answer': None, 'reason': reason, 'text': None}

    if exit_code != 0:
        return fail('終了コードが0ではない')
    events = []
    for line in json_lines(stdout_text):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return fail('標準出力に JSON でない行がある')
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            # Review 💚20261125-8 S5: a line like `null` or `[]` must not crash the reader.
            return fail('標準出力に形の違う行がある')
        events.append(event)
    turn_ends = [i for i, e in enumerate(events) if e.get('type') == 'turn.completed']
    if not turn_ends:
        return fail('turn.completed がない')
    end = turn_ends[-1]
    if end != len(events) - 1:
        # Review M4: an item after the last turn.completed means the formal answer and
        # what the conversation ended with may differ.  Never pick one of them.
        return fail('turn.completed のあとに項目がある')
    if end == 0:
        return fail('最後の返事がない')
    before = events[end - 1]
    item = before.get('item') if isinstance(before.get('item'), dict) else {}
    if before.get('type') != 'item.completed' or _item_type(item) != 'agent_message':
        return fail('turn.completed の直前が agent_message ではない')
    text = item.get('text')
    if not isinstance(text, str):
        return fail('最後の返事に本文がない')
    try:
        answer = json.loads(text)
    except ValueError:
        return fail('最後の返事が JSON ではない')
    if not isinstance(answer, dict) or not validate(answer):
        return fail('最後の返事が決まった形に合わない')
    if answer.get('run_id') != run_id:
        return fail('run_id が違う')
    if answer.get('final') is not True:
        return fail('final が true ではない')
    if last_json_text is not None:
        try:
            if json.loads(last_json_text) != answer:
                return fail('last.json が最後の返事と違う')
        except ValueError:
            return fail('last.json が JSON ではない')
    return {'formal': True, 'answer': answer, 'reason': '', 'text': text}


def grade(answer, *, final_signal_confirmed, artifacts_confirmed):
    """Result label for the report (5-1).  "完了" is never automatic before X2 is settled."""
    result = str(answer.get('result', ''))
    if not result.startswith('完了'):
        return result
    if not final_signal_confirmed:
        return '一部完了（最終の裏付けなし）'
    if not artifacts_confirmed:
        return '一部完了（成果物を確かめられない）'
    return result


# ---------------------------------------------------------------- final-answer signal (Y2)
#
# Measured 2026-11-25 with Codex CLI 0.169.0 and gpt-6.2-sol: in the conversation
# file (CODEX_HOME/sessions/**/rollout-*-<thread>.jsonl) every assistant message has
# a ``phase``.  Progress messages between commands are ``commentary`` and only the
# final answer is ``final_answer``.  This is the sixth condition of 5-1.

def find_rollout(codex_home, thread_id):
    """The one conversation file of ``thread_id``, or None (zero or several)."""
    import glob
    if not isinstance(thread_id, str) or not thread_id or '/' in thread_id or '*' in thread_id:
        return None
    hits = glob.glob(os.path.join(glob.escape(str(codex_home)), 'sessions', '*', '*', '*',
                                  'rollout-*-' + glob.escape(thread_id) + '.jsonl'))
    return hits[0] if len(hits) == 1 else None


def thread_id_of(stdout_text):
    """The conversation ID of ``thread.started``; None when it is missing or not one."""
    found = set()
    for line in json_lines(stdout_text):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            thread = event.get('thread_id')
            # A list or an object here must not crash the reader; it is no conversation ID.
            found.add(thread if isinstance(thread, str) and thread else None)
    if len(found) != 1:
        return None
    return found.pop()


def final_signal(rollout_text, answer_text):
    """True when the last model item of the conversation is an assistant message with
    phase ``final_answer`` whose text is exactly ``answer_text``.

    ``answer_text`` must be the text of the formal answer itself (``formal_result``'s
    ``text``), never another message of the same run (review M4)."""
    if not isinstance(answer_text, str):
        return False
    last = None
    for line in json_lines(rollout_text):
        try:
            event = json.loads(line)
        except ValueError:
            return False
        payload = event.get('payload') if isinstance(event, dict) else None
        if not isinstance(payload, dict) or event.get('type') != 'response_item':
            continue
        kind = payload.get('type')
        if kind == 'reasoning' or payload.get('role') in ('user', 'developer', 'system'):
            continue   # not something the model did (instructions, tool lists, reasoning)
        last = payload   # assistant messages, tool calls and their outputs
    if (not last or last.get('type') != 'message' or last.get('role') != 'assistant'
            or last.get('phase') != 'final_answer'):   # re-review 💚20261125-8 S2: the role is required
        return False
    content = last.get('content') if isinstance(last.get('content'), list) else []
    text = ''.join(c.get('text', '') for c in content if isinstance(c, dict))
    return text == answer_text
