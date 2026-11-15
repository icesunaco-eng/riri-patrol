"""Bounded Miki implementation / Mamu inspection dispatch on Riri's official mailbox.

Only an explicitly registered, read-back request can start an actor. Model text
never selects a command, path, recipient, report destination or state operation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
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
from .config import Config
from .errors import PatrolError, RemoteError, SafetyError, StopRequested
from .http import Transport
from .local_outcomes import atomic_json, digest, read_json
from .models import REPORT_PAGES
from .notion import NotionClient, text_chunks
from .parse import block_text, parse_reports, parse_requests, replace_status, validate_request

ROOT = Path('/Users/sunako.jp/riri-patrol')
MIKI = Path('/Users/sunako.jp/miki-AI')
MAMU = Path('/Users/sunako.jp/mamu-AI')
HEAVY = Path('/private/tmp/heroines-heavy-model.lock')
MAIL = '413f1dff94dc81d2beebdf1b69f5c439'
PROJECT = 'https://app.notion.com/p/418f1dff94dc81ffabc6d13f9984b5ca'
ACTORS = {'miki': ('みに', 'みき', '🩵'), 'mamu': ('みき', 'まむ', '💜')}
IDENTITY = ('actor', 'request_id', 'run_id', 'prior_run', 'spec_sha256', 'artifact_sha256')
HASH = re.compile(r'[a-f0-9]{64}')
ORIGINAL = 'gemma26-A-v1'
CONTINUED = ORIGINAL + '-cont1'
REVISION = ORIGINAL + '-rev1'
REQUEST = '🩵20261115-1'
RUN = re.compile(r'gemma26-A-v1(?:-cont1|-rev1)?|mamu-a-review-[0-9a-f]{32}')
# Immutable fingerprints from the one cancelled, read-only attempt. Fingerprints
# cover the old Riri ledger, actor job, journal, 10 intents and 9 generation/result pairs.
ORIGINAL_LEDGER = '5a516fc8f99d35e85b7f99f81174c059274f91da9d31b6774bc396258f6ef40a'
ORIGINAL_EVIDENCE = '6b711de70571a2d38f47473cc7c1be63bd1889f53f4c63e754af08c01e3b5955'



def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def sha(text):
    return digest(text.encode('utf-8'))


def check_job(job):
    actor = job['actor']
    require(actor in ACTORS, 'unknown local actor')
    rid, run = job['request_id'], job['run_id']
    sender, receiver, color = ACTORS[actor]
    match = re.fullmatch(color + r'(\d{8})-[1-9]\d{0,8}', rid)
    require(match and match[1] >= '20261115', 'legacy or invalid local request')
    datetime.strptime(match[1], '%Y%m%d')
    require(RUN.fullmatch(run) and HASH.fullmatch(job['spec_sha256']), 'invalid local job')
    if actor == 'miki':
        require(run == ORIGINAL and job['artifact_sha256'] is None and job['prior_run'] is None, 'use explicit continuation for linked A')
    else:
        require(run.startswith('mamu-a-review-') and job['prior_run'] in (ORIGINAL, CONTINUED) and HASH.fullmatch(job['artifact_sha256']), 'invalid inspection job')


@contextmanager
def exclusive(path, *, wait=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1, 'unsafe task lock')
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def heavy_free(path=HEAVY):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(fd)


class Mailbox:
    def __init__(self, cfg):
        self.cfg = cfg
        self.anchors = {}
        def guard():
            if cfg.stop.exists() or (cfg.root / 'STOP').exists():
                raise StopRequested('stopped')
        transport = Transport(deadline=time.monotonic() + 6, before_request=guard)
        self.notion = NotionClient(cfg.notion_token, transport, cfg.notion_version)

    def intake(self, job):
        require(self.cfg.requests_page.replace('-', '') == MAIL, 'official request page changed')
        return self.intake_from(self.notion.read_blocks(MAIL), job)

    def intake_from(self, rows, job, *, parsed=False):
        rows = [r for r in (rows if parsed else parse_requests(rows)) if r.id == job['request_id']]
        require(len(rows) == 1, 'missing or duplicate formal request')
        r = rows[0]
        sender, receiver, _ = ACTORS[job['actor']]
        require(r.requester == sender and r.recipients == [receiver] and r.status in ('未着手','対応中','完了') and r.fields.get('案件ページ') == PROJECT, 'formal request scope mismatch')
        markers = re.findall(r'(?<![\w-])local-run=([\w-]+)', r.text)
        require(markers == [job['run_id']], 'formal run marker mismatch')
        validate_request(r)
        compose.assert_request_safe(r, self.cfg.secrets)
        compose.instruction(r, [receiver], self.cfg.pages)  # Existing 3000 body / 1500 header budget.
        return {'block_id': r.block_id, 'request_sha256': sha(replace_status(r.text, '未着手')), 'received_at': now(),'source_status':r.status}

    def report_page(self, actor):
        name = ACTORS[actor][1]
        official = REPORT_PAGES[name].split('/')[-1]
        require(self.cfg.pages[name].split('/')[-1].replace('-', '') == official, 'formal report page mismatch')
        return official

    def current_report(self, job):
        page = self.report_page(job['actor'])
        blocks = self.notion.read_blocks(page)
        headers = [b for b in blocks if b.get('type') in ('heading_1','heading_2','heading_3') and not b.get('archived') and block_text(b).strip() == '報告一覧（新しい順）']
        require(len(headers) == 1, 'formal report header missing or duplicate')
        self.anchors[page] = headers[0]['id']
        # Match only this formal ID, leaving unrelated old reports untouched.
        lines = [b for b in blocks if re.search(r'^依頼ID[：:]\s*' + re.escape(job['request_id']) + r'\s*$', block_text(b), re.M)]
        rows = parse_reports(lines, expected_reporter=ACTORS[job['actor']][1])
        if lines:
            require(len(rows) == len(lines) == 1 and rows[0].id==job['request_id'], 'report identity collision')
            return {'block_id':rows[0].block_id,'report_sha256':rows[0].digest,'report_page_id':page,
                    'text':rows[0].text,'type':lines[0]['type'],'version':lines[0].get('last_edited_time')}
        return None

    def find_report(self, job, body):
        record=self.current_report(job)
        if record is None:
            return None
        require(record['text']==body,'report content collision')
        return {k:record[k] for k in ('block_id','report_sha256','report_page_id')}

    def replace_report(self, previous, new_body):
        # Compare the exact block/version immediately before PATCH. Reconciliation
        # after a timeout is read-only and never appends a second report.
        block=self.notion._request('GET',previous['block_id'])
        require(previous['version'] and block.get('last_edited_time')==previous['version']
                and block.get('id')==previous['block_id'] and block.get('type')=='code'
                and block.get('object')=='block' and block.get('in_trash') is False and not block.get('archived')
                and block_text(block)==previous['text'],'report changed before replacement')
        chunks=[{'type':'text','text':{'content':v}} for v in text_chunks(new_body)]
        self.notion._request('PATCH',block['id'],payload={'code':{'language':block['code'].get('language','plain text'),
                                                                 'rich_text':chunks}})

    def append_report(self, job, body):
        page = self.report_page(job['actor'])
        require(page in self.anchors, 'formal report destination not verified')
        chunks = [{'type':'text', 'text':{'content':t}} for t in text_chunks(body)]
        self.notion._request('PATCH', page, children=True, payload={'position':{'type':'after_block','after_block':{'id':self.anchors[page]}}, 'children':[{'object':'block','type':'code','code':{'language':'plain text','rich_text':chunks}}]})


def make_report(job, payload, secrets):
    actor = job['actor']; expected_stage = 'inspection_wait' if actor == 'miki' else 'reviewed'
    require(payload['actor'] == actor and payload['request_id'] == job['request_id'] and payload['run_id'] == job['run_id']
            and payload['stage'] in ({expected_stage,'implementation_incomplete'} if actor == 'miki' else {'reviewed','inspection_completed','inspection_incomplete'}) and HASH.fullmatch(payload['artifact_sha256']) and HASH.fullmatch(payload['result_sha256']), 'report identity mismatch')
    if actor == 'miki' and payload['stage']=='inspection_wait':
        require(payload['quality'] in ('pending','partial','failed','unverified'), 'implementation awaits inspection')
    if actor == 'mamu':
        require(payload['artifact_sha256'] == job['artifact_sha256'], 'inspection target changed')
    require(payload['quality'] in ('pending','unassessed','passed','failed','partial','unverified'), 'invalid quality')
    summary = payload['summary']
    require(isinstance(summary,str) and summary.strip() and len(summary) <= 2300, 'invalid result summary')
    compose.assert_safe(summary, secrets)
    # Prefix every model/source line; it must not inject a second formal field.
    summary = '\n'.join('・' + line for line in summary.strip().splitlines())
    status = '一部完了（実装候補保存・検査待ち）' if actor == 'miki' else '完了（検査報告・対象コードの品質判定とは別）'
    if payload['stage']=='implementation_incomplete':
        require(actor=='miki' and payload['quality'] in ('failed','unverified'), 'incomplete implementation cannot pass')
        status='一部完了（実装未完・検査対象候補なし）'
    if payload['stage']=='inspection_incomplete':
        require(actor=='mamu' and payload['quality'] in ('partial','failed','unverified'), 'incomplete report cannot pass')
        status='一部完了（検査未了）'
    body = '\n'.join(['【完了報告】',f"依頼ID：{job['request_id']}",f"報告した子：{ACTORS[actor][1]}",'案件ページ：'+PROJECT,'作業場所URL：取得できない',
        '作業場所URLが取得できない理由：このMacのローカルプロセスで実行し、共有された会話URLがないため。','結果：'+status,'やったこと：\n'+summary,
        '証拠：'+f"run={job['run_id']} stage={payload['stage']} spec_sha256={job['spec_sha256']} artifact_sha256={payload['artifact_sha256']} result_sha256={payload['result_sha256']}",
        'できなかったこと・未確認：案件全体の完成確認は別途。内容品質区分 '+payload['quality'],'ブロッカー：保存済み実行結果を参照。',
        '次にやること：'+('保存済み診断を確認。' if payload['stage']=='implementation_incomplete' else 'この版をまむが検査。' if actor=='miki' else '指摘があれば仕様と照合。')])
    if job.get('amends'):
        v=job['amends']
        body+='\n訂正履歴：第2版。前版run='+v['run_id']+' 前版報告SHA-256='+v['receipt']['report_sha256']+'\n訂正理由：'+v['reason']
    compose.assert_safe(body,secrets)
    return body


def received_before_patrol(cfg, req, reports):
    """Compare this patrol's actual Notion rows with the exact existing local receipt."""
    markers = re.findall(r'(?<![\w-])local-run=([\w-]+)', req.text)
    if cfg.mode != 'live' or len(markers) != 1 or not RUN.fullmatch(markers[0]) or len(reports) != 1:
        return False
    run = markers[0]
    base = cfg.root / 'state' / 'local-tasks'
    if base.resolve() != base or not base.is_dir():
        return False
    try:
        job = read_json(base / (run + '.job.json'))
        if job['run_id'] != run or job['request_id'] != req.id:
            return False
        check_job(job)
        if (job['report_state'], job['execution_state'], job['resource_state']) != ('received', 'finished', 'released'):
            return False
        mail = Mailbox(cfg)
        intake = mail.intake_from([req], job, parsed=True)
        if any(intake[k] != job['request_receipt'][k] for k in ('block_id', 'request_sha256')):
            return False
        actor = job['actor']
        packet = {**job['outbox'], 'actor': actor, 'request_id': req.id, 'run_id': run}
        body = make_report(job, packet, cfg.secrets)
        report = reports[0]
        receipt = job['receipt']
        expected = {'actor': actor, 'request_id': req.id, 'run_id': run,
                    'receiver': 'riri', 'source': 'formal_page_full_readback',
                    'artifact_sha256': packet['artifact_sha256'], 'result_sha256': packet['result_sha256'],
                    'report_page_id': mail.report_page(actor), 'block_id': report.block_id,
                    'report_sha256': report.digest}
        return (report.id == req.id and report.reporter == ACTORS[actor][1]
                and report.text == body and sha(body) == report.digest == job['report_sha256']
                and all(receipt[k] == v for k, v in expected.items()))
    except (KeyError, TypeError, ValueError, OSError, PatrolError):
        return False


