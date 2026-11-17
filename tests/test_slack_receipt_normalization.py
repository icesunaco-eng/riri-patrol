"""Desensitized Slack response shapes; never read captured requests or network."""
import copy
import unittest

from riri.slack_receipt import message_content_matches
from riri.slack_payload import rich_blocks, visible_text

USER = 'UTESTICE'
MARKER = 'rp:' + 'b' * 24
EXPECTED = ('<@UTESTICE>\n自動通知\n依頼ID：💛20990101-1\n確認：💜🩷\n'
    'よろしくお願いします🍚🔥 e\u0301 &amp; B\n' + MARKER)
EMOJI = {'💛': ('yellow_heart', '1f49b'), '💜': ('purple_heart', '1f49c'),
    '🩷': ('pink_heart', '1fa77'), '🍚': ('rice', '1f35a'), '🔥': ('fire', '1f525'),
    '🧡': ('orange_heart', '1f9e1'), '❤️': ('heart', '2764-fe0f')}


def rendered_message(expected=EXPECTED):
    """Synthetic equivalent of observed user/text/emoji(name+unicode) shape."""
    prefix = '<@' + USER + '>'
    body = visible_text(expected)[len(prefix):]
    elements = [{'type': 'user', 'user_id': USER}]
    pending = ''
    position = 0
    while position < len(body):
        emoji = next((value for value in EMOJI if body.startswith(value, position)), None)
        if emoji is None:
            pending += body[position]
            position += 1
            continue
        if pending:
            elements.append({'type': 'text', 'text': pending})
            pending = ''
        name, unicode = EMOJI[emoji]
        elements.append({'type': 'emoji', 'name': name, 'unicode': unicode})
        position += len(emoji)
    if pending:
        elements.append({'type': 'text', 'text': pending})
    text = expected
    for value, (name, _) in EMOJI.items():
        text = text.replace(value, ':' + name + ':')
    return {'text': text, 'blocks': [{'type': 'rich_text', 'block_id': 'synthetic',
        'elements': [{'type': 'rich_text_section', 'elements': elements}]}]}


def elements(message):
    return message['blocks'][0]['elements'][0]['elements']


