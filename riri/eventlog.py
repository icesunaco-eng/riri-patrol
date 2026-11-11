from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import os
import re
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


def slack_evidence(now, ts):
    """Never present a practice/manual acknowledgement as a real Slack ts."""
    if isinstance(ts, str) and re.fullmatch(r"\d+\.\d+", ts):
        try:
            # History reconciliation may happen hours after the actual post.
            # The row's event time is our observation; evidence is Slack's ts.
            posted = datetime.fromtimestamp(float(ts), timezone.utc)
            return display_time(posted) + " JST ts " + ts
        except (OverflowError, OSError, ValueError):
            return "投稿時刻未確認 ts " + ts
    return "練習（実投稿なし）" if isinstance(ts, str) and ts.startswith("practice-") else "手動確認（ts未確認）"


def new_event(now, run_started, action, req_id="", actor="", result="完了", evidence="", memo="", progress=""):
    event_id = "e" + uuid.uuid4().hex[:12]
    if not progress:
        if action == "アカウント違いで停止":
            progress, action = "アカウント違いで停止", "依頼処理・Slack操作を停止"
        elif action == "依頼を拾った":
            progress, action = req_id + " を拾った", "状態を対応中にした"
        elif action == "Slackに投稿した":
            progress = req_id + " の指示文を作成済み"
        elif action == "完了にした":
            progress = req_id + " の宛先全員の完了報告を確認"
        elif action == "完了報告を見つけた":
            progress = req_id + " の新しい報告を確認"
        elif req_id:
            progress = req_id + " を確認"
        else:
            progress = "見回り全体を確認"
    return {"event_id": event_id, "at": iso(now), "run_started": iso(run_started),
            "row": [display_time(now), display_time(run_started), progress, req_id, actor,
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

