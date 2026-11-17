import copy
from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.errors import RemoteError, SafetyError
from riri.compose import instruction
from riri.mentions import add_report_mention
from riri.mamu_bridge import (FixedNotionStore, MamuBridge, REPORT_TO, REQUESTS_PAGE_ID,
    dispatch, main, validate_text)
from riri.models import REPORT_PAGES, RECIPIENTS
from riri.notion import NotionClient
from riri.parse import ID_RE, parse_reports, parse_requests


RID = "🧡20261113-1"
POLICY = {"schema_version": 1, "id_prefix": "🧡", "approved": True,
    "approval_source": "synthetic-owner-approval", "approved_at": "2026-11-13T10:00:00+09:00"}


def text(status="未着手"):
    label = "連携テスト。実障害ではありません。処理・配送不要。" if status == "完了" else ""
    return "\n".join(["【りりへの依頼】", "依頼ID：" + RID, "宛先：@あいす", "依頼した子：まむ",
        "状態：" + status, "案件ページ（NotionのURL）：https://app.notion.com/p/417f1dff94dc81f69bdfe9efc8c717ab",
        "GitHubプロジェクト：なし", "やること：" + label + "内容をあいすへ届けてほしい。",
        "手順：", "1. あいすは根拠を確認し、たかむねへ原因・対処法・必要な判断を伝えてほしい。",
        "   - 根拠：架空ログsample1。", "使うもの：案件ページ。", "やってはいけないこと：自動修復しない。",
        "期待する結果：同じIDで段階を別々に確認。", "期限・実行タイミング：本人の許可範囲で。",
        "報告先：" + REPORT_TO, "うまくいかないとき：止めて未確認を残す。"])


def block(value, block_id="11111111-2222-3333-4444-555555555555"):
    return {"id": block_id, "type": "code", "code": {"rich_text": [{"text": {"content": value}}]}}


class Store:
    def __init__(self):
        self.rows, self.writes, self.reads = [], 0, 0
        self.append_error = None
        self.commit_on_error = False
        self.hide = False
        self.read_error_after_write = False
        self.on_append = None

    def read(self):
        self.reads += 1
        if self.read_error_after_write and self.writes:
            raise RemoteError("synthetic read failed")
        return [] if self.hide else copy.deepcopy(self.rows)

    def append(self, value):
        self.writes += 1
        if self.on_append:
            self.on_append()
        if self.append_error:
            if self.commit_on_error:
                self.rows.append(block(value))
            raise self.append_error
        self.rows.append(block(value))


class TestMamuBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config(root=self.root, expected_user_id="UBOTFAKE", ice_mention_user_id="UICEFAKE")
        self.store = Store()
        self.bridge = MamuBridge(self.cfg, self.store, POLICY)

    def test_new_save_readback_and_all_receipts_separate(self):
        result = self.bridge.submit(RID, text())
        self.assertEqual(result["result"], "saved_verified")
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(self.store.reads, 2)
        self.assertEqual(result["stages"]["notion_saved"]["status"], "confirmed")
        for stage in ("riri_intake", "ice_delivered", "ice_received", "takamune_reported"):
            self.assertEqual(result["stages"][stage]["status"], "unconfirmed")

    def test_intent_is_fsynced_before_append(self):
        observed = []
        self.store.on_append = lambda: observed.append(json.loads(self.bridge.path.read_text())["requests"][RID]["phase"])
        self.bridge.submit(RID, text())
        self.assertEqual(observed, ["intent"])

    def test_submit_again_and_restart_only_reads(self):
        self.bridge.submit(RID, text())
        self.bridge.submit(RID, text())
        MamuBridge(self.cfg, self.store, POLICY).submit(RID, text())
        self.assertEqual(self.store.writes, 1)

    def test_interruption_after_durable_intent_never_blindly_rewrites(self):
        def crash():
            raise KeyboardInterrupt()
        self.store.on_append = crash
        with self.assertRaises(KeyboardInterrupt):
            self.bridge.submit(RID, text())
        self.assertEqual(json.loads(self.bridge.path.read_text())["requests"][RID]["phase"], "intent")
        self.store.on_append = None
        self.assertEqual(MamuBridge(self.cfg, self.store, POLICY).submit(RID, text())["result"], "saved_unknown")
        self.assertEqual(self.store.writes, 1)

    def test_same_id_different_content_rejected(self):
        self.bridge.submit(RID, text())
        result = self.bridge.submit(RID, text().replace("架空ログsample1", "架空ログsample2"))
        self.assertEqual(result["result"], "collision")
        self.assertEqual(self.store.writes, 1)

    def test_preexisting_remote_same_text_deduplicates(self):
        self.store.rows.append(block(text()))
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_verified")
        self.assertEqual(self.store.writes, 0)

    def test_remote_duplicate_id_never_appends(self):
        self.store.rows = [block(text()), block(text(), "second")]
        self.assertEqual(self.bridge.submit(RID, text())["result"], "collision")
        self.assertEqual(self.store.writes, 0)

    def test_remote_changed_text_never_appends(self):
        self.store.rows = [block(text().replace("sample1", "sampleX"))]
        self.assertEqual(self.bridge.submit(RID, text())["result"], "collision")
        self.assertEqual(self.store.writes, 0)

    def test_uncertain_write_with_commit_is_verified_without_retry(self):
        self.store.append_error = RemoteError("uncertain", uncertain=True)
        self.store.commit_on_error = True
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_verified")
        self.assertEqual(self.store.writes, 1)

    def test_uncertain_zero_results_never_retries(self):
        self.store.append_error = RemoteError("uncertain", uncertain=True)
        first = self.bridge.submit(RID, text())
        again = self.bridge.submit(RID, text())
        self.assertEqual(first["result"], "saved_unknown")
        self.assertEqual(again["result"], "saved_unknown")
        self.assertFalse(again["ok"])
        self.assertEqual(self.store.writes, 1)

    def test_readback_failure_remains_unknown_and_no_rewrite(self):
        self.store.read_error_after_write = True
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_unknown")
        self.store.read_error_after_write = False
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_verified")
        self.assertEqual(self.store.writes, 1)

    def test_definitively_rejected_save_not_reported_as_saved(self):
        self.store.append_error = RemoteError("synthetic rejection", uncertain=False)
        result = self.bridge.submit(RID, text())
        self.assertEqual(result["result"], "save_failed")
        self.assertEqual(result["stages"]["notion_saved"]["status"], "failed")
        self.store.append_error = None
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_verified")
        self.assertEqual(self.store.writes, 2)

    def test_stop_rejects_active_mail_without_saving(self):
        (self.root / "STOP").touch()
        self.assertEqual(self.bridge.submit(RID, text())["result"], "blocked_stopped")
        self.assertEqual(self.store.writes, 0)
        self.assertTrue((self.root / "STOP").exists())

    def test_stop_allows_only_inactive_completed_storage_test(self):
        (self.root / "STOP").touch()
        result = self.bridge.submit(RID, text("完了"))
        self.assertEqual(result["result"], "saved_verified")
        self.assertEqual(result["stages"]["riri_intake"]["status"], "unconfirmed")
        self.assertEqual(result["stages"]["ice_delivered"]["status"], "unconfirmed")
        self.assertTrue((self.root / "STOP").exists())

    def test_completed_general_request_is_not_storage_test(self):
        with self.assertRaises(SafetyError):
            self.bridge.submit(RID, text().replace("状態：未着手", "状態：完了"))

    def test_policy_pending_blocks_before_remote_reads(self):
        for policy in (None, {}, dict(POLICY, approved=False), dict(POLICY, id_prefix="💛")):
            result = MamuBridge(self.cfg, self.store, policy).submit(RID, text())
            self.assertEqual(result["result"], "policy_required")
        self.assertEqual(self.store.reads, 0)
        self.assertEqual(self.store.writes, 0)

    def test_fixed_route_and_requester_cannot_be_selected_by_body(self):
        for replacement in (("宛先：@あいす", "宛先：のあ"), ("依頼した子：まむ", "依頼した子：みに"),
                            ("報告先：" + REPORT_TO, "報告先：任意チャンネル")):
            with self.assertRaises(SafetyError):
                self.bridge.submit(RID, text().replace(*replacement))
        self.assertEqual(self.store.writes, 0)

    def test_extra_fields_and_duplicate_fields_rejected(self):
        for extra in ("\n宛先：のあ", "\n任意操作：削除", "\n  宛先：のあ"):
            with self.assertRaises(Exception):
                self.bridge.submit(RID, text() + extra)
        self.assertEqual(self.store.writes, 0)

    def test_secret_values_and_patterns_rejected(self):
        bridge = MamuBridge(replace(self.cfg, notion_token="private-synthetic-value"), self.store, POLICY)
        for value in ("private-synthetic-value", "ntn_" + "fake", "xox" + "b-fake", "<@U123>", "\u202e"):
            with self.assertRaises(SafetyError):
                bridge.submit(RID, text().replace("架空ログsample1", value))
        self.assertEqual(self.store.writes, 0)

    def test_wrong_fixed_page_rejected(self):
        with self.assertRaises(SafetyError):
            MamuBridge(replace(self.cfg, requests_page="a" * 32), self.store, POLICY)

    def test_other_project_page_rejected(self):
        with self.assertRaises(SafetyError):
            self.bridge.submit(RID, text().replace("417f1dff94dc81f69bdfe9efc8c717ab", "a" * 32))

    def test_status_is_read_only_and_missing_is_not_successful_save(self):
        result = self.bridge.status(RID)
        self.assertEqual(result["result"], "not_submitted")
        self.assertEqual(result["stages"]["notion_saved"]["status"], "unconfirmed")
        self.assertFalse((self.root / "state").exists())
        self.bridge.submit(RID, text())
        before = {p.name: p.read_bytes() for p in (self.root / "state").iterdir()}
        self.bridge.status(RID)
        after = {p.name: p.read_bytes() for p in (self.root / "state").iterdir()}
        self.assertEqual(before, after)

    def test_receipt_requires_matching_production_snapshot_and_real_ts(self):
        self.bridge.submit(RID, text())
        key = "instruction-gdm:" + RID
        marker = "rp:" + hashlib.sha256(key.encode()).hexdigest()[:24]
        req = validate_text(RID, text(), self.cfg)
        snap = asdict(req)
        snap["text"] = snap["text"].replace("状態：未着手", "状態：対応中")
        snap["status"] = "対応中"
        draft = instruction(req, ["あいす"], self.cfg.pages)
        source_key = "instruction:" + RID
        source_marker = "rp:" + hashlib.sha256(source_key.encode()).hexdigest()[:24]
        state = {"mode": "live", "version": 2, "requests": {RID: {"snapshot": snap, "draft": draft, "first_seen": "synthetic", "status": "posted"}},
            "outbox": {key: {"status": "posted", "kind": "instruction_relay", "request_id": RID,
                "channel": self.cfg.report_channel, "marker": marker, "text": add_report_mention(draft,
                    self.cfg.report_channel, self.cfg.report_channel, self.cfg.ice_mention_user_id,
                    self.cfg.expected_user_id) + "\n" + marker,
                "ts": "1234567890.123456"}}}
        state["outbox"][source_key] = {"status": "posted", "kind": "instruction", "request_id": RID,
            "channel": self.cfg.instruction_channel, "marker": source_marker,
            "text": draft + "\n" + source_marker, "ts": "1234567890.123455"}
        path = self.root / "state/state.json"
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        result = self.bridge.status(RID)
        self.assertEqual(result["stages"]["riri_intake"]["status"], "confirmed")
        self.assertEqual(result["stages"]["ice_delivered"]["status"], "confirmed")
        self.assertEqual(result["stages"]["ice_received"]["status"], "unconfirmed")
        self.assertEqual(result["stages"]["takamune_reported"]["status"], "unconfirmed")
        self.assertEqual(before, path.read_bytes())
        relay_text = state["outbox"][key]["text"]
        state["outbox"][key]["text"] = RID + " changed content\n" + marker
        path.write_text(json.dumps(state))
        self.assertEqual(self.bridge.status(RID)["stages"]["ice_delivered"]["status"], "unconfirmed")
        state["outbox"][key]["text"] = relay_text
        state["outbox"][source_key]["text"] = RID + " wrong source body\n" + source_marker
        path.write_text(json.dumps(state))
        self.assertEqual(self.bridge.status(RID)["stages"]["ice_delivered"]["status"], "unconfirmed")
        state["outbox"][source_key]["text"] = draft + "\n" + source_marker
        state["outbox"][key]["ts"] = "practice-key"
        path.write_text(json.dumps(state))
        self.assertEqual(self.bridge.status(RID)["stages"]["ice_delivered"]["status"], "unconfirmed")
        state["outbox"][key]["ts"] = "1234567890.123456"
        state["requests"][RID]["snapshot"]["requester"] = "みに"
        path.write_text(json.dumps(state))
        self.assertEqual(self.bridge.status(RID)["stages"]["riri_intake"]["status"], "unconfirmed")

    def test_v2_local_receipts_bind_policy_material_and_optional_owner_evidence(self):
        from riri.delivery_policy import decorate_delivery, make_delivery_policy
        from riri.slack import REPORT_OWNER_ID
        self.bridge.submit(RID, text())
        req = validate_text(RID, text(), self.cfg)
        draft = instruction(req, ['あいす'], self.cfg.pages)
        source_key = 'instruction:' + RID
        policy = make_delivery_policy(self.cfg, 'aisu_instruction', bundle_id=source_key,
            canonical_text=draft, request_id=RID, recipients=['あいす'])
        state = {'mode': 'live', 'version': 2, 'requests': {RID: {
            'snapshot': asdict(req), 'draft': draft, 'first_seen': 'synthetic', 'status': 'posted'}}, 'outbox': {}}
        for key, kind, channel, stamp in ((source_key, 'instruction', self.cfg.instruction_channel, '1234567890.123455'),
                ('instruction-gdm:' + RID, 'instruction_relay', self.cfg.report_channel, '1234567890.123456')):
            marker = 'rp:' + hashlib.sha256(key.encode()).hexdigest()[:24]
            state['outbox'][key] = {'status': 'posted', 'kind': kind, 'request_id': RID,
                'channel': channel, 'marker': marker,
                'text': decorate_delivery(draft, policy, self.cfg.expected_user_id) + '\n' + marker,
                'ts': stamp, 'delivery_policy': copy.deepcopy(policy), 'delivery_receipt': {
                    'channel': channel, 'user_id': REPORT_OWNER_ID, 'ts': stamp,
                    'canonical_hash': policy['canonical_hash'], 'mention_name': 'あいす',
                    'mention_mode': 'official', 'mention_user_id': self.cfg.ice_mention_user_id}}
        path = self.root / 'state/state.json'
        path.write_text(json.dumps(state))
        result = self.bridge.status(RID)
        self.assertEqual(result['stages']['ice_delivered']['status'], 'confirmed')
        evidence = result['stages']['ice_delivered']['evidence']
        self.assertTrue(evidence['owner_receipts_recorded'])
        self.assertEqual(evidence['delivery_policy_version'], 'dual-v2')
        self.assertEqual(evidence['actor_verification'], 'unconfirmed_by_bridge')
        self.assertEqual(result['stages']['ice_received']['status'], 'unconfirmed')
        self.assertEqual(result['stages']['takamune_reported']['status'], 'unconfirmed')
        mutations = [('delivery_policy', 'request_id', '🧡20261113-2'),
            ('delivery_policy', 'canonical_hash', 'f' * 64),
            ('delivery_policy', 'recipients', ['あいす', 'のあ']),
            ('delivery_policy', 'bundle_id', 'other-bundle'),
            ('delivery_receipt', 'user_id', 'UOTHER'),
            ('delivery_receipt', 'channel', self.cfg.report_channel),
            ('delivery_receipt', 'canonical_hash', 'f' * 64),
            ('delivery_receipt', 'ts', '1234567890.999999')]
        for field, key, value in mutations:
            changed = copy.deepcopy(state)
            changed['outbox'][source_key][field][key] = value
            path.write_text(json.dumps(changed))
            with self.subTest(field=field, key=key):
                self.assertEqual(self.bridge.status(RID)['stages']['ice_delivered']['status'], 'unconfirmed')
        for item in state['outbox'].values():
            item.pop('delivery_receipt')
        path.write_text(json.dumps(state))
        evidence = self.bridge.status(RID)['stages']['ice_delivered']['evidence']
        self.assertFalse(evidence['owner_receipts_recorded'])
        self.assertEqual(evidence['actor_verification'], 'unconfirmed_by_bridge')

    def test_scoped_reservation_is_not_intake(self):
        self.bridge.submit(RID, text())
        req=validate_text(RID,text(),self.cfg)
        info={"snapshot":asdict(req),"status":"held","hold_reason":"scoped_test_only",
              "first_seen":"reservation-only","picked_event":False,
              "scoped_execution":{"phase":"reserved"}}
        path=self.root/'state/state.json'
        path.write_text(json.dumps({"mode":"live","version":2,"requests":{RID:info},"outbox":{}}))
        result=self.bridge.status(RID)
        self.assertEqual(result['stages']['riri_intake']['status'],'unconfirmed')
        self.assertEqual(result['stages']['ice_delivered']['status'],'unconfirmed')
        info.pop('hold_reason');info['scoped_execution']['phase']='executed'
        path.write_text(json.dumps({"mode":"live","version":2,"requests":{RID:info},"outbox":{}}))
        self.assertEqual(self.bridge.status(RID)['stages']['riri_intake']['status'],'unconfirmed')

    def test_notional_status_change_does_not_lose_saved_receipt(self):
        self.bridge.submit(RID, text())
        self.store.rows[0] = block(text().replace("状態：未着手", "状態：対応中"))
        self.assertEqual(self.bridge.status(RID)["result"], "saved_verified")

    def test_missing_after_verified_never_reappends(self):
        self.bridge.submit(RID, text())
        self.store.rows = []
        self.assertEqual(self.bridge.submit(RID, text())["result"], "saved_unknown")
        self.assertEqual(self.store.writes, 1)

    def test_mamu_name_report_and_id_support_is_additive(self):
        self.assertIn("まむ", RECIPIENTS)
        self.assertIn("まむ", REPORT_PAGES)
        for prefix in ("🤍", "💚", "💙", "🩵", "❤️", "💛", "🧡"):
            self.assertIsNotNone(ID_RE.fullmatch(prefix + "20261113-1"))
        report = "【完了報告】\n依頼ID：" + RID + "\n報告した子：まむ\n結果：完了"
        self.assertEqual(parse_reports([block(report)], expected_reporter="まむ")[0].reporter, "まむ")

    def test_dispatch_rejects_destination_or_operation_arguments(self):
        for payload in ({"op": "delete", "request_id": RID},
                        {"op": "status", "request_id": RID, "page": "x"}):
            with self.assertRaises(SafetyError):
                dispatch(payload)

    def test_cli_error_keeps_valid_id_and_unknown_stages_without_exception_text(self):
        outgoing = io.StringIO()
        with patch("sys.argv", ["bridge"]), patch("sys.stdin", io.StringIO(json.dumps({"op": "status", "request_id": RID}))), \
                patch("sys.stdout", outgoing), patch("riri.mamu_bridge.dispatch", side_effect=RuntimeError("sensitive-synthetic-detail")):
            self.assertEqual(main(), 2)
        result = json.loads(outgoing.getvalue())
        self.assertEqual(result["request_id"], RID)
        self.assertEqual(result["result"], "bridge_error")
        self.assertNotIn("sensitive-synthetic-detail", outgoing.getvalue())
        self.assertTrue(all(item["status"] == "unconfirmed" for item in result["stages"].values()))

    def test_existing_patrol_takes_mamu_once_and_relays_same_material_offline(self):
        from tests.test_patrol_flow import Clock, FakeNotion, FakeSlack, OfflineTransport
        from riri.llm import DraftService
        from riri.patrol import Patrol
        from riri.delivery_policy import CHANNELS, delivery_content
        from riri.state import StateStore
        cfg = replace(self.cfg, mode="live", expected_bot_id="BTEST", expected_user_id="UTEST",
            instruction_channel=CHANNELS[0], report_channel=CHANNELS[1])
        notion, slack, clock = FakeNotion(cfg), FakeSlack(), Clock()
        class Adapter:
            def read(self):
                return copy.deepcopy(notion.blocks[REQUESTS_PAGE_ID])
            def append(self, value):
                notion.blocks[REQUESTS_PAGE_ID].append(block(value))
        bridge = MamuBridge(cfg, Adapter(), POLICY)
        self.assertEqual(bridge.submit(RID, text())["result"], "saved_verified")
        def run():
            draft = DraftService(OfflineTransport(), enabled=False, report_pages=cfg.pages)
            return Patrol(cfg, slack, notion, draft, clock=clock,
                notifier=lambda: None, unexpected_notifier=lambda: None).run()
        self.assertEqual(run()["status"], "ok")
        posts = [item for item in slack.posts if RID in item[1]]
        self.assertEqual(len(posts), 2)
        self.assertEqual([item[0] for item in posts], [cfg.instruction_channel, cfg.report_channel])
        for _, body, _ in posts:
            self.assertIn("架空ログsample1", body)
            self.assertIn("たかむねへ原因・対処法・必要な判断", body)
            self.assertTrue(body.startswith('<@UICEFAKE>\n'))
        state = StateStore(self.root, 'live').load()
        source = state['outbox']['instruction:' + RID]
        relay = state['outbox']['instruction-gdm:' + RID]
        self.assertEqual(source['status'], 'posted')
        self.assertEqual(relay['status'], 'posted')
        self.assertEqual(delivery_content(source['text'], source['delivery_policy'], cfg.expected_user_id)[1],
            delivery_content(relay['text'], relay['delivery_policy'], cfg.expected_user_id)[1])
        result = bridge.status(RID)
        self.assertEqual(result["stages"]["riri_intake"]["status"], "confirmed")
        self.assertEqual(result["stages"]["ice_delivered"]["status"], "confirmed")
        self.assertFalse(result['stages']['ice_delivered']['evidence']['owner_receipts_recorded'])
        self.assertEqual(result['stages']['ice_delivered']['evidence']['actor_verification'], 'unconfirmed_by_bridge')
        self.assertEqual(result["stages"]["ice_received"]["status"], "unconfirmed")
        for _ in range(2):
            clock.advance()
            self.assertEqual(run()["status"], "ok")
        self.assertEqual(len([item for item in slack.posts if RID in item[1]]), 2)


