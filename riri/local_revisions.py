"""One post-inspection A amendment. Preparing a correction never queues model work."""
from datetime import datetime
import json
from pathlib import Path
import re
from . import compose
from .errors import PatrolError, RemoteError
from .local_outcomes import atomic_json, digest, read_json
from .local_tasks import (CONTINUED, HASH, IDENTITY, MIKI, REQUEST, REVISION, RUN, alive,
                          exclusive, make_report, now, require, sha)

WORKSPACE=Path('/Users/sunako.jp/Documents/Codex/2026-11-14/files-pasted-by-the-user-notion')
DIAGNOSTIC=WORKSPACE/'outputs/20261115T1933_JST_miki_mamu_3_4/A-frozen-owner-diagnostics.json'
COPY=WORKSPACE/'work/gemma26-development/copies/A-rev1'
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


def check_actor(frozen, judgment_sha):
    """Accept only the published, linked result of a separately approved actor run."""
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
    f=base/'jobs'/(REVISION+'.json')
    raw=evidence(f)
    job=json.loads(raw)
    require(job['run_id']==job['formal_run_id']==REVISION and job['request_id']==REQUEST and job['prior_run']==CONTINUED
            and job['label']=='A' and job['mode']=='implementation' and job['repo_id']==MODEL and job['revision']==MODEL_REV
            and job.get('revision_judgment_sha256')==judgment_sha and job['terminal'] is True and job['human_code_fixes']==0
            and job['model_patch_count']>0 and Path(job['repo_root'])==COPY,'linked actual actor job required')
    start,end=map(datetime.fromisoformat,(job['current_started_at'],job['ended_at']))
    require(datetime.fromisoformat(frozen['recorded_at'])<=datetime.fromisoformat(decision['budget_start'])<=start<=end<=datetime.fromisoformat(decision['budget_deadline']),
            'new job outside authorized time range')
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

    def existing():
        row=svc.load(run)
        require(row.get('revision_identity')==identity,'revision collision')
        return svc.public(row)

    with exclusive(svc.base/'journal.lock'):
        if svc.path(run).exists():
            return existing()
    original=svc.load(old['run_id'])
    require(verified(original)==old['receipt'],'prior receipt differs')
    require(archive(svc,original)['body']==old['body'],'prior report differs')
    # A continuation uses the immutable first A marker. The first Mamu job has
    # its own immutable source marker which remains valid for its revision.
    formal=svc.load('gemma26-A-v1') if actor=='miki' else original
    source=svc.mail.intake(formal)
    require(all(source[k]==original['request_receipt'][k] for k in ('block_id','request_sha256')),'original formal request changed')
    row={k:original[k] for k in IDENTITY}
    if actor=='miki':
        findings=check_actor(frozen,data['judgment_sha256'])
        reason=findings['reason']
        row.update(artifact_sha256=findings['artifact_sha256'],execution_state='finished',resource_state='released',
                   stage='inspection_wait',quality=findings['quality'],ended_at=now(),actor_job_sha256=findings['actor_job_sha256'])
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
        packet.update(stage='inspection_wait',summary='凍結した前版への追試に基づく訂正候補を保存しました。独立再検査待ちです。')
        complete={**packet,**{k:row[k] for k in ('actor','request_id','run_id')}}
        row.update(report_state='pending',outbox=packet,report_sha256=sha(make_report(row,complete,svc.cfg.secrets)))
    else:
        cmd=svc.command(row)  # Verify the controlled inspection metadata before queuing.
        evidence(Path(cmd[-1]).parent/'input_validation.py',row['artifact_sha256'])
    with exclusive(svc.base/'journal.lock'):
        if svc.path(run).exists():
            return existing()
        require(not any(j.get('amends',{}).get('run_id')==old['run_id'] for j in svc.rows()),'second revision of the same parent')
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
