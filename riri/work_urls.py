"""Validate supplied work-session link forms without sharing or fetching them.

A supported URL shape does not prove that the work happened in that conversation.
The source reporter supplies the URL; this code never derives one from a local
Claude session ID and never creates a public share.
"""
import re
from urllib.parse import urlsplit

UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
PATHS = {
    'chatgpt.com': re.compile(r'/(?:c/' + UUID + r'|g/g-[A-Za-z0-9_-]+/c/' + UUID + r')'),
    'claude.ai': re.compile(r'/(?:chat/' + UUID + r'|code/cse_[A-Za-z0-9]{8,96})'),
    'grok.com': re.compile(r'/c/' + UUID),
}
URL_FIELDS = {'作業場所URL', '作業チャットURL', '実行セッションURL'}
MAX_URL_LENGTH = 512


def validate_work_url(value, secrets=()):
    """(safe original URL, status, reason); unsafe raw values never escape."""
    if not isinstance(value, str) or not value.strip():
        return '', 'missing', 'missing'
    value = value.strip()
    if re.match(r'^(?:なし|無し|不明|未確認|未記載|未取得|取得不可)(?:$|[（(：:])', value):
        return '', 'missing', 'unavailable'
    if (len(value) > MAX_URL_LENGTH or re.search(r'[\s<>|\\"\'`%\x00-\x20\x7f]', value) or
            any(ord(c) > 127 for c in value) or
            any(secret and secret in value for secret in secrets) or
            re.search(r'xox|ntn_|secret_', value, re.I)):
        return '', 'invalid', 'unsafe'
    try:
        parts = urlsplit(value)
        if parts.username or parts.password or parts.port:
            return '', 'invalid', 'unsafe'
        if parts.query or parts.fragment:
            return '', 'unsupported', 'unsupported'
        if parts.scheme == 'codex':
            if (value.startswith('codex://threads/') and parts.netloc == 'threads' and
                    re.fullmatch('/' + UUID, parts.path)):
                return value, 'valid', 'supported_format'
            return '', 'unsupported', 'unsupported'
        if not value.startswith('https://') or parts.scheme != 'https':
            return '', 'invalid', 'unsafe'
        pattern = PATHS.get(parts.netloc.lower())
        if pattern is not None and pattern.fullmatch(parts.path):
            return value, 'valid', 'supported_format'
    except ValueError:
        return '', 'invalid', 'unsafe'
    return '', 'unsupported', 'unsupported'


def supplied_work_url(values):
    if len(values) != 1:
        return ('', 'missing', 'missing') if not values else ('', 'invalid', 'multiple_values')
    return validate_work_url(values[0])
