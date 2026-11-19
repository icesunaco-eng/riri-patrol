"""Ordinary requests for まむ / みき: hand off, wake one runner, ghost-write the report.

Design: Notion「まむ・みき｜りり経由の依頼受付と完了報告 設計 v0.3」.

Riri owns every credential. A job is a frozen copy of one formal request; the
actor's runner receives it on stdin with a minimal environment and returns one
result file. Riri then writes the actor's 【完了報告】 on the actor's own report
page and reads it back. Model text never selects a page, a command or a state
transition here: it only becomes report prose after the existing safety check.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

from . import compose
from .errors import PatrolError, SafetyError, StopRequested
from .local_outcomes import atomic_json, read_json
from .models import REPORT_PAGES

AGENTS = {'まむ': 'mamu', 'みき': 'miki'}
NAMES = {v: k for k, v in AGENTS.items()}
HEARTS = {'mamu': '🧡', 'miki': '💜'}
HEAVY = Path('/private/tmp/heroines-heavy-model.lock')
STATUSES = ('完了', '一部完了', 'できなかった', '承認待ち')
TOOLS = ('read', 'write', 'run', 'github', 'notion', 'request')
RUNNER = {'mamu': 'start-mamu-agent.sh', 'miki': 'start-miki-agent.sh'}
MAX_ATTEMPTS = 3
RETRY_GAP = timedelta(minutes=5)
WAIT_LIMIT = timedelta(hours=24)
RUN_LIMIT = timedelta(minutes=35)   # the runner stops itself at 30 minutes
ANSWER_LIMIT = 3000
RESULT_LIMIT = 256 * 1024
FIXED_MARKERS = ('local-run=', 'manifest-sha256=', 'project-id=')
RUN_ID = re.compile(r'agent-(mamu|miki)-[0-9a-f]{20}')


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat()


def parse_time(value):
    return datetime.fromisoformat(value)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def run_id(request_id, agent):
    return 'agent-' + agent + '-' + sha(request_id + '\n' + agent)[:20]


def base(cfg):
    return Path(cfg.root) / 'state' / 'agent-lane'


def enabled(cfg, agent):
    return bool(getattr(cfg, 'local_agent_' + agent, False))


def lane_names(req):
    """まむ / みき among the recipients, unless an existing fixed job owns this request.

    A request carrying any fixed-job marker (even a damaged one) stays with the
    existing bounded path, which holds it with the precise missing-marker reason.
    """
    if any(marker in req.text for marker in FIXED_MARKERS):
        return []
    return [name for name in req.recipients if name in AGENTS]


@contextmanager
def exclusive(path, *, wait=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1, 'unsafe lane lock')
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def alive(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def heavy_free(path=HEAVY):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@dataclass
class Intake:
    taken: list = field(default_factory=list)   # lane names that got a job
    off: list = field(default_factory=list)     # lane names held because the lane is off
    defer: bool = False                         # rate limited: try again next patrol

    @property
    def only(self):
        """True when nothing is left for the ordinary Slack instruction."""
        return bool(self.taken or self.off)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = base(cfg)
        self.jobs = self.root / 'jobs'

    def setup(self):
        for path in (self.root, self.jobs):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(not path.is_symlink() and path.stat().st_uid == os.getuid(), 'unsafe lane directory')
            os.chmod(path, 0o700)

    def path(self, run):
        require(RUN_ID.fullmatch(run) is not None, 'invalid lane run')
        return self.jobs / (run + '.json')

    def load(self, run):
        return read_json(self.path(run))

    def save(self, job):
        atomic_json(self.path(job['run_id']), job)

    def rows(self):
        if not self.jobs.exists():
            return []
        out = []
        for entry in sorted(self.jobs.iterdir()):
            if entry.suffix == '.json' and RUN_ID.fullmatch(entry.stem):
                try:
                    out.append(read_json(entry))
                except (OSError, ValueError):
                    continue
        return out

    def children(self):
        path = self.root / 'children.json'
        return read_json(path) if path.exists() else {}

    def depth_of(self, request_id):
        child = self.children().get(request_id)
        return int(child.get('depth', 0)) if isinstance(child, dict) else 0


def within_limits(cfg, rows, agent, at):
    made = [parse_time(j['created_at']) for j in rows if j.get('agent') == agent]
    hour = sum(1 for t in made if at - t < timedelta(hours=1))
    day = sum(1 for t in made if at - t < timedelta(days=1))
    return hour < getattr(cfg, 'agent_hourly', 20) and day < getattr(cfg, 'agent_daily', 150)


def handoff_text(req, name):
    return ('【受け渡し】' + compose.safe_reference(req.id) + '（' + req.requester + '→' + name + '）を'
            + name + 'に渡したよ🍚 結果は' + name + 'の完了報告ページに出るよ')


def off_text(req, name):
    return (compose.safe_reference(req.id) + ' は' + name + '宛てだけど、' + name
            + 'の受け口が止まっているので保留にしたよ🍚 状態は未着手のまま。受け口をオンにしたら次の見回りで受け取るね')


def intake(patrol, req):
    """Take the lane recipients of one new 未着手 request. Live mode only."""
    cfg = patrol.config
    names = lane_names(req)
    result = Intake()
    if cfg.mode != 'live' or not names:
        return result
    store = Store(cfg)
    store.setup()
    at = now()
    with exclusive(store.root / 'intake.lock', wait=True):
        rows = store.rows()
        for name in names:
            agent = AGENTS[name]
            if not enabled(cfg, agent):
                key = 'agent-off:' + agent + ':' + req.id
                if key not in patrol.state['outbox']:
                    patrol.queue(key, cfg.report_channel, off_text(req, name), 'agent_notice', request_id=req.id)
                    patrol.event('受け口オフのため保留', req.id, req.requester, '一部完了', memo=name)
                result.off.append(name)
                continue
            if req.requester == name:
                raise SafetyError(name + 'は自分宛ての依頼を受けられません')
            run = run_id(req.id, agent)
            if store.path(run).exists():
                result.taken.append(name)
                continue
            if not within_limits(cfg, rows, agent, at):
                result.defer = True
                continue
            tools = [t for t in getattr(cfg, 'agent_tools', ('read',)) if t in TOOLS]
            job = {
                'schema': 1, 'run_id': run, 'agent': agent, 'name': name,
                'request_id': req.id, 'requester': req.requester, 'recipients': list(req.recipients),
                'block_id': req.block_id, 'request_sha256': sha(req.text), 'text': req.text,
                'fields': {k: v for k, v in req.fields.items() if isinstance(v, str)},
                'tools': tools, 'depth': store.depth_of(req.id),
                'created_at': iso(at), 'not_before': iso(at), 'attempts': 0,
                'state': 'queued', 'report_state': 'none',
            }
            store.save(job)
            rows.append(job)
            patrol.queue('agent-handoff:' + agent + ':' + req.id, cfg.report_channel,
                         handoff_text(req, name), 'agent_handoff', request_id=req.id)
            patrol.event(name + 'に受け渡し', req.id, req.requester, '一部完了', memo='run=' + run)
            result.taken.append(name)
    patrol.save()
    return result


# ---------------------------------------------------------------- reporting

def clean_lines(text, limit):
    text = (text or '').strip()
    if len(text) > limit:
        text = text[:limit] + '…'
    return text


def report_body(job, result, secrets):
    name = job['name']
    status = result['status']
    summary = clean_lines(result.get('summary') or '（まとめがありません）', 1500)
    # Prefix every model line so it can never become a second formal field.
    summary = '\n'.join('・' + line for line in summary.splitlines() if line.strip()) or '・（まとめがありません）'
    ev = result.get('evidence') if isinstance(result.get('evidence'), dict) else {}
    parts = ['run=' + job['run_id'], 'result_sha256=' + result['_sha256'], 'りりが代筆']
    for key, label in (('model', 'モデル'), ('steps', '歩数')):
        if isinstance(ev.get(key), (str, int)):
            parts.append(label + '=' + str(ev[key]))
    for key, label in (('files', 'ファイル'), ('commits', 'コミット'), ('prs', 'PR'), ('notion', 'Notion'), ('requests', '出した依頼')):
        items = ev.get(key)
        if isinstance(items, list) and items:
            parts.append(label + '=' + '、'.join(str(x) for x in items[:8]))
    approvals = result.get('approvals') if isinstance(result.get('approvals'), list) else []
    unverified = clean_lines(result.get('unverified') or '', 400)
    unverified = ('モデル（ローカル）の作業結果。内容の正しさは依頼した子が確認する。' + (unverified.replace('\n', ' ') if unverified else ''))
    if approvals:
        unverified += ' 承認待ち：' + '／'.join(clean_lines(str(a), 120).replace('\n', ' ') for a in approvals[:5])
    reason = clean_lines(result.get('reason') or '', 300).replace('\n', ' ')
    next_step = clean_lines(result.get('next') or '依頼した子が結果を確認する', 300).replace('\n', ' ')
    project = job['fields'].get('案件ページ', '').strip() or 'なし'
    body = '\n'.join([
        '【完了報告】', '依頼ID：' + job['request_id'], '報告した子：' + name, '案件ページ：' + project,
        '作業場所URL：取得できない',
        '作業場所URLが取得できない理由：このMacのローカルプロセスで実行し、共有された会話URLがないため。',
        '結果：' + status, 'やったこと：\n' + summary, '証拠：' + ' '.join(parts),
        'できなかったこと・未確認：' + unverified,
        'ブロッカー：' + (reason or 'なし'),
        '次にやること：' + next_step])
    compose.assert_safe(body, secrets)
    return body


def answer_block(job, result, secrets):
    answer = (result.get('answer') or '').strip()
    if not answer:
        return None
    cut = len(answer) > ANSWER_LIMIT
    answer = answer[:ANSWER_LIMIT]
    lines = ['【回答全文】run=' + job['run_id']] + ['｜' + line for line in answer.splitlines()]
    if cut:
        lines.append('（3000字で切りました。続きは分けて依頼してね）')
    text = '\n'.join(lines)
    compose.assert_safe(text, secrets)
    return text


class Mailbox:
    """Append and read back one report on the actor's own report page."""
    HEADER = '報告一覧（新しい順）'

    def __init__(self, cfg, notion=None):
        self.cfg = cfg
        if notion is None:
            from .http import Transport
            from .notion import NotionClient

            def guard():
                if cfg.stop.exists():
                    raise StopRequested('stopped')
            notion = NotionClient(cfg.notion_token, Transport(deadline=time.monotonic() + 20, before_request=guard),
                                  cfg.notion_version)
        self.notion = notion

    def page(self, agent):
        name = NAMES[agent]
        official = REPORT_PAGES[name].split('/')[-1]
        require(self.cfg.pages[name].split('/')[-1].replace('-', '') == official, 'report page mismatch')
        return official

    def find(self, job, body):
        from .parse import block_text, parse_reports
        page = self.page(job['agent'])
        blocks = self.notion.read_blocks(page)
        headers = [b for b in blocks if b.get('type', '').startswith('heading_') and not b.get('archived')
                   and block_text(b).strip() == self.HEADER]
        require(len(headers) == 1, 'report header missing or duplicate')
        lines = [b for b in blocks if re.search(r'^依頼ID[：:]\s*' + re.escape(job['request_id']) + r'\s*$', block_text(b), re.M)]
        rows = parse_reports(lines, expected_reporter=job['name'])
        if not lines:
            return headers[0]['id'], None
        require(len(lines) == len(rows) == 1, 'report identity collision')
        require(rows[0].text == body, 'report content collision')
        return headers[0]['id'], {'block_id': rows[0].block_id, 'report_sha256': rows[0].digest, 'page_id': page}

    def append(self, job, anchor, body, answer):
        from .notion import text_chunks
        page = self.page(job['agent'])
        children = [{'object': 'block', 'type': 'code', 'code': {'language': 'plain text', 'rich_text':
                     [{'type': 'text', 'text': {'content': t}} for t in text_chunks(body)]}}]
        if answer:
            children.append({'object': 'block', 'type': 'code', 'code': {'language': 'plain text', 'rich_text':
                             [{'type': 'text', 'text': {'content': t}} for t in text_chunks(answer)]}})
        self.notion._request('PATCH', page, children=True, payload={
            'position': {'type': 'after_block', 'after_block': {'id': anchor}}, 'children': children})


