"""Explicit, locked, local-only resolutions. No network clients are constructed."""
from datetime import datetime, timezone
import json
import re

from .errors import SafetyError, sanitize
from .eventlog import LocalLogs, iso, new_event
from .lock import patrol_lock
from .state import StateStore

OUTCOMES = {"instruction": ("posted", "not_posted"), "notice": ("posted", "cancel"),
            "notion": ("checked", "cancel"), "log": ("written", "not_written")}


def held_items(state):
    """A projection only: never expose message bodies, snapshots or credentials."""
    result = []
    def add(kind, identifier, reason, item):
        entry = {"type": kind, "id": sanitize(identifier), "reason": reason,
                 "allowed_outcomes": list(OUTCOMES.get(kind, ("source_edit_required",)))}
        marker = item.get("marker", "")
        if isinstance(marker, str) and re.fullmatch(r"rp:[0-9a-f]{24}", marker):
            entry["marker"] = marker
        result.append(entry)
    for rid, info in state["requests"].items():
        if info.get("status") != "held":
            continue
        if info.get("hold_reason") == "unsafe_content":
            add("request", rid, "unsafe_content", info)
        elif info.get("hold_reason") == "notion_conflict":
            add("notion", rid, "notion_conflict", info)
        else:
            item = state["outbox"].get("instruction:" + rid, {})
            if item.get("kind") == "instruction" and item.get("status") == "held":
                add("instruction", rid, "post_result_unknown", item)
    for key, item in state["outbox"].items():
        if item.get("status") == "held" and item.get("kind") != "instruction":
            add("notice", key, "post_result_unknown", item)
    for report_key, report in state.get("quarantined_reports", {}).items():
        if report.get("status") == "held":
            add("report", report_key, "unsafe_content", report)
    batch = state.get("log_batch")
    if batch and batch.get("status") in ("sending", "held"):
        add("log", batch["batch_id"], "log_result_unknown", batch)
    return result


def _cancel_hold_notices(state, key, stamp):
    for notice_key, notice in state["outbox"].items():
        if notice.get("status") == "queued" and (
                notice_key == "hold:" + key or
                (notice.get("kind") == "hold_notice" and notice.get("held_key") == key)):
            notice.update(status="cancelled", cancelled_at=stamp, reason="manual_resolution")


def _notice_effects(state, item, outcome, now):
    events = []
    kind = item["kind"]
    # Cancel consumes these exact versions too, without claiming they were posted.
    if kind == "reports":
        for report in item["reports"]:
            state["seen_reports"][report["block_id"]] = report["digest"]
            versions = state["seen_report_versions"].setdefault(report["block_id"], [])
            if report["digest"] not in versions:
                versions.append(report["digest"])
            if outcome == "posted":
                events.append(new_event(now, now, "完了報告を見つけた", report["id"],
                                        report["reporter"], report["result"]))
    elif kind == "self":
        if item["request_id"] not in state["riri_notified"]:
            state["riri_notified"].append(item["request_id"])
            if outcome == "posted":
                events.append(new_event(now, now, "りり自身あてのため飛ばした", item["request_id"]))
    elif outcome == "posted":
        if kind == "recovery":
            state["last_error"] = None
        elif kind == "error":
            previous = state["errors"].get(item["error_name"], {})
            state["errors"][item["error_name"]] = {
                "last_notified_at": iso(now), "count": previous.get("count", 0) + 1}
        elif kind == "maintenance":
            if item.get("size_notice"):
                state["last_log_size_notice_at"] = iso(now)
            if item.get("buffer_notice"):
                state["buffer_warning_sent"] = True
    return events


def resolve_request(config, request_id, outcome, *, clock=None, logs=None):
    """Backward-compatible two-argument instruction resolution."""
    return resolve_held(config, "instruction", request_id, outcome, clock=clock, logs=logs)


