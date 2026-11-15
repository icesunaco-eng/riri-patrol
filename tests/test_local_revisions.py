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
        self.api=MutableNotion(fixture.fake)
        self.svc.mail.notion=self.api
        self.svc.register(helpers.A)
        a=self.svc.load(t.ORIGINAL)
        a.update(execution_state='reconcile',resource_state='released');self.svc.save(a)
        a.update(run_id=t.CONTINUED,prior_run=t.ORIGINAL,execution_state='executing')
        self.svc.save(a)
        self.svc.report({**helpers.P,'run_id':t.CONTINUED})
        a=self.svc.load(t.CONTINUED);a.update(execution_state='finished',resource_state='released');self.svc.save(a)
        self.old=t.LocalTasks.public(a)['receipt']
        self.review=helpers.M['run_id']
        page=self.svc.mail.report_page('mamu')
        self.api.blocks[page]=[helpers.block('報告一覧（新しい順）','heading_2',i='e'*32)]
        self.svc.register({**helpers.M,'prior_run':t.CONTINUED})
        m=self.svc.load(self.review);m['execution_state']='executing';self.svc.save(m)
        self.svc.report({**helpers.P,'run_id':self.review,'request_id':m['request_id'],'actor':'mamu','stage':'reviewed','quality':'unassessed'})
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
        with patch.object(self.svc,'command',return_value=['fixed-inspector',str(staged/'job.json')]):
            got=rv.register_amendment(self.svc,req)
            self.assertEqual(got['artifact_sha256'],self.findings['artifact_sha256'])
            self.assertTrue(self.svc.runnable(self.svc.load(new)))
            self.assertEqual(got['request_id'],helpers.M['request_id'])
            with self.assertRaises(SafetyError):rv.register_amendment(self.svc,{**req,'run_id':'mamu-a-review-'+'a'*32})
        row=self.svc.load(new);row['execution_state']='executing';self.svc.save(row)
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
