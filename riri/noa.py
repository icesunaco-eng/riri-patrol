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

from .errors import SafetyError, StopRequested, TimeBudgetExceeded, sanitize
from .eventlog import iso, new_event
from .http import NoRedirect

CHANNEL = "C0CFM2HRA5V"
STATES = {"queued", "retry", "sending", "done", "held", "rejected", "simulated", "reviewed"}
TS = re.compile(r"[0-9]{1,16}\.[0-9]{1,6}\Z")


def stamp(value):
    if not isinstance(value, str) or not TS.fullmatch(value):
        raise ValueError("Invalid timestamp")
    return Decimal(value)


def validate_state(state):
    """Corrupt dedup or quota records may never silently reset to first activation."""
    if not isinstance(state, dict) or set(state) != {"cutoff", "watermark", "items", "attempts"}:
        raise ValueError("Invalid wake state")
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
        total += item["attempt_count"]
        if stamp(item["ts"]) <= stamp(state["cutoff"]):
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
    def __init__(self):
        super().__init__("Noa execution rejected")


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
            if 300 <= exc.code < 500:
                raise NoaRejected() from None
            raise NoaUncertain() from None
        except (NoaUncertain, NoaRejected, NoaRateLimit):
            raise
        except (StopRequested, TimeBudgetExceeded):
            # The guard already passed: a signal here cannot prove no send.
            raise NoaUncertain() from None
        except Exception:
            # Never propagate exception strings, response bodies or tokens.
            raise NoaUncertain() from None


class NoaWake:
    def __init__(self, patrol, client=None):
        self.p = patrol
        self.s = patrol.state["noa_wake"]
        self.client = client or NoaClient(patrol.config.noa_fire_url, patrol.config.noa_fire_token,
                                        guard=patrol.check_time, clock=patrol.clock)

    def finish(self, item, status, reason, evidence=""):
        item.update(status=status, reason=reason)
        action = {"done": "のあを起こした", "simulated": "のあの起動を練習した（実行なし）",
                  "held": "のあの起動結果不明のため保留", "rejected": "のあの起動を拒否された"}[status]
        ev = new_event(self.p.clock(), self.p.started_at, action,
                       result="完了" if status == "done" else ("練習" if status == "simulated" else "できなかった"),
                       evidence=evidence, memo=reason, progress="@のあ のメッセージを拾った")
        # Terminal status and event intent are ONE durable write, before local append.
        ev = json.loads(sanitize(json.dumps(ev, ensure_ascii=False), self.p.config.secrets))
        self.p.state["log_buffer"].append(ev)
        self.p.save()
        self.p.logs.event(ev)
        self.p.events_added += 1

    def notices(self):
        for key, item in self.s["items"].items():
            if item["status"] in {"held", "rejected"}:
                # Stable outbox key. Ambiguous delivery is handled by the existing
                # outbox; this code never creates a second notice for the same trigger.
                self.p.queue("noa-notice:" + key, self.p.config.report_channel,
                    "のあの起動を確認できませんでした。自動では再実行しません。確認をお願いします🍚\n"
                    + "指示メッセージ ts " + item["ts"], "noa_notice")

    def run(self):
        p, s = self.p, self.s
        if not p.config.noa_wake_enabled:
            return
        p.check_time()
        if p.config.instruction_channel != CHANNEL:
            raise SafetyError("Noa wake requires the approved instruction channel")
        for item in s["items"].values():
            if item["status"] == "sending":
                self.finish(item, "held", "interrupted_send")
        self.notices()
        if s["cutoff"] is None:
            # Activation is a local boundary; never consume pre-activation history.
            now = p.clock().astimezone(timezone.utc)
            cutoff = str(int(now.timestamp())) + "." + f"{now.microsecond:06d}"
            s["cutoff"] = s["watermark"] = cutoff
            p.save()
            return
        pending = [i["ts"] for i in s["items"].values() if i["status"] in {"queued", "retry"}]
        oldest = min([s["watermark"]] + pending, key=stamp)
        p.check_time()
        # SlackClient returns only after every page succeeds. No state change
        # whatsoever from a partial scan, and pending work is never discarded.
        messages = p.slack.history(CHANNEL, oldest)
        if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
            raise SafetyError("Invalid Noa history")
        try:
            for m in messages:
                stamp(m.get("ts"))
        except ValueError:
            raise SafetyError("Invalid Noa history timestamp") from None
        by_ts = {}
        for m in messages:
            if m["ts"] in by_ts and by_ts[m["ts"]] != m:
                raise SafetyError("Conflicting Noa history records")
            by_ts[m["ts"]] = m
        watermark = s["watermark"]
        for m in messages:
            ts = m["ts"]
            if stamp(ts) > stamp(watermark):
                watermark = ts
            if (stamp(ts) <= stamp(s["cutoff"]) or not isinstance(m.get("text"), str) or
                    "@のあ" not in m["text"] or not isinstance(m.get("user"), str) or
                    not re.fullmatch(r"U[A-Z0-9]+", m["user"]) or
                    m["user"] in {"U0CFH9JRCKV", p.config.expected_user_id} or
                    any(k in m for k in ("bot_id", "subtype", "bot_profile"))):
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
            if (not m or not isinstance(m.get("text"), str) or "@のあ" not in m["text"] or
                    m.get("user") in {"U0CFH9JRCKV", p.config.expected_user_id} or
                    not isinstance(m.get("user"), str) or not re.fullmatch(r"U[A-Z0-9]+", m["user"]) or
                    any(k in m for k in ("bot_id", "subtype", "bot_profile"))):
                self.finish(item, "rejected", "source_unavailable_or_changed")
                continue
            if (item.get("source_digest") != hashlib.sha256(m["text"].encode()).hexdigest() or
                    item.get("source_user") != m["user"] or
                    sanitize(m["text"], p.config.secrets) != m["text"]):
                self.finish(item, "rejected", "source_changed_or_contains_credential")
                continue
            if p.config.mode == "practice":
                self.finish(item, "simulated", "practice_no_post")
                # Never write the source body, endpoint or token to practice logs.
                p.logs.practice({"operation": "noa_wake", "status": "simulated", "ts": item["ts"]})
                break
            try:
                self.client.validate()
            except SafetyError:
                self.finish(item, "rejected", "invalid_configuration")
                break
            p.check_time()
            link = p.slack.permalink(CHANNEL, item["ts"])
            validate_permalink(link, CHANNEL, item["ts"])
            when = datetime.fromtimestamp(int(stamp(item["ts"])), timezone.utc)
            text = "送信者: " + m["user"] + "\n送信時刻: " + iso(when) + " JST\nメッセージ:\n" + m["text"] + "\nSlack: " + link
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
            except NoaRejected:
                self.finish(item, "rejected", "explicit_rejection")
            except Exception:
                self.finish(item, "held", "unknown_outcome")
            else:
                self.finish(item, "done", "session_verified", evidence)
            break  # At most ONE actual attempt in this process/run, even on 429.
        self.notices()
