from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import os
import stat
import uuid

from .errors import sanitize, SafetyError

JST = timezone(timedelta(hours=9))


def iso(value):
    return value.astimezone(JST).isoformat()


def dt(value):
    return datetime.fromisoformat(value)


def display_time(value):
    return value.astimezone(JST).strftime("%Y-%m-%d %H:%M")


class LocalLogs:
    """Append-only. No expiry deletion or rotation that removes data."""
    def __init__(self, root, secrets=(), verbose=False):
        self.root, self.secrets, self.verbose = Path(root), secrets, verbose

    def _append(self, name, line, private=False):
        folder = self.root / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if private:
            flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(folder / name, flags, 0o600)
        if private:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                os.close(fd)
                raise SafetyError('Private audit log must be a regular owner-only file')
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(sanitize(line, self.secrets) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def operational(self, message):
        self._append("patrol.log", message)

    def event(self, event):
        self._append("events.jsonl", json.dumps(event, ensure_ascii=False))

    def audit(self, entry):
        """Private recovery evidence: fsynced, redacted, never echoed to stdout."""
        self._append("notion-audit.jsonl", json.dumps(entry, ensure_ascii=False), private=True)

    def practice(self, entry):
        line = json.dumps(entry, ensure_ascii=False)
        self._append("practice.jsonl", line)
        if self.verbose:
            print(sanitize(line, self.secrets))


def new_event(now, run_started, action, req_id="", actor="", result="完了", evidence="", memo=""):
    event_id = "e" + uuid.uuid4().hex[:12]
    return {"event_id": event_id, "at": iso(now), "run_started": iso(run_started),
            "row": [display_time(now), display_time(run_started), action, req_id, actor,
                    action, result, evidence, "#" + event_id + (" " + memo if memo else "")]}


def row_ids(rows):
    ids = set()
    for row in rows:
        cells = row.get("table_row", {}).get("cells", [])
        if len(cells) != 9:
            continue
        text = "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in cells[8])
        for part in text.split():
            if part.startswith("#e"):
                ids.add(part[1:])
    return ids


def notion_rows(events):
    return [{"object": "block", "type": "table_row", "table_row": {
        "cells": [[{"type": "text", "text": {"content": cell[i:i + 2000]}} for i in range(0, len(cell), 2000)]
                  for cell in event["row"]]}} for event in events]
