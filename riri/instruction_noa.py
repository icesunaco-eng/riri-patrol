"""Opt-in, at-most-once wake for newly generated, confirmed instructions.

Keep this extension separate from the historical human-mention state reader.
"""
from datetime import datetime
from .noa import (NoaWake, CHANNEL, stamp, validate_permalink, session_evidence,
                  NoaRejected)
from .errors import SafetyError, StopRequested, TimeBudgetExceeded, AccountMismatch, sanitize
from .eventlog import iso
from .local_noa import LocalNoaClient, any_evidence, local_evidence_installed

LOCAL_INCOMPLETE = ("のあの会話履歴を最後まで確認できなかったため、続きに送るか新しく始めるかを決められず、"
                    "のあへ届けませんでした。自動では再実行しません。必要なら指示を投稿しなおしてください。")
ROUTINE_HELD = ("のあの起動結果が不明のため保留しています。Claudeの予定タスク（routine）の実行履歴とセッションを確認し、"
                "確認結果を記録してください。自動では再実行しません。")


class RecordingClient:
    """Wraps a routine client so each attempt records its transport before I/O."""
    transport = "routine"

    def __init__(self, inner, on_attempt):
        self.inner, self.on_attempt = inner, on_attempt

    def validate(self):
        return self.inner.validate()

    def fire(self, text):
        self.on_attempt({"transport": "routine"})
        return self.inner.fire(text)

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
        if client is None and patrol.config.noa_transport == "local":
            client = LocalNoaClient(patrol.config, guard=patrol.check_time, clock=patrol.clock)
        if isinstance(client, LocalNoaClient):
            client.on_attempt = self._record_attempt
        elif client is not None:
            client = RecordingClient(client, self._record_attempt)
        super().__init__(patrol, client)
        if not isinstance(self.client, (LocalNoaClient, RecordingClient)):
            self.client = RecordingClient(self.client, self._record_attempt)

    def _record_attempt(self, record):
        """Durably note which transport an in-flight attempt used (and a safe
        local reference), so a later held notice points at the right place even
        after NOA_TRANSPORT changes. Only the currently sending item exists."""
        p = self.p
        targets = [i for i in p.state["noa_wake"]["items"].values() if i.get("status") == "sending"]
        targets += [x["noa_instruction_wake"] for x in p.state["outbox"].values()
                    if x.get("noa_instruction_wake", {}).get("status") == "sending"]
        for item in targets:
            if "transport" not in item:
                item["transport"] = record["transport"]
                if record.get("ref"):
                    item["local_ref"] = record["ref"]
        p.save()

    def _held_guidance(self, item):
        """Where to look. Unknown-route records are never asserted to one route."""
        transport = item.get("transport")
        if transport == "local":
            return ("MacのClaude Code（「のあ」フォルダ）の会話と、りりの " + item.get("local_ref", "logs/noa-local") +
                    "（.in.txt／.out.log）")
        if transport is None and self.p.config.noa_transport == "local":
            return ("起動経路を特定できないため、Claudeの予定タスク（routine）の実行履歴と、"
                    "MacのClaude Code（「のあ」フォルダ）の会話・りりの logs/noa-local の両方")
        return None

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
            item.update(status="sending", attempt_at=iso(p.clock()))
            p.save()  # The at-most-once fence must be durable before external I/O.
            try:
                result = self.client.fire(text)
                evidence = any_evidence(result, session_evidence)
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    item.update(status="queued", attempt_at=None)
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
                item.update(status="done", reason="session_verified", evidence=evidence)
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
        if self._instruction_wakes():
            return
        if activated:
            return False
        if len(self._recent_attempts()) >= 10:
            return
        return super()._run()
