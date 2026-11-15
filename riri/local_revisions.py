"""One post-inspection A amendment. Preparing a correction never queues model work."""
from datetime import datetime
import json
from pathlib import Path
import re
import os
import shlex
import subprocess
from . import compose
from .errors import PatrolError, RemoteError
from .local_outcomes import atomic_json, digest, read_json
from .local_tasks import (CONTINUED, HASH, IDENTITY, MAMU, MIKI, REQUEST, REVISION, RUN, alive,
                          exclusive, make_report, now, require, sha)

WORKSPACE=Path('/Users/sunako.jp/Documents/Codex/2026-11-14/files-pasted-by-the-user-notion')
DIAGNOSTIC=WORKSPACE/'outputs/20261115T1933_JST_miki_mamu_3_4/A-frozen-owner-diagnostics.json'
COPY=WORKSPACE/'work/gemma26-development/copies/A-rev1'
QUALITY=WORKSPACE/'outputs/20261115T2104_JST_A_revision/A-quality.json'
TARGET='qbench/input_validation.py'
MODEL_REV='0e3cbab38ce568cf6e23543010d08d03b731910c'
MODEL='mlx-community/gemma-4-26B-A4B-it-qat-4bit'


def evidence(path, expected=None):
    require(path.resolve()==path and path.is_file() and path.stat().st_size<=4*1024**2,'revision evidence missing or unsafe')
    raw=path.read_bytes()
    if expected is not None:
        require(isinstance(expected,str) and HASH.fullmatch(expected) and digest(raw)==expected,'revision evidence hash mismatch')
    return raw


def verified(row):
    ack=row.get('receipt',{})
    require(row.get('execution_state')=='finished' and row.get('resource_state')=='released'
            and row.get('report_state')=='received' and ack.get('receiver')=='riri'
            and ack.get('source')=='formal_page_full_readback'
            and all(ack[k]==row[k] for k in ('actor','run_id','request_id'))
            and ack['report_sha256']==row['report_sha256']
            and all(ack[k]==row['outbox'][k] for k in ('artifact_sha256','result_sha256')),
            'verified upstream receipt required')
    return ack


def archive(svc,row):
    ack=verified(row)
    remote=svc.mail.current_report(row)
    require(remote and remote['type']=='code' and remote['version'] and sha(remote['text'])==ack['report_sha256']
            and all(remote[k]==ack[k] for k in ('block_id','report_sha256','report_page_id')),
            'old report or block no longer matches receipt')
    return {'run_id':row['run_id'],'receipt':ack,'body':remote['text'],'version':remote['version']}


def correction_path(svc):
    return svc.base/(REVISION+'.amend-intent.json')


def prepare(svc,data):
    svc.check_live()
    svc.setup()
    require(data['run_id']==REVISION and data['prior_run']==CONTINUED and data['request_id']==REQUEST,'wrong A correction')
    review_id=data['review_run']
    require(isinstance(review_id,str) and re.fullmatch('mamu-a-review-[a-f0-9]{32}',review_id),'invalid original review')
    requested={k:data[k] for k in ('run_id','prior_run','request_id','review_run','diagnostic_sha256')}
    path=correction_path(svc)

    def compact(saved):
        require(all(saved[k]==v for k,v in requested.items()),'correction identity collision')
        return {**requested,'state':saved['state'],'recorded_at':saved['recorded_at'],'worker_started':False,
                'notion_write':False,'mismatch_cases':saved['mismatch_cases']}

    with exclusive(svc.base/'journal.lock'):
        if path.exists():
            return compact(read_json(path))
    a=svc.load(CONTINUED)
    review=svc.load(review_id)
    ack=verified(a)
    checked=verified(review)
    require(ack['actor']=='miki' and ack['request_id']==REQUEST and a['stage']=='inspection_wait'
            and review['actor']=='mamu' and review['stage'] in ('reviewed','inspection_completed')
            and review['prior_run']==CONTINUED and a['spec_sha256']==review['spec_sha256']
            and ack['artifact_sha256']==checked['artifact_sha256'],'wrong reviewed version')
    diagnostic=json.loads(evidence(DIAGNOSTIC,data['diagnostic_sha256']))
    require(diagnostic['source_sha256']==ack['artifact_sha256'] and diagnostic['spec_sha256']==a['spec_sha256']
            and diagnostic['first_opinion_run']==review_id and diagnostic['first_opinion_result_sha256']==checked['result_sha256']
            and datetime.fromisoformat(diagnostic['recorded_at'])>=datetime.fromisoformat(checked['received_at']),
            'post-inspection host diagnosis does not match first review')
    cases=[case['case'] for case in diagnostic['tested_cases'] if case['matches'] is False and case['expected']!=case['observed']]
    require(0<len(cases)<=64 and all(isinstance(name,str) and re.fullmatch('[a-z0-9_]{1,80}',name) for name in cases),'no confirmed diagnostic mismatch')
    frozen={**requested,'state':'awaiting_new_budget','recorded_at':now(),'spec_sha256':a['spec_sha256'],
            'mismatch_cases':cases,'evidence_role':'post_model_host_assessment','miki':archive(svc,a),'mamu':archive(svc,review)}
    with exclusive(svc.base/'journal.lock'):
        if not path.exists():
            atomic_json(path,frozen)
        return compact(read_json(path))