def child_env():
    base = {k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','LANG','LC_ALL') if k in os.environ}
    return {**base,'PATH':'/Library/Frameworks/Python.framework/Versions/3.14/bin:/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}


def clean_subprocess(root, args):
    # Inherit neither Notion access nor stdio from the polling/actor process.
    return subprocess.Popen([sys.executable,'-B','-m','riri.local_tasks',*args], cwd=root,
                            env=child_env(), start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True).pid


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalTasks:
    def __init__(self,cfg, *, mailbox=None, spawn=clean_subprocess, available=heavy_free):
        self.cfg, self.root = cfg,cfg.root
        self.base = cfg.root / 'state' / 'local-tasks'
        self.mail = mailbox or Mailbox(cfg)
        self.spawn, self.available = spawn, available

    def setup(self):
        require(not self.base.parent.is_symlink() and not self.base.is_symlink(), 'invalid task folder')
        self.base.mkdir(mode=0o700,parents=True,exist_ok=True)
        require(self.base.stat().st_uid == os.getuid() and not (self.base.stat().st_mode & 0o077), 'task folder is not private')

    def check_live(self):
        require(self.cfg.mode == 'live','local route disabled in practice')
        if self.cfg.stop.exists() or (self.root/'STOP').exists():
            raise StopRequested('stopped')

    def path(self,run):
        require(isinstance(run,str) and RUN.fullmatch(run), 'invalid run')
        return self.base / (run+'.job.json')

    def load(self,run):
        return read_json(self.path(run))

    def rows(self):
        return sorted((read_json(p) for p in self.base.glob('*.job.json')),key=lambda j:j['queued_at'])

    def save(self,j):
        atomic_json(self.path(j['run_id']), j)

    @staticmethod
    def public(j):
        keys = (*IDENTITY,'request_receipt','execution_state','resource_state','report_state','receipt','queued_at','last_issue','driver_pid','child_pid','child_exit','started_at','ended_at','stage','quality','cancel_requested_at','stop_reason','continuation')
        result = {k:j[k] for k in keys if k in j}
        if j.get('amends'):
            v=j['amends']
            result['revision']={'version':2,'previous_run':v['run_id'],'previous_report_sha256':v['receipt']['report_sha256'],
                                'review_run':v['review_run'],'judgment_sha256':v['judgment_sha256']}
        if j.get('outbox'):
            result.update({k:j['outbox'][k] for k in ('artifact_sha256','result_sha256')})
        return result

    def stage(self,payload):
        self.check_live(); self.setup()
        job={k:payload[k] for k in IDENTITY}
        check_job(job)
        staged=self.base/(job['run_id']+'.staged.json')
        with exclusive(self.base/'journal.lock'):
            if staged.exists():
                require(read_json(staged)==job,'staged job collision')
            else:
                atomic_json(staged,job)
        return {'staged':True,'run_id':job['run_id'],'worker_started':False}

    def register(self, payload, *, requests=None):
        self.check_live(); self.setup()
        job = {k:payload[k] for k in IDENTITY}
        check_job(job)
        if job['actor'] == 'miki':
            require(job['spec_sha256'] == 'cf4c8221919b2c066fe29fe44751efcf2e1c7591fa86ffe289c95f31770fb864','wrong A specification')
        receipt = self.mail.intake(job) if requests is None else self.mail.intake_from(requests,job,parsed=True)
        with exclusive(self.base/'journal.lock'):
            if self.path(job['run_id']).exists():
                old = self.load(job['run_id'])
                require(all(old[k] == v for k,v in job.items()) and all(receipt[k] == old['request_receipt'][k] for k in ('block_id','request_sha256')), 'registered job collision')
                return self.public(old)
            require(receipt.get('source_status') != '完了', 'completed formal request cannot be registered')
            require(not any(j['request_id'] == job['request_id'] for j in self.rows()),'request ID already in use')
            if job['prior_run']:
                require(not (self.path(REVISION).exists() and self.load(REVISION).get('report_state')=='received'),
                        'revised candidate requires explicit same-request inspection')
                prev=self.load(job['prior_run'])
                require(prev.get('stage')=='inspection_wait' and prev['spec_sha256']==job['spec_sha256'] and prev.get('outbox',{}).get('artifact_sha256')==job['artifact_sha256'], 'upstream artifact mismatch')
            job.update(queued_at=now(),request_receipt=receipt,execution_state='queued',resource_state='idle',report_state='not_due',stage='requested',quality='unassessed')
            self.save(job)
            return self.public(job)

    def continuation_proof(self, parent, manifest_sha):
        """Verify the one reviewed interruption and deployed host without loading a model."""
        require(parent['actor']=='miki' and parent['request_id']==REQUEST and parent['run_id']==ORIGINAL
                and parent.get('execution_state')=='reconcile' and parent.get('resource_state')=='released'
                and parent.get('stop_reason')=='cancelled' and parent.get('child_exit')==-signal.SIGINT
                and parent.get('report_state')=='not_due' and not parent.get('outbox') and not parent.get('receipt')
                and parent.get('cancel_requested_at') and parent.get('ended_at'), 'original result not reconciled')
        require(all(type(parent.get(k)) is int and not alive(parent[k]) for k in ('child_pid','driver_pid'))
                and self.available(), 'original owner or heavyweight resource busy')
        require(digest(self.path(ORIGINAL).read_bytes())==ORIGINAL_LEDGER
                and (self.base/(ORIGINAL+'.actor-output')).read_bytes()==b'', 'original Riri evidence changed')
        base=MIKI/'data/development/gemma26'
        jobfile=base/'jobs'/(ORIGINAL+'.json')
        journalfile=base/'journal'/(ORIGINAL+'.json')
        modeldir=base/'model-runs'/ORIGINAL
        paths=[jobfile,journalfile,*modeldir.iterdir()]
        require(len(paths)==30 and all(f.resolve()==f and f.is_file() and f.stat().st_size<=2**21 for f in paths),
                'invalid original actor evidence')
        hashes={f.relative_to(base).as_posix():digest(f.read_bytes()) for f in paths}
        combined=digest(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode())
        require(combined==ORIGINAL_EVIDENCE, 'original actor evidence changed')
        worker_pids={json.loads(f.read_bytes())['worker_pid'] for f in modeldir.glob('*.generation.json')}
        require(all(type(pid) is int and pid>0 and not alive(pid) for pid in worker_pids),'original worker PID still present')
        actor=json.loads(jobfile.read_bytes())
        journal=json.loads(journalfile.read_bytes())
        for f,expected in journal['current_hashes'].items():
            target=Path(actor['repo_root'])/f
            require(target.resolve()==target and digest(target.read_bytes())==expected,'A starting copy changed')
        for dirname in ('jobs','journal','model-runs'):
            target=base/dirname/(CONTINUED if dirname=='model-runs' else CONTINUED+'.json')
            require(not target.exists() and not target.is_symlink(),'continuation outcome already exists')
        manifest_file=MIKI/'install-manifest.json'
        manifest=manifest_file.read_bytes()
        require(isinstance(manifest_sha,str) and HASH.fullmatch(manifest_sha) and digest(manifest)==manifest_sha,
                'Miki deployment changed')
        files=json.loads(manifest)['files']
        for rel,expected in files.items():
            path=MIKI/rel
            require(not Path(rel).is_absolute() and '..' not in Path(rel).parts and path.resolve()==path
                    and path.is_file() and digest(path.read_bytes())==expected,'Miki deployment hash mismatch')
        entry=(MIKI/'miki/cli.py').read_text()
        require('--continue-from' in entry and files['miki/gemma_development.py']!=actor['binding']['code_sha256']['gemma_development.py'],
                'continuation host not deployed')
        version=re.search(r'^__version__ = "([^"]+)"$',(MIKI/'miki/__init__.py').read_text(),re.M)
        require(version is not None,'Miki version missing')
        return {'parent_run':ORIGINAL,'parent_ledger_sha256':ORIGINAL_LEDGER,'parent_actor_evidence_sha256':combined,
                'parent_actor_output_sha256':digest(b''),'calls_consumed':10,'calls_remaining':2,'calls_total':12,
                'actor_manifest_sha256':manifest_sha,'actor_version':version[1],'parent_actor_files':len(hashes)}

    def continue_a(self, payload):
        self.check_live(); self.setup()
        require(payload['request_id']==REQUEST and payload['run_id']==CONTINUED and payload['prior_run']==ORIGINAL,
                'unsupported continuation')
        manifest_sha=payload['actor_manifest_sha256']
        def registered():
            saved=self.load(CONTINUED)
            require(saved['continuation']['actor_manifest_sha256']==manifest_sha, 'continuation deployment collision')
            return self.public(saved)

        with exclusive(self.base/'journal.lock'):
            if self.path(CONTINUED).exists():
                return registered()
        parent=self.load(ORIGINAL)
        receipt=self.mail.intake(parent)
        require(receipt['source_status']!='完了' and all(receipt[k]==parent['request_receipt'][k]
                for k in ('block_id','request_sha256')), 'formal source changed or already closed')
        with exclusive(self.base/'journal.lock'):
            if self.path(CONTINUED).exists():
                return registered()
            with exclusive(self.base/'actor.lock'):
                proof=self.continuation_proof(self.load(ORIGINAL),manifest_sha)
                require(not self.mail.find_report(parent,''), 'original formal report already exists')
                require(not (self.base/(CONTINUED+'.actor-output')).exists(), 'continuation output already exists')
                job={k:parent[k] for k in IDENTITY}
                job.update(run_id=CONTINUED,prior_run=ORIGINAL,continuation=proof,request_receipt=receipt,
                           queued_at=now(),execution_state='queued',resource_state='idle',report_state='not_due',
                           stage='requested',quality='unassessed')
                self.save(job)
                return self.public(job)

    def status(self,run,rid):
        j=self.load(run)
        require(j['request_id']==rid,'request ID mismatch')
        return self.public(j)

    def cancel(self,run,rid):
        self.setup()
        with exclusive(self.base/'journal.lock'):
            j=self.load(run)
            require(j['request_id']==rid,'request ID mismatch')
            require(run!=ORIGINAL or not self.path(CONTINUED).exists(),'original archived by linked continuation')
            if j['execution_state'] in ('queued','launch_pending'):
                j.update(execution_state='cancelled',resource_state='idle',ended_at=now())
            elif j['execution_state']=='executing':
                j.setdefault('cancel_requested_at',now())
            self.save(j)
            return self.public(j)

    def wait_actor(self,proc,run,seconds=1800):
        # The actor CLI owns its detached model worker and performs its normal
        # cancellation cleanup on interruption. Never signal a lock file PID.
        limit=time.monotonic()+seconds
        while True:
            try:
                return proc.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                pass
            cancelled=self.load(run).get('cancel_requested_at')
            if cancelled or time.monotonic()>=limit or self.cfg.stop.exists():
                with exclusive(self.base/'journal.lock',wait=True):
                    row=self.load(run)
                    row['stop_reason']='cancelled' if cancelled else ('STOP' if self.cfg.stop.exists() else 'actor_timeout')
                    self.save(row)
                # Popen has not been reaped, so this pid cannot be recycled.
                # Use the CLI's own SIGINT/finally first. Any orphaned model
                # continues to hold the real OS lock until it releases or exits.
                proc.send_signal(signal.SIGINT)
                try:
                    return proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        return proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        return proc.wait(timeout=5)

    def report(self,payload):
        self.setup()
        run=payload['run_id']
        with exclusive(self.base/'journal.lock',wait=True):
            j=self.load(run)
            require(run!=ORIGINAL or not self.path(CONTINUED).exists(),'original archived by linked continuation')
            require(j['execution_state'] in ('executing','finished','reconcile','report_pending'),'no actor claim')
            body=make_report(j,payload,self.cfg.secrets)
            packet={k:payload[k] for k in ('artifact_sha256','result_sha256','summary','stage','quality')}
            if 'outbox' in j:
                require(j['outbox']==packet and j['report_sha256']==sha(body),'report collision')
            else:
                j.update(outbox=packet,report_sha256=sha(body),report_state='pending',stage=payload['stage'],quality=payload['quality'])
                self.save(j)  # Durable before any access to Notion.
        return self.deliver(run)

    def deliver(self,run):
        # This per-run lock spans read/append/readback, but never guards GPU use.
        with exclusive(self.base/(run+'.report.lock')):
            with exclusive(self.base/'journal.lock'):
                j=self.load(run)
                if j.get('report_state')=='received':
                    return self.public(j)
                require('outbox' in j,'no pending report')
                packet={**j['outbox'], **{k:j[k] for k in ('actor','request_id','run_id')}}
                body=make_report(j,packet,self.cfg.secrets)
                require(sha(body)==j['report_sha256'],'outbox changed')
                j['last_report_attempt_at']=now();self.save(j)
            try:
                self.check_live()
                if j.get('amends'):
                    from .local_revisions import deliver_revision
                    remote=deliver_revision(self,j,body)
                else:
                    remote=self.mail.find_report(j,body)
                if remote is None and j['report_state']=='pending' and not j.get('amends'):
                    with exclusive(self.base/'journal.lock'):
                        j=self.load(run);j['report_state']='unknown';self.save(j)
                    # If this process dies during append, subsequent attempts only read.
                    try:
                        self.mail.append_report(j,body)
                    except PatrolError as exc:
                        if (isinstance(exc,RemoteError) and not exc.uncertain) or getattr(exc,'not_sent',False):
                            with exclusive(self.base/'journal.lock'):
                                j=self.load(run);j['report_state']='pending';self.save(j)
                        raise
                    remote=self.mail.find_report(j,body)
                if remote:
                    with exclusive(self.base/'journal.lock'):
                        j=self.load(run)
                        require(remote['report_sha256']==j['report_sha256'],'readback mismatch')
                        j.update(report_state='received',receipt={**remote,'request_id':j['request_id'],'run_id':run,
                                 'actor':j['actor'],'artifact_sha256':packet['artifact_sha256'],'result_sha256':packet['result_sha256'],
                                 'received_at':now(),'receiver':'riri','source':'formal_page_full_readback'})
                        if j.get('amends'):
                            j['receipt'].update(version=2,previous_run=j['amends']['run_id'],previous_report_sha256=j['amends']['receipt']['report_sha256'])
                        if (j['execution_state']=='reconcile' and not alive(j.get('driver_pid')) and not alive(j.get('child_pid'))
                                and self.available()):
                            j.update(execution_state='finished',resource_state='released',release_observed_at=now())
                        self.save(j)
            except (PatrolError,OSError,ValueError) as exc:
                with exclusive(self.base/'journal.lock'):
                    j=self.load(run); j['last_issue']=type(exc).__name__;self.save(j)
            return self.public(self.load(run))

    def runnable(self,j):
        if j['execution_state'] not in ('queued','launch_pending'):
            return False
        if j['run_id']==CONTINUED:
            return j['prior_run']==ORIGINAL and j.get('continuation',{}).get('calls_remaining')==2
        if j['prior_run']:
            if j['actor']=='mamu' and j['prior_run']!=REVISION and self.path(REVISION).exists() and self.load(REVISION).get('report_state')=='received':
                return False
            prev=self.load(j['prior_run'])
            if not (prev.get('stage')=='inspection_wait' and prev.get('resource_state')=='released' and prev.get('report_state')=='received'
                    and prev['receipt']['artifact_sha256']==j['artifact_sha256']):
                return False
        return True

    def tick(self):
        self.check_live()
        if not self.base.exists():
            return {'selected_run':None}
        try:
            with exclusive(self.base/'tick.lock'):
                jobs=self.rows()
                pending=sorted((j for j in jobs if j['report_state'] in ('pending','unknown')),key=lambda j:j.get('last_report_attempt_at',''))
                if pending:
                    try:
                        self.deliver(pending[0]['run_id'])
                    except BlockingIOError:
                        pass
                with exclusive(self.base/'journal.lock'):
                    jobs=self.rows()
                    if any(j['execution_state']=='executing' and (alive(j.get('driver_pid')) or alive(j.get('child_pid'))) for j in jobs):
                        return {'selected_run':None,'reason':'owned_execution_active'}
                    if not self.available():
                        for j in jobs:
                            if j['execution_state'] in ('queued','launch_pending'):
                                j.update(resource_state='waiting',last_issue='heavy_model_busy');self.save(j)
                        return {'selected_run':None,'reason':'heavy_model_busy'}
                    try:
                        with exclusive(self.base/'actor.lock'):
                            pass
                    except BlockingIOError:
                        return {'selected_run':None,'reason':'actor_lease_owned'}
                    for j in jobs:
                        if j['execution_state']=='finished' and j['resource_state']=='release_unconfirmed':
                            j.update(resource_state='released',release_observed_at=now());self.save(j)
                        if j['execution_state']=='executing':
                            # Unknown outcome is never a reason to generate again.
                            j.update(execution_state='finished' if j.get('outbox') else 'reconcile',resource_state='released',last_issue='driver_exited_reconciled',ended_at=now())
                            self.save(j)
                    for j in self.rows():
                        if not self.runnable(j):
                            if j['execution_state']=='queued':
                                j.update(last_issue='waiting_for_prior_report_or_release');self.save(j)
                            continue
                        j.update(execution_state='launch_pending',last_launch_attempt_at=now())
                        self.save(j)
                        pid=self.spawn(self.root,['--execute',j['run_id']])
                        return {'selected_run':j['run_id'],'driver_pid':pid}
                return {'selected_run':None}
        except BlockingIOError:
            return {'selected_run':None,'reason':'already_checking'}

    def command(self,j):
        if j['actor']=='miki':
            cmd=[str(MIKI/'start-miki.sh'),'gemma-develop','A','--request-id',j['request_id']]
            if j['run_id']==CONTINUED:
                require(self.continuation_proof(self.load(ORIGINAL),j['continuation']['actor_manifest_sha256'])==j['continuation'],
                        'continuation evidence changed before launch')
                cmd.extend(['--continue-from',ORIGINAL])
            return cmd
        d=MAMU/'state'/'inspections'/'jobs'/j['run_id']; f=d/'job.json'
        require(f.resolve()==f and f.is_file() and f.stat().st_size<65536,'inspection job unavailable')
        metadata=json.loads(f.read_bytes())
        required={'run_id':j['run_id'],'request_id':j['request_id'],'prior_run_id':j['prior_run'],'artifact_sha256':j['artifact_sha256'],'spec_sha256':j['spec_sha256'],'mode':'inspect_miki_A'}
        require(all(metadata.get(k)==v for k,v in required.items()),'inspection job mismatch')
        return [str(MAMU/'start-mamu.sh'),'inspect-job','--job',str(f)]

    def execute(self,run):
        self.check_live()
        # The lease is also inherited by the actor's CLI. Orphaned live callers
        # cannot overlap a following launch even if their driver disappears.
        with exclusive(self.base/'actor.lock') as actor_lease:
            with exclusive(self.base/'journal.lock'):
                j=self.load(run)
                require(self.runnable(j) and j['execution_state']=='launch_pending','job already claimed')
                if not self.available():
                    j.update(execution_state='queued',resource_state='waiting',last_issue='heavy_model_busy')
                    self.save(j);return self.public(j)
                try:
                    cmd=self.command(j)
                    self.check_live()
                except (SafetyError,KeyError,ValueError,OSError):
                    j.update(execution_state='reconcile',resource_state='idle',last_issue='job_binding_unavailable');self.save(j);raise
                j.update(execution_state='executing',resource_state='owned',driver_pid=os.getpid(),started_at=now())
                self.save(j)
            out=self.base/(run+'.actor-output')
            try:
                fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as sink:
                    proc=subprocess.Popen(cmd,cwd=Path(cmd[0]).parent,stdin=subprocess.DEVNULL,stdout=sink,stderr=subprocess.DEVNULL,
                                          env=child_env(),close_fds=True,pass_fds=(actor_lease,),start_new_session=True)
                    with exclusive(self.base/'journal.lock',wait=True):
                        j=self.load(run);j['child_pid']=proc.pid;self.save(j)
                    code=self.wait_actor(proc,run)
                    sink.flush();os.fsync(sink.fileno())
            except Exception:
                # If spawn outcome is uncertain, another driver must reconcile.
                with exclusive(self.base/'journal.lock',wait=True):
                    j=self.load(run);j['last_issue']='actor_exception';self.save(j)
                raise
            with exclusive(self.base/'journal.lock',wait=True):
                j=self.load(run)
                j.update(execution_state='finished' if j.get('outbox') else 'reconcile',
                         resource_state='released' if self.available() else 'release_unconfirmed',
                         ended_at=now(),child_exit=code)
                if not j.get('outbox'):
                    j['last_issue']='actor_returned_without_result_receipt'
                self.save(j);return self.public(j)


def kick_if_pending(cfg,requests=()):
    """Opt-in for this exact project. Do not dispatch generic or historical mail."""
    base=cfg.root/'state'/'local-tasks'
    if cfg.mode!='live' or cfg.stop.exists() or not base.is_dir() or base.is_symlink():
        return
    svc=LocalTasks(cfg)
    for req in requests:
        if req.status not in ('未着手','対応中') or req.fields.get('案件ページ')!=PROJECT:
            continue
        marked=re.findall(r'(?<![\w-])local-run=([\w-]+)',req.text)
        if len(marked)!=1 or not RUN.fullmatch(marked[0]):
            continue
        run=marked[0]
        if run in (CONTINUED,REVISION) or svc.path(run).exists():
            continue
        try:
            if run=='gemma26-A-v1':
                # Owner explicitly staged this finite implementation. Shipping
                # software alone is never a command to process an older request.
                stage=base/(run+'.staged.json')
                if not stage.is_file():
                    continue
                job=read_json(stage)
            else:
                # The inspection preparer pins the target/spec before publishing
                # this fresh local-run marker. No Notion text can select a path.
                d=MAMU/'state'/'inspections'/'jobs'/run;f=d/'job.json'
                if not f.is_file() or f.resolve()!=f or f.stat().st_size>65536:
                    continue
                raw=json.loads(f.read_bytes())
                job={'actor':'mamu','run_id':run,'request_id':raw['request_id'],'prior_run':raw['prior_run_id'],
                     'artifact_sha256':raw['artifact_sha256'],'spec_sha256':raw['spec_sha256']}
                svc.command(job)
            if job['request_id']==req.id:
                svc.register(job,requests=requests)
        except (OSError,KeyError,ValueError,PatrolError):
            # Leave unready work for the next normal poll; continue with other jobs.
            continue
    rows=svc.rows()
    if any(j['execution_state'] in ('queued','launch_pending','executing') or j['report_state'] in ('pending','unknown') for j in rows):
        clean_subprocess(cfg.root,['--tick'])


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    try:
        cfg=Config.load(ROOT)
        svc=LocalTasks(cfg)
        if argv==['--tick']:
            result=svc.tick()
        elif len(argv)==2 and argv[0]=='--execute':
            result=svc.execute(argv[1])
        else:
            require(not argv,'unexpected command arguments')
            raw=sys.stdin.buffer.read(16385)
            require(len(raw)<=16384,'local request too large')
            data=json.loads(raw)
            op=data['op']
            if op in ('prepare-amendment','amend','review-version'):
                from .local_revisions import register_amendment
                result=register_amendment(svc,data)
            elif op=='continue': result=svc.continue_a(data)
            elif op=='stage': result=svc.stage(data)
            elif op=='register': result=svc.register(data)
            elif op=='report': result=svc.report(data)
            elif op=='cancel': result=svc.cancel(data['run_id'],data['request_id'])
            elif op=='status': result=svc.status(data['run_id'],data['request_id'])
            elif op=='tick': result=svc.tick()
            else: raise SafetyError('unsupported operation')
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'reason':type(exc).__name__}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
