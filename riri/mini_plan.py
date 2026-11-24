"""Deciding where みに works and whether a continuation may resume.  Not wired in yet.

Design: みに wake design v1.0 (Notion), 4-1-3 / 4-2 / 4-3.

* ``find_case``: みに's (B) rule.  The 32-hex page ID at the end of the
  案件ページ URL is matched against the 「NotionページID:」/「受付別名ID:」 lines
  of ``案件/*/AGENTS.md`` and ``独立監視/*/AGENTS.md``, counted per folder.
* ``workplace``: the six-row table of 4-2, first match wins.
* ``materials_changed``: the freshness check right before launch (X4).
* ``continuation_check``: the six checks of 4-3 for resuming a conversation.
"""
import os
import re
from pathlib import Path

_PAGE_ID_RE = re.compile(r'([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')


def page_id_of(url):
    """Lower-case, hyphen-less 32-hex ID at the end of the URL path, or None."""
    if not isinstance(url, str):
        return None
    path = re.split(r'[?#]', url.strip(), maxsplit=1)[0].rstrip('/')
    tail = path.rsplit('/', 1)[-1]
    found = _PAGE_ID_RE.findall(tail)
    if not found:
        return None
    return found[-1].replace('-', '').lower()


def find_case(mini_dir, page_url):
    """Returns ``{'kind': 'one'|'none'|'many'|'unreadable', 'folder': Path|None, 'reason': str}``."""
    target = page_id_of(page_url)
    if not target:
        return {'kind': 'none', 'folder': None, 'reason': '案件ページのIDが読めない'}
    hits, unreadable = [], []
    for group in ('案件', '独立監視'):
        base = Path(mini_dir) / group
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            agents = folder / 'AGENTS.md'
            if not folder.is_dir() or not agents.exists():
                continue
            if folder.is_symlink() or agents.is_symlink():
                unreadable.append(folder)
                continue
            try:
                text = agents.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                unreadable.append(folder)
                continue
            ids = set()
            for line in text.splitlines():
                # Real AGENTS.md files write these as list items, often with the ID in backticks:
                # 「- NotionページID: `416f…`（新しい依頼の「案件ページ」に使う）」
                m = re.match(r'^\s*(?:[-*・]\s*)?(?:NotionページID|受付別名ID)\s*[:：]\s*(.+?)\s*$', line)
                if m:
                    found = _PAGE_ID_RE.findall(m[1])
                    pid = page_id_of(m[1]) or (found[0].replace('-', '').lower() if len(found) == 1 else None)
                    if pid:
                        ids.add(pid)
            if target in ids:
                hits.append(folder)
    if unreadable:
        return {'kind': 'unreadable', 'folder': None, 'reason': 'シンボリックリンクか読めない AGENTS.md がある'}
    if len(hits) == 1:
        return {'kind': 'one', 'folder': hits[0], 'reason': ''}
    if not hits:
        return {'kind': 'none', 'folder': None, 'reason': '一致する案件がない'}
    return {'kind': 'many', 'folder': None, 'reason': '一致する案件が2つ以上ある'}


def workplace(*, always_ok, case, stage, write_ok, case_folder_real, write_roots, targets):
    """The 4-2 table.  Returns the row number, read targets, write places and kind.

    ``always_ok``: every always-required material was read.
    ``case``: result of ``find_case``.  ``write_ok``: the write-work materials are complete.
    ``targets``: real paths of the work targets named by the case's AGENTS.md.
    """
    roots = {os.path.realpath(r) for r in write_roots}

    def under(path):
        real = os.path.realpath(path)
        return any(real == r or real.startswith(r.rstrip('/') + '/') for r in roots)

    if not always_ok:
        folder = case['folder'] if case.get('kind') == 'one' else None
        return {'row': 1, 'read': [folder] if folder else [], 'write': [], 'kind': '報告だけ', 'reason': '必須の資料が欠けた'}
    if case.get('kind') != 'one':
        return {'row': 2, 'read': [], 'write': [], 'kind': '報告だけ', 'reason': case.get('reason', '')}
    folder = case['folder']
    if stage == 1:
        return {'row': 3, 'read': [folder] + list(targets), 'write': [], 'kind': '通常（読むだけ）', 'reason': ''}
    if stage == 2 and write_ok and under(case_folder_real):
        allowed = [t for t in targets if under(t)]
        blocked = [t for t in targets if not under(t)]
        return {'row': 4, 'read': [folder] + list(targets), 'write': [case_folder_real] + allowed,
                'kind': '通常', 'reason': '', 'blocked_targets': blocked}
    if stage == 2:
        why = '資料不足' if not write_ok else '許可外'
        return {'row': 5, 'read': [folder] + list(targets), 'write': [], 'kind': '報告だけ', 'reason': why}
    return {'row': 6, 'read': [], 'write': [], 'kind': '報告だけ', 'reason': '判定不能'}


def materials_changed(fixed, fresh):
    """Names whose hash differs (or that disappeared) right before launch (X4)."""
    changed = [name for name, digest in fixed.items() if fresh.get(name) != digest]
    return sorted(changed + [name for name in fresh if name not in fixed])


def continuation_check(*, original, block_count, block_hash, writer_ok, now_plan, consumed, block_id):
    """The six checks of 4-3.  Returns (ok, reason)."""
    if not original or not original.get('thread_id') or not original.get('end_evidence'):
        return False, '元の run に会話IDと終了の証拠がない（新しい依頼IDで出し直す）'
    if original.get('original_hash_now') != original.get('normalized_hash'):
        return False, '元の依頼が書き換わった'
    if block_count != 1:
        return False, '続きのブロックが1つではない'
    if not block_hash:
        return False, '続きの本文を固定できない'
    if not writer_ok:
        return False, '続きを書いた人が元の依頼した子・たかむねに決まらない（P2）'
    for key in ('read', 'write', 'row', 'stage', 'fingerprint'):
        if now_plan.get(key) != original.get('plan', {}).get(key):
            return False, f'元と違う：{key}'
    if (original.get('request_id'), block_id) in consumed:
        return False, 'この続きはもう処理した'
    return True, ''