# ---------------------------------------------------------------- runtime

def agent_root(agent):
    from .paths import MAMU_ROOT, MIKI_ROOT
    return Path(MAMU_ROOT if agent == 'mamu' else MIKI_ROOT)


def result_path(job):
    return agent_root(job['agent']) / 'state' / 'agent-runs' / job['run_id'] / 'result.json'


def read_result(job):
    path = result_path(job)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as fh:
        info = os.fstat(fh.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size <= RESULT_LIMIT, 'invalid result file')
        raw = fh.read(RESULT_LIMIT + 1)
    data = json.loads(raw.decode('utf-8'))
    require(isinstance(data, dict) and data.get('schema') == 1 and data.get('run_id') == job['run_id']
            and data.get('agent') == job['agent'] and data.get('status') in STATUSES, 'invalid result')
    for key in ('summary', 'unverified', 'next', 'answer', 'reason'):
        require(data.get(key) is None or isinstance(data[key], str), 'invalid result field')
    data['_sha256'] = hashlib.sha256(raw).hexdigest()
    return data


def minimal_env(agent):
    keep = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    keep['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin'
    keep['HEROINES_AGENT'] = agent
    return keep


def launch(cfg, job, popen=subprocess.Popen):
    """Start the actor runner detached; it gets the job on stdin and no credentials."""
    root = agent_root(job['agent'])
    script = root / RUNNER[job['agent']]
    require(script.is_file() and not script.is_symlink(), 'runner missing')
    packet = {k: job[k] for k in ('schema', 'run_id', 'agent', 'name', 'request_id', 'requester', 'recipients',
                                  'text', 'fields', 'tools', 'depth')}
    packet['broker'] = [sys.executable, '-B', '-m', 'riri.agent_broker']
    packet['broker_cwd'] = str(cfg.root)
    log_dir = base(cfg) / 'logs'
    log_dir.mkdir(mode=0o700, exist_ok=True)
    out = os.open(log_dir / (job['run_id'] + '.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        proc = popen([str(script)], cwd=str(root), stdin=subprocess.PIPE, stdout=out, stderr=out,
                     env=minimal_env(job['agent']), start_new_session=True, close_fds=True)
        proc.stdin.write(json.dumps(packet, ensure_ascii=False).encode('utf-8'))
        proc.stdin.close()
    finally:
        os.close(out)
    return proc.pid


def finish(job, status, reason):
    """A result written by Riri itself when the runner could not produce one."""
    raw = json.dumps({'schema': 1, 'run_id': job['run_id'], 'agent': job['agent'], 'status': status,
                      'reason': reason, 'summary': reason}, ensure_ascii=False)
    data = json.loads(raw)
    data['_sha256'] = sha(raw)
    return data


def tick(patrol, *, mailbox=None, popen=subprocess.Popen, clock=now):
    """One bounded step per patrol: collect, report one, launch one."""
    cfg = patrol.config
    if cfg.mode != 'live':
        return {'lane': 'practice'}
    store = Store(cfg)
    if not store.jobs.exists():
        return {'lane': 'empty'}
    try:
        lock = exclusive(store.root / 'tick.lock')
        lock.__enter__()
    except BlockingIOError:
        return {'lane': 'busy'}
    try:
        at = clock()
        rows = store.rows()
        for job in rows:
            collect(store, job, at)
        rows = store.rows()
        pending = sorted((j for j in rows if j['state'] == 'done' and j['report_state'] in ('pending', 'unknown')),
                         key=lambda j: j.get('finished_at', ''))
        reported = None
        if pending:
            reported = deliver(cfg, store, pending[0], mailbox or Mailbox(cfg))
        launched = None
        if not cfg.stop.exists() and not any(j['state'] == 'running' for j in rows):
            launched = start_next(cfg, store, rows, at, popen)
        return {'lane': 'ok', 'reported': reported, 'launched': launched}
    finally:
        lock.__exit__(None, None, None)


def collect(store, job, at):
    if job['state'] == 'queued' and at - parse_time(job['created_at']) > WAIT_LIMIT:
        job.update(state='done', report_state='pending', finished_at=iso(at),
                   result=finish(job, 'できなかった', '24時間待っても始められませんでした'))
        store.save(job)
        return
    if job['state'] != 'running':
        return
    path = result_path(job)
    if path.exists():
        try:
            result = read_result(job)
        except (OSError, ValueError, SafetyError) as exc:
            result = finish(job, 'できなかった', '結果ファイルを読めませんでした（' + type(exc).__name__ + '）')
        if result.get('reason') == 'busy' and result['status'] == 'できなかった' and job['attempts'] < MAX_ATTEMPTS:
            job.update(state='queued', not_before=iso(at + RETRY_GAP), last_busy_at=iso(at))
            os.replace(path, path.with_name('result.busy-' + str(job['attempts']) + '.json'))
        else:
            job.update(state='done', report_state='pending', finished_at=iso(at), result=result)
        store.save(job)
        return
    started = parse_time(job['started_at'])
    if not alive(job.get('pid')):
        job.update(state='done', report_state='pending', finished_at=iso(at),
                   result=finish(job, 'できなかった', '作業プロセスが結果を残さずに終わりました'))
        store.save(job)
    elif at - started > RUN_LIMIT:
        try:
            os.killpg(job['pid'], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        job.update(state='done', report_state='pending', finished_at=iso(at),
                   result=finish(job, '一部完了', '時間の上限（30分）を超えたので止めました'))
        store.save(job)


def start_next(cfg, store, rows, at, popen):
    queued = sorted((j for j in rows if j['state'] == 'queued' and enabled(cfg, j['agent'])
                     and parse_time(j['not_before']) <= at), key=lambda j: j['created_at'])
    if not queued:
        return None
    job = queued[0]
    if not heavy_free():
        job['attempts'] += 1
        if job['attempts'] >= MAX_ATTEMPTS:
            job.update(state='done', report_state='pending', finished_at=iso(at),
                       result=finish(job, 'できなかった', '重いモデルが使用中で、3回試しても始められませんでした'))
        else:
            job['not_before'] = iso(at + RETRY_GAP)
        store.save(job)
        return None
    job['attempts'] += 1
    job.update(state='running', started_at=iso(at))
    store.save(job)   # durable before the process exists
    try:
        job['pid'] = launch(cfg, job, popen)
    except (OSError, SafetyError) as exc:
        job.update(state='done', report_state='pending', finished_at=iso(at),
                   result=finish(job, 'できなかった', '作業プロセスを起動できませんでした（' + type(exc).__name__ + '）'))
    store.save(job)
    return job['run_id']


def deliver(cfg, store, job, mailbox):
    result = job['result']
    body = report_body(job, result, cfg.secrets)
    answer = answer_block(job, result, cfg.secrets)
    if job.get('report_sha256'):
        require(job['report_sha256'] == sha(body), 'report outbox changed')
    else:
        job['report_sha256'] = sha(body)
        store.save(job)   # durable before any Notion write
    try:
        anchor, found = mailbox.find(job, body)
        if found is None and job['report_state'] == 'pending':
            job['report_state'] = 'unknown'
            store.save(job)   # a crash during append only ever reads afterwards
            mailbox.append(job, anchor, body, answer)
            anchor, found = mailbox.find(job, body)
        if found:
            job.update(report_state='received', receipt={**found, 'received_at': iso(), 'source': 'full_readback'})
            store.save(job)
    except (PatrolError, OSError, ValueError) as exc:
        from .errors import RemoteError
        if isinstance(exc, RemoteError) and not exc.uncertain and job['report_state'] == 'unknown':
            job['report_state'] = 'pending'   # explicitly rejected: nothing was written
        job['last_issue'] = type(exc).__name__
        store.save(job)
    return job['run_id']