def judgment(frozen, judgment_sha):
    base=MIKI/'data/development/gemma26'
    decision=json.loads(evidence(base/'judgments'/(REVISION+'.json'),judgment_sha))
    for key in ('run_id','prior_run','request_id','review_run','diagnostic_sha256','spec_sha256'):
        require(decision.get(key)==frozen[key],'judgment is for a different request or evidence')
    before=frozen['miki']['receipt']
    require(decision['prior_artifact_sha256']==before['artifact_sha256'] and decision['prior_result_sha256']==before['result_sha256']
            and decision['review_result_sha256']==frozen['mamu']['receipt']['result_sha256'] and decision['decision']=='revision_required'
            and decision['new_budget_approved'] is True,'judgment or new budget unconfirmed')
    reason=decision['reason']
    require(isinstance(reason,str) and reason.strip() and len(reason)<=500 and len(reason.splitlines())==1,'invalid revision reason')
    return decision


def actor_identity(frozen, judgment_sha):
    base=MIKI/'data/development/gemma26'
    decision=judgment(frozen,judgment_sha)
    raw=evidence(base/'jobs'/(REVISION+'.json'));job=json.loads(raw)
    require(job['run_id']==job['formal_run_id']==REVISION and job['request_id']==REQUEST and job['prior_run']==CONTINUED
            and job['label']=='A' and job['mode']=='implementation' and job['repo_id']==MODEL and job['revision']==MODEL_REV
            and job.get('revision_judgment_sha256')==judgment_sha and Path(job['repo_root'])==COPY,'wrong revision actor')
    start=datetime.fromisoformat(job['current_started_at'])
    require(datetime.fromisoformat(frozen['recorded_at'])<=datetime.fromisoformat(decision['budget_start'])<=start<
            datetime.fromisoformat(decision['budget_deadline']),'job started outside new budget')
    return base,decision,raw,job


def checked_time(value,start,end):
    return datetime.fromisoformat(start)<=datetime.fromisoformat(value)<=datetime.fromisoformat(end)


def owned_worker(pid,parent,path):
    if type(pid) is not int or type(parent) is not int or pid<=0 or parent<=0:
        return False
    try:
        proc=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=','-o','ppid=','-o','command='],
                            capture_output=True,text=True,timeout=2)
        uid,ppid,command=proc.stdout.strip().split(None,2)
        return int(uid)==os.getuid() and int(ppid)==parent and str(path) in shlex.split(command) and alive(parent)
    except (OSError,ValueError,subprocess.SubprocessError):
        return False


def fixed_counts(box):
    scores=[]
    for key in ('fixed_checks','additional_checks'):
        value=box.get(key,{})
        scores.append({k:value[k] for k in ('passed','total') if type(value.get(k)) is int})
    return {'fixed_checks':scores[0],'additional_checks':scores[1]}


