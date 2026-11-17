"""Explicit, persisted authority for the two approved owner-posting routes.

Legacy outbox records never acquire this policy implicitly. Source text cannot
select a Slack identity, destination, or official mention.
"""
import hashlib
import re

from .compose import assert_safe
from .errors import AccountMismatch, SafetyError
from .models import RECIPIENTS
from .parse import ID_RE

VERSION = 'dual-v2'
INSTRUCTION_CHANNEL = 'C0CFM2HRA5V'
REPORT_CHANNEL = 'C0CFL6A8QJH'
CHANNELS = (INSTRUCTION_CHANNEL, REPORT_CHANNEL)
POLICY_KEYS = frozenset({'version', 'purpose', 'bundle_id', 'request_id',
    'recipients', 'requester', 'mention_name', 'mention_mode', 'mention_user_id',
    'canonical_hash', 'channels'})


def configured_mentions(ice_user_id, requester_mention_ids, expected_user_id):
    """Return a validated copy; no fallback to IDs supplied by a request."""
    from .mentions import validate_mention_id
    if not isinstance(requester_mention_ids, dict):
        raise AccountMismatch('Requester mention mapping is invalid')
    mapping = dict(requester_mention_ids)
    for name, user_id in mapping.items():
        if name not in RECIPIENTS:
            raise AccountMismatch('Requester mention mapping has an unknown name')
        validate_mention_id(user_id, expected_user_id)
    validate_mention_id(ice_user_id, expected_user_id)
    if 'あいす' in mapping and mapping['あいす'] != ice_user_id:
        raise AccountMismatch('Aisu mention mapping conflicts with configured identity')
    mapping['あいす'] = ice_user_id
    if len(set(mapping.values())) != len(mapping):
        raise AccountMismatch('Requester mention identities are not unique')
    return mapping


