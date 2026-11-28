"""Who wrote a block (P2) and the human completion approval (5-3, B2).

Design: みに wake design v1.0 (Notion), 共通の決まり P2 and 5-3.
Nothing in patrol calls this yet.  Notion access is passed in as functions so
the flow can be tested without the network.

P2: the writer of a block is decided from *both* ``created_by`` and
``last_edited_by``, mapped through MINI_AUTHOR_MAP (Notion ID -> exactly one
member).  The only exception is a 【りりへの依頼】 block whose last editor is
りり's own integration and whose only change is the status field.  Approval
blocks never get that exception.

B2: the approval snapshot (block ID, text hash, editors, version) is written to
the run record as "pending" before the status changes; after the change the
block is read back and compared again.  If it no longer matches, the status is
put back to 対応中 and one notice per version is sent.  A restart redoes the
comparison while the record still says "pending".
"""
import hashlib
import re


def _h(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def parse_approval(text):
    if not text.lstrip().startswith('【完了承認】'):
        return None
    fields = {}
    for line in text.splitlines()[1:]:
        m = re.match(r'^\s*([^：:]+)[：:]\s*(.*?)\s*$', line)
        if m:
            key = re.sub(r'\s+', '', m[1])
            if key in fields:
                return None  # duplicate field: never guess
            fields[key] = m[2]
    version = re.fullmatch(r'第(\d+)版', fields.get('版', ''))
    need = ('依頼ID', '本文ハッシュ', '承認した人')
    if not version or any(not fields.get(k) for k in need):
        return None
    return {'request_id': fields['依頼ID'], 'version': int(version[1]),
            'body_hash': fields['本文ハッシュ'], 'approver': fields['承認した人']}


def writer_of(block, author_map, riri_ids, *, status_only_change=False, is_request_block=False):
    """P2: the one member who wrote this block, or None (hold)."""
    created = (block.get('created_by') or {}).get('id')
    edited = (block.get('last_edited_by') or {}).get('id')
    if not created or not edited:
        return None
    creator = author_map.get(created)
    if not creator:
        return None
    if edited in riri_ids:
        return creator if (is_request_block and status_only_change) else None
    return creator if author_map.get(edited) == creator else None


def snapshot(block, text):
    return {'block_id': block['id'], 'text_sha256': _h(text),
            'created_by': (block.get('created_by') or {}).get('id'),
            'last_edited_by': (block.get('last_edited_by') or {}).get('id'),
            'last_edited_time': block.get('last_edited_time')}


def check_approval(block, text, *, author_map, riri_ids, current):
    """Is this approval valid for the current report?  Returns (ok, reason)."""
    parsed = parse_approval(text)
    if not parsed:
        return False, '【完了承認】の形ではない'
    if writer_of(block, author_map, riri_ids) != parsed['approver']:
        return False, '書き手が承認した人と一致しない（P2）'
    if parsed['approver'] not in current['approvers']:
        return False, '承認できる人ではない'
    if (parsed['request_id'], parsed['version'], parsed['body_hash']) != (
            current['request_id'], current['version'], current['body_hash']):
        return False, '依頼ID・版・本文ハッシュが現在の報告と違う'
    return True, ''


def _same(snap, block, text):
    now = snapshot(block, text)
    # last_edited_time is minute-granular in Notion: the hash and editors decide.
    return all(now[k] == snap[k] for k in ('block_id', 'text_sha256', 'created_by', 'last_edited_by'))


def approve(store, run_id, *, block_id, get_block, block_text, set_status, read_status,
            notify, author_map, riri_ids, current):
    """Steps 1-4 of 5-3.  Returns the final approval state."""
    block = get_block(block_id)
    text = block_text(block)
    ok, reason = check_approval(block, text, author_map=author_map, riri_ids=riri_ids, current=current)
    if not ok:
        return 'invalid: ' + reason
    snap = snapshot(block, text)
    snap['version'] = current['version']

    def pending(record):
        record['approval'] = {**snap, 'state': 'pending'}
        return record
    store.update(run_id, pending)
    set_status('完了')
    return confirm(store, run_id, get_block=get_block, block_text=block_text, set_status=set_status,
                   read_status=read_status, notify=notify, author_map=author_map,
                   riri_ids=riri_ids, current=current)


def confirm(store, run_id, *, get_block, block_text, set_status, read_status, notify,
            author_map, riri_ids, current):
    """Step 3-5 of 5-3: read back and either confirm or put the status back."""
    record = store.read(run_id) or {}
    snap = record.get('approval')
    if not snap or snap.get('state') != 'pending':
        return (snap or {}).get('state', 'none')
    block = get_block(snap['block_id'])
    text = block_text(block)
    ok, _ = check_approval(block, text, author_map=author_map, riri_ids=riri_ids, current=current)
    still = ok and _same(snap, block, text) and snap['version'] == current['version']
    if still and read_status() == '完了':
        store.update(run_id, lambda r: {**r, 'approval': {**r['approval'], 'state': 'confirmed'}})
        return 'confirmed'
    set_status('対応中')
    key = ('approval_invalid', current['request_id'], current['version'])

    def invalid(r):
        sent = set(map(tuple, r.get('notices', [])))
        r['approval'] = {**r['approval'], 'state': 'invalid_reverted'}
        if key not in sent:
            r.setdefault('notices', []).append(list(key))
        return r
    before = set(map(tuple, (store.read(run_id) or {}).get('notices', [])))
    store.update(run_id, invalid)
    if key not in before:
        notify(key)
    return 'invalid_reverted'


recover = confirm  # a restart redoes the read-back while the record says "pending"
