"""Opt-in local delivery: hand Noa's message to Claude Code on this Mac.

Used instead of the cloud routine when NOA_TRANSPORT=local. The patrol returns
after a detached supervisor starts. That supervisor records one CLI outcome;
neither component certifies task completion or automatically retries the work.

Session choice: among Claude Code transcripts whose recorded cwd is exactly
NOA_LOCAL_DIR, continue the most recently modified one that mentions the same
request ID / related request IDs or the same project page. Otherwise start a
new session in NOA_LOCAL_DIR.

No shell is used. The supervisor and its CLI child get a minimal environment
(no Riri credentials), private run-bound input/output and a separate process
group. Once the supervisor starts the delivery is final; a spawn failure before
that supervisor exists is a known not-sent rejection.
"""
from contextlib import contextmanager
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
from pathlib import Path

from .errors import SafetyError, StopRequested, TimeBudgetExceeded
from .local_outcomes import atomic_json, digest, INPUT_LIMIT, paths

# The owner chooses one explicitly. bypassPermissions was approved by たかむね
# (2026-11-13): prohibited actions are stated in each request instead.
PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}
_EMOJI = "[\U0001F300-\U0001FAFF☀-➿]️?"
REQUEST_ID = re.compile(r"(?<!\d)(\d{8}-\d+)(?!\d)")
EMOJI_ID = re.compile("(" + _EMOJI + r")(\d{8}-\d+)(?!\d)")
PROJECT_LINE = re.compile(r"^\s*案件ページ\s*[：:]\s*(.+)$", re.M)
# A Notion page ID as either 8-4-4-4-12 or 32 contiguous hex digits.
NOTION_ID = re.compile(r"(?<![0-9a-fA-F])(?:([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-"
                       r"([0-9a-fA-F]{4})-([0-9a-fA-F]{12})|([0-9a-fA-F]{32}))(?![0-9a-fA-F])")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,127}")
MAX_SCAN_FILES = 5000   # transcripts whose cwd is read (all folders, newest first)
MAX_CANDIDATES = 400    # transcripts in NOA_LOCAL_DIR whose body is searched
ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL")
_DETACHED_CHILDREN = []


def _reap_detached(child):
    """Keep a real child handle alive and reap it without delaying this patrol.

    This bookkeeping never changes run receipts or kills/retries a process.
    A daemon waiter lets the patrol exit while its detached supervisor lives on.
    """
    if not callable(getattr(child, "wait", None)):
        return
    _DETACHED_CHILDREN.append(child)

    def wait():
        try:
            child.wait()
        except Exception:
            pass
        finally:
            try:
                _DETACHED_CHILDREN.remove(child)
            except ValueError:
                pass

    try:
        threading.Thread(target=wait, name="noa-local-reaper", daemon=True).start()
    except RuntimeError:
        # The child already exists: a reaper resource failure is not an unsent
        # delivery. Keep the handle rather than change the at-most-once result.
        pass


class Key(str):
    """A match key; kind is "request" or "notion"."""
    def __new__(cls, value, kind):
        obj = super().__new__(cls, value)
        obj.kind = kind
        return obj


def notion_ids(text):
    """Every Notion ID in text, normalized to 32 lowercase hex digits."""
    out = []
    for m in NOTION_ID.finditer(text):
        out.append((m.group(6) or "".join(m.group(i) for i in range(1, 6))).lower())
    return out


def match_keys(text):
    """IDs that identify the same work. Emoji-prefixed IDs keep their prefix
    so 💛20261113-1 and 💚20261113-1 never match each other. Only the
    「案件ページ」 line contributes Notion IDs (report pages are in every request)."""
    keys = [Key(m.group(1) + m.group(2), "request") for m in EMOJI_ID.finditer(text)]
    if not keys:
        keys = [Key(m.group(1), "request") for m in REQUEST_ID.finditer(text)]
    for line in PROJECT_LINE.findall(text):
        keys += [Key(i, "notion") for i in notion_ids(line)]
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def key_patterns(keys):
    """Compiled patterns applied to transcript lines (raw or JSON-escaped)."""
    pats = []
    for k in keys:
        if getattr(k, "kind", "request") == "notion":
            c = k.lower()
            dashed = "-".join((c[:8], c[8:12], c[12:16], c[16:20], c[20:]))
            pats.append(re.compile(r"(?<![0-9a-fA-F])(?:" + c + "|" + dashed + r")(?![0-9a-fA-F])", re.I))
        else:
            escaped = json.dumps(str(k))[1:-1]
            pats.append(re.compile(r"(?<!\d)" + re.escape(str(k)) + r"(?!\d)"))
            if escaped != k:
                pats.append(re.compile(r"(?<!\d)" + re.escape(escaped) + r"(?!\d)", re.I))
    return pats


class SearchIncomplete(Exception):
    """The newest match could not be determined; never fall back to a new session."""


def _scan(path, cwd, pats):
    """(belongs_to_cwd, session_id, hit). One streaming pass, no size cap."""
    found_cwd, sid, hit = None, None, False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if found_cwd is None and '"cwd"' in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if isinstance(row, dict) and isinstance(row.get("cwd"), str):
                    found_cwd = row["cwd"]
                    sid = row.get("sessionId") or Path(path).stem
                    if found_cwd != cwd:
                        return False, None, False
            if not hit and any(p.search(line) for p in pats):
                hit = True
            if found_cwd is not None and hit:
                break
    if found_cwd != cwd or not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return False, None, False
    return True, sid, hit


