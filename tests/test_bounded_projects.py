from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from riri import bounded_projects as b, project_ops as o, local_tasks as t
from riri.errors import SafetyError
from riri.parse import parse_requests, parse_reports
from test_local_tasks import TasksTest, block, request


def digest(data):
    return hashlib.sha256(data).hexdigest()


class BoundedProjectsTest(unittest.TestCase):
    def setUp(self):
        fixture=TasksTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.svc=fixture.svc;self.fake=fixture.fake;self.page=fixture.page
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name).resolve();self.live=self.home/'actor'
        self.ref=self.home/'owner-record.md';self.ref.write_text('authorized scope from test operator')
        actor=Path(os.environ.get('RIRI_TEST_MIKI_SOURCE',str(t.MIKI))).resolve()
        self.enterContext(patch.object(b,'MIKI',actor))
        self.enterContext(patch.object(o,'registry',return_value={'みに':'🩵','まむ':'🧡','みき':'💜'}))
        self.enterContext(patch.object(b,'proof_dir',side_effect=lambda j:self.live/j['run_id']))

    def scope(self,name,n):
        root=self.home/name;root.mkdir()
        spec=(name+' task').encode();(root/'spec.md').write_bytes(spec);material={'spec.md':digest(spec)}
        job={'schema':1,'project':name,'job_id':name+'-v1','actor':'miki','mode':'review','materials':material,
             'spec_sha256':digest(spec),'write_files':[],'limits':{'runs':1,'seconds':80,'output_tokens':500}}
        return {'project':name,'project_url':'https://app.notion.com/p/'+str(n)*32,'spec_version':'v1','material_root':str(root),
                'materials':material,'spec_sha256':digest(spec),'actors':{'miki':['review']},'requesters':['みに'],
                'data_class':'synthetic','delegation_ref':str(self.ref),'delegation_sha256':digest(self.ref.read_bytes()),
                'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'jobs':[job]}

    def formal(self,s,n):
        job=s['jobs'][0];run='ops-'+str(n)*32;rid='🩵20261116-'+str(n)
        sha=b.candidate_api()[0](job)
        text=request({'actor':'miki','request_id':rid,'run_id':run}).replace(t.PROJECT,s['project_url'])
        text+=f" manifest-sha256={sha} project-id={s['project']}\nGitHubプロジェクト：なし"
        return block(text,i=str(n)*32),run,rid

    def intake(self,raw):
        self.fake.blocks[self.svc.cfg.requests_page]=raw
        rows=parse_requests(raw)
        return [o.patrol_intake(self.svc,r,rows) for r in rows]

    def test_two_projects_formal_normal_path_and_replay(self):
        a,c=self.scope('one',1),self.scope('two',2)
        for s in (a,c):self.assertFalse(b.install_scope(self.svc,s)['queued'])
        req,run,_=self.formal(a,1);other,r2,_=self.formal(c,2)
        self.assertEqual(self.intake([req,other]),[True,True])
        saved=self.svc.path(run).read_bytes()
        self.assertEqual(self.intake([req,other]),[True,True])
        self.assertEqual(saved,self.svc.path(run).read_bytes())
        r=self.svc.load(run);q=self.svc.load(r2)
        self.assertEqual(q['material_root'],c['material_root'])
        self.assertNotEqual(r['owner_receipt_sha256'],q['owner_receipt_sha256'])
        self.assertNotEqual(b.proof_dir(r),b.proof_dir(q))
        self.assertEqual(self.svc.command(r)[1:3],['bounded-run','--job'])
        self.assertEqual(self.svc.tick()['selected_run'],run)
        # An occupied shared lock prevents either formal job from being selected.
        self.svc.available=lambda:False
        self.assertEqual(self.svc.tick()['reason'],'heavy_model_busy')
        self.assertEqual(r['worker_seconds'],80);self.assertEqual(r['deadline'],a['expires_at'])

    def test_tamper_expiry_forged_delegation_and_crossover(self):
        a,c=self.scope('one',1),self.scope('two',2)
        b.install_scope(self.svc,a)
        raw,run,_=self.formal(a,1)
        for src,dst in [('project-id=one','project-id=two'),('GitHubプロジェクト：なし','GitHubプロジェクト：x/y'),
                        (a['project_url'],c['project_url']),('project-id=one','owner_approved=true'),('project-id=one','project-id=one parent-request=🩵20261115-1')]:
            changed=block(raw['code']['rich_text'][0]['plain_text'].replace(src,dst))
            self.assertEqual(self.intake([changed]),[False]);self.assertFalse(self.svc.path(run).exists())
        with self.assertRaises(SafetyError):b.install_scope(self.svc,{**a,'data_class':'owner_approved'})
        with self.assertRaises(SafetyError):b.install_scope(self.svc,{**a,'expires_at':'2001-01-01T01:00:00+00:00'})
        self.assertEqual(self.intake([raw]),[True])
        claimed=self.svc.load(run)
        self.assertEqual(claimed['material_root'],a['material_root'])
        with patch.object(b,'datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(days=1)
            with self.assertRaisesRegex(SafetyError,'expired'):self.svc.command(claimed)
        (Path(a['material_root'])/'spec.md').write_text('different')
        with self.assertRaises(ValueError):self.svc.command(claimed)
        c['material_root']=a['material_root']
        with self.assertRaises((SafetyError,ValueError)):b.install_scope(self.svc,c)

    def test_distinct_append_only_job_preserves_scope_and_consumed_cap(self):
        a=self.scope('one',1);first=b.install_scope(self.svc,a)
        raw,run,_=self.formal(a,1);self.assertEqual(self.intake([raw]),[True])
        renamed=deepcopy(a);renamed['jobs'][0]['job_id']='one-v2'
        second=b.install_scope(self.svc,renamed)
        self.assertNotEqual(first['owner_receipt_sha256'],second['owner_receipt_sha256'])
        self.assertEqual(b.install_scope(self.svc,a),first)
        raw2,r2,_=self.formal(renamed,2)
        self.assertEqual(self.intake([raw,raw2]),[True,False])
        self.assertFalse(self.svc.path(r2).exists());self.assertTrue(self.svc.path(run).exists())
        self.assertIn('budget',t.read_json(o.keyed(self.svc,r2,'.ops-hold.json'))['reason'])
        with self.assertRaises(SafetyError):b.install_scope(self.svc,{**a,'max_jobs':2})
        swapped,_,_=self.formal(a,3)
        self.assertEqual(self.intake([swapped]),[False])
        self.assertIn('already reserved',t.read_json(o.keyed(self.svc,'ops-'+'3'*32,'.ops-hold.json'))['reason'])

    def test_local_proofs_offline_delivery_release_and_no_second_append(self):
        a=self.scope('one',1);b.install_scope(self.svc,a)
        raw,run,rid=self.formal(a,1);self.assertEqual(self.intake([raw]),[True])
        j=self.svc.load(run);j.update(execution_state='executing',resource_state='owned',child_pid=92,driver_pid=91)
        self.svc.save(j);folder=b.proof_dir(j);folder.mkdir(parents=True)
        def save(name,data):
            path=folder/name;path.write_text(json.dumps(data));return digest(path.read_bytes())
        content={'verdict':'no_finding','findings':[],'not_executed':True}
        identity={'route':b.ROUTE,'project_id':'one','manifest_sha256':j['manifest_sha256'],'mode':'review'}
        gen=save('generation.json',{'run_id':run,'job_run_id':run,'text':json.dumps(content),**identity,'profile_id':'gemma-4-26b-a4b-it-qat-4bit-bounded','model_path':str(b.MIKI/'models/gemma-4-26B-A4B-it-qat-4bit'),'request_id':rid,'worker_pid':2000000000,
           'repo_id':'mlx-community/gemma-4-26B-A4B-it-qat-4bit','model_revision':'0e3cbab38ce568cf6e23543010d08d03b731910c','truncated':False})
        save('intent.json',{**identity,'owner_receipt_sha256':j['owner_receipt_sha256'],'request_id':rid,'job_run_id':run,'request_block_id':j['request_receipt']['block_id'],'cli_pid':92,'driver_pid':91,
                            'manifest_sha256':j['manifest_sha256'],'job_sha256':j['ops_job_sha256']})
        record={'schema':1,'route':b.ROUTE,'run_id':run,'request_id':rid,'project_id':'one','job_sha256':j['manifest_sha256'],
                'actor':'miki','mode':'review','kind':'review','status':'structured','generation_sha256':gen,
                'content':content}
        h=save('artifact.json',record)
        packet={'actor':'miki','mode':'review','project_id':'one','request_id':rid,'run_id':run,'artifact_sha256':h,'result_sha256':gen,
                'stage':'bounded_reviewed','quality':'unverified','summary':'読取り所見を保存。','worker_pid':2000000000}
        save('outbox.json',packet)
        with self.assertRaisesRegex(SafetyError,'worker exit'):o.report(self.svc,packet)
        save('result.json',{'terminal':True,'run_id':run,'request_id':rid,'resource_state':'released','lock_after':'free',
                            'runtime':{'stats':{'worker_exited':True,'worker_pid':2000000000}}})
        bad=deepcopy(record);bad['content']['verdict']='finding'
        altered={**packet,'artifact_sha256':save('artifact.json',bad)}
        save('outbox.json',altered)
        with self.assertRaisesRegex(SafetyError,'original reply'):o.report(self.svc,altered)
        save('artifact.json',record);save('outbox.json',packet)
        self.fake.break_read=True
        self.assertEqual(o.report(self.svc,packet)['report_state'],'pending')
        self.fake.break_read=False
        self.assertEqual(o.report(self.svc,packet)['report_state'],'received')
        self.assertEqual(o.report(self.svc,packet)['receipt']['result_sha256'],gen)
        self.assertEqual(self.fake.posts,1)
        got=parse_reports(self.fake.blocks[self.page],expected_reporter='みき')[0]
        self.assertIn(a['project_url'],got.text);self.assertNotIn(t.PROJECT,got.text)
        self.assertNotIn('この版をまむが検査',got.text)
        j=self.svc.load(run);self.assertTrue(self.svc.release_confirmed(j))
        (folder/'result.json').unlink();self.assertFalse(self.svc.release_confirmed(j))
        self.assertTrue(o.patrol_intake(self.svc,parse_requests([raw])[0],parse_requests([raw])))

    def test_normal_patrol_holds_missing_markers(self):
        from types import SimpleNamespace
        from riri.patrol import Patrol
        a=self.scope('one',1);b.install_scope(self.svc,a)
        raw,run,rid=self.formal(a,1)
        text=raw['code']['rich_text'][0]['plain_text']
        holds=[]
        p=SimpleNamespace(config=self.svc.cfg,state={'requests':{},'outbox':{}},invalid_report_ids=set(),instruction_count=0,
                          post_count=0,current_reports=[],request_problem=lambda r,e:holds.append(str(e)))
        Patrol.requests(p,parse_requests([block(text.replace('local-run=','lost-run='))]))
        self.assertEqual(len(holds),1);self.assertIn('local-run',holds[0]);self.assertFalse(self.svc.path(run).exists())

    def test_archived_worker_recovery_is_readback_only_and_binds_both_pids_and_sha(self):
        a=self.scope('one',1);b.install_scope(self.svc,a)
        raw,run,rid=self.formal(a,1);self.assertEqual(self.intake([raw]),[True])
        j=self.svc.load(run);cli=2000000002;worker=2000000003
        j.update(child_pid=cli,driver_pid=2000000001,execution_state='reconcile',resource_state='release_unconfirmed')
        self.svc.save(j);folder=b.proof_dir(j);folder.mkdir(parents=True)
        def save(name,data):
            f=folder/(name+'.json');f.write_text(json.dumps(data));return digest(f.read_bytes())
        stamp={'route':b.ROUTE,'project_id':'one','mode':'review','request_id':rid,'manifest_sha256':j['manifest_sha256']}
        gsha=save('generation',{**stamp,'run_id':run,'job_run_id':run,'worker_pid':worker})
        isha=save('intent',{**stamp,'job_run_id':run,'cli_pid':cli,'driver_pid':j['driver_pid'],
                  'request_block_id':j['request_receipt']['block_id'],'job_sha256':j['ops_job_sha256']})
        event={'request_id':rid,'run_id':run,'worker_pid':worker,'result_sha256':gsha}
        psha=save('pending',event)
        with patch.object(t,'heavy_free',return_value=False),patch('subprocess.Popen') as spawn:
            with self.assertRaisesRegex(SafetyError,'still present'):b.recover_saved_once(self.svc,j)
            spawn.assert_not_called()
        with patch.object(t,'heavy_free',return_value=True),patch('subprocess.Popen') as spawn:
            b.recover_saved_once(self.svc,j);b.recover_saved_once(self.svc,j)
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(spawn.call_args.args[0][1:],['bounded-submit','--run-id',run])
            self.assertFalse(o.release_evidence(j,worker,gsha))
            release={'run_id':run,'request_id':rid,'worker_pid':worker,'cli_pid':cli,'result_sha256':gsha,
                     'pending_sha256':psha,'intent_sha256':isha,'lock_after':'free','terminal':True,'resource_state':'released'}
            save('release-reconciliation',{**release,'cli_pid':cli+1})
            self.assertFalse(o.release_evidence(j,worker,gsha))
            save('release-reconciliation',release)
            self.assertTrue(o.release_evidence(j,worker,gsha))
            self.assertFalse(o.release_evidence(j,worker,'0'*64))
            save('pending',{**event,'run_id':'other'})
            self.assertFalse(o.release_evidence(j,worker,gsha))
