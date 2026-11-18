import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.errors import AccountMismatch, NotionConflict, RemoteError
from riri.llm import DraftService
from riri.patrol import Patrol
from riri.state import StateStore, initial_state
from riri.parse import replace_status
from riri.notion import STATUS
from riri.lock import patrol_lock
from riri.eventlog import LocalLogs
from riri.errors import TimeBudgetExceeded
from riri.runtime import hard_deadline
import time


def block(text, id="request-block", kind="code"):
    return {"id": id, "type": kind, kind: {"rich_text": [{"text": {"content": text}}]}}


def request(number=1, recipients="@うさ", status="未着手", task="手順を確認する"):
    return block(f"【りりへの依頼】\n依頼ID：💛20990101-{number}\n依頼した子：うさ\n"
                 f"宛先：{recipients}\n状態：{status}\nやること：{task}\n手順：\n1. 読んで確かめる\n"
                 "期限・実行タイミング：今日中\nGitHubプロジェクト：なし\n", f"request-{number}")


def report(number=1, name="うさ", result="完了", id="report-1"):
    return block(f"【完了報告】\n依頼ID：💛20990101-{number}\n報告した子：{name}\n"
                 f"結果：{result}\n"
                 "作業場所URL：https://chatgpt.com/c/11111111-1111-4111-8111-111111111111\n"
                 "やったこと：模擬作業を確認した", id, "paragraph")


class Clock:
    def __init__(self):
        self.value = datetime(2099, 1, 1, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, seconds=300):
        self.value += timedelta(seconds=seconds)


class FakeSlack:
    def __init__(self):
        self.verified = False
        self.posts = []
        self.auth_calls = 0
        self.error = None
        self.reject_account = False
        self.history = []
        self.find_calls = 0
    def auth_test(self):
        self.auth_calls += 1
        if self.reject_account:
            raise AccountMismatch("mock identity mismatch")
        self.verified = True
        return {"bot_id": "BTEST", "user_id": "UTEST"}
    def post(self, channel, text, *, dry_run=False):
        if not self.verified:
            raise AssertionError("unverified write")
        if dry_run:
            return None
        if self.error:
            error, self.error = self.error, None
            raise error
        ts = str(len(self.posts) + 1) + ".000001"
        self.posts.append((channel, text, ts))
        self.history.append((channel, text, ts))
        return ts
    def find_post(self, channel, marker, oldest):
        self.find_calls += 1
        return next((ts for ch, text, ts in self.history if ch == channel and marker in text.splitlines()), None)


class FakeNotion:
    def __init__(self, config):
        self.config = config
        self.blocks = {config.requests_page: []}
        self.blocks.update({url.split("/")[-1]: [] for url in config.pages.values()})
        self.writes = []
        self.reads = []
        self.rows = []
        self.append_error = None
        self.read_error = None
        self.status_error_after_apply = False
    def read_blocks(self, page):
        self.reads.append(page)
        if self.read_error:
            raise self.read_error
        return copy.deepcopy(self.blocks[page])
    def set_status(self, block_id, original_text, status, *, dry_run=False, audit=None, expected_last_edited_time=None, practice_status=None):
        for blk in self.blocks[self.config.requests_page]:
            if blk["id"] == block_id:
                current = blk["code"]["rich_text"][0]["text"]["content"]
                if current != original_text:
                    raise AssertionError("stale write")
                matches = list(STATUS.finditer(current))
                previous = matches[0].group(2) if len(matches) == 1 else None
                if practice_status is not None:
                    if not dry_run:
                        raise AssertionError("virtual state not allowed live")
                    previous = practice_status
                if previous != status and (previous, status) not in (("未着手", "対応中"), ("対応中", "完了")):
                    from riri.errors import SafetyError
                    raise SafetyError("Disallowed status transition")
                if dry_run:
                    return
                blk["code"]["rich_text"][0]["text"]["content"] = replace_status(current, status)
                self.writes.append(("status", block_id, status))
                if self.status_error_after_apply:
                    self.status_error_after_apply = False
                    raise NotionConflict("status outcome could not be verified")
                return
        raise AssertionError("missing block")
    def find_log_table(self, page):
        return "table", "header", self.rows
    def read_log_rows(self, table):
        return copy.deepcopy(self.rows)
    def append_rows(self, table, header, rows, *, dry_run=False):
        if dry_run:
            return
        if self.append_error:
            raise self.append_error
        self.writes.append(("log", len(rows)))
        self.rows[0:0] = copy.deepcopy(rows)


