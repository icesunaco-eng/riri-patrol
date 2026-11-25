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

from riri.mini_run import child_env, reap_run_mates, run_mates


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
    """SIGTERM the child's whole group, then SIGKILL.  Survivors are reaped later."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # an exited (zombie-only) group can refuse signals on macOS
        try:
            child.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


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

    try:
        with open(stdout_path, 'wb') as out, open(stderr_path, 'wb') as err:
            child = subprocess.Popen(argv, env=child_env(base_env, run_id), stdin=subprocess.PIPE,
                                     stdout=out, stderr=err, start_new_session=True, close_fds=True)
    except OSError as exc:
        return store.update(run_id, lambda r: {**r, 'end_evidence': {
            'kind': 'launch_failed', 'error': type(exc).__name__, 'at': _now()}})

    store.update(run_id, lambda r: {**r, 'child': {'pid': child.pid, 'pgid': child.pid, 'at': _now()}})

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
    while child.poll() is None:
        if stop_check():
            reason = 'stop'
        elif time.monotonic() >= deadline:
            reason = 'timeout'
        if reason != 'exited':
            _stop_group(child, min(grace, 30.0))
            break
        time.sleep(poll)
    exit_code = child.wait()
    feeder.join(timeout=5)

    mates_empty = reap_run_mates(run_id, child.pid, grace=grace, poll=poll, lister=lister)
    digest = _sha256_file(stdout_path)

    def finish(record):
        record['exit'] = {'code': exit_code, 'reason': reason, 'stdout_sha256': digest, 'at': _now()}
        if mates_empty:
            record['end_evidence'] = {'kind': 'watched', 'exit_code': exit_code,
                                      'reason': reason, 'mates_empty': True, 'at': _now()}
        else:
            record['end_unconfirmed'] = {'reason': 'run mates still alive', 'at': _now()}
        return record
    return store.update(run_id, finish)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _item_type(item):
    return item.get('type') or item.get('item_type')


def formal_result(stdout_text, last_json_text, run_id, validate, exit_code):
    """The five checks of 5-1.  Returns ``{'formal': bool, 'answer': ..., 'reason': ...}``."""
    def fail(reason):
        return {'formal': False, 'answer': None, 'reason': reason, 'text': None}

    if exit_code != 0:
        return fail('終了コードが0ではない')
    events = []
    for line in stdout_text.splitlines():
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
    for line in stdout_text.splitlines():
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
    for line in rollout_text.splitlines():
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
