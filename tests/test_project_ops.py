import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from riri import project_ops as o
from riri import local_tasks as t
from riri.errors import SafetyError
from riri.parse import parse_requests
from test_local_tasks import TasksTest, block, request

H='4'*64
RUN='ops-'+'1'*32
RID='🩵20261116-1'


class ProjectOpsTest(unittest.TestCase):
    def setUp(self):
        f=TasksTest();f.setUp();self.addCleanup(f.doCleanups)
        self.svc=f.svc;self.api=f.fake;self.tmp=f.root
        area=self.tmp/'project';self.target=area/'candidate/miki/a_check_summary.py'
        self.target.parent.mkdir(parents=True);spec=area/'materials/spec.md';spec.parent.mkdir();spec.write_text('fixed')
        self.m={'project_id':'project-sample','project_url':t.PROJECT,'spec_version':'1','spec_sha256':t.digest(spec.read_bytes()),
                'spec_path':str(spec),'work_root':str(area/'candidate'),'allowed_files':['miki/a_check_summary.py'],
                'repo_url':'https://github.com/icesunaco-eng/miki-ai','acceptance':{'case_count':1,'verify_sha256':'d'*64},'reply_to':'みに','limits':o.LIMITS}
        for name,value in [('AREA',area),('MIKI',self.tmp/'miki'),('MAMU',self.tmp/'mamu')]:
            p=patch.object(o,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(o,'plan',return_value=self.m);p.start();self.addCleanup(p.stop)
        p=patch.object(o,'registry',return_value={'みに':'🩵','みき':'💜','まむ':'🧡'});p.start();self.addCleanup(p.stop)
        self.stage={'manifest_sha256':H,'run_id':RUN,'request_id':RID,'requester':'みに'}
        self.text=request({'actor':'miki','request_id':RID,'run_id':RUN})+' manifest-sha256='+H+' project-id=project-sample\nGitHubプロジェクト：icesunaco-eng/miki-ai'
        self.rows=parse_requests([block(self.text)])
        self.api.blocks[self.svc.cfg.requests_page]=[block(self.text)]
        self.api.blocks[self.svc.mail.report_page('mamu')]=[block('報告一覧（新しい順）','heading_2')]
        self.svc.setup()

    def accept(self):
        o.stage(self.svc,self.stage)
        o.patrol_intake(self.svc,self.rows[0],self.rows)
        return self.svc.load(RUN)

    def completed_output(self):
        j=self.accept();j.update(execution_state='executing',resource_state='owned',started_at=t.now(),child_pid=100,driver_pid=99);self.svc.save(j)
        folder=o.actor_dir(j);f=folder/'artifact'/j['allowed_files'][0];f.parent.mkdir(parents=True)
        self.target.write_bytes(b'candidate');f.write_bytes(self.target.read_bytes());g=folder/'generation.json'
        g.write_text(json.dumps({'request_id':RID,'run_id':RUN,'job_run_id':RUN,'worker_pid':2000000000,'repo_id':'mlx-community/gemma-4-26B-A4B-it-qat-4bit','model_revision':'0e3cbab38ce568cf6e23543010d08d03b731910c'}))
        (folder/'intent.json').write_text(json.dumps({'cli_pid':100,'driver_pid':99,'request_block_id':j['request_receipt']['block_id'],'job_sha256':j['ops_job_sha256']}))
        checks=folder/'checks.json';checks.write_text(json.dumps({'ok':True,'execution_ok':True,'passed':1,'total':1,'cases':[{'id':'one','passed':True}],
                                                                'artifact_sha256':t.digest(f.read_bytes()),'verify_sha256':'d'*64}))
        release=patch.object(o,'release_evidence',return_value=True);release.start();self.addCleanup(release.stop)
        p={'op':'ops-report','checks_sha256':t.digest(checks.read_bytes()),'worker_pid':2000000000,'actor':'miki','request_id':RID,'run_id':RUN,'stage':'inspection_wait','quality':'pending',
           'artifact_sha256':t.digest(f.read_bytes()),'result_sha256':t.digest(g.read_bytes()),'summary':'本人出力を保存。'}
        (folder/'outbox.json').write_text(json.dumps(p))
        return j,p

    def test_registration_formal_readback_cap_and_stale_or_missing_material_holds(self):
        o.patrol_intake(self.svc,self.rows[0],self.rows)
        self.assertFalse(self.svc.path(RUN).exists())
        j=self.accept();self.assertEqual(j['request_receipt']['block_id'],self.rows[0].block_id)
        self.assertEqual(t.LocalTasks.public(j)['parent_request_id'],RID)
        self.assertEqual(self.svc.command(j)[1],'ops-run')
        before=self.svc.path(RUN).read_bytes();o.patrol_intake(self.svc,self.rows[0],self.rows)
        self.assertEqual(before,self.svc.path(RUN).read_bytes())
        with self.assertRaises(SafetyError):o.stage(self.svc,{**self.stage,'requester':'まむ'})
        with self.assertRaises(SafetyError):o.stage(self.svc,{**self.stage,'run_id':'ops-'+'2'*32})
        changed=parse_requests([block(self.text.replace('実行','異なる仕事'))])
        o.patrol_intake(self.svc,changed[0],changed)
        self.assertEqual(self.svc.load(RUN)['execution_state'],'held')
        self.assertTrue(o.keyed(self.svc,RUN,'.ops-hold.json').exists())

    def test_new_child_once_original_run_release_and_independent_result_return(self):
        j,p=self.completed_output();draft={'request_id':'💜20261116-1','save_state':'saved_verified'}
        with patch.object(o,'child_relay',return_value=draft) as relay:
            r=o.report(self.svc,p)
            self.assertEqual(r['report_state'],'received');self.assertEqual(r['resource_state'],'owned')
            self.assertEqual(r['next_request_id'],draft['request_id'])
            self.assertEqual(o.report(self.svc,p)['receipt'],r['receipt']);relay.assert_called_once()
            o.reconcile_children(self.svc,self.svc.rows());relay.assert_called_once()
            o.update(self.svc,RUN,execution_state='finished',resource_state='released')
            o.reconcile_children(self.svc,self.svc.rows());o.reconcile_children(self.svc,self.svc.rows());self.assertEqual(relay.call_count,2)
        child=o.read_json(o.keyed(self.svc,r['next_run'],'.ops-stage.json'));self.assertEqual(child['step'],1)
        ct=request({'actor':'mamu','run_id':child['run_id'],'request_id':child['request_id']})+' manifest-sha256='+H+' project-id=project-sample\nGitHubプロジェクト：icesunaco-eng/miki-ai'
        rows=parse_requests([block(ct,i='e'*32)]);o.patrol_intake(self.svc,rows[0],rows)
        cj=self.svc.load(child['run_id']);self.assertTrue(self.svc.runnable(cj))
        self.assertEqual(cj['deadline'],j['deadline']);self.assertEqual(cj['artifact_sha256'],p['artifact_sha256'])
        o.update(self.svc,cj['run_id'],execution_state='executing',resource_state='owned',started_at=t.now())
        folder=o.actor_dir(cj);folder.mkdir(parents=True)
        data={'run_id':cj['run_id'],'request_id':cj['request_id'],'artifact_sha256':p['artifact_sha256'],'spec_sha256':cj['spec_sha256'],'prior_run_id':RUN,'generated':{'lifecycle':{'pid':2000000000}}}
        result=folder/'result.json';result.write_text(json.dumps(data))
        c={**data,'op':'ops-report','actor':'mamu','stage':'reviewed','quality':'unassessed','result_sha256':t.digest(result.read_bytes()),'summary':'検査意見。'}
        (folder/'outbox.json').write_text(json.dumps(c));cr=o.report(self.svc,c);self.assertEqual(cr['report_state'],'received')
        o.update(self.svc,cj['run_id'],execution_state='finished',resource_state='released')
        ack=cr['receipt'];queued={}
        def queue(k,ch,txt,kind):
            return queued.setdefault(k,{'status':'posted','channel':ch,'ts':'1800000000.000001'})
        patrol=SimpleNamespace(config=self.svc.cfg,state={'seen_reports':{ack['block_id']:ack['report_sha256']}},queue=queue)
        o.return_results(patrol);o.return_results(patrol);self.assertEqual(len(queued),2)
        rr=self.svc.load(RUN)['return_receipt']
        self.assertEqual((rr['recipient'],rr['state'],rr['recipient_read']),('みに','delivered','unconfirmed'))

    def test_source_hash_changed_no_formal_write_or_child_and_deadline_blocks_execution(self):
        j,p=self.completed_output();self.target.write_bytes(b'altered')
        with patch.object(o,'child_relay') as relay:
            with self.assertRaises(SafetyError):o.report(self.svc,p)
            relay.assert_not_called()
        self.assertEqual(self.api.posts,0)
        o.update(self.svc,RUN,execution_state='queued',deadline='2000-01-01T00:00:00+00:00')
        o.reconcile_children(self.svc,self.svc.rows());self.assertFalse(self.svc.runnable(self.svc.load(RUN)))
        self.assertEqual(self.svc.load(RUN)['last_issue'],'parent_budget_expired')

    def test_stop_and_formal_removal_and_oversized_material_hold(self):
        j=self.accept();self.api.blocks[self.svc.cfg.requests_page]=[]
        with self.assertRaises(SafetyError):self.svc.command(j)
        self.svc.cfg.stop.touch()
        with self.assertRaises(o.StopRequested):o.stage(self.svc,self.stage)
        self.assertFalse(self.svc.load(RUN).get('started_at'))
        self.svc.cfg.stop.unlink()
        base=self.text.replace('やること：実行','やること：'+'文'*3001)
        rows=parse_requests([block(base)])
        self.assertFalse(o.patrol_intake(self.svc,rows[0],rows))
        self.assertFalse(self.svc.runnable(self.svc.load(RUN)))

    def test_unknown_report_readback_no_repeat_append_and_cancel_stops(self):
        j,p=self.completed_output();self.api.ambiguous=True
        with patch.object(o,'child_relay',return_value={'request_id':'💜20261116-1'}):
            self.assertEqual(o.report(self.svc,p)['report_state'],'unknown')
            self.api.ambiguous=False
            self.assertEqual(o.report(self.svc,p)['report_state'],'unknown')
        self.assertEqual(self.api.posts,1)
        self.svc.cancel(RUN,RID)
        self.assertIsNotNone(self.svc.load(RUN)['cancel_requested_at'])
