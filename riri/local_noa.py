"""Opt-in local delivery: hand Noa's message to Claude Code on this Mac.

Used instead of the cloud routine when NOA_TRANSPORT=local. Riri only
delivers; it never waits for, judges or retries Noa's work.

Session choice: among Claude Code transcripts whose recorded cwd is exactly
NOA_LOCAL_DIR, continue the most recently modified one that mentions the same
request ID / related request IDs or the same project page. Otherwise start a
new session in NOA_LOCAL_DIR.

No shell is used. The child gets a minimal environment (no Riri credentials),
the message on stdin, its own process group, and output to a private log.
Once the child is spawned the delivery is final; a spawn failure before the
child exists is a known not-sent rejection.
"""
from contextlib import contextmanager
import json
import os
import re
import subprocess
from pathlib import Path

from .errors import SafetyError, StopRequested, TimeBudgetExceeded

# The owner chooses one explicitly. bypassPermissions was approved by たかむね
# (2026-11-13): prohibited actions are stated in each request instead.
PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}
REQUEST_ID = re.compile(r"(?:[\U0001F300-\U0001FAFF☀-➿]️?)?(?<!\d)(\d{8}-\d+)(?!\d)")
EMOJI_ID = re.compile(r"([\U0001F300-\U0001FAFF☀-➿]️?)(\d{8}-\d+)(?!\d)")
PROJECT_LINE = re.compile(r"^\s*案件ページ\s*[：:]\s*(.+)$", re.M)
NOTION_ID = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,127}")
MAX_FILES = 400
MAX_FILE_BYTES = 32 * 1024 * 1024
HEAD_LINES = 50
ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL")


def match_keys(text):
    """IDs that identify the same work. Emoji-prefixed IDs keep their prefix
    so 💛20261113-1 and 💚20261113-1 never match each other."""
    keys = []
    for m in EMOJI_ID.finditer(text):
        keys.append(m.group(1) + m.group(2))
    if not keys:
        keys = [m.group(1) for m in REQUEST_ID.finditer(text)]
    for line in PROJECT_LINE.findall(text):
        keys += NOTION_ID.findall(line.replace("-", ""))
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _forms(key):
    # Transcripts may store non-ASCII raw or as JSON \u escapes.
    escaped = json.dumps(key)[1:-1]
    return {key, escaped, escaped.lower()}


def _session_of(path, cwd):
    """Return the sessionId if this transcript belongs to cwd, else None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(HEAD_LINES):
                line = fh.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "cwd" in row:
                    sid = row.get("sessionId") or Path(path).stem
                    if row["cwd"] != cwd or not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
                        return None
                    return sid
    except OSError:
        return None
    return None


def find_session(projects_dir, cwd, keys):
    """Most recently modified matching session ID for cwd, or None."""
    if not keys:
        return None
    root = Path(projects_dir)
    if not root.is_dir():
        return None
    files = []
    for p in root.glob("*/*.jsonl"):
        try:
            if p.is_file() and not p.is_symlink():
                files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(key=lambda x: x[0], reverse=True)
    forms = [f for k in keys for f in _forms(k)]
    for _, path in files[:MAX_FILES]:
        sid = _session_of(path, cwd)
        if sid is None:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for f in forms:
            if re.search(r"(?<!\d)" + re.escape(f) + r"(?![\d])", body):
                return sid
    return None


def local_evidence(result):
    if (not isinstance(result, dict) or result.get("type") != "local_session" or
            result.get("mode") not in {"resume", "new"} or
            not isinstance(result.get("pid"), int) or result["pid"] <= 0):
        return None
    if result["mode"] == "resume":
        sid = result.get("session_id")
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            return None
        return "local:resume:" + sid
    return "local:new:pid" + str(result["pid"])


def any_evidence(result, original):
    """Local results get local evidence; everything else is the original check."""
    if isinstance(result, dict) and result.get("type") == "local_session":
        from .noa import NoaUncertain
        evidence = local_evidence(result)
        if evidence is None:
            raise NoaUncertain()
        return evidence
    return original(result)


@contextmanager
def local_evidence_installed(enabled):
    """riri/noa.py is hash-pinned by compatibility tests, so the human-mention
    path's evidence check is widened only for the duration of one local run."""
    from . import noa
    if not enabled:
        yield
        return
    original = noa.session_evidence
    noa.session_evidence = lambda result: any_evidence(result, original)
    try:
        yield
    finally:
        noa.session_evidence = original


class LocalNoaClient:
    def __init__(self, config, *, guard, clock, projects_dir=None, popen=None):
        self.cfg, self.guard, self.clock = config, guard, clock
        self.projects_dir = Path(projects_dir or Path.home() / ".claude" / "projects")
        self.popen = popen or subprocess.Popen

    def validate(self):
        d, b, mode = self.cfg.noa_local_dir, self.cfg.noa_claude_bin, self.cfg.noa_local_permission_mode
        if not d or not os.path.isabs(d) or not os.path.isdir(d):
            raise SafetyError("NOA_LOCAL_DIR must be an existing absolute directory")
        if not b or not os.path.isabs(b) or not os.path.isfile(b) or not os.access(b, os.X_OK):
            raise SafetyError("NOA_CLAUDE_BIN must be an absolute executable path")
        if mode not in PERMISSION_MODES:
            raise SafetyError("NOA_LOCAL_PERMISSION_MODE must be one of: " + ", ".join(sorted(PERMISSION_MODES)))

    def fire(self, text):
        from .noa import NoaRejected
        self.validate()
        try:
            self.guard()
        except (StopRequested, TimeBudgetExceeded) as exc:
            exc.not_sent = True
            raise
        cwd = os.path.realpath(self.cfg.noa_local_dir)
        sid = find_session(self.projects_dir, cwd, match_keys(text))
        args = [self.cfg.noa_claude_bin, "-p", "--permission-mode", self.cfg.noa_local_permission_mode]
        if sid:
            args += ["--resume", sid]
        logdir = Path(self.cfg.root) / "logs" / "noa-local"
        stamp = self.clock().strftime("%Y%m%dT%H%M%S%fZ")
        try:
            logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            inbox = logdir / (stamp + ".in.txt")
            fd = os.open(inbox, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            out = os.open(logdir / (stamp + ".out.log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise NoaRejected("local_prepare_failed") from None
        env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
        try:
            self.guard()  # Last not-sent boundary before the child exists.
        except (StopRequested, TimeBudgetExceeded) as exc:
            os.close(out)
            exc.not_sent = True
            raise
        try:
            with open(inbox, "rb") as stdin:
                child = self.popen(args, cwd=cwd, env=env, stdin=stdin, stdout=out,
                                   stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except OSError:
            raise NoaRejected("local_spawn_failed") from None
        finally:
            os.close(out)
        return {"type": "local_session", "mode": "resume" if sid else "new",
                "session_id": sid, "pid": child.pid}
