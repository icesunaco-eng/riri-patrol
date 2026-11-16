"""Operator-registered delegations for bounded work on the normal formal inbox.

Only the separate administrative entry point installs a scope. A formal request
can select a catalogued job, but cannot supply paths, a delegation or commands.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import project_ops as ops
from .local_outcomes import read_json
from .local_tasks import HASH, MIKI, exclusive, require

ROUTE = 'bounded-v1'
KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,79}')
SCOPE = {'project','project_url','spec_version','material_root','materials','spec_sha256',
         'actors','requesters','delegation_ref','delegation_sha256','data_class','expires_at','jobs'}
OPTIONS = {'implementation','max_jobs','job_sha256'}


def candidate_api():
    # Only the installed actor package supplies these pure, reviewed validators.
    if str(MIKI) not in sys.path:
        sys.path.insert(0,str(MIKI))
    from miki import bounded_intake, bounded_adapter
    for module in (bounded_intake,bounded_adapter):
        require(Path(module.__file__).resolve().parent==(MIKI/'miki').resolve(),'installed bounded adapter unavailable')
    return bounded_intake.validate,bounded_adapter.read_materials


def expiry(value):
    require(isinstance(value,str),'absolute delegation expiry required')
    day=datetime.fromisoformat(value)
    require(day.tzinfo is not None,'absolute delegation expiry required')
    return day


def scope_file(svc,project):
    require(isinstance(project,str) and KEY.fullmatch(project),'invalid project identity')
    return svc.base/('project-'+project+'.bounded-scope.json')


def overlap(a,b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def require_root(name):
    require(isinstance(name,str),'absolute registered root required')
    p=Path(name)
    require(p.is_absolute() and p.resolve()==p and p.is_dir() and p.stat().st_uid==os.getuid()
            and not p.stat().st_mode & 0o022,'unsafe registered root')
    return p


def checked_scope(scope,svc):
    require(isinstance(scope,dict) and SCOPE<=scope.keys()<=SCOPE|OPTIONS,'scope fields incomplete or unsupported')
    scope_file(svc,scope['project'])
    require(isinstance(scope['project_url'],str) and re.fullmatch(r'https://app\.notion\.com/p/[0-9a-f]{32}',scope['project_url'])
            and isinstance(scope['spec_version'],str) and re.fullmatch(r'[a-zA-Z0-9._-]{1,80}',scope['spec_version']),'invalid project or specification version')
    require(scope['data_class'] in ('synthetic','public','approved_non_sensitive'),'data classification not delegated')
    names=scope['requesters']
    require(isinstance(names,list) and names and len(names)==len(set(names)) and set(names)<=ops.registry().keys()
            and 'みき' not in names,'unknown or self requester')
    require(isinstance(scope['actors'],dict) and set(scope['actors'])=={'miki'} and
            isinstance(scope['actors']['miki'],list) and scope['actors']['miki'] and
            set(scope['actors']['miki'])<={'review','implementation'},'unsupported actor or work mode')
    cap=scope.get('max_jobs',1)
    require(type(cap) is int and 1<=cap<=32,'invalid registration budget')
    require(expiry(scope['expires_at'])>datetime.now(timezone.utc),'delegation expired')
    materials=require_root(scope['material_root'])
    source=Path(scope['delegation_ref'])
    require(not source.is_relative_to(materials) and not overlap(materials,svc.root)
            and not overlap(materials,MIKI) and not overlap(materials,ops.MAMU),'materials and protected policy/live data must be separate')
    ops.blob(source,scope['delegation_sha256'])
    require(source.stat().st_uid==os.getuid() and not source.stat().st_mode & 0o022,'unsafe delegation reference')
    validate,read=candidate_api()
    jobs=scope['jobs']
    require(isinstance(jobs,list) and 1<=len(jobs)<=cap,'no registered job proposals')
    seen=set();ids=set();bound={}
    for job in jobs:
        digest=validate(job)
        require(digest not in seen and job['job_id'] not in ids and job['project']==scope['project']
                and job['mode'] in scope['actors']['miki'] and job['actor']=='miki'
                and job['spec_sha256']==scope['spec_sha256']
                and all(scope['materials'].get(k)==v for k,v in job['materials'].items()),'job differs from delegation')
        seen.add(digest);ids.add(job['job_id']);bound.update(job['materials'])
        read(materials,job)
    require(bound==scope['materials'] and ('job_sha256' not in scope or seen=={scope['job_sha256']}),
            'catalog or approved material set differs')
    if 'implementation' in scope['actors']['miki']:
        imp=scope.get('implementation',{})
        require(set(imp)=={'repo_url','baseline_commit','work_root','write_files','acceptance'},'implementation snapshot missing')
        work=require_root(imp['work_root'])
        require(not any(overlap(work,p) for p in (svc.root,MIKI,ops.MAMU,materials)) and
                not source.is_relative_to(work) and
                re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',imp['repo_url']) and
                re.fullmatch(r'[a-f0-9]{40}',imp['baseline_commit']),'unsafe implementation scope')
        test=imp['acceptance'];verifier=Path(test['verify_path'])
        require(set(test)=={'runner','verify_path','verify_sha256','case_count'} and test['runner']=='python-sandbox'
                and type(test['case_count']) is int and 1<=test['case_count']<=100 and
                verifier.suffix=='.py' and verifier.parent==materials.parent and not verifier.is_relative_to(materials),'unsafe verifier')
        ops.blob(verifier,test['verify_sha256'])
        for j in jobs:
            if j['mode']=='implementation':
                require(j['write_files']==imp['write_files'],'write scope mismatch')
                ops.blob(work/j['write_files'][0],j['materials'][j['write_files'][0]])
        verify_repo(imp)
    else:
        require('implementation' not in scope,'read-only project cannot configure writes')


def verify_repo(imp):
    work=imp['work_root']
    for args,expected in [(['rev-parse','HEAD'],imp['baseline_commit']),(['remote','get-url','origin'],imp['repo_url'])]:
        actual=subprocess.check_output(['/usr/bin/git','-C',work,*args],text=True,timeout=5).strip()
        require(actual.removesuffix('.git')==expected,'registered repository or baseline changed')


def install_scope(svc,scope):
    svc.check_live();svc.setup();checked_scope(scope,svc)
    path=scope_file(svc,scope['project'])
    with exclusive(svc.base/'journal.lock',wait=True):
        for f in svc.base.glob('project-*.bounded-scope.json'):
            other=read_json(f)
            if other==scope:
                path=f
                break
            if other['project']==scope['project']:
                require(other['project_url']==scope['project_url'],'registered project page changed')
                continue
            def roots(reg):
                return [Path(reg['material_root'])]+([Path(reg['implementation']['work_root'])] if 'implementation' in reg else [])
            require(not any(overlap(a,b) for a in roots(other) for b in roots(scope)),'different projects share a material or output root')
        else:
            if path.exists():
                new_ids={j['job_id'] for j in scope['jobs']}
                for f in grant_files(svc,scope['project']):
                    previous=read_json(f)
                    require(not (new_ids & {j['job_id'] for j in previous['jobs']}),'existing registered job version cannot be replaced')
                    if scope['delegation_sha256']==previous['delegation_sha256']:
                        require(scope.get('max_jobs',1)==previous.get('max_jobs',1) and
                                expiry(scope['expires_at'])<=expiry(previous['expires_at']),'existing delegation cannot be extended or reset')
                revision=hashlib.sha256(json.dumps(scope,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                path=path.with_name('project-'+scope['project']+'.'+revision+'.bounded-scope.json')
        ops.immutable(path,scope)
    return {'state':'project_registered','project':scope['project'],'owner_receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'job_slots':scope.get('max_jobs',1),'queued':False}


def marker(text,label):
    values=re.findall(r'(?<![\w-])'+label+r'=([^\s、。]+)',text)
    require(len(values)==1,'missing or ambiguous '+label)
    return values[0]


def grant_files(svc,project):
    first=scope_file(svc,project)
    return ([first] if first.exists() else [])+sorted(svc.base.glob('project-'+project+'.*.bounded-scope.json'))


def scope_for(svc,staged):
    paths=[p for p in grant_files(svc,staged['project_id']) if hashlib.sha256(ops.blob(p)).hexdigest()==staged['owner_receipt_sha256']]
    require(len(paths)==1,'pinned registered project scope missing')
    path=paths[0]
    raw=ops.blob(path,staged['owner_receipt_sha256'])
    scope=read_json(path)
    require(json.loads(raw)==scope and scope['project']==staged['project_id'],'scope identity changed')
    return scope,path


def plan(svc,staged,*,active=False):
    s,path=scope_for(svc,staged);validate,read=candidate_api()
    sha=staged['manifest_sha256']
    jobs=[j for j in s['jobs'] if validate(j)==sha]
    require(len(jobs)==1,'job digest is not registered for project')
    job=jobs[0]
    require(job['project']==s['project'] and job['actor']==staged['actor']=='miki' and
            job['mode'] in s['actors']['miki'] and staged['requester'] in s['requesters'],'scope or requester mismatch')
    imp=s.get('implementation',{}) if job['mode']=='implementation' else {}
    if active:
        require(expiry(s['expires_at'])>datetime.now(timezone.utc),'delegation expired before execution')
        ops.blob(s['delegation_ref'],s['delegation_sha256'])
        read(Path(s['material_root']),job)
        if imp:
            verify_repo(imp)
            target=job['write_files'][0]
            ops.blob(Path(imp['work_root'])/target,job['materials'][target])
            ops.blob(imp['acceptance']['verify_path'],imp['acceptance']['verify_sha256'])
    return {'project_id':s['project'],'project_url':s['project_url'],'spec_sha256':job['spec_sha256'],'spec_version':s['spec_version'],
            'material_root':s['material_root'],'reply_to':staged['requester'],'bounded_job':job,'allowed_files':job['write_files'],
            'worker_seconds':job['limits']['seconds'],'deadline':s['expires_at'],'queue_expires_at':s['expires_at'],
            'owner_receipt_path':str(path),'acceptance':{'runner':'review-artifact'},**imp}


def discover(svc,req,rows,run):
    project=marker(req.text,'project-id');sha=marker(req.text,'manifest-sha256')
    require(HASH.fullmatch(sha),'invalid job digest')
    files=grant_files(svc,project)
    require(files,'registered project delegation missing')
    validate,_=candidate_api()
    files=[p for p in files if any(validate(j)==sha for j in read_json(p)['jobs'])]
    require(len(files)==1,'job not registered or ambiguous for project')
    path=files[0]
    staged={'route':ROUTE,'project_id':project,'owner_receipt_sha256':hashlib.sha256(ops.blob(path)).hexdigest(),
            'manifest_sha256':sha,'run_id':run,'request_id':req.id,'requester':req.requester,'actor':'miki',
            'prior_run':None,'root_run':run,'parent_request_id':req.id,'step':0}
    recognize(svc,staged,rows)
    return staged


def recognize(svc,staged,rows):
    from .local_tasks import MAIL
    from . import compose
    from .parse import validate_request,replace_status
    matching=[r for r in rows if r.id==staged['request_id']]
    require(len(matching)==1 and svc.cfg.requests_page.replace('-','')==MAIL,'missing or duplicate formal ID')
    r=matching[0];m=plan(svc,staged)
    require(not re.search(r'(?<![\w-])(?:parent-request|prior-run|previous-run|parent-run|artifact-sha256)=',r.text),
            'dependent inspection requires registered parent run, received report and release')
    heart=ops.registry().get(r.requester)
    require(heart and r.requester==staged['requester'] and re.fullmatch(re.escape(heart)+r'[0-9]{8}-[1-9][0-9]*',r.id)
            and r.recipients==['みき'] and r.block_type=='code' and r.fields.get('案件ページ')==m['project_url'],'formal scope, sender or destination differs')
    for label,key in [('local-run','run_id'),('manifest-sha256','manifest_sha256'),('project-id','project_id')]:
        require(marker(r.text,label)==staged[key],'formal '+label+' differs')
    repo=r.fields.get('GitHubプロジェクト','')
    if m.get('repo_url'):
        require(repo.removeprefix('https://github.com/').removesuffix('.git')==m['repo_url'].removeprefix('https://github.com/'),'repository differs')
    else:
        require(repo in ('','なし','不要'),'unregistered repository')
    validate_request(r);compose.assert_request_safe(r,svc.cfg.secrets);compose.instruction(r,['みき'],svc.cfg.pages)
    return {'block_id':r.block_id,'request_sha256':ops.sha(replace_status(r.text,'未着手')),'received_at':ops.now(),'source_status':r.status}


def register(svc,staged,rows):
    receipt=recognize(svc,staged,rows);run=staged['run_id']
    with exclusive(svc.base/'journal.lock',wait=True):
        if svc.path(run).exists():
            j=svc.load(run)
            require(all(j.get(k)==v for k,v in staged.items()) and
                    all(j['request_receipt'][k]==receipt[k] for k in ('block_id','request_sha256')),'registered formal identity changed')
            fixed=json.loads(ops.blob(ops.keyed(svc,run,'.ops-job.json'),j['ops_job_sha256']))
            require(all(j.get(k)==v for k,v in fixed.items()),'registered actor binding changed')
            if (j['execution_state']=='held' and j.get('last_issue')=='formal_request_conflict' and
                    not any(j.get(k) for k in ('started_at','driver_pid','child_pid','cancel_requested_at','outbox'))):
                plan(svc,staged,active=True)
                hold=ops.keyed(svc,run,'.ops-hold.json')
                j.update(execution_state='queued',last_issue='original_formal_source_restored',
                         restored_hold_sha256=hashlib.sha256(ops.blob(hold)).hexdigest())
                svc.save(j)
            return
        require(receipt['source_status']!='完了' and svc.mail.current_report(staged) is None,'historical request or existing formal report')
        s,_=scope_for(svc,staged);binding={**staged,**plan(svc,staged,active=True)}
        reservations=0
        for f in svc.base.glob('*.ops-stage.json'):
            if f==ops.keyed(svc,run,'.ops-stage.json'):continue
            old=read_json(f)
            require(old['request_id']!=staged['request_id'],'formal ID is already reserved')
            if old.get('route')==ROUTE and old['project_id']==staged['project_id']:
                require(old['manifest_sha256']!=staged['manifest_sha256'],'single-run job already reserved')
                old_scope,_=scope_for(svc,old)
                if old_scope['delegation_sha256']==s['delegation_sha256']:
                    reservations+=1
        require(reservations<s.get('max_jobs',1),'project run budget exhausted')
        require(all(j['request_id']!=staged['request_id'] and j.get('next_request_id')!=staged['request_id']
                    and j.get('next_run')!=run for j in svc.rows()),'formal ID or run belongs to existing parent or child')
        ops.immutable(ops.keyed(svc,run,'.ops-stage.json'),staged)
        path=ops.keyed(svc,run,'.ops-job.json');ops.immutable(path,binding)
        svc.save({**binding,'request_receipt':receipt,'ops_job_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                  'execution_state':'queued','resource_state':'idle','report_state':'not_due','stage':'requested','quality':'unassessed',
                  'queued_at':ops.now(),'return_state':'not_due','recipient_read_state':'unconfirmed'})


def proof_dir(j):
    return MIKI/'data/development/bounded'/j['run_id']


def source_packet(j,input_data=None):
    from .local_tasks import alive
    from .local_revisions import owned_worker
    folder=proof_dir(j)
    box=json.loads(ops.blob(folder/'outbox.json'))
    p={k:box[k] for k in ops.PACKET}
    require(all(p[k]==j[k] for k in ('actor','request_id','run_id')) and
            (input_data is None or all(input_data.get(k)==v for k,v in p.items())),'packet differs from formal outbox')
    job=j['bounded_job'];mode=job['mode']
    art=json.loads(ops.blob(folder/'artifact.json',p['artifact_sha256']))
    identity={'schema':1,'route':ROUTE,'actor':'miki','request_id':j['request_id'],'run_id':j['run_id'],
              'project_id':j['project_id'],'mode':mode,'kind':mode,'job_sha256':j['manifest_sha256']}
    require(all(art.get(k)==v for k,v in identity.items()),'artifact belongs to a different registration')
    gen=folder/'generation.json'
    if gen.exists():
        g=json.loads(ops.blob(gen,p['result_sha256']))
        intent=json.loads(ops.blob(folder/'intent.json'))
        pid=g['worker_pid']
        require(art['generation_sha256']==p['result_sha256'] and type(pid) is int and pid>0 and box['worker_pid']==pid
                and isinstance(g.get('text'),str) and (g['text'].strip() or p['stage']=='bounded_incomplete')
                and g['run_id']==g['job_run_id']==j['run_id'] and g['request_id']==j['request_id']
                and g['repo_id']=='mlx-community/gemma-4-26B-A4B-it-qat-4bit'
                and g['model_revision']=='0e3cbab38ce568cf6e23543010d08d03b731910c'
                and intent['job_sha256']==j['ops_job_sha256'] and intent['manifest_sha256']==j['manifest_sha256']
                and intent['cli_pid']==j['child_pid'] and intent['driver_pid']==j['driver_pid']
                and intent['request_block_id']==j['request_receipt']['block_id'],'generation or formal owner differs')
        if alive(pid):
            require(owned_worker(pid,j['child_pid'],MIKI/'miki/model_worker.py'),'worker lacks formal parent')
        else:
            require(ops.release_evidence(j,pid,p['result_sha256']),'worker exit not confirmed')
    else:
        f=json.loads(ops.blob(folder/'terminal_failure.json',p['result_sha256']))
        require(p['stage']=='bounded_incomplete' and art['generation_sha256'] is None and
                f.get('request_id')==j['request_id'] and f.get('run_id')==j['run_id'] and f.get('reason')
                and type(f.get('worker_started')) is bool and (not f['worker_started'] or
                (f.get('worker_exited') is True and type(f.get('worker_pid')) is int and f['worker_pid']>0
                 and not alive(f['worker_pid']))),'worker outcome is unknown')
    content=art['content']
    if gen.exists():
        expected={'route':ROUTE,'project_id':j['project_id'],'manifest_sha256':j['manifest_sha256'],'mode':mode}
        profile='gemma-4-26b-a4b-it-qat-4bit-bounded'
        require(all(g.get(k)==v and intent.get(k)==v for k,v in expected.items()) and
                g.get('profile_id')==profile and g.get('model_path')==str(MIKI/'models/gemma-4-26B-A4B-it-qat-4bit')
                and intent.get('owner_receipt_sha256')==j['owner_receipt_sha256'] and
                intent.get('request_id')==j['request_id'] and intent.get('job_run_id')==j['run_id'] and
                box.get('project_id')==j['project_id'] and box.get('mode')==mode,'project, profile, mode or owner proof changed')
    if p['stage']=='bounded_reviewed':
        require(mode=='review' and art['status']=='structured' and isinstance(content,dict)
                and g.get('truncated') is False and p['quality']=='unverified','incomplete inspection cannot be reported as structured')
        from miki.mf_review_packet import parse_reply
        fence=re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*",g['text'],re.I|re.S)
        reply=parse_reply(fence[1] if fence else g['text'])
        require(json.dumps(reply,sort_keys=True)==json.dumps(content,sort_keys=True),
                'review artifact differs from validated original reply')
    elif p['stage']=='bounded_implemented':
        require(mode=='implementation' and art['status']=='scored' and g.get('truncated') is False
                and content['relative_path']==j['allowed_files'][0],'invalid implementation evidence')
        f=folder/'artifact'/content['relative_path']
        ops.blob(f,content['source_sha256']);checks=json.loads(ops.blob(folder/'checks.json',content['checks_sha256']))
        require(checks.get('execution_ok') is True and checks['artifact_sha256']==content['source_sha256'] and
                checks['verify_sha256']==j['acceptance']['verify_sha256'] and
                checks['passed']==content['passed'] and checks['total']==content['total']==j['acceptance']['case_count']
                and type(checks['passed']) is int and 0<=checks['passed']<=checks['total'] and
                p['quality']==('pending' if checks['passed']==checks['total'] else 'partial' if checks['passed'] else 'failed'),
                'scoring differs from submitted candidate')
    else:
        require(p['stage']=='bounded_incomplete' and art['status']=='incomplete' and isinstance(content,dict)
                and content.get('reason') and p['quality'] in ('failed','unverified'),'invalid incomplete outcome')
    return p,box


def hold_reason(svc,req):
    runs=re.findall(r'(?<![\w-])local-run=([^\s、。]+)',req.text)
    if len(runs)==1 and ops.OPS.fullmatch(runs[0]):
        path=ops.keyed(svc,runs[0],'.ops-hold.json')
        if path.exists():
            reason=read_json(path).get('reason','')
            # Diagnostics in the private journal may include local filesystem names.
            for token,label in [('expired','受付または実行の期限が過ぎています'),('budget','許可された実行枠を使い切りました'),
                                ('already reserved','同じjobまたは依頼IDの枠は予約済みです'),('material','固定した資料の内容または配置が一致しません'),
                                ('digest','登録済みjobのSHAを特定できません'),('delegation','対応する案件の委任記録がありません'),
                                ('registered','登録済みの案件・仕事内容・jobを照合できません'),('scope','案件・依頼元・対象範囲を照合できません'),
                                ('formal','同じ正式依頼・宛先・版を確認できません'),('repository','登録済みrepoと一致しません')]:
                if token in reason:return label
            return '登録情報・許可・資料を照合できません。保留理由はりりの非公開記録に保存されています'
    return 'local-run / project-id / manifest-sha256 と登録済みの仕事内容・資料が必要です'


def released(j):
    """Check saved exit evidence as well as the caller's actual free lock."""
    from .local_tasks import alive
    pid=j.get('worker_pid')
    if type(pid) is int and pid>0:
        return ops.release_evidence(j,pid,j['outbox']['result_sha256'])
    folder=proof_dir(j)
    try:
        fail=json.loads(ops.blob(folder/'terminal_failure.json',j['outbox']['result_sha256']))
        return (fail['request_id']==j['request_id'] and fail['run_id']==j['run_id'] and
                (fail.get('worker_started') is False or (fail.get('worker_exited') is True and
                type(fail.get('worker_pid')) is int and fail['worker_pid']>0 and not alive(fail['worker_pid']))))
    except (KeyError,TypeError,ValueError,OSError,ops.PatrolError):
        return False


