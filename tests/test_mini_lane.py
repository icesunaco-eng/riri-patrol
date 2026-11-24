"""みにの受け口（riri/mini_lane.py）：受付・予約・資料・起動・見張り役・正式な結果・代筆。

本物の Codex は使わない。同じ形の JSONL と会話記録を出す偽の codex で、見張り役の
プロセス（run_supervisor）から代筆の読み戻しまでを通す。設計 v1.0 の完成基準 2・8・17・22・25・33。
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from riri import mini_lane as lane
from riri import mini_supervisor
from riri.config import Config, mini_author_map
from riri.errors import AccountMismatch, SafetyError
from riri.mini_run import RunStore
from riri.parse import parse_reports, parse_requests
from tests.test_patrol_flow import block

PID = '41ff1dff94dc81448e87dc3ad998ce51'


def request_text(number=1, recipients='みに', requester='のあ', status='未着手', page=PID, extra=''):
    return (f"【りりへの依頼】\n依頼ID：💚20990101-{number}\n依頼した子：{requester}\n宛先：{recipients}\n"
            f"状態：{status}\n案件ページ：https://app.notion.com/p/{page}\nやること：設計を読んで感想を3行で{extra}\n"
            "手順：\n1. 読む\n期限・実行タイミング：今日中\nGitHubプロジェクト：なし\n")


def req(**kw):
    return parse_requests([block(request_text(**kw), 'request-' + str(kw.get('number', 1)))])[0]


class FakeNotion:
    def __init__(self):
        self.pages = {PID: [block('案件ページの本文', 'case-1')]}
        self.props = {'名前': 'みにを起こす仕組み'}
        self.change_on_read = None
        self.reads = 0

    def read_blocks(self, page_id):
        self.reads += 1
        if self.change_on_read and self.reads in self.change_on_read:
            return [block('案件ページの本文（書き換え ' + str(self.reads) + '）', 'case-1')]
        return list(self.pages[page_id])

    def read_page(self, page_id):
        return {'object': 'page', 'parent': {'type': 'data_source_id'}, 'properties': self.props,
                'last_edited_time': '2026-11-25T00:00:00.000Z'}


class FakePatrol:
    def __init__(self, cfg, notion):
        self.config, self.notion = cfg, notion
        self.state = {'outbox': {}}
        self.events, self.lane_taken, self.lane_given, self.lane_only_calls = [], {}, {}, []

    def clock(self):
        return datetime.now(timezone.utc)

    def queue(self, key, channel, text, kind='notice', **meta):
        self.state['outbox'].setdefault(key, {'channel': channel, 'text': text, 'kind': kind, **meta})

    def event(self, *args, **kw):
        self.events.append(args)

    def lane_only(self, r):
        self.lane_only_calls.append(r.id)


class FakeMailbox:
    def __init__(self):
        self.blocks, self.appends = [], 0

    def find(self, record, body):
        rows = parse_reports(self.blocks, expected_reporter='みに')
        hit = [r for r in rows if r.id == record['request_id']]
        if not hit:
            return 'anchor', None
        self.assert_same = hit[0].text == body
        return 'anchor', {'block_id': hit[0].block_id, 'report_sha256': hit[0].digest, 'page_id': 'p'}

    def append(self, record, anchor, body):
        self.appends += 1
        self.blocks.insert(0, block(body, 'rep-' + str(self.appends)))


FAKE_CODEX = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    prompt = sys.stdin.read()
    run = prompt.split('run ')[1].split('）')[0]
    mode = os.environ.get('FAKE_MODE', 'ok')
    answer = json.dumps({"run_id": run, "final": True, "result": "完了", "summary": "読んだ\\n感想を書いた",
                         "artifacts": [], "unverified": "なし", "blockers": "なし", "next": "のあが確認する"},
                        ensure_ascii=False)
    home = Path(os.environ['CODEX_HOME'])
    d = home / 'sessions' / '2026' / '11' / '25'
    d.mkdir(parents=True, exist_ok=True)
    phase = 'commentary' if mode == 'no_signal' else 'final_answer'
    item = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": answer}]}}
    (d / ('rollout-2026-11-25T00-00-00-th-' + run + '.jsonl')).write_text(json.dumps(item, ensure_ascii=False) + "\\n")
    out = Path(sys.argv[sys.argv.index('-o') + 1])
    out.write_text(answer)
    for e in ({"type": "thread.started", "thread_id": "th-" + run}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": answer}},
              {"type": "turn.completed", "usage": {}}):
        print(json.dumps(e, ensure_ascii=False), flush=True)
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(os.path.realpath(self.tmp.name))
        self.home = t / 'home'
        self.root = t / 'riri'
        for p in (self.home / '.codex', self.home / 'みに' / '案件' / '設計', self.home / '.codex-riri', self.root):
            p.mkdir(parents=True)
        (self.home / '.codex' / 'AGENTS.md').write_text('みにのルール')
        (self.home / '.codex' / 'mini-heroins.md').write_text('ヒロインズの決まり')
        (self.home / 'みに' / 'AGENTS.md').write_text('みにの入口')
        case = self.home / 'みに' / '案件' / '設計'
        (case / 'AGENTS.md').write_text(f'- NotionページID: `{PID}`\n')
        (case / '案件メモ.md').write_text('メモ')
        (self.root / '.env').write_text('RIRI_MODE=live\nMINI_TRANSPORT=local\nMINI_STAGE=1\n')
        self.codex = t / 'fake_codex.py'
        self.codex.write_text(FAKE_CODEX)
        self.cfg = Config(root=self.root, mode='live', mini_transport='local', mini_stage=1,
                          notion_token='secret-notion-token-value')
        self.patches = [patch.object(lane, 'home', lambda: self.home)]
        for p in self.patches:
            p.start()
        self.notion = FakeNotion()
        self.patrol = FakePatrol(self.cfg, self.notion)
        self.spawned = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def store(self):
        return RunStore(self.root)

    def popen(self, *args, **kw):
        self.spawned.append(args[0])

        class P:
            pid = os.getpid()   # a living PID: the supervisor "is running"
        return P()

    def run_supervisor(self, mode='ok'):
        """What the detached ``python -m riri.mini_lane supervise <run>`` does, with the fake codex."""
        run = [r for r in self.store().all_runs() if r.get('state') == 'spawning'][0]['run_id']

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        env = {'FAKE_MODE': mode}
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, env), \
                patch.object(lane, 'child_base_env', lambda cfg, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': mode, 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(self.cfg, run), 0)
        return run


class IntakeTests(Base):
    def test_off_by_default_keeps_todays_path(self):
        # 完成基準15：オフ（既定）なら何も受けない
        cfg = Config(root=self.root, mode='live')
        self.assertEqual((cfg.mini_transport, cfg.mini_stage), ('off', 0))
        self.assertFalse(lane.intake(FakePatrol(cfg, self.notion), req()).taken)
        for c in (replace(self.cfg, mini_stage=0), replace(self.cfg, mini_transport='off'), replace(self.cfg, mode='practice')):
            self.assertFalse(lane.intake(FakePatrol(c, self.notion), req()).taken)
        (self.root / 'STOP').touch()
        self.assertFalse(lane.intake(self.patrol, req()).taken)
        self.assertEqual(self.store().all_runs(), [])

    def test_reserves_once_without_posting(self):
        for _ in range(3):
            out = lane.intake(self.patrol, req())
        self.assertTrue(out.taken)
        runs = self.store().all_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]['state'], runs[0]['requester']), ('reserved', 'のあ'))
        self.assertEqual(self.patrol.state['outbox'], {})   # 受け渡しは起動のあと

    def test_other_recipients_and_self_and_continuation(self):
        self.assertFalse(lane.intake(self.patrol, req(recipients='あいす')).taken)
        with self.assertRaises(SafetyError):
            lane.intake(self.patrol, req(requester='みに'))
        held = lane.intake(self.patrol, req(number=2, extra='\n元の依頼ID：💚20990101-1'))
        self.assertTrue(held.held)

    def test_status_only_change_is_the_same_and_body_change_after_launch_is_held(self):
        # 完成基準22・33
        lane.intake(self.patrol, req())
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda r: {**r, 'launch_mark': 'x'})
        self.assertEqual(lane.intake(self.patrol, req(status='対応中')).held, '')
        changed = parse_requests([block(request_text().replace('3行', '5行'), 'request-1')])[0]
        self.assertIn('本文だけ', lane.intake(self.patrol, changed).held)
        self.assertEqual(len(self.store().all_runs()), 1)


class FlowTests(Base):
    def test_whole_flow_to_report_with_final_signal(self):
        # 完成基準2（偽の codex）：受付 → 予約 → 起動 → 受け渡し（1回）→ 報告 → 読み戻し
        r = req()
        lane.intake(self.patrol, r)
        box = FakeMailbox()
        out = lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn('riri.mini_lane', self.spawned[0])
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'spawning')
        self.assertEqual(rec['case']['kind'], 'one')
        self.assertEqual(rec['plan']['row'], 3)                     # 段1・案件ちょうど1つ
        self.assertEqual(sorted(rec['materials']), ['case_agents', 'case_memo', 'case_page', 'ledger_row',
                                                     'mini_agents', 'request', 'rules_agents', 'rules_heroins'])
        prompt = (Path(rec['run_dir']) / 'prompt.txt').read_text()
        self.assertIn('本番の ~/.codex は読まない', prompt)
        self.assertIn(str(Path(rec['run_dir']) / 'input' / 'rules_agents.txt'), prompt)
        self.assertEqual(oct(os.stat(Path(rec['run_dir']) / 'input' / 'request.txt').st_mode & 0o777), '0o400')
        self.assertIsNone(out.get('launched') and lane.tick(self.patrol, [r], mailbox=box, popen=self.popen).get('launched'))
        self.assertEqual(len(self.spawned), 1)                      # 見張り役が生きている間は次を起こさない

        run = self.run_supervisor()
        rec = self.store().read(run)
        self.assertTrue(rec['launch_mark'] and rec['child'] and rec['end_evidence']['mates_empty'])
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        rec = self.store().read(run)
        self.assertEqual(rec['outcome']['label'], '完了')
        self.assertTrue(rec['outcome']['final_signal'])
        self.assertEqual(rec['report_state'], 'received')
        self.assertEqual(box.appends, 1)
        report = parse_reports(box.blocks, expected_reporter='みに')[0]
        self.assertEqual((report.id, report.result), ('💚20990101-1', '完了'))
        self.assertIn('最終の裏付け=あり', report.text)
        handoffs = [k for k in self.patrol.state['outbox'] if k.startswith('mini-handoff:')]
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(self.patrol.lane_only_calls, ['💚20990101-1'])
        # 処理済みの受付：同じ依頼はもう起きない
        self.assertTrue(lane.intake(self.patrol, r).taken)
        self.assertEqual(len(self.store().all_runs()), 1)

    def test_without_final_signal_it_stays_partial(self):
        r = req()
        lane.intake(self.patrol, r)
        box = FakeMailbox()
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        run = self.run_supervisor(mode='no_signal')
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        self.assertEqual(self.store().read(run)['outcome']['label'], '一部完了（最終の裏付けなし）')

    def test_launch_mark_without_end_evidence_holds_everything(self):
        # 完成基準8・17：印のあとで終わりが分からなければ保留。次も起こさない
        r1, r2 = req(number=1), req(number=2)
        lane.intake(self.patrol, r1)
        lane.intake(self.patrol, r2)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999},
                                            'spawn': {'pid': 999999, 'at': 'x'}})
        for _ in range(2):
            lane.tick(self.patrol, [r1, r2], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(self.spawned, [])
        holds = [k for k in self.patrol.state['outbox'] if k.startswith('mini-hold:')]
        self.assertEqual(holds, ['mini-hold:' + run])

    def test_supervisor_that_never_marked_is_released_after_grace(self):
        r = req()
        lane.intake(self.patrol, r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        run = self.store().all_runs()[0]['run_id']
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self.store().update(run, lambda x: {**x, 'spawn': {'pid': 999999, 'at': old}})
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len(self.spawned), 2)                      # 未起動として外れ、もう一度
        self.assertEqual(self.patrol.state['outbox'], {})

    def test_lane_switched_off_before_the_mark_is_not_started(self):
        # 完成基準17：印の直前にオフ・段0・STOP なら起こさない
        r = req()
        lane.intake(self.patrol, r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        (self.root / '.env').write_text('RIRI_MODE=live\nMINI_TRANSPORT=off\n')
        run = self.store().all_runs()[0]['run_id']
        self.assertEqual(lane.run_supervisor(self.cfg, run), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'lane switched off')

    def test_materials_changing_right_before_launch(self):
        # 完成基準7：直前の照合で違えば起こさない。2回続けば保留
        r = req()
        lane.intake(self.patrol, r)
        self.notion.change_on_read = {2, 4}
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(self.spawned, [])
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(self.spawned, [])
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['state'], rec['x4_changed']), ('held', ['case_page']))
        self.assertIn('mini-x4:' + rec['run_id'], self.patrol.state['outbox'])

    def test_missing_rules_means_report_only(self):
        (self.home / '.codex' / 'mini-heroins.md').unlink()
        r = req()
        lane.intake(self.patrol, r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['plan']['row'], rec['plan']['kind']), (1, '報告だけ'))
        self.assertIn('報告だけ', (Path(rec['run_dir']) / 'prompt.txt').read_text())

    def test_stop_blocks_new_launches(self):
        r = req()
        lane.intake(self.patrol, r)
        (self.root / 'STOP').touch()
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(self.spawned, [])


class PatrolHookTests(Base):
    """本物の Patrol を偽の Slack・Notion で回して、組み込みの所だけを確かめる。"""

    def run_patrol(self, cfg, notion, slack, clock):
        from riri.llm import DraftService
        from riri.patrol import Patrol
        from tests.test_patrol_flow import OfflineTransport
        draft = DraftService(OfflineTransport(), enabled=False, report_pages=cfg.pages)
        return Patrol(cfg, slack, notion, draft, clock=clock, notifier=lambda: None).run()

    def setup_patrol(self, recipients):
        from tests.test_patrol_flow import Clock, FakeNotion as FlowNotion, FakeSlack, request
        cfg = Config(root=self.root, llm_enabled=False, expected_bot_id='BTEST', expected_user_id='UTEST',
                     ice_mention_user_id='U0TESTICE000', mode='live', mini_transport='local', mini_stage=1)

        class Notion(FlowNotion):
            def read_page(self, page_id):
                raise AssertionError('案件ページのない依頼では読まない')
        notion = Notion(cfg)
        notion.blocks[cfg.requests_page].insert(0, request(recipients=recipients))
        spawned = []
        hooks = [patch.object(lane, 'spawn_supervisor', lambda c, run, popen=None: spawned.append(run) or os.getpid()),
                 patch('socket.socket', side_effect=AssertionError('network forbidden'))]
        for h in hooks:
            h.start()
            self.addCleanup(h.stop)
        return cfg, notion, FakeSlack(), Clock(), spawned

    def test_mini_only_request_stays_untouched_until_launch(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        self.run_patrol(cfg, notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertFalse(any('みにへの依頼があるので' in t or '【りりからのお願い】' in t for t in texts), texts)
        self.assertEqual([w for w in notion.writes if w[0] == 'status'], [])     # 未着手のまま
        self.assertEqual(len(spawned), 1)
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['plan']['row'], rec['state']), (1, 'spawning'))    # 案件ページなし → 報告だけ
        self.run_patrol(cfg, notion, slack, clock)
        self.assertEqual(len(self.store().all_runs()), 1)

    def test_mixed_recipients_still_instruct_the_others(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        self.run_patrol(cfg, notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertTrue(any('【りりからのお願い】' in t for t in texts), texts)
        self.assertFalse(any('みにへの依頼があるので' in t for t in texts), texts)
        self.assertEqual(len(self.store().all_runs()), 1)

    def test_lane_off_keeps_the_old_notice(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        cfg = replace(cfg, mini_transport='off')
        for _ in range(3):
            self.run_patrol(cfg, notion, slack, clock)
        self.assertTrue(any('みにへの依頼があるので' in p[1] for p in slack.posts))
        self.assertEqual((spawned, self.store().all_runs()), ([], []))


class ConfigTests(unittest.TestCase):
    def test_settings_and_author_map(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, '.env').write_text('MINI_TRANSPORT=local\nMINI_STAGE=7\nMINI_REASONING_EFFORT=huge\n'
                                          'MINI_WRITE_ROOTS=/a:rel:/b\nMINI_TIMEOUT_MIN=0\n')
            cfg = Config.load(root)
        self.assertEqual((cfg.mini_transport, cfg.mini_stage, cfg.mini_reasoning_effort, cfg.mini_write_roots,
                          cfg.mini_timeout_min), ('local', 2, 'max', ('/a', '/b'), 1))
        good = '{"' + 'a' * 32 + '": "たかむね", "' + 'b' * 8 + '-' + 'b' * 24 + '": "のあ"}'
        self.assertEqual(mini_author_map(good), {'a' * 32: 'たかむね', 'b' * 32: 'のあ'})
        for bad in ('{"' + 'a' * 32 + '": "たかむね", "' + 'a' * 32 + '": "のあ"}',   # 1つのIDに2人
                    '{"x": "のあ"}', '{"' + 'a' * 32 + '": "だれか"}', '[1]'):
            with self.assertRaises(AccountMismatch):
                mini_author_map(bad)


if __name__ == '__main__':
    unittest.main()
