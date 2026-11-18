"""Pure source checks: a changed task cannot stop other completion decisions."""
from .errors import StatusMissing
from .models import Request
from .parse import replace_status


def source_issue(request, info, *, live):
    # Corrupt accepted data is not a recoverable edit in the current source.
    snapshot = Request(**info['snapshot'])
    original = replace_status(snapshot.text, '未着手')
    if request is None:
        return 'source_missing'
    if request.block_id != info['block_id']:
        return 'source_changed'
    try:
        current = replace_status(request.text, '未着手')
    except StatusMissing:
        return 'source_status_changed'
    if current != original:
        return 'source_changed'
    if live and request.status not in {'対応中', '完了'}:
        return 'source_status_changed'
    return None