def durable_outbox(base,mid,packet_hash):
    require(isinstance(mid,str) and re.fullmatch(re.escape(REVISION)+r'-turn-01-[0-9a-f]{20}',mid),'invalid revision model turn')
    folder=base/'model-runs'/REVISION
    box=json.loads(evidence(folder/(mid+'.outbox.json'),packet_hash))
    return folder,box


def before_release(svc,frozen,data):
    base,decision,raw,job=actor_identity(frozen,data['judgment_sha256'])
    mid=data['model_run_id'];folder,box=durable_outbox(base,mid,data['actor_outbox_sha256'])
    require(box['job_run_id']==REVISION and box['request_id']==REQUEST and box['model_run_id']==mid
            and box['judgment_sha256']==data['judgment_sha256'] and box['expected_report_sha256']==data['expected_report_sha256'],
            'actor durable outbox differs')
    intent=json.loads(evidence(folder/(mid+'.intent.json')))
    gen=json.loads(evidence(folder/(mid+'.generation.json'),box['result_sha256']))
    require(job['terminal'] is False and intent['stage']=='source' and intent['job_run_id']==intent['formal_run_id']==REVISION
            and intent['request_id']==REQUEST and intent['model_run_id']==mid and intent['revision_judgment_sha256']==data['judgment_sha256']
            and gen['run_id']==mid and gen['job_run_id']==REVISION and gen['repo_id']==MODEL and gen['model_revision']==MODEL_REV
            and gen['profile_id']=='gemma-4-26b-a4b-it-qat-4bit-development' and gen['worker_pid']==box['worker_pid']
            and box['version']==2 and box['prior_run']==CONTINUED and box['review_run']==frozen['review_run']
            and box['prior_artifact_sha256']==frozen['miki']['receipt']['artifact_sha256']
            and intent.get('budget_deadline')==box['budget_deadline']==decision['budget_deadline']
            and isinstance(gen['text'],str) and isinstance(gen['truncated'],bool)
            and checked_time(intent['started_at'],job['current_started_at'],gen['recorded_at'])
            and checked_time(gen['recorded_at'],intent['started_at'],decision['budget_deadline'])
            and datetime.fromisoformat(box['recorded_at'])>=datetime.fromisoformat(gen['recorded_at']),
            'actor generation or authorized interval differs')
    require(owned_worker(gen['worker_pid'],intent['cli_pid'],MIKI/'miki/model_worker.py') and not svc.available(),
            'pre-release worker and lock not independently observed')
    journal=json.loads(evidence(base/'journal'/(REVISION+'.json')))
    target=COPY/TARGET;evidence(target,box['artifact_sha256'])
    old=frozen['miki']['receipt']['artifact_sha256']
    applied=any(e['action'].get('action')=='apply' and e['provenance'].get('run_id')==mid
                and e['provenance'].get('actor')=='miki_full_source' and e['response_sha256']==box['result_sha256']
                and e['result'].get('sha256',{}).get(TARGET)==box['artifact_sha256'] for e in journal['events'])
    require(journal['run_id']==REVISION and journal['mock'] is False and journal['pending'] is None
            and journal['current_hashes'][TARGET]==box['artifact_sha256'],'pending or changed actor mutation')
    if box['stage']=='inspection_wait':
        require(applied and box['artifact_sha256']!=old and gen['truncated'] is False
                and box['quality'] in ('partial','pending','failed'),'changed actual candidate required')
        add=box['additional_checks'];fixed=box['fixed_checks']
        accepted=(fixed.get('passed')==fixed.get('total')==37 and add.get('ok') is True and
                  add.get('passed')==add.get('total')==7 and add.get('artifact_sha256')==box['artifact_sha256'])
        require(box['quality_gate_ok'] is accepted and (box['quality']=='pending') is accepted,'claimed fixed evidence differs')
        if box['quality']=='pending':
            require(any(e['action'].get('action')=='finish' and e['provenance'].get('run_id')==mid
                        and e['result'].get('oracle',{}).get('ok') is True for e in journal['events']),'fixed checks unverified')
    else:
        require(box['stage']=='implementation_incomplete' and box['quality'] in ('failed','unverified')
                and not applied and box['artifact_sha256']==old,'unverified candidate failure')
    return {**fixed_counts(box),**{k:box[k] for k in ('stage','quality','artifact_sha256','result_sha256','worker_pid')},'actor_cli_pid':intent['cli_pid'],
            'actor_job_sha256':digest(raw),'reason':decision['reason'],'actor_outbox_sha256':data['actor_outbox_sha256'],
            'model_run_id':mid,'source_recorded_at':gen['recorded_at']}


