"""RIRI-3000-20261115 self-tests. No network or real Claude execution."""
import hashlib
import unittest
from unittest.mock import patch

import test_patrol_flow as flows
import test_local_outcomes as outcomes
import test_review_v104_user_reports as user_reports
from test_mamu_bridge import RID, text as mamu_text
from test_work_url_budget import GUIDANCE, request_at_legacy_length
from riri.compose import CLOSINGS, INSTRUCTION_BODY_FIELDS, instruction, instruction_budget
from riri.config import Config
from riri.errors import UnsafeContent
from riri.llm import DraftService, validate_draft
from riri.mamu_bridge import validate_text
from riri.parse import parse_requests


class NeverLLM:
    def request(self, *args, **kwargs):
        raise AssertionError('Long text must use the lossless deterministic type')


def unicode_request(length, unit):
    req = request_at_legacy_length(500)
    # Body source values only: headers, labels, defaults and escaping are separate.
    space = length - sum(len(req.fields.get(key) or '') for key in INSTRUCTION_BODY_FIELDS)
    req.fields['やること'] += unit * (space // len(unit)) + 'あ' * (space % len(unit))
    return req


class Instruction3000(unittest.TestCase):
    def test_body_2999_3000_3001_including_fixed_guidance(self):
        for length in (1500, 2999, 3000):
            with self.subTest(length=length):
                req = unicode_request(length, 'あ')
                canonical = instruction(req, req.recipients)
                budget = instruction_budget(req, req.recipients)
                self.assertEqual(budget['body_code_points'], length)
                self.assertEqual(len(canonical), length + budget['header_code_points'] + 72)
                self.assertIn(req.fields['やること'], canonical)
                self.assertTrue(validate_draft(canonical, req, req.recipients))
        req = unicode_request(3001, 'あ')
        with self.assertRaises(UnsafeContent):
            instruction(req, req.recipients)

    def test_unicode_code_points_not_graphemes_or_utf16(self):
        for unit, count, utf16 in [('😀', 1, 2), ('e\u0301', 2, 2),
                                   ('👨‍👩‍👧‍👦', 7, 11), ('❤️', 2, 2)]:
            with self.subTest(unit=unit):
                self.assertEqual(len(unit), count)
                self.assertEqual(len(unit.encode('utf-16-le')) // 2, utf16)
                req = unicode_request(3000, unit)
                canonical = instruction(req, req.recipients)
                self.assertEqual(instruction_budget(req, req.recipients)['body_code_points'], 3000)
                self.assertIn(unit, canonical)
                req.fields['やること'] += 'あ'
                with self.assertRaises(UnsafeContent):
                    instruction(req, req.recipients)

    def test_line_endings_and_entities_never_consume_body_capacity(self):
        req = request_at_legacy_length(500)
        req.fields['手順'] = 'a\r\nb\rc\nd & < > e\u0301'
        canonical = instruction(req, req.recipients)
        self.assertIn('a\n  b\n  c\n  d &amp; &lt; &gt; e\u0301', canonical)
        self.assertNotIn('\r', canonical)
        body = sum(len(req.fields.get(key) or '') for key in INSTRUCTION_BODY_FIELDS)
        req.fields['やること'] += 'あ' * (3000 - body)
        budget = instruction_budget(req, req.recipients)
        self.assertEqual(budget['body_code_points'], 3000)
        self.assertGreater(budget['body_wire_code_points'], 3000)
        req.fields['やること'] += '&'  # five wire characters, never silent slicing
        with self.assertRaises(UnsafeContent):
            instruction(req, req.recipients)

    def test_long_llm_full_equality_and_deterministic_draft(self):
        req = unicode_request(3000, 'あ')
        canonical = instruction(req, req.recipients)
        self.assertEqual(DraftService(NeverLLM()).draft(req, req.recipients), (canonical, '型'))
        prefix = canonical.rsplit('\n', 1)[0] + '\n'
        for closing in CLOSINGS:
            self.assertTrue(validate_draft(prefix + closing, req, req.recipients))
        for n, _ in enumerate(canonical.splitlines()[:-1]):
            rows = canonical.splitlines()
            self.assertFalse(validate_draft('\n'.join(rows[:n] + rows[n+1:]), req, req.recipients))
        for bad in (canonical[:1500], canonical[:-1], canonical + '追加',
                    canonical.replace('いつまでに：今日中', 'いつまでに：明日')):
            self.assertFalse(validate_draft(bad, req, req.recipients))

    def test_long_unsafe_and_unencodable_inputs_stay_rejected(self):
        for bad in ('<@U123>', '@here', '\u202e', '\ud800', '\udfff'):
            req = request_at_legacy_length(2990)
            req.fields['やること'] += bad
            with self.subTest(bad=repr(bad)), self.assertRaises(UnsafeContent):
                instruction(req, req.recipients)

    def test_mamu_bridge_uses_same_3000_boundary_before_any_write(self):
        original = mamu_text()
        cfg = Config(root='.')
        req = validate_text(RID, original, cfg)
        body = sum(len(req.fields.get(key) or '') for key in INSTRUCTION_BODY_FIELDS)
        padded = original.replace('内容をあいすへ届けてほしい。',
                                  '内容をあいすへ届けてほしい。' + 'あ' * (3000 - body))
        accepted = validate_text(RID, padded, cfg)
        self.assertEqual(instruction_budget(accepted, ['あいす'], cfg.pages)['body_code_points'], 3000)
        with self.assertRaises(UnsafeContent):
            validate_text(RID, padded.replace('内容をあいすへ届けてほしい。', '内容をあいすへ届けてほしい。あ'), cfg)


class BoundaryPatrol(unittest.TestCase):
    setUp = flows.FlowTests.setUp
    tearDown = flows.FlowTests.tearDown
    run_once = flows.FlowTests.run_once
    state = flows.FlowTests.state

    def test_prepare_send_relay_and_restart_keep_full_text_once(self):
        # あいす triggers the report-channel relay as well as the instruction.
        block = flows.request(recipients='@あいす', task='対象を確認する')
        req = parse_requests([block])[0]
        padding = 3000 - sum(len(req.fields.get(key) or '') for key in INSTRUCTION_BODY_FIELDS)
        block['code']['rich_text'][0]['text']['content'] = block['code']['rich_text'][0]['text']['content'].replace(
            '対象を確認する', '対象を確認する' + 'あ' * padding)
        self.notion.blocks[self.cfg.requests_page] = [block]
        self.assertEqual(self.run_once(live=True)['instructions'], 1)
        state = self.state(True)
        canonical = state['requests'][req.id]['draft']
        posted_req = parse_requests([self.notion.blocks[self.cfg.requests_page][0]])[0]
        self.assertEqual(instruction_budget(posted_req, posted_req.recipients)['body_code_points'], 3000)
        self.assertEqual(state['requests'][req.id]['status'], 'posted')
        for key in ('instruction:' + req.id, 'instruction-gdm:' + req.id):
            item = state['outbox'][key]
            self.assertEqual(item['status'], 'posted')
            self.assertIn(canonical, item['text'])
            self.assertLess(len(item['text']), 4000)
        posts = list(self.slack.posts)
        self.clock.advance()
        self.run_once(live=True)
        self.assertEqual(self.slack.posts, posts)

    def test_3001_stops_before_status_change_and_send(self):
        req = unicode_request(3001, 'あ')
        self.notion.blocks[self.cfg.requests_page] = [flows.request(recipients='@のあ', task=req.fields['やること'])]
        self.assertEqual(self.run_once(live=True)['instructions'], 0)
        self.assertFalse(any(kind == 'status' for kind, *_ in self.notion.writes))
        self.assertFalse(any(ch == self.cfg.instruction_channel for ch, *_ in self.slack.posts))

    def test_expansion_alone_never_releases_old_length_hold(self):
        req = request_at_legacy_length(2000)
        block = flows.request(recipients='@のあ', task=req.fields['やること'])
        self.notion.blocks[self.cfg.requests_page] = [block]
        # Reproduce the actual old renderer's hold, then restore the new limit.
        with patch('riri.compose.INSTRUCTION_BODY_LIMIT', 1500):
            self.assertEqual(self.run_once(live=True)['instructions'], 0)
        before = self.state(True)['requests'][req.id].copy()
        self.assertEqual(before['status'], 'held')
        self.clock.advance()
        self.assertEqual(self.run_once(live=True)['instructions'], 0)
        after = self.state(True)['requests'][req.id]
        for field in ('status', 'hold_reason', 'unsafe_digest', 'first_seen', 'block_id'):
            self.assertEqual(after[field], before[field])
        self.assertFalse(any(ch == self.cfg.instruction_channel for ch, *_ in self.slack.posts))


class BoundarySlackTransport(unittest.TestCase):
    setUp = user_reports.UserReportsTests.setUp
    client = user_reports.UserReportsTests.client

    def test_actual_slack_client_posts_complete_single_payload_with_decoration(self):
        req = request_at_legacy_length(3000)
        canonical = instruction(req, req.recipients)
        client, transport = self.client({'ok': True, 'ts': '1.1'}, {'ok': True, 'ts': '2.2'})
        client.post('CINSTRUCTIONS', canonical + '\n' + user_reports.MARK, kind='instruction')
        client.post('CREPORT', '<@UICE>\n' + canonical + '\n' + user_reports.MARK,
                    kind='instruction_relay')
        messages = [kwargs['payload']['text'] for _, _, kwargs in transport.calls
                    if (kwargs.get('payload') or {}).get('text')]
        self.assertEqual(len(messages), 2)  # one payload per existing destination
        self.assertEqual(messages[0], canonical + '\n' + user_reports.MARK)
        self.assertEqual(messages[1], '<@UICE>\n' + user_reports.AUTOMATED_REPORT_LABEL + '\n' +
                         canonical + '\n' + user_reports.MARK)
        self.assertTrue(all(len(message) < 4000 for message in messages))


class BoundaryLocalInput(unittest.TestCase):
    setUp = outcomes.WorkerTests.setUp
    reap = outcomes.WorkerTests.reap
    spawn = outcomes.WorkerTests.spawn
    fire = outcomes.WorkerTests.fire

    def test_3000_material_utf8_and_hash_reach_only_fake_worker(self):
        req = unicode_request(3000, '😀e\u0301')
        canonical = instruction(req, req.recipients)
        result, observed = self.fire(canonical)
        raw = (self.root / (result['local_ref'] + '.in.txt')).read_bytes()
        self.assertEqual(raw, canonical.encode('utf-8'))
        self.assertEqual(observed['input_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.attempt_records[0]['input_sha256'], observed['input_sha256'])
        self.assertEqual(observed['outcome'], 'cli_exited')
