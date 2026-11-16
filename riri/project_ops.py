"""Bounded formal jobs using the existing local actor scheduler and mail delivery."""
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from . import compose
from .errors import PatrolError, StopRequested
from .local_outcomes import atomic_json, read_json
from .local_tasks import (ACTORS, HASH, MAIL, MAMU, MIKI, PROJECT, alive, exclusive,
                          make_report, now, require, sha)
from .parse import parse_requests, replace_status, validate_request

OPS = re.compile(r'ops-[0-9a-f]{32}')
AREA = Path('/Users/sunako.jp/Documents/Codex/2026-11-14/files-pasted-by-the-user-notion/work/phase56-ops')
LIMITS = {'max_runs':4,'max_repairs':1,'wall_seconds':2700,'worker_seconds':180}
EMPTY = hashlib.sha256(b'').hexdigest()
PACKET = ('actor','request_id','run_id','artifact_sha256','result_sha256','stage','quality','summary')


def blob(path, expected=None, limit=512*1024):
    p=Path(path)
    require(p.is_absolute() and p.resolve()==p and p.is_file() and p.stat().st_size<=limit,'missing or unsafe project material')
    data=p.read_bytes()
    if expected is not None:
        require(HASH.fullmatch(expected) and hashlib.sha256(data).hexdigest()==expected,'project material changed')
    return data


def registry():
    rows=json.loads(blob(MIKI/'registry.json'))['agents']
    names={r['name']:r['heart'] for r in rows if r.get('normal_request_route') and r.get('heart')}
    require(len(names)==len(rows) and len(set(names.values()))==len(names),'ambiguous actor registry')
    return names


def plan(sha256, *, verify_repo=False):
    path=AREA/'materials/manifest.json';m=json.loads(blob(path,sha256))
    require(m['project_url']==PROJECT and re.fullmatch('[a-z0-9][-a-z0-9]{1,80}',m['project_id'])
            and m['spec_version'] and m['limits']==LIMITS and m['reply_to'] in registry()
            and m['repo_url']=='https://github.com/icesunaco-eng/miki-ai'
            and re.fullmatch('[a-f0-9]{40}',m['baseline_commit']) and Path(m['work_root'])==AREA/'candidate'
            and (AREA/'candidate').resolve()==AREA/'candidate'
            and m['allowed_files']==['miki/a_check_summary.py'],'unregistered project or work scope')
    spec=Path(m['spec_path']);test=Path(m['acceptance']['verify_path'])
    require(spec.parent==test.parent==path.parent and m['acceptance']['runner']=='python-sandbox'
            and m['acceptance']['case_count']>0 and m['materials'][str(spec)]==m['spec_sha256']
            and m['materials'][str(test)]==m['acceptance']['verify_sha256'],'unbound specification or verifier')
    for name,digest in m['materials'].items():
        require(Path(name).parent==path.parent,'material outside approved folder');blob(name,digest)
    if verify_repo:
        head=subprocess.check_output(['/usr/bin/git','-C',m['work_root'],'rev-parse','HEAD'],text=True).strip()
        require(head==m['baseline_commit'],'project checkout changed')
    return m


def keyed(svc,run,suffix):
    require(isinstance(run,str) and OPS.fullmatch(run),'invalid project run')
    return svc.base/(run+suffix)


def immutable(path,obj):
    if path.exists():
        require(read_json(path)==obj,'immutable identity collision')
    else:
        atomic_json(path,obj)


def stage(svc,data):
    svc.check_live();svc.setup()
    m=plan(data['manifest_sha256'],verify_repo=True);run=data['run_id'];rid=data['request_id'];sender=data['requester']
    heart=registry().get(sender)
    require(heart and sender==m['reply_to'] and re.fullmatch(re.escape(heart)+r'\d{8}-[1-9]\d*',rid),'sender or reply identity differs')
    require(rid[len(heart):len(heart)+8]>= '20261116','historical request')
    actor=data.get('actor','miki');require(actor in ACTORS and sender!=ACTORS[actor][1],'invalid registered recipient')
    if actor=='mamu':
        require('review_artifact' in m and 'review_artifact_sha256' in m and Path(m['review_artifact']).is_relative_to(AREA/'materials'),'direct inspection lacks registered input')
        blob(m['review_artifact'],m['review_artifact_sha256'])
    record={'manifest_sha256':data['manifest_sha256'],'run_id':run,'request_id':rid,'requester':sender,'actor':actor,
            'prior_run':None,'root_run':run,'parent_request_id':rid,'step':0}
    with exclusive(svc.base/'journal.lock',wait=True):
        file=keyed(svc,run,'.ops-stage.json')
        for other in svc.base.glob('*.ops-stage.json'):
            item=read_json(other)
            require(other==file or item['request_id']!=rid,'formal ID already staged')
            if not item['prior_run'] and item['manifest_sha256']==record['manifest_sha256']:
                require(other==file,'project already has an original execution budget')
        immutable(file,record)
    return {'state':'awaiting_formal_request','run_id':run,'request_id':rid,'worker_started':False}


