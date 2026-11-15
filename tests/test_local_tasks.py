import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from riri.config import Config
from riri.errors import RemoteError, SafetyError
from riri.local_tasks import LocalTasks, Mailbox, PROJECT, exclusive, heavy_free
from riri.models import REPORT_PAGES

SPEC='cf4c8221919b2c066fe29fe44751efcf2e1c7591fa86ffe289c95f31770fb864'
A=dict(request_id='🩵20261115-1',run_id='gemma26-A-v1',actor='miki',spec_sha256=SPEC,artifact_sha256=None,prior_run=None)
M=dict(request_id='💜20261115-1',run_id='mamu-a-review-'+'c'*32,actor='mamu',spec_sha256=SPEC,artifact_sha256='f'*64,prior_run=A['run_id'])
P=dict(request_id=A['request_id'],run_id=A['run_id'],actor='miki',artifact_sha256='f'*64,result_sha256='a'*64,summary='固定したAの実装を保存。',quality='pending',stage='inspection_wait')
def block(text,kind='code',i='f'*32):
    return {'type':kind,'id':i,kind:{'rich_text':[{'plain_text':text}]}}

class Fake:
    def __init__(self):
        self.blocks={};self.posts=0;self.break_read=False;self.ambiguous=False
    def read_blocks(self,page):
        if self.break_read:
            raise RemoteError('offline',uncertain=False)
        return self.blocks.get(page,[])
    def _request(self,*args,**kwargs):
        payload=kwargs['payload'];self.posts+=1
        entry=payload['children'][0];text=''.join(x['text']['content'] for x in entry['code']['rich_text'])
        if self.ambiguous:
            raise RemoteError('unknown',uncertain=True)
        self.blocks[args[1]].append(block(text,i='2'*32))

def request(job):
    sender,target=('みに','みき') if job['actor']=='miki' else ('みき','まむ')
    return (f"【りりへの依頼】\n依頼ID：{job['request_id']}\n依頼した子：{sender}\n宛先：{target}\n状態：未着手\n案件ページ：{PROJECT}\nやること：実行\n期限・実行タイミング：受付後\n使うもの：local-run={job['run_id']}")

class TasksTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); cfg=Config(root=self.root,mode='live')
        self.mail=Mailbox(cfg); self.fake=Fake();self.mail.notion=self.fake
        self.audit=[]
        self.svc=LocalTasks(cfg,mailbox=self.mail,spawn=lambda root,argv:self.audit.append(argv) or 10,available=lambda:True)
        self.fake.blocks[cfg.requests_page]=[block(request(A)),block(request(M),i='b'*32)]
        self.page=REPORT_PAGES['みき'].split('/')[-1]
        self.fake.blocks[self.page]=[block('報告一覧（新しい順）','heading_2')]
    def active(self):
        self.svc.register(A);j=self.svc.load(A['run_id']);j['execution_state']='executing';j['driver_pid']=os.getpid();self.svc.save(j)
    def test_original_requires_matching_formal_request_and_limits(self):
        self.fake.blocks[self.svc.cfg.requests_page][0]=block(request(A).replace('local-run=','local-run=other'))
        with self.assertRaises(SafetyError):self.svc.register(A)
        self.fake.blocks[self.svc.cfg.requests_page][0]=block(request(A).replace('やること：実行','やること：'+'あ'*3001))
        with self.assertRaises(SafetyError):self.svc.register(A)
        self.fake.blocks[self.svc.cfg.requests_page][0]=block(request(A))
        self.svc.register(A);self.svc.register(A)
        self.assertEqual(len(self.svc.rows()),1)
        self.assertEqual(self.svc.tick()['selected_run'],A['run_id'])
        self.assertEqual(self.audit,[['--execute',A['run_id']]])
        with self.assertRaises(SafetyError):self.svc.register({**A,'request_id':'🩵20261114-1'})
    def test_outbox_survives_outage_and_no_duplicate_after_restart(self):
        self.active();self.fake.break_read=True
        self.assertEqual(self.svc.report(P)['report_state'],'pending');self.assertEqual(self.fake.posts,0)
        self.fake.break_read=False
        got=self.svc.deliver(A['run_id']);self.assertEqual(got['report_state'],'received')
        self.assertEqual(got['receipt']['artifact_sha256'],P['artifact_sha256'])
        self.assertEqual(got['receipt']['result_sha256'],P['result_sha256'])
        from riri.parse import block_text,parse_reports
        report=parse_reports(self.fake.blocks[self.page],expected_reporter='みき')[0]
        self.assertEqual(report.digest,got['receipt']['report_sha256'])
        self.assertIn('作業場所URL：取得できない',report.text)
        self.assertIn('作業場所URLが取得できない理由：',report.text)
        self.assertFalse(report.work_url)
        self.assertEqual(self.svc.report(P)['receipt'],got['receipt']);self.assertEqual(self.fake.posts,1)
        with self.assertRaises(SafetyError):self.svc.report({**P,'summary':'異なる本文'})
    def test_patrol_observes_early_formal_report_without_reissuing_request(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from riri.errors import DuplicateRequestId
        from riri.local_tasks import received_before_patrol
        from riri.parse import parse_requests, parse_reports
        from riri.patrol import Patrol
        self.active()
        self.assertEqual(self.svc.report(P)['report_state'], 'received')
        job = self.svc.load(A['run_id'])
        job.update(execution_state='finished', resource_state='released')
        self.svc.save(job)
        req = parse_requests(self.fake.blocks[self.svc.cfg.requests_page])[0]
        reports = parse_reports(self.fake.blocks[self.page], expected_reporter='みき')
        patrol = SimpleNamespace(state={'requests': {}, 'outbox': {}}, config=self.svc.cfg,
                                 invalid_report_ids=set(), post_count=0, instruction_count=0,
                                 current_reports=reports)
        before = deepcopy(patrol.state)
        for _ in range(2):
            Patrol.requests(patrol, [req])
        self.assertEqual(patrol.state, before)
        self.assertEqual(self.fake.posts, 1)
        self.assertEqual(self.audit, [])
        for target, key in [('receipt','block_id'), ('receipt','result_sha256'),
                            ('receipt','run_id'), ('receipt','report_page_id'),
                            ('request_receipt','request_sha256'), ('outbox','summary')]:
            with self.subTest(target=target, key=key):
                changed=deepcopy(job); changed[target][key]='different';self.svc.save(changed)
                with self.assertRaises(DuplicateRequestId):
                    Patrol.requests(patrol, [req])
        for key, value in [('report_state','unknown'),('execution_state','executing'),
                           ('resource_state','loaded')]:
            self.svc.save({**job,key:value})
            self.assertFalse(received_before_patrol(self.svc.cfg,req,reports))
        self.svc.save(job)
        for field in ('block_id','text','digest','reporter'):
            changed=deepcopy(reports[0]);setattr(changed,field,'different')
            self.assertFalse(received_before_patrol(self.svc.cfg,req,[changed]))
        self.assertFalse(received_before_patrol(self.svc.cfg,req,reports*2))
        changed=deepcopy(req);changed.text=changed.text.replace('local-run=', 'other-run=')
        self.assertFalse(received_before_patrol(self.svc.cfg,changed,reports))
        changed=deepcopy(req);changed.block_id='different'
        self.assertFalse(received_before_patrol(self.svc.cfg,changed,reports))

    def test_uncertain_append_never_retries_blindly(self):
        self.active();self.fake.ambiguous=True
        self.assertEqual(self.svc.report(P)['report_state'],'unknown')
        self.fake.ambiguous=False
        self.assertEqual(self.svc.report(P)['report_state'],'unknown');self.assertEqual(self.fake.posts,1)
    def test_inspection_waits_for_verified_report_and_release(self):
        self.active();self.svc.report(P);self.svc.register(M)
        self.assertIsNone(self.svc.tick()['selected_run'])
        j=self.svc.load(A['run_id']);j.update(execution_state='finished',resource_state='released');self.svc.save(j)
        self.assertEqual(self.svc.tick()['selected_run'],M['run_id'])
        self.assertFalse(self.svc.runnable(j))
    def test_only_one_spawn_crosses_the_actor_claim(self):
        self.svc.register(A);self.svc.tick()
        shell=self.root/'actor';shell.write_text('#!/bin/sh\nexit 4\n');shell.chmod(0o700)
        with patch.object(self.svc,'command',return_value=[str(shell)]):
            result=self.svc.execute(A['run_id'])
            self.assertEqual(result['execution_state'],'reconcile')
            self.assertEqual(result['resource_state'],'released')
            with self.assertRaises(SafetyError):self.svc.execute(A['run_id'])
            self.assertIsNone(self.svc.tick()['selected_run'])
    def test_external_heavy_owner_or_lease_prevents_running(self):
        self.svc.register(A);self.svc.available=lambda:False
        self.assertEqual(self.svc.tick()['reason'],'heavy_model_busy');self.assertFalse(self.audit)
        self.svc.available=lambda:True
        with exclusive(self.svc.base/'actor.lock'):
            self.assertIsNone(self.svc.tick()['selected_run'])
        with exclusive(self.root/'heavy'):
            self.assertFalse(heavy_free(self.root/'heavy'))
        self.assertTrue(heavy_free(self.root/'heavy'))
    def test_report_rejects_unregistered_identity_and_does_not_spoof_receipt(self):
        self.active();self.fake.blocks[self.page].append(block('【完了報告】\n依頼ID：'+A['request_id']+'\n報告した子：みき\n結果：完了\nやったこと：別の報告'))
        self.assertNotEqual(self.svc.report(P)['report_state'],'received');self.assertFalse(self.fake.posts)
        with self.assertRaises(SafetyError):self.svc.report({**P,'actor':'mamu'})

    def test_poll_only_picks_staged_new_requests_and_does_not_repeat(self):
        from riri.local_tasks import kick_if_pending
        from riri.parse import parse_requests
        reqs=parse_requests([block(request(A))])
        with patch('riri.local_tasks.clean_subprocess') as wake:
            kick_if_pending(self.svc.cfg,reqs);wake.assert_not_called()
            self.svc.stage(A)
            kick_if_pending(self.svc.cfg,reqs);self.assertEqual(wake.call_count,1)
            self.assertEqual(self.svc.load(A['run_id'])['execution_state'],'queued')
            kick_if_pending(self.svc.cfg,reqs)
            self.assertEqual(len(self.svc.rows()),1)

    def test_late_receipt_after_driver_exit_without_regeneration(self):
        self.active();j=self.svc.load(A['run_id']);j.update(execution_state='reconcile',driver_pid=None,resource_state='released');self.svc.save(j)
        r=self.svc.report(P)
        self.assertEqual((r['execution_state'],r['report_state']),('finished','received'))
        self.assertEqual(self.audit,[])

    def test_cancel_and_timeout_touch_only_owned_light_actor(self):
        import subprocess,sys,time
        self.svc.register(A)
        self.assertEqual(self.svc.cancel(A['run_id'],A['request_id'])['execution_state'],'cancelled')
        self.assertIsNone(self.svc.tick()['selected_run'])
        unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(12)'])
        try:
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(12)'],stderr=subprocess.DEVNULL)
            self.assertNotEqual(self.svc.wait_actor(child,A['run_id'],seconds=.01),0)
            self.assertIsNone(unrelated.poll())
            self.assertEqual(self.svc.load(A['run_id'])['stop_reason'],'actor_timeout')
        finally:
            unrelated.terminate();unrelated.wait()

    def test_earliest_dependency_waiter_does_not_starve_later_eligible_job(self):
        self.active();self.svc.report(P);self.svc.register(M)
        j=self.svc.load(A['run_id']);j.update(execution_state='queued',driver_pid=None,queued_at='9999-12-31T12:00:00+00:00');self.svc.save(j)
        self.assertEqual(self.svc.tick()['selected_run'],A['run_id'])
        self.assertEqual(self.svc.load(M['run_id'])['last_issue'],'waiting_for_prior_report_or_release')

    def interrupted(self):
        from riri import local_tasks as t
        svc=self.svc
        svc.register(A)
        old=svc.load(t.ORIGINAL)
        old.update(execution_state='reconcile',resource_state='released',stop_reason='cancelled',child_exit=-2,
                   ended_at='2026-11-15',cancel_requested_at='2026-11-15',driver_pid=2**30,child_pid=2**30)
        svc.save(old)
        (svc.base/(t.ORIGINAL+'.actor-output')).touch()
        fake=self.root/'miki'
        patcher=patch('riri.local_tasks.MIKI',fake);patcher.start();self.addCleanup(patcher.stop)
        model=fake/'data/development/gemma26'
        directory=model/'model-runs'/t.ORIGINAL; directory.mkdir(parents=True)
        (model/'jobs').mkdir();(model/'journal').mkdir()
        (fake/'miki').mkdir()
        for name,data in {'start-miki.sh':'fixed','miki/cli.py':'--continue-from','miki/__init__.py':'__version__ = "0.4.1"',
                          'miki/gemma_development.py':'new host'}.items():
            (fake/name).write_text(data)
        files={f.relative_to(fake).as_posix():t.digest(f.read_bytes()) for f in fake.rglob('*') if f.is_file()}
        manifest=fake/'install-manifest.json';manifest.write_text(json.dumps({'files':files}))
        (model/'jobs'/(t.ORIGINAL+'.json')).write_text(json.dumps({'repo_root':str(fake),'binding':{'code_sha256':{'gemma_development.py':'old'}}}))
        (model/'journal'/(t.ORIGINAL+'.json')).write_text(json.dumps({'current_hashes':{'start-miki.sh':files['start-miki.sh']}}))
        for i in range(1,11):
            for suffix in (['intent','generation','result'] if i<10 else ['intent']):
                (directory/f'turn-{i:02d}.{suffix}.json').write_text('{"worker_pid":1073741824}')
        hashes={f.relative_to(model).as_posix():t.digest(f.read_bytes()) for f in model.rglob('*.json')}
        for k,v in [('ORIGINAL_EVIDENCE',t.digest(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode())),
                    ('ORIGINAL_LEDGER',t.digest(svc.path(t.ORIGINAL).read_bytes()))]:
            p=patch.object(t,k,v);p.start();self.addCleanup(p.stop)
        return {'request_id':t.REQUEST,'prior_run':t.ORIGINAL,'run_id':t.CONTINUED,
                'actor_manifest_sha256':t.digest(manifest.read_bytes())},directory

    def test_only_explicit_continuation_once_same_formal_request_and_budget(self):
        c,_=self.interrupted()
        svc=self.svc
        parent=svc.path(A['run_id']).read_bytes()
        for fn in [svc.stage,svc.register]:
            with self.assertRaises(SafetyError):fn({**A,**c})
        with exclusive(svc.base/'actor.lock'):
            with self.assertRaises(BlockingIOError):svc.continue_a(c)
        got=svc.continue_a(c)
        self.assertEqual(got['continuation']['calls_remaining'],2)
        self.assertEqual(got['request_receipt']['block_id'],svc.load(A['run_id'])['request_receipt']['block_id'])
        self.assertEqual(svc.continue_a(c),got)
        self.assertEqual(svc.tick()['selected_run'],c['run_id'])
        self.assertEqual(svc.command(svc.load(c['run_id']))[-2:],['--continue-from',A['run_id']])
        for fn,args in [(svc.report,[P]),(svc.cancel,[A['run_id'],A['request_id']])]:
            with self.assertRaises(SafetyError):fn(*args)
        self.assertEqual(parent,svc.path(A['run_id']).read_bytes())
        child=svc.load(c['run_id']);child['execution_state']='executing';svc.save(child)
        received=svc.report({**P,'run_id':c['run_id'],'quality':'partial'})
        self.assertEqual(received['receipt']['run_id'],c['run_id'])
        m=svc.register({**M,'prior_run':c['run_id']})
        self.assertEqual(m['prior_run'],c['run_id'])
        self.assertFalse(svc.runnable(m))
        child=svc.load(c['run_id']);child['resource_state']='released';svc.save(child)
        self.assertTrue(svc.runnable(m))
        self.assertEqual(parent,svc.path(A['run_id']).read_bytes())

    def test_continuation_rejects_new_results_and_changed_deployment(self):
        c,d=self.interrupted()
        with self.assertRaises(SafetyError):self.svc.continue_a({**c,'actor_manifest_sha256':'0'*64})
        self.assertFalse(self.svc.path(c['run_id']).exists())
        self.svc.continue_a(c)
        (d/'turn-10.generation.json').write_text('{}')
        with self.assertRaises(SafetyError):self.svc.command(self.svc.load(c['run_id']))
        self.assertEqual(self.fake.posts,0)

    def test_prior_official_report_blocks_explicit_continuation(self):
        c,_=self.interrupted()
        self.fake.blocks[self.page].append(block('【完了報告】\n依頼ID：'+A['request_id']+'\n報告した子：みき\n結果：一部完了\nやったこと：記録'))
        with self.assertRaises(SafetyError):self.svc.continue_a(c)
        self.assertFalse(self.svc.path(c['run_id']).exists())

    def test_diagnostic_report_cannot_enable_an_inspection(self):
        self.active()
        data={**P,'stage':'implementation_incomplete','quality':'unverified','summary':'候補を保存できませんでした。'}
        with self.assertRaises(SafetyError):self.svc.report({**data,'quality':'passed'})
        self.assertEqual(self.svc.report(data)['report_state'],'received')
        with self.assertRaises(SafetyError):self.svc.register(M)
