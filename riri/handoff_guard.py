"""Conservative content comparison for newly dispatched, target-specific work.

This only identifies duplicate candidates. It does not resolve, replay or erase
an existing request. Short templates without an identifiable target are outside
this guard because identical wording can describe separate work.
"""
import hashlib
from datetime import datetime
import json
import re

from .models import Request
from .parse import ID_RE


EXECUTION_FIELDS = ('案件ページ', 'GitHubプロジェクト', 'やること', '手順', '使うもの',
                    'やってはいけないこと', '期待する結果', '期限・実行タイミング',
                    'うまくいかないとき')
TERMINAL = {'done', 'cancelled', 'skipped', 'source_removed'}
PLACEHOLDERS = {'', 'なし', '無し', '未確認', '未定', '不明', '要確認', 'なし（変更しない）'}


def normalized(value):
    """Normalize line endings/outer whitespace, never change a path or URL."""
    return '\n'.join(line.strip() for line in str(value or '').replace('\r\n', '\n').split('\n')).strip()


def execution_fingerprint(req):
    page = normalized(req.fields.get('案件ページ'))
    resources = normalized(req.fields.get('使うもの'))
    page_target = re.search(r'https?://[^\s<>]+', page)
    resources_target = (resources not in PLACEHOLDERS and not re.match(
        r'^(?:なし|無し|未確認|未定|不明|要確認|架空データのみ|模擬データのみ|N/A)(?:$|[（(、，,。])',
        resources, re.I))
    if not page_target and not resources_target:
        return None
    fields = {key: normalized(req.fields.get(key)) for key in EXECUTION_FIELDS}
    payload = {'recipients': sorted(set(req.recipients)), 'fields': fields}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _seen_at(info):
    try:
        value = datetime.fromisoformat(info.get('first_seen', ''))
        return value if value.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def _prior_active(req, original, positions, state):
    """Saved dispatch/observation precedes page layout as ordering evidence."""
    current = state['requests'].get(req.id, {})
    prior = state['requests'].get(original.id, {})
    if not prior:
        # A manually active source can exist without a local dispatch record.
        return original.status == '対応中'
    if not current:
        return True
    if (prior.get('status') == 'posted' or
            state.get('outbox', {}).get('instruction:' + original.id, {}).get('status') == 'posted'):
        # Never deliver an equivalent waiting item while another is dispatched,
        # even if the waiting source or an old queue has since been moved.
        return True
    a, b = _seen_at(prior), _seen_at(current)
    if a is not None and b is not None and a != b:
        return a < b
    # For as-yet unsent work observed in the same tick, retain source ordering.
    return positions[original.id] > positions[req.id]


def older_active_duplicate(req, requests, state):
    """Return a prior matching active request and digest, or None.

Persisted snapshots describe work actually dispatched. Saved active observations
are stronger ordering evidence than a subsequently rearranged page. Source
completion and terminal state end eligibility. Already-dispatched requests are
never rewritten by this guard.
"""
    digest = execution_fingerprint(req)
    if digest is None:
        return None
    positions = {r.id: n for n, r in enumerate(requests)}
    for original in reversed(requests):
        if (original.id == req.id or original.status == '完了' or
                not ID_RE.fullmatch(original.id)):
            continue
        info = state['requests'].get(original.id, {})
        if info.get('status') in TERMINAL or info.get('hold_reason') == 'content_duplicate':
            continue
        if not _prior_active(req, original, positions, state):
            continue
        snapshot = info.get('snapshot')
        candidate = Request(**snapshot) if isinstance(snapshot, dict) else original
        if execution_fingerprint(candidate) == digest:
            return original.id, digest
    return None
