"""Verified bot client plus owner-authorized, visibly automated report posting."""
import html
import re
from urllib.parse import urlencode
from .errors import AccountMismatch, RemoteError, SafetyError, UncertainPost, StopRequested
from .slack_payload import needs_blocks, read_rich_blocks, read_known_rich_blocks, rich_blocks, visible_text

REPORT_OWNER_ID = 'U0C5V11S37X'
USER_REPORT_CHANNEL = 'C0CFL6A8QJH'
AUTOMATED_REPORT_LABEL = '【りり自動通知・本人承認の代理投稿／本文は本人の直接指示ではありません】'
FORBIDDEN_USER_IDS = frozenset({REPORT_OWNER_ID})
USER_POST_KINDS = frozenset({'instruction', 'instruction_relay', 'reports'})

class UserTokenUnavailable(RemoteError):
    """Known unsent, isolated failure; never fall back to a different identity."""
    def __init__(self):
        super().__init__('Report user token unavailable', uncertain=False)
        self.not_sent = True


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
                 report_channel="", ice_mention_user_id="", report_user_token="",
                 instruction_channel="C0CFM2HRA5V", requester_mention_ids=None):
        self.token, self.expected_bot_id, self.expected_user_id = token, expected_bot_id, expected_user_id
        self.transport, self.verified = transport, False
        self._identity = None
        self.report_user_verified = False
        self.expected_app_id = None
        self.report_user_token = report_user_token
        self.report_channel, self.ice_mention_user_id = report_channel, ice_mention_user_id
        self.instruction_channel = instruction_channel
        self.requester_mention_ids = dict(requester_mention_ids or {})
        self._delivery_identity = None
        self._delivery_owner_app_ok = False
        self.last_delivery_receipt = None

    def _request(self, endpoint, payload=None, query=None, *, use_report_user=False):
        token = self.token
        try:
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
                    if not self.report_user_verified:
                        raise UserTokenUnavailable()
                    if not isinstance(payload, dict) or payload.get('channel') != USER_REPORT_CHANNEL or query is not None:
                        raise SafetyError('Report user token destination rejected')
                    from .mentions import assert_outgoing_safe
                    text = payload.get('text')
                    if 'blocks' in payload:
                        if 'text' in payload:
                            raise SafetyError('Ambiguous report payload rejected')
                        text = read_rich_blocks(payload.get('blocks'), self.ice_mention_user_id)
                    assert_outgoing_safe(text, USER_REPORT_CHANNEL, self.report_channel,
                        self.ice_mention_user_id, self.expected_user_id, (self.token, self.report_user_token))
                    prefix = f'<@{self.ice_mention_user_id}>\n' + AUTOMATED_REPORT_LABEL + '\n'
                    if (not text.startswith(prefix) or
                            not re.fullmatch(r'rp:[0-9a-f]{24}', text.splitlines()[-1])):
                        raise SafetyError('Report user provenance rejected')
                else:
                    raise SafetyError('Report user token method rejected')
                token = self.report_user_token
        except (SafetyError, AccountMismatch) as exc:
            exc.not_sent = True
            raise
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
            if use_report_user and endpoint == 'chat.postMessage' and code in {'not_authed', 'invalid_auth', 'token_revoked', 'account_inactive', 'missing_scope', 'no_permission'}:
                self.report_user_verified = False
                raise UserTokenUnavailable()
            raise RemoteError('Slack API rejected request', uncertain=uncertain)
        return result

    def auth_test(self):
        self.verified, self._identity = False, None
        self.report_user_verified = False
        self.expected_app_id = None
        self._delivery_identity = None
        self._delivery_owner_app_ok = False
        self.last_delivery_receipt = None
        if not self.token.startswith('xoxb-') or not self.expected_bot_id or not self.expected_user_id or self.expected_user_id in FORBIDDEN_USER_IDS:
            raise AccountMismatch('Slack bot identity configuration rejected')
        if self.report_channel or self.ice_mention_user_id:
            from .mentions import validate_mention_id
            validate_mention_id(self.ice_mention_user_id, self.expected_user_id)
            self._channel(self.report_channel)
        who = self._request('auth.test')
        if (who.get('bot_id') != self.expected_bot_id or who.get('user_id') != self.expected_user_id or who.get('user_id') in FORBIDDEN_USER_IDS):
            raise AccountMismatch('Slack bot identity mismatch')
        self._identity = (self.token, self.expected_bot_id, self.expected_user_id, self.report_channel, self.ice_mention_user_id, self.report_user_token)
        self.verified = True
        self.expected_app_id = who.get('app_id')
        self._delivery_identity = (self.instruction_channel, dict(self.requester_mention_ids))
        if self.report_user_token:
            try:
                user = self._request('auth.test', use_report_user=True)
                if user.get('user_id') != REPORT_OWNER_ID:
                    raise AccountMismatch('Report user identity mismatch')
                if who.get('team_id') != user.get('team_id') or not user.get('team_id'):
                    raise AccountMismatch('Report user workspace mismatch')
                self.report_user_verified = True
                self._delivery_owner_app_ok = ('app_id' not in user or user['app_id'] == self.expected_app_id)
            except StopRequested:
                raise
            except (AccountMismatch, SafetyError, RemoteError, OSError, TimeoutError):
                # Bot work remains authorized. Do not expose exception bodies,
                # and never substitute bot identity for queued user work.
                self.report_user_verified = False
        return who

    def _guard(self):
        if not self.verified or self._identity != (self.token, self.expected_bot_id, self.expected_user_id, self.report_channel, self.ice_mention_user_id, self.report_user_token) or self.expected_user_id in FORBIDDEN_USER_IDS:
            raise AccountMismatch('Slack bot has not been verified')

    @staticmethod
    def _channel(channel):
        if not isinstance(channel, str) or not re.fullmatch(r'[CGD][A-Z0-9]+', channel):
            raise SafetyError('Invalid Slack channel')

    def wants_report_user(self, channel, kind):
        return bool(self.report_user_token and channel == self.report_channel and kind in USER_POST_KINDS)

    def validate_delivery(self, channel, text, policy):
        """Validate only the new persisted route; legacy post rules stay intact."""
        from .delivery_policy import validate_delivery_policy, delivery_content
        self._guard()
        if self._delivery_identity != (self.instruction_channel, self.requester_mention_ids):
            raise AccountMismatch('Dual delivery configuration changed after authentication')
        validate_delivery_policy(policy, instruction_channel=self.instruction_channel,
            report_channel=self.report_channel, ice_user_id=self.ice_mention_user_id,
            requester_mention_ids=self.requester_mention_ids,
            expected_user_id=self.expected_user_id, secrets=(self.token, self.report_user_token))
        if channel not in policy['channels']:
            raise SafetyError('Dual delivery destination rejected')
        prefix, body, marker = delivery_content(text, policy, self.expected_user_id,
            (self.token, self.report_user_token))
        wire = prefix + AUTOMATED_REPORT_LABEL + '\n' + body + '\n' + marker
        from .compose import INSTRUCTION_DELIVERY_LIMIT
        if len(wire) > INSTRUCTION_DELIVERY_LIMIT:
            raise SafetyError('Dual delivery envelope exceeded')
        return wire

    @staticmethod
    def _delivery_uncertain():
        error = UncertainPost('Owner delivery receipt is incomplete or differs; do not resend')
        error.integrity_failure = True
        return error

    def _delivery_receipt(self, message, channel, wire, policy, *, structured=False, ts=None):
        """Return a timestamp only for a complete, exact owner-authored message."""
        if not isinstance(message, dict) or message.get('user') != REPORT_OWNER_ID:
            return None
        if 'channel' in message and message['channel'] != channel:
            return None
        if 'app_id' in message and message['app_id'] != self.expected_app_id:
            return None
        metadata = message.get('response_metadata')
        if (message.get('warning') or message.get('warnings')
                or (isinstance(metadata, dict) and metadata.get('warnings'))):
            return None
        stamp = message.get('ts')
        if (not isinstance(stamp, str) or not re.fullmatch(r'\d+\.\d+', stamp)
                or (ts is not None and stamp != ts)):
            return None
        permitted = policy['mention_user_id'] if policy['mention_mode'] == 'official' else ''
        if structured or 'blocks' in message:
            if read_rich_blocks(message.get('blocks'), permitted) != visible_text(wire):
                return None
            # A second, contradictory fallback could notify a different person
            # even when the visible blocks match. Empty server fallback is fine
            # because the complete body/mention is proven by the rich elements.
            if 'text' in message and message['text'] not in ('', wire, visible_text(wire)):
                return None
        else:
            if message.get('text') != wire:
                return None
        return stamp

    def _record_delivery_receipt(self, channel, stamp, policy):
        self.last_delivery_receipt = {'channel': channel, 'user_id': REPORT_OWNER_ID,
            'ts': stamp, 'canonical_hash': policy['canonical_hash'],
            'mention_name': policy['mention_name'], 'mention_mode': policy['mention_mode'],
            'mention_user_id': policy['mention_user_id']}

    def post_delivery(self, channel, text, policy, *, dry_run=False):
        """Owner-only posting for the two explicitly approved v2 purposes.

        This path does not widen _request's legacy user-token API rights. No
        destination, method, mention, or identity can be selected by source text.
        """
        self.last_delivery_receipt = None
        try:
            wire = self.validate_delivery(channel, text, policy)
            permitted = policy['mention_user_id'] if policy['mention_mode'] == 'official' else ''
            structured = needs_blocks(wire)
            payload = {'channel': channel, 'parse': 'none', 'link_names': False,
                'unfurl_links': False, 'unfurl_media': False}
            if structured:
                payload['blocks'] = rich_blocks(wire, permitted)
            else:
                payload['text'] = wire
        except (SafetyError, AccountMismatch) as exc:
            exc.not_sent = True
            raise
        if dry_run:
            return None
        if (not self.report_user_token.startswith('xoxp-') or not self.report_user_verified
                or not self._delivery_owner_app_ok):
            raise UserTokenUnavailable()
        try:
            result = self.transport.request('POST', 'https://slack.com/api/chat.postMessage',
                headers={'Authorization': 'Bearer ' + self.report_user_token}, payload=payload, timeout=20)
        except StopRequested:
            raise
        except RemoteError as exc:
            if exc.uncertain:
                raise UncertainPost() from None
            raise
        except (OSError, TimeoutError):
            raise UncertainPost() from None
        if not isinstance(result, dict) or result.get('ok') is not True:
            code = result.get('error') if isinstance(result, dict) else None
            if isinstance(code, str) and code in {'not_authed', 'invalid_auth', 'token_revoked', 'account_inactive', 'missing_scope', 'no_permission'}:
                self.report_user_verified = False
                raise UserTokenUnavailable()
            if isinstance(code, str) and code in DEFINITE_POST_REJECTIONS:
                raise RemoteError('Slack owner delivery rejected', uncertain=False)
            raise UncertainPost()
        metadata = result.get('response_metadata')
        if (result.get('warning') or result.get('warnings')
                or (isinstance(metadata, dict) and metadata.get('warnings'))):
            raise self._delivery_uncertain()
        stamp = result.get('ts')
        if (result.get('channel') != channel or not isinstance(stamp, str)
                or not re.fullmatch(r'\d+\.\d+', stamp)
                or self._delivery_receipt(result.get('message'), channel, wire, policy,
                    structured=structured, ts=stamp) != stamp):
            raise self._delivery_uncertain()
        self._record_delivery_receipt(channel, stamp, policy)
        return stamp

    def find_delivery_post(self, channel, marker, oldest, expected_text, policy):
        """Read-only exact reconciliation, including after the owner token is removed."""
        self.last_delivery_receipt = None
        wire = self.validate_delivery(channel, expected_text, policy)
        if (not isinstance(marker, str) or not re.fullmatch(r'rp:[0-9a-f]{24}', marker)
                or wire.splitlines()[-1] != marker):
            raise SafetyError('Invalid exact owner delivery marker')
        self.last_found_owner_post = None
        found = []
        permitted = policy['mention_user_id'] if policy['mention_mode'] == 'official' else ''
        for message in self.history(channel, oldest):
            if message.get('user') != REPORT_OWNER_ID:
                continue
            # History is scoped by channel; optional message channel/app must agree.
            stamp = self._delivery_receipt(message, channel, wire, policy,
                structured=needs_blocks(wire))
            if stamp is not None:
                found.append(stamp)
                continue
            raw = message.get('text')
            block_text = read_rich_blocks(message.get('blocks'), permitted)
            if ((isinstance(raw, str) and marker in html.unescape(raw).splitlines())
                    or (isinstance(block_text, str) and marker in block_text.splitlines())):
                raise self._delivery_uncertain()
        if len(found) > 1:
            raise self._delivery_uncertain()
        if not found:
            return None
        self.last_found_owner_post = (channel, marker, found[0])
        self._record_delivery_receipt(channel, found[0], policy)
        return found[0]

    def post(self, channel, text, *, dry_run=False, kind='notice'):
        # Every failure in this block occurs before a transport call. Preserve
        # explicit not-sent evidence rather than treating it as an unknown write.
        try:
            self._guard()
            self._channel(channel)
            from .mentions import assert_outgoing_safe
            assert_outgoing_safe(text, channel, self.report_channel, self.ice_mention_user_id,
                                   self.expected_user_id, (self.token, self.report_user_token))
            use_user = self.wants_report_user(channel, kind)
            if use_user:
                if channel != USER_REPORT_CHANNEL:
                    raise SafetyError('Report user token destination rejected')
                if not self.report_user_verified:
                    raise UserTokenUnavailable()
                if not re.fullmatch(r'rp:[0-9a-f]{24}', text.splitlines()[-1]):
                    raise SafetyError('Automated report requires final patrol marker')
                prefix = f'<@{self.ice_mention_user_id}>\n'
                if not text.startswith(prefix):
                    raise SafetyError('Automated report requires configured mention')
                text = prefix + AUTOMATED_REPORT_LABEL + '\n' + text[len(prefix):]
            if kind in {'instruction', 'instruction_relay'}:
                from .compose import INSTRUCTION_DELIVERY_LIMIT
                prefix = f'<@{self.ice_mention_user_id}>\n'
                decoration = len(prefix) if channel == self.report_channel and text.startswith(prefix) else 0
                decoration += len(AUTOMATED_REPORT_LABEL) + 1 if use_user else 0
                if re.fullmatch(r'rp:[0-9a-f]{24}', text.splitlines()[-1]):
                    decoration += 28
                if decoration > 256 or len(text) > INSTRUCTION_DELIVERY_LIMIT:
                    raise SafetyError('Instruction delivery envelope exceeded')
            payload = {'channel': channel, 'parse': 'none', 'link_names': False,
                       'unfurl_links': False, 'unfurl_media': False}
            structured = needs_blocks(text)
            if structured:
                payload['blocks'] = rich_blocks(text, self.ice_mention_user_id if channel == self.report_channel else '')
            else:
                payload['text'] = text
        except (SafetyError, AccountMismatch) as exc:
            exc.not_sent = True
            raise
        if dry_run:
            return None
        try:
            result = self._request('chat.postMessage', payload, use_report_user=use_user)
        except StopRequested:
            # Keep transport's known-not-sent evidence (or its absence) intact.
            raise
        except RemoteError as error:
            if error.uncertain:
                raise UncertainPost() from None
            raise
        except (OSError, TimeoutError):
            raise UncertainPost() from None
        metadata = result.get('response_metadata')
        message = result.get('message')
        warnings = [result.get('warning'), result.get('warnings'),
                    metadata.get('warnings') if isinstance(metadata, dict) else None]
        if (any('truncat' in str(value).lower() or 'split' in str(value).lower() for value in warnings)
                or (structured and read_rich_blocks(message.get('blocks') if isinstance(message, dict) else None,
                    self.ice_mention_user_id if channel == self.report_channel else '') != visible_text(text))):
            error = UncertainPost('Slack delivery completeness is unconfirmed; do not resend')
            error.integrity_failure = True
            raise error
        ts = result.get('ts')
        if not isinstance(ts, str) or not re.fullmatch(r'\d+\.\d+', ts):
            raise UncertainPost('Slack success response lacked message timestamp')
        return ts

    def find_exact_post(self, channel, marker, oldest, expected_text, kind='notice'):
        """Reconcile long blocks by complete content; a marker alone is insufficient."""
        if not marker or '\n' in marker or '\r' in marker:
            raise SafetyError('Invalid exact message marker')
        prefix = f'<@{self.ice_mention_user_id}>\n'
        owner_text = (prefix + AUTOMATED_REPORT_LABEL + '\n' + expected_text[len(prefix):]
                      if kind in USER_POST_KINDS and expected_text.startswith(prefix) else expected_text)
        if not needs_blocks(expected_text) and not needs_blocks(owner_text):
            return self.find_post(channel, marker, oldest)
        self.last_found_owner_post = None
        found = []
        for message in self.history(channel, oldest):
            owner = channel == USER_REPORT_CHANNEL and message.get('user') == REPORT_OWNER_ID
            bot = message.get('bot_id') == self.expected_bot_id and message.get('user') == self.expected_user_id
            if not (bot or (owner and (not self.expected_app_id or not message.get('app_id') or
                                      message['app_id'] == self.expected_app_id))):
                continue
            actual = read_rich_blocks(message.get('blocks'), self.ice_mention_user_id if channel == self.report_channel else '')
            # Missing/malformed blocks never prove a long post was complete.
            if actual is None or marker not in actual.splitlines():
                continue
            expected = visible_text(owner_text if owner else expected_text)
            if actual != expected:
                raise UncertainPost('Long message marker matched without full content')
            ts = message.get('ts')
            if not isinstance(ts, str) or not re.fullmatch(r'\d+\.\d+', ts):
                raise UncertainPost()
            found.append((ts, owner))
        if len(found) != 1:
            return None
        ts, owner = found[0]
        if owner:
            self.last_found_owner_post = (channel, marker, ts)
        return ts

    def probe_report_history(self):
        """Read one message with the bot token; no pagination or mutation."""
        self._guard()
        self._channel(self.report_channel)
        result = self._request('conversations.history', query={'channel': self.report_channel, 'limit': 1})
        batch = result.get('messages')
        if not isinstance(batch, list) or any(not isinstance(m, dict) for m in batch):
            raise RemoteError('Incomplete Slack history probe')
        return True

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
                if m.get('user') == REPORT_OWNER_ID and (has_patrol_marker(m.get('text')) or
                    has_patrol_marker(read_known_rich_blocks(m.get('blocks'),
                        [self.ice_mention_user_id, *self.requester_mention_ids.values()])))
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
        self.last_found_owner_post = None
        if not marker or '\n' in marker or '\r' in marker:
            raise SafetyError('Invalid exact message marker')
        # Exhaust history BEFORE accepting a match: partial reads cannot resolve uncertainty.
        matches = [m for m in self.history(channel, oldest)
                   if ((m.get('bot_id') == self.expected_bot_id and m.get('user') == self.expected_user_id) or
                       (channel == USER_REPORT_CHANNEL and m.get('user') == REPORT_OWNER_ID and
                        (not self.expected_app_id or not m.get('app_id') or m['app_id'] == self.expected_app_id) and has_patrol_marker(m.get('text'))))
                   and isinstance(m.get('text'), str)
                   and marker in html.unescape(m['text']).splitlines()
                   and isinstance(m.get('ts'), str) and re.fullmatch(r'\d+\.\d+', m['ts'])]
        if matches and matches[0].get('user') == REPORT_OWNER_ID and channel == USER_REPORT_CHANNEL:
            self.last_found_owner_post = (channel, marker, matches[0]['ts'])
        return matches[0]['ts'] if matches else None
