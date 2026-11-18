#!/usr/bin/env python3
"""Compare the member lists in miki, mamu and myu with config/members.json.

Read-only: the other programs' source is parsed, never imported or changed.
Errors (exit 1) are contradictions with the roster, such as a different heart.
Warnings are gaps, such as a member the program does not know yet.

    python3 scripts/check_members.py [--miki DIR] [--mamu DIR] [--myu DIR]

miki and mamu default to the paths in riri/paths.py; myu runs on another Mac,
so it is checked only when --myu is given.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from riri import members as roster  # noqa: E402
from riri.paths import MAMU_ROOT, MIKI_ROOT  # noqa: E402

HEART_GROUP = re.compile(r'\(\?:((?:[^()|]+\|)*[^()|]+)\)')


def _assignment(source, name):
    """Literal value assigned to a top-level or class-level name, or None."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def _pattern_hearts(text):
    found = set()
    for group in HEART_GROUP.findall(text):
        parts = group.split('|')
        # Heart lists only: every alternative is a one- or two-codepoint emoji, no letters.
        if parts and all(len(p) <= 2 and not p.isascii() and not any(c.isalnum() for c in p) for p in parts):
            found.update(parts)
    return found


class Report:
    def __init__(self):
        self.errors, self.warnings, self.checked, self.skipped = [], [], [], []

    def error(self, where, text):
        self.errors.append(where + ': ' + text)

    def warn(self, where, text):
        self.warnings.append(where + ': ' + text)


def check_miki(root, members, report):
    path = Path(root) / 'registry.json'
    if not path.is_file():
        report.skipped.append('みき（registry.json がない：%s）' % path)
        return
    report.checked.append('みき')
    known = {}
    for agent in json.loads(path.read_text(encoding='utf-8')).get('agents', []):
        name = roster.resolve(members, agent.get('name'))
        where = 'みき registry.json「%s」' % agent.get('name')
        if name is None:
            report.error(where, '名簿にいない名前')
            continue
        known[name] = agent
        entry = next(m for m in members if m['name'] == name)
        if roster.resolve(members, agent.get('agent_id')) != name:
            report.error(where, 'agent_id %r が名簿の %s（%s）と合わない' % (agent.get('agent_id'), entry['id'], '・'.join(entry['aliases']) or '別名なし'))
        if agent.get('heart') and agent['heart'] != entry['heart']:
            report.error(where, '色 %s が名簿の %s と違う' % (agent['heart'], entry['heart']))
        page = agent.get('report_page_url')
        if page and entry.get('report_page') and page != entry['report_page']:
            report.error(where, '完了報告ページが名簿と違う')
    missing = sorted(roster.names(members, 'agent') - set(known) - {'みき'})
    if missing:
        report.warn('みき registry.json', '登録がない：' + '・'.join(missing))


def check_mamu(root, members, report):
    guard, relay = Path(root) / 'mamu_guard.py', Path(root) / 'mamu_relay.py'
    if not guard.is_file():
        report.skipped.append('まむ（mamu_guard.py がない：%s）' % guard)
        return
    report.checked.append('まむ')
    table = _assignment(guard.read_text(encoding='utf-8'), 'MEMBERS') or {}
    for key, name in table.items():
        where = 'まむ mamu_guard.py MEMBERS[%r]' % key
        if roster.resolve(members, name) != name:
            report.error(where, '名簿にいない名前 %r' % name)
        elif roster.resolve(members, key) != name:
            report.error(where, 'ID %r が名簿の %s と合わない' % (key, name))
    missing = sorted(roster.names(members, 'agent') - set(table.values()))
    if missing:
        report.warn('まむ mamu_guard.py MEMBERS', '登録がない：' + '・'.join(missing))
    if relay.is_file():
        source = relay.read_text(encoding='utf-8')
        own = re.search(r"^ID\s*=\s*re\.compile\(r'([^\[\\]+)\[", source, re.M)
        if own and roster.hearts(members).get(own.group(1)) != 'まむ':
            report.error('まむ mamu_relay.py ID', '依頼IDの色 %s は名簿ではまむの色ではない' % own.group(1))
        related = _pattern_hearts(source)
        unknown = sorted(related - set(roster.hearts(members)))
        if unknown:
            report.error('まむ mamu_relay.py', '名簿にない色：' + ''.join(unknown))
        absent = sorted(set(roster.hearts(members)) - related)
        if related and absent:
            report.warn('まむ mamu_relay.py related_request_id',
                        '受け付けない色：' + '・'.join('%s（%s）' % (h, roster.hearts(members)[h]) for h in absent))


def check_myu(root, members, report):
    link, reader = Path(root) / 'myu_app' / 'riri_link.py', Path(root) / 'myu_app' / 'slack_reader.py'
    if not link.is_file():
        report.skipped.append('みゅう（riri_link.py がない：%s）' % link)
        return
    report.checked.append('みゅう')
    expected = set(roster.hearts(members))
    found = _pattern_hearts(link.read_text(encoding='utf-8'))
    if found != expected:
        report.error('みゅう riri_link.py ID', '色の一覧が名簿と違う（多い：%s／足りない：%s）' % (
            ''.join(sorted(found - expected)) or 'なし', ''.join(sorted(expected - found)) or 'なし'))
    if reader.is_file():
        hearts = _assignment(reader.read_text(encoding='utf-8'), 'HEARTS')
        if hearts is not None and set(hearts.values()) != expected:
            report.error('みゅう slack_reader.py HEARTS', '色の一覧が名簿と違う')


def run(miki=None, mamu=None, myu=None, roster_path=roster.ROSTER):
    members = roster.load(roster_path)
    report = Report()
    check_miki(miki or MIKI_ROOT, members, report)
    check_mamu(mamu or MAMU_ROOT, members, report)
    if myu:
        check_myu(myu, members, report)
    else:
        report.skipped.append('みゅう（--myu を指定したときだけ）')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--miki')
    parser.add_argument('--mamu')
    parser.add_argument('--myu')
    args = parser.parse_args()
    report = run(args.miki, args.mamu, args.myu)
    print('確認した：' + ('・'.join(report.checked) or 'なし'))
    for line in report.skipped:
        print('確認できなかった：' + line)
    for line in report.errors:
        print('不一致：' + line)
    for line in report.warnings:
        print('未登録：' + line)
    if not report.errors and not report.warnings:
        print('名簿と一致しています。')
    return 1 if report.errors else 0


if __name__ == '__main__':
    sys.exit(main())
