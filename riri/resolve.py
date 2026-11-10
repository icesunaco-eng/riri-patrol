"""Explicit, locked, local-only resolution of an uncertain Slack instruction."""
from datetime import datetime, timezone
import json

from .errors import SafetyError, sanitize
from .eventlog import LocalLogs, iso, new_event
from .lock import patrol_lock
from .state import StateStore


def resolve_request(config, request_id, outcome, *, clock=None, logs=None):
    if outcome not in ("posted", "not_posted"):
        raise SafetyError("解決方法は posted または not_posted です")
    clock = clock or (lambda: datetime.now(timezone.utc))
    logs = logs or LocalLogs(config.root, config.secrets)
    # No clients, transports, or model are constructed by this operation.
    with patrol_lock(config.root) as acquired:
        if not acquired:
            return {"status": "locked", "posts": 0}
        store = StateStore(config.root, config.mode)
        state = store.load()
        info = state["requests"].get(request_id)
        key = "instruction:" + request_id
        item = state["outbox"].get(key)
        if (not info or info.get("status") != "held" or not item or
                item.get("status") != "held" or item.get("kind") != "instruction" or
                info.get("hold_reason") == "notion_conflict"):
            raise SafetyError("Slack投稿結果が保留中の依頼だけ解決できます。状態は変更していません")
        now = clock()
        stamp = iso(now)
        info["resolved_at"] = item["resolved_at"] = stamp
        info["resolution"] = item["resolution"] = outcome
        if outcome == "posted":
            item.update(status="posted", posted_at=stamp, manually_confirmed=True)
            info.update(status="posted", posted_at=stamp, posted_event=True,
                        slack_ts=item.get("ts"), manually_confirmed=True)
            info.pop("retry_permitted_once", None)
            item.pop("retry_permitted_once", None)
        else:
            item.update(status="queued", retry_permitted_once=True)
            info.update(status="posting", retry_permitted_once=True)
        notice = state["outbox"].get("hold:" + key)
        if notice and notice.get("status") == "queued":
            notice.update(status="cancelled", cancelled_at=stamp, reason="manual_resolution")
        ev = new_event(now, now, "保留を手動解決", request_id, "たかむね",
                       evidence="--resolve " + outcome,
                       memo="投稿済み確認" if outcome == "posted" else "次回に一度だけ投稿を許可")
        ev = json.loads(sanitize(json.dumps(ev, ensure_ascii=False), config.secrets))
        state["log_buffer"].append(ev)
        store.save(state)  # Resolution and audit intent are a single durable commit.
        logs.event(ev)
        logs.operational("保留を手動解決：" + request_id + " " + outcome)
        return {"status": "resolved", "request_id": request_id, "outcome": outcome, "posts": 0}