def intake(svc,rows,staged):
    m=plan(staged['manifest_sha256'])
    matching=[r for r in rows if r.id==staged['request_id']]
    require(len(matching)==1 and svc.cfg.requests_page.replace('-','')==MAIL,'missing or duplicate formal request')
    r=matching[0];actor=staged['actor'];sender=staged['requester'];run=staged['run_id']
    require(r.block_type=='code' and r.requester==sender and r.recipients==[ACTORS[actor][1]]
            and r.fields.get('案件ページ')==m['project_url'] and r.status in ('未着手','対応中','完了'),'formal project identity mismatch')
    for label,value in [('local-run',run),('manifest-sha256',staged['manifest_sha256']),('project-id',m['project_id'])]:
        require(re.findall(r'(?<![\w-])'+label+r'=([\w-]+)',r.text)==[value],'project reference mismatch: '+label)
    repo=r.fields.get('GitHubプロジェクト','').removeprefix('https://github.com/').removesuffix('.git').removesuffix('/')
    require(repo==m['repo_url'].removeprefix('https://github.com/'),'formal repository differs')
    validate_request(r);compose.assert_request_safe(r,svc.cfg.secrets)
    compose.instruction(r,[ACTORS[actor][1]],svc.cfg.pages)
    return {'block_id':r.block_id,'request_sha256':sha(replace_status(r.text,'未着手')),'received_at':now(),'source_status':r.status}


def reconcile_intake(svc,j,staged,receipt):
    run=j['run_id'];folder=actor_dir(j)
    require(all(j.get(k)==v for k,v in staged.items()) and all(j['request_receipt'][k]==receipt[k] for k in ('block_id','request_sha256')),
            'formal request changed')
    fixed=json.loads(blob(keyed(svc,run,'.ops-job.json'),j['ops_job_sha256']))
    require(all(j.get(k)==v for k,v in fixed.items()),'registered job binding changed')
    prior=j['prior_run']
    if prior:
        prev=svc.load(prior)
        require(prev['next_run']==run and prev['next_request_id']==j['request_id'] and prev['root_run']==j['root_run']
                and prev['receipt']['artifact_sha256']==j['artifact_sha256'],'previous formal receipt changed')
        if j['actor']=='miki':
            # The working copy is intentionally mutable. Its *input* remains in the upstream review's immutable snapshot.
            blob(prev['artifact_path'],j['artifact_sha256'])
            previous=folder/'previous-source.py'
            if previous.exists() or (j.get('receipt') and j.get('outbox',{}).get('artifact_sha256')!=j['artifact_sha256']):
                blob(previous,j['artifact_sha256'])
        else:
            blob(j['artifact_path'],j['artifact_sha256'])
    ack=j.get('receipt')
    if ack:
        require(ack['report_sha256']==j['report_sha256'] and ack['request_id']==j['request_id'] and ack['run_id']==run
                and all(ack[k]==j['outbox'][k] for k in ('result_sha256','artifact_sha256')),'completed report linkage changed')
        if j['actor']=='miki':
            src=folder/'generation.json'
            if not src.is_file():src=folder/'terminal_failure.json'
            blob(src,ack['result_sha256'])
            if ack['artifact_sha256']!=EMPTY:blob(folder/'artifact'/j['allowed_files'][0],ack['artifact_sha256'])
        else:blob(folder/'result.json',ack['result_sha256'])
    hold=keyed(svc,run,'.ops-hold.json')
    if not (hold.exists() and ack and j['actor']=='miki' and prior and j['execution_state']=='finished' and j['resource_state']=='released'
            and ack['artifact_sha256']!=j['artifact_sha256']):return
    raw=blob(hold);entry=json.loads(raw)
    if (entry.get('reason')=='project material changed' and entry.get('formal_source_sha256')==receipt['request_sha256']
            and entry.get('request_id')==j['request_id'] and entry.get('run_id')==run):
        blob(j['artifact_path'],ack['artifact_sha256'])
        digest=hashlib.sha256(raw).hexdigest()
        proof={'run_id':run,'request_id':j['request_id'],'hold_sha256':digest,'state':'resolved_by_immutable_intake',
               'request_sha256':receipt['request_sha256'],'input_sha256':j['artifact_sha256'],
               'saved_output_sha256':ack['artifact_sha256'],'report_sha256':ack['report_sha256']}
        record=keyed(svc,run,'.ops-hold-resolved.'+digest+'.json')
        with exclusive(svc.base/'journal.lock',wait=True):
            immutable(record,proof)
            current=svc.load(run)
            if current.get('intake_reconciliation')!=proof:
                current['intake_reconciliation']=proof;svc.save(current)