class TestFixedNotionStore(unittest.TestCase):
    def test_fixed_append_and_full_pagination_readback(self):
        class Fake:
            def __init__(self):
                self.calls = []
            def request(self, method, url, headers=None, payload=None, timeout=None):
                self.calls.append((method, url, payload))
                if method == "PATCH":
                    return {"object": "list", "results": [], "has_more": False}
                if "start_cursor" not in url:
                    heading = {"id": "a" * 32, "type": "heading_2", "heading_2": {
                        "rich_text": [{"text": {"content": "依頼一覧（新しい順）"}}]}}
                    return {"results": [heading], "has_more": True, "next_cursor": "cursor2"}
                return {"results": [block(text())], "has_more": False}
        transport = Fake()
        store = FixedNotionStore(NotionClient("synthetic-token", transport))
        rows = store.read()
        store.append(text())
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.id for r in parse_requests(rows)], [RID])
        self.assertEqual(len(transport.calls), 3)
        for _, url, _ in transport.calls:
            self.assertTrue(url.startswith("https://api.notion.com/v1/blocks/" + REQUESTS_PAGE_ID + "/children"))
        self.assertEqual(len(transport.calls[-1][2]["children"]), 1)
        self.assertEqual(transport.calls[-1][2]["position"], {"type": "after_block", "after_block": {"id": "a" * 32}})

    def test_missing_or_duplicate_anchor_fails_without_patch(self):
        class Client:
            def read_blocks(self, page):
                return []
            def _request(self, *args, **kwargs):
                raise AssertionError("must not write")
        store = FixedNotionStore(Client())
        store.read()
        with self.assertRaises(RemoteError):
            store.append(text())


if __name__ == "__main__":
    unittest.main()
