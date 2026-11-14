"""Opt-in, at-most-once wake for newly generated, confirmed instructions.

Keep this extension separate from the historical human-mention state reader.
"""
from datetime import datetime
import hashlib
from .compose import safe_reference
from .noa import (NoaWake, CHANNEL, stamp, validate_permalink, session_evidence,
                  NoaRejected)
from .errors import SafetyError, StopRequested, TimeBudgetExceeded, AccountMismatch, sanitize
from .eventlog import iso
from .local_noa import LocalNoaClient, any_evidence, local_evidence_installed

LOCAL_INCOMPLETE = ("のあの会話履歴を最後まで確認できなかったため、続きに送るか新しく始めるかを決められず、"
                    "のあへ届けませんでした。自動では再実行しません。必要なら指示を投稿しなおしてください。")
ROUTINE_HELD = ("のあの起動結果が不明のため保留しています。Claudeの予定タスク（routine）の実行履歴とセッションを確認し、"
                "確認結果を記録してください。自動では再実行しません。")


ROUTE_KEYS = ("transport", "local_ref", "local_run_id", "local_input_sha256", "execution")


def forget_route(item):
    """Drop the route evidence of a previous attempt; True when something changed."""
    changed = False
    for key in ROUTE_KEYS:
        if key in item:
            del item[key]
            changed = True
    return changed


class RecordingClient:
    """Wraps the real client so one fire() is bound to exactly one item: the
    route (and a safe local reference) is recorded on that item only, before
    the external effect can happen."""

    def __init__(self, inner, wake):
        self.inner, self.wake = inner, wake
        if isinstance(inner, LocalNoaClient):
            # Called by the local client after its last not-sent boundary.
            inner.on_attempt = lambda record: wake._record_attempt(record)

    @property
    def transport(self):
        return getattr(self.inner, "transport", "routine")

    def validate(self):
        return self.inner.validate()

    def fire(self, text):
        self.wake._begin_attempt(text)
        try:
            if not isinstance(self.inner, LocalNoaClient):
                self.wake._record_attempt({"transport": "routine"})
            return self.inner.fire(text)
        finally:
            self.wake._attempt_item = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

INSTRUCTION_WAKE_STATES = {"queued", "sending", "done", "held", "rejected", "disabled", "simulated"}


def validate_instruction_wakes(outbox):
    """Optional outbox metadata; old state needs no migration or historical replay."""
    for key, source in outbox.items():
        if "noa_instruction_wake" not in source:
            continue
        item = source["noa_instruction_wake"]
        if (source.get("kind") != "instruction" or source.get("channel") != CHANNEL or
                key != "instruction:" + source.get("request_id", "") or
                not isinstance(item, dict) or item.get("status") not in INSTRUCTION_WAKE_STATES or
                "attempt_at" not in item):
            raise ValueError("Invalid instruction wake intent")
        attempted = item["attempt_at"] is not None
        if attempted:
            if not isinstance(item["attempt_at"], str) or datetime.fromisoformat(item["attempt_at"]).tzinfo is None:
                raise ValueError("Invalid instruction wake attempt")
        if (item["status"] in {"sending", "done", "held"} and not attempted or
                item["status"] in {"queued", "disabled", "simulated"} and attempted):
            raise ValueError("Invalid instruction wake lifecycle")


