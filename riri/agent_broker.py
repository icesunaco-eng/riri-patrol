"""Notion window for a running まむ/みき job. The runner calls this; the model never sees a token.

    echo '{"op": "notion_read", "run_id": "...", "agent": "mamu", "page_id": "..."}' | python -B -m riri.agent_broker

Only a job that Riri started (state=running) may use it, and only with the
toolboxes Riri gave that job. Rule pages, report pages and the request list are
never written here: reports are Riri's, and new requests go through
``request_submit``, which is checked like any other formal request.
"""
import json
from pathlib import Path
import re
import sys
import time

from . import compose
from .agent_lane import AGENTS, HEARTS, NAMES, Store, exclusive, require
from .config import Config
from .errors import PatrolError, SafetyError, StopRequested
from .models import RECIPIENTS, REPORT_PAGES
from .parse import block_text, parse_requests, validate_request

READ_LIMIT = 60000
TEXT_LIMIT = 8000
MAX_WRITES = 20
MAX_REQUESTS = 5
MAX_DEPTH = 3
PAGE = re.compile(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
# 共通運用ルール・役割表・言動ガイド・細則：変更は承認が要る（設計 v0.3 の 4-3）。
RULE_PAGES = {'3ecf1dff94dc819e9ff8f8a7d5db746c', '413f1dff94dc8107b8cefa525c2c326a',
              '40ef1dff94dc810da820cef144deb503', '412f1dff94dc8104b551db9f09a3108c'}


def page_id(value):
    match = PAGE.search(value or '')
    require(match is not None, 'Notionのページを特定できません')
    return match.group(0).replace('-', '').lower()


def protected(cfg):
    pages = set(RULE_PAGES)
    pages.update(url.split('/')[-1].replace('-', '').lower() for url in REPORT_PAGES.values())
    pages.update(url.split('/')[-1].replace('-', '').lower() for url in cfg.pages.values())
    pages.add(cfg.requests_page.replace('-', '').lower())
    pages.add(cfg.log_page.replace('-', '').lower())
    return pages


def paragraphs(text):
    from .notion import text_chunks
    blocks = []
    for part in [p for p in text.split('\n\n') if p.strip()][:40]:
        blocks.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text':
                       [{'type': 'text', 'text': {'content': c}} for c in text_chunks(part)]}})
    return blocks


