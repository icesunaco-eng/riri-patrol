"""Local status projections. Saved observations never claim to be a live read."""
from .compose import safe_reference


def practice_cleanup_items(state):
    result = []
    for request_id, info in state["requests"].items():
        if info.get("status") != "done":
            continue
        observed = info.get("source_status_observed")
        observed_at = info.get("source_status_observed_at")
        if observed is None:
            observed = info.get("snapshot", {}).get("status", "未確認")
        if observed == "完了":
            continue
        if observed not in ("未着手", "対応中", "見つからない", "未確認"):
            observed = "不明（要確認）"
        result.append({"request": safe_reference(request_id), "practice_status": "完了",
                       "last_observed_notion_status": observed,
                       "observed_at": observed_at,
                       "needs_manual_verification": True})
    return result
