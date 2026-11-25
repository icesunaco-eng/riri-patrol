"""Fixed workflow. The language model cannot select actions or tools."""
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import time

from . import compose
from .delivery_policy import make_delivery_policy, decorate_delivery, validate_delivery_text
from .dual_delivery import (queue_aisu_instruction, instruction_delivery_status,
                            preflight_dual_source, queue_pair, confirmed_post)
from .report_delivery import (resolve_requester, bundle_id, report_refs_pending,
                              acknowledge_report_bundle, report_bundle_status)
from .mentions import add_report_mention, assert_outgoing_safe
from .relay import queue_instruction_relay
from .errors import (AccountMismatch, DuplicateRequestId, NotionConflict, PatrolError, RemoteError,
                     RequestIncomplete, SafetyError, StatusMissing, StopRequested, TimeBudgetExceeded, UnsafeContent, sanitize)
from .eventlog import LocalLogs, dt, iso, new_event, notion_rows, row_ids, slack_evidence
from .lock import patrol_lock
from .models import Request
from .parse import (parse_requests, parse_reports, report_references, report_candidate_ids,
                    replace_status, validate_request)
from .handoff_guard import older_active_duplicate
from .work_urls import validate_work_url
from .state import StateStore
from .status import local_execution_needs_review, completion_review_items
from .writer import LiveWriter, PracticeWriter
from . import notify


class StatusUnwritable(StatusMissing):
    """This request's 状態 line cannot be written (re-review 💚20261125-16 T3): only this request waits.
    Kept here, not in errors.py, whose bytes older versions' compatibility tests pin."""


