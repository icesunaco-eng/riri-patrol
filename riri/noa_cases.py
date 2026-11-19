"""Choose Noa's case folder for one local run (たかむね, 2026-11-19).

Under NOA_LOCAL_DIR, each case lives in 案件/<name>/ with a CLAUDE.md whose
"## 正本" section lists that case's Notion page. When the request's 「案件ページ」
line names exactly one such page, Claude Code is started in that folder, so the
case rules load with Noa's other CLAUDE.md files.

Anything unclear keeps NOA_LOCAL_DIR, the behaviour before this module: no
案件 folder, no match, two or more matches, a symlinked folder or rules file,
or a rules file that cannot be read completely. This module never guesses,
creates or edits a folder.
"""
import os
import re
import stat

from .local_noa import PROJECT_LINE, _names, notion_ids

CASES = "案件"
RULES = "CLAUDE.md"
RULES_LIMIT = 64 * 1024
CANONICAL = re.compile(r"^##\s*正本\s*$")
HEADING = re.compile(r"^##\s")


class Unclear(Exception):
    """A rules file exists but could not be read safely; do not pick a case."""


def requested_ids(text):
    """Notion page IDs on the request's 「案件ページ」 lines only."""
    ids = set()
    for line in PROJECT_LINE.findall(text or ""):
        ids.update(notion_ids(line))
    return ids


def canonical_ids(path):
    """Notion IDs in the "## 正本" section of one case's CLAUDE.md.

    Returns None when the folder has no rules file. Raises Unclear when the
    file is a symlink, not a regular file, too large or unreadable."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > RULES_LIMIT:
        raise Unclear()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(RULES_LIMIT + 1)
    except OSError:
        raise Unclear() from None
    if len(raw) > RULES_LIMIT:
        raise Unclear()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Unclear() from None
    ids, inside = set(), False
    for line in text.splitlines():
        if CANONICAL.match(line):
            inside = True
        elif HEADING.match(line):
            inside = False
        elif inside:
            ids.update(notion_ids(line))
    return ids


def case_dir(base, text):
    """The single case folder whose 正本 matches the request, else None.

    base must already be a real path (os.path.realpath of NOA_LOCAL_DIR)."""
    wanted = requested_ids(text)
    if not wanted:
        return None
    root = os.path.join(base, CASES)
    try:
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
        names = sorted(_names(root))
    except OSError:
        return None
    matches = []
    for name in names:
        sub = os.path.join(root, name)
        try:
            info = os.lstat(sub)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue  # links to live bodies (~/競馬 …) are never a case folder
        try:
            ids = canonical_ids(os.path.join(sub, RULES))
        except Unclear:
            return None
        if ids and ids & wanted:
            matches.append(sub)
    if len(matches) != 1:
        return None
    found = os.path.realpath(matches[0])
    if os.path.dirname(found) != os.path.realpath(root):
        return None
    return found
