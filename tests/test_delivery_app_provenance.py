"""Synthetic app provenance when Slack auth.test omits app_id.

Proof must come from the authenticated bot's own message, before a new owner
post. An owner-authored result is never allowed to establish its own authority.
"""
import copy
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from riri.config import Config
from riri.delivery_policy import CHANNELS, decorate_delivery, make_delivery_policy
from riri.errors import RemoteError, StopRequested, TimeBudgetExceeded, UncertainPost
from riri.slack import REPORT_OWNER_ID, SlackClient, UserTokenUnavailable

import test_dual_delivery_transport as fixtures


def bot_proof(app_id='ATEST'):
    return {'user': 'UTESTRIRI', 'bot_id': 'BTEST', 'app_id': app_id,
            'team': 'TTEST', 'ts': '20990100.000001', 'text': 'existing bot notice',
            'bot_profile': {'id': 'BTEST', 'app_id': app_id, 'team_id': 'TTEST',
                            'deleted': False, 'name': 'synthetic bot'}}


class AppProofTransport:
    def __init__(self, *, bot_app_id=None, owner=None, proofs=None):
        self.calls = []
        self.bot = {key: value for key, value in fixtures.BOT.items() if key != 'app_id'}
        if bot_app_id is not None:
            self.bot['app_id'] = bot_app_id
        self.owner = dict(fixtures.OWNER if owner is None else owner)
        self.proofs = [bot_proof()] if proofs is None else proofs
        self.target_messages = []
        self.more_proofs = False
        self.proof_error = None
        self.post_app_id = 'ATEST'

    def owner_message(self, text, stamp='20990101.000001'):
        # The posting bot/profile can differ from the authenticated reader bot.
        # Author and independently established application remain exact.
        return {'user': REPORT_OWNER_ID, 'bot_id': 'BOWNERPOST',
                'app_id': self.post_app_id, 'team': 'TTEST', 'ts': stamp, 'text': text,
                'bot_profile': {'id': 'BOWNERPOST', 'app_id': self.post_app_id,
                                'team_id': 'TTEST', 'deleted': False}}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
        if endpoint == 'auth.test':
            return copy.deepcopy(self.bot if kwargs['headers']['Authorization'] ==
                                 'Bearer ' + fixtures.BOT_TOKEN else self.owner)
        if endpoint == 'conversations.history':
            channel = parse_qs(urlsplit(url).query)['channel'][0]
            if channel == CHANNELS[1]:
                if self.proof_error:
                    raise self.proof_error
                result = {'ok': True, 'messages': copy.deepcopy(self.proofs)}
                if self.more_proofs:
                    result.update(has_more=True, response_metadata={'next_cursor': 'older-page'})
                return result
            return {'ok': True, 'messages': copy.deepcopy(self.target_messages)}
        if endpoint == 'chat.postMessage':
            payload = kwargs['payload']
            if 'blocks' in payload:
                raise AssertionError('App provenance tests use short canonical content')
            message = self.owner_message(payload['text'])
            return {'ok': True, 'channel': payload['channel'],
                    'ts': message['ts'], 'message': message}
        raise AssertionError('Only auth, bounded history and the requested post are permitted')

    def calls_for(self, endpoint):
        return [call for call in self.calls
                if urlsplit(call[1]).path.rsplit('/', 1)[-1] == endpoint]


class DeliveryAppProvenanceTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket', side_effect=AssertionError('No network in app provenance tests'))
        guard.start()
        self.addCleanup(guard.stop)
        self.config = Config(Path('/synthetic-app-provenance'),
            expected_bot_id='BTEST', expected_user_id='UTESTRIRI',
            ice_mention_user_id='UTESTICE', slack_token=fixtures.BOT_TOKEN,
            slack_report_user_token=fixtures.OWNER_TOKEN)
        self.policy = make_delivery_policy(self.config, 'aisu_instruction',
            bundle_id='instruction:💛20990101-1', canonical_text=fixtures.BODY,
            request_id='💛20990101-1', recipients=['あいす'])
        self.text = decorate_delivery(fixtures.BODY, self.policy,
                                     self.config.expected_user_id) + '\n' + fixtures.MARKER

    def client(self, transport, *, owner_token=True):
        config = self.config if owner_token else replace(self.config, slack_report_user_token='')
        client = SlackClient(config.slack_token, config.expected_bot_id,
            config.expected_user_id, transport, report_channel=config.report_channel,
            instruction_channel=config.instruction_channel,
            ice_mention_user_id=config.ice_mention_user_id,
            report_user_token=config.slack_report_user_token)
        client.auth_test()
        return client

    def assert_known_unsent(self, transport):
        client = self.client(transport)
        with self.assertRaises(UserTokenUnavailable) as error:
            client.post_delivery(CHANNELS[0], self.text, self.policy)
        self.assertTrue(error.exception.not_sent)
        self.assertEqual(transport.calls_for('chat.postMessage'), [])
        self.assertIsNone(client.last_delivery_receipt)

    def test_missing_auth_app_is_proven_before_legitimate_owner_receipt(self):
        transport = AppProofTransport()
        client = self.client(transport)
        self.assertIsNone(client.expected_app_id)
        self.assertEqual(client.post_delivery(CHANNELS[0], self.text, self.policy),
                         '20990101.000001')
        self.assertEqual(client.expected_app_id, 'ATEST')
        endpoints = [urlsplit(call[1]).path.rsplit('/', 1)[-1] for call in transport.calls]
        self.assertEqual(endpoints, ['auth.test', 'auth.test', 'conversations.history',
                                     'chat.postMessage'])
        proof_read = transport.calls_for('conversations.history')[0]
        query = parse_qs(urlsplit(proof_read[1]).query)
        self.assertEqual(proof_read[0], 'GET')
        self.assertEqual(query['channel'], [CHANNELS[1]])
        self.assertGreaterEqual(int(query['limit'][0]), 1)
        self.assertLessEqual(int(query['limit'][0]), 100)
        self.assertEqual(proof_read[2]['headers']['Authorization'], 'Bearer ' + fixtures.BOT_TOKEN)
        posted = transport.calls_for('chat.postMessage')[0]
        self.assertEqual(posted[2]['headers']['Authorization'], 'Bearer ' + fixtures.OWNER_TOKEN)
        self.assertEqual(client.last_delivery_receipt['user_id'], REPORT_OWNER_ID)

    def test_known_auth_app_needs_no_history_probe(self):
        transport = AppProofTransport(bot_app_id='ATEST', proofs=[])
        client = self.client(transport)
        self.assertEqual(client.post_delivery(CHANNELS[0], self.text, self.policy),
                         '20990101.000001')
        self.assertEqual(transport.calls_for('conversations.history'), [])

    def test_dry_run_with_missing_auth_app_does_not_read_or_post(self):
        transport = AppProofTransport(proofs=[])
        client = self.client(transport)
        before = len(transport.calls)
        self.assertIsNone(client.post_delivery(CHANNELS[0], self.text, self.policy, dry_run=True))
        self.assertEqual(len(transport.calls), before)
        self.assertIsNone(client.expected_app_id)
        self.assertIsNone(client.last_delivery_receipt)

    def test_dry_run_with_missing_auth_app_does_not_probe_or_create_receipt(self):
        transport = AppProofTransport(proofs=[])
        client = self.client(transport)
        self.assertIsNone(client.post_delivery(CHANNELS[0], self.text, self.policy, dry_run=True))
        self.assertEqual(transport.calls_for('conversations.history'), [])
        self.assertEqual(transport.calls_for('chat.postMessage'), [])
        self.assertIsNone(client.last_delivery_receipt)

    def test_proof_history_failure_is_known_unsent_and_never_posts(self):
        for error in (RemoteError('synthetic unavailable', uncertain=False), TimeoutError()):
            transport = AppProofTransport()
            transport.proof_error = error
            client = self.client(transport)
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(UserTokenUnavailable) as raised:
                    client.post_delivery(CHANNELS[0], self.text, self.policy)
                self.assertTrue(raised.exception.not_sent)
                self.assertFalse(raised.exception.uncertain)
                self.assertEqual(transport.calls_for('chat.postMessage'), [])
                self.assertIsNone(client.last_delivery_receipt)

    def test_missing_proof_fails_before_any_new_post(self):
        self.assert_known_unsent(AppProofTransport(proofs=[]))

    def test_stop_and_read_deadline_propagate_as_known_unsent_before_owner_post(self):
        for error_type in (StopRequested, TimeBudgetExceeded):
            with self.subTest(error_type=error_type.__name__):
                transport = AppProofTransport()
                transport.proof_error = error_type('synthetic interrupted provenance read')
                client = self.client(transport)
                with self.assertRaises(error_type) as failure:
                    client.post_delivery(CHANNELS[0], self.text, self.policy)
                self.assertTrue(failure.exception.not_sent)
                self.assertFalse(getattr(failure.exception, 'uncertain', False))
                self.assertEqual(len(transport.calls_for('conversations.history')), 1)
                self.assertEqual(transport.calls_for('chat.postMessage'), [])
                self.assertIsNone(client.last_delivery_receipt)

    def test_owner_message_cannot_prove_its_own_application(self):
        transport = AppProofTransport()
        transport.proofs = [transport.owner_message('existing owner notification')]
        self.assert_known_unsent(transport)

    def test_mismatched_bot_and_profile_identities_cannot_prove_application(self):
        changes = [('user', 'UTESTOTHER'), ('bot_id', 'BOTHER'), ('app_id', 'AOTHER'),
                   ('team', 'TOTHER'), ('profile_id', 'BOTHER'),
                   ('profile_app', 'AOTHER'), ('profile_team', 'TOTHER'),
                   ('profile_deleted', True)]
        for field, value in changes:
            proof = bot_proof()
            if field.startswith('profile_'):
                key = {'profile_id': 'id', 'profile_app': 'app_id',
                       'profile_team': 'team_id', 'profile_deleted': 'deleted'}[field]
                proof['bot_profile'][key] = value
            else:
                proof[field] = value
            with self.subTest(field=field):
                self.assert_known_unsent(AppProofTransport(proofs=[proof]))

    def test_missing_identity_fields_cannot_prove_application(self):
        for field in ('user', 'bot_id', 'app_id', 'team', 'bot_profile'):
            proof = bot_proof()
            proof.pop(field)
            with self.subTest(field=field):
                self.assert_known_unsent(AppProofTransport(proofs=[proof]))
        for field in ('id', 'app_id'):
            proof = bot_proof()
            proof['bot_profile'].pop(field)
            with self.subTest(profile_field=field):
                self.assert_known_unsent(AppProofTransport(proofs=[proof]))

    def test_optional_profile_team_may_be_absent_when_message_team_matches_auth(self):
        proof = bot_proof()
        proof['bot_profile'].pop('team_id')
        transport = AppProofTransport(proofs=[proof])
        client = self.client(transport)
        self.assertEqual(client.post_delivery(CHANNELS[0], self.text, self.policy),
                         '20990101.000001')

    def test_conflicting_apps_for_authenticated_bot_fail_before_post(self):
        self.assert_known_unsent(AppProofTransport(proofs=[bot_proof(), bot_proof('AOTHER')]))

    def test_only_one_bounded_proof_page_is_read_without_following_cursor(self):
        transport = AppProofTransport(proofs=[])
        transport.more_proofs = True
        self.assert_known_unsent(transport)
        self.assertEqual(len(transport.calls_for('conversations.history')), 1)

    def test_owner_auth_app_is_compared_after_independent_bot_proof(self):
        transport = AppProofTransport(owner=dict(fixtures.OWNER, app_id='ATEST'))
        client = self.client(transport)
        self.assertEqual(client.post_delivery(CHANNELS[0], self.text, self.policy),
                         '20990101.000001')
        self.assertEqual(len(transport.calls_for('conversations.history')), 1)

    def test_owner_auth_mismatch_or_bot_identity_never_posts(self):
        owners = [dict(fixtures.OWNER, app_id='AOTHER'),
                  dict(fixtures.OWNER, bot_id='BOWNERBOT'),
                  dict(fixtures.OWNER, team_id='TOTHER'),
                  dict(fixtures.OWNER, user_id='UTESTOTHER')]
        for owner in owners:
            with self.subTest(owner=sorted(owner)):
                self.assert_known_unsent(AppProofTransport(owner=owner))

    def test_proof_cached_for_second_room_but_reset_by_new_auth(self):
        transport = AppProofTransport()
        client = self.client(transport)
        for channel in CHANNELS:
            client.post_delivery(channel, self.text, self.policy)
        self.assertEqual(len(transport.calls_for('conversations.history')), 1)
        transport.proofs = []
        client.auth_test()
        before = len(transport.calls_for('chat.postMessage'))
        with self.assertRaises(UserTokenUnavailable):
            client.post_delivery(CHANNELS[0], self.text, self.policy)
        self.assertEqual(len(transport.calls_for('chat.postMessage')), before)

    def test_reconciliation_proves_app_with_bot_only_after_owner_token_removed(self):
        transport = AppProofTransport()
        client = self.client(transport, owner_token=False)
        wire = client.validate_delivery(CHANNELS[0], self.text, self.policy)
        transport.target_messages = [transport.owner_message(wire)]
        self.assertEqual(client.find_delivery_post(CHANNELS[0], fixtures.MARKER, '0',
                                                 self.text, self.policy), '20990101.000001')
        self.assertEqual(client.last_delivery_receipt['channel'], CHANNELS[0])
        self.assertEqual(transport.calls_for('chat.postMessage'), [])
        self.assertTrue(all(call[2]['headers']['Authorization'] == 'Bearer ' + fixtures.BOT_TOKEN
                            for call in transport.calls))

    def test_different_owner_receipt_app_stays_uncertain_after_valid_proof(self):
        transport = AppProofTransport()
        transport.post_app_id = 'AOTHER'
        client = self.client(transport)
        with self.assertRaises(UncertainPost):
            client.post_delivery(CHANNELS[0], self.text, self.policy)
        self.assertEqual(len(transport.calls_for('chat.postMessage')), 1)
        self.assertIsNone(client.last_delivery_receipt)


if __name__ == '__main__':
    unittest.main()