def archived_exit(j,result_sha=None):
    """Recheck one exited CLI/worker and its saved generation without invoking the actor."""
    from .local_tasks import alive,heavy_free
    folder=proof_dir(j)
    fields={}
    for name in ('intent','generation','pending'):
        raw=ops.blob(folder/(name+'.json'));fields[name]=json.loads(raw)
        fields[name+'_sha256']=hashlib.sha256(raw).hexdigest()
    intent,gen,event=(fields[k] for k in ('intent','generation','pending'))
    pid=gen.get('worker_pid');cli=intent.get('cli_pid')
    require(type(pid) is int and pid>0 and cli==j.get('child_pid') and type(cli) is int and cli>0
            and event.get('worker_pid')==pid and not any(alive(n) for n in (pid,cli,j.get('driver_pid')))
            and heavy_free(),'original actor, worker or heavy lock is still present')
    require(intent.get('driver_pid')==j.get('driver_pid') and intent.get('request_block_id')==j['request_receipt']['block_id']
            and intent.get('job_sha256')==j['ops_job_sha256'] and intent.get('job_run_id')==j['run_id']
            and gen.get('job_run_id')==gen.get('run_id')==event.get('run_id')==j['run_id']
            and all(x.get('request_id')==j['request_id'] for x in (intent,gen,event)) and
            all(gen.get(k)==intent.get(k)==j.get(k) for k in ('route','project_id','manifest_sha256')) and
            gen.get('mode')==intent.get('mode')==j['bounded_job']['mode'] and
            fields['generation_sha256']==event.get('result_sha256') and result_sha in (None,fields['generation_sha256']),
            'saved generation or delivery event differs from formal claim')
    return {'run_id':j['run_id'],'request_id':j['request_id'],'cli_pid':cli,'worker_pid':pid,
            'intent_sha256':fields['intent_sha256'],'pending_sha256':fields['pending_sha256'],
            'result_sha256':fields['generation_sha256']}