def terminal_failure(frozen,data):
    base,decision,raw,job=actor_identity(frozen,data['judgment_sha256'])
    require(job['terminal'] is True and job['model_patch_count']==0 and not job['artifacts']
            and job['final_hashes'][TARGET]==frozen['miki']['receipt']['artifact_sha256'],'failure not terminal or candidate changed')
    target=job['final_hashes'][TARGET];evidence(COPY/TARGET,target)
    folder=base/'model-runs'/REVISION;gens=list(folder.glob('*.generation.json'))
    detail=evidence(base/'jobs'/(REVISION+'.failure.json'));failure=json.loads(detail)
    require(failure['run_id']==REVISION and failure['request_id']==REQUEST and failure['judgment_sha256']==data['judgment_sha256']
            and failure['outcome']=='confirmed_failure' and failure['artifact_sha256']==target and failure['reason']
            and type(failure['worker_started']) is bool,'failure evidence differs')
    require(not gens,'saved generation must be used as result evidence')
    mid=failure['model_run_id'];pid=failure['worker_pid']
    if failure['worker_started']:
        require(isinstance(mid,str) and re.fullmatch(re.escape(REVISION)+r'-turn-01-[0-9a-f]{20}',mid) and type(pid) is int and pid>0 and not alive(pid),
                'failure worker release unknown')
        result=json.loads(evidence(folder/(mid+'.result.json')))
        require(result.get('ok') is False and result.get('stats',{}).get('worker_exited') is True
                and result['stats'].get('worker_pid')==pid,'failed worker identity not resolved')
    else:
        require(pid is None and (mid is None or re.fullmatch(re.escape(REVISION)+r'-turn-01-[0-9a-f]{20}',mid)),'workerless failure PID mismatch')
    if mid is None:
        require(not job['model_runs'] and failure['reason']==job.get('error') and failure['reason'] in ('resource_or_lock_blocked','budget_exhausted'),
                'unresolved attempt cannot be reported as unstarted')
    else:
        entry,=job['model_runs']
        rp=folder/(mid+'.result.json');result=json.loads(evidence(rp,entry['result_sha256']))
        require(entry['model_run_id']==mid and result.get('ok') is False and result.get('error_code')==job.get('error')==failure['reason']
                and (failure['worker_started'] or result.get('stats',{}).get('worker_started') is False),'unconfirmed failed execution')
    return {'stage':'implementation_incomplete','quality':'unverified','artifact_sha256':target,'result_sha256':digest(detail),
            'reason':decision['reason'],'actor_job_sha256':digest(raw),'model_run_id':mid,'worker_pid':pid}


