"""Opt-in routine wake. No automatic retry of an ambiguous execution.

This isolated transport does not extend the generic HTTP client's allowlist.
The response contract is mock-tested, not verified against a live routine API.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import hashlib
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from .errors import AccountMismatch, SafetyError, StopRequested, TimeBudgetExceeded, sanitize
from .eventlog import iso, new_event
from .http import NoRedirect

CHANNEL = "C0CFM2HRA5V"
STATES = {"queued", "retry", "sending", "done", "held", "rejected", "simulated", "reviewed"}
TS = re.compile(r"[0-9]{1,16}\.[0-9]{1,6}\Z")


def stamp(value):
    if not isinstance(value, str) or not TS.fullmatch(value):
        raise ValueError("Invalid timestamp")
    return Decimal(value)


LEGACY_KEYS = {"cutoff", "watermark", "items", "attempts"}
LIFECYCLE_KEYS = {"enabled", "origin_cutoff", "off_at", "failure_reason", "failure_serial"}
RECOVERY_KEYS = {"last_run_at", "stale_windows", "failure_episodes"}
FAILURES = {"configuration", "authentication", "component_unavailable"}
MAX_RUN_GAP = 15 * 60
RECOVERY_SECONDS = 60 * 60


def migrate_state(state):
    """Validate before migration; legacy activation history is not proof of ON."""
    validate_state(state)
    if set(state) == LEGACY_KEYS:
        state.update(enabled=False, origin_cutoff=state["cutoff"], off_at=None,
                     failure_reason=None, failure_serial=0)
    if "last_run_at" not in state:
        state.update(last_run_at=None, stale_windows=[], failure_episodes={})
        if state["failure_reason"] is not None:
            state["failure_episodes"][state["failure_reason"]] = {
                "serial": state["failure_serial"], "success_since": None, "last_success": None}
        for item in state["items"].values():
            if "notice_episode" in item:
                reason = "configuration" if item["reason"] == "invalid_configuration" else "authentication"
                previous = state["failure_episodes"].get(reason, {}).get("serial", 0)
                if item["notice_episode"] > previous:
                    state["failure_episodes"][reason] = {"serial": item["notice_episode"],
                        "success_since": None, "last_success": None}
    return state


def validate_state(state):
    """Corrupt dedup or quota records may never silently reset to first activation."""
    if not isinstance(state, dict) or set(state) not in (LEGACY_KEYS, LEGACY_KEYS | LIFECYCLE_KEYS,
            LEGACY_KEYS | LIFECYCLE_KEYS | RECOVERY_KEYS):
        raise ValueError("Invalid wake state")
    if "last_run_at" in state:
        def aware(value):
            if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError("Invalid wake recovery time")
        if state["last_run_at"] is not None:
            aware(state["last_run_at"])
        if not isinstance(state["failure_episodes"], dict) or not set(state["failure_episodes"]) <= FAILURES:
            raise ValueError("Invalid wake failure episodes")
        for episode in state["failure_episodes"].values():
            if (not isinstance(episode, dict) or set(episode) != {"serial", "success_since", "last_success"} or
                    type(episode["serial"]) is not int or not 1 <= episode["serial"] <= state["failure_serial"]):
                raise ValueError("Invalid wake failure episode")
            if (episode["success_since"] is None) != (episode["last_success"] is None):
                raise ValueError("Invalid wake success window")
            if episode["success_since"] is not None:
                aware(episode["success_since"])
                aware(episode["last_success"])
                if datetime.fromisoformat(episode["success_since"]) > datetime.fromisoformat(episode["last_success"]):
                    raise ValueError("Invalid wake success window")
        if not isinstance(state["stale_windows"], list):
            raise ValueError("Invalid wake stale windows")
        for window in state["stale_windows"]:
            if not isinstance(window, dict) or set(window) != {"start", "end", "retired", "count"}:
                raise ValueError("Invalid wake stale window")
            stamp(window["start"])
            if window["end"] is not None:
                if stamp(window["end"]) < stamp(window["start"]):
                    raise ValueError("Invalid wake stale window order")
            if (not isinstance(window["retired"], list) or
                    any(not isinstance(k, str) or not isinstance(state["items"], dict) or
                        k not in state["items"] or not isinstance(state["items"][k], dict) or
                        state["items"][k].get("reason") != "disabled_before_send" for k in window["retired"]) or
                    len(set(window["retired"])) != len(window["retired"]) or
                    (window["count"] is not None and (type(window["count"]) is not int or
                     window["count"] < len(window["retired"]) or window["end"] is None))):
                raise ValueError("Invalid wake stale count")
    if "enabled" in state:
        if (type(state["enabled"]) is not bool or type(state["failure_serial"]) is not int or
                state["failure_serial"] < 0 or
                (state["failure_reason"] is not None and state["failure_serial"] == 0) or state["failure_reason"] not in
                {None, "configuration", "authentication", "component_unavailable"}):
            raise ValueError("Invalid wake lifecycle")
        for key in ("origin_cutoff", "off_at"):
            if state[key] is not None:
                stamp(state[key])
        if (state["origin_cutoff"] is None) != (state["cutoff"] is None):
            raise ValueError("Invalid wake origin")
        if state["cutoff"] is not None and stamp(state["origin_cutoff"]) > stamp(state["cutoff"]):
            raise ValueError("Invalid wake origin")
        if state["enabled"] and state["cutoff"] is None:
            raise ValueError("Enabled wake without cutoff")
    for key in ("cutoff", "watermark"):
        if state[key] is not None:
            stamp(state[key])
    if (state['cutoff'] is None) != (state['watermark'] is None):
        raise ValueError("Invalid wake cutoff")
    if state['cutoff'] is not None and stamp(state['cutoff']) > stamp(state['watermark']):
        raise ValueError("Invalid wake watermark")
    if not isinstance(state["items"], dict) or not isinstance(state["attempts"], list):
        raise ValueError("Invalid wake records")
    if state['cutoff'] is None and (state['items'] or state['attempts']):
        raise ValueError("Wake records without activation cutoff")
    for attempt in state["attempts"]:
        if not isinstance(attempt, str) or datetime.fromisoformat(attempt).tzinfo is None:
            raise ValueError("Invalid attempt time")
    counts = {"queued": {0}, "retry": {1}, "sending": {1, 2}, "done": {1, 2},
              "held": {1, 2}, "rejected": {0, 1, 2}, "simulated": {0}, "reviewed": {1, 2}}
    total = 0
    for key, item in state["items"].items():
        if (not isinstance(item, dict) or item.get("status") not in STATES or
                item.get("channel") != CHANNEL or not isinstance(item.get("ts"), str) or
                key != CHANNEL + ":" + item["ts"] or
                type(item.get("attempt_count")) is not int or
                item["attempt_count"] not in counts[item["status"]] or
                not isinstance(item.get("source_digest"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["source_digest"]) or
                not isinstance(item.get("source_user"), str) or
                not re.fullmatch(r"U[A-Z0-9]+", item["source_user"])):
            raise ValueError("Invalid wake item")
        if "notice_episode" in item:
            if (type(item["notice_episode"]) is not int or item["notice_episode"] < 1 or
                    item["notice_episode"] > state.get("failure_serial", 0) or
                    item.get("reason") not in {"invalid_configuration", "authentication"}):
                raise ValueError("Invalid component notice intent")
        if "source_link" in item:
            try:
                validate_permalink(item["source_link"], CHANNEL, item["ts"])
            except SafetyError:
                raise ValueError("Invalid stored source link") from None
        if (state.get("enabled") and item["status"] in {"queued", "retry"} and
                stamp(item["ts"]) <= stamp(state["cutoff"])):
            raise ValueError("Pending wake before activation")
        total += item["attempt_count"]
        if stamp(item["ts"]) <= stamp(state.get("origin_cutoff", state["cutoff"])):
            raise ValueError("Wake item before cutoff")
        if item["status"] == "retry":
            if not isinstance(item.get("retry_at"), str) or datetime.fromisoformat(item["retry_at"]).tzinfo is None:
                raise ValueError("Invalid retry time")
    if total != len(state['attempts']):
        raise ValueError("Wake quota history inconsistent")


def validate_endpoint(url):
    # No query strings, percent encoding, credentials, ports, lookalike hosts,
    # redirects, alternate APIs or user-controlled DNS destinations.
    if not isinstance(url, str) or not re.fullmatch(
            r"https://api\.anthropic\.com/v1/claude_code/routines/[A-Za-z0-9_-]+/fire", url):
        raise SafetyError("Noa endpoint is not an allowed official routine fire URL")
    return url


def validate_permalink(url, channel, ts):
    if not isinstance(url, str) or not re.fullmatch(
            r"https://[a-z0-9][a-z0-9-]*\.slack\.com/archives/" + re.escape(channel) +
            r"/p" + re.escape(ts.replace('.', '')), url):
        raise SafetyError("Invalid Slack permalink")
    return url


def session_evidence(result):
    if not isinstance(result, dict) or result.get("type") != "routine_fire":
        raise NoaUncertain()
    sid, url = result.get("claude_code_session_id"), result.get("claude_code_session_url")
    if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or
            url != "https://claude.ai/code/" + sid):
        raise NoaUncertain()
    return url


class NoaUncertain(Exception):
    def __init__(self):
        super().__init__("Noa execution outcome unknown")


class NoaRejected(Exception):
    def __init__(self, reason="explicit_rejection"):
        super().__init__("Noa execution rejected")
        self.reason = reason


class NoaRateLimit(Exception):
    def __init__(self, delay):
        super().__init__("Noa execution rate limited")
        self.delay = delay


class NoaClient:
    def __init__(self, url, token, *, guard, clock, opener=None):
        self.url, self.token = url, token
        self.guard, self.clock = guard, clock
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def validate(self):
        validate_endpoint(self.url)
        if not self.token or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise SafetyError("Noa token missing or invalid")

    def fire(self, text):
        self.validate()
        req = urllib.request.Request(self.url, method="POST",
            data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.token,
                     "anthropic-beta": "experimental-cc-routine-2026-04-01",
                     "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        try:
            self.guard()
        except (StopRequested, TimeBudgetExceeded) as exc:
            exc.not_sent = True
            raise
        try:
            with self.opener.open(req, timeout=20) as response:
                if response.status < 200 or response.status >= 300:
                    raise NoaUncertain()
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise NoaUncertain()
                # A late response is not accepted after the run boundary. The
                # request may have executed, so this guard is inside uncertainty.
                self.guard()
                result = json.loads(raw)
                session_evidence(result)
                return result
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    value = exc.headers.get("Retry-After")
                    try:
                        delay = float(value)
                    except (ValueError, TypeError):
                        delay = (parsedate_to_datetime(value) - self.clock()).total_seconds()
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError()
                except (ValueError, TypeError, OverflowError):
                    # Proven rejection, but unsafe retry timing: terminal rejection.
                    raise NoaRejected() from None
                raise NoaRateLimit(delay) from None
            if exc.code in {401, 403}:
                raise NoaRejected("authentication") from None
            if 300 <= exc.code < 500:
                raise NoaRejected() from None
            raise NoaUncertain() from None
        except (NoaUncertain, NoaRejected, NoaRateLimit, AccountMismatch):
            raise
        except (StopRequested, TimeBudgetExceeded) as exc:
            # Preserve the global boundary while recording that the POST may
            # have executed. NoaWake durably holds it and re-raises immediately.
            exc.uncertain = True
            exc.not_sent = False
            raise
        except Exception:
            # Never propagate exception strings, response bodies or tokens.
            raise NoaUncertain() from None


class NoaWake:
    def __init__(self, patrol, client=None):
        self.p = patrol
        self.s = migrate_state(patrol.state["noa_wake"])
        self.client = client or NoaClient(patrol.config.noa_fire_url, patrol.config.noa_fire_token,
                                        guard=patrol.check_time, clock=patrol.clock)

    def sync_enabled(self):
        """Durable fence before reads; OFF never reads or queues external notices."""
        p, s = self.p, self.s
        enabled = bool(p.config.noa_wake_enabled)
        now = p.clock().astimezone(timezone.utc)
        ts = str(int(now.timestamp())) + "." + f"{now.microsecond:06d}"
        if not enabled:
            if s["enabled"] or s["off_at"] is None:
                self._stale_window(None)
                s.update(enabled=False, off_at=ts)
                events = self._retire_pending()
                self._break_success_windows()
                p.save()
                self._append_retirement_events(events)
            return False
        last = s["last_run_at"]
        gap = None if last is None else (now - datetime.fromisoformat(last)).total_seconds()
        stale = s["enabled"] and (gap is None or gap > MAX_RUN_GAP or gap < 0)
        if not s["enabled"] or stale:
            self._stale_window(ts)
            events = self._retire_pending()
            if s["origin_cutoff"] is None:
                s["origin_cutoff"] = ts
            # Never move the safety fence backwards if the system clock regresses.
            fence = max([ts, s["cutoff"] or ts, s["watermark"] or ts], key=stamp)
            s.update(enabled=True, cutoff=fence, watermark=fence, last_run_at=iso(now))
            self._break_success_windows()
            p.save()
            self._append_retirement_events(events)
            return True
        return False

    def _stale_window(self, end):
        s = self.s
        if s["cutoff"] is None:
            return
        pending = [k for k, i in s["items"].items() if i["status"] in {"queued", "retry"}]
        open_window = next((w for w in s["stale_windows"] if w["end"] is None), None)
        if open_window is not None:
            open_window["retired"] = sorted(set(open_window["retired"] + pending))
            if end is not None:
                open_window["end"] = max([end, open_window["start"]], key=stamp)
        else:
            s["stale_windows"].append({"start": s["watermark"],
                "end": None if end is None else max([end, s["watermark"]], key=stamp),
                "retired": pending, "count": None})

    def _eligible(self, m):
        return (isinstance(m.get("text"), str) and
            any(mention in m["text"] for mention in ("@のあ", "＠のあ")) and
            isinstance(m.get("user"), str) and bool(re.fullmatch(r"U[A-Z0-9]+", m["user"])) and
            m["user"] not in {"U0CFH9JRCKV", self.p.config.expected_user_id} and
            not any(k in m for k in ("bot_id", "subtype", "bot_profile")))

    def _history(self, oldest):
        self.p.check_time()
        messages = self.p.slack.history(CHANNEL, oldest)
        if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
            raise SafetyError("Invalid Noa history")
        by_ts = {}
        try:
            for m in messages:
                stamp(m.get("ts"))
                if m["ts"] in by_ts and by_ts[m["ts"]] != m:
                    raise SafetyError("Conflicting Noa history records")
                by_ts[m["ts"]] = m
        except ValueError:
            raise SafetyError("Invalid Noa history timestamp") from None
        self.p.check_time()
        return list(by_ts.values())

    def _stale_notices(self):
        # A failed/paginated scan leaves count unknown. Never announce a guessed total.
        for number, window in enumerate(self.s["stale_windows"], 1):
            if window["end"] is None:
                continue
            if window["count"] is None:
                messages = self._history(window["start"])
                unseen = {CHANNEL + ":" + m["ts"] for m in messages
                    if stamp(window["start"]) < stamp(m["ts"]) <= stamp(window["end"]) and
                    self._eligible(m) and CHANNEL + ":" + m["ts"] not in self.s["items"]}
                window["count"] = len(unseen | set(window["retired"]))
                self.p.save()  # Count intent survives crash before queue/outbox save.
            if window["count"]:
                self.p.queue("noa-stale:" + str(number), self.p.config.report_channel,
                    "起こさなかった @のあ が" + str(window["count"]) + "件あります。"
                    "停止・OFF・巡回の空白期間の指示は自動で再送しません。必要なら新しく投稿してください。",
                    "noa_notice")

    def _retire_pending(self):
        events = []
        for item in self.s["items"].values():
            if item["status"] in {"queued", "retry"}:
                item.update(status="rejected", reason="disabled_before_send")
                event = new_event(self.p.clock(), self.p.started_at,
                    "OFF切替のため未送信分の起動を取りやめた", result="起こさなかった",
                    memo="再びONにしても、この指示は自動再送しません。元の指示 ts " + item["ts"],
                    progress="のあの受付境界を更新した")
                events.append(event)
                self.p.state["log_buffer"].append(event)
        return events

    def _append_retirement_events(self, events):
        # Boundary, all retired records and log intents have already been fsynced.
        for event in events:
            self.p.logs.event(event)
            self.p.events_added += 1

    def _break_success_windows(self):
        for episode in self.s["failure_episodes"].values():
            episode.update(success_since=None, last_success=None)

    def _failure(self, reason):
        s = self.s
        episode = s["failure_episodes"].get(reason)
        if episode is None:
            s["failure_serial"] += 1
            episode = {"serial": s["failure_serial"], "success_since": None, "last_success": None}
            s["failure_episodes"][reason] = episode
        # Any partial failure interrupts evidence of continuously healthy operation.
        self._break_success_windows()
        s["failure_reason"] = reason
        return episode["serial"]

    def _success(self, reasons):
        now = self.p.clock()
        for reason in reasons:
            episode = self.s["failure_episodes"].get(reason)
            if episode is None:
                continue
            last = episode["last_success"]
            gap = None if last is None else (now - datetime.fromisoformat(last)).total_seconds()
            if gap is None or gap < 0 or gap > MAX_RUN_GAP:
                episode["success_since"] = iso(now)
            episode["last_success"] = iso(now)
            if (now - datetime.fromisoformat(episode["success_since"])).total_seconds() >= RECOVERY_SECONDS:
                del self.s["failure_episodes"][reason]
        if self.s["failure_reason"] in reasons:
            self.s["failure_reason"] = None

    def component_failure(self, reason="component_unavailable", item=None):
        """Reason-specific durable episodes survive alternating failures/successes."""
        if reason not in FAILURES:
            reason = "component_unavailable"
        episode = self._failure(reason)
        self.p.save()
        self._queue_component_notice(episode, reason, item)

    def _queue_component_notice(self, episode, reason, item=None):
        texts = {
            "configuration": "のあの起動設定を確認できません。URL・認証設定を確認してください。自動で再送しません。",
            "authentication": "のあの起動が認証・権限の理由で拒否されました。認証設定と権限を確認してください。自動で再送しません。",
            "component_unavailable": "のあの受付処理でエラーが発生しました。状態と接続設定を確認してください。送信結果不明のものは再送せず保留します。"}
        text = texts[reason]
        if item and item.get("source_link"):
            text += "\n" + validate_permalink(item["source_link"], CHANNEL, item["ts"])
        self.p.queue("noa-component:" + str(episode),
                     self.p.config.report_channel, text, "noa_notice")

    def _record_component_intent(self, item, reason):
        failure = "configuration" if reason == "invalid_configuration" else "authentication"
        item["notice_episode"] = self._failure(failure)

    def finish(self, item, status, reason, evidence=""):
        if status not in {"done", "simulated"}:
            self._partial_failure = True
        item.update(status=status, reason=reason)
        if reason in {"invalid_configuration", "authentication"}:
            # Same atomic write as terminal state; a crash cannot orphan notice intent.
            self._record_component_intent(item, reason)
        action = {"done": "のあを起こした", "simulated": "のあの起動を練習した（実行なし）",
                  "held": "のあの起動結果不明のため保留", "rejected": "のあの起動を拒否された"}[status]
        if status == "rejected" and reason in {"source_changed", "source_contains_credential",
                "source_unavailable_or_changed", "source_changed_or_contains_credential", "invalid_configuration"}:
            action = "のあの起動要求を送信せず終了した"
        ev = new_event(self.p.clock(), self.p.started_at, action,
                       result="完了" if status == "done" else ("練習" if status == "simulated" else "できなかった"),
                       evidence=evidence, memo={
                           "interrupted_send": "送信中に処理が中断したため、実行結果を確認するまで保留します。",
                           "unknown_outcome": "実行結果を確認できないため、自動再送せず保留します。",
                           "source_unavailable_or_changed": "元の指示が削除または変更されたため、起動しませんでした。",
                           "source_changed_or_contains_credential": "元の指示が変更されたか認証情報が含まれるため、起動しませんでした。",
                           "source_changed": "元の指示が変更されたため、起動しませんでした。",
                           "source_contains_credential": "元の指示に認証情報が含まれるため、起動しませんでした。",
                           "practice_no_post": "練習のため実際の起動はしていません。",
                           "invalid_configuration": "起動設定を確認できないため、起動しませんでした。",
                           "authentication": "認証または権限の問題で起動を拒否されました。",
                           "invalid_retry_after": "安全な再試行時刻を確認できないため、再送しません。",
                           "retry_exhausted": "許可された再試行でも制限されたため、これ以上再送しません。",
                           "explicit_rejection": "起動要求が明示的に拒否されたため、再送しません。",
                           "session_verified": "起動先のセッションを確認しました。",
                       }.get(reason, "起動を中止しました。状態を確認してください。"), progress="@のあ のメッセージを拾った")
        # Terminal status and event intent are ONE durable write, before local append.
        ev = json.loads(sanitize(json.dumps(ev, ensure_ascii=False), self.p.config.secrets))
        self.p.state["log_buffer"].append(ev)
        self.p.save()
        self.p.logs.event(ev)
        self.p.events_added += 1

    def notices(self):
        for key, item in self.s["items"].items():
            reason = item.get("reason")
            if item["status"] not in {"held", "rejected"} or reason == "disabled_before_send":
                continue
            if reason in {"invalid_configuration", "authentication"}:
                if "notice_episode" not in item:
                    # Backfill terminal records from versions without durable intent.
                    self._record_component_intent(item, reason)
                    self.p.save()
                failure = "configuration" if reason == "invalid_configuration" else "authentication"
                self._queue_component_notice(item["notice_episode"], failure, item)
                continue
            if item["status"] == "held":
                text = "のあの起動結果が不明のため保留しています。Claudeの予定タスク（routine）の実行履歴とセッションを確認し、確認結果を記録してください。自動では再実行しません。"
            elif reason in {"source_changed", "source_unavailable_or_changed"}:
                text = "元の指示が変更または削除されたため、のあを起動しませんでした。必要なら新しい指示を投稿してください。"
            elif reason in {"source_contains_credential", "source_changed_or_contains_credential"}:
                text = "指示に認証情報が含まれる可能性があるため、のあへ転送しませんでした。認証情報を除いた新しい指示を投稿してください。"
            else:
                text = "のあの起動要求が拒否されました。起動設定と制限を確認してください。自動では再実行しません。"
            source = "指示メッセージ ts " + item["ts"]
            if item.get("source_link"):
                source = validate_permalink(item["source_link"], CHANNEL, item["ts"])
            self.p.queue("noa-notice:" + key, self.p.config.report_channel,
                         text + "\n" + source, "noa_notice")
        for reason, episode in self.s["failure_episodes"].items():
            self._queue_component_notice(episode["serial"], reason)


    def run(self):
        self._healthy_fire = False
        self._partial_failure = False
        try:
            completed = self._run()
            if completed is not False:
                # Fresh work has been observed even if deferred stale accounting
                # later fails. Large old windows must not starve current reception.
                self.s["last_run_at"] = iso(self.p.clock())
                self.p.save()
            if self.p.config.noa_wake_enabled:
                self._stale_notices()
            if completed is not False:
                if self._partial_failure:
                    self._break_success_windows()
                else:
                    self._success(FAILURES if self._healthy_fire else {"component_unavailable"})
                self.p.save()
        except Exception:
            self._break_success_windows()
            self.p.save()
            raise

    def _run(self):
        p, s = self.p, self.s
        activated = self.sync_enabled()
        if not p.config.noa_wake_enabled:
            return False
        p.check_time()
        if p.config.instruction_channel != CHANNEL:
            raise SafetyError("Noa wake requires the approved instruction channel")
        for item in s["items"].values():
            if item["status"] == "sending":
                self.finish(item, "held", "interrupted_send")
        self.notices()
        if activated:
            return False
        pending = [i["ts"] for i in s["items"].values() if i["status"] in {"queued", "retry"}]
        oldest = min([s["watermark"]] + pending, key=stamp)
        # No enqueue or watermark advance until every history page succeeds.
        messages = self._history(oldest)
        by_ts = {m["ts"]: m for m in messages}
        watermark = s["watermark"]
        for m in messages:
            ts = m["ts"]
            if stamp(ts) > stamp(watermark):
                watermark = ts
            if stamp(ts) <= stamp(s["cutoff"]) or not self._eligible(m):
                continue
            key = CHANNEL + ":" + ts
            s["items"].setdefault(key, {"channel": CHANNEL, "ts": ts, "status": "queued", "attempt_count": 0,
                                        "source_digest": hashlib.sha256(m["text"].encode()).hexdigest(),
                                        "source_user": m["user"]})
        s["watermark"] = watermark
        p.save()
        # Rolling window includes every actual attempt (including 429 and retry).
        recent = [t for t in s["attempts"] if (p.clock() - datetime.fromisoformat(t)).total_seconds() < 3600]
        if len(recent) >= 10:
            return
        for key, item in sorted(s["items"].items(), key=lambda pair: stamp(pair[1]["ts"])):
            if item["status"] not in {"queued", "retry"}:
                continue
            if item["status"] == "retry" and p.clock() < datetime.fromisoformat(item["retry_at"]):
                continue
            p.check_time()
            m = by_ts.get(item["ts"])
            if (not m or not isinstance(m.get("text"), str) or not any(mention in m["text"] for mention in ("@のあ", "＠のあ")) or
                    m.get("user") in {"U0CFH9JRCKV", p.config.expected_user_id} or
                    not isinstance(m.get("user"), str) or not re.fullmatch(r"U[A-Z0-9]+", m["user"]) or
                    any(k in m for k in ("bot_id", "subtype", "bot_profile"))):
                self.finish(item, "rejected", "source_unavailable_or_changed")
                continue
            if (item.get("source_digest") != hashlib.sha256(m["text"].encode()).hexdigest() or
                    item.get("source_user") != m["user"]):
                self.finish(item, "rejected", "source_changed")
                continue
            if sanitize(m["text"], p.config.secrets) != m["text"]:
                self.finish(item, "rejected", "source_contains_credential")
                continue
            if p.config.mode == "practice":
                self.finish(item, "simulated", "practice_no_post")
                # Never write the source body, endpoint or token to practice logs.
                p.logs.practice({"operation": "noa_wake", "status": "simulated", "ts": item["ts"]})
                break
            try:
                self.client.validate()
            except SafetyError:
                self._partial_failure = True
                self.finish(item, "rejected", "invalid_configuration")
                self.component_failure("configuration")
                break
            p.check_time()
            link = p.slack.permalink(CHANNEL, item["ts"])
            validate_permalink(link, CHANNEL, item["ts"])
            item["source_link"] = link
            when = datetime.fromtimestamp(int(stamp(item["ts"])), timezone.utc)
            sender = "たかむね（" + m["user"] + "）" if m["user"] == "U0C5V11S37X" else m["user"]
            text = "送信者: " + sender + "\n送信時刻: " + iso(when) + " JST\nメッセージ:\n" + m["text"] + "\nSlack: " + link
            p.check_time()
            previous = item["status"]
            item.update(status="sending", attempt_count=item["attempt_count"] + 1)
            attempt_time = iso(p.clock())
            s["attempts"].append(attempt_time)
            p.save()  # fsync BEFORE external effect; crash => HOLD on next run.
            try:
                result = self.client.fire(text)
                evidence = session_evidence(result)
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    item.update(status=previous, attempt_count=item["attempt_count"] - 1)
                    s["attempts"].pop()
                    p.save()
                else:
                    self.finish(item, "held", "interrupted_send")
                raise
            except NoaRateLimit as exc:
                self._partial_failure = True
                if item["attempt_count"] < 2:
                    try:
                        retry_at = iso(p.clock() + timedelta(seconds=exc.delay))
                    except (OverflowError, ValueError):
                        self.finish(item, "rejected", "invalid_retry_after")
                    else:
                        item.update(status="retry", retry_at=retry_at)
                        p.save()
                else:
                    self.finish(item, "rejected", "retry_exhausted")
            except NoaRejected as exc:
                self._partial_failure = True
                self.finish(item, "rejected", exc.reason)
                if exc.reason == "authentication":
                    self.component_failure("authentication", item)
            except AccountMismatch:
                self.finish(item, "held", "interrupted_send")
                raise
            except Exception:
                self._partial_failure = True
                self.finish(item, "held", "unknown_outcome")
            else:
                self.finish(item, "done", "session_verified", evidence)
                self._healthy_fire = True
            break  # At most ONE actual attempt in this process/run, even on 429.
        self.notices()
