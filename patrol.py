#!/usr/bin/env python3
"""Run once only. This program never installs or registers itself."""
import sys

# Keep this bootstrap parseable before 3.11 and run it BEFORE version-dependent imports.
if sys.version_info < (3, 11):
    from riri.notify import python_too_old
    python_too_old()
    print("Python 3.11以降が必要です。通信せず終了します。", file=sys.stderr)
    raise SystemExit(2)

import argparse
import json
from pathlib import Path
import time

from riri.config import Config
from riri.errors import AccountMismatch, PatrolError, SafetyError, sanitize
from riri.eventlog import LocalLogs
from riri.http import Transport
from riri.llm import DraftService
from riri.notion import NotionClient
from riri.patrol import Patrol
from riri.runtime import hard_deadline
from riri.slack import SlackClient
from riri.state import StateStore
from riri.resolve import resolve_request
from riri import notify


def main(argv=None):
    parser = argparse.ArgumentParser(description="りりの見回りを1回だけ実行。既定は練習モード")
    parser.add_argument("--dry-run", action="store_true", help="本番設定でもこの回は練習モード")
    parser.add_argument("--flush-log", action="store_true", help="時間を待たずログを書き出す（練習なら記録だけ）")
    parser.add_argument("--verbose", action="store_true", help="秘密を伏せた練習記録を表示")
    local = parser.add_mutually_exclusive_group()
    local.add_argument("--status", action="store_true", help="状態の件数を表示。ネット接続・書き込みなし")
    local.add_argument("--resolve", nargs=2, metavar=("依頼ID", "posted|not_posted"),
                       help="保留のSlack投稿結果を手動確認。ロックを取得しローカルのみ変更")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help=".env・STOP・状態・ログのあるフォルダ")
    args = parser.parse_args(argv)
    root = args.root
    try:
        root = root.resolve()
        # Default STOP wins even over malformed .env. Custom STOP is checked again below.
        if not args.status and (root / "STOP").exists():
            LocalLogs(root).operational("STOPのため終了")
            print("STOPのため終了")
            return 0
        cfg = Config.load(root, args.dry_run)
        if cfg.stop.exists() and not args.status:
            LocalLogs(root, cfg.secrets).operational("STOPのため終了")
            print("STOPのため終了")
            return 0
        if args.status:
            state = StateStore(root, cfg.mode).load()
            summary = {"mode": cfg.mode, "last_run_at": state["last_run_at"],
                       "requests": len(state["requests"]), "buffered_events": len(state["log_buffer"]),
                       "held_message_ids": [x["marker"] for x in state["outbox"].values() if x["status"] == "held"],
                       "log_write_held": bool(state["log_batch"])}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.resolve:
            result = resolve_request(cfg, *args.resolve)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        def before_request():
            if cfg.stop.exists():
                raise SafetyError("STOPが置かれたため通信せず停止しました")
        transport = Transport(deadline=time.monotonic() + cfg.run_timeout, before_request=before_request)
        slack = SlackClient(cfg.slack_token, cfg.expected_bot_id, cfg.expected_user_id, transport)
        notion = NotionClient(cfg.notion_token, transport, cfg.notion_version)
        draft = DraftService(transport, cfg.ollama_url, cfg.ollama_model, cfg.ollama_num_ctx,
                             cfg.llm_enabled, cfg.secrets, cfg.pages)
        with hard_deadline(cfg.run_timeout):
            result = Patrol(cfg, slack, notion, draft, verbose=args.verbose).run(args.flush_log)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in ("ok", "locked", "stopped") else 2
    except Exception as exc:
        secrets = cfg.secrets if "cfg" in locals() else ()
        # Never print exception repr, traceback, HTTP body, or environment contents.
        message = sanitize("見回りを開始できませんでした：" + type(exc).__name__, secrets)
        try:
            LocalLogs(root, secrets).operational(message + ": " + sanitize(str(exc), secrets))
        except Exception:
            pass  # A full/read-only disk must not expose the original exception.
        if isinstance(exc, AccountMismatch):
            notify.account_mismatch()
        else:
            notify.unexpected_error()
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
