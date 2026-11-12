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
from riri.errors import AccountMismatch, PatrolError, SafetyError, StopRequested, sanitize
from riri.eventlog import LocalLogs
from riri.http import Transport
from riri.llm import DraftService
from riri.notion import NotionClient
from riri.patrol import Patrol
from riri.runtime import hard_deadline
from riri.slack import SlackClient
from riri.state import StateStore
from riri.status import practice_cleanup_items
from riri.resolve import resolve_request, resolve_held, held_items
from riri import notify


def main(argv=None):
    parser = argparse.ArgumentParser(description="りりの見回りを1回だけ実行。既定は練習モード")
    parser.add_argument("--dry-run", action="store_true", help="本番設定でもこの回は練習モード")
    parser.add_argument("--flush-log", action="store_true", help="時間を待たずログを書き出す（練習なら記録だけ）")
    parser.add_argument("--verbose", action="store_true", help="秘密を伏せた練習記録を表示")
    local = parser.add_mutually_exclusive_group()
    local.add_argument("--status", action="store_true", help="状態の件数を表示。ネット接続・書き込みなし")
    local.add_argument("--resolve", nargs="+", metavar="種類/ID/結果",
                       help="保留を手動確認。種類とIDと結果、または従来の依頼IDと結果。通信なし")
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
            practice_state = state if cfg.mode == "practice" else StateStore(root, "practice").load()
            summary = {"mode": cfg.mode, "last_run_at": state["last_run_at"],
                       "requests": len(state["requests"]), "buffered_events": len(state["log_buffer"]),
                       "held_message_ids": [x["marker"] for x in state["outbox"].values() if x["status"] == "held"],
                       "held": held_items(state),
                       "log_write_held": bool(state["log_batch"]),
                       "practice_cleanup": practice_cleanup_items(practice_state),
                       "practice_cleanup_note": "通信せず、保存済みの観測を表示しています。本番前にNotionで現在の状態を確認してください"}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.resolve:
            if len(args.resolve) == 2:
                result = resolve_request(cfg, *args.resolve)
            elif len(args.resolve) == 3:
                result = resolve_held(cfg, *args.resolve)
            else:
                raise SafetyError("--resolve は 種類 ID 結果（または依頼ID 結果）を指定してください")
            print(json.dumps(result, ensure_ascii=False))
            return 0
        def before_request():
            if cfg.stop.exists():
                raise StopRequested("STOPが置かれたため通信せず停止しました")
        transport = Transport(deadline=time.monotonic() + cfg.run_timeout, before_request=before_request)
        slack = SlackClient(cfg.slack_token, cfg.expected_bot_id, cfg.expected_user_id, transport,
                            report_channel=cfg.report_channel, ice_mention_user_id=cfg.ice_mention_user_id)
        logs = LocalLogs(root, cfg.secrets, args.verbose)
        notion = NotionClient(cfg.notion_token, transport, cfg.notion_version, operational=logs.operational)
        draft = DraftService(transport, cfg.ollama_url, cfg.ollama_model, cfg.ollama_num_ctx,
                             cfg.llm_enabled, cfg.secrets, cfg.pages)
        with hard_deadline(cfg.run_timeout):
            result = Patrol(cfg, slack, notion, draft, logs=logs, verbose=args.verbose).run(args.flush_log)
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