class OfflineTransport:
    def request(self, *args, **kwargs):
        raise AssertionError("LLM should be disabled")


class FlowTests(unittest.TestCase):
    def setUp(self):
        # Test artifacts are intentionally retained, not automatically deleted.
        self.root = Path(tempfile.mkdtemp(prefix="riri-patrol-test-"))
        self.cfg = Config(root=self.root, llm_enabled=False, expected_bot_id="BTEST", expected_user_id="UTEST", ice_mention_user_id="U0TESTICE000")
        self.clock, self.slack = Clock(), FakeSlack()
        self.notion = FakeNotion(self.cfg)
        self.notices = []
        self.no_network = patch("socket.socket", side_effect=AssertionError("network forbidden during offline tests"))
        self.no_network.start()
    def tearDown(self):
        self.no_network.stop()
    def run_once(self, live=False, flush=False, **kwargs):
        cfg = replace(self.cfg, mode="live" if live else "practice")
        draft = DraftService(OfflineTransport(), enabled=False, report_pages=cfg.pages)
        return Patrol(cfg, self.slack, self.notion, draft, clock=self.clock,
                      notifier=lambda: self.notices.append(1), **kwargs).run(flush_log=flush)
    def state(self, live=False):
        return StateStore(self.root, "live" if live else "practice").load()
    def add_request(self, *args, **kwargs):
        self.notion.blocks[self.cfg.requests_page].insert(0, request(*args, **kwargs))
    def add_report(self, name="うさ", **kwargs):
        self.notion.blocks[self.cfg.pages[name].split("/")[-1]].insert(0, report(name=name, **kwargs))

    def test_stop_and_lock_do_not_touch_clients_or_state(self):
        (self.root / "STOP").touch()
        self.assertEqual(self.run_once()["status"], "stopped")
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(self.slack.auth_calls, 0)
        self.assertEqual(self.notion.reads, [])
        other = self.root / "other"
        self.cfg = replace(self.cfg, root=other)
        with patrol_lock(other):
            self.assertEqual(self.run_once()["status"], "locked")
        self.assertEqual(self.slack.auth_calls, 0)

    def test_default_practice_no_external_writes_and_deduplicates(self):
        self.add_request()
        for _ in range(5):
            self.assertEqual(self.run_once()["status"], "ok")
            self.clock.advance()
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.notion.writes, [])
        lines = [json.loads(line) for line in (self.root / "logs/practice.jsonl").read_text().splitlines()]
        self.assertEqual(sum(r["operation"] == "slack_post" for r in lines), 1)
        self.assertEqual(sum(r["operation"] == "notion_status" for r in lines), 1)
        self.assertFalse((self.root / "state/state.json").exists())

    def test_live_posts_exactly_once_oldest_first_one_per_run(self):
        for n in range(1, 4): self.add_request(n)
        for n in range(1, 4):
            result = self.run_once(live=True)
            self.assertEqual(result["instructions"], 1)
            self.assertIn(f"依頼ID：💛20990101-{n}", self.slack.posts[-1][1])
            self.clock.advance()
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 3)

    def test_practice_then_live_isolated(self):
        self.add_request()
        self.run_once()
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.state()["mode"], "practice")
        self.assertEqual(self.state(True)["mode"], "live")

    def test_account_mismatch_no_post_or_notion_and_rate_limited_event(self):
        self.slack.reject_account = True
        for _ in range(2):
            self.assertEqual(self.run_once(live=True)["status"], "account_mismatch")
            self.clock.advance()
        self.assertEqual(len(self.state(True)["log_buffer"]), 1)
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.notion.reads, [])

    def test_duplicate_id_stops_before_any_state_patch(self):
        self.add_request()
        self.add_request()
        result = self.run_once(live=True)
        self.assertEqual(result['status'], 'held')
        self.assertEqual(self.notion.writes, [])
        self.assertEqual(len(self.slack.posts), 1)  # source notice only, no instruction

    def test_self_and_mini_only_once_and_total_budget(self):
        self.add_request(recipients="@りり")
        for _ in range(5): self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.notion.writes, [])
        self.add_request(2, recipients="@みに @りり")
        result = self.run_once(live=True)
        self.assertLessEqual(result["posts"], 4)
        for _ in range(5): self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 5)  # self, mixed self, instruction, relay, mini

    def test_initial_reports_suppressed_then_all_recipients_complete(self):
        self.add_request(recipients="@あいす @かれん")
        self.add_report(name="うさ", number=999, id="old")
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 2)  # instruction plus GDM relay
        self.add_report(name="あいす", id="aisu")
        self.add_report(name="かれん", id="karen", result="一部完了")
        self.run_once(live=True)
        self.assertEqual(self.state(True)["requests"]["💛20990101-1"]["status"], "posted")
        self.notion.blocks[self.cfg.pages["かれん"].split("/")[-1]] = [report(name="かれん", id="karen")]
        self.run_once(live=True)
        self.assertEqual(self.state(True)["requests"]["💛20990101-1"]["status"], "done")

    def test_uncertain_send_holds_no_resend_even_after_24h(self):
        self.add_request()
        self.slack.error = RemoteError("ambiguous", uncertain=True)
        self.run_once(live=True)
        self.assertEqual(self.state(True)["requests"]["💛20990101-1"]["status"], "held")
        self.clock.advance(90000)
        for _ in range(3): self.run_once(live=True)
        instructions = [text for ch, text, _ in self.slack.posts if ch == self.cfg.instruction_channel]
        self.assertEqual(instructions, [])
        self.assertTrue(any("確認してください" in text for _, text, _ in self.slack.posts))
        self.assertFalse(any("直りました" in text for _, text, _ in self.slack.posts))

    def test_uncertain_send_confirmed_by_history_no_second_post(self):
        self.add_request()
        self.slack.error = RemoteError("ambiguous", uncertain=True)
        self.run_once(live=True)
        state = self.state(True)
        item = state["outbox"]["instruction:💛20990101-1"]
        # Simulate a process crash while the post was in flight, before held was recorded.
        item["status"] = "sending"
        StateStore(self.root, "live").save(state)
        self.slack.history.append((item["channel"], item["text"], "99.000001"))
        self.run_once(live=True)
        info = self.state(True)["requests"]["💛20990101-1"]
        self.assertEqual(info["slack_ts"], "99.000001")
        self.assertEqual(info["status"], "posted")

    def test_uncertain_status_update_holds_without_automatic_dispatch(self):
        self.add_request()
        self.notion.status_error_after_apply = True
        self.run_once(live=True)
        self.assertEqual(self.state(True)["requests"]["💛20990101-1"]["status"], "held")
        self.run_once(live=True)
        self.assertEqual(len([p for p in self.slack.posts if p[0] == self.cfg.instruction_channel]), 0)

    def test_no_event_when_idle_flush_after_three_hours_newest_first(self):
        self.run_once(live=True)
        self.assertEqual(self.state(True)["log_buffer"], [])
        self.add_request()
        self.run_once(live=True)
        self.assertEqual(len(self.notion.rows), 0)
        self.clock.advance(10800)
        self.run_once(live=True)
        self.assertEqual(len(self.notion.rows), 2)
        self.run_once(live=True, flush=True)
        self.assertEqual(len(self.notion.rows), 2)
        self.assertEqual(self.state(True)["log_buffer"], [])

    def test_uncertain_log_append_stays_buffered_and_held(self):
        self.add_request()
        self.run_once(live=True)
        self.notion.append_error = RemoteError("ambiguous append", uncertain=True)
        self.run_once(live=True, flush=True)
        self.assertIsNotNone(self.state(True)["log_batch"])
        self.notion.append_error = None
        self.run_once(live=True, flush=True)
        self.assertEqual(self.notion.rows, [])
        self.assertGreater(len(self.state(True)["log_buffer"]), 0)

    def test_corrupt_state_does_not_reset_and_no_network(self):
        self.root.joinpath("state").mkdir()
        self.root.joinpath("state/state.json").write_text("{not-json")
        self.assertEqual(self.run_once(live=True)["status"], "state_error")
        self.assertEqual(self.slack.auth_calls, 0)

    def test_invalid_request_skipped_once_and_fix_rechecked(self):
        self.add_request(recipients="")
        for _ in range(3): self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 1)
        self.notion.blocks[self.cfg.requests_page] = [request()]
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 2)

    def test_log_retention_asks_without_deleting(self):
        self.cfg = replace(self.cfg, log_size_notice_bytes=1)
        self.run_once(live=True)
        kept = self.root / "logs/old.log"
        kept.write_text("keep me")
        self.clock.advance(31 * 86400)
        for _ in range(2): self.run_once(live=True)
        self.assertEqual(kept.read_text(), "keep me")
        self.assertEqual(sum("整理するか" in x[1] for x in self.slack.posts), 1)

    def test_queued_instruction_revalidates_edits_cancellation_and_removal(self):
        self.add_request()
        self.slack.error = RemoteError("explicit rejected", uncertain=False)
        self.run_once(live=True)
        original = copy.deepcopy(self.notion.blocks[self.cfg.requests_page])
        variants = [[], [request(status="完了")], [request(status="対応中", task="変更された依頼")]]
        for variant in variants:
            self.notion.blocks[self.cfg.requests_page] = variant
            self.run_once(live=True)
            self.assertFalse([p for p in self.slack.posts if p[0] == self.cfg.instruction_channel])
        self.notion.blocks[self.cfg.requests_page] = original
        self.run_once(live=True)
        self.assertEqual(len([p for p in self.slack.posts if p[0] == self.cfg.instruction_channel]), 1)

    def test_read_error_never_drains_queued_instruction(self):
        self.add_request()
        self.slack.error = RemoteError("explicit rejected", uncertain=False)
        self.run_once(live=True)
        self.notion.read_error = RemoteError("read failed")
        self.run_once(live=True)
        self.assertFalse([p for p in self.slack.posts if p[0] == self.cfg.instruction_channel])

    def test_historical_report_id_collision_stops_new_dispatch(self):
        self.add_request()
        self.add_report()
        result = self.run_once(live=True)
        self.assertEqual(result["error"], "DuplicateRequestId")
        self.assertFalse([p for p in self.slack.posts if p[0] == self.cfg.instruction_channel])
        self.assertEqual(self.notion.writes, [])

    def test_report_ack_crash_is_atomic_and_file_log_recovers(self):
        # These completed originals bind the two reports to their requester
        # without introducing unrelated new instruction deliveries.
        self.add_request(2, recipients="@うさ", status="完了")
        self.add_request(3, recipients="@あいす", status="完了")
        self.run_once(live=True)
        self.add_report(name="うさ", number=2, id="r2")
        self.add_report(name="あいす", number=3, id="r3")
        class CrashLogs(LocalLogs):
            def event(inner, event):
                if event["row"][5] == "たかむねりりあいすに報告":
                    raise KeyboardInterrupt("simulated power loss after state commit")
                super().event(event)
        with self.assertRaises(KeyboardInterrupt):
            self.run_once(live=True, logs=CrashLogs(self.root))
        state = self.state(True)
        self.assertIn("r2", state["seen_reports"])
        self.assertIn("r3", state["seen_reports"])
        bundle = next(iter(state["report_deliveries"].values()))
        self.assertEqual(bundle["status"], "posted")
        receipts = [state["outbox"][key] for key in bundle["keys"]]
        self.assertEqual({item["channel"] for item in receipts},
                         {self.cfg.instruction_channel, self.cfg.report_channel})
        self.assertTrue(all(item["status"] == "posted" and item["ts"] for item in receipts))
        before = len(self.slack.posts)
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), before)
        events = [json.loads(line) for line in (self.root / "logs/events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["row"][5] == "たかむねりりあいすに報告" for e in events), 2)

    def test_instruction_ack_crash_has_durable_posting_event(self):
        self.add_request()
        class CrashLogs(LocalLogs):
            def event(inner, event):
                if event["row"][5] == "#りりの指示部屋に投稿":
                    raise KeyboardInterrupt("simulated power loss")
                super().event(event)
        with self.assertRaises(KeyboardInterrupt):
            self.run_once(live=True, logs=CrashLogs(self.root))
        state = self.state(True)
        self.assertEqual(state["requests"]["💛20990101-1"]["status"], "posted")
        self.assertEqual(sum(e["row"][5] == "#りりの指示部屋に投稿" for e in state["log_buffer"]), 1)
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), 1)

    def test_budget_exhaustion_does_not_drop_mini_notice(self):
        self.cfg = replace(self.cfg, max_posts=1)
        self.add_request(recipients="@みに")
        first = self.run_once(live=True)
        self.assertEqual(first["posts"], 1)
        self.assertEqual(self.state(True)["outbox"]["mini:💛20990101-1"]["status"], "queued")
        second = self.run_once(live=True)
        self.assertEqual(second["posts"], 1)
        self.assertIn("【りりからのお願い】", self.slack.posts[-1][1])
        third = self.run_once(live=True)
        self.assertEqual(third["posts"], 1)
        self.assertIn("みにへの依頼", self.slack.posts[-1][1])

    def test_hard_deadline_interrupts_blocking_work(self):
        start = time.monotonic()
        with self.assertRaises(TimeBudgetExceeded):
            with hard_deadline(0.03):
                time.sleep(0.5)
        self.assertLess(time.monotonic() - start, 0.3)

    def test_completion_refuses_changed_dispatched_source(self):
        self.add_request()
        self.run_once(live=True)
        self.add_report()
        original = copy.deepcopy(self.notion.blocks[self.cfg.requests_page])
        changed = request(status="対応中", task="別の依頼内容")
        replacement = copy.deepcopy(original[0])
        replacement["id"] = "replacement-block"
        snapshot = copy.deepcopy(self.state(True)["requests"]["💛20990101-1"]["snapshot"])
        for variant in ([changed], [replacement]):
            self.notion.blocks[self.cfg.requests_page] = variant
            result = self.run_once(live=True)
            self.assertEqual(result["status"], "ok")
            self.assertNotIn("error", result)
            self.assertEqual(result["completion_reviews"], 1)
            info = self.state(True)["requests"]["💛20990101-1"]
            self.assertEqual(info["status"], "posted")
            self.assertEqual(info["completion_hold"]["status"], "pending")
            self.assertEqual(info["snapshot"], snapshot)
            self.assertNotIn(("status", "request-1", "完了"), self.notion.writes)
        self.notion.blocks[self.cfg.requests_page] = original
        self.run_once(live=True)
        self.assertIn(("status", "request-1", "完了"), self.notion.writes)
        self.assertEqual(self.state(True)["requests"]["💛20990101-1"]["completion_hold"]["status"], "resolved")

    def test_report_version_reversion_never_repeats_seen_pair(self):
        self.add_request(recipients="@うさ @あいす", status="完了")
        self.run_once(live=True)
        self.add_report(name="うさ", id="a")
        self.add_report(name="あいす", id="b")
        self.run_once(live=True)
        page = self.cfg.pages["うさ"].split("/")[-1]
        original = copy.deepcopy(self.notion.blocks[page])
        text = self.notion.blocks[page][0]["paragraph"]["rich_text"][0]["text"]
        text["content"] += "（更新）"
        self.run_once(live=True)
        before = len(self.slack.posts)
        self.notion.blocks[page] = original
        self.run_once(live=True)
        self.assertEqual(len(self.slack.posts), before)
        self.assertEqual(len(self.state(True)["seen_report_versions"]["a"]), 2)


class ConfigTests(unittest.TestCase):
    def test_invalid_mode_and_numeric_caps(self):
        root = Path(tempfile.mkdtemp(prefix="riri-config-test-"))
        for env in ({}, {"RIRI_MODE": "bad"}, {"RIRI_MODE": "LIVE"}):
            values = dict(env, EXPECTED_SLACK_BOT_ID="BTEST", EXPECTED_SLACK_USER_ID="UTEST")
            (root / ".env").write_text("\n".join(k + "=" + v for k, v in values.items()))
            self.assertEqual(Config.load(root, environ=env).mode, "practice")
        (root / ".env").write_text("EXPECTED_SLACK_BOT_ID=BTEST\nEXPECTED_SLACK_USER_ID=UTEST\nRIRI_MODE=live\nMAX_POSTS_PER_RUN=100\nRUN_TIMEOUT_SEC=999\n")
        cfg = Config.load(root, True, {"RIRI_MODE": "live", "MAX_POSTS_PER_RUN": "100", "RUN_TIMEOUT_SEC": "999"})
        self.assertEqual((cfg.mode, cfg.max_posts, cfg.run_timeout), ("practice", 4, 240))


if __name__ == "__main__": unittest.main()

