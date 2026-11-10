#!/usr/bin/env python3
"""Offline practice demonstration. Never reads .env or constructs HTTP clients."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from riri.config import Config
from riri.llm import DraftService
from riri.patrol import Patrol
from riri.state import StateStore
from riri.parse import replace_status
from tests.test_patrol_flow import Clock, FakeSlack, FakeNotion, OfflineTransport, request, report


def main():
    root = Path(tempfile.mkdtemp(prefix="riri-patrol-demo-"))
    cfg = Config(root=root, llm_enabled=False, expected_bot_id="BTEST", expected_user_id="UTEST")
    clock, slack = Clock(), FakeSlack()
    notion = FakeNotion(cfg)
    notion.blocks[cfg.requests_page] = [request(2, "@みに"), request(1, "@うさ")]
    results = []
    draft = DraftService(OfflineTransport(), enabled=False)
    for _ in range(3):
        results.append(Patrol(cfg, slack, notion, draft, clock=clock).run())
        clock.advance()
    # Practice never changes external state. Simulate the fixture's owner moving
    # this request to 対応中 before testing the valid 対応中 -> 完了 preview.
    for block in notion.blocks[cfg.requests_page]:
        if block['id'] == 'request-1':
            text = block['code']['rich_text'][0]['text']
            text['content'] = replace_status(text['content'], '対応中')
    notion.blocks[cfg.pages["うさ"].split("/")[-1]] = [report()]
    results.append(Patrol(cfg, slack, notion, draft, clock=clock).run())
    clock.advance(10800)
    results.append(Patrol(cfg, slack, notion, draft, clock=clock).run())
    state = StateStore(root, "practice").load()
    summary = {"mode": "practice", "network_connections": 0,
               "external_slack_writes": len(slack.posts), "external_notion_writes": len(notion.writes),
               "runs": results, "request_statuses": {k: v["status"] for k, v in state["requests"].items()},
               "buffered_events": len(state["log_buffer"]), "artifact_root": str(root)}
    (root / "demo-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    with patch("socket.socket", side_effect=AssertionError("network forbidden during offline demo")):
        main()
