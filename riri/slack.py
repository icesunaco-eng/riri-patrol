"""Narrow verified-bot Slack client. No impersonation, edit, or delete APIs."""
import html
import re
from urllib.parse import urlencode
from .errors import AccountMismatch, RemoteError, SafetyError, UncertainPost, StopRequested

FORBIDDEN_USER_IDS = frozenset({'U0C5V11S37X'})
# Only documented, definitive pre-send rejections are safe to classify as not sent.
# New/unknown server errors remain uncertain and must never authorize a resend.
DEFINITE_POST_REJECTIONS = frozenset({
    'not_authed', 'invalid_auth', 'token_revoked', 'account_inactive',
    'missing_scope', 'no_permission', 'channel_not_found', 'not_in_channel',
    'is_archived', 'restricted_action', 'restricted_action_read_only_channel',
    'restricted_action_thread_only_channel', 'restricted_action_non_threadable_channel',
    'no_text', 'msg_too_long', 'invalid_arguments', 'invalid_arg_name',
    'invalid_blocks', 'invalid_blocks_format', 'invalid_metadata_format',
    'invalid_metadata_schema', 'metadata_too_large', 'too_many_attachments',
    'rate_limited', 'ratelimited',
})

def safe_text(text, secrets=()):
    if not isinstance(text, str) or not text.strip():
        raise SafetyError('Empty outgoing text')
    if any(s and s in text for s in secrets) or re.search(r'xox|ntn_|secret_|<[@!]|@(channel|here|everyone)\b', text, re.I):
        raise SafetyError('Unsafe outgoing text')
    if any(ord(c) < 32 and c not in '\n\t\r' for c in text):
        raise SafetyError('Control character in outgoing text')

class SlackClient:
    def __init__(self, token, expected_bot_id, expected_user_id, transport):
        self.token, self.expected_bot_id, self.expected_user_id = token, expected_bot_id, expected_user_id
        self.transport, self.verified = transport, False
        self._identity = None

    def _request(self, endpoint, payload=None, query=None):
        url = 'https://slack.com/api/' + endpoint
        if query:
            url += '?' + urlencode(query)
        result = self.transport.request('POST' if payload is not None else 'GET', url,
            headers={'Authorization': 'Bearer ' + self.token}, payload=payload, timeout=20)
        if not isinstance(result, dict) or 'ok' not in result:
            raise RemoteError('Malformed Slack response', uncertain=endpoint == 'chat.postMessage')
        if result.get('ok') is not True:
            code = result.get('error')
            rejected = isinstance(code, str) and code in DEFINITE_POST_REJECTIONS
            uncertain = endpoint == 'chat.postMessage' and not rejected
            raise RemoteError('Slack API rejected request', uncertain=uncertain)
        return result

    def auth_test(self):
        self.verified, self._identity = False, None
        if not self.token.startswith('xoxb-') or not self.expected_bot_id or not self.expected_user_id or self.expected_user_id in FORBIDDEN_USER_IDS:
            raise AccountMismatch('Slack bot identity configuration rejected')
        who = self._request('auth.test')
        if (who.get('bot_id') != self.expected_bot_id or who.get('user_id') != self.expected_user_id or who.get('user_id') in FORBIDDEN_USER_IDS):
            raise AccountMismatch('Slack bot identity mismatch')
        self._identity = (self.token, self.expected_bot_id, self.expected_user_id)
        self.verified = True
        return who

    def _guard(self):
        if not self.verified or self._identity != (self.token, self.expected_bot_id, self.expected_user_id) or self.expected_user_id in FORBIDDEN_USER_IDS:
            raise AccountMismatch('Slack bot has not been verified')

    @staticmethod
    def _channel(channel):
        if not isinstance(channel, str) or not re.fullmatch(r'[CGD][A-Z0-9]+', channel):
            raise SafetyError('Invalid Slack channel')

    def post(self, channel, text, *, dry_run=False):
        self._guard()
        self._channel(channel)
        safe_text(text, (self.token,))
        if dry_run:
            return None
        try:
            result = self._request('chat.postMessage', {'channel': channel, 'text': text,
                'parse': 'none', 'link_names': False, 'unfurl_links': False, 'unfurl_media': False})
        except StopRequested:
            # Keep transport's known-not-sent evidence (or its absence) intact.
            raise
        except RemoteError as error:
            if error.uncertain:
                raise UncertainPost() from None
            raise
        except (OSError, TimeoutError):
            raise UncertainPost() from None
        ts = result.get('ts')
        if not isinstance(ts, str) or not re.fullmatch(r'\d+\.\d+', ts):
            raise UncertainPost('Slack success response lacked message timestamp')
        return ts

    def history(self, channel, oldest):
        self._guard()
        self._channel(channel)
        messages, seen, cursor = [], set(), None
        for _ in range(10000):
            query = {'channel': channel, 'oldest': str(oldest), 'inclusive': 'true', 'limit': 100}
            if cursor:
                query['cursor'] = cursor
            result = self._request('conversations.history', query=query)
            batch = result.get('messages')
            if not isinstance(batch, list) or any(not isinstance(m, dict) for m in batch):
                raise RemoteError('Incomplete Slack history')
            messages.extend(batch)
            cursor = result.get('response_metadata', {}).get('next_cursor', '')
            if not cursor:
                if result.get('has_more'):
                    raise RemoteError('Incomplete Slack history pagination')
                return messages
            if not isinstance(cursor, str) or cursor in seen:
                raise RemoteError('Invalid Slack history cursor')
            seen.add(cursor)
        raise RemoteError('Slack history pagination limit reached')

    def permalink(self, channel, ts):
        self._guard()
        self._channel(channel)
        if not isinstance(ts, str) or not re.fullmatch(r"\d+\.\d+", ts):
            raise SafetyError("Invalid Slack timestamp")
        result = self._request("chat.getPermalink", query={"channel": channel, "message_ts": ts})
        from .noa import validate_permalink
        return validate_permalink(result.get("permalink"), channel, ts)

    def find_post(self, channel, marker, oldest):
        if not marker or '\n' in marker or '\r' in marker:
            raise SafetyError('Invalid exact message marker')
        # Exhaust history BEFORE accepting a match: partial reads cannot resolve uncertainty.
        matches = [m for m in self.history(channel, oldest)
                   if m.get('bot_id') == self.expected_bot_id and m.get('user') == self.expected_user_id
                   and isinstance(m.get('text'), str)
                   and marker in html.unescape(m['text']).splitlines()
                   and isinstance(m.get('ts'), str) and re.fullmatch(r'\d+\.\d+', m['ts'])]
        return matches[0]['ts'] if matches else None
