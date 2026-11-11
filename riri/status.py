"""Local status projections. Saved observations never claim to be a live read."""
from .compose import safe_reference

PRACTICE_STATUSES = {
    "prepared": "準備済み", "posting": "投稿処理中", "posted": "指示済み",
    "done": "完了", "held": "保留", "skipped": "対象外",
    "cancelled": "取消済み", "source_removed": "元の依頼が削除済み",
}


def practice_cleanup_items(state):
    result = []
    for request_id, info in state["requests"].items():
        observed = info.get("source_status_observed")
        observed_at = info.get("source_status_observed_at")
        if observed is None:
            observed = info.get("snapshot", {}).get("status", "未確認")
        if observed == "完了":
            continue
        if observed not in ("未着手", "対応中", "見つからない", "未確認"):
            observed = "不明（要確認）"
        status = info.get("status")
        practice_status = (PRACTICE_STATUSES.get(status, "不明（要確認）")
                           if isinstance(status, str) else "不明（要確認）")
        result.append({"request": safe_reference(request_id), "practice_status": practice_status,
                       "last_observed_notion_status": observed,
                       "observed_at": observed_at,
                       "needs_manual_verification": True})
    return result