def _names(directory):
    """Entry names of one directory. os.scandir is used directly because
    Path.glob swallows listing errors (a denied folder would look empty)."""
    with os.scandir(directory) as it:
        return [entry.name for entry in it]


def _transcripts(root):
    """[(mtime, path)] of every regular <root>/*/*.jsonl file.

    Only a confirmed absence (FileNotFoundError: the root, a folder or a file
    is not there) is skipped. Every other listing/stat failure (permission,
    I/O, ...) propagates as OSError so the caller never mistakes it for
    "no transcripts". A root that exists but is not a directory is such a
    failure too: the history is somewhere this search cannot see. Symlinked
    files are not followed (lstat)."""
    root = os.fspath(root)
    try:
        if not stat.S_ISDIR(os.stat(root).st_mode):
            raise NotADirectoryError(root)
        folders = _names(root)
    except FileNotFoundError:
        return []
    files = []
    for folder in folders:
        sub = os.path.join(root, folder)
        try:
            if not stat.S_ISDIR(os.stat(sub).st_mode):
                continue
            leaves = _names(sub)
        except FileNotFoundError:
            continue
        for leaf in leaves:
            if not leaf.endswith(".jsonl"):
                continue
            path = os.path.join(sub, leaf)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                files.append((info.st_mtime, Path(path)))
    return files


def find_session(projects_dir, cwd, keys, guard=lambda: None):
    """Newest session in cwd mentioning a key; None when the search was complete
    and nothing matched. Raises SearchIncomplete when a limit, a listing/stat
    failure or an unreadable file means a newer match may have been missed."""
    if not keys:
        return None
    try:
        files = _transcripts(projects_dir)
    except OSError:
        raise SearchIncomplete() from None
    files.sort(key=lambda x: x[0], reverse=True)
    pats = key_patterns(keys)
    candidates = 0
    for n, (_, path) in enumerate(files):
        if n >= MAX_SCAN_FILES or candidates >= MAX_CANDIDATES:
            raise SearchIncomplete()
        guard()
        try:
            ours, sid, hit = _scan(path, cwd, pats)
        except OSError:
            raise SearchIncomplete() from None
        if not ours:
            continue
        candidates += 1
        if hit:
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
    transport = "local"

    def __init__(self, config, *, guard, clock, projects_dir=None, popen=None, on_attempt=None):
        self.cfg, self.guard, self.clock = config, guard, clock
        # Called with a safe reference before the child can exist (see L-03).
        self.on_attempt = on_attempt or (lambda record: None)
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
        try:
            sid = find_session(self.projects_dir, cwd, match_keys(text), self.guard)
        except (StopRequested, TimeBudgetExceeded) as exc:
            exc.not_sent = True
            raise
        except SearchIncomplete:
            raise NoaRejected("local_search_incomplete") from None
        logdir = Path(self.cfg.root) / "logs" / "noa-local"
        # Clock time plus a random suffix: never collides, never reveals content.
        stamp = self.clock().strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(8)
        local_ref = "logs/noa-local/" + stamp
        raw = text.encode("utf-8")
        if len(raw) > INPUT_LIMIT:
            raise NoaRejected("local_prepare_failed")
        try:
            run_paths = paths(self.cfg.root, local_ref)
            logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            inbox = run_paths["input"]
            fd = os.open(inbox, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(text)
                fh.flush(); os.fsync(fh.fileno())
            manifest = {"schema_version": 1, "run_id": stamp, "local_ref": local_ref,
                        "input_sha256": digest(raw), "created_at": self.clock().isoformat(),
                        "mode": "resume" if sid else "new", "selected_session_id": sid,
                        "binary": self.cfg.noa_claude_bin, "cwd": cwd,
                        "stop_file": str(Path(self.cfg.stop).absolute()),
                        "permission_mode": self.cfg.noa_local_permission_mode}
            atomic_json(run_paths["manifest"], manifest)
            out = os.open(run_paths["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(out, 0o600)
        except (OSError, ValueError):
            raise NoaRejected("local_prepare_failed") from None
        env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
        try:
            self.guard()  # Nothing is recorded as attempted if we stop here.
        except (StopRequested, TimeBudgetExceeded) as exc:
            os.close(out)
            exc.not_sent = True
            raise
        try:
            self.on_attempt({"transport": "local", "ref": local_ref, "run_id": stamp,
                             "input_sha256": manifest["input_sha256"]})
            # Saving the route takes time: a STOP or the time budget may have
            # arrived meanwhile. The child still does not exist, so this is
            # the last not-sent boundary.
            self.guard()
        except (StopRequested, TimeBudgetExceeded) as exc:
            os.close(out)
            exc.not_sent = True
            raise
        except BaseException:
            os.close(out)
            raise
        try:
            with open(inbox, "rb") as stdin:
                args = [sys.executable, str(Path(__file__).with_name("local_worker.py").resolve()),
                        str(run_paths["manifest"])]
                child = self.popen(args, cwd=cwd, env=env, stdin=stdin, stdout=out,
                                   stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except OSError:
            raise NoaRejected("local_spawn_failed") from None
        finally:
            os.close(out)
        _reap_detached(child)
        return {"type": "local_session", "mode": "resume" if sid else "new",
                "session_id": sid, "pid": child.pid, "run_id": stamp, "local_ref": local_ref,
                "receipt_ref": local_ref + ".result.json"}