class InstructionNoaWake(NoaWake):
    def __init__(self, patrol, client=None):
        validate_instruction_wakes(patrol.state["outbox"])
        self.p = patrol
        self._instruction_item = None  # set explicitly around a generated-instruction fire
        self._attempt_item = None      # the single item the current fire() belongs to
        if client is None and patrol.config.noa_transport == "local":
            client = LocalNoaClient(patrol.config, guard=patrol.check_time, clock=patrol.clock)
        if client is not None and not isinstance(client, RecordingClient):
            client = RecordingClient(client, self)
        super().__init__(patrol, client)
        if not isinstance(self.client, RecordingClient):
            self.client = RecordingClient(self.client, self)

    def _begin_attempt(self, text):
        """Identify the one item this fire() is for. The generated-instruction
        path passes its item explicitly. riri/noa.py (pinned) passes only the
        text, so a human mention is identified by the permalink that ends
        exactly this message. Anything else leaves the route unrecorded (shown
        as unknown later) instead of guessing."""
        item = self._instruction_item
        if item is None:
            hits = [i for i in self.s["items"].values()
                    if i.get("status") == "sending" and i.get("source_link") and
                    text.endswith("\nSlack: " + i["source_link"])]
            item = hits[0] if len(hits) == 1 else None
        self._attempt_item = item

    def _record_attempt(self, record):
        """Durably bind this attempt's transport (and safe local reference) to
        the one item being attempted, replacing whatever an earlier attempt
        left, so a later held notice points at the right place even after
        NOA_TRANSPORT changes. Other sending items are never touched."""
        item = self._attempt_item
        if item is None:
            return
        forget_route(item)
        item["transport"] = record["transport"]
        if record.get("ref"):
            item["local_ref"] = record["ref"]
        if record.get("run_id"):
            item["local_run_id"] = record["run_id"]
            item["local_input_sha256"] = record.get("input_sha256")
        self.p.save()

    def finish(self, item, status, reason, evidence=""):
        # The human-mention implementation remains hash-pinned. Widen only
        # this extension's meaning for new, receipt-tracked local starts.
        if (status == "done" and reason == "session_verified" and
                item.get("transport") == "local" and item.get("local_run_id")):
            item.update(status="done", reason="local_spawned")
            self.p.event("のあの起動を受け付けた", result="一部完了", evidence=evidence,
                         memo="実応答・CLI結果・依頼作業の完了は別に確認します。")
            return
        return super().finish(item, status, reason, evidence)

    def reconcile_local_results(self):
        """Observe only new run-bound receipts, never replay an old delivery.

        This is called before new starts. A just-spawned supervisor therefore
        gets until the following patrol to publish its first receipt.
        """
        from .local_outcomes import observe
        from .status import local_wake_items
        p = self.p
        labels = {"rate_limited": "利用上限による中断", "authentication_failed": "認証失敗",
                  "cli_failed": "CLIの異常終了", "cli_spawn_failed": "CLIの起動失敗",
                  "stopped_before_start": "STOPによる実行前の停止", "unknown": "実行結果が不明"}
        for identity, request_id, item in local_wake_items(p.state):
            if not item.get("local_run_id"):
                continue  # Preserve legacy done/reason/evidence byte-for-byte.
            p.check_time()
            observation = observe(p.config.root, item.get("local_ref"))
            if (observation.get("run_id") != item["local_run_id"] or
                    not item.get("local_input_sha256") or
                    observation.get("input_sha256") != item["local_input_sha256"]):
                observation = {"state": "unknown", "outcome": "unknown",
                               "run_id": item["local_run_id"], "partial_work_possible": True}
            # No raw output or arbitrary diagnostic text enters shared state.
            allowed = {"state", "outcome", "run_id", "exit_code", "partial_work_possible",
                       "input_sha256", "started_at", "completed_at", "reason"}
            observation = {k: v for k, v in observation.items() if k in allowed}
            outcome = observation.get("outcome", "unknown")
            changed = item.get("execution") != observation
            item["execution"] = observation
            if changed:
                p.event("のあの実行結果を観測", req_id=request_id,
                        result="一部完了" if outcome in {"pending", "cli_exited"} else "できなかった",
                        memo=("CLI終了（依頼作業の完了は別判定）" if outcome == "cli_exited" else
                              "実行中（依頼作業の完了は未確認）" if outcome == "pending" else
                              labels.get(outcome, "実行結果が不明")))
            if outcome not in {"pending", "cli_exited"}:
                # The stable intent survives crashes and is never a resend of
                # the instruction. It uses the existing Noa notice channel.
                key = "noa-local-outcome:" + hashlib.sha256(
                    (identity + ":" + item["local_run_id"]).encode()).hexdigest()[:24]
                reference = safe_reference(request_id)
                if not request_id:
                    try:
                        reference += "\n" + validate_permalink(item.get("source_link"), CHANNEL, item.get("ts"))
                    except (SafetyError, TypeError):
                        try:
                            stamp(item.get("ts"))
                        except (ValueError, TypeError):
                            pass
                        else:
                            reference += "\n指示メッセージ ts " + item["ts"]
                p.queue(key, p.config.report_channel,
                        "のあの今回の実行を確認しました：" + labels.get(outcome, "実行結果が不明") +
                        "。途中成果や後続の完了報告を確認してください。自動再送しません。\n" +
                        reference,
                        "noa_notice")

    def _recover(self):
        """Run before anything can be sent, separately from sending: every item
        left in sending by an earlier process becomes held (its route is not
        guessed), and items waiting to be tried carry no route of an attempt
        that is known not to have been sent."""
        changed = False
        for item in self.s["items"].values():
            if item["status"] == "sending":
                self.finish(item, "held", "interrupted_send")
            elif item["status"] in {"queued", "retry"}:
                changed = forget_route(item) or changed
        for source in self.p.state["outbox"].values():
            item = source.get("noa_instruction_wake")
            if not isinstance(item, dict):
                continue
            if item.get("status") == "sending":
                item.update(status="held", reason="interrupted_send")
                changed = True
            elif item.get("status") == "queued":
                changed = forget_route(item) or changed
        if changed:
            self.p.save()

    def _forget_unsent(self):
        """After a run: an item back in queued/retry was not sent on this route."""
        changed = False
        for item in self.s["items"].values():
            if item["status"] in {"queued", "retry"}:
                changed = forget_route(item) or changed
        for source in self.p.state["outbox"].values():
            item = source.get("noa_instruction_wake")
            if isinstance(item, dict) and item.get("status") == "queued":
                changed = forget_route(item) or changed
        if changed:
            self.p.save()

    def _held_guidance(self, item):
        """Where to look. Only a recorded routine attempt gets the routine text
        (None); a record without a route is never asserted to one route, whatever
        NOA_TRANSPORT is now."""
        transport = item.get("transport")
        if transport == "routine":
            return None
        if transport == "local":
            return ("MacのClaude Code（「のあ」フォルダ）の会話と、りりの " + item.get("local_ref", "logs/noa-local") +
                    "（.in.txt／.out.log）")
        return ("起動経路を特定できないため、Claudeの予定タスク（routine）の実行履歴と、"
                "MacのClaude Code（「のあ」フォルダ）の会話・りりの logs/noa-local の両方")

    def notices(self):
        # Same as NoaWake.notices (riri/noa.py is pinned) except held/local texts.
        for key, item in self.s["items"].items():
            reason = item.get("reason")
            if item["status"] not in {"held", "rejected"} or reason == "disabled_before_send":
                continue
            if reason in {"invalid_configuration", "authentication"}:
                if "notice_episode" not in item:
                    self._record_component_intent(item, reason)
                    self.p.save()
                failure = "configuration" if reason == "invalid_configuration" else "authentication"
                self._queue_component_notice(item["notice_episode"], failure, item)
                continue
            if item["status"] == "held":
                where = self._held_guidance(item)
                text = ROUTINE_HELD if where is None else (
                    "のあの起動結果が不明のため保留しています。" + where + "を確認し、確認結果を記録してください。自動では再実行しません。")
            elif reason in {"source_changed", "source_unavailable_or_changed"}:
                text = "元の指示が変更または削除されたため、のあを起動しませんでした。必要なら新しい指示を投稿してください。"
            elif reason in {"source_contains_credential", "source_changed_or_contains_credential"}:
                text = "指示に認証情報が含まれる可能性があるため、のあへ転送しませんでした。認証情報を除いた新しい指示を投稿してください。"
            elif reason == "local_search_incomplete":
                text = LOCAL_INCOMPLETE
            else:
                text = "のあの起動要求が拒否されました。起動設定と制限を確認してください。自動では再実行しません。"
            source = "指示メッセージ ts " + item["ts"]
            if item.get("source_link"):
                source = validate_permalink(item["source_link"], CHANNEL, item["ts"])
            self.p.queue("noa-notice:" + key, self.p.config.report_channel,
                         text + "\n" + source, "noa_notice")
        for reason, episode in self.s["failure_episodes"].items():
            self._queue_component_notice(episode["serial"], reason)

    def _instruction_notice(self, source, item):
        text = "りりからの指示による、のあの起動を確認できません。自動再送しません。"
        if item.get("reason") == "local_search_incomplete":
            text = LOCAL_INCOMPLETE
        elif item["status"] == "held":
            where = self._held_guidance(item)
            if where is not None:
                text += "確認先：" + where + "。"
        self.p.queue("noa-instruction:" + source["request_id"], self.p.config.report_channel,
                     text + "参照：" + source["marker"], "noa_notice")

    def run(self):
        self.reconcile_local_results()
        with local_evidence_installed(self.p.config.noa_transport == "local"):
            return super().run()

    def sync_enabled(self):
        if not self.p.config.noa_wake_enabled:
            for source in self.p.state["outbox"].values():
                item = source.get("noa_instruction_wake", {})
                if item.get("status") == "queued":
                    item.update(status="disabled", reason="disabled_before_send")
                elif item.get("status") == "sending":
                    item.update(status="held", reason="interrupted_send")
            self.p.save()
        return super().sync_enabled()

    def _recent_attempts(self):
        times = self.s["attempts"] + [source["noa_instruction_wake"]["attempt_at"]
            for source in self.p.state["outbox"].values()
            if source.get("noa_instruction_wake", {}).get("attempt_at") is not None]
        return [t for t in times if (self.p.clock() - datetime.fromisoformat(t)).total_seconds() < 3600]

    def _instruction_wakes(self):
        """Consume only locally-created instruction intents, never bot history.

        The instruction key is the request-ID dedup record. Sending is fsynced
        before fire; even 429/rejection never retries a generated instruction.
        Old outbox entries without this opt-in are deliberately not replayed.
        """
        p = self.p
        validate_instruction_wakes(p.state["outbox"])
        for key, source in list(p.state["outbox"].items()):
            item = source.get("noa_instruction_wake")
            if item is None:
                continue
            if item["status"] == "sending":
                item.update(status="held", reason="interrupted_send")
                p.save()
            if item["status"] in {"held", "rejected"}:
                self._instruction_notice(source, item)
            if item["status"] != "queued" or source.get("status") != "posted":
                continue
            info = p.state["requests"].get(source["request_id"], {})
            if ("のあ" not in info.get("recipients", []) or
                    source.get("channel") != p.config.instruction_channel or
                    not isinstance(source.get("text"), str) or
                    sanitize(source["text"], p.config.secrets) != source["text"]):
                item.update(status="rejected", reason="invalid_source")
                p.save()
                continue
            p.check_time()
            if p.config.mode == "practice":
                item.update(status="simulated", reason="practice_no_post")
                p.save()
                continue
            if len(self._recent_attempts()) >= 10:
                return False
            self.client.validate()
            stamp(source.get("ts"))
            link = validate_permalink(p.slack.permalink(CHANNEL, source["ts"]), CHANNEL, source["ts"])
            text = "りりからの依頼（依頼ID: " + source["request_id"] + "）\nメッセージ:\n" + source["text"] + "\nSlack: " + link
            if sanitize(text, p.config.secrets) != text:
                item.update(status="rejected", reason="invalid_source")
                p.save()
                continue
            p.check_time()
            forget_route(item)  # Same write as the fence: no earlier attempt's route survives.
            item.update(status="sending", attempt_at=iso(p.clock()))
            p.save()  # The at-most-once fence must be durable before external I/O.
            self._instruction_item = item  # The one item this fire() is for.
            try:
                try:
                    result = self.client.fire(text)
                finally:
                    self._instruction_item = None
                evidence = any_evidence(result, session_evidence)
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    item.update(status="queued", attempt_at=None)
                    forget_route(item)
                else:
                    item.update(status="held", reason="interrupted_send")
                p.save()
                raise
            except NoaRejected as exc:
                item.update(status="rejected", reason=exc.reason if exc.reason.startswith("local_") else "explicit_rejection")
            except AccountMismatch:
                item.update(status="held", reason="interrupted_send")
                p.save()
                raise
            except Exception:
                item.update(status="held", reason="unknown_outcome")
            else:
                reason = "local_spawned" if item.get("local_run_id") else "session_verified"
                item.update(status="done", reason=reason, evidence=evidence)
                self._healthy_fire = True
            p.save()
            if item["status"] != "done":
                self._partial_failure = True
                self._instruction_notice(source, item)
            return True  # One fire per patrol, shared with the human-mention path.
        return False


    def _run(self):
        activated = self.sync_enabled()
        if not self.p.config.noa_wake_enabled:
            return False
        self.p.check_time()
        if self.p.config.instruction_channel != CHANNEL:
            raise SafetyError("Noa wake requires the approved instruction channel")
        if activated:
            # Preserve the existing OFF/downtime boundary: only instructions
            # created or confirmed during this patrol may cross the activation fence.
            # A newly queued original may still await Slack confirmation.
            for source in self.p.state["outbox"].values():
                item = source.get("noa_instruction_wake", {})
                if item.get("status") != "queued":
                    continue
                current = False
                for field in ("created_at", "posted_at"):
                    try:
                        current = current or datetime.fromisoformat(source[field]) >= self.p.started_at
                    except (KeyError, TypeError, ValueError):
                        pass
                if not current:
                    item.update(status="disabled", reason="stale_before_send")
            self.p.save()
        self._recover()
        # Tell the owner about held/rejected human mentions now: the generated
        # instruction path below may return early (a send, or the hourly
        # limit) on every patrol, and the notice must not wait for it.
        self.notices()
        try:
            if self._instruction_wakes():
                return
            if activated:
                return False
            if len(self._recent_attempts()) >= 10:
                return
            return super()._run()
        finally:
            self._forget_unsent()
