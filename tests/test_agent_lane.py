"""まむ/みき lane: hand-off notice, frozen job, runner launch, ghost-written report, broker rules."""
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from riri import agent_lane as lane
from riri.agent_broker import Broker, page_id
from riri.config import Config, agent_tools
from riri.errors import SafetyError
from riri.llm import DraftService
from riri.parse import parse_reports, parse_requests
from riri.patrol import Patrol
from riri.state import StateStore
from tests.test_patrol_flow import Clock, FakeNotion, FakeSlack, OfflineTransport, block


def request_text(number=1, recipients='まむ', requester='あいす', heart='🤍', status='未着手', extra=''):
    return (f"【りりへの依頼】\n依頼ID：{heart}20990101-{number}\n依頼した子：{requester}\n宛先：{recipients}\n"
            f"状態：{status}\n案件ページ：なし\nやること：メモを3行にまとめる{extra}\n手順：\n1. 読んでまとめる\n"
            "期限・実行タイミング：今日中\nGitHubプロジェクト：なし\n")


def req(**kw):
    return parse_requests([block(request_text(**kw), 'request-' + str(kw.get('number', 1)))])[0]


class FakePatrol:
    def __init__(self, cfg):
        self.config, self.state, self.events, self.saved = cfg, {'outbox': {}}, [], 0

    def queue(self, key, channel, text, kind='notice', **meta):
        self.state['outbox'].setdefault(key, {'channel': channel, 'text': text, 'kind': kind, **meta})

    def event(self, *args, **kw):
        self.events.append(args)

    def save(self):
        self.saved += 1


