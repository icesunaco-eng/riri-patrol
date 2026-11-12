"""Synthetic-only tests for the trusted report mention boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.errors import AccountMismatch, SafetyError, UnsafeContent, UncertainPost
from riri.mentions import add_report_mention, assert_outgoing_safe, validate_mention_id
from riri.slack import FORBIDDEN_USER_IDS, SlackClient
from tests.test_api import Fake

ICE = 'U0TESTICE000'
SELF = 'UTESTRIRI'
REPORT = 'CTESTREPORT'
INSTRUCTION = 'CTESTINSTRUCTION'


class MentionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    def decorate(self, text, channel=REPORT):
        return add_report_mention(text, channel, REPORT, ICE, SELF)

    def validate(self, text, channel=REPORT):
        return assert_outgoing_safe(text, channel, REPORT, ICE, SELF)

    def client(self, *results):
        fake = Fake({'ok': True, 'bot_id': 'BTESTRIRI', 'user_id': SELF}, *results)
        client = SlackClient('xoxb-synthetic-test-only', 'BTESTRIRI', SELF, fake,
                             report_channel=REPORT, ice_mention_user_id=ICE)
        client.auth_test()
        return client, fake

    def test_configuration_missing_invalid_and_forbidden_fail_closed(self):
        for value in ['', None, 'Uabc', 'W123', 'U123 ', 'U123\n', '<@U123>', SELF, *FORBIDDEN_USER_IDS]:
            with self.subTest(value=value):
                with self.assertRaises(AccountMismatch):
                    Config(Path('.'), expected_bot_id='BTEST', expected_user_id=SELF,
                           ice_mention_user_id=value).validate_identity()

    def test_configuration_accepts_synthetic_id_without_enabling_noa(self):
        cfg = Config(Path('.'), expected_bot_id='BTEST', expected_user_id=SELF,
                     ice_mention_user_id=ICE)
        cfg.validate_identity()
        self.assertFalse(cfg.noa_wake_enabled)
        self.assertFalse(cfg.llm_enabled)

    def test_load_reads_only_literal_dotenv_and_default_is_empty(self):
        with TemporaryDirectory() as root:
            self.assertEqual(Config.load(root, environ={'ICE_MENTION_USER_ID': ICE}).ice_mention_user_id, '')
            (Path(root) / '.env').write_text('ICE_MENTION_USER_ID=' + ICE + '\n')
            self.assertEqual(Config.load(root, environ={'ICE_MENTION_USER_ID': 'UOTHER'}).ice_mention_user_id, ICE)

    def test_report_prefix_added_and_marker_stays_final_line(self):
        body = '完了\nrp:synthetic-marker'
        result = self.decorate(body)
        self.assertEqual(result, '<@' + ICE + '>\n' + body)
        self.assertEqual(result.splitlines()[-1], 'rp:synthetic-marker')
        self.assertEqual(self.validate(result), result)

    def test_instruction_destination_remains_undecorated(self):
        self.assertEqual(self.decorate('依頼文', INSTRUCTION), '依頼文')

    def test_source_mentions_even_configured_or_encoded_rejected(self):
        for body in ['<@' + ICE + '>', '<@UOTHER>', '&lt;@' + ICE + '&gt;',
                     '&#60;@UOTHER&#62;', '<!here>', '<#CTEST>', '@everyone']:
            with self.subTest(body=body):
                with self.assertRaises(UnsafeContent): self.decorate(body)

    def test_transport_only_accepts_exact_literal_first_line(self):
        for body in [' <@' + ICE + '>\nbody', '<@' + ICE + '> body',
                     '<@' + ICE + '>\r\nbody', '&lt;@' + ICE + '&gt;\nbody',
                     '<@UOTHER>\nbody', '<@' + ICE + '>\n<@' + ICE + '>\nbody',
                     '<@' + ICE + '>\n&lt;@UOTHER&gt;', '<@' + ICE + '>\n']:
            with self.subTest(body=body):
                with self.assertRaises(UnsafeContent): self.validate(body)

    def test_configured_mention_forbidden_on_other_destination(self):
        with self.assertRaises(UnsafeContent): self.validate(self.decorate('body'), INSTRUCTION)

    def test_legacy_body_is_unchanged_and_source_safety_remains(self):
        legacy = '以前の文面\nrp:legacy-marker'
        self.assertEqual(self.validate(legacy), legacy)
        with self.assertRaises(UnsafeContent): self.validate('&#60;!here&#62;')

    def test_configured_secret_not_hidden_by_prefix_exemption(self):
        with self.assertRaises(UnsafeContent):
            assert_outgoing_safe(self.decorate('body'), REPORT, REPORT, ICE, SELF, (ICE,))

    def test_post_payload_keeps_exact_marker_and_parse_none(self):
        client, fake = self.client({'ok': True, 'ts': '123.456'})
        text = self.decorate('done\nrp:test')
        self.assertEqual(client.post(REPORT, text), '123.456')
        payload = fake.calls[-1][2]['payload']
        self.assertEqual(payload['text'], text)
        self.assertEqual(payload['parse'], 'none')
        self.assertFalse(payload['link_names'])

    def test_transport_rejects_injected_body_before_request(self):
        client, fake = self.client()
        with self.assertRaises(UnsafeContent): client.post(REPORT, self.decorate('body') + '\n<@UOTHER>')
        self.assertEqual(len(fake.calls), 1)

    def test_auth_rejects_invalid_supplied_mention_before_network(self):
        fake = Fake()
        client = SlackClient('xoxb-synthetic-test-only', 'BTESTRIRI', SELF, fake,
                             report_channel=REPORT, ice_mention_user_id=SELF)
        with self.assertRaises(AccountMismatch): client.auth_test()
        self.assertEqual(fake.calls, [])

    def test_post_rejects_mutated_mention_or_destination(self):
        for attribute, value in [('ice_mention_user_id', 'UOTHER'), ('report_channel', 'COTHER')]:
            client, fake = self.client()
            setattr(client, attribute, value)
            with self.assertRaises(AccountMismatch): client.post(REPORT, 'legacy')
            self.assertEqual(len(fake.calls), 1)

    def test_post_uncertainty_is_not_retried(self):
        client, fake = self.client(TimeoutError())
        with self.assertRaises(UncertainPost): client.post(REPORT, self.decorate('body'))
        self.assertEqual(len(fake.calls), 2)

    def test_exact_marker_recovery_survives_new_prefix(self):
        text = self.decorate('done\nrp:match')
        client, _ = self.client({'ok': True, 'messages': [
            {'bot_id': 'BTESTRIRI', 'user': SELF, 'text': text + '-suffix', 'ts': '1.1'},
            {'bot_id': 'BTESTRIRI', 'user': SELF, 'text': text, 'ts': '2.2'}]})
        self.assertEqual(client.find_post(REPORT, 'rp:match', 0), '2.2')

    def test_empty_and_secret_bodies_are_rejected(self):
        for text in ['', ' ', 'secret_synthetic', '\x00', '\u202e']:
            with self.subTest(text=text):
                with self.assertRaises(UnsafeContent): self.decorate(text)

    def test_direct_missing_mention_is_not_inferred_from_identity(self):
        with self.assertRaises(AccountMismatch): add_report_mention('body', REPORT, REPORT, '', SELF)