def validate_policy_shape(policy, expected_user_id, secrets=()):
    """Validate stored policy without network access or changing its values."""
    from .mentions import validate_mention_id
    if (not isinstance(policy, dict) or set(policy) != POLICY_KEYS
            or policy.get('version') != VERSION
            or policy.get('channels') != list(CHANNELS)):
        raise SafetyError('Invalid dual delivery policy')
    bundle = policy.get('bundle_id')
    if not isinstance(bundle, str) or not bundle or len(bundle) > 512 or any(c in bundle for c in '\r\n'):
        raise SafetyError('Invalid dual delivery bundle')
    assert_safe(bundle, secrets)
    if not isinstance(policy.get('canonical_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', policy['canonical_hash']):
        raise SafetyError('Invalid canonical delivery digest')
    purpose = policy.get('purpose')
    if purpose == 'aisu_instruction':
        recipients = policy.get('recipients')
        if (not isinstance(recipients, list) or not recipients
                or any(not isinstance(n, str) or n not in RECIPIENTS for n in recipients)
                or len(set(recipients)) != len(recipients) or 'あいす' not in recipients
                or not isinstance(policy.get('request_id'), str) or not ID_RE.fullmatch(policy['request_id'])
                or policy.get('requester') != '' or policy.get('mention_name') != 'あいす'
                or policy.get('mention_mode') != 'official'):
            raise SafetyError('Invalid Aisu instruction delivery policy')
    elif purpose == 'requester_report':
        if (policy.get('requester') not in RECIPIENTS
                or policy.get('mention_name') != policy['requester']
                or policy.get('request_id') != '' or policy.get('recipients') != []):
            raise SafetyError('Invalid requester report delivery policy')
    else:
        raise SafetyError('Unapproved dual delivery purpose')
    if policy.get('mention_mode') == 'official':
        validate_mention_id(policy.get('mention_user_id'), expected_user_id)
    elif policy.get('mention_mode') != 'text' or policy.get('mention_user_id') != '':
        raise SafetyError('Invalid delivery mention mode')
    return policy


def validate_delivery_policy(policy, *, instruction_channel, report_channel,
                             ice_user_id, requester_mention_ids, expected_user_id,
                             secrets=()):
    validate_policy_shape(policy, expected_user_id, secrets)
    if (instruction_channel, report_channel) != CHANNELS:
        raise AccountMismatch('Dual delivery destinations differ from approved rooms')
    mapping = configured_mentions(ice_user_id, requester_mention_ids, expected_user_id)
    user_id = mapping.get(policy['mention_name'], '')
    mode = 'official' if user_id else 'text'
    if policy['mention_mode'] != mode or policy['mention_user_id'] != user_id:
        raise AccountMismatch('Delivery mention differs from configured requester identity')
    return policy


def make_delivery_policy(config, purpose, *, bundle_id, canonical_text,
                         request_id='', recipients=(), requester=''):
    assert_safe(canonical_text, config.secrets)
    if not canonical_text.strip():
        raise SafetyError('Empty canonical delivery')
    mapping = configured_mentions(config.ice_mention_user_id,
        config.requester_mention_ids, config.expected_user_id)
    name = 'あいす' if purpose == 'aisu_instruction' else requester
    user_id = mapping.get(name, '')
    policy = {'version': VERSION, 'purpose': purpose, 'bundle_id': bundle_id,
        'request_id': request_id, 'recipients': list(recipients), 'requester': requester,
        'mention_name': name, 'mention_mode': 'official' if user_id else 'text',
        'mention_user_id': user_id,
        'canonical_hash': hashlib.sha256(canonical_text.encode('utf-8')).hexdigest(),
        'channels': list(CHANNELS)}
    return validate_delivery_policy(policy, instruction_channel=config.instruction_channel,
        report_channel=config.report_channel, ice_user_id=config.ice_mention_user_id,
        requester_mention_ids=config.requester_mention_ids,
        expected_user_id=config.expected_user_id, secrets=config.secrets)


def mention_prefix(policy):
    return ('<@' + policy['mention_user_id'] + '>\n' if policy['mention_mode'] == 'official'
            else '@' + policy['mention_name'] + '\n')


def validate_canonical(text, policy, secrets=()):
    assert_safe(text, secrets)
    if not text.strip() or hashlib.sha256(text.encode('utf-8')).hexdigest() != policy['canonical_hash']:
        raise SafetyError('Canonical delivery content differs from persisted policy')
    return text


def decorate_delivery(text, policy, expected_user_id, secrets=()):
    """Decorate validated canonical text; the caller appends its own rp marker."""
    validate_policy_shape(policy, expected_user_id, secrets)
    validate_canonical(text, policy, secrets)
    result = mention_prefix(policy) + text
    if any(value and value in result for value in secrets):
        raise SafetyError('Configured secret in delivery envelope')
    return result


def delivery_content(text, policy, expected_user_id, secrets=()):
    """Validate a queued envelope and return (prefix, canonical body, marker)."""
    validate_policy_shape(policy, expected_user_id, secrets)
    if not isinstance(text, str) or any(value and value in text for value in secrets):
        raise SafetyError('Invalid delivery text')
    prefix = mention_prefix(policy)
    if not text.startswith(prefix):
        raise SafetyError('Missing expected delivery mention')
    body, separator, marker = text[len(prefix):].rpartition('\n')
    if not separator or not re.fullmatch(r'rp:[0-9a-f]{24}', marker):
        raise SafetyError('Missing final delivery marker')
    validate_canonical(body, policy, secrets)
    return prefix, body, marker


def validate_delivery_text(text, policy, *, ice_mention_user_id, expected_user_id,
                           requester_mention_ids=None, secrets=()):
    """Offline check for the patrol drain boundary; do not add proxy labels."""
    validate_delivery_policy(policy, instruction_channel=INSTRUCTION_CHANNEL,
        report_channel=REPORT_CHANNEL, ice_user_id=ice_mention_user_id,
        requester_mention_ids=requester_mention_ids or {},
        expected_user_id=expected_user_id, secrets=secrets)
    delivery_content(text, policy, expected_user_id, secrets)
    return text
