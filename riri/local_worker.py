"""Detached supervisor for one local run. It never reads credentials or retries.

The patrol only starts this worker. The worker owns exactly one Claude child,
waits for that child, and publishes a run-bound receipt without shared-state
writes. No shared conversation text is used to infer this run's outcome.
"""
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from riri.local_outcomes import (atomic_json, classify_run, digest, INPUT_LIMIT,
                                 OUTPUT_LIMIT, paths, private_fd, read_json, read_private, validate_manifest)


def now():
    return datetime.now(timezone.utc).isoformat()


def run(manifest_path):
    manifest_path = Path(manifest_path)
    # The known layout is root/logs/noa-local/<run>.manifest.json.
    if not manifest_path.is_absolute() or not manifest_path.name.endswith(".manifest.json"):
        return 2
    root = manifest_path.parent.parent.parent
    local_ref = "logs/noa-local/" + manifest_path.name.removesuffix(".manifest.json")
    p = paths(root, local_ref)
    if p["manifest"] != manifest_path:
        return 2
    manifest = validate_manifest(read_json(manifest_path), local_ref)
    if digest(read_private(p["input"], INPUT_LIMIT)) != manifest["input_sha256"]:
        return 2
    binary, cwd, mode = (manifest.get(k) for k in ("binary", "cwd", "permission_mode"))
    if (not isinstance(binary, str) or not os.path.isabs(binary) or not os.path.isfile(binary) or
            not os.access(binary, os.X_OK) or not isinstance(cwd, str) or not os.path.isabs(cwd) or
            not os.path.isdir(cwd) or mode not in {"default", "acceptEdits", "plan", "bypassPermissions"}):
        return 2
    sid = manifest.get("selected_session_id")
    if sid is not None and (not isinstance(sid, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_-]{7,127}", sid)):
        return 2
    stop_file = manifest.get("stop_file")
    if not isinstance(stop_file, str) or not os.path.isabs(stop_file):
        return 2
    # A second supervisor cannot start another child for this same run.
    lock = os.open(p["lock"], os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        # Never chmod an unexpected pre-existing lock target.
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return 2
        os.fchmod(lock, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        if p["receipt"].exists():
            return 2
        receipt = {"schema_version": 1, **{k: manifest[k] for k in ("run_id", "local_ref", "input_sha256")},
                   "started_at": now(), "state": "running", "outcome": "pending",
                   "partial_work_possible": True, "worker_pid": os.getpid()}
        atomic_json(p["receipt"], receipt)
        args = [binary, "-p", "--permission-mode", mode, "--output-format", "json"]
        if sid:
            args += ["--resume", sid]
        env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL") if k in os.environ}
        child = None
        try:
            # Validate and execute the same open input file descriptor. A later
            # path replacement cannot substitute another prompt after hashing.
            with os.fdopen(private_fd(p["input"]), "rb") as stdin:
                raw_input = stdin.read(INPUT_LIMIT + 1)
                if len(raw_input) > INPUT_LIMIT or digest(raw_input) != manifest["input_sha256"]:
                    raise ValueError("input changed before execution")
                stdin.seek(0)
                out = private_fd(p["output"], os.O_WRONLY)
                try:
                    os.ftruncate(out, 0)
                    err = os.open(p["stderr"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        os.fchmod(err, 0o600)
                        # The patrol's last guard preceded the detached worker.
                        # Check again here; never start a child after observed STOP.
                        if (root / "STOP").exists() or Path(stop_file).exists():
                            receipt.update(state="exited", outcome="stopped_before_start", exit_code=None,
                                           partial_work_possible=False, completed_at=now())
                            atomic_json(p["receipt"], receipt)
                            return 0
                        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=out,
                                                 stderr=err, close_fds=True)
                    finally:
                        os.close(err)
                finally:
                    os.close(out)
        except OSError:
            receipt.update(state="exited", outcome="cli_spawn_failed", exit_code=None,
                           partial_work_possible=False, completed_at=now())
            atomic_json(p["receipt"], receipt)
            return 0
        # Failure after this point must never be called an unsent spawn failure.
        receipt["child_pid"] = child.pid
        atomic_json(p["receipt"], receipt)
        exit_code = child.wait()
        try:
            raw = read_private(p["output"], OUTPUT_LIMIT)
            errors = read_private(p["stderr"], OUTPUT_LIMIT)
            classified = classify_run(raw, errors, exit_code)
            receipt.update(classified, output_sha256=digest(raw), stderr_sha256=digest(errors))
        except (OSError, ValueError):
            receipt.update(outcome="unknown")
        receipt.update(state="exited", exit_code=exit_code, completed_at=now())
        atomic_json(p["receipt"], receipt)
        return 0
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        code = run(sys.argv[1]) if len(sys.argv) == 2 else 2
    except Exception:
        # No exception repr, traceback, input, output, or environment in logs.
        code = 2
    raise SystemExit(code)
