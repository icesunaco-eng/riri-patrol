"""Offline v0.2 configuration, fixed notifications, and launchd template tests."""
import ast
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from riri.config import Config
from riri.errors import AccountMismatch
from riri import notify

ROOT = Path(__file__).resolve().parents[1]
IDS = 'EXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\n'


class ConfigRevisionTests(unittest.TestCase):
    def setUp(self):
        # Deliberately retained test artifacts; no automatic cleanup/deletion.
        self.root = Path(tempfile.mkdtemp(prefix='riri-config-v02-'))

    def write_env(self, text):
        (self.root / '.env').write_text(text, encoding='utf-8')

    def test_terminal_cannot_select_live_or_supply_tokens(self):
        self.write_env(IDS)
        injected = {'RIRI_MODE': 'live', 'SLACK_BOT_TOKEN': 'fake-shell-bot',
                    'NOTION_TOKEN': 'fake-shell-notion'}
        with patch.dict('os.environ', injected):
            cfg = Config.load(self.root)
        self.assertEqual((cfg.mode, cfg.slack_token, cfg.notion_token), ('practice', '', ''))
        cfg = Config.load(self.root, environ=injected)
        self.assertEqual((cfg.mode, cfg.slack_token, cfg.notion_token), ('practice', '', ''))

    def test_env_credentials_mode_and_destinations_override_shell(self):
        self.write_env(IDS + 'RIRI_MODE=live\nSLACK_BOT_TOKEN=fake-file-bot\n'
                       'NOTION_TOKEN=fake-file-notion\nSLACK_REPORT_CHANNEL=CFILE\n')
        cfg = Config.load(self.root, environ={'RIRI_MODE': 'practice',
            'SLACK_BOT_TOKEN': 'fake-shell-bot', 'NOTION_TOKEN': 'fake-shell-notion',
            'EXPECTED_SLACK_BOT_ID': 'BEVIL', 'EXPECTED_SLACK_USER_ID': 'UEVIL',
            'SLACK_REPORT_CHANNEL': 'CEVIL'})
        self.assertEqual((cfg.mode, cfg.slack_token, cfg.notion_token),
                         ('live', 'fake-file-bot', 'fake-file-notion'))
        self.assertEqual((cfg.expected_bot_id, cfg.expected_user_id, cfg.report_channel),
                         ('BTEST', 'UTEST', 'CFILE'))
        self.assertEqual(Config.load(self.root, dry_run=True).mode, 'practice')

    def test_missing_empty_ids_fail_even_practice_and_shell_cannot_supply(self):
        for text in ['', 'EXPECTED_SLACK_BOT_ID=BTEST\n',
                     'EXPECTED_SLACK_USER_ID=UTEST\n',
                     'EXPECTED_SLACK_BOT_ID=" "\nEXPECTED_SLACK_USER_ID=UTEST\n',
                     'EXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=\n']:
            self.write_env(text)
            with self.subTest(text=text), self.assertRaises(AccountMismatch):
                Config.load(self.root, environ={'EXPECTED_SLACK_BOT_ID':'BENV',
                                               'EXPECTED_SLACK_USER_ID':'UENV'})
        self.assertEqual(Config(root=self.root).expected_bot_id, '')
        self.assertEqual(Config(root=self.root).expected_user_id, '')

    def test_log_size_default_and_file_only_override(self):
        self.write_env(IDS)
        self.assertEqual(Config.load(self.root, environ={'LOG_SIZE_NOTICE_BYTES':'1'}).log_size_notice_bytes,
                         50 * 1024 * 1024)
        self.write_env(IDS + 'LOG_SIZE_NOTICE_BYTES=12345\n')
        self.assertEqual(Config.load(self.root).log_size_notice_bytes, 12345)
        self.write_env(IDS + 'LOG_SIZE_NOTICE_BYTES=not-a-number\n')
        self.assertEqual(Config.load(self.root).log_size_notice_bytes, 50 * 1024 * 1024)

    def test_env_invalid_mode_remains_practice(self):
        for value in ['', 'LIVE', 'bad']:
            self.write_env(IDS + 'RIRI_MODE=' + value + '\n')
            self.assertEqual(Config.load(self.root).mode, 'practice')

    def test_llm_is_disabled_unless_explicit_true_in_file(self):
        self.assertFalse(Config(root=self.root).llm_enabled)
        for value in (None,'','false','bad','1'):
            self.write_env(IDS + ('' if value is None else 'LLM_ENABLED='+value+'\n'))
            self.assertFalse(Config.load(self.root,environ={'LLM_ENABLED':'true'}).llm_enabled)
        self.write_env(IDS+'LLM_ENABLED=true\n')
        self.assertTrue(Config.load(self.root).llm_enabled)


class FixedNoticeTests(unittest.TestCase):
    def test_fixed_local_commands_only(self):
        with patch.object(notify.platform, 'system', return_value='Darwin'), \
             patch.object(notify.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(notify.python_too_old())
            self.assertTrue(notify.unexpected_error())
            self.assertTrue(notify.account_mismatch())
            self.assertFalse(notify.notify('secret; malicious input'))
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0][:2], ['/usr/bin/osascript', '-e'])
            self.assertEqual(kwargs['timeout'], 3)
            self.assertNotIn('secret', args[0][2])
            self.assertNotIn('shell', kwargs)

    def test_notification_failure_is_safe(self):
        with patch.object(notify.platform, 'system', return_value='Darwin'), \
             patch.object(notify.subprocess, 'run', side_effect=OSError('private details')):
            self.assertFalse(notify.unexpected_error())
        with patch.object(notify.platform, 'system', return_value='Linux'), \
             patch.object(notify.subprocess, 'run') as run:
            self.assertFalse(notify.python_too_old())
            run.assert_not_called()

    def test_module_has_old_python_compatible_syntax(self):
        ast.parse((ROOT / 'riri/notify.py').read_text(), feature_version=(3, 6))


class DeploymentTemplateTests(unittest.TestCase):
    def test_python_uses_reported_full_path_but_project_remains_unconfigured(self):
        data = plistlib.loads((ROOT / 'launchd/com.heroines.riri-patrol.plist.template').read_bytes())
        python = data['ProgramArguments'][0]
        self.assertTrue(Path(python).is_absolute())
        self.assertEqual(python, '/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14')
        self.assertIn('__PROJECT_ABSOLUTE_PATH__', data['ProgramArguments'][1])
        self.assertNotEqual(python, '/usr/bin/python3')
        self.assertEqual(data['StartInterval'], 300)

    def test_example_ids_require_explicit_verification(self):
        text = (ROOT / '.env.example').read_text()
        self.assertIn('EXPECTED_SLACK_BOT_ID=\n', text)
        self.assertIn('EXPECTED_SLACK_USER_ID=\n', text)
        self.assertIn('LOG_SIZE_NOTICE_BYTES=52428800', text)


if __name__ == '__main__':
    unittest.main()
