"""Owner-only dual route contracts; all receipts and credentials are synthetic."""
import copy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.delivery_policy import (CHANNELS, decorate_delivery, make_delivery_policy,
    validate_delivery_text)
from riri.errors import AccountMismatch, RemoteError, SafetyError, UncertainPost
from riri.slack import AUTOMATED_REPORT_LABEL, REPORT_OWNER_ID, SlackClient, UserTokenUnavailable
from riri.slack_payload import read_rich_blocks, rich_blocks, visible_text

BOT_TOKEN = 'xoxb' + '-dual-synthetic-only'
OWNER_TOKEN = 'xoxp' + '-dual-synthetic-only'
BOT = {'ok': True, 'user_id': 'UTESTRIRI', 'bot_id': 'BTEST',
    'team_id': 'TTEST', 'app_id': 'ATEST'}
OWNER = {'ok': True, 'user_id': REPORT_OWNER_ID, 'team_id': 'TTEST'}
MARKER = 'rp:' + 'a' * 24
BODY = '【完了報告】\n依頼ID：💛20990101-1\n結果：完了\n案件ページ：なし'


class ReceiptTransport:
    def __init__(self):
        self.calls = []
        self.owner = dict(OWNER)
        self.messages = []
        self.mutate = lambda result: result
        self.error = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if url.endswith('/auth.test'):
            return dict(BOT if kwargs['headers']['Authorization'] == 'Bearer ' + BOT_TOKEN else self.owner)
        if '/conversations.history?' in url:
            return {'ok': True, 'messages': copy.deepcopy(self.messages)}
        if url.endswith('/chat.postMessage'):
            if self.error:
                raise self.error
            payload = kwargs['payload']
            message = {'user': REPORT_OWNER_ID, 'ts': '20990101.000001', 'app_id': 'ATEST'}
            for key in ('text', 'blocks'):
                if key in payload:
                    message[key] = copy.deepcopy(payload[key])
            return self.mutate({'ok': True, 'channel': payload['channel'],
                'ts': message['ts'], 'message': message})
        raise AssertionError('No other API is authorized in this test')


class DualDeliveryTransportTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket', side_effect=AssertionError('No network in dual delivery tests'))
        guard.start()
        self.addCleanup(guard.stop)
        self.cfg = Config(Path('/synthetic-only'), expected_bot_id='BTEST',
            expected_user_id='UTESTRIRI', ice_mention_user_id='UTESTICE',
            requester_mention_ids={'うさ': 'UTESTUSA'},
            slack_token=BOT_TOKEN, slack_report_user_token=OWNER_TOKEN)

    def client(self, config=None, owner=None):
        config = config or self.cfg
        transport = ReceiptTransport()
        if owner is not None:
            transport.owner = owner
        client = SlackClient(config.slack_token, config.expected_bot_id,
            config.expected_user_id, transport, report_channel=config.report_channel,
            instruction_channel=config.instruction_channel,
            ice_mention_user_id=config.ice_mention_user_id,
            requester_mention_ids=config.requester_mention_ids,
            report_user_token=config.slack_report_user_token)
        client.auth_test()
        return client, transport

    def item(self, *, purpose='requester_report', requester='うさ', body=BODY,
             config=None, recipients=('あいす',)):
        cfg = config or self.cfg
        policy = make_delivery_policy(cfg, purpose, bundle_id='synthetic-bundle', canonical_text=body,
            requester=requester if purpose == 'requester_report' else '',
            request_id='💛20990101-1' if purpose == 'aisu_instruction' else '',
            recipients=recipients if purpose == 'aisu_instruction' else ())
        return policy, decorate_delivery(body, policy, cfg.expected_user_id, cfg.secrets) + '\n' + MARKER

    def wire(self, client, text, policy):
        return client.validate_delivery(CHANNELS[0], text, policy)

    def test_both_rooms_use_owner_and_official_aisu_mention(self):
        policy, text = self.item(purpose='aisu_instruction', recipients=('あいす', 'のあ'))
        client, transport = self.client()
        for channel in CHANNELS:
            self.assertEqual(client.post_delivery(channel, text, policy), '20990101.000001')
            payload = transport.calls[-1][2]['payload']
            self.assertEqual(payload['channel'], channel)
            self.assertTrue(payload['text'].startswith('<@UTESTICE>\n' + AUTOMATED_REPORT_LABEL + '\n'))
            self.assertEqual(payload['text'].splitlines()[-1], MARKER)
            self.assertEqual(transport.calls[-1][2]['headers']['Authorization'], 'Bearer ' + OWNER_TOKEN)
            self.assertFalse(payload['link_names'])
            self.assertEqual(payload['parse'], 'none')

    def test_requester_report_uses_requester_mapping_in_both_rooms(self):
        policy, text = self.item()
        client, transport = self.client()
        for channel in CHANNELS:
            client.post_delivery(channel, text, policy)
            posted = transport.calls[-1][2]['payload']['text']
            self.assertTrue(posted.startswith('<@UTESTUSA>\n'))
            self.assertNotIn('<@UTESTICE>', posted)

    def test_non_slack_requester_gets_only_canonical_text_name(self):
        policy, text = self.item(requester='みに')
        self.assertEqual(policy['mention_mode'], 'text')
        self.assertEqual(policy['mention_user_id'], '')
        client, transport = self.client()
        for channel in CHANNELS:
            client.post_delivery(channel, text, policy)
            posted = transport.calls[-1][2]['payload']['text']
            self.assertTrue(posted.startswith('@みに\n' + AUTOMATED_REPORT_LABEL + '\n'))
            self.assertNotIn('<@', posted)

    def test_missing_owner_token_never_posts_or_falls_back_to_bot(self):
        cfg = replace(self.cfg, slack_report_user_token='')
        policy, text = self.item(config=cfg)
        client, transport = self.client(cfg)
        initial = len(transport.calls)
        for channel in CHANNELS:
            with self.assertRaises(UserTokenUnavailable) as error:
                client.post_delivery(channel, text, policy)
            self.assertTrue(error.exception.not_sent)
        self.assertEqual(len(transport.calls), initial)

    def test_wrong_owner_or_workspace_never_posts(self):
        policy, text = self.item()
        for owner in (dict(OWNER, user_id='UTESTOTHER'), dict(OWNER, team_id='TOTHER'),
                      {'ok': False, 'error': 'token_revoked'}):
            client, transport = self.client(owner=owner)
            initial = len(transport.calls)
            with self.assertRaises(UserTokenUnavailable):
                client.post_delivery(CHANNELS[0], text, policy)
            self.assertEqual(len(transport.calls), initial)

    def test_owner_auth_from_known_other_app_blocks_only_new_route(self):
        policy, text = self.item()
        client, transport = self.client(owner=dict(OWNER, app_id='AOTHER'))
        self.assertTrue(client.report_user_verified)  # Legacy contract is unchanged.
        initial = len(transport.calls)
        with self.assertRaises(UserTokenUnavailable):
            client.post_delivery(CHANNELS[0], text, policy)
        self.assertEqual(len(transport.calls), initial)

    def test_dry_run_validates_without_posting(self):
        policy, text = self.item()
        client, transport = self.client()
        self.assertIsNone(client.post_delivery(CHANNELS[0], text, policy, dry_run=True))
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(client.last_delivery_receipt)

    def test_dry_run_without_owner_token_previews_both_rooms_without_actual_receipt(self):
        cfg = replace(self.cfg, slack_report_user_token='')
        policy, text = self.item(config=cfg)
        client, transport = self.client(cfg)
        initial = len(transport.calls)
        for channel in CHANNELS:
            self.assertIsNone(client.post_delivery(channel, text, policy, dry_run=True))
            self.assertIsNone(client.last_delivery_receipt)
        self.assertEqual(len(transport.calls), initial)

    def test_safe_receipt_is_reset_after_failure_or_practice(self):
        policy, text = self.item()
        client, transport = self.client()
        client.post_delivery(CHANNELS[0], text, policy)
        self.assertEqual(client.last_delivery_receipt, {
            'channel': CHANNELS[0], 'user_id': REPORT_OWNER_ID, 'ts': '20990101.000001',
            'canonical_hash': policy['canonical_hash'], 'mention_name': 'うさ',
            'mention_mode': 'official', 'mention_user_id': 'UTESTUSA'})
        client.post_delivery(CHANNELS[0], text, policy, dry_run=True)
        self.assertIsNone(client.last_delivery_receipt)
        client.post_delivery(CHANNELS[0], text, policy)
        with self.assertRaises(SafetyError):
            client.post_delivery('COTHER', text, policy)
        self.assertIsNone(client.last_delivery_receipt)

    def test_invalid_policies_make_no_transport_calls(self):
        original, text = self.item()
        mutations = [dict(version='dual-v3'), dict(purpose='arbitrary'),
            dict(channels=[CHANNELS[0], 'COTHER']), dict(requester='あいす'),
            dict(mention_user_id='UTESTOTHER'), dict(mention_name='あいす'),
            dict(mention_mode='text', mention_user_id=''),
            dict(canonical_hash='f' * 64), dict(recipients=['あいす']), dict(extra='data')]
        client, transport = self.client()
        initial = len(transport.calls)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                policy = dict(original, **mutation)
                with self.assertRaises((SafetyError, AccountMismatch)) as error:
                    client.post_delivery(CHANNELS[0], text, policy)
                self.assertTrue(error.exception.not_sent)
        self.assertEqual(len(transport.calls), initial)

    def test_other_destination_and_mutated_config_make_no_calls(self):
        policy, text = self.item()
        client, transport = self.client()
        initial = len(transport.calls)
        with self.assertRaises(SafetyError):
            client.post_delivery('COTHER', text, policy)
        client.requester_mention_ids['うさ'] = 'UTESTOTHER'
        with self.assertRaises(AccountMismatch):
            client.post_delivery(CHANNELS[0], text, policy)
        self.assertEqual(len(transport.calls), initial)

    def test_sources_cannot_inject_official_mentions_even_configured(self):
        for body in ('<@UTESTUSA>', '&lt;@UTESTUSA&gt;', '<!here>', '@everyone', 'ntn_test'):
            with self.subTest(body=body), self.assertRaises(SafetyError):
                self.item(body=body)

    def test_missing_or_mismatched_prefix_marker_body_is_rejected_before_send(self):
        policy, text = self.item()
        client, transport = self.client()
        initial = len(transport.calls)
        bad = [text.replace('UTESTUSA', 'UTESTICE'), text + 'extra',
            text.replace(BODY, BODY + '\nchanged'), text.replace(MARKER, 'no marker'),
            text + '\n<@UTESTUSA>', BODY + '\n' + MARKER]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(SafetyError):
                client.post_delivery(CHANNELS[0], value, policy)
        self.assertEqual(len(transport.calls), initial)

    def test_instruction_purpose_requires_canonical_aisu_recipient(self):
        for recipients in (('のあ',), ('@あいす',), ('あいす', 'unknown'), ('あいす', 'あいす')):
            with self.subTest(recipients=recipients), self.assertRaises(SafetyError):
                self.item(purpose='aisu_instruction', recipients=recipients)

    def test_unknown_requester_and_conflicting_or_duplicate_mapping_are_rejected(self):
        with self.assertRaises(SafetyError):
            self.item(requester='unknown')
        mappings = [{'あいす': 'UTESTOTHER'}, {'うさ': 'UTESTICE'},
            {'unknown': 'UTESTOTHER'}, {'うさ': REPORT_OWNER_ID}, {'うさ': 'UTESTRIRI'},
            {'うさ': 'not-an-id'}]
        for mapping in mappings:
            with self.subTest(mapping=mapping), self.assertRaises((SafetyError, AccountMismatch)):
                self.item(config=replace(self.cfg, requester_mention_ids=mapping))

    def test_offline_envelope_validator_does_not_add_label(self):
        policy, text = self.item()
        self.assertEqual(validate_delivery_text(text, policy, ice_mention_user_id='UTESTICE',
            expected_user_id='UTESTRIRI', requester_mention_ids={'うさ': 'UTESTUSA'}), text)
        self.assertNotIn(AUTOMATED_REPORT_LABEL, text)

    def test_success_requires_all_exact_receipt_fields(self):
        policy, text = self.item()
        mutations = [lambda r: {k: v for k, v in r.items() if k != 'channel'},
            lambda r: dict(r, channel='COTHER'), lambda r: dict(r, ts='bad'),
            lambda r: dict(r, message=None),
            lambda r: dict(r, message={k: v for k, v in r['message'].items() if k != 'user'}),
            lambda r: dict(r, message=dict(r['message'], user='UTESTRIRI')),
            lambda r: dict(r, message=dict(r['message'], ts='20990101.999999')),
            lambda r: dict(r, message=dict(r['message'], app_id='AOTHER')),
            lambda r: dict(r, message=dict(r['message'], channel='COTHER')),
            lambda r: dict(r, message=dict(r['message'], text=r['message']['text'] + 'extra')),
            lambda r: dict(r, message=dict(r['message'], text=r['message']['text'].replace('UTESTUSA', 'UTESTICE'))),
            lambda r: dict(r, warning='future_warning'),
            lambda r: dict(r, response_metadata={'warnings': ['message_truncated']})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                client, transport = self.client()
                transport.mutate = mutate
                with self.assertRaises(UncertainPost) as error:
                    client.post_delivery(CHANNELS[0], text, policy)
                self.assertTrue(error.exception.integrity_failure)
                self.assertEqual(len(transport.calls), 3)

    def test_long_official_and_text_mode_receipts_are_lossless(self):
        for requester in ('うさ', 'みに'):
            policy, text = self.item(requester=requester, body=BODY + '\n' + '長文 &amp; ' * 700)
            client, transport = self.client()
            for channel in CHANNELS:
                client.post_delivery(channel, text, policy)
                payload = transport.calls[-1][2]['payload']
                self.assertIn('blocks', payload)
                self.assertNotIn('text', payload)
                expected = self.wire(client, text, policy)
                self.assertEqual(read_rich_blocks(payload['blocks'], policy['mention_user_id']), visible_text(expected))

    def test_long_incomplete_blocks_are_uncertain(self):
        policy, text = self.item(body=BODY + '\n' + '長い本文' * 1200)
        client, transport = self.client()
        def truncate(result):
            result['message']['blocks'][0]['elements'][0]['elements'].pop()
            return result
        transport.mutate = truncate
        with self.assertRaises(UncertainPost) as error:
            client.post_delivery(CHANNELS[0], text, policy)
        self.assertTrue(error.exception.integrity_failure)

    def test_long_contradictory_fallback_is_uncertain(self):
        policy, text = self.item(body=BODY + '\n' + '長い本文' * 1200)
        client, transport = self.client()
        transport.mutate = lambda result: dict(result, message=dict(result['message'], text='<@UTESTOTHER>'))
        with self.assertRaises(UncertainPost):
            client.post_delivery(CHANNELS[0], text, policy)
        self.assertIsNone(client.last_delivery_receipt)

    def test_timeout_is_uncertain_and_not_retried(self):
        policy, text = self.item()
        client, transport = self.client()
        transport.error = TimeoutError()
        with self.assertRaises(UncertainPost):
            client.post_delivery(CHANNELS[0], text, policy)
        self.assertEqual(len(transport.calls), 3)

    def test_definitive_rejection_and_token_revocation_are_known_unsent(self):
        policy, text = self.item()
        for code, error_type in [('token_revoked', UserTokenUnavailable), ('channel_not_found', RemoteError)]:
            client, transport = self.client()
            transport.mutate = lambda result, code=code: {'ok': False, 'error': code}
            with self.assertRaises(error_type) as error:
                client.post_delivery(CHANNELS[0], text, policy)
            self.assertFalse(error.exception.uncertain)
            self.assertEqual(len(transport.calls), 3)

    def test_unknown_response_is_uncertain(self):
        policy, text = self.item()
        for result in ({}, {'ok': False, 'error': []}, {'ok': False, 'error': 'future_error'}):
            client, transport = self.client()
            transport.mutate = lambda original, result=result: result
            with self.assertRaises(UncertainPost):
                client.post_delivery(CHANNELS[0], text, policy)

    def test_reconciliation_uses_bot_read_and_exact_owner_receipt_after_token_removal(self):
        policy, text = self.item()
        client, transport = self.client(replace(self.cfg, slack_report_user_token=''))
        wire = self.wire(client, text, policy)
        for channel in CHANNELS:
            transport.messages = [{'user': REPORT_OWNER_ID, 'text': wire, 'ts': '20990101.000001', 'app_id': 'ATEST'}]
            self.assertEqual(client.find_delivery_post(channel, MARKER, 0, text, policy), '20990101.000001')
            self.assertEqual(client.last_found_owner_post, (channel, MARKER, '20990101.000001'))
            self.assertEqual(transport.calls[-1][2]['headers']['Authorization'], 'Bearer ' + BOT_TOKEN)

    def test_long_requester_receipt_is_reconciled_and_excluded_from_human_wake(self):
        from types import SimpleNamespace
        from riri.noa import NoaWake
        policy, text = self.item(body='@のあ\n' + BODY + '\n' + '長文' * 2200)
        client, transport = self.client(replace(self.cfg, slack_report_user_token=''))
        wire = self.wire(client, text, policy)
        transport.messages = [{'user': REPORT_OWNER_ID,
            'blocks': rich_blocks(wire, policy['mention_user_id']), 'ts': '20990101.000001'}]
        self.assertEqual(client.find_delivery_post(CHANNELS[0], MARKER, 0, text, policy), '20990101.000001')
        message = client.history(CHANNELS[0], 0)[0]
        self.assertEqual(message['subtype'], 'riri_patrol')
        message['text'] = wire
        wake = object.__new__(NoaWake)
        wake.p = SimpleNamespace(config=self.cfg)
        self.assertFalse(wake._eligible(message))

    def test_history_body_author_channel_app_or_marker_mismatch_is_not_accepted(self):
        policy, text = self.item()
        client, transport = self.client()
        wire = self.wire(client, text, policy)
        good = {'user': REPORT_OWNER_ID, 'text': wire, 'ts': '20990101.000001', 'app_id': 'ATEST'}
        for change in (dict(text=wire.replace('結果：完了', '結果：changed')), dict(channel='COTHER'), dict(app_id='AOTHER'), dict(ts='bad')):
            transport.messages = [dict(good, **change)]
            with self.subTest(change=change), self.assertRaises(UncertainPost):
                client.find_delivery_post(CHANNELS[0], MARKER, 0, text, policy)
        for change in (dict(user='UTESTRIRI'), dict(text=wire.replace(MARKER, MARKER + '-suffix'))):
            transport.messages = [dict(good, **change)]
            self.assertIsNone(client.find_delivery_post(CHANNELS[0], MARKER, 0, text, policy))

    def test_duplicate_exact_history_receipts_stay_uncertain(self):
        policy, text = self.item()
        client, transport = self.client()
        wire = self.wire(client, text, policy)
        transport.messages = [{'user': REPORT_OWNER_ID, 'text': wire, 'ts': stamp}
            for stamp in ('20990101.000001', '20990101.000002')]
        with self.assertRaises(UncertainPost):
            client.find_delivery_post(CHANNELS[0], MARKER, 0, text, policy)

    def test_config_loads_only_literal_optional_json(self):
        with TemporaryDirectory() as root:
            path = Path(root) / '.env'
            self.assertEqual(Config.load(root).requester_mention_ids, {})
            path.write_text('SLACK_REQUESTER_MENTION_IDS=\'{"うさ":"UTESTUSA"}\'\n')
            self.assertEqual(Config.load(root).requester_mention_ids, {'うさ': 'UTESTUSA'})
            for value in ('[]', '{"unknown":"UTESTUSA"}', '{"うさ":3}',
                          '{"うさ":"UTESTUSA","うさ":"UTESTOTHER"}', '$(arbitrary command)'):
                path.write_text('SLACK_REQUESTER_MENTION_IDS=' + value + '\n')
                with self.subTest(value=value), self.assertRaises(AccountMismatch):
                    Config.load(root)


if __name__ == '__main__':
    unittest.main()
