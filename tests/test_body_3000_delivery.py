"""Body-only acceptance and single-message HTTP proof; no external writes."""
import copy
from dataclasses import replace
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import test_patrol_flow as flows
from test_instruction_3000 import unicode_request
from riri.compose import (INSTRUCTION_BODY_FIELDS, INSTRUCTION_DELIVERY_LIMIT,
                          instruction, instruction_budget)
from riri.errors import RemoteError, SafetyError, UnsafeContent
from riri.llm import validate_draft
from riri.parse import parse_requests
from riri.slack import AUTOMATED_REPORT_LABEL, SlackClient
from riri.slack_payload import needs_blocks, read_rich_blocks, rich_blocks, visible_text
from riri.notion import NotionClient, rich_text, text_chunks
from riri.mamu_bridge import FixedNotionStore
from riri.delivery_policy import REPORT_CHANNEL
from test_api import Fake, code_response


def body_request(body, recipient='@あいす', number=1):
    # The title consumes two body characters; all remaining source text is steps.
    block = flows.request(number=number, recipients=recipient, task='確認')
    source = block['code']['rich_text'][0]['text']['content']
    block['code']['rich_text'][0]['text']['content'] = source.replace('1. 読んで確かめる', body)
    return block


class EchoSlack:
    """Service double: receives ONE payload, echoes blocks, supports full history."""
    def __init__(self):
        self.posts = []
        self.calls = []
        self.uncertain = False
        self.reply_fault = None
        self.history_fault = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
        owner = kwargs['headers']['Authorization'].startswith('Bearer ' + 'xoxp' + '-')
        if endpoint == 'auth.test':
            return {'ok': True, 'user_id': 'UOWNER' if owner else 'UTEST', 'team_id': 'TTEST',
                    **({} if owner else {'bot_id': 'BTEST', 'app_id': 'ATEST'})}
        if endpoint == 'chat.postMessage':
            payload = kwargs['payload']
            ts = f'{len(self.posts) + 1}.000001'
            blocks = copy.deepcopy(payload.get('blocks'))
            message = {'channel': payload['channel'], 'ts': ts,
                       'user': 'UOWNER' if owner else 'UTEST',
                       'text': payload.get('text', ''),
                       **({'blocks': blocks} if blocks else {}),
                       **({} if owner else {'bot_id': 'BTEST'})}
            self.posts.append(message)
            if self.uncertain:
                self.uncertain = False
                raise RemoteError('synthetic timeout after send', uncertain=True)
            result = {'ok': True, 'channel': payload['channel'], 'ts': ts, 'message': copy.deepcopy(message)}
            if self.reply_fault == 'missing':
                result['message'].pop('blocks', None)
            elif self.reply_fault == 'changed':
                result['message']['blocks'][0]['elements'][0]['elements'][-1]['text'] += 'extra'
            elif self.reply_fault == 'warning':
                result['response_metadata'] = {'warnings': ['message_truncated']}
            return result
        if endpoint == 'conversations.history':
            channel = parse_qs(urlsplit(url).query)['channel'][0]
            messages = [copy.deepcopy(message) for message in self.posts if message['channel'] == channel]
            if self.history_fault == 'missing':
                for message in messages:
                    if 'blocks' in message:
                        message['text'] = read_rich_blocks(message.pop('blocks'), 'U0TESTICE000') or ''
            elif self.history_fault == 'changed':
                for message in messages:
                    if 'blocks' in message:
                        message['blocks'][0]['elements'][0]['elements'][-1]['text'] = (
                            message['blocks'][0]['elements'][0]['elements'][-1]['text'].replace('あ', '', 1))
            return {'ok': True, 'messages': messages, 'has_more': False}
        raise AssertionError(endpoint)


