"""Execute only the documented local preflight with synthetic Config.load."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://api.anthropic.com/v1/claude_code/routines/synthetic_preflight/fire'
TOKEN = 'synthetic-local-check-not-a-real-token'


class RunbookLocalPreflight(unittest.TestCase):
    def run_snippet(self, **changes):
        cfg = SimpleNamespace(noa_fire_url=URL, noa_fire_token=TOKEN,
            expected_bot_id='BSYNTHETIC', expected_user_id='USYNTHETIC',
            mode='live', llm_enabled=False, noa_wake_enabled=False)
        error = changes.pop('load_error', None)
        for key, value in changes.items(): setattr(cfg, key, value)
        text = (ROOT / 'docs/noa-activation-v1.md').read_text()
        code = re.search(r'```python\n(.*?)\n```', text, re.S)[1]
        output = io.StringIO()
        exit_code = 0
        with patch('riri.config.Config.load', return_value=cfg, side_effect=error), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
             patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden')), \
             redirect_stdout(output), redirect_stderr(output):
            try: exec(compile(code, 'documented-local-preflight', 'exec'), {})
            except SystemExit as exc: exit_code = exc.code
        result = output.getvalue()
        self.assertNotIn(URL, result)
        self.assertNotIn(TOKEN, result)
        return exit_code, result

    def test_valid_local_shape_reports_flags_only_with_no_network(self):
        status, text = self.run_snippet()
        self.assertEqual(status, 0)
        self.assertIn('NOA_OFF: True', text)
        self.assertIn('noa_config_shape_ready: True', text)
        self.assertNotIn('BSYNTHETIC', text)
        self.assertNotIn('USYNTHETIC', text)

    def test_missing_and_malformed_config_fail_without_values(self):
        for change in ({'noa_fire_url': ''}, {'noa_fire_token': ''},
                       {'noa_fire_url': 'https://invalid.example/' + TOKEN},
                       {'noa_fire_token': TOKEN + '\n'}, {'expected_user_id': ''}):
            with self.subTest(change=list(change)):
                status, text = self.run_snippet(**change)
                self.assertEqual(status, 2)
                self.assertNotIn('https://invalid.example', text)

    def test_wrong_mode_or_llm_enabled_is_not_ready(self):
        for change in ({'mode': 'practice'}, {'llm_enabled': True}):
            with self.subTest(change=change): self.assertEqual(self.run_snippet(**change)[0], 2)

    def test_loader_error_never_prints_exception_or_traceback(self):
        status, text = self.run_snippet(load_error=ValueError(TOKEN + URL))
        self.assertEqual(status, 2)
        self.assertEqual(text, 'local_config_check_failed（値は表示しません）\n')


if __name__ == '__main__':
    unittest.main()
