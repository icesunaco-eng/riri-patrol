"""Atomic state, isolated per mode. Corrupt state must never become 'first run'."""
from pathlib import Path
import json
import os
import uuid

from .errors import SafetyError


def initial_state(mode):
    return {"version": 2, "mode": mode, "first_run": True, "last_run_at": None,
            "requests": {}, "riri_notified": [], "incomplete_notified": {},
            "seen_reports": {}, "seen_report_versions": {}, "log_buffer": [], "last_log_flush_at": None,
            "errors": {}, "last_error": None, "last_account_mismatch_event_at": None,
            "outbox": {}, "log_batch": None, "retention_notified": [], "buffer_warning_sent": False,
            "last_log_size_notice_at": None, "last_summary_at": None,
            "quarantined_reports": {}, "resolutions": [],
            "noa_wake": {"cutoff": None, "watermark": None, "items": {}, "attempts": []}}


class StateStore:
    def __init__(self, root, mode):
        if mode not in ("live", "practice"):
            raise ValueError("invalid mode")
        self.root, self.mode = Path(root), mode
        self.path = self.root / "state" / ("state.json" if mode == "live" else "state.practice.json")

    def load(self):
        if not self.path.exists():
            return initial_state(self.mode)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            expected = initial_state(self.mode)
            if data.get("version") != 2 or data.get("mode") != self.mode:
                raise ValueError()
            # Additive v0.2 migration only. Never discard or reset v0.1 records.
            for key in ("last_log_size_notice_at", "last_summary_at"):
                data.setdefault(key, None)
            data.setdefault("quarantined_reports", {})
            data.setdefault("resolutions", [])
            data.setdefault("noa_wake", initial_state(self.mode)["noa_wake"])
            from .noa import validate_state
            validate_state(data["noa_wake"])
            for key, value in expected.items():
                if key not in data or (value is not None and type(data[key]) is not type(value)):
                    raise ValueError()
            for item in data["outbox"].values():
                if not isinstance(item, dict) or item.get("status") not in {"queued", "sending", "posted", "held", "cancelled", "blocked"}:
                    raise ValueError()
            return data
        except (ValueError, KeyError, OSError, TypeError):
            raise SafetyError("状態ファイルが不正です。初期化せず停止しました。復元を確認してください") from None

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
