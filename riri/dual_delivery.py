"""New delivery intents only. Legacy messages are never reidentified or replayed."""
import hashlib
import re

from .delivery_policy import make_delivery_policy, validate_delivery_text
from .errors import SafetyError
from .eventlog import iso
from .parse import parse_requests, replace_status


def queue_pair(patrol, intents, collection, identifier, bundle):
    """No partial plan may leak into a later error handler's state save."""
    keys = [entry[0] for entry in intents]
    if len(keys) != 2 or len(set(keys)) != 2 or any(key in patrol.state['outbox'] for key in keys):
        raise SafetyError('既存の配送記録を2か所配送へ取り替えることはできません')
    try:
        for key, channel, body, kind, meta in intents:
            patrol.queue(key, channel, body, kind, persist=False, **meta)
    except BaseException:
        # No remote call or save has occurred. Keep preexisting records intact.
        for key in keys:
            patrol.state['outbox'].pop(key, None)
        raise
    patrol.state.setdefault(collection, {})[identifier] = bundle
    patrol.save()


def confirmed_post(state, key):
    item = state['outbox'].get(key, {})
    stamp = item.get('ts')
    return (item.get('status') == 'posted' and isinstance(stamp, str) and
            (bool(re.fullmatch(r'[0-9]+\.[0-9]+', stamp)) or
             (state.get('mode') == 'practice' and stamp == 'practice-' + key)))


def queue_aisu_instruction(patrol, request_id, info, **source_meta):
    source_key = 'instruction:' + request_id
    relay_key = 'instruction-gdm:' + request_id
    source = patrol.state['outbox'].get(source_key)
    if source is not None:
        return source  # An existing intent keeps its original policy and body.
    body = info['draft']
    policy = make_delivery_policy(patrol.config, 'aisu_instruction',
        bundle_id=source_key, canonical_text=body, request_id=request_id,
        recipients=info['recipients'])
    bundle = {
        'keys': [source_key, relay_key], 'policy_version': 'dual-v2',
        'canonical_hash': hashlib.sha256(body.encode()).hexdigest(),
        'status': 'pending', 'recipient_receipt': 'unverified',
        'created_at': iso(patrol.clock())}
    queue_pair(patrol, [
        (source_key, patrol.config.instruction_channel, body, 'instruction',
         dict(source_meta, request_id=request_id, delivery_policy=policy)),
        (relay_key, patrol.config.report_channel, body, 'instruction_relay',
         dict(request_id=request_id, delivery_policy=policy, requires_posted=source_key))],
         'instruction_deliveries', request_id, bundle)
    info['dual_delivery_status'] = 'pending'
    return patrol.state['outbox'][source_key]


def instruction_delivery_status(state, request_id):
    bundle = state.get('instruction_deliveries', {}).get(request_id)
    if not bundle:
        return
    items = [state['outbox'].get(k, {}) for k in bundle['keys']]
    statuses = [item.get('status') for item in items]
    posted = [confirmed_post(state, key) for key in bundle['keys']]
    if all(posted):
        status = 'posted'
    elif any(s == 'posted' and not confirmed for s, confirmed in zip(statuses, posted)):
        status = 'held'
    elif 'held' in statuses:
        status = 'held'
    elif 'cancelled' in statuses:
        status = 'cancelled'
    elif 'posted' in statuses:
        status = 'partial'
    else:
        status = 'pending'
    bundle['status'] = status
    state['requests'].get(request_id, {})['dual_delivery_status'] = status


def preflight_dual_source(patrol, item):
    """A partial delivery cannot silently finish with a changed original."""
    policy = item.get('delivery_policy', {})
    if policy.get('purpose') != 'aisu_instruction':
        return
    if item.get('requires_posted'):
        # The first write may take time. Never use its pre-send Notion cache
        # as evidence that a still-unsent second leg has the same original.
        patrol.check_time()
        latest = parse_requests(patrol.notion.read_blocks(patrol.config.requests_page))
        ids = [request.id for request in latest]
        if len(ids) != len(set(ids)):
            raise SafetyError('2か所配送の直前に依頼IDの重複を検出しました')
        patrol.current_requests = {request.id: request for request in latest}
    request_id = item.get('request_id')
    info = patrol.state['requests'].get(request_id, {})
    req = patrol.current_requests.get(request_id)
    snapshot = info.get('snapshot', {})
    source = patrol.state['outbox'].get('instruction:' + str(request_id), {})
    if source.get('delivery_policy') != policy:
        raise SafetyError('2か所配送の保存済み方針が一致しません')
    validate_delivery_text(source.get('text'), policy,
        ice_mention_user_id=patrol.config.ice_mention_user_id,
        expected_user_id=patrol.config.expected_user_id,
        requester_mention_ids=patrol.config.requester_mention_ids,
        secrets=patrol.config.secrets)
    if (not req or req.block_id != snapshot.get('block_id') or
            replace_status(req.text, '未着手') != replace_status(snapshot.get('text', ''), '未着手') or
            policy.get('canonical_hash') != hashlib.sha256(info.get('draft', '').encode()).hexdigest() or
            policy.get('recipients') != info.get('recipients')):
        raise SafetyError('2か所配送の元依頼が変更されています。対象だけ照合を待ちます')