class Patrol:
    def __init__(self, config, slack, notion, draft, *, clock=None, monotonic=None,
                 notifier=None, unexpected_notifier=None, logs=None, verbose=False, noa_client=None):
        self.config, self.slack, self.notion, self.draft = config, slack, notion, draft
        self.lane_taken = {}
        self.lane_given = {}   # まむ/みき who got a job: their reports count for this request
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.notifier = notifier or notify.account_mismatch
        self.unexpected_notifier = unexpected_notifier or notify.unexpected_error
        self.logs = logs or LocalLogs(config.root, config.secrets, verbose)
        self.store = StateStore(config.root, config.mode)
        self.noa_client = noa_client
        writer_type = LiveWriter if config.mode == "live" else PracticeWriter
        self.writer = writer_type(slack, notion, self.logs, self.clock)

    def save(self):
        self.store.save(self.state)

    def check_time(self):
        if self.monotonic() - self.started_mono >= self.config.run_timeout:
            error = TimeBudgetExceeded("4分以内の実行時間の上限に達しました")
            error.not_sent = True  # This check is a pre-operation boundary, not I/O.
            raise error
        if self.config.stop.exists():
            # Mid-run STOP blocks the next remote operation as well.
            raise StopRequested("実行中にSTOPが置かれたため停止しました")

    def event(self, action, req_id="", actor="", result="完了", evidence="", memo=""):
        ev = new_event(self.clock(), self.started_at, action, req_id, actor, result, evidence, memo)
        ev = json.loads(sanitize(json.dumps(ev, ensure_ascii=False), self.config.secrets))
        self.state["log_buffer"].append(ev)
        self.save()
        self.logs.event(ev)
        self.events_added += 1

    def queue(self, key, channel, text, kind="notice", *, persist=True, **meta):
        if key not in self.state["outbox"]:
            if meta.get('delivery_policy') is not None:
                text = decorate_delivery(text, meta['delivery_policy'],
                                         self.config.expected_user_id, self.config.secrets)
            else:
                text = add_report_mention(text, channel, self.config.report_channel,
                    self.config.ice_mention_user_id, self.config.expected_user_id, self.config.secrets)
            digest = hashlib.sha256(key.encode()).hexdigest()[:24]
            marker = "rp:" + digest
            self.state["outbox"][key] = {
                "status": "queued", "channel": channel, "text": text + "\n" + marker,
                "marker": marker, "kind": kind, "created_at": iso(self.clock()), **meta}
            if persist:
                self.save()
        return self.state["outbox"][key]

    def acknowledge(self, key, item, ts, *, owner_post_confirmed=False, owner_success_at=None):
        # One durable commit covers message success, every seen marker, and every
        # event intent. A crash cannot leave a partially acknowledged report batch.
        item.update(status="posted", ts=ts, posted_at=iso(self.clock()))
        receipt = getattr(getattr(self, 'slack', None), 'last_delivery_receipt', None)
        if (item.get('delivery_policy') and isinstance(receipt, dict) and
                receipt.get('channel') == item['channel'] and receipt.get('ts') == ts and
                receipt.get('canonical_hash') == item['delivery_policy']['canonical_hash']):
            item['delivery_receipt'] = dict(receipt)
        if owner_post_confirmed:
            self.report_user_health(post_succeeded=True, success_at=owner_success_at, save=False)
        events = []
        if item["kind"] == "instruction":
            info = self.state["requests"][item["request_id"]]
            info.update(status="posted", slack_ts=ts, posted_at=item["posted_at"])
            info.pop("retry_permitted_once", None)
            item.pop("retry_permitted_once", None)
            if not info.get("posted_event"):
                info["posted_event"] = True
                events.append(new_event(self.clock(), self.started_at, "Slackに投稿した",
                    item["request_id"], info["snapshot"]["requester"], evidence=slack_evidence(self.clock(), ts),
                    memo=info["draft_source"] + " 宛先=" + info["snapshot"]["recipients_source"]))
        elif item["kind"] == "reports":
            finalized = (acknowledge_report_bundle(self.state, item['report_bundle_id'])
                         if item.get('report_bundle_id') else item['reports'])
            for report in finalized:
                self.state["seen_reports"][report["block_id"]] = report["digest"]
                versions = self.state["seen_report_versions"].setdefault(report["block_id"], [])
                if report["digest"] not in versions:
                    versions.append(report["digest"])
                events.append(new_event(self.clock(), self.started_at, "完了報告を見つけた",
                    report["id"], report["reporter"], report["result"], evidence=slack_evidence(self.clock(), ts)))
        elif item["kind"] == "self":
            if item["request_id"] not in self.state["riri_notified"]:
                self.state["riri_notified"].append(item["request_id"])
                events.append(new_event(self.clock(), self.started_at, "りり自身あてのため飛ばした", item["request_id"]))
        elif item["kind"] in {"recovery", "deadline_recovery"}:
            if ("recovery_error_serial" in item and
                    item["recovery_error_serial"] == self.state["core_error_serial"]):
                self.state["last_error"] = None
        elif item["kind"] == "error":
            previous = self.state["errors"].get(item["error_name"], {})
            self.state["errors"][item["error_name"]] = {"last_notified_at": iso(self.clock()),
                "count": previous.get("count", 0) + 1}
        elif item["kind"] == "maintenance":
            if item.get("size_notice"):
                self.state["last_log_size_notice_at"] = iso(self.clock())
            if item.get("buffer_notice"):
                self.state["buffer_warning_sent"] = True
        events = [json.loads(sanitize(json.dumps(ev, ensure_ascii=False), self.config.secrets)) for ev in events]
        if item.get('delivery_policy', {}).get('purpose') == 'aisu_instruction':
            instruction_delivery_status(self.state, item['request_id'])
        self.state["log_buffer"].extend(events)
        self.save()
        for ev in events:
            self.logs.event(ev)
            self.events_added += 1
        if item["kind"] == "instruction":
            self.ensure_followups(item["request_id"], self.state["requests"][item["request_id"]])

    def reconcile_local_events(self):
        """Rebuild an interrupted append-only file write from durable event intents."""
        path = self.config.root / "logs" / "events.jsonl"
        known = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    known.add(json.loads(line)["event_id"])
                except (ValueError, KeyError, TypeError):
                    raise SafetyError("出来事ログが破損しています。削除せず確認してください") from None
        for ev in self.state["log_buffer"]:
            if ev["event_id"] not in known:
                self.logs.event(ev)

    def preflight_instruction(self, item):
        info = self.state["requests"].get(item["request_id"])
        req = self.current_requests.get(item["request_id"])
        if not info or not req or req.block_id != info["block_id"]:
            raise SafetyError("投稿待ちの依頼が削除または置換されました。確認が必要です")
        old = Request(**info["snapshot"])
        if replace_status(old.text, "未着手") != replace_status(req.text, "未着手"):
            raise SafetyError("投稿待ちの依頼本文が変更されました。送信せず確認を待ちます")
        allowed = {"対応中"} if self.config.mode == "live" else {"未着手", "対応中"}
        if req.status not in allowed:
            raise SafetyError("投稿待ちの依頼の状態が変更されました。送信しません")

    def duplicate_before_send(self, item):
        """Re-read known-unsent work at the final instruction send boundary."""
        latest = parse_requests(self.notion.read_blocks(self.config.requests_page))
        ids = [req.id for req in latest]
        if len(ids) != len(set(ids)):
            raise DuplicateRequestId('送信直前の依頼IDが重なっています')
        self.current_requests = {req.id: req for req in latest}
        req = self.current_requests.get(item['request_id'])
        info = self.state['requests'].get(item['request_id'], {})
        if req is None or info.get('status') in {'posted', 'done'}:
            return False
        duplicate = older_active_duplicate(req, latest, self.state)
        if duplicate is None:
            return False
        self.hold_content_duplicate(req, *duplicate, item=item)
        return True

    def ensure_followups(self, request_id, info):
        # Only new v1.0.1 instruction intents opt in. Do not replay historical
        # already-posted instructions when upgrading an existing live state.
        source = self.state["outbox"].get("instruction:" + request_id, {})
        if source.get('delivery_policy', {}).get('version') == 'dual-v2':
            instruction_delivery_status(self.state, request_id)
        elif (source.get("relay_to_report") is True and source.get("status") == "posted"
                and source.get("channel") == self.config.instruction_channel
                and any(name in info["recipients"] for name in ("あいす", "みに"))):
            queue_instruction_relay(self, request_id, source)
        if "みに" in info["recipients"]:
            self.queue("mini:" + request_id, self.config.report_channel,
                       compose.mini_notice(request_id), "instruction_relay", request_id=request_id)

    def hold(self, key, item):
        first = item["status"] != "held"
        item["status"] = "held"
        item.pop("retry_permitted_once", None)
        if first:
            item["held_at"] = iso(self.clock())
            item["hold_episode"] = item.get("hold_episode", 0) + 1
            item["retry_consumed_at"] = iso(self.clock())
        if item["kind"] == "instruction":
            info = self.state["requests"][item["request_id"]]
            info["status"] = "held"
            info.pop("retry_permitted_once", None)
            info["retry_consumed_at"] = item.get("retry_consumed_at")
        if item.get('delivery_policy', {}).get('purpose') == 'aisu_instruction':
            instruction_delivery_status(self.state, item['request_id'])
        if item.get('report_bundle_id'):
            bundle = self.state.get('report_deliveries', {}).get(item['report_bundle_id'])
            if bundle:
                bundle['status'] = report_bundle_status(self.state, item['report_bundle_id'])
        self.save()
        if first:
            self.event("投稿結果不明のため保留", item.get("request_id", ""), result="一部完了",
                       memo="自動再投稿しません。たかむねの確認が必要です")
        self.logs.operational("投稿結果不明：" + key + "。再投稿せず確認待ち")
        # A hold notice itself can be uncertain; do not recurse into another hold notice.
        if item["kind"] != "hold_notice":
            self.queue("hold:" + key + ":" + str(item.get("hold_episode", 1)), self.config.report_channel,
                "投稿できたか確認できないため保留しました🍚 自動再投稿はしません。"
                + "たかむね、Slackの履歴を確認してください（" + item["marker"] + "）",
                "hold_notice", held_key=key, hold_episode=item.get("hold_episode", 1))

    @staticmethod
    def is_noa_notice(item):
        return (item.get("kind") == "noa_notice" or
                str(item.get("held_key", "")).startswith(("noa-notice:", "noa-component:", "noa-stale:")))

    def noa_notice_disabled(self, item):
        # Include uncertainty notices descended from a Noa notice. Disabling
        # the feature freezes both their history reconciliation and their sends.
        return (not self.config.noa_wake_enabled and
                (item.get("kind") == "noa_notice" or
                 str(item.get("held_key", "")).startswith(("noa-notice:", "noa-component:", "noa-stale:"))))

    def reconcile(self, keys=None):
        for key, item in list(self.state["outbox"].items()):
            if keys is not None and key not in keys:
                continue
            if item["status"] != "sending":
                continue
            if self.noa_notice_disabled(item):
                continue
            if keys is None and getattr(self, "isolate_noa", False) and self.is_noa_notice(item):
                continue
            try:
                self.check_time()
            except Exception:
                self.hold(key, item)
                raise
            # Practice records are append-only; if acknowledgement was interrupted, do not
            # fabricate a second preview. Their sequence is reconciled from the local log.
            if self.config.mode == "practice":
                path = self.config.root / "logs" / "practice.jsonl"
                found = False
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if record.get("operation") == "slack_post" and record.get("key") == key:
                            found = True
                            break
                if found:
                    self.acknowledge(key, item, "practice-" + key)
                else:
                    self.hold(key, item)
                continue
            # Exhaustive history pagination is the client's responsibility. A failure or
            # an absent marker is NOT evidence that an uncertain write did not execute.
            oldest = str((self.clock() - timedelta(hours=24)).timestamp())
            try:
                if item.get('delivery_policy') is not None and hasattr(self.slack, 'find_delivery_post'):
                    ts = self.slack.find_delivery_post(item['channel'], item['marker'], oldest,
                                                      item['text'], item['delivery_policy'])
                elif hasattr(self.slack, 'find_exact_post'):
                    ts = self.slack.find_exact_post(item['channel'], item['marker'], oldest,
                                                    item['text'], kind=self.outgoing_kind(key, item))
                else:
                    ts = self.slack.find_post(item["channel"], item["marker"], oldest)
            except Exception as exc:
                self.hold(key, item)
                if not isinstance(exc, RemoteError):
                    raise
                continue
            if ts:
                owner_confirmed = getattr(self.slack, "last_found_owner_post", None) == (item["channel"], item["marker"], ts)
                success_at = None
                if owner_confirmed:
                    try:
                        if not re.fullmatch(r"\d{1,12}\.\d{1,6}", ts):
                            raise ValueError("invalid timestamp")
                        seconds, micros = ts.split(".")
                        success_at = datetime.fromtimestamp(int(seconds), timezone.utc).replace(microsecond=int(micros.ljust(6, "0")))
                        if success_at > self.clock():
                            success_at = None
                    except (ValueError, OverflowError, OSError, TypeError):
                        pass
                self.acknowledge(key, item, ts, owner_post_confirmed=owner_confirmed, owner_success_at=success_at)
            else:
                self.hold(key, item)

    def report_user_health(self, *, post_succeeded=False, success_at=None, save=True):
        if not hasattr(self.slack, "report_user_verified"):
            return
        notices = [(k, v) for k, v in self.state["outbox"].items()
                   if k.startswith("report-user-failed:")]
        if not self.config.slack_report_user_token and not post_succeeded:
            for _, item in notices:
                if not item.get("resolved_at"):
                    item.update(resolved_at=iso(self.clock()), resolution="token_removed")
                    if item["status"] == "queued":
                        item["status"] = "cancelled"
            if save:
                self.save()
            return
        if post_succeeded:
            for _, item in notices:
                if not item.get("resolved_at"):
                    try:
                        # A delayed receipt must not clear a newer failure episode.
                        failed_at = max(dt(item["created_at"]), dt(item["last_failure_at"]))
                        if success_at is None or success_at.tzinfo is None or failed_at.tzinfo is None or success_at < failed_at:
                            continue
                    except (ValueError, TypeError, KeyError):
                        continue
                    item["resolved_at"] = iso(self.clock())
                    item["resolution"] = "owner_post_confirmed"
                    if item["status"] == "queued":
                        item["status"] = "cancelled"
            if save:
                self.save()
            return
        if not self.config.slack_report_user_token or self.slack.report_user_verified:
            return  # auth.test alone is not evidence that posting has recovered.
        active = [(k, v) for k, v in notices if not v.get("resolved_at")]
        key = active[-1][0] if active else "report-user-failed:" + str(len(notices) + 1)
        item = self.queue(key, self.config.instruction_channel,
            "代理投稿用のトークンが使えません。設定を確認してね。代理投稿だけ待機し、ボットの処理は続けます。")
        item["last_failure_at"] = iso(self.clock())
        self.save()
        last = item.get("last_failure_log_at")
        if not last or (self.clock() - dt(last)).total_seconds() >= 3600:
            item["last_failure_log_at"] = iso(self.clock())
            self.save()
            self.event("代理投稿を待機", result="できなかった", memo="代理投稿用設定を確認")

    @staticmethod
    def outgoing_kind(key, item):
        # Pre-v105 relay records used notice. Do not rewrite persisted payloads.
        if item["kind"] == "notice" and key.startswith(("instruction-gdm:", "mini:")):
            return "instruction_relay"
        return item["kind"]

    def drain(self, keys=None):
        if not hasattr(self, "preflight_waiting"):
            self.preflight_waiting = set()
        # Snapshot: acknowledgement may add a mini notice, which the next drain sends.
        for key, item in list(self.state["outbox"].items()):
            if key in self.preflight_waiting:
                continue
            if keys is not None and key not in keys:
                continue
            if item["status"] != "queued":
                continue
            if self.noa_notice_disabled(item):
                continue
            kind = self.outgoing_kind(key, item)
            if (getattr(self, 'duplicate_request_ids', set()) and
                    kind in {'instruction', 'instruction_relay'}):
                continue  # Keep the original fail-closed instruction boundary.
            if item.get('report_bundle_id'):
                # Validate both legs and their immutable report refs before any send.
                report_bundle_status(self.state, item['report_bundle_id'])
            if item.get('requires_posted') and not confirmed_post(self.state, item['requires_posted']):
                continue
            if (self.config.mode == 'live' and item.get('delivery_policy') is not None and
                    hasattr(self.slack, 'post_delivery') and not self.slack.report_user_verified):
                continue
            if (item.get('delivery_policy') is None and hasattr(self.slack, "wants_report_user") and
                    self.slack.wants_report_user(item["channel"], kind) and
                    not self.slack.report_user_verified):
                continue
            if keys is None and getattr(self, "isolate_noa", False) and self.is_noa_notice(item):
                continue
            if ((item["kind"] == "recovery" or "recovery_error_serial" in item) and
                    item.get("recovery_error_serial") != self.state["core_error_serial"]):
                # Queued is known unsent. Legacy generic recoveries have no
                # episode proof; retain their record without emitting old claims.
                item.update(status="cancelled", cancelled_at=iso(self.clock()),
                            reason="stale_or_unversioned_recovery")
                self.save()
                continue
            if ((item["kind"] == "recovery" or "recovery_error_serial" in item)
                    and self.recovery_unresolved()):
                continue
            if (item["kind"] == "deadline_recovery" and
                    self.state["outbox"].get(item["alert_key"], {}).get("status") != "posted"):
                continue
            if self.post_count >= self.config.max_posts:
                return
            if item["kind"] == "instruction" and self.instruction_count >= 1:
                continue
            if (item["kind"] == "instruction" and
                    item.get("request_id") in getattr(self, "invalid_report_ids", set())):
                # A malformed report may already describe this work. Do not
                # dispatch a known-unsent instruction until its source is fixed.
                continue
            self.check_time()
            try:
                if item.get('delivery_policy') is None:
                    assert_outgoing_safe(item["text"], item["channel"], self.config.report_channel,
                        self.config.ice_mention_user_id, self.config.expected_user_id, self.config.secrets)
                else:
                    validate_delivery_text(item['text'], item['delivery_policy'],
                        ice_mention_user_id=self.config.ice_mention_user_id,
                        expected_user_id=self.config.expected_user_id,
                        requester_mention_ids=self.config.requester_mention_ids,
                        secrets=self.config.secrets)
            except UnsafeContent:
                item.update(status="held", hold_reason="unsafe_content", held_at=iso(self.clock()))
                if item["kind"] == "instruction":
                    req = self.current_requests.get(item["request_id"])
                    if req:
                        self.quarantine_request(req)
                else:
                    refs = [compose.safe_reference(r.get("id"), r.get("reporter", "")) for r in item.get("reports", [])]
                    if not refs and item.get("request_id"):
                        refs = [compose.safe_reference(item["request_id"], item.get("reporter", ""))]
                    self.queue("unsafe-outbox:" + key, self.config.report_channel,
                        "安全チェックに通らない投稿文を1件保留しました🍚 内容を転記せず、他の処理を続けます。"
                        + ("\n" + "\n".join(refs) if refs else ""),
                        "unsafe_notice", held_key=key)
                self.save()
                continue
            if item.get('delivery_policy', {}).get('purpose') == 'aisu_instruction':
                try:
                    preflight_dual_source(self, item)
                except (StopRequested, TimeBudgetExceeded, AccountMismatch):
                    raise
                except SafetyError:
                    item.update(status='held', hold_reason='source_changed', held_at=iso(self.clock()))
                    instruction_delivery_status(self.state, item['request_id'])
                    self.save()
                    continue
            if item["kind"] == "instruction":
                if self.duplicate_before_send(item):
                    continue
                self.preflight_instruction(item)
                info = self.state["requests"][item["request_id"]]
                if item.pop("retry_permitted_once", None):
                    info.pop("retry_permitted_once", None)
                    info["retry_consumed_at"] = item["retry_consumed_at"] = iso(self.clock())
            elif item.pop("retry_permitted_once", None):
                item["retry_consumed_at"] = iso(self.clock())
            item["status"] = "sending"
            item["attempted_at"] = iso(self.clock())
            self.save()  # Intent durable before every remote send, including notices.
            self.post_count += 1
            if item["kind"] == "instruction":
                self.instruction_count += 1
            try:
                ts = self.writer.slack_post(item["channel"], item["text"], key, kind=kind,
                                            delivery=item.get('delivery_policy'))
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    item["status"] = "queued"
                    item["last_attempt_result"] = ("stopped_before_send" if isinstance(exc, StopRequested)
                                                   else "deadline_before_send")
                    self.post_count -= 1
                    if item["kind"] == "instruction":
                        self.instruction_count -= 1
                    self.save()
                else:
                    # A stop without proof of a pre-send boundary is uncertain.
                    self.hold(key, item)
                raise
            except RemoteError as exc:
                from .slack import UserTokenUnavailable
                if isinstance(exc, UserTokenUnavailable):
                    self.post_count -= 1
                    if item["kind"] == "instruction":
                        self.instruction_count -= 1
                    item.update(status="queued", last_attempt_result="not_sent")
                    self.save()
                    self.report_user_health()
                    continue
                if getattr(exc, 'integrity_failure', False):
                    item['delivery_integrity_unconfirmed'] = True
                    self.hold(key, item)
                elif exc.uncertain:
                    self.reconcile(keys={key})
                else:
                    # Explicit API rejection is known not to have executed.
                    item["status"] = "queued"
                    self.save()
                raise
            except SafetyError as exc:
                if getattr(exc, "not_sent", False):
                    self.post_count -= 1
                    if item["kind"] == "instruction":
                        self.instruction_count -= 1
                    self.preflight_waiting.add(key)
                    refs = [compose.safe_reference(r.get("id"), r.get("reporter", "")) for r in item.get("reports", [])]
                    if kind == "instruction_relay":
                        refs = [compose.safe_reference(item.get("request_id")) + "／宛先：たかむねりりあいす"]
                    elif not refs and item.get("request_id"):
                        refs = [compose.safe_reference(item["request_id"], item.get("reporter", ""))]
                    item.update(status="queued", last_attempt_result="not_sent")
                    self.queue("preflight:" + key, self.config.report_channel,
                        "送信前の安全確認で待機しました。次の見回りで再確認します。"
                        + ("\n" + "\n".join(refs) if refs else ""))
                    self.save()
                    continue
                self.reconcile(keys={key})
                raise
            except Exception:
                # An unexpected return from a write is also uncertain, never a retry permit.
                self.reconcile(keys={key})
                raise
            owner_confirmed = (self.config.mode == "live" and hasattr(self.slack, "wants_report_user") and
                               (item.get('delivery_policy') is not None or self.slack.wants_report_user(item["channel"], kind)))
            self.acknowledge(key, item, ts, owner_post_confirmed=owner_confirmed, owner_success_at=self.clock())

    def read_all(self):
        self.check_time()
        requests = parse_requests(self.notion.read_blocks(self.config.requests_page))
        seen, duplicates = set(), set()
        for req in requests:
            if req.id in seen:
                duplicates.add(req.id)
            seen.add(req.id)
        self.duplicate_request_ids = duplicates
        if duplicates:
            identity = sorted((r.id, r.block_id, hashlib.sha256(r.text.encode()).hexdigest())
                              for r in requests if r.id in duplicates)
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            self.queue('duplicate-sources:' + digest, self.config.report_channel,
                '同じ依頼IDの原本が複数あるため指示配送を止めています。'
                '該当する報告は依頼元の照合待ちとし、他の完了報告は続けます🍚',
                'source_notice')
        reports = []
        self.invalid_report_ids = set()
        self.invalid_report_outcomes = set()
        self.invalid_reporters = set()
        self.invalid_report_keys = set()
        for name, url in self.config.pages.items():
            self.check_time()
            page_id = url.rstrip("/").split("/")[-1]
            reports.extend(parse_reports(self.notion.read_blocks(page_id), expected_reporter=name,
                on_invalid=lambda block, text, exc, name=name: self.report_problem(name, block, text, exc)))
        self.apply_work_url_policy(reports)
        return requests, reports

    def apply_work_url_policy(self, reports):
        """Retain URL history and safe links, without making them completion gates."""
        versions = {report.block_id + ':' + report.digest for report in reports}
        policy = self.state.get('work_url_policy')
        if policy is None:
            policy = {'version': 1, 'activated_at': iso(self.clock()),
                      'legacy_versions': sorted(versions), 'pending_versions': {}}
            self.state['work_url_policy'] = policy
            self.save()  # Baseline never grows after a restart or partial later read.
        if (not isinstance(policy, dict) or policy.get('version') != 1 or
                not isinstance(policy.get('activated_at'), str) or
                not isinstance(policy.get('legacy_versions'), list) or
                any(not isinstance(v, str) for v in policy['legacy_versions']) or
                not isinstance(policy.get('pending_versions'), dict) or
                any(not isinstance(v, dict) or v.get('status') not in {'pending', 'superseded', 'optional'}
                    for v in policy['pending_versions'].values()) or
                policy.get('completion_requirement', 'optional') != 'optional'):
            raise SafetyError('作業場所URLの移行記録が不正です。初期化せず確認を待ちます')
        policy['completion_requirement'] = 'optional'
        policy.setdefault('optional_since', iso(self.clock()))
        legacy = set(policy['legacy_versions'])
        newest = {}
        for report in reports:
            newest.setdefault((report.id, report.reporter), report.block_id + ':' + report.digest)
        for key, item in policy['pending_versions'].items():
            if key not in versions and item.get('status') == 'pending':
                item.update(status='superseded', superseded_at=iso(self.clock()))
            elif item.get('status') == 'pending':
                # This only retires the old URL requirement. Actual completion
                # still uses current formal reports and every existing guard.
                item.update(status='optional', resolved_at=iso(self.clock()),
                            resolution='url_is_optional')
        for report in reports:
            key = report.block_id + ':' + report.digest
            report.work_url_legacy = key in legacy
            report.work_url_superseded = key != newest[(report.id, report.reporter)]
            if report.work_url:
                report.work_url, report.work_url_status, report.work_url_reason = validate_work_url(
                    report.work_url, self.config.secrets)
        for key, item in self.state['outbox'].items():
            if (key.startswith('work-url:') and item.get('kind') == 'work_url_notice'
                    and item.get('status') == 'queued'):
                # Do not send obsolete mandatory-URL advice. Unknown outcomes
                # and already-posted text/receipts must never be rewritten.
                item.update(status='cancelled', cancelled_at=iso(self.clock()),
                            reason='work_url_optional')
        self.save()

    def report_problem(self, expected_reporter, block, text, exc):
        ids, unknown_id = report_references(text)
        ids.update(report_candidate_ids(text))
        self.invalid_report_ids.update(ids)
        self.invalid_report_outcomes.update((request_id, expected_reporter) for request_id in ids)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        quarantine_key = block['id'] + ':' + digest
        self.invalid_report_keys = getattr(self, 'invalid_report_keys', set())
        self.invalid_report_keys.add(quarantine_key)
        record = self.state['quarantined_reports'].setdefault(quarantine_key, {
            'block_id': block['id'], 'digest': digest, 'reporter': expected_reporter,
            'request_id': next(iter(ids)) if len(ids) == 1 else '',
            'candidate_request_ids': sorted(ids), 'at': iso(self.clock()),
            'reason': 'ambiguous_report' if ids else 'unidentified_report'})
        record.update(status='isolated', needs_review=True)
        fingerprint = hashlib.sha256(json.dumps(
            [expected_reporter, block['id'], text], ensure_ascii=False).encode()).hexdigest()
        key = 'report:' + fingerprint
        if key not in self.state['incomplete_notified']:
            # Never quote the malformed ID, body, reporter or exception message.
            # The page owner is trusted configuration; source references are
            # limited to validated IDs and a syntactically valid Notion block ID.
            reference = compose.safe_reference(next(iter(ids)) if len(ids) == 1 else '', expected_reporter)
            block_id = block['id'].replace('-', '')
            if re.fullmatch(r'[0-9a-fA-F]{32}', block_id):
                reference += '\nhttps://app.notion.com/p/' + block_id
            if unknown_id:
                explanation = ('依頼IDを確定できない報告1件を要確認として隔離しました。原本と履歴は保持します。'
                    + ('読み取れた候補IDの自動完了は待ちます。' if ids else '')
                    + '対象IDと報告者が明確な別の報告は処理を続けます。')
            else:
                explanation = '形式が不正な完了報告1件だけを飛ばし、ほかの依頼・報告を続けます。'
            self.queue('invalid-report:' + fingerprint, self.config.report_channel,
                'この報告の書き方を確認してね🍚 ' + explanation +
                '\n' + reference + '\n報告の識別番号：' + fingerprint[:12])
            self.state['incomplete_notified'][key] = iso(self.clock())
            self.event('完了報告の形式を確認', actor=expected_reporter, result='できなかった',
                       memo=type(exc).__name__ + '／報告の識別番号：' + fingerprint[:12])

    def request_problem(self, req, exc):
        fingerprint = hashlib.sha256(req.text.encode()).hexdigest()
        key = req.id + ":" + type(exc).__name__ + ":" + fingerprint
        if key not in self.state["incomplete_notified"]:
            self.queue("invalid:" + key, self.config.report_channel,
                req.id + " の依頼を処理できません。" + sanitize(str(exc), self.config.secrets)
                + "。書き直されたら次の見回りで確認します🍚")
            self.state["incomplete_notified"][key] = iso(self.clock())
            self.event("依頼の不足で停止", req.id, req.requester, "できなかった", memo=type(exc).__name__)

    def prepare(self, req, info=None):
        # Reject genuinely dangerous page fields before drafting, status writes,
        # or any instruction/self notice. Benign titles/omitted URLs are allowed.
        compose.project_page(req.fields, self.config.secrets)
        others = [name for name in req.recipients if name != "りり"
                  and name not in getattr(self, 'lane_taken', {}).get(req.id, ())]
        if "りり" in req.recipients:
            self.queue("self:" + req.id, self.config.report_channel, compose.riri_notice(req.id),
                       "self", request_id=req.id)
        if not others:
            item = self.state["outbox"].get("instruction:" + req.id)
            if item and item.get("status") == "held" and item.get("hold_reason") == "unsafe_content":
                # A correction to self-only no longer needs an instruction.
                # Preserve its original local record, but do not leave a stale hold.
                item.update(status="cancelled", cancelled_at=iso(self.clock()), reason="source_self_only")
            self.state["requests"][req.id] = {"status": "skipped", "snapshot": asdict(req),
                                             "first_seen": iso(self.clock())}
            self.save()
            return
        if info is None:
            self.check_time()
            if 'local-run=ops-' in req.text:
                text,source=compose.instruction(req,others,self.config.pages),'official_project_canonical'
            else:
                text, source = self.draft.draft(req, others)
            compose.assert_safe(text, self.config.secrets)
            info = {"status": "prepared", "snapshot": asdict(req), "block_id": req.block_id,
                    "first_seen": iso(self.clock()), "recipients": others,
                    "draft_source": source, "draft": text, "picked_event": False,
                    **({"lane_recipients": list(self.lane_given[req.id])} if self.lane_given.get(req.id) else {}),
                    "source_status_observed": req.status, "source_status_observed_at": iso(self.clock())}
            if 'あいす' in others:
                info['delivery_policy_version'] = 'dual-v2'
            self.state["requests"][req.id] = info
            self.save()  # Survives a crash after state update, before Slack intent.
        else:
            saved = Request(**info["snapshot"])
            if (req.block_id != info["block_id"] or
                    replace_status(saved.text, "未着手") != replace_status(req.text, "未着手")):
                raise SafetyError("処理中の依頼本文が変更されています。たかむねの確認が必要です")
        if req.status == "未着手":
            self.check_time()
            self.change_status(req, "対応中", info)
            if self.config.mode == "live":
                req.text = replace_status(req.text, "対応中")
                req.status = "対応中"
        elif req.status != "対応中":
            raise SafetyError("処理中の依頼の状態が変更されています")
        if not info["picked_event"]:
            info["picked_event"] = True
            self.event("依頼を拾った", req.id, req.requester, memo="宛先=" + req.recipients_source)
        source_meta = ({"noa_instruction_wake": {"status": "queued", "attempt_at": None}}
                       if self.config.noa_wake_enabled and "のあ" in others else {})
        if info.get('delivery_policy_version') == 'dual-v2':
            item = queue_aisu_instruction(self, req.id, info, **source_meta)
        else:
            item = self.queue("instruction:" + req.id, self.config.instruction_channel, info["draft"],
                              "instruction", request_id=req.id,
                              relay_to_report=any(name in others for name in ("あいす", "みに")),
                              **source_meta)
        if item["status"] == "blocked":
            item["status"] = "queued"
        elif item["status"] == "held" and item.get("hold_reason") == "unsafe_content":
            # It never reached the transport; retain the old text as a local
            # revision rather than losing history or reusing an unsafe draft.
            item.setdefault("local_revisions", []).append({"text": item["text"], "at": iso(self.clock())})
            item.update(status="queued", text=info["draft"] + "\n" + item["marker"])
            item.pop("hold_reason", None)
        info["status"] = "posting"
        self.save()

    def lane_only(self, req):
        """Every non-りり recipient went to the まむ/みき lane: record it and mark 対応中."""
        info = {"status": "lane", "snapshot": asdict(req), "block_id": req.block_id,
                "first_seen": iso(self.clock()), "recipients": [n for n in req.recipients if n in self.lane_given.get(req.id, ())],
                "lane": sorted(self.lane_taken[req.id]),
                "source_status_observed": req.status, "source_status_observed_at": iso(self.clock())}
        if req.status == "未着手" and self.config.mode == "live":
            # A "lane" request is never prepared again, so the intent to write 対応中 is saved
            # with it (re-review 💚20261125-20, U6 kind): a stop before the write is then
            # recovered as an interrupted write (recover_status_intents), never left silent.
            info["status_before_notion_hold"] = "lane"
            info["notion_write_intent"] = {"status": "対応中", "at": iso(self.clock())}
        self.state["requests"][req.id] = info
        self.save()
        if req.status == "未着手":
            self.check_time()
            self.change_status(req, "対応中", info)
        self.drain()

    def change_status(self, req, status, info):
        if self.config.mode == "live":
            info["status_before_notion_hold"] = info["status"]
            info["notion_write_intent"] = {"status": status, "at": iso(self.clock())}
            self.save()
        try:
            if self.config.mode == "practice":
                self.writer.set_status(req, status, practice_status=info.get("practice_status"))
            else:
                self.writer.set_status(req, status)
        except Exception as exc:
            # Explicit pre-write failures are safe to retry; ambiguous writes and
            # unknown exceptions are held. The intent also protects a hard crash.
            known_not_written = (isinstance(exc, RemoteError) and not exc.uncertain) or (
                isinstance(exc, (SafetyError, StatusMissing)) and not isinstance(exc, NotionConflict)
                and not getattr(exc, "uncertain", False)) or (
                isinstance(exc, TimeBudgetExceeded) and getattr(exc, "not_sent", False)
                and not getattr(exc, "uncertain", False))
            if known_not_written:
                if isinstance(exc, StatusMissing) and not isinstance(exc, StatusUnwritable):
                    # Re-review 💚20261125-16 T3: nothing was written and this block's status
                    # line cannot be found.  Only this request waits (see hold_status_unwritable).
                    # Re-review 💚20261125-20 U6: the write intent stays saved until the hold and
                    # its notice are saved together; a stop in between is then recovered as an
                    # interrupted write (recover_status_intents), never left without a word.
                    raise StatusUnwritable(str(exc)) from exc
                info.pop("notion_write_intent", None)
                info.pop("status_before_notion_hold", None)
                self.save()
                raise
            info.setdefault("status_before_notion_hold", info["status"])
            info.update(status="held", hold_reason="notion_conflict", held_at=iso(self.clock()))
            item = self.state["outbox"].get("instruction:" + req.id)
            if item and item["status"] == "queued":
                item.update(status="held", hold_reason="notion_conflict")
            self.event("Notion同時編集のため保留", req.id, req.requester, "一部完了",
                       memo="自動上書き・自動継続を停止。ローカルの変更前後ログを確認してください")
            self.queue_notion_hold(req.id, info)
            raise
        info.pop("notion_write_intent", None)
        info.pop("status_before_notion_hold", None)
        if self.config.mode == "practice":
            info["practice_status"] = status
        self.save()

    def hold_status_unwritable(self, req, info=None, stage="start", exc=None):
        """T3: this request was accepted but its 状態 line cannot be written.  Hold only this
        request and say so once; the other requests in the same patrol go on.

        ``stage`` says what has already happened, so the notice never asks to redo work:
        "start" (nothing was handed out; write it again under a new ID), "handoff" (a lane
        already took it and its work may be running: set 対応中 by hand, never a new ID, which
        would start the same work twice; re-review 💚20261125-20 U4) or "complete" (the reports
        are in; set 完了 by hand).  The hold and its notice are saved together (U6): the notice
        is queued before anything else is saved."""
        from .notion import StatusUnsupported
        info = info if info is not None else self.state["requests"].setdefault(req.id, {
            "snapshot": asdict(req), "block_id": req.block_id, "first_seen": iso(self.clock()),
            "recipients": list(req.recipients)})
        info.pop("notion_write_intent", None)          # cleared in the same save as the hold (U6)
        info.setdefault("status_before_notion_hold", info.get("status"))
        info.update(status="held", hold_reason="status_unwritable", held_at=iso(self.clock()),
                    status_unwritable_stage=stage)
        item = self.state["outbox"].get("instruction:" + req.id)
        if item and item.get("status") == "queued":
            item.update(status="held", hold_reason="status_unwritable")
        unsupported = isinstance(getattr(exc, "__cause__", None), StatusUnsupported) or isinstance(exc, StatusUnsupported)
        why = ("依頼のブロックにメンションなど文字以外の部分があって、" if unsupported else "")
        command = "patrol.py --resolve status " + req.id + " "
        # The notice also says how the hold is cleared: an uncleared hold keeps every later
        # "直りました" waiting (recovery_unresolved), so following the advice must end the hold.
        if stage == "complete":
            text = (req.id + " は報告がそろったけど、" + why + "「状態」の行を「完了」に書き換えられないので保留したよ🍚 "
                    "やり直しはいらないよ。たかむねが依頼の状態の行を確かめて手で「完了」にしたら、"
                    + command + "done で保留を外してね。ほかの依頼は続けているよ。")
        elif stage == "handoff":
            text = (req.id + " は受け口に渡した（作業はもう始まっているかもしれない）けど、" + why
                    + "「状態」の行を「対応中」に書き換えられないので保留したよ🍚 新しい依頼IDで出し直さないでね"
                    "（同じ作業がもう1回動いちゃう）。たかむねが状態の行を手で「対応中」にしたら、"
                    + command + "checked で保留を外してね。報告はこのまま届くよ。ほかの依頼は続けているよ。")
        else:
            text = (req.id + " は" + why + "「状態」の行を書き換えられないので保留したよ🍚 依頼ID・宛先・依頼した子・状態の行を"
                    "案件ページの行より前に1つずつ置いて" + ("、文字だけで" if unsupported else "")
                    + "新しい依頼IDで書き直してね。書き直したら " + command
                    + "cancel で保留を外してね。ほかの依頼は続けているよ。")
        self.queue("status-unwritable:" + req.id, self.config.report_channel, text,
                   "hold_notice", held_key="instruction:" + req.id)       # saves the hold and the notice together
        self.event("状態を書けないため保留", req.id, req.requester, "一部完了",
                   memo="状態の行を書き換えられない（" + stage + "）。ほかの依頼は続ける")

    def queue_notion_hold(self, request_id, info):
        from .resolve import notion_checked_restore
        episode = info["notion_hold_episode"] = info.get("notion_hold_episode", 0) + 1
        # The notice also says how the hold is cleared: an uncleared hold keeps every later
        # "直りました" waiting (recovery_unresolved). It names only a command that
        # `--resolve` will accept for this hold (notion_checked_restore).
        command = "patrol.py --resolve notion " + request_id + " "
        restore = notion_checked_restore(info)
        text = request_id + " のNotion更新を確認できないため保留しました🍚 自動継続せず、たかむねの確認を待ちます。"
        if restore == "lane":
            text += ("受け口に渡した依頼なので、新しい依頼IDで出し直さないでね（同じ作業がもう1回動いちゃう）。"
                     "依頼の本文と状態の行をローカルの記録と照らして確かめ、状態の行を「対応中」にしたら "
                     + command + "checked で続きへ（報告はこのまま届くよ）、取りやめるなら "
                     + command + "cancel で保留を外してね。")
        elif restore:
            text += ("依頼の本文と状態の行をローカルの記録と照らして確かめたら " + command
                     + "checked で続きへ、取りやめるなら " + command + "cancel で保留を外してね。")
        else:
            text += "続きには戻せない保留なので、取りやめるなら " + command + "cancel で保留を外してね。"
        self.queue("notion-hold:" + request_id + ":" + str(episode), self.config.report_channel,
                   text, "hold_notice", held_key="instruction:" + request_id)

    def recover_status_intents(self):
        for request_id, info in self.state["requests"].items():
            if not info.get("notion_write_intent") or info["status"] == "held":
                continue
            info.setdefault("status_before_notion_hold", info["status"])
            info.update(status="held", hold_reason="notion_conflict", held_at=iso(self.clock()))
            self.event("Notion更新中断のため保留", request_id, result="一部完了",
                       memo="書込後確認の確定前に中断。ローカル監査ログを手動確認してください")
            self.queue_notion_hold(request_id, info)

    def requests(self, requests):
        by_id = {r.id: r for r in requests}
        corrected = set()
        for request_id, info in list(self.state["requests"].items()):
            if request_id in self.invalid_report_ids:
                continue
            if info.get("status") == "source_removed":
                # A reappearing ID must never silently disappear or revive a
                # previously cancelled instruction. One fixed notice asks for
                # a new ID; its outbox key also prevents duplicate notices.
                req = by_id.get(request_id)
                key = "source-reappeared:" + request_id
                if req is not None and req.status != "完了" and key not in self.state["outbox"]:
                    self.queue(key, self.config.report_channel,
                        "取りさげた依頼IDがもう一度見つかりました🍚 自動で復活・投稿しません。必要な依頼は新しいIDで書いてください。\n"
                        + compose.safe_reference(request_id), "source_notice", request_id=request_id)
                    self.event("取りさげた依頼IDの再登場を確認", request_id,
                               memo="同じIDを再利用せず新しいIDで作成するよう通知")
                continue
            if (info.get("hold_reason") == "unsafe_content" or
                    info.get("status") == "retry_after_edit"):
                # Keep corrections visible until validation and preparation succeed.
                # Also repair the intermediate status used by older implementations.
                info.update(status="held", hold_reason="unsafe_content")
                req = by_id.get(request_id)
                if req is None:
                    info.update(status="source_removed", hold_reason="", source_removed_at=iso(self.clock()))
                    item = self.state["outbox"].get("instruction:" + request_id)
                    if item and item.get("hold_reason") == "unsafe_content" and item["status"] == "held":
                        item.update(status="cancelled", cancelled_at=iso(self.clock()), reason="source_removed")
                    self.event("保留の元依頼が見つからない", request_id, memo="自動投稿せず記録を保持")
                elif hashlib.sha256(req.text.encode()).hexdigest() != info.get("unsafe_digest"):
                    corrected.add(request_id)
                self.save()
                continue
            if info["status"] == "posted":
                self.ensure_followups(request_id, info)
            if info["status"] == "prepared":
                if request_id not in by_id:
                    raise SafetyError("処理中の依頼が見つかりません")
                try:
                    self.prepare(by_id[request_id], info)
                except compose.ProjectPageUnsafe:
                    self.quarantine_request(by_id[request_id], project_page=True)
                    continue
                except StatusUnwritable as exc:
                    self.hold_status_unwritable(by_id[request_id], info, exc=exc)
                    continue
                self.drain()
                return
        if self.instruction_count or self.post_count >= self.config.max_posts:
            return
        for req in reversed(requests):
            if req.id in self.invalid_report_ids:
                continue
            if req.id in self.state["requests"] and req.id not in corrected:
                continue
            previous = self.state["requests"].get(req.id, {})
            retry_after_edit = req.id in corrected
            item = self.state["outbox"].get("instruction:" + req.id, {})
            known_unsent_retry = (retry_after_edit and item.get("status") == "held" and
                                 item.get("hold_reason") == "unsafe_content")
            if req.status == "完了" and retry_after_edit:
                previous.update(status="cancelled", hold_reason="", resolution="source_completed", resolved_at=iso(self.clock()))
                if item.get("status") == "held" and item.get("hold_reason") == "unsafe_content":
                    item.update(status="cancelled", reason="source_completed", cancelled_at=iso(self.clock()))
                self.save()
                continue
            if req.status in ("対応中", "完了") and not (req.status == "対応中" and known_unsent_retry):
                continue
            try:
                validate_request(req)
            except (RequestIncomplete, StatusMissing) as exc:
                self.request_problem(req, exc)
                continue
            if req.status != "未着手" and not known_unsent_retry:
                raise StatusMissing("状態が書かれていません")
            same_id_reports = [report for report in self.current_reports if report.id == req.id]
            if same_id_reports:
                from .local_tasks import received_before_patrol
                if not retry_after_edit and received_before_patrol(self.config, req, same_id_reports):
                    # The formal actor already completed and reported this exact request.
                    # Normal report sharing/assessment below has its own durable receipt.
                    continue
                raise DuplicateRequestId("新しい依頼と同じIDの過去報告があります。新しい依頼IDで確認してください")
            duplicate = older_active_duplicate(req, requests, self.state)
            if duplicate is not None:
                self.hold_content_duplicate(req, *duplicate)
                continue
            from .agent_lane import intake as lane_intake
            try:
                lane = lane_intake(self, req)
            except SafetyError as exc:
                self.request_problem(req, RequestIncomplete(str(exc)))
                continue
            if lane.defer:
                continue
            if lane.taken:
                self.lane_given[req.id] = list(lane.taken)
            if lane.only:
                self.lane_taken[req.id] = set(lane.taken) | set(lane.off)
                if not [n for n in req.recipients if n != 'りり' and n not in self.lane_taken[req.id]]:
                    if lane.taken:
                        try:
                            self.lane_only(req)
                        except StatusUnwritable as exc:
                            # the lane already took it (jobs exist): never ask for a new ID (U4)
                            self.hold_status_unwritable(req, stage="handoff", exc=exc)
                    continue
            if self.config.mode=='live' and req.recipients==['みき'] and 'local-run=ops-' not in req.text and not re.search(r'(?<![\w-])local-run=gemma26-A-v1(?:\s|$)',req.text):
                from .bounded_projects import hold_reason
                self.request_problem(req,RequestIncomplete(hold_reason(None,req)))
                continue
            if self.config.mode=='live' and 'local-run=ops-' in req.text:
                from .local_tasks import LocalTasks
                from .project_ops import patrol_intake
                svc=LocalTasks(self.config)
                svc.setup()
                if not patrol_intake(svc,req,requests):
                    from .bounded_projects import hold_reason
                    self.request_problem(req,RequestIncomplete(hold_reason(svc,req)))
                    continue
            try:
                self.prepare(req)
            except StopRequested:
                raise
            except StatusUnwritable as exc:
                self.hold_status_unwritable(req, exc=exc)
                continue
            except compose.ProjectPageUnsafe:
                self.quarantine_request(req, project_page=True)
                continue
            except UnsafeContent:
                self.quarantine_request(req)
                continue
            self.drain()
            if self.instruction_count or self.post_count >= self.config.max_posts:
                break

    def hold_content_duplicate(self, req, original_id, digest, item=None):
        """Retain original source/snapshot and known-unsent intent without replay."""
        info = self.state['requests'].setdefault(req.id, {
            'snapshot': asdict(req), 'block_id': req.block_id,
            'first_seen': iso(self.clock()), 'recipients': list(req.recipients)})
        info.update(status='held', hold_reason='content_duplicate', duplicate_of=original_id,
                    execution_digest=digest, held_at=iso(self.clock()),
                    source_status_observed=req.status, source_status_observed_at=iso(self.clock()))
        if item is not None:
            item.update(status='held', hold_reason='content_duplicate', duplicate_of=original_id,
                        execution_digest=digest, held_at=iso(self.clock()))
        key = 'content-duplicate:' + req.id
        if key not in self.state['outbox']:
            self.queue(key, self.config.report_channel,
                '活動中の依頼と宛先・対象・実行内容が同じため、後から来た依頼を保留しました🍚 '
                '原本と履歴は保持し、自動では配送・再開しません。\n'
                + compose.safe_reference(req.id) + '\n先の依頼：' + compose.safe_reference(original_id),
                'duplicate_notice', request_id=req.id, duplicate_of=original_id)
            self.event('同じ実行内容の後発依頼を保留', req.id, req.requester,
                       result='一部完了', memo='元依頼：' + original_id)
        self.save()

    def quarantine_request(self, req, project_page=False):
        digest = hashlib.sha256(req.text.encode()).hexdigest()
        previous = self.state["requests"].get(req.id, {})
        self.state["requests"][req.id] = dict(previous, status="held", hold_reason="unsafe_content",
            unsafe_digest=digest, block_id=req.block_id, first_seen=previous.get("first_seen", iso(self.clock())),
            source_status_observed=req.status, source_status_observed_at=iso(self.clock()))
        key = "unsafe-request:" + req.block_id + ":" + digest
        if key not in self.state["outbox"]:
            self.queue(key, self.config.report_channel,
                ("依頼の案件ページ欄を確認してね🍚 安全チェックに通らないため指示前に保留しました。本文は転記しません。\n"
                 if project_page else "投稿文の安全チェックに通らない依頼を1件保留しました🍚 内容を転記せず、他の依頼を続けます。元の依頼を確認してください。\n")
                + compose.safe_reference(req.id),
                "unsafe_notice")
            self.event("安全でない投稿文を保留", req.id, result="できなかった", memo="本文を修正したら再確認")
        self.save()

    def hold_completion(self, request_id, info, reason):
        """Persist a local review and one notice, without undoing delivery."""
        previous = info.get('completion_hold')
        if previous is not None and (not isinstance(previous, dict) or
                previous.get('status') not in {'pending', 'resolved'}):
            raise SafetyError('完了判定の確認待ち記録が不正です')
        first = not previous or previous['status'] != 'pending'
        if first:
            episode = (previous.get('episode', 0) if previous else 0) + 1
            info['completion_hold'] = {'status': 'pending', 'reason': reason,
                'episode': episode, 'first_observed_at': iso(self.clock())}
        record = info['completion_hold']
        record.update(reason=reason, last_observed_at=iso(self.clock()))
        key = 'completion-review:' + request_id + ':' + str(record['episode'])
        record['notice_key'] = key
        self.queue(key, self.config.report_channel,
            compose.safe_reference(request_id) + ' の元依頼の本文・ブロック・状態を確認できないため、'
            'この依頼の自動完了だけを確認待ちにしました🍚 '
            'ほかの依頼・報告・ログ同期は続けます。配信時の内容との照合をお願いします。',
            'completion_notice', persist=False, request_id=request_id)
        if first:
            self.event('完了判定だけ確認待ち', request_id, result='一部完了',
                       memo=reason + '：元依頼・配信記録は変更せず、他の見回りを継続')
        else:
            self.save()

    def resolve_completion(self, request_id, info):
        record = info.get('completion_hold')
        if record and record.get('status') == 'pending':
            record.update(status='resolved', resolved_at=iso(self.clock()))
            notice = self.state['outbox'].get(record.get('notice_key'))
            if notice and notice.get('status') == 'queued':
                notice.update(status='cancelled', cancelled_at=iso(self.clock()),
                              reason='completion_source_restored')
            self.event('完了判定の原本一致を再確認', request_id,
                       memo='配信時と同じ原本へ復帰。作業結果・状態更新は別に確認')

    def reports(self, requests, reports, first_run):
        if first_run:
            return
        accepted = []
        for r in reports:
            fingerprint = hashlib.sha256(json.dumps(
                [r.reporter, r.block_id, r.text], ensure_ascii=False).encode()).hexdigest()
            if "report:" + fingerprint in self.state["incomplete_notified"]:
                self.invalid_report_outcomes.add((r.id, r.reporter))
                self.state["seen_reports"][r.block_id] = r.digest
                versions = self.state["seen_report_versions"].setdefault(r.block_id, [])
                if r.digest not in versions:
                    versions.append(r.digest)
                continue
            accepted.append(r)
        reports = accepted
        self.save()
        current_versions = ({r.block_id + ":" + r.digest for r in reports} |
                            getattr(self, 'invalid_report_keys', set()))
        for key, record in self.state["quarantined_reports"].items():
            if key in current_versions:
                record["status"] = ('isolated' if record.get('reason') in
                    {'ambiguous_report', 'unidentified_report'} else 'held')
            elif record["status"] in {"held", "isolated"}:
                record.update(status="superseded", superseded_at=iso(self.clock()))
        pending_versions = report_refs_pending(self.state)
        new = [r for r in reports if r.digest not in self.state["seen_report_versions"].get(r.block_id, [])
               and (r.block_id, r.digest) not in pending_versions]
        fields_by_id = {req.id: req.fields for req in requests}
        for request_id, info in self.state["requests"].items():
            snapshot = info.get("snapshot")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("fields"), dict):
                fields_by_id[request_id] = snapshot["fields"]
        # Bound each summary while preserving unreported versions for later runs.
        chunks, by_requester = [], {}
        for report in new:
            quarantine_key = report.block_id + ":" + report.digest
            if quarantine_key in self.state["quarantined_reports"]:
                continue
            try:
                if len(compose.report_summary([report], fields_by_id, self.config.secrets)) > 2800:
                    raise UnsafeContent("完了報告の安全な文面が長すぎます")
            except UnsafeContent as exc:
                self.state["quarantined_reports"][quarantine_key] = {
                    "block_id": report.block_id, "digest": report.digest, "status": "held",
                    "request_id": report.id, "reporter": report.reporter,
                    "reason": "unsafe_content", "at": iso(self.clock())}
                self.queue("unsafe-report:" + quarantine_key, self.config.report_channel,
                    ("依頼の案件ページ欄を確認してね🍚 案件ページの安全チェックに通らない完了報告を1件保留しました。本文は転記しません。\n"
                     if isinstance(exc, compose.ProjectPageUnsafe) else
                     "投稿文の安全チェックに通らない完了報告を1件保留しました🍚 内容を転記せず、他の報告を続けます。元の報告を確認してください。\n")
                    + compose.safe_reference(report.id, report.reporter),
                    "unsafe_notice")
                self.event("安全でない報告文を保留", report.id, result="できなかった", memo="本文を修正したら再確認")
                continue
            try:
                requester = resolve_requester(report, requests, self.state)
            except SafetyError:
                self.state.setdefault('report_requester_waiting', {})[quarantine_key] = {
                    'block_id': report.block_id, 'digest': report.digest,
                    'request_id': report.id, 'reporter': report.reporter,
                    'status': 'pending', 'reason': 'requester_unconfirmed', 'at': iso(self.clock())}
                self.save()
                continue
            waiting = self.state.setdefault('report_requester_waiting', {}).get(quarantine_key)
            if waiting:
                waiting.update(status='resolved', requester=requester)
            batch = by_requester.get(requester, [])
            candidate = batch + [report]
            if batch and len(compose.report_summary(candidate, fields_by_id, self.config.secrets)) > 2800:
                chunks.append((requester, batch))
                by_requester[requester] = [report]
            else:
                by_requester[requester] = candidate
        chunks.extend((requester, batch) for requester, batch in by_requester.items() if batch)
        for requester, batch in chunks:
            identifier = bundle_id(batch, requester)
            if identifier in self.state.setdefault('report_deliveries', {}):
                continue
            body = 'お願いもと：' + requester + '\n' + compose.report_summary(batch, fields_by_id, self.config.secrets)
            policy = make_delivery_policy(self.config, 'requester_report', bundle_id=identifier,
                                          canonical_text=body, requester=requester)
            keys = ['reports:' + identifier + ':instruction', 'reports:' + identifier + ':group']
            serialized = [asdict(r) for r in batch]
            bundle = {
                'reports': serialized, 'requester': requester, 'keys': keys,
                'policy_version': 'dual-v2', 'status': 'pending',
                'mention_mode': policy['mention_mode'], 'recipient_receipt': 'unverified',
                'created_at': iso(self.clock())}
            intents = [(key, channel, body, 'reports', dict(reports=serialized,
                       report_bundle_id=identifier, delivery_policy=policy))
                       for key, channel in zip(keys, (self.config.instruction_channel, self.config.report_channel))]
            queue_pair(self, intents, 'report_deliveries', identifier, bundle)
        self.drain()
        by_id = {r.id: r for r in requests}
        # First occurrence in each report page is newest. A newer partial outcome
        # does not inherit an older complete outcome for the same person and ID.
        outcomes = {}
        for report in reports:
            if report.block_id + ":" + report.digest in self.state["quarantined_reports"]:
                outcomes.setdefault((report.id, report.reporter), "保留")
                continue
            outcomes.setdefault((report.id, report.reporter), report.result)
        for request_id, info in self.state["requests"].items():
            if request_id in getattr(self, 'duplicate_request_ids', set()):
                continue
            # まむ/みき lane recipients complete a request like any other recipient.
            names = list(info.get("recipients") or []) + list(info.get("lane_recipients") or [])
            if info["status"] not in ("posted", "lane") or not names:
                continue
            eligible = all(name not in self.invalid_reporters and
                   (request_id, name) not in self.invalid_report_outcomes and
                   outcomes.get((request_id, name)) == "完了" for name in names)
            if not eligible and info.get('completion_hold', {}).get('status') != 'pending':
                continue
            req = by_id.get(request_id)
            from .completion_review import source_issue
            reason = source_issue(req, info, live=self.config.mode == 'live')
            if reason:
                self.hold_completion(request_id, info, reason)
                continue
            self.resolve_completion(request_id, info)
            if eligible:
                if req.status != "完了":
                    # In practice, external state stays 未着手 by design.
                    self.check_time()
                    if self.config.mode == "practice" and req.status == "未着手" and info.get("practice_status") is None:
                        # v0.2 practice states did not persist the virtual status.
                        # Revalidate a dry-run transition instead of guessing it or
                        # changing the actual source to make completion pass.
                        self.change_status(req, "対応中", info)
                    try:
                        self.change_status(req, "完了", info)
                    except StatusUnwritable as exc:
                        self.hold_status_unwritable(req, info, stage="complete", exc=exc)
                        continue
                info["status"] = "done"
                self.event("完了にした", request_id, req.requester,
                           evidence="https://app.notion.com/p/" + self.config.requests_page.replace("-", ""))

    def log_hold(self, batch):
        # Durable stable intent: posted/sending/held notices are never resent.
        # No event() here: a log sink failure must not recursively feed itself.
        self.log_unconfirmed = True
        self.logs.operational("Notionログの書込結果は未確認。自動再書込せず照合のみ継続")
        if self.slack.verified:
            self.queue("log-hold:" + batch["batch_id"], self.config.report_channel,
                "Notionログの書き込み結果を確認できないため保留しています🍚 自動で重ねて書かず、次の見回りで照合します。解消しないときは確認してください。")

    def log_sync_notices(self):
        # Preserve v1.0.3's original key and every durable outbox record.
        return {key: item for key, item in self.state["outbox"].items()
                if key == "log-sync-failed" or
                re.fullmatch(r"log-sync-failed:[1-9][0-9]*", key)}

    def log_sync_pending(self):
        return any(item.get("reason") != "log_sync_confirmed"
                   for item in self.log_sync_notices().values())

    def log_sync_failure_key(self):
        notices = self.log_sync_notices()
        # One notification per continuous failure, including across restarts.
        for key, item in notices.items():
            if item.get("reason") != "log_sync_confirmed":
                return key
        if not notices:
            return "log-sync-failed"
        serial = max([1] + [int(key.split(":")[1]) for key in notices if ":" in key]) + 1
        return "log-sync-failed:" + str(serial)

    def confirm_log_sync(self):
        for notice in self.log_sync_notices().values():
            # Never rewrite old text, markers, or delivery status. A future
            # failure receives its own key only after confirmed recovery.
            notice["reason"] = "log_sync_confirmed"

    def flush(self, force=False):
        try:
            self.flush_log_buffer(force)
        except (StopRequested, TimeBudgetExceeded):
            raise
        except AccountMismatch as exc:
            # Do not enter the Slack-mismatch-only log sink fallback below
            # when the mismatching operation was the log sink itself.
            exc.log_account_mismatch = True
            raise
        except (RemoteError, SafetyError) as exc:
            # Only the log sink is isolated. Keep every event and uncertain
            # intent; the core can still drain reports and complete this run.
            batch = self.state["log_batch"]
            if batch:
                self.log_hold(batch)
            else:
                self.log_unconfirmed = True
                # Existing field also bounds known failed/pre-write attempts.
                self.state["last_log_flush_at"] = iso(self.clock() - timedelta(minutes=self.config.log_flush_min)
                    if isinstance(exc, SafetyError) and getattr(exc, "not_sent", False) else self.clock())
                key = self.log_sync_failure_key()
                notice = self.state["outbox"].get(key)
                if notice:
                    notice["reason"] = "log_sync_pending"
                if self.slack.verified:
                    # queue() persists new notices atomically with the cooldown;
                    # existing notices are committed with their pending marker
                    # in the save below. No confirmed+new-cooldown crash window.
                    self.queue(key, self.config.report_channel,
                        ("Notionログの書込前確認で待機しました。次の見回りで再確認します。ログ表と接続を確認してください。"
                         if isinstance(exc, SafetyError) and getattr(exc, "not_sent", False) else
                         "Notionログを同期できませんでした🍚 内容は保持し、通常の同期間隔まで待ちます。ログ表と接続を確認してください。"),
                        reason="log_sync_pending")
                self.save()
                self.logs.operational("Notionログの同期に失敗。内容を保持し、次の同期時刻まで待機")

    def flush_log_buffer(self, force=False):
        buffer = self.state["log_buffer"]
        old_batch = self.state["log_batch"]
        if not buffer and not old_batch:
            return
        now = self.clock()
        # Pending outcomes are always READ-only reconciled, even within the
        # normal append interval. Absence never grants permission to rewrite.
        if not old_batch and not force and (now - dt(self.state["last_log_flush_at"])).total_seconds() < self.config.log_flush_min * 60:
            # Do not claim global recovery while a known failed log sync still
            # has buffered data. Reuse the existing notice as durable evidence.
            if self.log_sync_pending():
                self.log_unconfirmed = True
            return
        self.check_time()
        table, header, existing = self.notion.find_log_table(self.config.log_page)
        # find_log_table already reads and validates the entire table. Reuse
        # that snapshot for deduplication. append_rows independently checks the
        # header before writing and verifies all event IDs after writing.
        known = row_ids(existing)
        if self.config.mode == "practice":
            path = self.config.root / "logs" / "practice.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        raise SafetyError("練習ログが破損しています。削除せず確認してください") from None
                    if record.get("operation") == "notion_log":
                        known.update(ev["event_id"] for ev in record["events"])
        if old_batch:
            ids = set(old_batch["event_ids"])
            if ids <= known:
                self.state["log_buffer"] = [ev for ev in buffer if ev["event_id"] not in ids]
                self.state["log_batch"] = None
                self.state["last_log_flush_at"] = iso(now)
                self.confirm_log_sync()
                notice = self.state["outbox"].get("log-hold:" + old_batch["batch_id"])
                if notice and notice["status"] == "queued":
                    notice.update(status="cancelled", cancelled_at=iso(now), reason="log_confirmed")
                self.save()
            else:
                self.log_hold(old_batch)
            # This run only reconciles the old outcome, never appends a new
            # batch (including old error events) immediately after confirmation.
            return
        self.state["log_buffer"] = [ev for ev in buffer if ev["event_id"] not in known]
        self.save()
        ordered = sorted(reversed(self.state["log_buffer"]), key=lambda ev: ev["at"], reverse=True)
        # Oldest chunk first; every chunk is prepended after the header.
        chunks = [ordered[i:i + 100] for i in range(0, len(ordered), 100)]
        for events in reversed(chunks):
            self.check_time()
            ids = {ev["event_id"] for ev in events}
            batch_id = hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]
            self.state["log_batch"] = {"batch_id": batch_id, "event_ids": sorted(ids), "status": "sending"}
            self.save()
            try:
                self.writer.append_log(self.config.log_page, table, header, events, notion_rows(events))
            except (StopRequested, TimeBudgetExceeded) as exc:
                if getattr(exc, "not_sent", False) and not getattr(exc, "uncertain", False):
                    # The log append has not started. Preserve all events and
                    # allow a later normal flush, without a false unknown batch.
                    self.state["log_batch"] = None
                    self.save()
                raise
            except SafetyError as exc:
                if getattr(exc, "not_sent", False):
                    self.state["log_batch"] = None
                    self.save()
                raise
            except RemoteError as exc:
                if not exc.uncertain:
                    # flush() atomically saves this known-unwritten result
                    # with its cooldown and pending notice metadata. A crash
                    # before that commit conservatively retains the old batch.
                    self.state["log_batch"] = None
                raise
            self.state["log_buffer"] = [ev for ev in self.state["log_buffer"] if ev["event_id"] not in ids]
            self.state["log_batch"] = None
            self.save()
        self.state["last_log_flush_at"] = iso(now)
        self.confirm_log_sync()
        self.save()

    def maintenance(self):
        now = self.clock()
        previous = self.state["last_summary_at"]
        if (now - dt(previous)).total_seconds() < self.config.log_flush_min * 60:
            return
        self.state["last_summary_at"] = iso(now)
        pending = any(x["kind"] == "maintenance" and x["status"] in ("queued", "sending", "held")
                      for x in self.state["outbox"].values())
        size = sum(p.stat().st_size for directory in ("logs", "state")
                   for p in (self.config.root / directory).rglob("*") if p.is_file() and not p.is_symlink())
        last = self.state["last_log_size_notice_at"]
        size_notice = size > self.config.log_size_notice_bytes and (
            not last or (now - dt(last)).total_seconds() >= 7 * 86400)
        buffer_notice = len(self.state["log_buffer"]) > 500 and not self.state["buffer_warning_sent"]
        lines = []
        if size_notice:
            lines.append(f"ローカルのログ・状態ファイルが合計{size:,}バイトになりました🍚 "
                         "自動削除はしません。たかむね、整理するか確認をお願いします。")
        if buffer_notice:
            lines.append("Notionへ書けていないログが500件を超えました。消さずに保持しています。確認をお願いします。")
        if lines and not pending:
            self.queue("maintenance:" + iso(now), self.config.report_channel,
                       "3時間ごとのまとめ報告\n" + "\n".join(lines), "maintenance",
                       size_notice=size_notice, buffer_notice=buffer_notice)
        self.save()

    def handle_error(self, exc):
        name = type(exc).__name__
        now = self.clock()
        self.logs.operational(name + ": " + sanitize(str(exc), self.config.secrets))
        if not isinstance(exc, PatrolError):
            self.unexpected_notifier()
        previous = self.state["errors"].get(name, {})
        self.state["last_error"] = name
        self.state["core_error_serial"] += 1
        # A new failure invalidates only known-unsent recovery intents. Never
        # relabel uncertain sending/held records or pretend they were delivered.
        for item in self.state["outbox"].values():
            if (item["status"] == "queued" and
                    (item["kind"] == "recovery" or "recovery_error_serial" in item) and
                    item.get("recovery_error_serial") != self.state["core_error_serial"]):
                item.update(status="cancelled", cancelled_at=iso(now), reason="superseded_core_error")
        eligible = not previous.get("last_notified_at") or (now - dt(previous["last_notified_at"])).total_seconds() >= self.config.error_notify_min * 60
        self.event("エラーで停止", result="できなかった", memo=name)
        pending_same = any(item["kind"] == "error" and item.get("error_name") == name and item["status"] in ("queued", "sending", "held")
                           for item in self.state["outbox"].values())
        if eligible and not pending_same and self.slack.verified and not self.config.stop.exists():
            bucket = iso(now)
            error_key = "error:" + name + ":" + bucket
            self.queue(error_key, self.config.report_channel,
                       "りりの見回りでエラーが出たので止めました🍚 " + name + "："
                       + (sanitize(str(exc), self.config.secrets)[:160] if isinstance(exc, PatrolError)
                          else "想定外のエラーです。ローカルログを確認してください") + "（" + iso(now) + " JST）",
                       "error", error_name=name)
            self.save()
            try:
                self.drain(keys={error_key})
            except Exception:
                # Keep durable sending/queued entries, never a recursive error send.
                self.logs.operational("エラー通知を送れませんでした。結果は状態ファイルで確認待ち")

    def record_deadline(self):
        """Local-only exit: never reset the timer or attempt I/O on expired budget."""
        status = self.state["deadline_state"]
        if not status["consecutive"]:
            status["episode"] += 1
            status["first_at"] = iso(self.clock())
            status["alert_key"] = None
        status["consecutive"] += 1
        baseline = max(dt(self.state["last_run_at"] or status["first_at"]), dt(status["first_at"]))
        due = (status["consecutive"] >= 3 or
               (self.clock() - baseline).total_seconds() >= 1800)
        if due and not status["alert_key"]:
            key = "deadline-alert:" + str(status["episode"])
            status["alert_key"] = key
            self.queue(key, self.config.report_channel,
                       "時間切れが続いています🍚 見回りを最後まで完了できていません。", "deadline_alert")
        self.save()

    def complete_patrol(self):
        # Called only after every required step and a final STOP/deadline check.
        # Recovery is a durable intent for the next safe start, not late exit I/O.
        status = self.state["deadline_state"]
        alert_key, episode = status["alert_key"], status["episode"]
        status.update(consecutive=0, first_at=None, alert_key=None)
        # The deadline grace period starts at completion, not at run entry.
        self.state["last_run_at"] = iso(self.clock())
        if alert_key:
            # A post-cap deferral may leave a known-unsent generic recovery from
            # an earlier completion. Fold it into this deadline recovery atomically.
            for item in self.state["outbox"].values():
                if (item["kind"] == "recovery" and item["status"] == "queued" and
                        item.get("recovery_error_serial") == self.state["core_error_serial"]):
                    item.update(status="cancelled", cancelled_at=iso(self.clock()),
                                reason="merged_deadline_recovery")
        recovery_meta = ({"recovery_error_serial": self.state["core_error_serial"]}
                         if self.error_recovery_due(allow_unresolved=bool(alert_key)) else {})
        if alert_key:
            key = "deadline-recovery:" + str(episode)
            self.queue(key, self.config.report_channel,
                       "直りました🍚🔥 時間切れが解消し、依頼・報告の見回りを最後まで実行できました。",
                       "deadline_recovery", alert_key=alert_key, **recovery_meta)
        elif recovery_meta:
            self.queue("recovery:v08:" + str(self.state["core_error_serial"]),
                       self.config.report_channel, "直りました🍚🔥", "recovery", **recovery_meta)
        self.save()

    def recovery_unresolved(self):
        return (bool(completion_review_items(self.state)) or self.log_unconfirmed or
                      (bool(self.state["log_buffer"]) and self.log_sync_pending()) or
                      any(x["status"] in ("sending", "held") for x in self.state["outbox"].values()) or
                      any(x.get("noa_instruction_wake", {}).get("status") in {"sending", "held", "rejected"} for x in self.state["outbox"].values()) or
                      any(x["status"] == "held" for x in self.state["requests"].values()) or
                      any(x["status"] == "held" for x in self.state["quarantined_reports"].values()) or
                      bool(self.state["log_batch"]) or
                      any(x["status"] in ("sending", "held") for x in self.state["noa_wake"]["items"].values()))

    def error_recovery_due(self, allow_unresolved=False):
        pending = any(x["status"] in ("queued", "sending", "held") and
                      (x["kind"] == "recovery" or x.get("recovery_error_serial") == self.state["core_error_serial"])
                      for x in self.state["outbox"].values())
        return bool(self.state["last_error"] and
                    (allow_unresolved or not self.recovery_unresolved()) and not pending)

    def deadline_notices(self):
        keys = {key for key, item in self.state["outbox"].items()
                if item["kind"] in {"deadline_alert", "deadline_recovery"} or
                str(item.get("held_key", "")).startswith(("deadline-alert:", "deadline-recovery:"))}
        try:
            self.reconcile(keys=keys)
            self.drain(keys=keys)
        except (StopRequested, TimeBudgetExceeded, AccountMismatch):
            raise
        except Exception:
            self.logs.operational("時間切れの通知は未確認。送信状態を保持")

    def optional_noa(self, wake):
        """Optional component failures never prevent the core patrol from running."""
        if not self.config.noa_wake_enabled:
            wake.run()  # Local OFF transition only; no notice/history reconciliation.
            return True
        component_running = False
        try:
            keys = {key for key, item in self.state["outbox"].items() if self.is_noa_notice(item)}
            self.reconcile(keys=keys)
            component_running = True
            wake.run()
            component_running = False
            keys = {key for key, item in self.state["outbox"].items() if self.is_noa_notice(item)}
            self.drain(keys=keys)
            return self.state["noa_wake"]["failure_reason"] is None
        except (StopRequested, TimeBudgetExceeded, AccountMismatch):
            raise
        except Exception as exc:
            # Never copy remote response bodies or arbitrary exception details.
            self.logs.operational("のあの受付を一時停止：" + sanitize(type(exc).__name__, self.config.secrets))
            try:
                if component_running:
                    wake.component_failure()
                keys = {key for key, item in self.state["outbox"].items() if self.is_noa_notice(item)}
                self.drain(keys=keys)
            except (StopRequested, TimeBudgetExceeded, AccountMismatch):
                raise
            except Exception:
                self.logs.operational("のあの受付の通知は未確認。自動再投稿せず状態を保持")
            return False

    def run(self, flush_log=False):
        # STOP is checked before lock/state/auth/LLM. Only one operational line is allowed.
        if self.config.stop.exists():
            self.logs.operational("STOPのため終了")
            return {"status": "stopped", "posts": 0}
        with patrol_lock(self.config.root) as acquired:
            if not acquired:
                self.logs.operational("ロック中のためスキップ")
                return {"status": "locked", "posts": 0}
            self.started_at, self.started_mono = self.clock(), self.monotonic()
            self.post_count = self.instruction_count = self.events_added = 0
            self.slack.verified = False
            self.isolate_noa = True
            self.core_completed = False
            self.log_unconfirmed = False
            try:
                self.state = self.store.load()
                from .instruction_noa import validate_instruction_wakes
                try:
                    validate_instruction_wakes(self.state["outbox"])
                except (ValueError, TypeError, KeyError):
                    raise SafetyError("のあ起動の状態が不正です。初期化せず停止します") from None
            except SafetyError as exc:
                self.logs.operational(str(exc))
                return {"status": "state_error", "posts": 0}
            self.state["last_log_flush_at"] = self.state["last_log_flush_at"] or iso(self.started_at)
            self.state["last_summary_at"] = self.state["last_summary_at"] or iso(self.started_at)
            if self.config.mode == "live":
                self.logs.operational("本番（.envより）")
            try:
                if self.config.stop.exists():
                    raise StopRequested("STOPのため終了")
                from .instruction_noa import InstructionNoaWake as NoaWake
                wake = NoaWake(self, self.noa_client)
                if not self.config.noa_wake_enabled:
                    # This local boundary must survive auth failure or an already
                    # expired budget; otherwise a later ON could consume stale work.
                    wake.sync_enabled()
                self.check_time()
                self.config.validate_identity()
                self.slack.auth_test()
                if self.config.mode == "practice" and hasattr(self.slack, "probe_report_history"):
                    self.slack.probe_report_history()
                    if self.config.slack_report_user_token and not self.slack.report_user_verified:
                        raise AccountMismatch("代理投稿用の本人確認が完了していません")
                self.report_user_health()
                self.check_time()
                self.deadline_notices()
                requests, reports = self.read_all()
                self.current_requests = {req.id: req for req in requests
                                         if req.id not in self.duplicate_request_ids}
                self.current_reports = reports
                for request_id, info in self.state["requests"].items():
                    observed = self.current_requests.get(request_id)
                    info["source_status_observed"] = observed.status if observed else "見つからない"
                    info["source_status_observed_at"] = iso(self.clock())
                self.reconcile_local_events()
                self.recover_status_intents()
                first_run = self.state["first_run"]
                if first_run:
                    self.state["seen_reports"] = {r.block_id: r.digest for r in reports}
                    self.state["seen_report_versions"] = {r.block_id: [r.digest] for r in reports}
                    self.state["first_run"] = False
                    self.save()
                self.reconcile()
                self.drain()
                if not self.duplicate_request_ids:
                    self.requests(requests)
                self.drain()
                self.reports(requests, reports, first_run)
                from .agent_lane import tick as lane_tick
                try:
                    lane_tick(self)
                except (OSError,ValueError,KeyError,PatrolError) as exc:
                    self.logs.operational("まむ・みき受け口の照合待ち："+type(exc).__name__)
                from .project_ops import return_results
                try:
                    return_results(self)
                except (OSError,ValueError,KeyError,PatrolError) as exc:
                    self.logs.operational("ローカル結果返却の照合待ち："+type(exc).__name__)
                self.maintenance()
                self.flush(flush_log)
                self.drain()
                self.check_time()
                self.complete_patrol()
                self.core_completed = True
                # Optional control-flow exits cannot erase completed core progress.
                if not self.duplicate_request_ids:
                    self.optional_noa(wake)
                # Existing patrol remains responsive; only explicitly registered
                # two-actor jobs can cause the independent scheduler to wake.
                from .local_tasks import kick_if_pending
                try:
                    if not self.duplicate_request_ids:
                        kick_if_pending(self.config,requests)
                except Exception as exc:
                    self.logs.operational("ローカル実行受付の照合待ち：" + type(exc).__name__)
                # No recovery sends here: intents wait for the next safe run.
                # A newly held Noa result also blocks delivery on subsequent runs.
                self.logs.operational(iso(self.started_at) + (" 動いた・なにもなし" if not self.events_added else " 見回り終了"))
                owner_waiting = (self.config.mode == 'live' and hasattr(self.slack, 'post_delivery') and
                    not self.slack.report_user_verified and any(
                        x.get('delivery_policy') and x['status'] == 'queued' for x in self.state['outbox'].values()))
                held = (bool(self.duplicate_request_ids) or owner_waiting or local_execution_needs_review(self.state) or any(x.get("noa_instruction_wake", {}).get("status") in {"sending", "held", "rejected"} for x in self.state["outbox"].values()) or (bool(self.config.slack_report_user_token) and not getattr(self.slack, "report_user_verified", True)) or self.log_unconfirmed or any(x["status"] == "held" for x in self.state["outbox"].values()) or
                        any(x["status"] == "held" for x in self.state["requests"].values()) or
                        any(x["status"] == "held" for x in self.state["quarantined_reports"].values()) or bool(self.state["log_batch"]) or
                              any(x["status"] == "held" for x in self.state["noa_wake"]["items"].values()))
                result = {"status": "held" if held else "ok", "posts": self.post_count, "instructions": self.instruction_count}
                reviews = completion_review_items(self.state)
                if reviews:
                    result['completion_reviews'] = len(reviews)
                return result
            except StopRequested:
                self.logs.operational("STOPのため終了")
                return {"status": "stopped", "posts": self.post_count}
            except AccountMismatch as exc:
                self.slack.verified = False
                self.notifier()
                self.logs.operational("アカウント違いで停止：" + sanitize(str(exc), self.config.secrets))
                prev = self.state["last_account_mismatch_event_at"]
                if not prev or (self.clock() - dt(prev)).total_seconds() >= 3600:
                    self.state["last_account_mismatch_event_at"] = iso(self.clock())
                    self.event("アカウント違いで停止", result="できなかった")
                self.save()
                if getattr(exc, "log_account_mismatch", False):
                    # A Notion-side identity failure is not permission to retry
                    # that same sink under the Slack-only fallback.
                    return {"status": "account_mismatch", "posts": self.post_count}
                # Slack failure does not disable the separately authenticated
                # Notion log sink. Only the due log flush is allowed here:
                # never read requests/reports, compose, post, or change status.
                try:
                    self.reconcile_local_events()
                    self.flush(flush_log)
                except (StopRequested, TimeBudgetExceeded):
                    self.logs.operational("アカウント違いの記録は保持。STOPまたは期限でログ同期を中断")
                except Exception as log_error:
                    self.logs.operational("アカウント違いのログ同期は未確認："
                                          + sanitize(type(log_error).__name__, self.config.secrets))
                return {"status": "account_mismatch", "posts": 0}
            except Exception as exc:
                if (isinstance(exc, TimeBudgetExceeded) and getattr(exc, "not_sent", False)
                        and not getattr(exc, "uncertain", False)):
                    # A known-unsent deadline is a quiet scheduling boundary.
                    # Keep queued/held state intact, but never manufacture an
                    # error/recovery notification pair for this deferral.
                    if not self.core_completed:
                        self.record_deadline()
                    self.logs.operational("送信前の時間切れのため終了。送信待ちを保持")
                    return {"status": "deadline", "posts": self.post_count}
                if self.core_completed:
                    self.logs.operational("のあの受付は未確認。完了済みの見回りと送信状態を保持")
                    return {"status": "error", "error": type(exc).__name__, "posts": self.post_count}
                try:
                    self.handle_error(exc)
                except Exception:
                    # The CLI's final boundary also catches failures of this safe fallback.
                    self.unexpected_notifier()
                    self.logs.operational("エラー処理を完了できませんでした。自動で処理を続けません")
                return {"status": "error", "error": type(exc).__name__, "posts": self.post_count}