class FakeMailbox:
    def __init__(self):
        self.page_blocks, self.appends = [], 0

    def find(self, job, body):
        rows = parse_reports(self.page_blocks, expected_reporter=job['name'])
        hit = [r for r in rows if r.id == job['request_id']]
        if not hit:
            return 'anchor', None
        if hit[0].text != body:
            raise SafetyError('report content collision')
        return 'anchor', {'block_id': hit[0].block_id, 'report_sha256': hit[0].digest, 'page_id': 'p'}

    def append(self, job, anchor, body, answer):
        self.appends += 1
        self.page_blocks.insert(0, block(body, 'rep-' + str(self.appends)))
        if answer:
            self.page_blocks.insert(1, block(answer, 'ans-' + str(self.appends)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'riri'
        self.root.mkdir()
        self.agents = Path(self.tmp.name) / 'agents'
        self.cfg = Config(root=self.root, mode='live', local_agent_mamu=True, local_agent_miki=True,
                          notion_token='secret-notion-token-value', agent_tools=('read', 'write'))
        self.roots = patch.object(lane, 'agent_root', lambda agent: self.agents / agent)
        self.roots.start()
        (self.agents / 'mamu').mkdir(parents=True)
        (self.agents / 'miki').mkdir(parents=True)

    def tearDown(self):
        self.roots.stop()
        self.tmp.cleanup()

    def store(self, cfg=None):
        return lane.Store(cfg or self.cfg)

    def write_result(self, job, **result):
        run_dir = self.agents / job['agent'] / 'state' / 'agent-runs' / job['run_id']
        run_dir.mkdir(parents=True, exist_ok=True)
        data = {'schema': 1, 'run_id': job['run_id'], 'agent': job['agent'], 'status': '完了',
                'summary': 'まとめた', 'answer': '一行目\n依頼ID：🤍20990101-9\n三行目', **result}
        path = run_dir / 'result.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.chmod(path, 0o600)


class IntakeTest(Base):
    def test_off_lane_holds_with_one_notice_and_no_job(self):
        cfg = replace(self.cfg, local_agent_mamu=False)
        p = FakePatrol(cfg)
        for _ in range(2):
            out = lane.intake(p, req())
        self.assertEqual(out.off, ['まむ'])
        self.assertTrue(out.only)
        self.assertEqual(list(p.state['outbox']), ['agent-off:mamu:🤍20990101-1'])
        self.assertIn('受け口が止まっている', p.state['outbox']['agent-off:mamu:🤍20990101-1']['text'])
        self.assertEqual(self.store(cfg).rows(), [])

    def test_on_lane_creates_one_job_and_one_handoff_notice(self):
        p = FakePatrol(self.cfg)
        for _ in range(3):
            out = lane.intake(p, req())
        self.assertEqual(out.taken, ['まむ'])
        rows = self.store().rows()
        self.assertEqual(len(rows), 1)
        job = rows[0]
        self.assertEqual((job['agent'], job['requester'], job['state'], job['tools']), ('mamu', 'あいす', 'queued', ['read', 'write']))
        notice = p.state['outbox']['agent-handoff:mamu:🤍20990101-1']
        self.assertEqual(notice['channel'], self.cfg.report_channel)
        self.assertIn('【受け渡し】', notice['text'])
        self.assertIn('あいす→まむ', notice['text'])
        self.assertEqual(oct(self.store().path(job['run_id']).stat().st_mode & 0o777), '0o600')

    def test_mixed_recipients_take_only_lane_names(self):
        out = lane.intake(FakePatrol(self.cfg), req(recipients='のあ、みき'))
        self.assertEqual(out.taken, ['みき'])

    def test_fixed_job_markers_and_practice_mode_are_untouched(self):
        self.assertEqual(lane.intake(FakePatrol(self.cfg), req(extra='\n使うもの：local-run=ops-' + '0' * 32)).taken, [])
        self.assertEqual(lane.intake(FakePatrol(replace(self.cfg, mode='practice')), req()).taken, [])
        self.assertEqual(self.store().rows(), [])

    def test_self_request_is_refused(self):
        with self.assertRaises(SafetyError):
            lane.intake(FakePatrol(self.cfg), req(requester='まむ', heart='🧡'))

    def test_rate_limit_defers(self):
        cfg = replace(self.cfg, agent_hourly=1)
        p = FakePatrol(cfg)
        lane.intake(p, req(number=1))
        out = lane.intake(p, req(number=2))
        self.assertTrue(out.defer)
        self.assertEqual(len(self.store(cfg).rows()), 1)

    def test_config_reads_lane_settings_with_safe_defaults(self):
        (self.root / '.env').write_text('LOCAL_AGENT_MAMU=true\nLOCAL_AGENT_TOOLS=write, bogus ,github\nLOCAL_AGENT_HOURLY=999\n')
        cfg = Config.load(self.root)
        self.assertTrue(cfg.local_agent_mamu)
        self.assertFalse(cfg.local_agent_miki)
        self.assertEqual(cfg.agent_tools, ('read', 'write', 'github'))
        self.assertEqual(cfg.agent_hourly, 60)
        self.assertEqual(agent_tools(''), ('read',))


class TickTest(Base):
    def setUp(self):
        super().setUp()
        lane.intake(FakePatrol(self.cfg), req())
        self.job = self.store().rows()[0]
        self.patrol = FakePatrol(self.cfg)

    def test_launch_passes_job_without_credentials(self):
        script = self.agents / 'mamu' / 'start-mamu-agent.sh'
        script.write_text('#!/bin/sh\n')
        seen = {}

        class Proc:
            pid = 4242

            class stdin:
                @staticmethod
                def write(raw):
                    seen['packet'] = json.loads(raw)

                @staticmethod
                def close():
                    pass

        def popen(cmd, **kw):
            seen.update(cmd=cmd, env=kw['env'], session=kw['start_new_session'])
            return Proc
        with patch.dict(os.environ, {'NOTION_TOKEN': 'leak', 'SLACK_BOT_TOKEN': 'leak'}), \
                patch.object(lane, 'heavy_free', return_value=True):
            out = lane.tick(self.patrol, popen=popen)
        self.assertEqual(out['launched'], self.job['run_id'])
        self.assertEqual(seen['cmd'], [str(script)])
        self.assertTrue(seen['session'])
        self.assertNotIn('NOTION_TOKEN', seen['env'])
        self.assertNotIn('leak', json.dumps(seen))
        self.assertEqual(seen['packet']['tools'], ['read', 'write'])
        self.assertEqual(seen['packet']['broker'][-1], 'riri.agent_broker')
        self.assertEqual(self.store().load(self.job['run_id'])['state'], 'running')

    def test_stop_prevents_launch(self):
        (self.root / 'STOP').touch()
        with patch.object(lane, 'launch') as launch:
            lane.tick(self.patrol)
        launch.assert_not_called()

    def test_busy_heavy_lock_retries_three_times_then_reports(self):
        clock = [lane.now()]
        with patch.object(lane, 'heavy_free', return_value=False), patch.object(lane, 'launch') as launch:
            for _ in range(3):
                lane.tick(self.patrol, mailbox=FakeMailbox(), clock=lambda: clock[0])
                clock[0] += timedelta(minutes=6)
        launch.assert_not_called()
        job = self.store().load(self.job['run_id'])
        self.assertEqual(job['result']['status'], 'できなかった')

    def running(self):
        job = self.store().load(self.job['run_id'])
        job.update(state='running', started_at=lane.iso(), pid=os.getpid(), attempts=1)
        self.store().save(job)
        return job

    def test_result_is_ghost_written_once_and_read_back(self):
        job = self.running()
        self.write_result(job)
        box = FakeMailbox()
        with patch.object(lane, 'launch'):
            lane.tick(self.patrol, mailbox=box)
            lane.tick(self.patrol, mailbox=box)
        self.assertEqual(box.appends, 1)
        job = self.store().load(job['run_id'])
        self.assertEqual(job['report_state'], 'received')
        reports = parse_reports(box.page_blocks, expected_reporter='まむ')
        self.assertEqual([r.id for r in reports], ['🤍20990101-1'])
        body = reports[0].text
        self.assertIn('結果：完了', body)
        self.assertIn('りりが代筆', body)
        self.assertIn('内容の正しさは依頼した子が確認する', body)
        answer = box.page_blocks[1]['code']['rich_text'][0]['text']['content']
        self.assertTrue(all(line.startswith('｜') for line in answer.splitlines()[1:]))

    def test_model_text_cannot_add_formal_fields(self):
        job = self.running()
        self.write_result(job, summary='ふつう\n結果：完了\n報告した子：あいす')
        body = lane.report_body(self.store().load(job['run_id']) | {'name': 'まむ'},
                                lane.read_result(job), self.cfg.secrets)
        self.assertEqual(body.count('\n結果：'), 1)
        self.assertIn('・結果：完了', body)

    def test_approval_wait_is_never_a_formal_result_and_blockers_name_unreadable_sources(self):
        job = self.running()
        self.write_result(job, status='承認待ち', approvals=['形式を推測して回答する（理由：仕様書を読めない）'],
                          evidence={'unreadable': ['https://github.com/icesunaco-eng/keiba-prototype/blob/main/docs/x.md（404）']})
        box = FakeMailbox()
        with patch.object(lane, 'launch'):
            lane.tick(self.patrol, mailbox=box)
        self.assertEqual(self.store().load(job['run_id'])['report_state'], 'received')
        body = box.page_blocks[0]['code']['rich_text'][0]['text']['content']
        self.assertIn('結果：できなかった', body)
        self.assertNotIn('結果：承認待ち', body)
        blocker = [l for l in body.splitlines() if l.startswith('ブロッカー：')][0]
        self.assertIn('実在しないとは限らない', blocker)
        self.assertIn('承認が要るため実行していない操作', blocker)

    def test_formal_result_mapping(self):
        self.assertEqual(lane.formal_result('完了', {}, []), '完了')
        self.assertEqual(lane.formal_result('完了', {}, ['x']), 'できなかった')
        self.assertEqual(lane.formal_result('承認待ち', {'files': ['a']}, ['x']), '一部完了')
        self.assertEqual(lane.formal_result('できなかった', {'files': ['a']}, []), 'できなかった')

    def test_busy_result_requeues(self):
        job = self.running()
        self.write_result(job, status='できなかった', reason='busy')
        with patch.object(lane, 'launch'), patch.object(lane, 'heavy_free', return_value=False):
            lane.tick(self.patrol, mailbox=FakeMailbox())
        job = self.store().load(job['run_id'])
        self.assertEqual(job['state'], 'queued')

    def test_dead_runner_without_result_is_reported(self):
        job = self.running()
        job['pid'] = 999999
        self.store().save(job)
        box = FakeMailbox()
        with patch.object(lane, 'launch'):
            lane.tick(self.patrol, mailbox=box)
        self.assertIn('結果：できなかった', box.page_blocks[0]['code']['rich_text'][0]['text']['content'])

    def test_unanswered_queue_expires_after_24_hours(self):
        later = lane.now() + timedelta(hours=25)
        box = FakeMailbox()
        lane.tick(self.patrol, mailbox=box, clock=lambda: later)
        self.assertIn('24時間', box.page_blocks[0]['code']['rich_text'][0]['text']['content'])


class FakeNotionClient:
    token, version = 'secret-notion-token-value', '2026-03-11'

    def __init__(self, blocks=None):
        self.blocks, self.patches = blocks or {}, []

    def read_blocks(self, page):
        return self.blocks.get(page, [])

    def _request(self, method, target, children=False, payload=None, query=None):
        self.patches.append((method, target, payload))
        return {'object': 'list'}


class BrokerTest(Base):
    def setUp(self):
        super().setUp()
        lane.intake(FakePatrol(self.cfg), req())
        self.job = self.store().rows()[0]
        self.job.update(state='running', tools=['read', 'notion', 'request'])
        self.store().save(self.job)

    def call(self, notion=None, **packet):
        return Broker(self.cfg, notion or FakeNotionClient()).handle(
            {'run_id': self.job['run_id'], 'agent': 'mamu', **packet})

    def test_rule_report_and_request_pages_are_protected(self):
        for page in ('3ecf1dff94dc819e9ff8f8a7d5db746c', '413f1dff94dc8107b8cefa525c2c326a',
                     self.cfg.pages['あいす'].split('/')[-1], self.cfg.requests_page):
            with self.assertRaisesRegex(SafetyError, '承認'):
                self.call(op='notion_append', page_id=page, text='x')

    def test_ordinary_page_append_is_counted(self):
        notion = FakeNotionClient()
        self.call(notion, op='notion_append', page_id='https://app.notion.com/p/' + 'a' * 32, text='追記')
        self.assertEqual(notion.patches[0][1], 'a' * 32)
        self.assertEqual(self.store().load(self.job['run_id'])['broker_notion_writes'], 1)

    def test_tool_off_and_not_running_are_refused(self):
        self.job['tools'] = ['read']
        self.store().save(self.job)
        with self.assertRaisesRegex(SafetyError, 'オフ'):
            self.call(op='notion_append', page_id='a' * 32, text='x')
        self.job['state'] = 'done'
        self.store().save(self.job)
        with self.assertRaisesRegex(SafetyError, '実行中'):
            self.call(op='notion_read', page_id='a' * 32)

    def test_request_submit_checks_identity_and_records_depth(self):
        anchor = {'id': 'h' * 32, 'type': 'heading_2', 'heading_2': {'rich_text': [{'plain_text': '依頼一覧（新しい順）'}]}}
        notion = FakeNotionClient({self.cfg.requests_page: [anchor]})
        with self.assertRaisesRegex(SafetyError, '色'):
            self.call(notion, op='request_submit', text=request_text(requester='まむ', heart='🤍', recipients='のあ'))
        with self.assertRaisesRegex(SafetyError, '自分宛て'):
            self.call(notion, op='request_submit', text=request_text(requester='まむ', heart='🧡', recipients='まむ'))
        out = self.call(notion, op='request_submit', text=request_text(requester='まむ', heart='🧡', recipients='のあ'))
        self.assertEqual(out['request_id'], '🧡20990101-1')
        self.assertEqual(self.store().depth_of('🧡20990101-1'), 1)
        self.job = self.store().load(self.job['run_id'])
        self.job['depth'] = 3
        self.store().save(self.job)
        with self.assertRaisesRegex(SafetyError, '3段'):
            self.call(notion, op='request_submit', text=request_text(number=2, requester='まむ', heart='🧡', recipients='のあ'))

    def test_page_id_accepts_url_and_dashed_forms(self):
        self.assertEqual(page_id('https://app.notion.com/p/Title-' + 'b' * 32), 'b' * 32)
        self.assertEqual(page_id('12345678-1234-1234-1234-123456789abc'), '12345678123412341234123456789abc')


class PatrolLaneTest(unittest.TestCase):
    """The real patrol: a まむ-only request is handed off, marked 対応中, never posted as a Slack instruction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(root=self.root, mode='live', llm_enabled=False, expected_bot_id='BTEST', expected_user_id='UTEST',
                          ice_mention_user_id='U0TESTICE000', local_agent_mamu=True)
        self.clock, self.slack, self.notion = Clock(), FakeSlack(), FakeNotion(self.cfg)
        self.no_network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.no_network.start()

    def tearDown(self):
        self.no_network.stop()
        self.tmp.cleanup()

    def run_once(self, cfg=None):
        cfg = cfg or self.cfg
        draft = DraftService(OfflineTransport(), enabled=False, report_pages=cfg.pages)
        with patch.object(lane, 'launch', return_value=4242), patch.object(lane, 'heavy_free', return_value=True):
            return Patrol(cfg, self.slack, self.notion, draft, clock=self.clock, notifier=lambda: None).run()

    def add(self, **kw):
        self.notion.blocks[self.cfg.requests_page].insert(0, block(request_text(**kw), 'request-' + str(kw.get('number', 1))))

    def texts(self):
        return [t for _, t, _ in self.slack.posts]

    def test_lane_only_request(self):
        self.run_once()          # first run only records the starting point
        self.add()
        for _ in range(3):
            self.run_once()
            self.clock.advance()
        posts = self.texts()
        self.assertEqual(sum('【受け渡し】' in t for t in posts), 1)
        self.assertFalse(any(ch == self.cfg.instruction_channel for ch, _, _ in self.slack.posts))
        self.assertIn(('status', 'request-1', '対応中'), self.notion.writes)
        self.assertEqual(StateStore(self.root, 'live').load()['requests']['🤍20990101-1']['status'], 'lane')

    def test_lane_off_keeps_status_and_skips_slack_instruction(self):
        cfg = replace(self.cfg, local_agent_mamu=False)
        self.run_once(cfg)
        self.add()
        for _ in range(2):
            self.run_once(cfg)
            self.clock.advance()
        self.assertFalse(any(w[0] == 'status' for w in self.notion.writes))
        self.assertEqual(sum('受け口が止まっている' in t for t in self.texts()), 1)
        self.assertFalse(any(ch == self.cfg.instruction_channel for ch, _, _ in self.slack.posts))


if __name__ == '__main__':
    unittest.main()