def check_actor(frozen, judgment_sha):
    """Accept only the published, linked result of a separately approved actor run."""
    base,decision,raw,job=actor_identity(frozen,judgment_sha)
    before=frozen['miki']['receipt'];reason=decision['reason']
    require(job['run_id']==job['formal_run_id']==REVISION and job['request_id']==REQUEST and job['prior_run']==CONTINUED
            and job['label']=='A' and job['mode']=='implementation' and job['repo_id']==MODEL and job['revision']==MODEL_REV
            and job.get('revision_judgment_sha256')==judgment_sha and job['terminal'] is True and job['human_code_fixes']==0
            and job['model_patch_count']>0 and Path(job['repo_root'])==COPY,'linked actual actor job required')
    start,end=map(datetime.fromisoformat,(job['current_started_at'],job['ended_at']))
    require(start<=end,'actor time reversed')
    art,=job['artifacts']
    target=COPY/'qbench/input_validation.py'
    require(art['path']==str(target) and art['sha256']!=before['artifact_sha256']
            and art['sha256']==job['final_hashes']['qbench/input_validation.py'],'unchanged or unbound artifact')
    evidence(target,art['sha256'])
    matched=[]
    folder=base/'model-runs'/REVISION
    for entry in job['model_runs']:
        mid=entry['model_run_id']
        require(re.fullmatch(re.escape(REVISION)+r'-turn-[0-9]{2}-[a-f0-9]{20}',mid),'invalid actor turn')
        rp=folder/(mid+'.result.json');gp=folder/(mid+'.generation.json')
        require(entry['result']==str(rp) and entry['generation']==str(gp),'actor evidence path changed')
        result=json.loads(evidence(rp,entry['result_sha256']))
        saved=json.loads(evidence(gp,result['generation_sha256']))
        stats=result['stats']
        source_time=saved.get('recorded_at',job['ended_at'])
        require(checked_time(source_time,job['current_started_at'],decision['budget_deadline']),'generation outside authorized time range')
        require(result['ok'] is True and result['repo_id']==saved['repo_id']==MODEL and saved['model_revision']==result['model_revision']==MODEL_REV
                and saved['job_run_id']==REVISION and saved['run_id']==result['development_run_id']==mid
                and saved['text']==result['text'] and result['truncated'] is False and saved['truncated'] is False
                and entry['worker_exited'] is True and stats['worker_exited'] is True and stats['worker_returncode']==0
                and type(entry['pid']) is int and entry['pid']>0 and entry['pid']==saved['worker_pid']==stats['worker_pid']
                and not alive(entry['pid']),'actor result or release unverified')
        if result['generation_sha256']==art['checked_source']:
            matched.append(mid)
    require(len(matched)==1,'changed artifact lacks a unique real generation')
    journal=json.loads(evidence(base/'journal'/(REVISION+'.json')))
    require(journal['run_id']==REVISION and journal['mock'] is False and journal['pending'] is None
            and journal['current_hashes']['qbench/input_validation.py']==art['sha256']
            and any(e['action'].get('action')=='apply' and e['provenance']['run_id']==matched[0]
                    and e['provenance']['actor']=='miki_full_source' and e['provenance']['revision']==MODEL_REV
                    and e['result'].get('sha256',{}).get('qbench/input_validation.py')==art['sha256'] for e in journal['events']),'saved implementation provenance mismatch')
    return {'reason':reason,'artifact_sha256':art['sha256'],'result_sha256':art['checked_source'],'actor_job_sha256':digest(raw),
            'quality':'pending' if job['host_checks_passed'] is True else 'partial'}


def terminal_outcome(frozen,data):
    base=MIKI/'data/development/gemma26'
    folder=base/'model-runs'/REVISION
    boxes=list(folder.glob('*.outbox.json'))
    if not boxes:
        if (base/'jobs'/(REVISION+'.failure.json')).is_file():
            return terminal_failure(frozen,data)
        findings=check_actor(frozen,data['judgment_sha256'])
        return {**findings,'stage':'inspection_wait'}
    require(len(boxes)==1,'ambiguous actor outbox')
    mid=boxes[0].name.removesuffix('.outbox.json')
    raw=evidence(boxes[0]);_,box=durable_outbox(base,mid,digest(raw))
    _,decision,job_raw,job=actor_identity(frozen,data['judgment_sha256'])
    require(job['terminal'] is True and box['judgment_sha256']==data['judgment_sha256']
            and box['expected_report_sha256']==data['expected_report_sha256'] and box['request_id']==REQUEST
            and box['job_run_id']==REVISION and box['model_run_id']==mid,'terminal outbox binding mismatch')
    intent=json.loads(evidence(folder/(mid+'.intent.json')))
    gen=json.loads(evidence(folder/(mid+'.generation.json'),box['result_sha256']))
    result=json.loads(evidence(folder/(mid+'.result.json'),intent['result_sha256']))
    stats=result['stats'];pid=gen['worker_pid'];entry,=job['model_runs']
    require(intent['revision_judgment_sha256']==data['judgment_sha256'] and intent['request_id']==REQUEST
            and intent['job_run_id']==intent['formal_run_id']==REVISION and intent['model_run_id']==mid
            and result['development_run_id']==gen['run_id']==mid and result['model_revision']==gen['model_revision']==MODEL_REV
            and gen['job_run_id']==REVISION and gen['repo_id']==result['repo_id']==MODEL and gen['text']==result['text']
            and result['generation_sha256']==box['result_sha256'] and entry['result_sha256']==intent['result_sha256']
            and entry['model_run_id']==mid and entry['worker_exited'] is True and stats['worker_exited'] is True
            and stats['worker_returncode']==0 and type(pid) is int and pid>0 and stats['worker_pid']==box['worker_pid']==entry['pid']==pid
            and not alive(pid) and checked_time(intent['started_at'],job['current_started_at'],gen['recorded_at'])
            and checked_time(gen['recorded_at'],intent['started_at'],decision['budget_deadline']),'terminal source or release differs')
    if box['stage']=='inspection_wait':
        valid=check_actor(frozen,data['judgment_sha256'])
        require(valid['artifact_sha256']==box['artifact_sha256'] and valid['result_sha256']==box['result_sha256']
                and box['quality'] in ('pending','partial','failed') and (box['quality']!='pending' or job['host_checks_passed'] is True),
                'terminal candidate differs from announced candidate')
    else:
        old=frozen['miki']['receipt']['artifact_sha256'];evidence(COPY/TARGET,old)
        require(box['stage']=='implementation_incomplete' and box['quality'] in ('failed','unverified')
                and not job['artifacts'] and job['model_patch_count']==0 and box['artifact_sha256']==job['final_hashes'][TARGET]==old,
                'incomplete terminal candidate mismatch')
    return {**fixed_counts(box),**{k:box[k] for k in ('stage','quality','artifact_sha256','result_sha256')},'reason':decision['reason'],
            'actor_job_sha256':digest(job_raw),'actor_outbox_sha256':digest(raw),'worker_pid':pid,
            'model_run_id':mid,'actor_cli_pid':intent['cli_pid'],'source_recorded_at':gen['recorded_at']}


