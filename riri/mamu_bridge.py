"""Fixed mamu -> riri Notion mailbox; never starts patrol or posts to Slack.

The CLI has no destination, root, credential, or general-operation arguments.
Credentials stay in this process. An uncertain append is reconciled read-only.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid

from .compose import assert_safe, instruction
from .config import Config
from .errors import PatrolError, RemoteError, SafetyError
from .http import Transport
from .models import Request
from .notion import NotionClient, text_chunks
from .parse import block_text, fields_from_text, normalize_key, parse_requests, replace_status, validate_request
from .runtime import hard_deadline

REQUESTS_PAGE_ID = "413f1dff94dc81d2beebdf1b69f5c439"
PROJECT_URL = "https://app.notion.com/p/417f1dff94dc81f69bdfe9efc8c717ab"
from .paths import MAMU_RELAY_POLICY as POLICY_PATH, RIRI_ROOT
MAX_INPUT = 32 * 1024
FIELDS = frozenset({"依頼ID", "宛先", "依頼した子", "状態", "案件ページ",
    "GitHubプロジェクト", "やること", "手順", "使うもの", "やってはいけないこと",
    "期待する結果", "期限・実行タイミング", "報告先", "うまくいかないとき"})
REPORT_TO = "たかむねりりあいす（あいすからたかむねへ）。"
TEST_MARKERS = ("連携テスト", "実障害ではありません", "処理・配送不要")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(text):
    return replace_status(text, "未着手")


def _check_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"🧡[0-9]{8}-[1-9][0-9]{0,8}", value):
        raise SafetyError("invalid_request_id")
    datetime.strptime(value[1:].split("-")[0], "%Y%m%d")
    return value


def validate_policy(policy):
    if (not isinstance(policy, dict) or type(policy.get("schema_version")) is not int
            or policy["schema_version"] != 1 or policy.get("approved") is not True
            or policy.get("id_prefix") != "🧡"
            or not isinstance(policy.get("approval_source"), str)
            or not policy["approval_source"].strip()):
        raise SafetyError("policy_required")
    approved_at = datetime.fromisoformat(policy.get("approved_at", ""))
    if approved_at.utcoffset() is None:
        raise SafetyError("policy_required")


def validate_text(request_id, text, config):
    _check_id(request_id)
    if (not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_INPUT
            or "\r" in text or text.splitlines()[0] != "【りりへの依頼】"):
        raise SafetyError("invalid_template")
    assert_safe(text, config.secrets)
    # Only the known template labels and indented continuation lines are legal.
    # This prevents extra route/operation fields from being smuggled into data.
    labels = []
    for line in text.splitlines()[1:]:
        if not line.strip() or line.startswith((" ", "\t")) or re.match(r"[0-9]+[.)] ", line):
            continue
        match = re.fullmatch(r"([^：:]+)[：:](.*)", line)
        if match is None or normalize_key(match[1]) not in FIELDS:
            raise SafetyError("invalid_template")
        labels.append(normalize_key(match[1]))
    if len(labels) != len(FIELDS) or set(labels) != FIELDS:
        raise SafetyError("invalid_template")
    fields = fields_from_text(text)
    if (set(fields) != FIELDS or any(not value for value in fields.values())
            or fields["依頼ID"] != request_id or fields["依頼した子"] != "まむ"
            or fields["宛先"] not in {"あいす", "@あいす", "＠あいす"}
            or fields["状態"] not in {"未着手", "完了"}
            or fields["案件ページ"] != PROJECT_URL
            or fields["報告先"] != REPORT_TO):
        raise SafetyError("invalid_template")
    if fields["状態"] == "完了" and not all(marker in text for marker in TEST_MARKERS):
        raise SafetyError("inactive_test_required")
    req = Request(request_id, "まむ", ["あいす"], "欄", fields["状態"], "", text, fields)
    validate_request(req)
    # Use the same renderer/length/safety checks that riri will use later.
    instruction(req, ["あいす"], config.pages)
    return req


class FixedNotionStore:
    def __init__(self, client):
        self.client = client
        self.anchor = None

    def read(self):
        rows = self.client.read_blocks(REQUESTS_PAGE_ID)
        anchors = [b.get("id") for b in rows if b.get("type") in {"heading_1", "heading_2", "heading_3"}
            and not b.get("archived") and block_text(b).strip() == "依頼一覧（新しい順）"]
        self.anchor = anchors[0] if len(anchors) == 1 else None
        return rows

    def append(self, text):
        if not isinstance(self.anchor, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", self.anchor):
            raise RemoteError("request_list_heading_unconfirmed", uncertain=False)
        chunks = [{"type": "text", "text": {"content": chunk}} for chunk in text_chunks(text)]
        # One code block, fixed parent, append-only. No update/delete method.
        self.client._request("PATCH", REQUESTS_PAGE_ID, children=True, payload={
            "position": {"type": "after_block", "after_block": {"id": self.anchor}}, "children": [
            {"object": "block", "type": "code", "code": {"language": "plain text", "rich_text": chunks}}]})


class MamuBridge:
    def __init__(self, config, store, policy, *, clock=_now):
        if config.requests_page.replace("-", "").lower() != REQUESTS_PAGE_ID:
            raise SafetyError("fixed_destination_required")
        self.config, self.store, self.policy, self.clock = config, store, policy, clock
        self.root = config.root
        self.path = self.root / "state" / "mamu-bridge.json"

    @contextmanager
    def _lock(self):
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / "mamu-bridge.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise SafetyError("unsafe_bridge_lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "requests": {}}
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise SafetyError("unsafe_bridge_state")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or not isinstance(state.get("requests"), dict)):
            raise SafetyError("invalid_bridge_state")
        for request_id, item in state["requests"].items():
            _check_id(request_id)
            if (not isinstance(item, dict) or item.get("phase") not in {"intent", "verified", "not_written"}
                    or not re.fullmatch(r"[0-9a-f]{64}", item.get("text_sha256", ""))):
                raise SafetyError("invalid_bridge_state")
        return state

    def _save(self, state):
        temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _base(self, op, request_id):
        return {"ok": False, "op": op, "request_id": request_id, "result": "unconfirmed",
            "stop_present": self.config.stop.exists(), "stages": {
                key: {"status": "unconfirmed", "evidence": {}} for key in
                ("notion_saved", "riri_intake", "ice_delivered", "ice_received", "takamune_reported")}}

    def _remote(self, request_id, expected_hash):
        # A complete pagination failure is a read failure, never 'not found'.
        matches = [req for req in parse_requests(self.store.read()) if req.id == request_id]
        if not matches:
            return "missing", None
        if len(matches) != 1:
            return "collision", None
        req = matches[0]
        if (req.requester != "まむ" or req.recipients != ["あいす"]
                or _sha(_canonical(req.text)) != expected_hash):
            return "collision", None
        return "verified", req

    def _delivery_record(self, record, key, kind, channel, draft, request_id):
        """Read legacy or v2 local receipts, never infer recipient acknowledgement."""
        marker = 'rp:' + _sha(key)[:24]
        if (record.get('status') != 'posted' or record.get('kind') != kind
                or record.get('request_id') != request_id or record.get('channel') != channel
                or record.get('marker') != marker
                or not isinstance(record.get('ts'), str)
                or not re.fullmatch(r'[0-9]+\.[0-9]+', record['ts'])):
            return False
        policy = record.get('delivery_policy')
        if policy is None:
            from .mentions import add_report_mention
            expected = draft if kind == 'instruction' else add_report_mention(draft,
                channel, self.config.report_channel, self.config.ice_mention_user_id,
                self.config.expected_user_id, self.config.secrets)
            return record.get('text') == expected + '\n' + marker
        from .delivery_policy import delivery_content, validate_delivery_policy
        from .slack import REPORT_OWNER_ID
        validate_delivery_policy(policy, instruction_channel=self.config.instruction_channel,
            report_channel=self.config.report_channel, ice_user_id=self.config.ice_mention_user_id,
            requester_mention_ids=self.config.requester_mention_ids,
            expected_user_id=self.config.expected_user_id, secrets=self.config.secrets)
        if (policy['purpose'] != 'aisu_instruction' or policy['request_id'] != request_id
                or policy['recipients'] != ['あいす'] or policy['bundle_id'] != 'instruction:' + request_id):
            return False
        _, body, actual_marker = delivery_content(record.get('text'), policy,
            self.config.expected_user_id, self.config.secrets)
        if body != draft or actual_marker != marker:
            return False
        if 'delivery_receipt' in record:
            receipt = record['delivery_receipt']
            expected_receipt = {'channel': channel, 'user_id': REPORT_OWNER_ID,
                'ts': record['ts'], 'canonical_hash': _sha(draft),
                'mention_name': policy['mention_name'], 'mention_mode': policy['mention_mode'],
                'mention_user_id': policy['mention_user_id']}
            if receipt != expected_receipt:
                return False
        return True

    def _receipt(self, result, expected_hash):
        # Read only; never loads/mutates StateStore or runs its migration code.
        path = self.root / "state" / "state.json"
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("mode") != "live" or state.get("version") != 2:
                return
            info = state.get("requests", {}).get(result["request_id"], {})
            # A local safety reservation is not a pickup by the dispatcher.
            if info.get("hold_reason") == "scoped_test_only":
                return
            if info.get("scoped_execution") and not info.get("picked_event"):
                return
            snap = info.get("snapshot", {})
            if (snap.get("id") != result["request_id"] or snap.get("requester") != "まむ"
                    or snap.get("recipients") != ["あいす"]
                    or _sha(_canonical(snap.get("text", ""))) != expected_hash):
                return
            if info.get("scoped_execution") and (
                    info.get("scoped_execution", {}).get("phase") != "executed"
                    or info.get("block_id") != snap.get("block_id")
                    or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", info.get("block_id", ""))):
                return
            result["stages"]["riri_intake"] = {"status": "confirmed", "evidence": {
                "source": "riri/state/state.json", "request_id": result["request_id"],
                "text_sha256": expected_hash, "first_seen": info.get("first_seen"),
                "local_status": info.get("status")}}
            # The full canonical payload must survive both original and relay.
            from .llm import validate_draft
            # Reparse the hash-checked original text. Do not let a stale/tampered
            # snapshot.fields independently redefine the material being proved.
            request = parse_requests([{"id": snap.get("block_id", ""), "type": "code",
                "code": {"rich_text": [{"text": {"content": snap["text"]}}]}}])[0]
            validate_request(request)
            draft = info.get("draft")
            if not validate_draft(draft, request, ["あいす"], self.config.secrets, self.config.pages):
                return
            source_key = "instruction:" + result["request_id"]
            source = state.get("outbox", {}).get(source_key, {})
            if not self._delivery_record(source, source_key, 'instruction',
                    self.config.instruction_channel, draft, result['request_id']):
                return
            key = "instruction-gdm:" + result["request_id"]
            item = state.get("outbox", {}).get(key, {})
            expected_marker = "rp:" + _sha(key)[:24]
            if (item.get('delivery_policy') == source.get('delivery_policy')
                    and self._delivery_record(item, key, 'instruction_relay',
                        self.config.report_channel, draft, result['request_id'])):
                result["stages"]["ice_delivered"] = {"status": "confirmed", "evidence": {
                    "source": "riri/state/state.json", "channel": self.config.report_channel,
                    "request_id": result["request_id"], "marker": expected_marker,
                    "ts": item["ts"], "posted_at": item.get("posted_at"),
                    "source_ts": source["ts"], "canonical_payload_sha256": _sha(draft),
                    "actor_verification": "unconfirmed_by_bridge",
                    "owner_receipts_recorded": bool(source.get('delivery_receipt') and item.get('delivery_receipt')),
                    "delivery_policy_version": 'dual-v2' if source.get('delivery_policy') else 'legacy',
                    "meaning": "existing riri report-channel post receipt; recipient acknowledgement unconfirmed"}}
        except (ValueError, OSError, TypeError, KeyError, IndexError, PatrolError):
            result["receipt_error"] = "local_receipt_unreadable"

    def _verified(self, result, req, expected_hash):
        result.update(ok=True, result="saved_verified")
        result["stages"]["notion_saved"] = {"status": "confirmed", "evidence": {
            "page_id": REQUESTS_PAGE_ID, "block_id": req.block_id,
            "canonical_sha256": expected_hash, "text_sha256": _sha(req.text), "read_back_at": self.clock(),
            "source_status": req.status}}
        self._receipt(result, expected_hash)
        return result

    def submit(self, request_id, text):
        result = self._base("submit", request_id)
        try:
            validate_policy(self.policy)
        except (ValueError, TypeError, SafetyError):
            result["result"] = "policy_required"
            return result
        req = validate_text(request_id, text, self.config)
        expected_hash = _sha(_canonical(text))
        # STOP is not bypassed to leave a future executable test in the mailbox.
        # A visibly inactive, completed storage-only test never invokes patrol.
        if self.config.stop.exists() and req.status != "完了":
            result["result"] = "blocked_stopped"
            return result
        with self._lock():
            state = self._load()
            previous = state["requests"].get(request_id)
            if previous and previous["text_sha256"] != expected_hash:
                result["result"] = "collision"
                return result
            observed, remote = self._remote(request_id, expected_hash)
            if observed == "collision":
                result["result"] = "collision"
                return result
            if observed == "verified":
                state["requests"][request_id] = {"phase": "verified", "text_sha256": expected_hash,
                    "block_id": remote.block_id, "verified_at": self.clock()}
                self._save(state)
                return self._verified(result, remote, expected_hash)
            if previous and previous["phase"] in {"intent", "verified"}:
                # Even a zero-result read may lag a prior append. Never reappend.
                result["result"] = "saved_unknown"
                return result
            state["requests"][request_id] = {"phase": "intent", "text_sha256": expected_hash,
                "attempt_at": self.clock()}
            self._save(state)  # Intent durable BEFORE the one possible mutation.
            if self.config.stop.exists() and req.status != "完了":
                state["requests"][request_id]["phase"] = "not_written"
                self._save(state)
                result["result"] = "blocked_stopped"
                return result
            try:
                self.store.append(text)
            except RemoteError as exc:
                if not exc.uncertain:
                    state["requests"][request_id]["phase"] = "not_written"
                    self._save(state)
                    result["result"] = "save_failed"
                    result["stages"]["notion_saved"]["status"] = "failed"
                    return result
                # Reconcile only; do not retry even if the write response failed.
            except Exception:
                result["result"] = "saved_unknown"
                return result
            try:
                observed, remote = self._remote(request_id, expected_hash)
            except Exception:
                result["result"] = "saved_unknown"
                return result
            if observed == "verified":
                state["requests"][request_id].update(phase="verified", block_id=remote.block_id,
                    verified_at=self.clock())
                self._save(state)
                return self._verified(result, remote, expected_hash)
            result["result"] = "collision" if observed == "collision" else "saved_unknown"
            return result

    def status(self, request_id):
        _check_id(request_id)
        result = self._base("status", request_id)
        state = self._load()
        item = state["requests"].get(request_id)
        if item is None:
            result.update(ok=True, result="not_submitted")
            return result
        observed, req = self._remote(request_id, item["text_sha256"])
        if observed == "verified":
            return self._verified(result, req, item["text_sha256"])
        result.update(ok=observed != "collision", result="collision" if observed == "collision" else "saved_unknown")
        # Local intake/delivery can remain evidence even if the source was removed.
        self._receipt(result, item["text_sha256"])
        return result


def dispatch(payload, *, root=RIRI_ROOT, policy_path=POLICY_PATH, transport=None):
    if (not isinstance(payload, dict) or not isinstance(payload.get("op"), str)
            or payload["op"] not in {"submit", "status"}):
        raise SafetyError("invalid_operation")
    allowed = {"op", "request_id", "text"} if payload["op"] == "submit" else {"op", "request_id"}
    if set(payload) != allowed:
        raise SafetyError("invalid_input")
    config = Config.load(root)
    if not config.notion_token:
        raise SafetyError("existing_notion_connection_required")
    policy = None
    if payload["op"] == "submit" and policy_path.is_file():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    bridge = MamuBridge(config, FixedNotionStore(NotionClient(config.notion_token,
        transport or Transport(deadline=time.monotonic() + 90), config.notion_version)), policy)
    if payload["op"] == "submit":
        return bridge.submit(payload["request_id"], payload["text"])
    return bridge.status(payload["request_id"])


def main():
    payload = None
    try:
        if len(sys.argv) != 1:
            raise SafetyError("arguments_not_supported")
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw.encode("utf-8")) > MAX_INPUT:
            raise SafetyError("input_too_large")
        with hard_deadline(90):
            payload = json.loads(raw)
            result = dispatch(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except (Exception, KeyboardInterrupt):
        # No exception repr, remote body, input, or token is ever printed.
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if not isinstance(request_id, str) or not re.fullmatch(r"🧡[0-9]{8}-[1-9][0-9]{0,8}", request_id):
            request_id = None
        op = payload.get("op") if isinstance(payload, dict) else None
        print(json.dumps({"ok": False, "op": op if isinstance(op, str) and op in {"submit", "status"} else None,
            "request_id": request_id, "result": "bridge_error", "error_code": "operation_unconfirmed",
            "stop_present": (RIRI_ROOT / "STOP").exists(), "stages": {
                key: {"status": "unconfirmed", "evidence": {}} for key in
                ("notion_saved", "riri_intake", "ice_delivered", "ice_received", "takamune_reported")}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
