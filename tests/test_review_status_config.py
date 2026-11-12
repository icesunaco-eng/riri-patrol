"""Offline review fixes for cleanup visibility, safe references and dotenv data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from riri.config import Config, read_env
from riri.errors import AccountMismatch
from riri.resolve import held_items
from riri.state import initial_state
from riri.status import PRACTICE_STATUSES, practice_cleanup_items


class ReviewStatusTests(unittest.TestCase):
    def test_cleanup_includes_every_local_state_until_source_observed_complete(self):
        state = initial_state('practice')
        for index, status in enumerate(PRACTICE_STATUSES, 1):
            state['requests'][f'💛20990101-{index}'] = {
                'status': status, 'source_status_observed': '未着手',
                'source_status_observed_at': '2099-01-01T00:00:00+00:00'}
        before = deepcopy(state)
        items = practice_cleanup_items(state)
        self.assertEqual(len(items), len(PRACTICE_STATUSES))
        self.assertEqual({item['practice_status'] for item in items}, set(PRACTICE_STATUSES.values()))
        self.assertTrue(all(item['needs_manual_verification'] for item in items))
        self.assertEqual(state, before)
        for info in state['requests'].values():
            info['source_status_observed'] = '完了'
        self.assertEqual(practice_cleanup_items(state), [])

    def test_cleanup_prefers_latest_observation_and_keeps_missing_or_unknown_source(self):
        state = initial_state('practice')
        for index, observed in enumerate(('対応中', '見つからない', '未確認', 'private source'), 1):
            state['requests'][f'💛20990101-{index}'] = {
                'status': 'held', 'source_status_observed': observed, 'snapshot': {'status': '完了'}}
        items = practice_cleanup_items(state)
        self.assertEqual([item['last_observed_notion_status'] for item in items],
                         ['対応中', '見つからない', '未確認', '不明（要確認）'])
        self.assertNotIn('private source', json.dumps(items))

    def test_cleanup_unknown_local_state_and_old_snapshot_are_safe(self):
        state = initial_state('practice')
        for index, status in enumerate(('secret_private', None, ['private']), 1):
            state['requests'][f'💛20990101-{index}'] = {'status': status, 'snapshot': {'status': '未着手'}}
        items = practice_cleanup_items(state)
        self.assertTrue(all(item['practice_status'] == '不明（要確認）' for item in items))
        self.assertTrue(all(item['observed_at'] is None for item in items))
        self.assertNotIn('private', json.dumps(items))

    def test_held_report_reference_has_request_and_reporter_without_body(self):
        state = initial_state('practice')
        state['quarantined_reports']['block:digest'] = {
            'status': 'held', 'request_id': '💛20990101-1', 'reporter': 'あいす',
            'text': 'private source', 'summary': 'private summary'}
        before = deepcopy(state)
        item = held_items(state)[0]
        self.assertEqual(item['request'], '依頼ID：💛20990101-1／報告した子：あいす')
        self.assertEqual(item['allowed_outcomes'], ['source_edit_required'])
        self.assertNotIn('private', json.dumps(item))
        self.assertEqual(state, before)

    def test_held_report_reference_rejects_unsafe_names_and_handles_old_records(self):
        state = initial_state('practice')
        state['quarantined_reports'] = {
            'new:digest': {'status': 'held', 'request_id': 'secret_private', 'reporter': '@channel'},
            'old:digest': {'status': 'held'},
            'empty:digest': {'status': 'held', 'request_id': None, 'reporter': None},
            'done:digest': {'status': 'superseded', 'request_id': '💛20990101-1', 'reporter': 'あいす'}}
        items = held_items(state)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item['request'] == '依頼ID：形式を確認してください／報告した子：名前を確認してください'
                            for item in items))
        self.assertNotIn('@channel', json.dumps(items))
        self.assertNotIn('secret_private', json.dumps(items))


class ReviewConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='riri-review-status-config-'))
        self.path = self.root / '.env'

    def write_env(self, text):
        self.path.write_text(text, encoding='utf-8')

    def test_inline_comments_apply_to_modes_ids_boolean_and_integer_values(self):
        self.write_env('RIRI_MODE=live # only file setting authorizes live\n'
                       'EXPECTED_SLACK_BOT_ID=BTEST # checked\n'
                       'EXPECTED_SLACK_USER_ID="UTEST" # checked\n'
                       'ICE_MENTION_USER_ID=U0TESTICE000 # synthetic\n'
                       'LLM_ENABLED=true # explicit opt-in\n'
                       'OLLAMA_NUM_CTX=2048 # numeric comment\n')
        cfg = Config.load(self.root)
        cfg.validate_identity()
        self.assertEqual((cfg.mode, cfg.expected_bot_id, cfg.expected_user_id), ('live', 'BTEST', 'UTEST'))
        self.assertTrue(cfg.llm_enabled)
        self.assertEqual(cfg.ollama_num_ctx, 2048)
        self.assertEqual(Config.load(self.root, dry_run=True).mode, 'practice')

    def test_quoted_hashes_and_unquoted_fragments_remain_literal(self):
        self.write_env('A="hello # world" # comment\n'
                       "B='hello # world' # comment\n"
                       'C=https://example.invalid/page#section # comment\n'
                       'D="" # empty quoted\nE= # empty plain\n'
                       'export F = plain value # comment\n')
        self.assertEqual(read_env(self.path), {
            'A': 'hello # world', 'B': 'hello # world',
            'C': 'https://example.invalid/page#section', 'D': '', 'E': '', 'F': 'plain value'})

    def test_dotenv_never_expands_variables_commands_or_backslashes(self):
        self.write_env('A="$RIRI_TEST_VALUE ${RIRI_TEST_VALUE} $(touch marker) `command`" # comment\n'
                       'B=$RIRI_TEST_VALUE # comment\n'
                       r'C="C:\\folder\\file # data"' + '\n'
                       r'D="escaped \"# quoted\" data" # comment' + '\n')
        with patch.dict('os.environ', {'RIRI_TEST_VALUE': 'private-expanded-value'}):
            env = read_env(self.path)
        self.assertEqual(env['A'], '$RIRI_TEST_VALUE ${RIRI_TEST_VALUE} $(touch marker) `command`')
        self.assertEqual(env['B'], '$RIRI_TEST_VALUE')
        self.assertEqual(env['C'], r'C:\\folder\\file # data')
        self.assertEqual(env['D'], r'escaped \"# quoted\" data')
        self.assertFalse((self.root / 'marker').exists())
        self.assertNotIn('private-expanded-value', json.dumps(env))

    def test_malformed_quoted_values_fail_without_disclosing_value(self):
        for value in ('"private-secret', "'private-secret", '"private-secret" trailing-data'):
            self.write_env('A=' + value + '\n')
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                read_env(self.path)
            self.assertNotIn('private-secret', str(error.exception))

    def test_missing_identity_loads_for_audited_stop_and_local_only_status(self):
        variants = ('', 'EXPECTED_SLACK_BOT_ID=BTEST # checked\n',
                    'EXPECTED_SLACK_USER_ID=UTEST # checked\n',
                    'EXPECTED_SLACK_BOT_ID=" " # blank\nEXPECTED_SLACK_USER_ID=UTEST\n',
                    'EXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID= # blank\n')
        for text in variants:
            self.write_env(text)
            with self.subTest(text=text):
                cfg = Config.load(self.root, environ={'EXPECTED_SLACK_BOT_ID': 'BENV',
                                                     'EXPECTED_SLACK_USER_ID': 'UENV'})
                self.assertEqual(cfg.mode, 'practice')
                with self.assertRaises(AccountMismatch):
                    cfg.validate_identity()
        self.write_env('EXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\nICE_MENTION_USER_ID=U0TESTICE000\n')
        self.assertIsNone(Config.load(self.root).validate_identity())


if __name__ == '__main__':
    unittest.main()