def confirm_release(svc,row,frozen,data,findings=None):
    # Called under the local journal lock. No network calls or model starts.
    verified_result=findings or terminal_outcome(frozen,data)
    for k in ('stage','quality','artifact_sha256','result_sha256'):
        require(row['outbox'][k]==verified_result[k],'terminal version differs from submitted version')
    if row.get('actor_outbox_sha256'):
        require(verified_result.get('actor_outbox_sha256')==row['actor_outbox_sha256'],'source outbox changed')
    row['terminal_job_sha256']=verified_result['actor_job_sha256']
    parent=verified_result.get('actor_cli_pid')
    if not svc.available():
        row.update(execution_state='awaiting_actor_release',resource_state='release_unconfirmed')
    elif parent and alive(parent):
        row.update(execution_state='awaiting_cli_exit',resource_state='worker_released')
    else:
        row.update(execution_state='finished',resource_state='released')
        row.setdefault('release_observed_at',now());row.setdefault('ended_at',now())
    svc.save(row)


def reconcile_released(svc,row):
    if row['run_id']!=REVISION or row['execution_state'] not in ('awaiting_actor_release','awaiting_cli_exit'):
        return
    if alive(row.get('worker_pid')) or not svc.available():
        return
    try:
        confirm_release(svc,row,read_json(correction_path(svc)),row['revision_identity'])
    except (OSError,KeyError,ValueError,TypeError,PatrolError):
        return  # Model exit alone never proves a result.


def review_eligibility(row,jobpath):
    quality_raw=evidence(QUALITY);q=json.loads(quality_raw);meta=json.loads(evidence(jobpath))
    key=row['amends']['judgment_sha256'];decision=json.loads(evidence(MIKI/'data/development/gemma26/judgments'/(REVISION+'.json'),key))
    require(q['run_id']==REVISION and q['artifact_sha256']==row['artifact_sha256'] and q['judgment_sha256']==key
            and q['passed'] is True and q['scope_ok'] is True and all(q[name]['passed']==q[name]['total']==num
            for name,num in [('fixed',37),('diagnostics',7)]),'A independent quality gate not passed')
    require(meta['review_version']==2 and meta['judgment_sha256']==key and meta['budget_deadline']==decision['budget_deadline']
            and datetime.fromisoformat(decision['budget_start'])<=datetime.fromisoformat(now())<datetime.fromisoformat(decision['budget_deadline']),'review budget expired or unbound')
    return digest(quality_raw)


