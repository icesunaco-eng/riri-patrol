"""Opt-in, at-most-once wake for newly generated, confirmed instructions.

Keep this extension separate from the historical human-mention state reader.
"""
from datetime import datetime
from .noa import (NoaWake, CHANNEL, stamp, validate_permalink, session_evidence,
                  NoaRejected)
from .errors import SafetyError, StopRequested, TimeBudgetExceeded, AccountMismatch, sanitize
from .eventlog import iso

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
        super().__init__(patrol, client)

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
                p.queue("noa-instruction:" + source["request_id"], p.config.report_channel,
                    "りりからの指示による、のあの起動を確認できません。自動再送しません。参照：" + source["marker"],
                    "noa_notice")
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
                evidence = session_evidence(result)
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    item.update(status="queued", attempt_at=None)
                else:
                    item.update(status="held", reason="interrupted_send")
                p.save()
                raise
            except NoaRejected:
                item.update(status="rejected", reason="explicit_rejection")
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
                p.queue("noa-instruction:" + source["request_id"], p.config.report_channel,
                    "りりからの指示による、のあの起動を確認できません。自動再送しません。参照：" + source["marker"],
                    "noa_notice")
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