class Broker:
    def __init__(self, cfg, notion, store=None):
        self.cfg, self.notion = cfg, notion
        self.store = store or Store(cfg)

    def job(self, packet):
        run, agent = packet.get('run_id'), packet.get('agent')
        require(agent in NAMES, 'unknown agent')
        job = self.store.load(run)
        require(job['agent'] == agent and job['state'] == 'running', 'このjobは実行中ではありません')
        return job

    def allow(self, job, tool):
        require(tool in job['tools'], tool + 'の道具箱はまだオフです')

    def count(self, job, key, limit):
        with exclusive(self.store.root / 'broker.lock', wait=True):
            fresh = self.store.load(job['run_id'])
            used = fresh.get('broker_' + key, 0)
            require(used < limit, key + 'の上限に達しました')
            fresh['broker_' + key] = used + 1
            self.store.save(fresh)

    def handle(self, packet):
        if self.cfg.stop.exists():
            raise StopRequested('STOP')
        job = self.job(packet)
        op = packet.get('op')
        if op == 'notion_read':
            self.allow(job, 'read')
            return self.read(page_id(packet.get('page_id')))
        if op in ('notion_append', 'notion_create_page'):
            self.allow(job, 'notion')
            target = page_id(packet.get('page_id') or packet.get('parent_page_id'))
            require(target not in protected(self.cfg), 'このページの変更は承認が要ります（共通ルール・完了報告・依頼一覧・ログ）')
            text = packet.get('text') or ''
            require(isinstance(text, str) and 0 < len(text) <= TEXT_LIMIT, '本文が空か長すぎます')
            compose.assert_safe(text, self.cfg.secrets)
            self.count(job, 'notion_writes', MAX_WRITES)
            if op == 'notion_append':
                self.notion._request('PATCH', target, children=True, payload={'children': paragraphs(text)})
                return {'ok': True, 'page_id': target}
            title = packet.get('title') or ''
            require(isinstance(title, str) and 0 < len(title) <= 200 and '\n' not in title, 'タイトルが不正です')
            compose.assert_safe(title, self.cfg.secrets)
            created = self.create_page(target, title, text)
            return {'ok': True, 'page_id': created}
        if op == 'request_submit':
            self.allow(job, 'request')
            return self.submit(job, packet.get('text') or '')
        raise SafetyError('知らない操作です')

    def read(self, target):
        rows = self.notion.read_blocks(target)
        out = []
        for b in rows:
            kind = b.get('type', '')
            if kind == 'child_page':
                out.append('[子ページ] ' + b.get('child_page', {}).get('title', '') + ' id=' + b.get('id', ''))
            elif kind == 'child_database':
                out.append('[DB] ' + b.get('child_database', {}).get('title', '') + ' id=' + b.get('id', ''))
            else:
                text = block_text(b)
                if text:
                    out.append(('# ' if kind.startswith('heading_') else '') + text)
        text = '\n'.join(out)
        return {'ok': True, 'page_id': target, 'text': text[:READ_LIMIT], 'cut': len(text) > READ_LIMIT}

    def create_page(self, parent, title, text):
        result = self.notion.transport.request('POST', 'https://api.notion.com/v1/pages', headers={
            'Authorization': 'Bearer ' + self.notion.token, 'Notion-Version': self.notion.version}, payload={
            'parent': {'page_id': parent},
            'properties': {'title': {'title': [{'type': 'text', 'text': {'content': title}}]}},
            'children': paragraphs(text)}, timeout=20)
        require(isinstance(result, dict) and result.get('object') == 'page', 'ページを作れませんでした')
        return result.get('id', '')

    def submit(self, job, text):
        name, heart = job['name'], HEARTS[job['agent']]
        require(job.get('depth', 0) + 1 <= MAX_DEPTH, '依頼のつながりは3段までです')
        draft = {'id': 'agent-draft', 'type': 'code', 'code': {'rich_text': [{'text': {'content': text}}]}}
        rows = parse_requests([draft])
        require(len(rows) == 1, '【りりへの依頼】を1件だけ書いてください')
        req = rows[0]
        require(req.requester == name and req.id.startswith(heart), '依頼した子と依頼IDの色は' + name + '（' + heart + '）にしてください')
        require(req.status == '未着手' and name not in req.recipients and all(r in RECIPIENTS for r in req.recipients),
                '宛先か状態が不正です（自分宛ては不可、状態は未着手）')
        validate_request(req)
        compose.assert_request_safe(req, self.cfg.secrets)
        compose.instruction(req, [r for r in req.recipients if r != 'りり'] or ['りり'], self.cfg.pages)
        self.count(job, 'requests', MAX_REQUESTS)
        page = self.cfg.requests_page
        blocks = self.notion.read_blocks(page)
        require(not any(re.search(r'^依頼ID[：:]\s*' + re.escape(req.id) + r'\s*$', block_text(b), re.M) for b in blocks),
                '同じ依頼IDがもうあります')
        anchors = [b['id'] for b in blocks if b.get('type', '').startswith('heading_') and not b.get('archived')
                   and block_text(b).strip() == '依頼一覧（新しい順）']
        require(len(anchors) == 1, '依頼一覧の見出しを確認できません')
        from .notion import text_chunks
        self.notion._request('PATCH', page, children=True, payload={
            'position': {'type': 'after_block', 'after_block': {'id': anchors[0]}}, 'children': [
                {'object': 'block', 'type': 'code', 'code': {'language': 'plain text', 'rich_text':
                 [{'type': 'text', 'text': {'content': c}} for c in text_chunks(text)]}}]})
        with exclusive(self.store.root / 'broker.lock', wait=True):
            path = self.store.root / 'children.json'
            children = self.store.children()
            children[req.id] = {'parent_run': job['run_id'], 'parent_request': job['request_id'],
                                'depth': job.get('depth', 0) + 1}
            from .local_outcomes import atomic_json
            atomic_json(path, children)
        return {'ok': True, 'request_id': req.id}


def main(argv=None, stdin=None, root=None):
    try:
        packet = json.loads((stdin or sys.stdin).read(200000))
        require(isinstance(packet, dict), 'invalid packet')
        cfg = Config.load(Path(root or Path(__file__).resolve().parents[1]))
        require(cfg.mode == 'live', 'りりが本番モードではありません')
        from .http import Transport
        from .notion import NotionClient

        def guard():
            if cfg.stop.exists():
                raise StopRequested('STOP')
        notion = NotionClient(cfg.notion_token, Transport(deadline=time.monotonic() + 30, before_request=guard),
                              cfg.notion_version)
        out = Broker(cfg, notion).handle(packet)
    except (PatrolError, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, SafetyError) else type(exc).__name__
        out = {'ok': False, 'error': message[:300]}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