def mamu_report_evidence(row,payload):
    folder=MAMU/'state/inspections/runs'/row['run_id']
    data=json.loads(evidence(folder/'result.json',payload['result_sha256']))
    box=json.loads(evidence(folder/'outbox.json'))
    require(all(box[k]==payload[k] for k in ('actor','request_id','run_id','stage','quality','result_sha256','artifact_sha256','summary'))
            and data['run_id']==row['run_id'] and data['request_id']==row['request_id'] and data['prior_run_id']==REVISION
            and data['artifact_sha256']==payload['artifact_sha256'] and data['spec_sha256']==row['spec_sha256'],
            'stored inspection result differs from formal report')
    generation=data.get('generated',{});life=generation.get('lifecycle',{});pid=life.get('pid')
    if type(pid) is int and owned_worker(pid,row.get('child_pid'),MAMU/'mamu_model_worker.py'):
        return pid
    if pid:
        release=json.loads(evidence(folder/'release.json'))
        require(release['result_sha256']==box['result_sha256'] and release['worker_exit']['worker_pid']==pid
                and release['worker_exit']['worker_exited'] is True and not alive(pid),'inspection release unconfirmed')
    else:
        require(data.get('ok') is False and payload['stage']=='inspection_incomplete' and
                generation.get('worker_started') is False,'inspection worker identity unknown')
    return None


def register_amendment(svc,data):
    if data['op']=='prepare-amendment':
        return prepare(svc,data)
    svc.check_live()
    svc.setup()
    frozen=read_json(correction_path(svc))
    actor='miki' if data['op']=='amend' else 'mamu'
    old=frozen[actor]
    run=data['run_id']
    parent=CONTINUED if actor=='miki' else REVISION
    require(data['request_id']==old['receipt']['request_id'] and data['prior_run']==parent and data['review_run']==frozen['review_run']
            and data['expected_report_sha256']==old['receipt']['report_sha256'] and HASH.fullmatch(data['judgment_sha256']),'revision input mismatch')
    if actor=='miki':
        require(run==REVISION,'invalid implementation revision ID')
    else:
        require(RUN.fullmatch(run) and run.startswith('mamu-a-review-') and run!=old['run_id'],'new inspection run required')
    identity={k:data[k] for k in ('run_id','prior_run','request_id','review_run','expected_report_sha256','judgment_sha256')}

    phase=data.get('phase','terminal')
    require(phase in ('terminal','before_release') and (actor=='miki' or phase=='terminal'),'invalid report phase')

    def existing():
        row=svc.load(run)
        require(row.get('revision_identity')==identity,'revision collision')
        if actor=='miki' and phase=='before_release':
            require(row.get('model_run_id')==data['model_run_id'] and row.get('actor_outbox_sha256')==data['actor_outbox_sha256'],
                    'pre-release packet collision')
        elif actor=='miki' and row['execution_state']!='finished':
            confirm_release(svc,row,frozen,data)
        return svc.public(row)

    with exclusive(svc.base/'journal.lock'):
        saved=existing() if svc.path(run).exists() else None
    if saved:
        return svc.deliver(run) if actor=='miki' and saved['report_state']!='received' else saved
    original=svc.load(old['run_id'])
    require(verified(original)==old['receipt'] and sha(old['body'])==old['receipt']['report_sha256'],'prior receipt differs')
    source=original['request_receipt']
    if actor=='mamu':
        require(archive(svc,original)['body']==old['body'],'prior report differs')
        source=svc.mail.intake(original)
        require(all(source[k]==original['request_receipt'][k] for k in ('block_id','request_sha256')),'original formal request changed')
    row={k:original[k] for k in IDENTITY}
    if actor=='miki':
        live=phase=='before_release'
        findings=before_release(svc,frozen,data) if live else terminal_outcome(frozen,data)
        reason=findings['reason']
        state='awaiting_actor_release' if live else 'finished'
        row.update(artifact_sha256=findings['artifact_sha256'],execution_state=state,resource_state='owned' if live else 'released',
                   stage=findings['stage'],quality=findings['quality'],actor_job_sha256=findings['actor_job_sha256'])
        row.update({k:findings[k] for k in ('worker_pid','actor_cli_pid','actor_outbox_sha256','source_recorded_at','model_run_id') if k in findings})
    else:
        impl=svc.load(REVISION)
        verified(impl)
        require(impl['amends']['judgment_sha256']==data['judgment_sha256'] and impl['stage']=='inspection_wait','revised candidate not received')
        reason=impl['amends']['reason']
        row.update(artifact_sha256=impl['outbox']['artifact_sha256'],execution_state='queued',resource_state='idle',
                   stage='requested',quality='unassessed')
    compose.assert_safe(reason,svc.cfg.secrets)
    row.update(run_id=run,prior_run=parent,queued_at=now(),request_receipt=source,report_state='not_due',revision_identity=identity,
               amends={**old,'reason':reason,'review_run':frozen['review_run'],'judgment_sha256':data['judgment_sha256']})
    if actor=='miki':
        packet={k:findings[k] for k in ('artifact_sha256','result_sha256','quality')}
        packet.update(stage=findings['stage'],summary=('凍結した前版への追試に基づく訂正候補を保存しました。独立再検査の適格性は別途確認します。'
                      if findings['stage']=='inspection_wait' else '訂正候補を保存できませんでした。確定した実行結果を保存しました。'))
        for name,label in [('fixed_checks','固定検証'),('additional_checks','追加診断')]:
            score=findings.get(name,{})
            if 'passed' in score and 'total' in score:
                packet['summary']+=f" {label}は{score['total']}件中{score['passed']}件が一致。"
        complete={**packet,**{k:row[k] for k in ('actor','request_id','run_id')}}
        row.update(report_state='pending',outbox=packet,report_sha256=sha(make_report(row,complete,svc.cfg.secrets)))
    else:
        cmd=svc.command(row)  # Verify the controlled inspection metadata before queuing.
        evidence(Path(cmd[-1]).parent/'input_validation.py',row['artifact_sha256'])
        row['quality_gate_sha256']=review_eligibility(row,Path(cmd[-1]))
    with exclusive(svc.base/'journal.lock'):
        if svc.path(run).exists():
            return existing()
        require(not any(j.get('amends',{}).get('run_id')==old['run_id'] for j in svc.rows()),'second revision of the same parent')
        if actor=='miki' and phase=='terminal':
            confirm_release(svc,row,frozen,data,findings)
        else:
            svc.save(row)
    return svc.deliver(run) if actor=='miki' else svc.public(row)


