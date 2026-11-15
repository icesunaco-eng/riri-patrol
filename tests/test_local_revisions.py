import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_local_tasks as helpers

from riri import local_revisions as rv
from riri import local_tasks as t
from riri.errors import RemoteError, SafetyError


class MutableNotion(helpers.Fake):
    def __init__(self,base):
        super().__init__()
        self.blocks=base.blocks
        self.patches=0
        self.fail=None

    def _request(self,method,uid,**kw):
        if kw.get('children'):
            result=super()._request(method,uid,**kw)
            block=self.blocks[uid][-1]
            block.update(object='block',in_trash=False,archived=False,last_edited_time='2026-11-15T01:00:01Z')
            return result
        block=next(b for page in self.blocks.values() for b in page if b['id']==uid and 'last_edited_time' in b)
        if method=='GET':
            return block
        assert method=='PATCH'
        self.patches+=1
        if self.fail=='reject':
            raise RemoteError('confirmed rejection',uncertain=False)
        if self.fail!='unknown_before':
            block['code']=kw['payload']['code']
            block['last_edited_time']='2026-11-15T01:00:02Z'
        if self.fail in ('unknown_before','unknown_after'):
            raise RemoteError('uncertain PATCH',uncertain=True)
        return block


class RevisionsTest(unittest.TestCase):
    def setUp(self):
        fixture=helpers.TasksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root,self.svc=fixture.root,fixture.svc
        p=patch.object(rv,'MIKI',self.root/'miki');p.start();self.addCleanup(p.stop)
        self.api=MutableNotion(fixture.fake)
        self.svc.mail.notion=self.api
        self.svc.register(helpers.A)
        a=self.svc.load(t.ORIGINAL)
        a.update(execution_state='reconcile',resource_state='released');self.svc.save(a)
        a.update(run_id=t.CONTINUED,prior_run=t.ORIGINAL,execution_state='executing')
        self.svc.save(a)
        self.old_artifact=t.digest(b'old reviewed candidate')
        self.svc.report({**helpers.P,'artifact_sha256':self.old_artifact,'run_id':t.CONTINUED})
        a=self.svc.load(t.CONTINUED);a.update(execution_state='finished',resource_state='released');self.svc.save(a)
        self.old=t.LocalTasks.public(a)['receipt']
        self.review=helpers.M['run_id']
        page=self.svc.mail.report_page('mamu')
        self.api.blocks[page]=[helpers.block('報告一覧（新しい順）','heading_2',i='e'*32)]
        self.svc.register({**helpers.M,'prior_run':t.CONTINUED,'artifact_sha256':self.old_artifact})
        m=self.svc.load(self.review);m['execution_state']='executing';self.svc.save(m)
        self.svc.report({**helpers.P,'artifact_sha256':self.old_artifact,'run_id':self.review,'request_id':m['request_id'],'actor':'mamu','stage':'reviewed','quality':'unassessed'})
        self.api.blocks[page][-1]['id']='b'*32
        m=self.svc.load(self.review);m['receipt']['block_id']='b'*32
        m.update(execution_state='finished',resource_state='released');self.svc.save(m)
        self.diag=self.root/'diagnostic.json'
        self.diag.write_text(json.dumps({'source_sha256':self.old['artifact_sha256'],'spec_sha256':a['spec_sha256'],
                                'first_opinion_run':self.review,'first_opinion_result_sha256':m['receipt']['result_sha256'],
                                'recorded_at':'2100-01-01T00:00:00+00:00',
                                'tested_cases':[{'case':'suffix','expected':'error','observed':'returned','matches':False}]}))
        p=patch.object(rv,'DIAGNOSTIC',self.diag);p.start();self.addCleanup(p.stop)
        self.prepare={'op':'prepare-amendment','run_id':t.REVISION,'prior_run':t.CONTINUED,'request_id':t.REQUEST,
                      'review_run':self.review,'diagnostic_sha256':t.digest(self.diag.read_bytes())}
        self.amend={**self.prepare,'op':'amend','expected_report_sha256':self.old['report_sha256'],'judgment_sha256':'d'*64}
        self.findings={'reason':'固定した入力の追試で不一致が確認されたため。','artifact_sha256':t.digest(b'revised candidate'),'result_sha256':'2'*64,
                       'actor_job_sha256':'3'*64,'quality':'partial'}

    def amend_fake_actor(self):
        with patch.object(rv,'check_actor',return_value=self.findings):
            return rv.register_amendment(self.svc,self.amend)

    def test_preparation_archives_originals_without_job_queue_or_edit(self):
        initial=self.svc.path(t.CONTINUED).read_bytes()
        a=rv.prepare(self.svc,self.prepare)
        self.assertEqual(a,rv.prepare(self.svc,self.prepare))
        self.assertEqual(a['state'],'awaiting_new_budget')
        self.assertFalse(self.svc.path(t.REVISION).exists())
        self.assertFalse(self.svc.tick()['selected_run'])
        self.assertEqual(self.api.patches,0)
        self.assertEqual(initial,self.svc.path(t.CONTINUED).read_bytes())
        with self.assertRaises(SafetyError):rv.prepare(self.svc,{**self.prepare,'diagnostic_sha256':'0'*64})
        with self.assertRaises((SafetyError,FileNotFoundError)):rv.register_amendment(self.svc,self.amend)

    def test_one_block_two_durable_versions_and_same_request_reinspection(self):
        rv.prepare(self.svc,self.prepare)
        frozen=rv.read_json(rv.correction_path(self.svc))
        old=self.svc.path(t.CONTINUED).read_bytes()
        saved=self.amend_fake_actor()
        self.assertEqual(saved['report_state'],'received')
        self.assertEqual(saved['receipt']['block_id'],self.old['block_id'])
        self.assertEqual(saved['receipt']['version'],2)
        self.assertEqual(saved['receipt']['previous_report_sha256'],self.old['report_sha256'])
        self.assertEqual(self.amend_fake_actor(),saved)
        self.assertEqual(self.api.patches,1)
        self.assertNotEqual(saved['receipt']['report_sha256'],self.old['report_sha256'])
        self.assertEqual(old,self.svc.path(t.CONTINUED).read_bytes())
        self.assertEqual(t.sha(frozen['miki']['body']),self.old['report_sha256'])
        rid=helpers.M['request_id'];new='mamu-a-review-'+'d'*32
        req={**self.amend,'op':'review-version','request_id':rid,'run_id':new,'prior_run':t.REVISION,
             'expected_report_sha256':frozen['mamu']['receipt']['report_sha256']}
        staged=self.root/'inspection';staged.mkdir();(staged/'input_validation.py').write_bytes(b'revised candidate')
        with patch.object(self.svc,'command',return_value=['fixed-inspector',str(staged/'job.json')]),patch.object(rv,'review_eligibility',return_value='e'*64):
            got=rv.register_amendment(self.svc,req)
            self.assertEqual(got['artifact_sha256'],self.findings['artifact_sha256'])
            self.assertTrue(self.svc.runnable(self.svc.load(new)))
            self.assertEqual(got['request_id'],helpers.M['request_id'])
            with self.assertRaises(SafetyError):rv.register_amendment(self.svc,{**req,'run_id':'mamu-a-review-'+'a'*32})
        row=self.svc.load(new);row['execution_state']='executing';self.svc.save(row)
        with patch.object(rv,'mamu_report_evidence',return_value=None):
            ack=self.svc.report({**helpers.P,'run_id':new,'request_id':rid,'actor':'mamu','stage':'reviewed','quality':'unassessed',
                             'artifact_sha256':self.findings['artifact_sha256'],'result_sha256':'8'*64})
        self.assertEqual(ack['report_state'],'received')
        self.assertEqual(ack['receipt']['block_id'],frozen['mamu']['receipt']['block_id'])
        self.assertEqual(self.api.patches,2)

    def test_network_unknown_then_observed_success_only_reads(self):
        rv.prepare(self.svc,self.prepare);self.api.fail='unknown_after'
        self.assertEqual(self.amend_fake_actor()['report_state'],'unknown')
        self.assertEqual(self.svc.deliver(t.REVISION)['report_state'],'received')
        self.assertEqual(self.api.patches,1)

    def test_uncertain_unchanged_never_retries_patch(self):
        rv.prepare(self.svc,self.prepare);self.api.fail='unknown_before'
        self.assertEqual(self.amend_fake_actor()['report_state'],'unknown')
        self.api.fail=None
        self.assertEqual(self.svc.deliver(t.REVISION)['report_state'],'unknown')
        self.assertEqual(self.api.patches,1)

    def test_source_conflict_and_confirmed_rejection(self):
        rv.prepare(self.svc,self.prepare);self.api.fail='reject'
        self.assertEqual(self.amend_fake_actor()['report_state'],'pending')
        self.api.fail=None
        block=self.api.blocks[self.svc.mail.report_page('miki')][-1]
        block['last_edited_time']='2026-11-16T01:00:02Z'
        self.assertNotEqual(self.svc.deliver(t.REVISION)['report_state'],'received')
        self.assertEqual(self.api.patches,1)

    def test_actor_proof_binds_new_authorization_real_turn_and_separate_copy(self):
        rv.prepare(self.svc,self.prepare)
        frozen=rv.read_json(rv.correction_path(self.svc))
        copy=self.root/'copies/A-rev1';target=copy/'qbench/input_validation.py';target.parent.mkdir(parents=True)
        target.write_text('def new_implementation():\n    return True\n');artifact=t.digest(target.read_bytes())
        fake=self.root/'miki'
        base=fake/'data/development/gemma26'
        for part in ('judgments','jobs','journal','model-runs/'+t.REVISION):
            (base/part).mkdir(parents=True)
        def save(path,obj):
            b=json.dumps(obj,ensure_ascii=False).encode();path.write_bytes(b);return t.digest(b)
        judgment=base/'judgments'/(t.REVISION+'.json')
        doc={k:frozen[k] for k in ('run_id','prior_run','request_id','review_run','diagnostic_sha256','spec_sha256')}
        doc.update(prior_artifact_sha256=self.old['artifact_sha256'],prior_result_sha256=self.old['result_sha256'],
                   review_result_sha256=frozen['mamu']['receipt']['result_sha256'],decision='revision_required',new_budget_approved=True,
                   reason='独立追試の不一致を修正。',budget_start='2098-01-01T00:00:00Z',budget_deadline='2098-01-01T00:02:00Z')
        judgment_hash=save(judgment,doc)
        mid=t.REVISION+'-turn-01-'+'a'*20
        folder=base/'model-runs'/t.REVISION;result=folder/(mid+'.result.json');generation=folder/(mid+'.generation.json');pid=2**30
        gen=save(generation,{'repo_id':rv.MODEL,'model_revision':rv.MODEL_REV,'job_run_id':t.REVISION,'run_id':mid,
                             'text':'model text','truncated':False,'worker_pid':pid})
        rh=save(result,{'ok':True,'repo_id':rv.MODEL,'model_revision':rv.MODEL_REV,'development_run_id':mid,'text':'model text',
                        'generation_sha256':gen,'truncated':False,'stats':{'worker_pid':pid,'worker_exited':True,'worker_returncode':0}})
        job={'run_id':t.REVISION,'formal_run_id':t.REVISION,'request_id':t.REQUEST,'prior_run':t.CONTINUED,'label':'A','mode':'implementation',
             'repo_id':rv.MODEL,'revision':rv.MODEL_REV,'revision_judgment_sha256':judgment_hash,'terminal':True,'human_code_fixes':0,
             'model_patch_count':1,'repo_root':str(copy),'current_started_at':'2098-01-01T00:00:10Z','ended_at':'2098-01-01T00:00:30Z',
             'host_checks_passed':True,'artifacts':[{'path':str(target),'sha256':artifact,'checked_source':gen}],
             'model_runs':[{'model_run_id':mid,'result':str(result),'generation':str(generation),'result_sha256':rh,
                            'worker_exited':True,'pid':pid}],'final_hashes':{'qbench/input_validation.py':artifact}}
        jobfile=base/'jobs'/(t.REVISION+'.json');save(jobfile,job)
        journal={'run_id':t.REVISION,'mock':False,'pending':None,'current_hashes':job['final_hashes'],
                 'events':[{'action':{'action':'apply'},'provenance':{'actor':'miki_full_source','revision':rv.MODEL_REV,'run_id':mid},
                            'result':{'sha256':job['final_hashes']}}]}
        save(base/'journal'/(t.REVISION+'.json'),journal)
        with patch.object(rv,'MIKI',fake),patch.object(rv,'COPY',copy):
            proof=rv.check_actor(frozen,judgment_hash)
            self.assertEqual(proof['result_sha256'],gen);self.assertEqual(proof['artifact_sha256'],artifact)
            job['ended_at']='2098-01-01T00:02:01Z';save(jobfile,job)
            with self.assertRaises(SafetyError):rv.check_actor(frozen,judgment_hash)
            job['ended_at']='2098-01-01T00:00:30Z';save(jobfile,job)
            target.write_text('host replacement')
            with self.assertRaises(SafetyError):rv.check_actor(frozen,judgment_hash)

    def active_source(self, *, run=t.REVISION):
        """Fake model output, no real worker or network is started by this fixture."""
        from datetime import datetime,timedelta
        rv.prepare(self.svc,self.prepare)
        frozen=rv.read_json(rv.correction_path(self.svc,run))
        start=frozen['recorded_at'];deadline=(datetime.fromisoformat(start)+timedelta(seconds=120)).isoformat()
        base=self.root/'miki/data/development/gemma26';copy=self.root/'copies'/('A-rev2' if run==t.REVISION2 else 'A-rev1')
        p1=patch.object(rv,'MIKI',self.root/'miki');p1.start();self.addCleanup(p1.stop)
        p2=patch.object(rv,'COPY',self.root/'copies/A-rev1');p2.start();self.addCleanup(p2.stop)
        self.svc.available=lambda:False
        def save(path,obj):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj));return t.digest(path.read_bytes())
        doc={k:frozen[k] for k in ('run_id','prior_run','request_id','review_run','diagnostic_sha256','spec_sha256')}
        doc.update(prior_artifact_sha256=self.old['artifact_sha256'],prior_result_sha256=self.old['result_sha256'],
                   review_result_sha256=frozen['mamu']['receipt']['result_sha256'],decision='revision_required',new_budget_approved=True,
                   reason='追試の不一致を修正。',budget_start=start,budget_deadline=deadline)
        if run==t.REVISION2:
            doc.update(version=3,previous_report_sha256=self.old['report_sha256'])
        jhash=save(base/'judgments'/(run+'.json'),doc)
        target=copy/rv.TARGET;target.parent.mkdir(parents=True);target.write_bytes(b'new model source')
        artifact=t.digest(target.read_bytes());pid=2**30;mid=run+'-turn-01-'+'b'*20
        root=base/'model-runs'/run
        gp=root/(mid+'.generation.json');it=root/(mid+'.intent.json');rp=root/(mid+'.result.json');bp=root/(mid+'.outbox.json')
        src=save(gp,{'job_run_id':run,'run_id':mid,'repo_id':rv.MODEL,'model_revision':rv.MODEL_REV,'text':'new model source',
                     'truncated':False,'worker_pid':pid,'profile_id':'gemma-4-26b-a4b-it-qat-4bit-development','recorded_at':start})
        intent={'stage':'source','job_run_id':run,'formal_run_id':run,'request_id':t.REQUEST,'model_run_id':mid,
                'revision_judgment_sha256':jhash,'started_at':start,'budget_deadline':deadline,'cli_pid':pid+1}
        save(it,intent)
        job={'run_id':run,'formal_run_id':run,'request_id':t.REQUEST,'prior_run':rv.predecessor(run),'label':'A','mode':'implementation',
             'repo_id':rv.MODEL,'revision':rv.MODEL_REV,'revision_judgment_sha256':jhash,'terminal':False,'repo_root':str(copy),
             'current_started_at':start,'started_at':start}
        jobfile=base/'jobs'/(run+'.json');save(jobfile,job)
        events=[{'action':{'action':'apply'},'provenance':{'actor':'miki_full_source','revision':rv.MODEL_REV,'run_id':mid},
                 'result':{'sha256':{rv.TARGET:artifact}},'response_sha256':src},
                {'action':{'action':'finish'},'provenance':{'run_id':mid},'result':{'oracle':{'ok':True}}}]
        save(base/'journal'/(run+'.json'),{'run_id':run,'current_hashes':{rv.TARGET:artifact},'pending':None,'mock':False,'events':events})
        box={'job_run_id':run,'request_id':t.REQUEST,'model_run_id':mid,'judgment_sha256':jhash,'expected_report_sha256':self.old['report_sha256'],
             'stage':'inspection_wait','quality':'pending','artifact_sha256':artifact,'result_sha256':src,'worker_pid':pid,'recorded_at':start,
             'version':3 if run==t.REVISION2 else 2,'prior_run':rv.predecessor(run),'review_run':frozen['review_run'],'prior_artifact_sha256':self.old['artifact_sha256'],
             'budget_deadline':deadline,'quality_gate_ok':True,'additional_checks':{'passed':rv.checks_required(run),'total':rv.checks_required(run),'ok':True,'artifact_sha256':artifact},
             'fixed_checks':{'passed':37,'total':37}}
        phase={**self.amend,'judgment_sha256':jhash,'phase':'before_release','model_run_id':mid,'actor_outbox_sha256':save(bp,box)}
        post={k:v for k,v in phase.items() if k not in ('phase','model_run_id','actor_outbox_sha256')}
        def finish():
            self.svc.available=lambda:True
            rh=save(rp,{'ok':True,'repo_id':rv.MODEL,'model_revision':rv.MODEL_REV,'development_run_id':mid,
                        'text':'new model source','generation_sha256':src,'truncated':False,
                        'stats':{'worker_pid':pid,'worker_exited':True,'worker_returncode':0}})
            save(it,{**intent,'result_sha256':rh})
            job.update(terminal=True,human_code_fixes=0,model_patch_count=1,host_checks_passed=True,
                       ended_at=(datetime.fromisoformat(deadline)+timedelta(seconds=4)).isoformat(),
                       artifacts=[{'path':str(target),'sha256':artifact,'checked_source':src}],final_hashes={rv.TARGET:artifact},
                       model_runs=[{'model_run_id':mid,'result':str(rp),'generation':str(gp),'result_sha256':rh,'pid':pid,'worker_exited':True}])
            save(jobfile,job)
        return phase,post,finish

    def second_source(self):
        rv.prepare(self.svc,self.prepare)
        prior=self.amend_fake_actor()['receipt']
        self.assertNotEqual(prior['artifact_sha256'],self.old_artifact)
        diag={'run_id':t.REVISION,'prior_artifact_sha256':self.old_artifact,'artifact_sha256':prior['artifact_sha256'],
              'judgment_sha256':self.amend['judgment_sha256'],'scope_ok':True,'passed':False,'fixed':{'passed':37,'total':37},
              'diagnostics':{'passed':6,'total':7,'artifact_sha256':prior['artifact_sha256'],
                             'checks':[{'case':'valid_long_suffix','expected':'returned','observed':'invalid_request_id','ok':False}]}}
        path=self.root/'rev1-quality.json';path.write_text(json.dumps(diag))
        p=patch.object(rv,'QUALITY',path);p.start();self.addCleanup(p.stop)
        self.old=prior
        self.prepare.update(run_id=t.REVISION2,prior_run=t.REVISION,diagnostic_sha256=t.digest(path.read_bytes()))
        self.amend.update(run_id=t.REVISION2,prior_run=t.REVISION,expected_report_sha256=prior['report_sha256'])
        return self.active_source(run=t.REVISION2)

    def test_third_version_receipt_preserves_both_parents_and_independent_first_opinion(self):
        phase,post,finish=self.second_source()
        frozen=rv.read_json(rv.correction_path(self.svc,t.REVISION2))
        self.assertEqual((frozen['version'],frozen['miki']['receipt'],frozen['mamu']['receipt']['artifact_sha256']),
                         (3,self.old,self.old_artifact))
        historical={r:self.svc.path(r).read_bytes() for r in (t.CONTINUED,t.REVISION,self.review)}
        with patch.object(rv,'owned_worker',return_value=True):
            live=rv.register_amendment(self.svc,phase)
        receipt=live['receipt']
        self.assertEqual((receipt['version'],receipt['block_id'],receipt['previous_report_sha256'],live['resource_state']),
                         (3,self.old['block_id'],self.old['report_sha256'],'owned'))
        finish()
        self.assertEqual(rv.register_amendment(self.svc,post)['resource_state'],'released')
        from riri.parse import parse_requests,parse_reports
        req=parse_requests([self.api.blocks[self.svc.cfg.requests_page][0]])[0]
        reports=parse_reports(self.api.blocks[self.svc.mail.report_page('miki')],expected_reporter='みき')
        self.assertTrue(t.received_before_patrol(self.svc.cfg,req,reports))
        for r,raw in historical.items():
            self.assertEqual(self.svc.path(r).read_bytes(),raw)
        self.assertEqual(self.api.patches,2)

    def test_third_version_binds_mamu_second_report_to_new_artifact_and_eight_named_checks(self):
        phase,post,finish=self.second_source()
        with patch.object(rv,'owned_worker',return_value=True):
            rv.register_amendment(self.svc,phase)
        impl=self.svc.load(t.REVISION2)
        with self.assertRaises(SafetyError):rv.verified(impl)
        finish();rv.register_amendment(self.svc,post)
        new='mamu-a-review-'+'d'*32;rid=helpers.M['request_id']
        old=self.svc.load(self.review)['receipt']
        req={**post,'op':'review-version','request_id':rid,'run_id':new,'prior_run':t.REVISION2,'expected_report_sha256':old['report_sha256']}
        quality=self.root/'outputs/A_rev2_20261115/A-quality.json';quality.parent.mkdir(parents=True)
        artifact=impl['receipt']['artifact_sha256']
        cases=['leap_date','non_leap_date','fullwidth_suffix','arabic_indic_suffix','dict_subclass_no_overrides','fullwidth_year','valid_long_suffix','empty_suffix']
        q={'run_id':t.REVISION2,'artifact_sha256':artifact,'judgment_sha256':phase['judgment_sha256'],'passed':True,'scope_ok':True,
           'fixed':{'passed':37,'total':37},'diagnostics':{'passed':8,'total':8,'checks':[{'case':c,'ok':True} for c in cases]}}
        meta=self.root/'inspection/job.json';meta.parent.mkdir()
        decision=json.loads((rv.MIKI/'data/development/gemma26/judgments'/(t.REVISION2+'.json')).read_bytes())
        meta.write_text(json.dumps({'review_version':2,'judgment_sha256':phase['judgment_sha256'],'budget_deadline':decision['budget_deadline']}))
        (meta.parent/'input_validation.py').write_bytes(b'new model source')
        with patch.object(rv,'WORKSPACE',self.root),patch.object(self.svc,'command',return_value=['inspector',str(meta)]):
            bad=json.loads(json.dumps(q));bad['diagnostics']['checks'][-1]['case']='leap_date'
            quality.write_text(json.dumps(bad))
            with self.assertRaises(SafetyError):rv.register_amendment(self.svc,req)
            quality.write_text(json.dumps(q));registered=rv.register_amendment(self.svc,req)
        self.assertEqual((registered['prior_run'],registered['artifact_sha256'],registered['revision']['version']),(t.REVISION2,artifact,2))
        self.assertTrue(self.svc.runnable(self.svc.load(new)))
        row=self.svc.load(new);row.update(execution_state='executing');self.svc.save(row)
        with patch.object(rv,'mamu_report_evidence',return_value=None):
            report=self.svc.report({**helpers.P,'actor':'mamu','request_id':rid,'run_id':new,'stage':'reviewed','quality':'unassessed',
                                    'result_sha256':'8'*64,'artifact_sha256':artifact})
        self.assertEqual((report['receipt']['version'],report['receipt']['previous_run'],report['receipt']['block_id']),(2,self.review,old['block_id']))
        self.assertEqual(report['receipt']['previous_report_sha256'],old['report_sha256'])
        self.assertEqual(self.api.patches,3)

    def test_third_version_unknown_readback_and_incomplete_eight_checks(self):
        phase,post,finish=self.second_source()
        self.api.fail='unknown_after'
        with patch.object(rv,'owned_worker',return_value=True):
            live=rv.register_amendment(self.svc,phase)
        self.assertEqual(live['report_state'],'unknown')
        finish();self.api.fail=None
        self.assertEqual(rv.register_amendment(self.svc,post)['receipt']['version'],3)
        self.assertEqual(self.api.patches,2)
        required={'passed':8,'total':8,'ok':True,'artifact_sha256':'a'*64}
        box={'additional_checks':{**required,'passed':7,'total':7},'fixed_checks':{'passed':37,'total':37},
             'quality':'pending','quality_gate_ok':True,'artifact_sha256':'a'*64}
        with self.assertRaises(SafetyError):rv.validate_counts(box,t.REVISION2)
        rv.validate_counts({**box,'additional_checks':required},t.REVISION2)

    def test_receipt_while_owned_then_late_release_with_no_second_patch(self):
        phase,post,finish=self.active_source()
        with patch.object(rv,'owned_worker',return_value=True):
            live=rv.register_amendment(self.svc,phase)
        self.assertEqual((live['report_state'],live['resource_state'],live['execution_state']),('received','owned','awaiting_actor_release'))
        self.assertTrue(live['receipt']['worker_alive_at_readback'])
        with self.assertRaises(SafetyError):rv.verified(self.svc.load(t.REVISION))
        finish()
        final=rv.register_amendment(self.svc,post)
        self.assertEqual((final['resource_state'],final['execution_state']),('released','finished'))
        self.assertEqual(self.api.patches,1)
        from riri.parse import parse_reports,parse_requests
        req=parse_requests([self.api.blocks[self.svc.cfg.requests_page][0]])[0]
        rpt=parse_reports(self.api.blocks[self.svc.mail.report_page('miki')], expected_reporter='みき')
        self.assertTrue(t.received_before_patrol(self.svc.cfg,req,rpt))
        archived=self.svc.load(t.CONTINUED);archived['receipt']['report_sha256']='f'*64;self.svc.save(archived)
        self.assertFalse(t.received_before_patrol(self.svc.cfg,req,rpt))

    def test_source_connection_failure_persists_outbox_until_after_release(self):
        phase,post,finish=self.active_source()
        self.api.break_read=True
        with patch.object(rv,'owned_worker',return_value=True):
            live=rv.register_amendment(self.svc,phase)
        self.assertEqual(live['report_state'],'pending');self.assertTrue(self.svc.load(t.REVISION)['outbox'])
        finish();self.api.break_read=False
        final=rv.register_amendment(self.svc,post)
        self.assertEqual((final['report_state'],final['resource_state']),('received','released'));self.assertEqual(self.api.patches,1)

    def test_source_unknown_patch_reconciles_read_only_after_restart(self):
        phase,post,finish=self.active_source()
        self.api.fail='unknown_after'
        with patch.object(rv,'owned_worker',return_value=True):
            live=rv.register_amendment(self.svc,phase)
        self.assertEqual(live['report_state'],'unknown')
        finish();self.api.fail=None
        final=rv.register_amendment(self.svc,post)
        self.assertEqual(final['report_state'],'received');self.assertEqual(self.api.patches,1)
        self.assertFalse(final['receipt']['worker_alive_at_readback'])

    def test_no_pre_release_receipt_without_corresponding_worker_or_fixed_artifact(self):
        phase,_,_=self.active_source()
        with patch.object(rv,'owned_worker',return_value=False),self.assertRaises(SafetyError):rv.register_amendment(self.svc,phase)
        with patch.object(rv,'owned_worker',return_value=True),self.assertRaises(SafetyError):
            rv.register_amendment(self.svc,{**phase,'actor_outbox_sha256':'7'*64})
        self.assertFalse(self.svc.path(t.REVISION).exists());self.assertEqual(self.api.patches,0)

    def test_terminal_confirmed_no_generation_has_diagnostic_receipt_and_no_review(self):
        phase,post,_=self.active_source()
        base=rv.MIKI/'data/development/gemma26';folder=base/'model-runs'/t.REVISION
        for f in folder.iterdir():f.unlink()
        jobfile=base/'jobs'/(t.REVISION+'.json')
        job=json.loads(jobfile.read_bytes());job.update(terminal=True,model_patch_count=0,model_runs=[],artifacts=[],
              final_hashes={rv.TARGET:self.old_artifact},error='resource_or_lock_blocked')
        jobfile.write_text(json.dumps(job));(rv.COPY/rv.TARGET).write_bytes(b'old reviewed candidate')
        fail=base/'jobs'/(t.REVISION+'.failure.json')
        fail.write_text(json.dumps({'outcome':'confirmed_failure','run_id':t.REVISION,'request_id':t.REQUEST,
              'judgment_sha256':post['judgment_sha256'],'artifact_sha256':self.old_artifact,'reason':job['error'],
              'worker_started':False,'worker_pid':None,'model_run_id':None,'recorded_at':t.now()}))
        self.svc.available=lambda:True
        final=rv.register_amendment(self.svc,post)
        self.assertEqual(final['stage'],'implementation_incomplete')
        self.assertEqual(final['receipt']['result_sha256'],t.digest(fail.read_bytes()))
        self.assertEqual(final['report_state'],'received')
        frozen=rv.read_json(rv.correction_path(self.svc));req={**post,'op':'review-version','request_id':helpers.M['request_id'],
              'run_id':'mamu-a-review-'+'d'*32,'prior_run':t.REVISION,'expected_report_sha256':frozen['mamu']['receipt']['report_sha256']}
        with self.assertRaises(SafetyError):rv.register_amendment(self.svc,req)

    def test_fresh_quality_artifact_and_deadline_gate_review_on_registration_and_launch(self):
        phase,_,_=self.active_source();base=rv.MIKI/'data/development/gemma26'
        judgment=json.loads((base/'judgments'/(t.REVISION+'.json')).read_bytes())
        row={'prior_run':t.REVISION,'amends':{'judgment_sha256':phase['judgment_sha256']},'artifact_sha256':'a'*64}
        quality=self.root/'measured-quality.json';meta=self.root/'staged-review-job.json'
        good={'run_id':t.REVISION,'judgment_sha256':phase['judgment_sha256'],'artifact_sha256':row['artifact_sha256'],'passed':True,
              'scope_ok':True,'fixed':{'passed':37,'total':37},'diagnostics':{'passed':7,'total':7}}
        meta.write_text(json.dumps({'review_version':2,'judgment_sha256':phase['judgment_sha256'],'budget_deadline':judgment['budget_deadline']}))
        with patch.object(rv,'QUALITY',quality),patch.object(rv,'now',return_value=judgment['budget_start']):
            quality.write_text(json.dumps(good));self.assertEqual(rv.review_eligibility(row,meta),t.digest(quality.read_bytes()))
            for changed in [{'passed':False},{'scope_ok':False},{'diagnostics':{'passed':6,'total':7}},{'artifact_sha256':'1'*64}]:
                quality.write_text(json.dumps({**good,**changed}))
                with self.assertRaises(SafetyError):rv.review_eligibility(row,meta)
            quality.write_text(json.dumps(good))
        with patch.object(rv,'QUALITY',quality),patch.object(rv,'now',return_value='2199-01-01T00:00:00+00:00'):
            with self.assertRaises(SafetyError):rv.review_eligibility(row,meta)

    def test_local_ps_binds_a_process_to_its_parent_and_script_without_any_model(self):
        import os,subprocess,sys
        script=self.root/'cpu_only_fixture.py';script.write_text('import time\ntime.sleep(3)\n')
        child=subprocess.Popen([sys.executable,str(script)])
        try:
            self.assertTrue(rv.owned_worker(child.pid,os.getpid(),script))
            self.assertFalse(rv.owned_worker(child.pid,os.getpid()+1,script))
            self.assertFalse(rv.owned_worker(child.pid,os.getpid(),self.root/'different.py'))
        finally:
            child.terminate();child.wait()

    def test_mamu_report_preserves_real_result_identity_before_or_after_physical_exit(self):
        run='mamu-a-review-'+'a'*32;rid=helpers.M['request_id'];d=self.root/'mamu/state/inspections/runs'/run;d.mkdir(parents=True)
        row={'prior_run':t.REVISION,'run_id':run,'request_id':rid,'spec_sha256':helpers.SPEC,'child_pid':2**30}
        result={'run_id':run,'request_id':rid,'prior_run_id':t.REVISION,'artifact_sha256':'b'*64,
                'spec_sha256':helpers.SPEC,'ok':True,'generated':{'lifecycle':{'pid':2**29}}}
        f=d/'result.json';f.write_text(json.dumps(result));h=t.digest(f.read_bytes())
        packet={'actor':'mamu','run_id':run,'request_id':rid,'stage':'reviewed','quality':'unassessed',
                'artifact_sha256':result['artifact_sha256'],'result_sha256':h,'summary':'検査を保存しました。'}
        (d/'outbox.json').write_text(json.dumps(packet))
        with patch.object(rv,'MAMU',self.root/'mamu'):
            with patch.object(rv,'owned_worker',return_value=True):
                self.assertEqual(rv.mamu_report_evidence(row,packet),2**29)
            with self.assertRaises(SafetyError):rv.mamu_report_evidence(row,{**packet,'result_sha256':'0'*64})
            (d/'release.json').write_text(json.dumps({'result_sha256':h,'worker_exit':{'worker_pid':2**29,'worker_exited':True}}))
            self.assertIsNone(rv.mamu_report_evidence(row,packet))
