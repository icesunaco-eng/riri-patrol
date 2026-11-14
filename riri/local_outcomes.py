"""Private, run-bound local CLI receipts. Observations never certify task completion.

This module does not read .env, send messages, run programs, change saved state,
or infer an old run's result from the newest shared conversation record.
"""
import fcntl
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

REF = re.compile(r"logs/noa-local/([0-9]{8}T[0-9]{12}Z-[a-f0-9]{8,32})")
HASH = re.compile(r"[a-f0-9]{64}")
JSON_LIMIT = 65536
INPUT_LIMIT = 2 * 1024 * 1024
OUTPUT_LIMIT = 8 * 1024 * 1024
OUTCOMES = {"pending", "cli_exited", "rate_limited", "authentication_failed", "cli_failed",
            "cli_spawn_failed", "stopped_before_start", "unknown"}


def paths(root, local_ref):
    if not isinstance(local_ref, str) or not REF.fullmatch(local_ref):
        raise ValueError("invalid local reference")
    root = Path(root).resolve()
    for p in (root / "logs", root / "logs" / "noa-local"):
        if p.is_symlink():
            raise ValueError("symlink directory")
    prefix = root / local_ref
    return {key: Path(str(prefix) + suffix) for key, suffix in
            {"manifest": ".manifest.json", "receipt": ".result.json", "input": ".in.txt",
             "output": ".out.log", "stderr": ".err.log", "lock": ".run.lock"}.items()}


def private_fd(path, flags=os.O_RDONLY):
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
            info.st_uid != os.getuid() or info.st_nlink != 1):
        os.close(fd)
        raise ValueError("invalid private file")
    return fd


def read_private(path, limit):
    fd = private_fd(path)
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if info.st_size > limit:
            raise ValueError("invalid private file")
        raw = fh.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("private file too large")
        return raw


def read_json(path):
    data = json.loads(read_private(path, JSON_LIMIT))
    if not isinstance(data, dict):
        raise ValueError("invalid record")
    return data


