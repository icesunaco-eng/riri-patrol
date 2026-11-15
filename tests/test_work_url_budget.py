"""The fixed URL reminder must not consume the existing material allowance."""
import unittest

import test_patrol_flow as flows
from riri.compose import CLOSINGS, instruction
from riri.errors import UnsafeContent
from riri.llm import validate_draft
from riri.parse import parse_requests


GUIDANCE = ('完了報告には「作業場所URL：実際に作業したチャット・実行セッションのURL」を必ず記入'
            '（成果物URLとは別。未取得なら理由を記入し追記待ち）')


def request_at_legacy_length(length, steps=None):
    """Construct material at an exact pre-reminder canonical size."""
    original = flows.request(recipients='@のあ', task='確認する')
    req = parse_requests([original])[0]
    if steps is not None:
        req.fields['手順'] = steps
    current = instruction(req, req.recipients)
    old = current.replace('\n' + GUIDANCE, '', 1)
    req.fields['やること'] += 'あ' * (length - len(old))
    return req


class WorkURLBudget(unittest.TestCase):
    def test_old_canonical_1500_is_still_allowed_with_fixed_reminder(self):
        req = request_at_legacy_length(1500)
        text = instruction(req, req.recipients)
        self.assertIn(GUIDANCE, text)
        self.assertEqual(len(text.replace('\n' + GUIDANCE, '', 1)), 1500)
        self.assertEqual(len(text), 1500 + len(GUIDANCE) + 1)
        self.assertIn(req.fields['やること'], text)

    def test_old_canonical_1501_now_fits_without_truncation(self):
        req = request_at_legacy_length(1501)
        text = instruction(req, req.recipients)
        self.assertEqual(len(text), 1501 + len(GUIDANCE) + 1)
        self.assertIn(req.fields['やること'], text)

    def test_user_copy_of_reminder_does_not_gain_another_fixed_budget(self):
        req = request_at_legacy_length(3001, steps=GUIDANCE)
        with self.assertRaises(UnsafeContent):
            instruction(req, req.recipients)

    def test_llm_validator_uses_same_fixed_budget_and_keeps_reminder(self):
        req = request_at_legacy_length(1500)
        canonical = instruction(req, req.recipients)
        prefix = canonical.rsplit('\n', 1)[0] + '\n'
        for closing in CLOSINGS:
            self.assertTrue(validate_draft(prefix + closing, req, req.recipients))
        self.assertFalse(validate_draft(canonical.replace('\n' + GUIDANCE, '', 1), req, req.recipients))
        self.assertFalse(validate_draft(canonical + '余分', req, req.recipients))
        over = request_at_legacy_length(3001)
        self.assertFalse(validate_draft(canonical, over, over.recipients))


class WorkURLBudgetPatrol(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state

    def test_existing_material_capacity_survives_real_prepare_and_send_callsite(self):
        req = request_at_legacy_length(1500)
        block = flows.request(recipients='@のあ', task=req.fields['やること'])
        self.notion.blocks[self.cfg.requests_page] = [block]
        result = self.run_once(live=True)
        self.assertEqual(result['instructions'], 1)
        state = self.state(True)
        self.assertEqual(state['requests'][req.id]['status'], 'posted')
        text = state['outbox']['instruction:' + req.id]['text']
        self.assertIn(GUIDANCE, text)
        self.assertIn(req.fields['やること'], text)


if __name__ == '__main__':
    unittest.main()
