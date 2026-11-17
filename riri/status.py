"""Local status projections. Saved observations never claim to be a live read."""
from .compose import safe_reference


def dual_delivery_items(state):
    """Metadata only. Two posts never prove that the recipient began the work."""
    result = {'instructions': [], 'reports': [], 'requester_waiting': []}
    for request_id, bundle in state.get('instruction_deliveries', {}).items():
        result['instructions'].append({'request': safe_reference(request_id),
            'status': bundle.get('status'), 'recipient_receipt': bundle.get('recipient_receipt'),
            'destinations': [{'channel': state['outbox'].get(key, {}).get('channel'),
                              'status': state['outbox'].get(key, {}).get('status'),
                              'owner_receipt_recorded': bool(state['outbox'].get(key, {}).get('delivery_receipt'))}
                             for key in bundle.get('keys', [])]})
    for bundle in state.get('report_deliveries', {}).values():
        result['reports'].append({'requester': bundle.get('requester'),
            'requests': [safe_reference(r.get('id'), r.get('reporter', '')) for r in bundle.get('reports', [])],
            'status': bundle.get('status'), 'mention_mode': bundle.get('mention_mode'),
            'recipient_receipt': bundle.get('recipient_receipt'),
            'destinations': [{'channel': state['outbox'].get(key, {}).get('channel'),
                              'status': state['outbox'].get(key, {}).get('status')}
                             for key in bundle.get('keys', [])]})
    result['requester_waiting'] = [{'request': safe_reference(r.get('request_id'), r.get('reporter', '')),
        'reason': r.get('reason')} for r in state.get('report_requester_waiting', {}).values()
        if r.get('status') == 'pending']
    return result


def local_wake_items(state):
    """(stable identity, request ID, saved item), without source bodies."""
    for key, source in list(state.get("outbox", {}).items()):
        item = source.get("noa_instruction_wake", {})
        if item.get("transport") == "local":
            yield key, source.get("request_id", ""), item
    for key, item in list(state.get("noa_wake", {}).get("items", {}).items()):
        if item.get("transport") == "local":
            yield key, "", item


def local_execution_items(state, root):
    """Read-only result projections; old delivery done never means work done."""
    from .local_outcomes import observe
    result = []
    for identity, request_id, item in local_wake_items(state):
        observation = observe(root, item.get("local_ref"))
        expected = item.get("local_run_id")
        if expected and (observation.get("run_id") != expected or
                         not item.get("local_input_sha256") or
                         observation.get("input_sha256") != item["local_input_sha256"]):
            observation = {"state": "unknown", "outcome": "unknown", "partial_work_possible": True}
        elif not expected:
            observation = {"state": "legacy_unobserved", "outcome": "unknown",
                           "partial_work_possible": True}
        result.append({"request": safe_reference(request_id),
                       "delivery_status": item.get("status"),
                       "delivery_reason": item.get("reason"),
                       "execution": observation,
                       "task_completion": state.get("requests", {}).get(request_id, {}).get("status") == "done",
                       "task_completion_note": "CLI終了と正式な依頼完了は別判定です"})
    return result


def isolated_report_items(state):
    """Expose reviewable references, never the malformed report body."""
    return [{"request": safe_reference(item.get("request_id"), item.get("reporter", "")),
             "reason": item.get("reason"), "needs_review": True}
            for item in state.get("quarantined_reports", {}).values()
            if item.get("status") == "isolated"]


def work_url_pending_items(state):
    return [{"request": safe_reference(item.get("request_id"), item.get("reporter", "")),
             "reason": item.get("reason"), "needs_url": True}
            for item in state.get("work_url_policy", {}).get("pending_versions", {}).values()
            if item.get("status") == "pending"]


def local_execution_needs_review(state):
    """A healthy patrol is not proof that an unfinished local run succeeded."""
    for _, request_id, item in local_wake_items(state):
        observation = item.get("execution", {})
        if not item.get("local_run_id") or item.get("status") == "reviewed" or not observation:
            continue
        if request_id and state.get("requests", {}).get(request_id, {}).get("status") == "done":
            continue  # Later formal completion coexists with this run's history.
        if observation.get("outcome") not in {"pending", "cli_exited"}:
            return True
    return False

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