class BodyBudgets(unittest.TestCase):
    def test_notion_status_preserves_3000_emoji_across_bounded_text_elements(self):
        block = body_request('😀' * 2998)
        original = block['code']['rich_text'][0]['text']['content']
        parts = [{'type': 'text', 'text': {'content': part}, 'annotations': {'bold': True}}
                 for part in text_chunks(original)]
        block['code']['rich_text'] = copy.deepcopy(parts)
        req = parse_requests([block])[0]
        self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
        before = code_response('block', original)
        before['code']['rich_text'] = parts
        changed = original.replace('状態：未着手', '状態：対応中')
        http = Fake(before, code_response('block', changed), code_response('block', changed))
        NotionClient('synthetic-notion', http).set_status('block', original, '対応中')
        patched = http.calls[-2][2]['payload']['code']['rich_text']
        self.assertEqual(rich_text(patched), changed)
        self.assertTrue(all(len(p['text']['content'].encode('utf-16-le')) // 2 <= 2000 for p in patched))
        self.assertTrue(all(p['annotations']['bold'] for p in patched))

    def test_mamu_uses_one_code_block_with_bounded_unicode_elements(self):
        class Client:
            def _request(inner, *args, **kwargs):
                inner.payload = kwargs['payload']
        client = Client()
        store = FixedNotionStore(client)
        store.anchor = '11111111-2222-3333-4444-555555555555'
        source = '😀' * 3000 + '\ne\u0301\n'
        store.append(source)
        children = client.payload['children']
        self.assertEqual(len(children), 1)
        parts = children[0]['code']['rich_text']
        self.assertEqual(rich_text(parts), source)
        self.assertTrue(all(len(p['text']['content'].encode('utf-16-le')) // 2 <= 2000 for p in parts))

    def test_full_body_is_independent_of_variable_headers_up_to_1500(self):
        req = unicode_request(3000, 'あ')
        original = req.fields['やること']
        for header_target in (700, 1499, 1500):
            current = instruction_budget(req, req.recipients)
            req.requester += 'A' * (header_target - current['header_code_points'])
            budget = instruction_budget(req, req.recipients)
            self.assertEqual(budget['header_code_points'], header_target)
            self.assertEqual(budget['body_code_points'], 3000)
            self.assertIn(original, instruction(req, req.recipients))
        req.requester += 'A'
        with self.assertRaisesRegex(UnsafeContent, 'ヘッダー'):
            instruction(req, req.recipients)

    def test_default_none_and_metadata_never_reduce_standalone_body_3000(self):
        req = unicode_request(500, 'あ')
        for key in INSTRUCTION_BODY_FIELDS:
            req.fields[key] = ''
        req.fields['やること'] = 'あ' * 3000
        req.fields['期限・実行タイミング'] = '今日中' * 30
        req.fields['GitHubプロジェクト'] = 'github metadata' * 15
        self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
        self.assertIn('あ' * 3000, instruction(req, req.recipients))

    def test_body_can_span_all_five_original_fields(self):
        req = unicode_request(500, 'あ')
        for key in INSTRUCTION_BODY_FIELDS:
            req.fields[key] = 'あ' * 600
        self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
        for key in INSTRUCTION_BODY_FIELDS:
            req.fields[key] += 'あ'
            with self.assertRaisesRegex(UnsafeContent, '本文'):
                instruction(req, req.recipients)
            req.fields[key] = req.fields[key][:-1]

    def test_escape_newline_and_crlf_growth_have_separate_wire_budget(self):
        req = unicode_request(500, 'あ')
        for key in INSTRUCTION_BODY_FIELDS:
            req.fields[key] = ''
        for body in ('&' * 3000, '\n' * 3000, 'a\r\n' * 1000, '😀' * 3000, 'e\u0301' * 1500):
            req.fields['やること'] = body
            budget = instruction_budget(req, req.recipients)
            self.assertEqual(budget['body_code_points'], 3000)
            self.assertLessEqual(budget['canonical_code_points'], 16572)
            self.assertTrue(validate_draft(instruction(req, req.recipients), req, req.recipients))
        req.fields['やること'] += 'あ'
        with self.assertRaises(UnsafeContent):
            instruction(req, req.recipients)

    def test_rich_text_is_literal_and_only_permitted_user_element_exists(self):
        original = '<@UICE>\n' + '&amp;lt; <literal> 😀 e\u0301\n' * 300 + '\nrp:' + 'a' * 24
        blocks = rich_blocks(original, 'UICE')
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]['elements']), 1)
        self.assertEqual(read_rich_blocks(blocks, 'UICE'), visible_text(original))
        blocks[0]['elements'][0]['elements'].append({'type': 'user', 'user_id': 'UOTHER'})
        self.assertIsNone(read_rich_blocks(blocks, 'UICE'))


class BodyDelivery(unittest.TestCase):
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state

    def setUp(self):
        flows.FlowTests.setUp(self)
        for name, value in [('REPORT_OWNER_ID', 'UOWNER')]:
            p = patch('riri.slack.' + name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = replace(self.cfg, report_channel=REPORT_CHANNEL, slack_report_user_token='xoxp' + '-synthetic-only')
        self.notion.config = self.cfg
        self.http = EchoSlack()
        self.new_client()

    def new_client(self, token='xoxp' + '-synthetic-only'):
        self.slack = SlackClient('xoxb' + '-synthetic-only', 'BTEST', 'UTEST', self.http,
                                 report_channel=self.cfg.report_channel, ice_mention_user_id=self.cfg.ice_mention_user_id,
                                 instruction_channel=self.cfg.instruction_channel,
                                 requester_mention_ids=self.cfg.requester_mention_ids,
                                 report_user_token=token)

    def test_body_3000_is_single_full_http_message_per_destination(self):
        # Max escaping guarantees this exercises blocks, including GDM provenance.
        block = body_request('&' * 2998)
        req = parse_requests([block])[0]
        self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
        self.notion.blocks[self.cfg.requests_page] = [block]
        self.assertEqual(self.run_once(live=True)['instructions'], 1)
        state = self.state(True)
        canonical = state['requests'][req.id]['draft']
        expected = {channel: '<@U0TESTICE000>\n' + AUTOMATED_REPORT_LABEL + '\n' + canonical
                    for channel in (self.cfg.instruction_channel, self.cfg.report_channel)}
        for channel, text in expected.items():
            posts = [m for m in self.http.posts if m['channel'] == channel and m.get('blocks')]
            self.assertEqual(len(posts), 1)
            key = ('instruction:' if channel == self.cfg.instruction_channel else 'instruction-gdm:') + req.id
            exact = text + '\n' + state['outbox'][key]['marker']
            self.assertEqual(read_rich_blocks(posts[0]['blocks'], 'U0TESTICE000'), visible_text(exact))
            self.assertEqual(state['outbox'][key]['status'], 'posted')
            payloads = [kw['payload'] for _, url, kw in self.http.calls if url.endswith('chat.postMessage')
                        and kw['payload']['channel'] == channel and 'blocks' in kw['payload']]
            self.assertEqual(len(payloads), 1)
            self.assertNotIn('text', payloads[0])  # no long fallback text for Slack to split
            self.assertLess(len(exact), INSTRUCTION_DELIVERY_LIMIT)
        count = len(self.http.posts)
        self.new_client()
        self.clock.advance()
        self.run_once(live=True)
        self.assertEqual(len(self.http.posts), count)
        owner = next(m for m in self.slack.history(self.cfg.report_channel, 0) if m['user'] == 'UOWNER')
        self.assertEqual(owner['subtype'], 'riri_patrol')

    def test_maximum_body_escape_and_maximum_header_send_one_complete_message(self):
        req = unicode_request(500, 'あ')
        for key in INSTRUCTION_BODY_FIELDS:
            req.fields[key] = ''
        req.fields['やること'] = '&' * 3000
        req.requester += 'A' * (1500 - instruction_budget(req, req.recipients)['header_code_points'])
        canonical = instruction(req, req.recipients)
        self.assertEqual(len(canonical), 16572)
        self.slack.auth_test()
        marker = 'rp:' + 'c' * 24
        self.slack.post(self.cfg.instruction_channel, canonical + '\n' + marker, kind='instruction')
        self.assertEqual(len(self.http.posts), 1)
        self.assertEqual(read_rich_blocks(self.http.posts[0]['blocks'], ''),
                         visible_text(canonical) + '\n' + marker)

    def test_long_owner_history_reconciles_after_user_token_removed(self):
        self.slack.auth_test()
        marker = 'rp:' + 'd' * 24
        text = '<@U0TESTICE000>\n' + 'あ' * 4500 + '\n' + marker
        self.http.uncertain = True
        with self.assertRaises(RemoteError):
            self.slack.post(self.cfg.report_channel, text, kind='instruction_relay')
        self.new_client(token='')
        self.slack.auth_test()
        self.assertEqual(self.slack.find_exact_post(self.cfg.report_channel, marker, 0, text,
                                                  kind='instruction_relay'), '1.000001')

    def test_structured_user_provenance_rejects_extra_active_elements_before_send(self):
        self.slack.auth_test()
        blocks = rich_blocks('<@U0TESTICE000>\n' + AUTOMATED_REPORT_LABEL + '\n' +
                             'あ' * 4500 + '\nrp:' + 'e' * 24, 'U0TESTICE000')
        blocks[0]['elements'][0]['elements'].append({'type': 'user', 'user_id': 'UOTHER'})
        before = len(self.http.posts)
        with self.assertRaises(SafetyError) as caught:
            self.slack._request('chat.postMessage', {'channel': self.cfg.report_channel, 'blocks': blocks}, use_report_user=True)
        self.assertTrue(caught.exception.not_sent)
        self.assertEqual(len(self.http.posts), before)

    def test_valid_fallback_text_cannot_hide_untrusted_blocks(self):
        self.slack.auth_test()
        text = '<@U0TESTICE000>\n' + AUTOMATED_REPORT_LABEL + '\nvalid\nrp:' + 'f' * 24
        with self.assertRaises(SafetyError):
            self.slack._request('chat.postMessage', {'channel': self.cfg.report_channel, 'text': text,
                'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': '<@UOTHER>'}}]},
                use_report_user=True)
        self.assertEqual(self.http.posts, [])

    def test_unicode_and_newlines_3000_survive_prepare_and_transport(self):
        # One code point title? The fixture uses two plus these 2998 raw code points.
        for number, steps in enumerate(('😀' * 2998, 'e\u0301' * 1499, 'a\n' * 1498 + 'aa'), 1):
            with self.subTest(steps=steps[:10]):
                block = body_request(steps, number=number)
                req = parse_requests([block])[0]
                self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
                canonical = instruction(req, req.recipients)
                self.notion.blocks[self.cfg.requests_page] = [block]
                self.assertEqual(self.run_once(live=True)['instructions'], 1)
                item = self.state(True)['outbox']['instruction:' + req.id]
                self.assertEqual(item['status'], 'posted')
                payload = self.http.posts[-2]  # instruction, then its existing GDM relay
                exact = '<@U0TESTICE000>\n' + AUTOMATED_REPORT_LABEL + '\n' + canonical + '\n' + item['marker']
                if payload.get('blocks'):
                    self.assertEqual(read_rich_blocks(payload['blocks'], 'U0TESTICE000'), visible_text(exact))
                else:
                    self.assertEqual(payload['text'], exact)
                self.clock.advance()

    def test_warning_or_changed_reply_is_held_without_marker_only_ack_or_resend(self):
        self.notion.blocks[self.cfg.requests_page] = [body_request('&' * 2998, '@のあ')]
        self.http.reply_fault = 'warning'
        self.run_once(live=True)
        rid = parse_requests(self.notion.blocks[self.cfg.requests_page])[0].id
        item = self.state(True)['outbox']['instruction:' + rid]
        self.assertEqual(item['status'], 'held')
        self.assertTrue(item['delivery_integrity_unconfirmed'])
        before = sum(m['channel'] == self.cfg.instruction_channel for m in self.http.posts)
        self.http.reply_fault = None
        self.new_client()
        self.clock.advance()
        self.run_once(live=True)
        self.assertEqual(sum(m['channel'] == self.cfg.instruction_channel for m in self.http.posts), before)

    def test_uncertain_long_post_reconciles_only_with_full_blocks(self):
        block = body_request('&' * 2998, '@のあ')
        self.notion.blocks[self.cfg.requests_page] = [block]
        self.http.uncertain = True
        self.run_once(live=True)
        rid = parse_requests([block])[0].id
        self.assertEqual(self.state(True)['outbox']['instruction:' + rid]['status'], 'posted')
        self.assertEqual(sum(m['channel'] == self.cfg.instruction_channel for m in self.http.posts), 1)

    def test_uncertain_marker_with_missing_or_changed_body_cannot_ack(self):
        self.notion.blocks[self.cfg.requests_page] = [body_request('あ' * 2998, '@のあ')]
        # Force the conservative structured path even for this plain body.
        req = parse_requests(self.notion.blocks[self.cfg.requests_page])[0]
        source = self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content']
        self.notion.blocks[self.cfg.requests_page][0]['code']['rich_text'][0]['text']['content'] = source.replace(
            '依頼した子：うさ', '依頼した子：' + 'う' * 700)
        self.http.uncertain = True
        self.http.history_fault = 'changed'
        self.run_once(live=True)
        self.assertEqual(self.state(True)['outbox']['instruction:' + req.id]['status'], 'held')
        self.assertEqual(sum(m['channel'] == self.cfg.instruction_channel for m in self.http.posts), 1)

    def test_structured_reply_missing_or_changed_is_unconfirmed(self):
        self.slack.auth_test()
        text = 'あ' * 4500 + '\nrp:' + 'b' * 24
        for fault in ('missing', 'changed'):
            self.http.reply_fault = fault
            with self.subTest(fault=fault), self.assertRaises(RemoteError) as caught:
                self.slack.post(self.cfg.instruction_channel, text, kind='instruction')
            self.assertTrue(caught.exception.integrity_failure)

    def test_envelope_overflow_is_known_unsent(self):
        self.slack.auth_test()
        before = len(self.http.posts)
        with self.assertRaises(SafetyError) as caught:
            self.slack.post(self.cfg.instruction_channel, 'あ' * (INSTRUCTION_DELIVERY_LIMIT + 1), kind='instruction')
        self.assertTrue(caught.exception.not_sent)
        self.assertEqual(len(self.http.posts), before)