def atomic_json(path, value):
    """Create/replace only our own regular private file, never a symlink."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink destination")
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(value, fh, ensure_ascii=False)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(manifest, local_ref):
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("local_ref") != local_ref or
            manifest.get("run_id") != REF.fullmatch(local_ref).group(1) or
            not isinstance(manifest.get("input_sha256"), str) or
            not HASH.fullmatch(manifest["input_sha256"]) or
            manifest.get("mode") not in {"new", "resume"} or
            not isinstance(manifest.get("created_at"), str)):
        raise ValueError("invalid manifest binding")
    _time(manifest["created_at"])
    return manifest


def _time(value):
    if not isinstance(value, str):
        raise ValueError("invalid receipt time")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("receipt time needs timezone")
    return result


def _diagnostic(text):
    text = text.strip()
    if re.match(r"^You've hit your (?:\d+-hour|usage|weekly|session) limit(?:\s|[·.:]|$)", text):
        return "rate_limited"
    if text.startswith(("Not logged in", "Invalid API key", "Authentication failed")):
        return "authentication_failed"
    return None


def classify_output(raw, exit_code):
    """Only typed error results (or isolated failing CLI diagnostics) classify errors.

    A successful answer quoting an error is not a failure. Raw result/error text
    stays in the private output file and is never copied into state or notices.
    """
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        result = None
    if isinstance(result, dict) and result.get("type") == "result":
        failed = result.get("is_error") is True or str(result.get("subtype", "")).startswith("error")
        if not failed and result.get("is_error") is False and result.get("subtype") == "success" and exit_code == 0:
            return {"outcome": "cli_exited", "result_format": "json"}
        if failed:
            error = result.get("error")
            kind = error.get("type") if isinstance(error, dict) else error
            if isinstance(kind, str) and kind in {"rate_limit", "rate_limit_error"}:
                return {"outcome": "rate_limited", "result_format": "json"}
            if isinstance(kind, str) and kind in {"authentication_error", "authentication_failed"}:
                return {"outcome": "authentication_failed", "result_format": "json"}
            errors = result.get("errors", [])
            if isinstance(errors, str):
                errors = [errors]
            if not isinstance(errors, list):
                errors = []
            if isinstance(result.get("result"), str):
                errors = errors + [result["result"]]
            for text in errors:
                if isinstance(text, str) and _diagnostic(text):
                    return {"outcome": _diagnostic(text), "result_format": "json"}
            return {"outcome": "cli_failed", "result_format": "json"}
    if exit_code != 0:
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
        # This exact CLI warning may precede its single quota/auth error line.
        lines = [line for line in lines if not line.startswith("Warning: this sign-in ends between ")]
        if len(lines) == 1 and _diagnostic(lines[0]):
            return {"outcome": _diagnostic(lines[0]), "result_format": "diagnostic"}
        return {"outcome": "cli_failed", "result_format": "unrecognized"}
    return {"outcome": "unknown", "result_format": "unrecognized"}


def classify_run(output, stderr, exit_code):
    result = classify_output(output, exit_code)
    if result["outcome"] in {"unknown", "cli_failed"} and exit_code != 0:
        diagnostic = classify_output(stderr, exit_code)
        if diagnostic["outcome"] in {"rate_limited", "authentication_failed"}:
            return diagnostic
    return result


def _lock_held(path):
    fd = private_fd(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def observe(root, local_ref):
    """Return a safe saved observation; missing legacy manifests stay unobserved.

    No mutation or process probing. A running receipt requires its supervisor's
    live advisory lock; a lost supervisor is unknown, never an automatic retry.
    """
    unknown = {"state": "unknown", "outcome": "unknown", "partial_work_possible": True}
    base = {}
    try:
        p = paths(root, local_ref)
        try:
            manifest = read_json(p["manifest"])
        except FileNotFoundError:
            return {"state": "legacy_unobserved", "outcome": "unknown", "partial_work_possible": True}
        validate_manifest(manifest, local_ref)
        base = {"run_id": manifest["run_id"], "input_sha256": manifest["input_sha256"],
                "local_ref": local_ref, "partial_work_possible": True}
        if digest(read_private(p["input"], INPUT_LIMIT)) != manifest["input_sha256"]:
            raise ValueError("input binding mismatch")
        receipt = read_json(p["receipt"])
        if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1 or any(receipt.get(k) != manifest[k]
                for k in ("run_id", "local_ref", "input_sha256")) or
                receipt.get("state") not in {"running", "exited"} or
                receipt.get("outcome") not in OUTCOMES or
                type(receipt.get("partial_work_possible")) is not bool):
            raise ValueError("receipt binding mismatch")
        created, started = _time(manifest["created_at"]), _time(receipt.get("started_at"))
        if started < created:
            raise ValueError("receipt started before input")
        if receipt["state"] == "running":
            if (receipt["outcome"] != "pending" or receipt.get("exit_code") is not None or
                    receipt.get("completed_at") is not None):
                raise ValueError("invalid running receipt")
            if not _lock_held(p["lock"]):
                return {**unknown, **base, "reason": "supervisor_unconfirmed"}
            return {**base, "state": "running", "outcome": "pending", "started_at": receipt.get("started_at")}
        exit_code = receipt.get("exit_code")
        if receipt["outcome"] == "pending" or _time(receipt.get("completed_at")) < started:
            raise ValueError("invalid final receipt")
        no_child = receipt["outcome"] in {"cli_spawn_failed", "stopped_before_start"}
        if ((no_child and (exit_code is not None or receipt["partial_work_possible"] or receipt.get("child_pid"))) or
                (not no_child and (type(exit_code) is not int or not receipt["partial_work_possible"])) or
                (receipt["outcome"] == "cli_exited" and exit_code != 0)):
            raise ValueError("invalid exit status")
        if not no_child:
            raw = read_private(p["output"], OUTPUT_LIMIT)
            errors = read_private(p["stderr"], OUTPUT_LIMIT)
            if digest(raw) != receipt.get("output_sha256") or digest(errors) != receipt.get("stderr_sha256"):
                raise ValueError("output binding mismatch")
            if classify_run(raw, errors, exit_code)["outcome"] != receipt["outcome"]:
                raise ValueError("output outcome mismatch")
        return {**base, "state": "exited", "outcome": receipt["outcome"], "exit_code": exit_code,
                "partial_work_possible": receipt["partial_work_possible"],
                "started_at": receipt.get("started_at"), "completed_at": receipt.get("completed_at")}
    except (OSError, ValueError, TypeError, KeyError):
        return {**unknown, **base}
