"""Atomic state, isolated per mode. Corrupt state must never become 'first run'."""
from datetime import datetime
from pathlib import Path
import json
import os
import uuid

from .errors import SafetyError


def initial_state(mode):
    return {"version": 2, "mode": mode, "first_run": True, "last_run_at": None,
            "deadline_state": {"consecutive": 0, "episode": 0, "first_at": None, "alert_key": None},
            "requests": {}, "riri_notified": [], "incomplete_notified": {},
            "seen_reports": {}, "seen_report_versions": {}, "log_buffer": [], "last_log_flush_at": None,
            "errors": {}, "last_error": None, "core_error_serial": 0, "last_account_mismatch_event_at": None,
            "outbox": {}, "log_batch": None, "retention_notified": [], "buffer_warning_sent": False,
            "last_log_size_notice_at": None, "last_summary_at": None,
            "quarantined_reports": {}, "resolutions": [],
            "noa_wake": {"cutoff": None, "watermark": None, "items": {}, "attempts": [],
                         "enabled": False, "origin_cutoff": None, "off_at": None,
                         "failure_reason": None, "failure_serial": 0}}


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
            data.setdefault("deadline_state", initial_state(self.mode)["deadline_state"])
            data.setdefault("core_error_serial", 0)
            if type(data["core_error_serial"]) is not int or data["core_error_serial"] < 0:
                raise ValueError()
            deadline = data["deadline_state"]
            if (not isinstance(deadline, dict) or set(deadline) != {"consecutive", "episode", "first_at", "alert_key"} or
                    any(type(deadline[k]) is not int or deadline[k] < 0 for k in ("consecutive", "episode")) or
                    any(deadline[k] is not None and not isinstance(deadline[k], str) for k in ("first_at", "alert_key"))):
                raise ValueError()
            if deadline["consecutive"]:
                if (deadline["episode"] < 1 or not deadline["first_at"] or
                        datetime.fromisoformat(deadline["first_at"]).utcoffset() is None):
                    raise ValueError()
            elif deadline["first_at"] is not None or deadline["alert_key"] is not None:
                raise ValueError()
            if (deadline["alert_key"] is not None and
                    deadline["alert_key"] != "deadline-alert:" + str(deadline["episode"])):
                raise ValueError()
            from .noa import migrate_state, validate_state
            migrate_state(data["noa_wake"])
            validate_state(data["noa_wake"])
            for key, value in expected.items():
                if key not in data or (value is not None and type(data[key]) is not type(value)):
                    raise ValueError()
            for item in data["outbox"].values():
                if not isinstance(item, dict) or item.get("status") not in {"queued", "sending", "posted", "held", "cancelled", "blocked"}:
                    raise ValueError()
                if "recovery_error_serial" in item and (
                        item.get("kind") not in {"recovery", "deadline_recovery"} or
                        type(item["recovery_error_serial"]) is not int or
                        not 0 <= item["recovery_error_serial"] <= data["core_error_serial"]):
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
