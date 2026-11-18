#!/usr/bin/env python3
"""List which old requests on 「りりへの依頼」 could move to a monthly archive page.

Read-only. It reads the request page (GET only, with riri's own Notion client
and .env) or a saved block list, and riri's state file. It never edits Notion,
the state, or anything else; moving blocks stays a manual step (docs/archive.md).

A request can move only when all of these hold:
- its ID date is before the first day of --before (e.g. --before 2026-11);
- Notion says 完了 and riri's state says done;
- no riri post for it is still queued, sending or held;
- it does not appear in riri's local-tasks or mamu-bridge records, or in miki's / mamu's
  relay records.
Everything else is listed with the reason it has to stay.

    python3 scripts/archive_plan.py --before 2026-11            # live read
    python3 scripts/archive_plan.py --before 2026-11 --blocks saved-blocks.json --json
"""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from riri.parse import parse_requests  # noqa: E402
from riri.paths import MAMU_ROOT, MIKI_ROOT, RIRI_ROOT  # noqa: E402

OPEN_POSTS = {'queued', 'sending', 'held', 'posting', 'prepared'}
ID_DATE = re.compile(r'(?<![0-9])(20[0-9]{6})-[0-9]+$')


def cutoff(month):
    match = re.fullmatch(r'(20[0-9]{2})-(0[1-9]|1[0-2])', month or '')
    if not match:
        raise SystemExit('--before は 2026-11 の形で指定してください')
    return date(int(match.group(1)), int(match.group(2)), 1)


def id_date(request_id):
    match = ID_DATE.search(request_id)
    if not match:
        return None
    text = match.group(1)
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def mentioned_in(folders, request_id, limit=5000):
    """Folders (by label) whose files mention request_id; bounded and read-only."""
    found = []
    for label, folder in folders:
        folder = Path(folder)
        if folder.is_file():
            files = [folder]
        elif folder.is_dir():
            files = (p for p in folder.rglob('*') if p.is_file())
        else:
            continue
        for count, path in enumerate(files):
            if count >= limit:
                found.append(label + '（多すぎて確認しきれない）')
                break
            try:
                if request_id in path.read_text(encoding='utf-8', errors='ignore'):
                    found.append(label)
                    break
            except OSError:
                continue
    return found


def plan(blocks, state, before, folders=()):
    by_request = {}
    for key, item in (state.get('outbox') or {}).items():
        if item.get('request_id') and item.get('status') in OPEN_POSTS:
            by_request.setdefault(item['request_id'], []).append('%s（%s）' % (key, item['status']))
    known = state.get('requests') or {}
    movable, stay, newer = [], [], 0
    for req in parse_requests(blocks):
        when = id_date(req.id)
        if when is None:
            stay.append({'id': req.id, 'block_id': req.block_id, 'reasons': ['IDから日付が読めない']})
            continue
        if when >= before:
            newer += 1
            continue
        reasons = []
        if req.status != '完了':
            reasons.append('Notionの状態が「%s」' % (req.status or '空欄'))
        info = known.get(req.id)
        if info is None:
            reasons.append('りりの状態ファイルに記録がない')
        elif info.get('status') != 'done':
            reasons.append('りりの記録が「%s」' % info.get('status'))
        reasons += ['未送信・保留の投稿：' + p for p in by_request.get(req.id, [])]
        reasons += ['ほかの記録に出てくる：' + f for f in mentioned_in(folders, req.id)]
        row = {'id': req.id, 'block_id': req.block_id, 'requester': req.requester}
        if reasons:
            row['reasons'] = reasons
            stay.append(row)
        else:
            movable.append(row)
    return {'before': before.isoformat(), 'movable': movable, 'stay': stay, 'newer': newer,
            'blocks_total': len(blocks)}


def live_blocks(root):
    from riri.config import Config
    from riri.http import Transport
    from riri.notion import NotionClient
    config = Config.load(root)
    client = NotionClient(config.notion_token, Transport(deadline=time.monotonic() + 120), config.notion_version)
    return client.read_blocks(config.requests_page)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--before', required=True, help='この月より前のIDだけを対象にする（例 2026-11）')
    parser.add_argument('--blocks', help='保存済みのブロック一覧（JSON）。省略するとNotionを読む')
    parser.add_argument('--state', help='りりの状態ファイル（既定は state/state.json）')
    parser.add_argument('--json', action='store_true', help='結果をJSONで出す')
    args = parser.parse_args()
    before = cutoff(args.before)
    blocks = json.loads(Path(args.blocks).read_text(encoding='utf-8')) if args.blocks else live_blocks(RIRI_ROOT)
    state_path = Path(args.state) if args.state else RIRI_ROOT / 'state' / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    folders = [('りりのlocal-tasks', RIRI_ROOT / 'state' / 'local-tasks'),
               ('りりのまむ連携記録', RIRI_ROOT / 'state' / 'mamu-bridge.json'),
               ('みきの連携記録', MIKI_ROOT / 'data' / 'relay'),
               ('まむの連携記録', MAMU_ROOT / 'state' / 'relay')]
    result = plan(blocks, state, before, folders)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print('対象：%s より前の依頼ID（ページ全体 %d ブロック、対象外の新しい依頼 %d 件）'
          % (result['before'], result['blocks_total'], result['newer']))
    print('移してよい：%d 件' % len(result['movable']))
    for row in result['movable']:
        print('  %s  block %s' % (row['id'], row['block_id']))
    print('残す：%d 件' % len(result['stay']))
    for row in result['stay']:
        print('  %s  %s' % (row['id'], '／'.join(row['reasons'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