def register(svc,staged,rows):
    svc.check_live()
    run=staged['run_id'];receipt=intake(svc,rows,staged)
    if svc.path(run).exists():
        reconcile_intake(svc,svc.load(run),staged,receipt)
        return
    m=plan(staged['manifest_sha256'],verify_repo=True)
    prior=staged['prior_run'];root=svc.load(staged['root_run']) if prior else None
    deadline=root['deadline'] if root else (datetime.fromisoformat(receipt['received_at'])+timedelta(seconds=m['limits']['wall_seconds'])).isoformat()
    actor=staged['actor'];target=Path(m['work_root'])/m['allowed_files'][0];artifact=None
    if prior:
        prev=svc.load(prior)
        require(prev.get('next_request_id')==staged['request_id'] and prev.get('next_run')==run
                and prev['step']+1==staged['step']<m['limits']['max_runs'] and prev['root_run']==root['run_id'],'invalid child binding')
        artifact=prev['outbox']['artifact_sha256']
        if actor=='mamu':
            target=MIKI/'data/development/ops56'/prior/'artifact'/m['allowed_files'][0]
        blob(target,artifact)
    elif actor=='mamu':
        target=Path(m['review_artifact']);artifact=m['review_artifact_sha256'];blob(target,artifact)
    binding={**staged,**{k:m[k] for k in ('project_id','project_url','spec_version','spec_path','spec_sha256','work_root','allowed_files','acceptance','reply_to')},
             'manifest_path':str(AREA/'materials/manifest.json'),'artifact_path':str(target),'artifact_sha256':artifact,'deadline':deadline,
             'worker_seconds':m['limits']['worker_seconds']}
    if prior and actor=='miki':
        binding['correction']=prev['correction']
    with exclusive(svc.base/'journal.lock',wait=True):
        if svc.path(run).exists():
            old=svc.load(run)
            require(old['request_receipt']['request_sha256']==receipt['request_sha256'] and old['request_receipt']['block_id']==receipt['block_id'],'formal request changed')
            return
        file=keyed(svc,run,'.ops-job.json')
        # A crash may leave the immutable job before the scheduler row.
        if file.exists():
            saved=read_json(file);binding['deadline']=saved['deadline']
        immutable(file,binding)
        svc.save({**binding,'request_receipt':receipt,'ops_job_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                  'execution_state':'queued','resource_state':'idle','report_state':'not_due','stage':'requested','quality':'unassessed',
                  'queued_at':now(),'return_state':'not_due','recipient_read_state':'unconfirmed'})


def runnable(svc,j):
    if datetime.fromisoformat(now())>=datetime.fromisoformat(j['deadline']) or j.get('cancel_requested_at'):
        return False
    if j.get('retry_after') and datetime.fromisoformat(now())<datetime.fromisoformat(j['retry_after']):
        return False
    if not j['prior_run']:return True
    prev=svc.load(j['prior_run'])
    if prev.get('worker_pid') and not release_evidence(prev,prev['worker_pid'],prev['outbox']['result_sha256']):return False
    return (prev.get('next_run')==j['run_id'] and prev.get('next_request_id')==j['request_id']
            and prev['report_state']=='received' and prev['execution_state']=='finished' and prev['resource_state']=='released'
            and prev['receipt']['artifact_sha256']==j['artifact_sha256'])


def command(svc,j):
    plan(j['manifest_sha256'],verify_repo=True)
    staged=read_json(keyed(svc,j['run_id'],'.ops-stage.json'))
    rows=parse_requests(svc.mail.notion.read_blocks(MAIL))
    proof=intake(svc,rows,staged)
    require(proof['source_status']!='完了' and all(proof[k]==j['request_receipt'][k] for k in ('request_sha256','block_id')),
            'formal source removed, changed, or closed')
    path=keyed(svc,j['run_id'],'.ops-job.json');blob(path,j['ops_job_sha256'])
    if j['artifact_sha256']:blob(j['artifact_path'],j['artifact_sha256'])
    return [str((MIKI if j['actor']=='miki' else MAMU)/('start-'+j['actor']+'.sh')),'ops-run','--job',str(path)]