class SlackReceiptNormalizationTests(unittest.TestCase):
    def match(self, message, expected=EXPECTED, structured=False, permitted_user=USER):
        return message_content_matches(message, expected, permitted_user, structured)

    def test_observed_short_emoji_shape_preserves_complete_body_target_and_marker(self):
        self.assertTrue(self.match(rendered_message()))

    def test_orange_heart_request_and_colon_fallback_are_equivalent(self):
        expected = EXPECTED.replace('💛20990101-1', '🧡20990101-2')
        self.assertTrue(self.match(rendered_message(expected), expected))

    def test_literal_long_rich_payload_remains_lossless(self):
        expected = EXPECTED + '\n' + '長文 e\u0301 &amp; ' * 500 + '\n' + MARKER
        message = {'blocks': rich_blocks(expected, USER)}
        self.assertTrue(self.match(message, expected, structured=True))
        message['text'] = ''
        self.assertTrue(self.match(message, expected, structured=True))
        message['text'] = expected + 'extra'
        self.assertFalse(self.match(message, expected, structured=True))

    def test_text_only_exact_short_is_allowed_but_aliases_without_mapping_are_not(self):
        self.assertTrue(self.match({'text': EXPECTED}))
        message = rendered_message()
        message.pop('blocks')
        self.assertFalse(self.match(message))
        self.assertFalse(self.match({'text': EXPECTED}, structured=True))
        self.assertFalse(self.match({}))
        self.assertFalse(self.match({'text': ''}))

    def test_text_only_escaped_prefix_is_literal_and_not_an_official_mention(self):
        escaped = EXPECTED.replace('<@UTESTICE>', '&lt;@UTESTICE&gt;')
        self.assertFalse(self.match({'text': escaped}))
        double_escaped = EXPECTED.replace('<@UTESTICE>', '&amp;lt;@UTESTICE&amp;gt;')
        self.assertFalse(self.match({'text': double_escaped}))

    def test_text_only_entity_spelling_must_remain_exact_without_rich_proof(self):
        expected = '<@UTESTICE>\nliteral &amp;lt;tag&amp;gt; &amp;amp;\n' + MARKER
        self.assertTrue(self.match({'text': expected}, expected))
        self.assertFalse(self.match({'text': visible_text(expected)}, expected))
        self.assertFalse(self.match({'text': visible_text(visible_text(expected))}, expected))

    def test_both_available_representations_must_match(self):
        message = rendered_message()
        message['text'] += 'extra'
        self.assertFalse(self.match(message))
        message = rendered_message()
        elements(message)[1]['text'] += 'extra'
        self.assertFalse(self.match(message))
        message = rendered_message()
        message['text'] = 3
        self.assertFalse(self.match(message))

    def test_no_whitespace_combining_or_nfc_normalization(self):
        for old, new in [(' e\u0301 ', ' é '), (' e\u0301 ', ' e '), ('\n確認', '\n 確認'),
                         ('自動通知\n', '自動通知\r\n'), (' B\n', ' B \n')]:
            modified = EXPECTED.replace(old, new)
            self.assertNotEqual(modified, EXPECTED)
            self.assertFalse(self.match(rendered_message(modified)))

    def test_body_or_marker_change_is_never_hidden_by_normalization(self):
        for value in (EXPECTED + 'extra', EXPECTED.replace('確認', '変更'),
                      EXPECTED.replace(MARKER, MARKER[:-1]), EXPECTED.replace(MARKER, MARKER + '-suffix')):
            self.assertFalse(self.match(rendered_message(value)))

    def test_wrong_extra_or_text_disguised_user_is_rejected(self):
        for element in ({'type': 'user', 'user_id': 'UTESTOTHER'},
                        {'type': 'user', 'user_id': USER},
                        {'type': 'channel', 'channel_id': 'CTEST'},
                        {'type': 'broadcast', 'range': 'here'},
                        {'type': 'button', 'text': 'click'},
                        {'type': 'date', 'timestamp': 1}):
            message = rendered_message()
            elements(message).append(element)
            self.assertFalse(self.match(message))
        message = rendered_message()
        elements(message)[0]['user_id'] = 'UTESTOTHER'
        self.assertFalse(self.match(message))
        message = rendered_message()
        elements(message)[0] = {'type': 'text', 'text': '<@UTESTICE>'}
        self.assertFalse(self.match(message))

    def test_emoji_unicode_and_name_must_be_valid_and_unambiguous(self):
        invalid = ['', '61', '202e', 'd800', '110000', '1f35a-200d', 'fe0f',
            '1f35a-0301', '1f35a\n', 'not-unicode', '1f35a-1f35a']
        for unicode in invalid:
            message = rendered_message()
            emoji = next(e for e in elements(message) if e['type'] == 'emoji')
            emoji['unicode'] = unicode
            self.assertFalse(self.match(message), unicode)
        for name in ('', ':rice:', 'rice\n', 'other name'):
            message = rendered_message()
            next(e for e in elements(message) if e['type'] == 'emoji')['name'] = name
            self.assertFalse(self.match(message), name)
        message = rendered_message()
        emoji = [e for e in elements(message) if e['type'] == 'emoji']
        emoji[1]['name'] = emoji[0]['name']
        self.assertFalse(self.match(message))

    def test_no_unknown_element_fields_or_custom_emoji_url(self):
        for field, value in [('url', 'https://example.invalid/emoji'), ('style', {'bold': True}), ('extra', True)]:
            message = rendered_message()
            next(e for e in elements(message) if e['type'] == 'emoji')[field] = value
            self.assertFalse(self.match(message))
        message = rendered_message()
        elements(message)[1]['style'] = {'bold': True}
        self.assertFalse(self.match(message))

    def test_literal_colon_name_is_preserved_when_same_name_has_a_real_emoji_elsewhere(self):
        expected = '<@UTESTICE>\nliteral :rice: and 🍚\n' + MARKER
        self.assertTrue(self.match(rendered_message(expected), expected))

    def test_variation_selector_is_preserved(self):
        expected = '<@UTESTICE>\n❤️\n' + MARKER
        self.assertTrue(self.match(rendered_message(expected), expected))
        message = rendered_message(expected)
        next(e for e in elements(message) if e['type'] == 'emoji')['unicode'] = '2764'
        self.assertFalse(self.match(message, expected))

    def test_text_mode_has_no_official_user_element(self):
        expected = '@みに\n本文 🍚\n' + MARKER
        message = {'text': expected, 'blocks': rich_blocks(expected, '')}
        self.assertTrue(self.match(message, expected, permitted_user=''))
        elements(message).insert(0, {'type': 'user', 'user_id': USER})
        self.assertFalse(self.match(message, expected, permitted_user=''))

    def test_exact_passive_url_link_is_allowed_but_no_different_target_or_label(self):
        url = 'https://example.invalid/path?a=1&b=2'
        expected = '<@UTESTICE>\nURL：' + url.replace('&', '&amp;') + '\n' + MARKER
        message = {'text': '<@UTESTICE>\nURL：<' + url + '|' + url + '>\n' + MARKER,
            'blocks': [{'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [
                {'type': 'user', 'user_id': USER}, {'type': 'text', 'text': '\nURL：'},
                {'type': 'link', 'url': url, 'text': url}, {'type': 'text', 'text': '\n' + MARKER}]}]}]}
        self.assertTrue(self.match(message, expected))
        for change in ({'url': 'https://other.invalid/'}, {'text': 'click here'}, {'unsafe': True}):
            changed = copy.deepcopy(message)
            elements(changed)[2].update(change)
            self.assertFalse(self.match(changed, expected))

    def test_unknown_or_missing_blocks_never_claim_a_complete_long_delivery(self):
        for blocks in (None, [], [{'type': 'actions', 'elements': []}],
                       [{'type': 'rich_text', 'elements': []}]):
            self.assertFalse(self.match({'text': EXPECTED, 'blocks': blocks}))
        message = rendered_message()
        message['blocks'].append(copy.deepcopy(message['blocks'][0]))
        self.assertFalse(self.match(message))

    def test_malformed_elements_are_rejected_without_exception(self):
        for value in (None, [], 'text', 4, {'type': 'text'}, {'type': 'text', 'text': []},
                      {'type': 'emoji', 'name': [], 'unicode': '1f35a'},
                      {'type': 'emoji', 'name': 'rice', 'unicode': None}):
            message = rendered_message()
            elements(message).append(value)
            self.assertFalse(self.match(message))
        for value in (None, 'sections', {}, [None]):
            message = rendered_message()
            message['blocks'][0]['elements'] = value
            self.assertFalse(self.match(message))

    def test_unicode_field_cannot_relabel_ascii_or_invisible_content_as_emoji(self):
        for glyph, encoded in [('a', '0061'), ('\u202e', '202e'), ('\u0301', '0301'),
                               ('\u200d', '200d'), ('\ufe0f', 'fe0f')]:
            expected = '<@UTESTICE>\n' + glyph + '\n' + MARKER
            message = {'text': '<@UTESTICE>\n:fake:\n' + MARKER,
                'blocks': [{'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [
                    {'type': 'user', 'user_id': USER}, {'type': 'text', 'text': '\n'},
                    {'type': 'emoji', 'name': 'fake', 'unicode': encoded},
                    {'type': 'text', 'text': '\n' + MARKER}]}]}]}
            self.assertFalse(self.match(message, expected), encoded)

    def test_hidden_active_or_different_mention_in_fallback_never_matches(self):
        for token in ('<@UTESTOTHER>', '<!here>', '<#CTEST>', '&lt;@UTESTOTHER&gt;'):
            message = rendered_message()
            message['text'] += token
            self.assertFalse(self.match(message))
        self.assertFalse(self.match({'text': '<@UTESTOTHER>\nbody\n' + MARKER},
            '<@UTESTOTHER>\nbody\n' + MARKER, permitted_user=''))

    def test_actual_slack_client_post_delivery_accepts_synthetic_server_emoji_response(self):
        from pathlib import Path
        from test_dual_delivery_transport import BOT_TOKEN, OWNER_TOKEN, ReceiptTransport
        from riri.config import Config
        from riri.delivery_policy import CHANNELS, decorate_delivery, make_delivery_policy
        from riri.slack import SlackClient
        from unittest.mock import patch
        cfg = Config(Path('/synthetic-only'), expected_user_id='UTESTRIRI', expected_bot_id='BTEST',
            ice_mention_user_id=USER, slack_token=BOT_TOKEN, slack_report_user_token=OWNER_TOKEN)
        canonical = '依頼ID：💛20990101-1\n確認：💜🩷\nよろしくお願いします🍚🔥'
        policy = make_delivery_policy(cfg, 'aisu_instruction', bundle_id='synthetic',
            canonical_text=canonical, request_id='💛20990101-1', recipients=['あいす'])
        queued = decorate_delivery(canonical, policy, cfg.expected_user_id) + '\n' + MARKER
        transport = ReceiptTransport()
        def render(result):
            server = rendered_message(result['message']['text'])
            result['message'].update(server)
            return result
        transport.mutate = render
        with patch('socket.socket', side_effect=AssertionError('No network')):
            client = SlackClient(cfg.slack_token, cfg.expected_bot_id, cfg.expected_user_id,
                transport, report_channel=cfg.report_channel, ice_mention_user_id=USER,
                report_user_token=cfg.slack_report_user_token)
            client.auth_test()
            for channel in CHANNELS:
                self.assertEqual(client.post_delivery(channel, queued, policy), '20990101.000001')
                self.assertEqual(client.last_delivery_receipt['canonical_hash'], policy['canonical_hash'])


if __name__ == '__main__':
    unittest.main()
