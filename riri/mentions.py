"""Only the locally configured report recipient may be added by trusted code."""
import re

from .compose import assert_safe
from .errors import AccountMismatch, UnsafeContent


def validate_mention_id(user_id, expected_user_id):
    # Reuse the existing owner exclusion; never introduce a second real ID list.
    from .slack import FORBIDDEN_USER_IDS
    if (not isinstance(user_id, str) or not re.fullmatch(r'U[A-Z0-9]+', user_id)
            or user_id in FORBIDDEN_USER_IDS or user_id == expected_user_id):
        raise AccountMismatch('ICE_MENTION_USER_ID の設定が未指定または不正です（値は表示しません）')
    return user_id


def add_report_mention(text, channel, report_channel, user_id, expected_user_id, secrets=()):
    # Inspect BEFORE adding a mention, including encoded content and configured
    # recipient mentions supplied by an untrusted request/report body.
    assert_safe(text, secrets)
    if not text.strip():
        raise UnsafeContent('Empty outgoing text')
    if channel != report_channel:
        return text
    validate_mention_id(user_id, expected_user_id)
    return f'<@{user_id}>\n{text}'


def assert_outgoing_safe(text, channel, report_channel, user_id, expected_user_id, secrets=()):
    """Validate persisted text without rewriting legacy outbox entries.

    Only a literal, exact first line on the configured report destination is
    exempted. All remaining text retains the original strict safety boundary.
    This is a transport check, not permission to accept mentions from sources;
    add_report_mention performs that pre-decoration source validation.
    """
    if not isinstance(text, str) or not text.strip():
        raise UnsafeContent('Empty outgoing text')
    prefix = f'<@{user_id}>\n' if isinstance(user_id, str) and user_id else None
    body = text
    if channel == report_channel and prefix and text.startswith(prefix):
        validate_mention_id(user_id, expected_user_id)
        body = text[len(prefix):]
    assert_safe(body, secrets)
    # Do not let removing the permitted prefix hide a configured secret.
    if any(secret and secret in text for secret in secrets):
        raise UnsafeContent('Unsafe outgoing text')
    if not body.strip():
        raise UnsafeContent('Empty outgoing body')
    return text