def actor_exit(j,path):
    if j.get('outbox') or not path.is_file() or path.stat().st_size>65536:
        return
    try:
        feedback=json.loads(path.read_bytes())
    except (ValueError,UnicodeError):
        return
    if feedback.get('worker_started') is not False or feedback.get('status') not in ('resource_wait','budget_exhausted'):
        return
    if (actor_dir(j)/'generation.json').exists() or (actor_dir(j)/'outbox.json').exists():
        return
    if feedback['status']=='budget_exhausted':
        j.update(execution_state='expired',last_issue='parent_budget_expired')
        return
    checks=j.setdefault('resource_checks',[])
    checks.append({'at':now(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'worker_started':False})
    remaining=datetime.fromisoformat(j['deadline'])-datetime.fromisoformat(now())
    if len(checks)<3 and remaining.total_seconds()>300:
        j.update(execution_state='queued',resource_state='waiting',last_issue='resource_wait',
                 retry_after=(datetime.fromisoformat(now())+timedelta(seconds=300)).isoformat())
    else:
        j.update(execution_state='held',resource_state='waiting',last_issue='resource_wait_limit')

def actor_dir(j):
    return (MIKI/'data/development/ops56' if j['actor']=='miki' else MAMU/'state/inspections/runs')/j['run_id']


def release_evidence(j,pid,result_sha):
    folder=actor_dir(j)
    try:
        if j['actor']=='miki':
            end=json.loads(blob(folder/'result.json'))
            stats=end['runtime']['stats']
            return (end['run_id']==j['run_id'] and end['request_id']==j['request_id'] and end['terminal'] is True
                    and end['resource_state']=='released' and end['lock_after'] in ('absent','free')
                    and stats['worker_exited'] is True and stats['worker_pid']==pid and not alive(pid))
        end=json.loads(blob(folder/'release.json'))
        return end['result_sha256']==result_sha and end['worker_exit']['worker_exited'] is True and end['worker_exit']['worker_pid']==pid and not alive(pid)
    except (ValueError,KeyError,OSError,PatrolError):
        return False


def source_packet(j,input_data=None):
    folder=actor_dir(j);box=json.loads(blob(folder/'outbox.json'));p={k:box[k] for k in PACKET}
    require(all(p[k]==j[k] for k in ('actor','run_id','request_id')) and
            (input_data is None or all(input_data[k]==p[k] for k in PACKET)),'actor packet differs from durable file')
    gen=folder/('generation.json' if j['actor']=='miki' else 'result.json')
    if not gen.is_file() and j['actor']=='miki':
        gen=folder/'terminal_failure.json';failure=json.loads(blob(gen,p['result_sha256']))
        require(failure['run_id']==j['run_id'] and failure['request_id']==j['request_id'] and failure['stage']==p['stage']=='implementation_incomplete'
                and type(failure['worker_started']) is bool and failure['reason']
                and (not failure['worker_started'] or (failure.get('worker_exited') is True and type(failure.get('worker_pid')) is int
                and failure['worker_pid']>0 and not alive(failure['worker_pid']))),'unconfirmed absence of generation')
    else:
        result=json.loads(blob(gen,p['result_sha256']))
        require(result.get('run_id')==j['run_id'] and result.get('request_id')==j['request_id'],'actor record identity differs')
        if j['actor']=='miki':
            intent=json.loads(blob(folder/'intent.json'))
            require(result['job_run_id']==j['run_id'] and result['repo_id']=='mlx-community/gemma-4-26B-A4B-it-qat-4bit'
                    and result['model_revision']=='0e3cbab38ce568cf6e23543010d08d03b731910c'
                    and intent['job_sha256']==j['ops_job_sha256'] and intent['cli_pid']==j['child_pid'] and intent['driver_pid']==j['driver_pid']
                    and intent['request_block_id']==j['request_receipt']['block_id'] and box['worker_pid']==result['worker_pid'],
                    'implementation provenance differs from formal actor')
        else:
            require(result['artifact_sha256']==j['artifact_sha256'] and result['spec_sha256']==j['spec_sha256']
                    and result['prior_run_id']==j['prior_run'],'inspection references a different source')
        pid=result.get('worker_pid') if j['actor']=='miki' else result.get('generated',{}).get('lifecycle',{}).get('pid')
        if pid:
            from .local_revisions import owned_worker
            script=MIKI/'miki/model_worker.py' if j['actor']=='miki' else MAMU/'mamu_model_worker.py'
            if alive(pid):
                require(owned_worker(pid,j.get('child_pid'),script),'worker is not owned by this formal CLI')
            else:
                require(release_evidence(j,pid,p['result_sha256']),'worker release record unavailable')
        else:
            require(j['actor']=='mamu' and p['stage']=='inspection_incomplete' and result.get('ok') is False,
                    'missing actual worker')
    if j['actor']=='miki' and p['stage']=='inspection_wait':
        score=inspection_score(j,p,box)
        require(score is not None and p['quality']==('pending' if score['passed']==score['total'] else 'partial'),
                'candidate or fixed score differs')
    elif j['actor']=='miki' and not Path(j['artifact_path']).exists():
        require(p['stage']=='implementation_incomplete' and p['artifact_sha256']==EMPTY,'unverified absence of artifact')
    else:
        blob(j['artifact_path'],p['artifact_sha256'])
    return p,box


def inspection_score(j,p,box):
    folder=actor_dir(j)
    if box.get('artifact_present') is False or not (folder/'generation.json').is_file():return None
    checks=json.loads(blob(folder/'checks.json',box['checks_sha256']))
    if checks.get('execution_ok') is not True:return None
    gen=json.loads(blob(folder/'generation.json',p['result_sha256']))
    if gen.get('truncated') is not False or (j['step'] and j['artifact_sha256']==p['artifact_sha256']):return None
    count=j['acceptance']['case_count'];cases=checks['cases'];passed=checks['passed']
    require(type(passed) is int and 0<=passed<=count==checks['total']==len(cases)
            and len({x['id'] for x in cases})==count and all(type(x['passed']) is bool for x in cases)
            and sum(x['passed'] for x in cases)==passed and checks['ok'] is (passed==count)
            and checks['artifact_sha256']==p['artifact_sha256'] and checks['verify_sha256']==j['acceptance']['verify_sha256'],
            'scored candidate or artifact differs')
    blob(j['artifact_path'],p['artifact_sha256']);blob(folder/'artifact'/j['allowed_files'][0],p['artifact_sha256'])
    return {'passed':passed,'total':count,'checks_sha256':box['checks_sha256']}


def amend_inspection_route(svc,j):
    # Only an already released, first-stage, actually scored candidate may have its routing corrected.
    if (j.get('next_run') or j.get('route_amendment') or j['step']!=0 or j['actor']!='miki' or j['quality']!='failed'
            or j['stage']!='implementation_incomplete' or j['report_state']!='received'
            or j['execution_state']!='finished' or j['resource_state']!='released' or j.get('cancel_requested_at')
            or datetime.fromisoformat(now())>=datetime.fromisoformat(j['deadline'])):return False
    packet,box=source_packet(j);score=inspection_score(j,packet,box)
    if not score:return False
    from .local_revisions import archive
    previous=archive(svc,j)
    child_run='ops-'+uuid.uuid5(uuid.NAMESPACE_URL,'riri-ops:'+j['run_id']+':next').hex
    child=child_relay(j,child_run,'draft')
    with exclusive(svc.base/'journal.lock',wait=True):
        current=svc.load(j['run_id'])
        require(current.get('receipt')==previous['receipt'] and not current.get('next_run'),'prior formal receipt changed')
        prior_file=keyed(svc,j['run_id'],'.ops-report-v1.json')
        # Archival retry after a crash must preserve exactly the first version of all records.
        if not prior_file.exists():atomic_json(prior_file,{'row':current,'formal':previous})
        else:require(read_json(prior_file)['formal']==previous,'prior report archive collision')
        current.update(next_run=child_run,next_request_id=child['request_id'],next_state='draft',inspection_score=score,
                       route_amendment=previous,report_state='pending',return_state='superseded_by_inspection')
        current.pop('receipt',None)
        current['report_sha256']=sha(make_report(current,packet,svc.cfg.secrets))
        svc.save(current)
    return True


def child_relay(j,child_run,action,reason=None):
    # Both actor-owned idempotent relays are deterministic, lightweight local entry points.
    if j['actor']=='miki':
        from importlib import import_module
        if str(MIKI) not in sys.path:sys.path.insert(0,str(MIKI))
        relay=import_module('miki.relay').Relay(MIKI/'data/relay',MIKI/'registry.json',[j['project_url']])
        payload={'recipient_id':'mamu','github_project':'icesunaco-eng/miki-ai','project_url':j['project_url'],'objective':'固定した成果物を独立検査する',
                 'steps':'仕様と対象成果物を照合し、確認できた所見と未確認を保存する。',
                 'materials':f"local-run={child_run} manifest-sha256={j['manifest_sha256']} project-id={j['project_id']} parent-request={j['request_id']} artifact-sha256={j['outbox']['artifact_sha256']}",
                 'permission_scope':'固定資料と対象成果物のみ。運用設定や本番データを変更しない。',
                 'expected_result':'独立した確認結果と対象SHAを同じ正式IDへ報告。','timing':'親案件の残り時間内。'}
        key=child_run
        return relay.draft(payload,key) if action=='draft' else relay.submit(key)
    return mamu_relay(j,child_run,action,reason)


def validate_correction(j,c):
    folder=actor_dir(j)
    require(HASH.fullmatch(c['evidence_sha256']) and 0<len(c['reason'])<=800,'correction evidence missing')
    evidence=json.loads(blob(folder/'correction-evidence.json',c['evidence_sha256']))
    require(evidence.get('confirmed') is True and evidence['reason']==c['reason']
            and evidence.get('case_id') and evidence.get('actual')!=evidence.get('expected')
            and all(evidence[k]==j[k] for k in ('run_id','request_id','artifact_sha256','spec_sha256')),
            'unverified correction evidence')
    generation=blob(folder/'generation.json',evidence['generation_sha256'])
    result=json.loads(blob(folder/'result.json'))
    require(result['generation_sha256']==hashlib.sha256(generation).hexdigest(),'first opinion linkage differs')
    if evidence.get('source')=='host_post_opinion':
        checks=json.loads(blob(folder/'host-checks.json',evidence['host_checks_sha256']))
        require(checks['generation_sha256']==result['generation_sha256'] and checks['artifact_sha256']==j['artifact_sha256']
                and checks['state']=='completed' and checks['passed'] is False
                and any(row['passed'] is False and row['actual']!=row['expected'] for row in checks['cases']),
                'post-opinion reproduction does not establish a discrepancy')
    else:
        require(evidence.get('source')=='independent_finding_reproduced' and evidence.get('source_quote') and evidence.get('requirement_quote')
                and evidence['source_quote'] in blob(j['artifact_path'],j['artifact_sha256']).decode('utf-8')
                and evidence['requirement_quote'] in blob(j['spec_path'],j['spec_sha256']).decode('utf-8'),
                'independent finding does not match the reviewed version')
    return c


def prepare_packet(svc,j,input_data=None):
    packet,box=source_packet(j,input_data)
    planned=j.copy();planned['outbox']={k:packet[k] for k in PACKET[3:]}
    # Validate the entire actor payload before reserving a child's formal ID.
    make_report(planned,packet,svc.cfg.secrets)
    score=inspection_score(j,packet,box) if j['actor']=='miki' else None
    wants_child=score is not None
    if score:planned['inspection_score']=score
    if j['actor']=='mamu' and packet['stage'] in ('reviewed','inspection_completed') and packet['quality']=='failed' and box.get('correction'):
        planned['correction']=validate_correction(j,box['correction']);wants_child=True
    if wants_child and j['step']+1<LIMITS['max_runs'] and datetime.fromisoformat(now())<datetime.fromisoformat(j['deadline']):
        child_run='ops-'+uuid.uuid5(uuid.NAMESPACE_URL,'riri-ops:'+j['run_id']+':next').hex
        child=child_relay(planned,child_run,'draft',planned.get('correction'))
        planned.update(next_run=child_run,next_request_id=child['request_id'],next_state='draft')
    body=make_report(planned,packet,svc.cfg.secrets)
    with exclusive(svc.base/'journal.lock',wait=True):
        live=svc.load(j['run_id'])
        if live.get('outbox'):
            require(live['outbox']==planned['outbox'],'conflicting repeated actor report');return
        require(live['execution_state'] in ('executing','reconcile','finished'),'actor was never claimed')
        live.update({k:planned[k] for k in ('next_run','next_request_id','next_state','correction','inspection_score') if k in planned})
        source=json.loads(blob(actor_dir(j)/('outbox.json' if j['actor']=='miki' else 'result.json')))
        pid=source.get('worker_pid') if j['actor']=='miki' else source.get('generated',{}).get('lifecycle',{}).get('pid')
        live.update(worker_pid=pid,actor_cli_pid=j.get('child_pid'))
        live.update(outbox=planned['outbox'],report_sha256=sha(body),report_state='pending',stage=packet['stage'],quality=packet['quality'])
        svc.save(live)


def report(svc,data):
    svc.check_live();svc.setup();run=data['run_id']
    with exclusive(keyed(svc,run,'.ops-prepare.lock'),wait=True):
        j=svc.load(run)
        require(j.get('ops_job_sha256') and j['execution_state'] in ('executing','reconcile','finished') and
                j['request_id']==data['request_id'],'no live project claim')
        if not j.get('outbox'):
            prepare_packet(svc,j,data)
        else:
            require(all(data[k]==j['outbox'][k] for k in PACKET[3:]) and data['actor']==j['actor'],'report collision')
    return svc.deliver(run)


def mamu_relay(j,run,action,correction):
    require(isinstance(correction,dict),'grounded correction required')
    payload={'parent_run':j['run_id'],'run_id':run,'request_id':j['request_id'],'project_id':j['project_id'],
             'project_url':j['project_url'],'spec_sha256':j['spec_sha256'],'artifact_sha256':j['outbox']['artifact_sha256'],
             'manifest_sha256':j['manifest_sha256'],**correction}
    if str(MIKI) not in sys.path:sys.path.insert(0,str(MIKI))
    from miki.relay import NotionMailbox,canonical
    box=NotionMailbox();day=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y%m%d')
    payload.update(formal_day=day,formal_ids=[r['id'] for r in box.requests() if re.fullmatch('🧡'+day+r'-[1-9][0-9]*',r['id'])])
    proc=subprocess.run([str(MAMU/'start-mamu.sh'),'ops-correction-draft'],input=json.dumps(payload),text=True,capture_output=True,timeout=12,check=False)
    require(proc.returncode==0,'correction draft awaits reconciliation')
    draft=json.loads(proc.stdout);rid=draft['request_id'];text=draft['text']
    require(rid.startswith('🧡') and draft['text_sha256']==sha(text),'correction draft differs')
    if action=='draft':return draft
    # Mamu's own ledger allocates the ID. Riri's durable intent controls the single append.
    from .local_tasks import LocalTasks,Config,ROOT
    svc=LocalTasks(Config.load(ROOT));svc.check_live()
    if str(MIKI) not in sys.path:sys.path.insert(0,str(MIKI))
    old=svc.load(j['run_id'])
    require(old['next_request_id']==rid and old['next_run']==run,'correction allocation conflict')
    def verify():
        rows=[r for r in box.requests() if r['id']==rid]
        require(len(rows)<=1,'duplicate correction request')
        if rows:
            require(rows[0]['requester']=='まむ' and rows[0]['recipients']==['みき'] and canonical(rows[0]['text'])==canonical(text),'correction formal ID collision')
            return True
        return False
    confirmed=verify()
    if not confirmed and old.get('next_state')=='draft':
        block={'object':'block','id':'0'*32,'type':'code','code':{'rich_text':[{'type':'text','plain_text':text,'text':{'content':text}}]}}
        staged={'request_id':rid,'requester':'まむ','actor':'miki','run_id':run,'manifest_sha256':j['manifest_sha256']}
        intake(svc,parse_requests([block]),staged)
        update(svc,j['run_id'],next_state='submit_unconfirmed')
        box.append_request(text);confirmed=verify()
    return {**draft,'saved_verified':confirmed}


def reconcile_children(svc,rows):
    for j in rows:
        if not j.get('ops_job_sha256'):continue
        run=j['run_id']
        if not j.get('next_run') and j.get('report_state')=='received':
            try:
                with exclusive(keyed(svc,run,'.ops-prepare.lock')):
                    if amend_inspection_route(svc,j):
                        svc.deliver(run);j=svc.load(run)
            except (OSError,ValueError,PatrolError,KeyError):
                continue
        if j['execution_state'] in ('queued','launch_pending') and datetime.fromisoformat(now())>=datetime.fromisoformat(j['deadline']):
            with exclusive(svc.base/'journal.lock',wait=True):
                current=svc.load(run)
                if current['execution_state'] in ('queued','launch_pending'):
                    current.update(execution_state='expired',resource_state='released',ended_at=now(),last_issue='parent_budget_expired');svc.save(current)
            continue
        if (not j.get('outbox') and j.get('started_at') and (actor_dir(j)/'outbox.json').is_file()
                and j['execution_state'] in ('executing','reconcile','finished')):
            try:
                with exclusive(keyed(svc,run,'.ops-prepare.lock')):
                    prepare_packet(svc,j)
            except (BlockingIOError,OSError,ValueError,KeyError,PatrolError,RuntimeError):pass
        if (not j.get('next_run') or j['report_state']!='received' or j['resource_state']!='released'
                or j['execution_state']!='finished' or j.get('next_state')=='submitted'):continue
        if j.get('worker_pid') and not release_evidence(j,j['worker_pid'],j['outbox']['result_sha256']):
            continue
        with exclusive(keyed(svc,run,'.ops-child.lock')):
            if datetime.fromisoformat(now())>=datetime.fromisoformat(j['deadline']) or j.get('cancel_requested_at'):
                update(svc,run,next_state='budget_closed');continue
            child=child_relay(j,j['next_run'],'submit',j.get('correction'))
            require(child.get('request_id')==j['next_request_id'],'child formal ID changed')
            # The next *normal* patrol independently reads and validates the formal child block before queuing it.
            actor='mamu' if j['actor']=='miki' else 'miki'
            staged={'actor':actor,'manifest_sha256':j['manifest_sha256'],'run_id':j['next_run'],'request_id':j['next_request_id'],
                    'requester':ACTORS[j['actor']][1],'prior_run':run,'root_run':j['root_run'],'parent_request_id':j['parent_request_id'],'step':j['step']+1}
            with exclusive(svc.base/'journal.lock',wait=True):
                immutable(keyed(svc,staged['run_id'],'.ops-stage.json'),staged)
                live=svc.load(run)
                live['next_state']='submitted' if child.get('save_state')=='saved_verified' or child.get('saved_verified') is True else 'submit_unconfirmed'
                svc.save(live)


def update(svc,run,**changes):
    with exclusive(svc.base/'journal.lock',wait=True):
        row=svc.load(run);row.update(changes);svc.save(row)


def patrol_intake(svc,req,requests):
    markers=re.findall(r'(?<![\w-])local-run=(ops-[\w-]+)',req.text)
    if len(markers)!=1 or not OPS.fullmatch(markers[0]):return False
    run=markers[0];file=keyed(svc,run,'.ops-stage.json')
    try:
        require(file.exists(),'approved local manifest registration missing')
        staged=read_json(file)
        register(svc,staged,requests)
    except (KeyError,OSError,ValueError,PatrolError) as exc:
        # Stored separately from operational rows: an invalid request cannot occupy the actor queue.
        h={'request_id':req.id,'run_id':run,'reason':str(exc)[:180],'formal_source_sha256':sha(replace_status(req.text,'未着手'))}
        with exclusive(svc.base/'journal.lock',wait=True):
            path=keyed(svc,run,'.ops-hold.json')
            if not path.exists() or read_json(path)!=h:atomic_json(path,h)
        if svc.path(run).exists():
            j=svc.load(run)
            if j['execution_state'] in ('queued','launch_pending'):update(svc,run,execution_state='held',last_issue='formal_request_conflict')
        return False
    return True


def return_results(patrol):
    from .local_tasks import LocalTasks
    svc=LocalTasks(patrol.config)
    if patrol.config.mode!='live' or not svc.base.is_dir():return
    for j in svc.rows():
        if not j.get('ops_job_sha256'):continue
        if (j['execution_state'] in ('expired','cancelled','held','reconcile') and not j.get('outbox')
                and not alive(j.get('child_pid')) and not alive(j.get('driver_pid')) and svc.available()):
            if (actor_dir(j)/'outbox.json').is_file():continue
            root=svc.load(j['root_run'])
            why=j.get('last_issue') or j.get('stop_reason') or 'result_unconfirmed'
            text=f"@{j['reply_to']} りりから実行状態の返却です。親案件 {j['project_id']}、依頼 {j['request_id']}、run {j['run_id']} は {why}。本人による成果・内容合格は未確認です。"
            if j['reply_to']=='みに':text+=' あいすかたかむねからみにへお渡しください。'
            save_return(patrol,svc,j,root,text,None)
            continue
        if j['execution_state']!='finished' or j['resource_state']!='released' or j['report_state']!='received':continue
        if j.get('next_run') and j.get('next_state')!='budget_closed':continue
        if j.get('worker_pid') and not release_evidence(j,j['worker_pid'],j['outbox']['result_sha256']):continue
        root=svc.load(j['root_run']);block=j['receipt']['block_id'];digest=j['receipt']['report_sha256']
        if patrol.state['seen_reports'].get(block)!=digest:continue
        who=j['reply_to'];require(who==root['requester'] and who in registry(),'return recipient differs')
        text=(f"@{who} りりから正式依頼の結果です。親案件 {j['project_id']}、元依頼 {root['request_id']}、今回 {j['request_id']}。"
              f"\n成果物SHA-256 {j['receipt']['artifact_sha256']}\n内容区分 {j['quality']}。"
              f"\n正式報告 https://app.notion.com/p/{j['receipt']['report_page_id'].replace('-','')}#{block.replace('-','')}")
        if who=='みに':text+='\nあいすかたかむねからみにへお渡しください。'
        save_return(patrol,svc,j,root,text,digest)


def save_return(patrol,svc,j,root,text,digest):
    who=j['reply_to']
    require(who==root['requester'] and who in registry(),'return destination changed')
    keys=[]
    for dest,channel in [('registered',patrol.config.instruction_channel),('common',patrol.config.report_channel)]:
        key='ops-return:'+j['run_id']+':'+dest+':'+(digest or 'unconfirmed')
        item=patrol.queue(key,channel,text,'notice')
        keys.append((key,item))
    delivered=all(item['status']=='posted' and item.get('ts') for _,item in keys)
    record={'state':'delivered' if delivered else 'delivery_pending','recipient':who,'request_id':root['request_id'],
            'final_run':j['run_id'],'formal_report_sha256':digest,'receipts':[{k:v for k,v in x.items() if k in ('status','ts','posted_at','channel','marker')} for _,x in keys],
            'recipient_read':'unconfirmed'}
    if j.get('return_receipt')!=record:
        update(svc,j['run_id'],return_state=record['state'],return_receipt=record)
        if root['run_id']!=j['run_id']:update(svc,root['run_id'],return_state=record['state'],return_receipt=record)