def deliver_revision(svc,row,body):
    old=row['amends']
    remote=svc.mail.current_report(row)
    require(remote and remote['block_id']==old['receipt']['block_id'] and remote['type']=='code'
            and remote['report_page_id']==old['receipt']['report_page_id'],'formal amendment block missing or moved')
    if remote['text']==body:
        return {k:remote[k] for k in ('block_id','report_sha256','report_page_id')}
    require(remote['text']==old['body'] and remote['report_sha256']==old['receipt']['report_sha256']
            and remote['version']==old['version'],'amendment conflict: initial report changed')
    if row['report_state']!='pending':
        return None
    if row['actor']=='miki':
        source=svc.mail.intake(svc.load('gemma26-A-v1'))
        require(all(source[k]==row['request_receipt'][k] for k in ('request_sha256','block_id')),'original formal request changed')
    # Durable before crossing the network. A crash/unknown outcome only reads.
    with exclusive(svc.base/'journal.lock'):
        current=svc.load(row['run_id'])
        current['report_state']='unknown'
        svc.save(current)
    try:
        svc.mail.replace_report(remote,body)
    except PatrolError as exc:
        if (isinstance(exc,RemoteError) and not exc.uncertain) or getattr(exc,'not_sent',False):
            with exclusive(svc.base/'journal.lock'):
                current=svc.load(row['run_id'])
                current['report_state']='pending'
                svc.save(current)
        raise
    updated=svc.mail.find_report(row,body)
    require(updated and updated['block_id']==remote['block_id'] and updated['report_page_id']==remote['report_page_id'],'amendment readback mismatch')
    return updated