def reconciled_exit(j,pid,result_sha):
    try:
        folder=proof_dir(j);observed=json.loads(ops.blob(folder/'release-reconciliation.json'))
        proof=archived_exit(j,result_sha)
        return (pid==proof['worker_pid'] and all(observed.get(k)==v for k,v in proof.items())
                and observed.get('resource_state')=='released' and observed.get('terminal') is True
                and observed.get('lock_after') in ('free','absent'))
    except (KeyError,TypeError,ValueError,OSError,ops.PatrolError):
        return False


def recover_saved_once(svc,j):
    """Start only the actor's no-model reconciliation, once per immutable result."""
    from .local_tasks import child_env
    folder=proof_dir(j)
    if (j['execution_state'] not in ('finished','reconcile') or j.get('cancel_requested_at')
            or (j.get('report_state')=='received' and j.get('resource_state')=='released')
            or not (folder/'generation.json').is_file()
            or not (folder/'pending.json').is_file()
            or ((folder/'outbox.json').exists() and (folder/'release-reconciliation.json').exists())):
        return
    proof=archived_exit(j)
    if (folder/'outbox.json').is_file() and ops.release_evidence(j,proof['worker_pid'],proof['result_sha256']):
        return
    key=ops.keyed(svc,j['run_id'],'.bounded-recovery-attempt.json')
    with exclusive(svc.base/'journal.lock',wait=True):
        svc.check_live()
        live=svc.load(j['run_id'])
        if key.exists() or (live['report_state']=='received' and live['resource_state']=='released'):
            return
        ops.immutable(key,{**proof,'command':'bounded-submit','reserved_at':ops.now()})
    # No timeout/retry loop here. The actor uses its existing bounded notification,
    # immutable outbox and formal readback. Unknown process startup is never retried.
    subprocess.Popen([str(MIKI/'start-miki.sh'),'bounded-submit','--run-id',j['run_id']],
                     cwd=MIKI,env=child_env(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
