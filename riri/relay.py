"""Isolate an unsafe report-channel relay without replaying its source."""
import hashlib

from .compose import safe_reference
from .errors import UnsafeContent
from .eventlog import iso
from .mentions import add_report_mention


def _marker(key):
    return 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]


def _warn(patrol, request_id, key):
    # This fixed message never quotes the unsafe source or exception. queue's
    # stable key preserves sending/held/posted outcomes, including uncertainty.
    patrol.queue('relay-unsafe:' + request_id, patrol.config.report_channel,
        '指示の複製を安全確認のため保留しました。元の指示は投稿済みです。'
        '本文は転記せず、自動再投稿もしません。たかむね、内容を確認してください（'
        + safe_reference(request_id) + '）',
        'hold_notice', held_key=key, hold_episode=1)


def queue_instruction_relay(patrol, request_id, source):
    """Queue an already-posted instruction's exact body, or quarantine only it.

    The caller checks recipient/opt-in eligibility. Existing relay items are
    immutable here. New quarantines use the existing notice/held shape and
    unsafe_content reason, which allows cancellation only in the resolver.
    """
    key = 'instruction-gdm:' + request_id
    existing = patrol.state['outbox'].get(key)
    if existing is not None:
        if existing.get('status') == 'held' and existing.get('hold_reason') == 'unsafe_content':
            # A crash between saving a hold and queuing its warning is recoverable
            # without attempting the rejected body or rewriting any prior item.
            _warn(patrol, request_id, key)
        return existing
    if (not isinstance(source, dict) or source.get('status') != 'posted'
            or source.get('channel') != patrol.config.instruction_channel):
        return None
    try:
        marker = _marker('instruction:' + request_id)
        text = source.get('text')
        ending = '\n' + marker
        if source.get('marker') != marker or not isinstance(text, str) or not text.endswith(ending):
            raise UnsafeContent('確定した指示の照合印を確認できません')
        return patrol.queue(key, patrol.config.report_channel, text[:-len(ending)],
                            'instruction_relay', request_id=request_id)
    except UnsafeContent:
        # queue validates before inserting. Never retain the rejected body in a
        # new record, overwrite a pre-existing intent, or change source status.
        if key in patrol.state['outbox']:
            raise
        text = add_report_mention('安全確認のため複製を保留（本文は保存しません）',
            patrol.config.report_channel, patrol.config.report_channel,
            patrol.config.ice_mention_user_id, patrol.config.expected_user_id,
            patrol.config.secrets)
        marker = _marker(key)
        item = {'status': 'held', 'channel': patrol.config.report_channel,
                'text': text + '\n' + marker, 'marker': marker, 'kind': 'notice',
                'request_id': request_id, 'created_at': iso(patrol.clock()),
                'held_at': iso(patrol.clock()), 'hold_episode': 1,
                'hold_reason': 'unsafe_content'}
        patrol.state['outbox'][key] = item
        patrol.save()
        _warn(patrol, request_id, key)
        return item
