"""Verified bot client plus owner-authorized, visibly automated report posting."""
import html
import re
from urllib.parse import urlencode
from .errors import AccountMismatch, RemoteError, SafetyError, UncertainPost, StopRequested

REPORT_OWNER_ID = 'U0C5V11S37X'
USER_REPORT_CHANNEL = 'C0CFL6A8QJH'
AUTOMATED_REPORT_LABEL = '【りり自動通知・本人承認の代理投稿／本文は本人の直接指示ではありません】'
FORBIDDEN_USER_IDS = frozenset({REPORT_OWNER_ID})

def has_patrol_marker(text):
    return isinstance(text, str) and any(re.fullmatch(r'rp:[0-9a-f]{24}', line) for line in html.unescape(text).splitlines())
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
    def __init__(self, token, expected_bot_id, expected_user_id, transport, *,
                 report_channel="", ice_mention_user_id="", report_user_token=""):
        self.token, self.expected_bot_id, self.expected_user_id = token, expected_bot_id, expected_user_id
        self.transport, self.verified = transport, False
        self._identity = None
        self.report_user_token = report_user_token
        self.report_channel, self.ice_mention_user_id = report_channel, ice_mention_user_id

    def _request(self, endpoint, payload=None, query=None, *, use_report_user=False):
        token = self.token
        if use_report_user:
            # Hard-coded destination, no arbitrary scopes/methods via this token.
            if (not self.report_user_token.startswith('xoxp-') or
                    self.report_channel != USER_REPORT_CHANNEL):
                raise AccountMismatch('Report user token configuration rejected')
            if endpoint == 'auth.test':
                if payload is not None or query is not None:
                    raise SafetyError('Invalid report identity request')
            elif endpoint == 'chat.postMessage':
                self._guard()
                if not isinstance(payload, dict) or payload.get('channel') != USER_REPORT_CHANNEL or query is not None:
                    raise SafetyError('Report user token destination rejected')
                from .mentions import assert_outgoing_safe
                text = payload.get('text')
                assert_outgoing_safe(text, USER_REPORT_CHANNEL, self.report_channel,
                    self.ice_mention_user_id, self.expected_user_id, (self.token, self.report_user_token))
                prefix = f'<@{self.ice_mention_user_id}>\n' + AUTOMATED_REPORT_LABEL + '\n'
                if (not text.startswith(prefix) or
                        not re.fullmatch(r'rp:[0-9a-f]{24}', text.splitlines()[-1])):
                    raise SafetyError('Report user provenance rejected')
            else:
                raise SafetyError('Report user token method rejected')
            token = self.report_user_token
        url = 'https://slack.com/api/' + endpoint
        if query:
            url += '?' + urlencode(query)
        result = self.transport.request('POST' if payload is not None else 'GET', url,
            headers={'Authorization': 'Bearer ' + token}, payload=payload, timeout=20)
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
        if self.report_channel or self.ice_mention_user_id:
            from .mentions import validate_mention_id
            validate_mention_id(self.ice_mention_user_id, self.expected_user_id)
            self._channel(self.report_channel)
        who = self._request('auth.test')
        if (who.get('bot_id') != self.expected_bot_id or who.get('user_id') != self.expected_user_id or who.get('user_id') in FORBIDDEN_USER_IDS):
            raise AccountMismatch('Slack bot identity mismatch')
        if self.report_user_token:
            user = self._request('auth.test', use_report_user=True)
            if user.get('user_id') != REPORT_OWNER_ID or user.get('bot_id'):
                raise AccountMismatch('Report user identity mismatch')
            if who.get('team_id') != user.get('team_id') or not user.get('team_id'):
                raise AccountMismatch('Report user workspace mismatch')
        self._identity = (self.token, self.expected_bot_id, self.expected_user_id, self.report_channel, self.ice_mention_user_id, self.report_user_token)
        self.verified = True
        return who

    def _guard(self):
        if not self.verified or self._identity != (self.token, self.expected_bot_id, self.expected_user_id, self.report_channel, self.ice_mention_user_id, self.report_user_token) or self.expected_user_id in FORBIDDEN_USER_IDS:
            raise AccountMismatch('Slack bot has not been verified')

    @staticmethod
    def _channel(channel):
        if not isinstance(channel, str) or not re.fullmatch(r'[CGD][A-Z0-9]+', channel):
            raise SafetyError('Invalid Slack channel')

    def post(self, channel, text, *, dry_run=False):
        self._guard()
        self._channel(channel)
        from .mentions import assert_outgoing_safe
        assert_outgoing_safe(text, channel, self.report_channel, self.ice_mention_user_id,
                               self.expected_user_id, (self.token, self.report_user_token))
        use_user = bool(self.report_user_token and channel == self.report_channel)
        if use_user:
            if not re.fullmatch(r'rp:[0-9a-f]{24}', text.splitlines()[-1]):
                raise SafetyError('Automated report requires final patrol marker')
            # Visible provenance is mandatory, including old persisted outbox
            # entries. Never remove the exact first mention or final rp line.
            prefix = f'<@{self.ice_mention_user_id}>\n'
            if not text.startswith(prefix):
                raise SafetyError('Automated report requires configured mention')
            text = prefix + AUTOMATED_REPORT_LABEL + '\n' + text[len(prefix):]
        if dry_run:
            return None
        try:
            result = self._request('chat.postMessage', {'channel': channel, 'text': text,
                'parse': 'none', 'link_names': False, 'unfurl_links': False, 'unfurl_media': False}, use_report_user=use_user)
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
            # Synthetic subtype marks our own automated owner posts as
            # automation for existing consumers (Noa excludes all subtypes).
            # Preserve text/rp/author for read-only uncertain-post reconciliation.
            messages.extend(dict(m, subtype='riri_patrol')
                if m.get('user') == REPORT_OWNER_ID and has_patrol_marker(m.get('text'))
                else m for m in batch)
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
                   if ((m.get('bot_id') == self.expected_bot_id and m.get('user') == self.expected_user_id) or
                       (channel == USER_REPORT_CHANNEL and m.get('user') == REPORT_OWNER_ID and
                        not m.get('bot_id') and has_patrol_marker(m.get('text'))))
                   and isinstance(m.get('text'), str)
                   and marker in html.unescape(m['text']).splitlines()
                   and isinstance(m.get('ts'), str) and re.fullmatch(r'\d+\.\d+', m['ts'])]
        return matches[0]['ts'] if matches else None