def resolve_held(config, kind, identifier, outcome, *, clock=None, logs=None):
    if kind not in OUTCOMES or outcome not in OUTCOMES[kind]:
        raise SafetyError("保留の種類または解決方法が不正です。状態は変更していません")
    clock = clock or (lambda: datetime.now(timezone.utc))
    logs = logs or LocalLogs(config.root, config.secrets)
    with patrol_lock(config.root) as acquired:
        if not acquired:
            return {"status": "locked", "posts": 0}
        store = StateStore(config.root, config.mode)
        state = store.load()
        info = state["requests"].get(identifier)
        key = "instruction:" + identifier
        item = state["outbox"].get(key)
        valid = False
        restore = None
        if kind == "instruction":
            valid = (info and info.get("status") == "held" and item and
                     item.get("status") == "held" and item.get("kind") == "instruction" and
                     info.get("hold_reason") not in ("notion_conflict", "unsafe_content") and
                     item.get("hold_reason") not in ("notion_conflict", "unsafe_content"))
        elif kind == "notice":
            key = identifier
            item = state["outbox"].get(key)
            valid = item and item.get("status") == "held" and item.get("kind") != "instruction"
        elif kind == "notion":
            valid = info and info.get("status") == "held" and info.get("hold_reason") == "notion_conflict"
            if valid and outcome == "checked":
                target = info.get("notion_write_intent", {}).get("status")
                restore = {"対応中": "prepared", "完了": "posted"}.get(target)
                if target is None:
                    restore = {"prepared": "prepared", "posting": "prepared", "posted": "posted"}.get(
                        info.get("status_before_notion_hold"))
                valid = restore is not None
        else:
            batch = state.get("log_batch")
            valid = batch and batch.get("batch_id") == identifier and batch.get("status") in ("sending", "held")
        if not valid:
            raise SafetyError("指定の種類・IDの保留だけ解決できます。状態は変更していません")
        now, events = clock(), []
        stamp = iso(now)
        if kind == "instruction":
            info.update(resolved_at=stamp, resolution=outcome)
            item.update(resolved_at=stamp, resolution=outcome)
            if outcome == "posted":
                item.update(status="posted", posted_at=stamp, manually_confirmed=True)
                info.update(status="posted", posted_at=stamp, posted_event=True,
                            slack_ts=item.get("ts"), manually_confirmed=True)
                info.pop("retry_permitted_once", None)
                item.pop("retry_permitted_once", None)
            else:
                item.update(status="queued", retry_permitted_once=True)
                info.update(status="posting", retry_permitted_once=True)
            _cancel_hold_notices(state, key, stamp)
        elif kind == "notice":
            item.update(resolved_at=stamp, resolution=outcome, manually_confirmed=True)
            if outcome == "posted":
                item.update(status="posted", posted_at=stamp)
                item.setdefault("ts", None)  # Manual confirmation is not a Slack timestamp.
            else:
                item.update(status="cancelled", cancelled_at=stamp)
            events.extend(_notice_effects(state, item, outcome, now))
            _cancel_hold_notices(state, key, stamp)
        elif kind == "notion":
            info.update(status=restore if outcome == "checked" else "cancelled",
                        resolved_at=stamp, resolution=outcome, manually_confirmed=True)
            info.pop("notion_write_intent", None)
            info.pop("hold_reason", None)
            info.pop("status_before_notion_hold", None)
            if item and item.get("status") in ("queued", "held"):
                if outcome == "cancel":
                    item.update(status="cancelled", cancelled_at=stamp)
                elif item.get("hold_reason") == "notion_conflict" and restore == "prepared":
                    item.update(status="blocked")
                    item.pop("hold_reason", None)
            _cancel_hold_notices(state, key, stamp)
        else:
            if outcome == "written":
                ids = set(batch["event_ids"])
                state["log_buffer"] = [ev for ev in state["log_buffer"] if ev["event_id"] not in ids]
                state["last_log_flush_at"] = stamp
            state["log_batch"] = None
            notice = state["outbox"].get("log-hold:" + identifier)
            if notice and notice.get("status") == "queued":
                notice.update(status="cancelled", cancelled_at=stamp, reason="manual_resolution")
        events.append(new_event(now, now, "保留を手動解決", identifier, "たかむね",
                                evidence="--resolve " + kind + " " + outcome,
                                memo="手動確認・ローカル状態のみ変更"))
        events = [json.loads(sanitize(json.dumps(ev, ensure_ascii=False), config.secrets)) for ev in events]
        state["log_buffer"].extend(events)
        state.setdefault("resolutions", []).append(json.loads(sanitize(json.dumps({
            "type": kind, "id": identifier, "outcome": outcome, "at": stamp}, ensure_ascii=False), config.secrets)))
        store.save(state)  # All acknowledgement effects and audit intents commit together.
        for ev in events:
            logs.event(ev)
        logs.operational("保留を手動解決：" + identifier + " " + outcome)
        return {"status": "resolved", "type": kind, "id": identifier,
                "request_id": identifier, "outcome": outcome, "posts": 0}
