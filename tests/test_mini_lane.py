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
FAKE_TOKEN = 'xo' + 'xb-0000-fake'     # トークンの形の文字はその場で組み立てる（v1.1.3 の決まり）


def request_text(number=1, recipients='みに', requester='のあ', status='未着手', page=PID, extra=''):
    return (f"【りりへの依頼】\n依頼ID：💚20990101-{number}\n依頼した子：{requester}\n宛先：{recipients}\n"
            f"状態：{status}\n案件ページ：https://app.notion.com/p/{page}\nやること：設計を読んで感想を3行で{extra}\n"
            "手順：\n1. 読む\n期限・実行タイミング：今日中\nGitHubプロジェクト：なし\n")


def req(**kw):
    return parse_requests([block(request_text(**kw), 'request-' + str(kw.get('number', 1)))])[0]


LEDGER_DS = 'a55c89fd-e231-4dfa-b739-d520b8c5e31e'   # 進行中プロジェクトのデータソース


class FakeNotion:
    def __init__(self):
        self.pages = {PID: [block('案件ページの本文', 'case-1')]}
        self.props = {'名前': 'みにを起こす仕組み'}
        self.parent = {'type': 'data_source_id', 'data_source_id': LEDGER_DS}
        self.requests = []          # りりへの依頼ページのブロック（起こす直前に読み直す）
        self.on_request_read = None
        self.change_on_read = None
        self.reads = 0
        self.request_reads = 0

    def read_blocks(self, page_id):
        if page_id == Config(root=Path('.')).requests_page:
            self.request_reads += 1
            if self.on_request_read:
                self.on_request_read(self, self.request_reads)
            return list(self.requests)
        if page_id == PID:
            self.reads += 1
            if self.change_on_read and self.reads in self.change_on_read:
                return [block('案件ページの本文（書き換え ' + str(self.reads) + '）', 'case-1')]
        return list(self.pages[page_id])

    def read_page(self, page_id):
        return {'object': 'page', 'parent': self.parent, 'properties': self.props,
                'last_edited_time': '2026-11-25T00:00:00.000Z'}


class FakePatrol:
    def __init__(self, cfg, notion):
        self.config, self.notion = cfg, notion
        self.state = {'outbox': {}, 'requests': {}}
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
    summary = "読んだ\\n感想を書いた" + (" xo" + "xb-1-fake" if mode == 'unsafe_summary' else "")
    answer = json.dumps({"run_id": run, "final": True, "result": "完了", "summary": summary,
                         "artifacts": [], "unverified": "なし", "blockers": "なし", "next": "のあが確認する"},
                        ensure_ascii=False)
    home = Path(os.environ['CODEX_HOME'])
    d = home / 'sessions' / '2026' / '11' / '25'
    d.mkdir(parents=True, exist_ok=True)
    phase = 'commentary' if mode == 'no_signal' else 'final_answer'
    other = answer.replace("感想を書いた", "別の本文")
    final_text = other if mode in ('other_final', 'after_turn') else answer
    item = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": final_text}]}}
    (d / ('rollout-2026-11-25T00-00-00-th-' + run + '.jsonl')).write_text(json.dumps(item, ensure_ascii=False) + "\\n")
    out = Path(sys.argv[sys.argv.index('-o') + 1])
    out.write_text(answer)
    if mode == 'no_thread':
        sys.exit(1)            # 会話が始まらないまま失敗する（標準出力は空）
    evs = [{"type": "thread.started", "thread_id": "th-" + run}, {"type": "turn.started"},
           {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": answer}},
           {"type": "turn.completed", "usage": {}}]
    if mode == 'after_turn':
        evs.append({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": other}})
    for e in evs:
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
        self.patches = [patch.object(lane, 'home', lambda: self.home),
                        patch.object(lane, 'versions', lambda cfg: {'codex': 'codex-cli 0.169.0', 'codex_exit': 0, 'macos': '27.2.1'})]
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

    def give(self, *reqs):
        """りりへの依頼ページに、この依頼のブロックを置く（起こす直前の読み直し用）。"""
        self.notion.requests = [block(r.text, r.block_id) for r in reqs]
        return reqs[0] if len(reqs) == 1 else reqs

    def take(self, *reqs):
        for r in reqs:
            lane.intake(self.patrol, r)
        return self.give(*reqs)

    def popen(self, *args, **kw):
        self.spawned.append(args[0])

        class P:
            pid = os.getpid()   # a living PID: the supervisor "is running"
        return P()

    def run_supervisor(self, mode='ok', expect=0):
        """What the detached ``python -m riri.mini_lane supervise <run>`` does, with the fake codex."""
        run = [r for r in self.store().all_runs() if r.get('state') == 'spawning'][0]['run_id']

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        env = {'FAKE_MODE': mode}
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, env), \
                patch.object(lane, 'child_base_env', lambda cfg, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': mode, 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), expect)
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
        self.assertIn('自分宛て', lane.intake(self.patrol, req(requester='みに')).held)
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
        self.take(r)
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
        self.take(r)
        box = FakeMailbox()
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        run = self.run_supervisor(mode='no_signal')
        lane.tick(self.patrol, [r], mailbox=box, popen=self.popen)
        self.assertEqual(self.store().read(run)['outcome']['label'], '一部完了（最終の裏付けなし）')

    def test_launch_mark_without_end_evidence_holds_everything(self):
        # 完成基準8・17：印のあとで終わりが分からなければ保留。次も起こさない
        r1, r2 = req(number=1), req(number=2)
        self.take(r1, r2)
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
        self.take(r)
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
        self.take(r)
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
        self.take(r)
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
        self.take(r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['plan']['row'], rec['plan']['kind']), (1, '報告だけ'))
        self.assertIn('報告だけ', (Path(rec['run_dir']) / 'prompt.txt').read_text())

    def test_stop_blocks_new_launches(self):
        r = req()
        self.take(r)
        (self.root / 'STOP').touch()
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(self.spawned, [])


class ReviewM1Tests(Base):
    """レビュー 💚20261125-5 の M1：起こす直前に依頼ページを読み直し、今日と同じ検査を通す。"""

    def tick(self, r):
        return lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)

    def test_withdrawn_after_reservation_is_not_launched(self):
        r = self.take(req())
        self.give(req(status='完了'))
        self.tick(r)                                   # 巡回が最初に読んだ r（未着手）を渡しても
        self.assertEqual(self.spawned, [])
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'dropped')
        self.assertIn('完了', rec['dropped_reason'])
        self.assertGreaterEqual(self.notion.request_reads, 1)

    def test_body_replaced_after_reservation_is_not_launched(self):
        r = self.take(req())
        self.give(parse_requests([block(request_text().replace('3行', '5行'), 'request-1')])[0])
        self.tick(r)
        self.assertEqual(self.spawned, [])
        self.assertIn('本文', self.store().all_runs()[0]['dropped_reason'])

    def test_duplicate_id_added_after_reservation_waits(self):
        r = self.take(req())
        self.notion.requests.append(block(request_text(), 'request-copy'))
        self.tick(r)
        self.assertEqual(self.spawned, [])
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['state'], rec['last_wait']), ('reserved', '同じ依頼IDの原本が複数ある'))

    def test_change_between_freeze_and_launch_is_caught(self):
        r = self.take(req())

        def withdraw(notion, n):
            if n == 2:                                 # 写しを作ったあと、起こす直前の2回目
                notion.requests = [block(request_text(status='完了'), 'request-1')]
        self.notion.on_request_read = withdraw
        self.tick(r)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.store().all_runs()[0]['state'], 'dropped')

    def test_unsafe_fields_raise_like_todays_path_and_other_text_is_held(self):
        from riri.errors import UnsafeContent
        with self.assertRaises(UnsafeContent):
            lane.intake(self.patrol, req(extra=' ' + FAKE_TOKEN))          # やること（今日も検査する欄）
        held = lane.intake(self.patrol, req(number=2, extra='\nメモ：' + FAKE_TOKEN))   # それ以外の行
        self.assertIn('安全チェック', held.held)
        self.assertEqual(self.store().all_runs(), [])

    def test_unsafe_text_added_after_reservation_is_held(self):
        r = self.take(req())
        self.give(parse_requests([block(request_text() + 'メモ：' + FAKE_TOKEN + '\n', 'request-1')])[0])
        self.tick(r)
        self.assertEqual(self.spawned, [])
        # 本文が変わったので、まず古い予約は外れる（新しい本文は受付で保留になる）
        self.assertEqual(self.store().all_runs()[0]['state'], 'dropped')

    def test_nested_condition_change_is_compared(self):
        # 入れ子（トグル）の中の条件も写しと照合に入る
        self.notion.pages[PID] = [dict(block('条件', 'toggle-1', 'toggle'), has_children=True)]
        self.notion.pages['toggle-1'] = [block('中の条件A', 'inner-1')]
        r = self.take(req())
        calls = {'n': 0}
        original = self.notion.read_blocks

        def read(page_id):
            if page_id == 'toggle-1':
                calls['n'] += 1
                return [block('中の条件' + ('A' if calls['n'] == 1 else 'B'), 'inner-1')]
            return original(page_id)
        self.notion.read_blocks = read
        self.tick(r)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.store().all_runs()[0]['x4_changed'], ['case_page'])
        run = self.store().all_runs()[0]['run_id']
        self.assertIn('  中の条件A', (lane.run_base(self.cfg) / run / 'input' / 'case_page.txt').read_text())

    def test_table_cells_and_links_are_part_of_the_material(self):
        # 表のセル・子ページの題名・リンク先も、写しと照合に入る（入れ子と同じ種類）
        def row(text, i):
            return {'id': i, 'type': 'table_row', 'table_row': {'cells': [[{'plain_text': '条件'}], [{'plain_text': text}]]}}
        self.notion.pages[PID] = [dict(block('', 'table-1', 'table'), has_children=True),
                                  {'id': 'cp', 'type': 'child_page', 'child_page': {'title': '要件の子ページ'}, 'has_children': True},
                                  {'id': 'bm', 'type': 'bookmark', 'bookmark': {'url': 'https://example.com/spec', 'caption': []}}]
        self.notion.pages['table-1'] = [row('3行', 'row-1')]
        r = self.take(req())
        calls = {'n': 0}
        original = self.notion.read_blocks

        def read(page_id):
            if page_id == 'table-1':
                calls['n'] += 1
                return [row('3行' if calls['n'] == 1 else '5行', 'row-1')]
            if page_id == 'cp':
                raise AssertionError('子ページはたどらない')
            return original(page_id)
        self.notion.read_blocks = read
        self.tick(r)
        self.assertEqual(self.spawned, [])
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['x4_changed'], ['case_page'])
        text = (lane.run_base(self.cfg) / rec['run_id'] / 'input' / 'case_page.txt').read_text()
        self.assertIn('条件 | 3行', text)
        self.assertIn('要件の子ページ', text)
        self.assertIn('https://example.com/spec', text)

    def test_secret_value_in_materials_is_held(self):
        self.notion.pages[PID] = [block('トークン：secret-notion-token-value', 'case-1')]
        r = self.take(req())
        self.tick(r)
        self.assertEqual(self.spawned, [])
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'held')
        self.assertIn('mini-held:' + rec['run_id'], self.patrol.state['outbox'])


class ReviewFixTests(Base):
    """レビュー 💚20261125-5 の M2・M4・M5・R1〜R4・R7。"""

    def tick(self, *rs, box=None):
        return lane.tick(self.patrol, list(rs), mailbox=box or FakeMailbox(), popen=self.popen)

    def test_m2_supervisor_rechecks_every_run_inside_the_lock(self):
        r = self.take(req())
        self.tick(r)
        run = self.store().all_runs()[0]['run_id']
        # 巡回の確認のあとで、終わりの分からない別の run ができた
        self.store().update('mini-' + 'f' * 20, lambda x: {'request_id': '💚x', 'block_id': 'b', 'state': 'launched',
                                                         'launch_mark': 'x'})
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'another run is unfinished')

    def test_m1_supervisor_reads_the_request_again_inside_the_lock(self):
        # 巡回の確認から見張り役の印までの数秒で取り下げられても起こさない
        r = self.take(req())
        self.tick(r)
        run = self.store().all_runs()[0]['run_id']
        self.give(req(status='完了'))
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'request recheck: drop')
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)    # 次の巡回で外れる
        self.assertEqual(self.store().read(run)['state'], 'dropped')

    def test_m1_supervisor_does_not_launch_when_the_recheck_cannot_finish(self):
        r = self.take(req())
        self.tick(r)
        run = self.store().all_runs()[0]['run_id']

        class Broken(FakeNotion):
            def read_blocks(self, page_id):
                raise OSError('network down')
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=Broken()), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual((rec['not_started']['reason'], rec['not_started']['detail']), ('request recheck: wait', '依頼ページを読み直せない（OSError）'))

    def test_r7_supervisor_refuses_other_versions(self):
        r = self.take(req())
        self.tick(r)
        run = self.store().all_runs()[0]['run_id']
        with patch.object(lane, 'versions', lambda cfg: {'codex': 'unknown', 'macos': '27.2.1'}):
            self.assertEqual(lane.run_supervisor(self.cfg, run), 1)
        self.assertEqual(self.store().read(run)['not_started']['reason'], 'version mismatch')

    def test_r7_patrol_does_not_spawn_with_other_versions_and_says_so_once(self):
        r = self.take(req())
        with patch.object(lane, 'versions', lambda cfg: {'codex': 'codex-cli 0.170.0', 'macos': '27.2.1'}):
            self.tick(r)
            self.tick(r)
        self.assertEqual(self.spawned, [])
        notes = [k for k in self.patrol.state['outbox'] if k.startswith('mini-version:')]
        self.assertEqual(len(notes), 1)
        self.assertIn('0.170.0', self.patrol.state['outbox'][notes[0]]['text'])

    def test_m4_answer_and_signal_from_different_texts(self):
        for mode, label in (('other_final', '一部完了（最終の裏付けなし）'), ('after_turn', 'できなかった')):
            with self.subTest(mode):
                for rec in self.store().all_runs():
                    (self.store().dir / (rec['run_id'] + '.json')).unlink()
                r = self.take(req(number=10 if mode == 'other_final' else 11))
                self.tick(r)
                run = self.run_supervisor(mode=mode)
                self.tick(r)
                out = self.store().read(run)['outcome']
                self.assertEqual(out['label'], label)
                self.assertFalse(out.get('final_signal'))

    def test_m5_ledger_must_be_the_official_one(self):
        self.notion.parent = {'type': 'data_source_id', 'data_source_id': '0' * 32}
        r = self.take(req())
        self.tick(r)
        rec = self.store().all_runs()[0]
        self.assertIn('ledger_row', rec['missing_always'])
        self.assertEqual((rec['plan']['row'], rec['plan']['kind']), (1, '報告だけ'))

    def test_m5_stage_2_reports_only_until_work_targets_exist(self):
        cfg = replace(self.cfg, mini_stage=2, mini_write_roots=(str(self.home / 'みに' / '案件'),))
        self.patrol = FakePatrol(cfg, self.notion)
        self.cfg = cfg
        r = self.take(req())
        self.tick(r)
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['plan']['row'], rec['plan']['write'], rec['plan']['reason']), (5, [], '資料不足'))
        self.assertIn('targets', rec['missing_write'])

    def test_r2_no_handoff_without_thread_started(self):
        r = self.take(req())
        box = FakeMailbox()
        self.tick(r, box=box)
        run = self.run_supervisor(mode='no_thread')
        self.tick(r, box=box)
        self.tick(r, box=box)
        rec = self.store().read(run)
        self.assertEqual(rec['handoff_state'], 'skipped')
        self.assertFalse([k for k in self.patrol.state['outbox'] if k.startswith('mini-handoff:')])
        self.assertEqual(self.patrol.lane_only_calls, [])               # 対応中にもしない
        self.assertEqual((rec['outcome']['label'], box.appends), ('できなかった', 1))

    def test_r4_manual_end_evidence_resolves_the_hold(self):
        r1, r2 = req(number=1), req(number=2)
        self.take(r1, r2)
        first = sorted(self.store().all_runs(), key=lambda x: x['reserved_at'])[0]['run_id']
        self.store().update(first, lambda x: {**x, 'state': 'launched', 'launch_mark': 'x', 'child': {'pid': 999999, 'pgid': 999999},
                                              'spawn': {'pid': 999999, 'at': 'x'},
                                              'end_unconfirmed': {'reason': 'run mates still alive', 'at': 'x'},
                                              'run_dir': str(self.root), 'stdout_path': str(self.root / 'none.jsonl')})
        self.tick(r1, r2)
        self.assertEqual(self.spawned, [])
        with self.assertRaises(SafetyError):
            lane.record_end(self.cfg, first, 'のあ', lister=lambda run, pgid: set())
        with self.assertRaises(SafetyError):
            lane.record_end(self.cfg, first, 'たかむね', lister=lambda run, pgid: {4242})
        lane.record_end(self.cfg, first, 'たかむね', lister=lambda run, pgid: set())
        rec = self.store().read(first)
        self.assertIn('end_unconfirmed', rec)                              # 履歴は残す
        self.assertEqual(rec['end_evidence']['kind'], 'manual')
        self.tick(r1, r2)
        self.assertEqual(len(self.spawned), 1)                             # 次を起こせる
        self.assertEqual(self.store().read(first)['outcome']['label'], 'できなかった')
        self.assertEqual(len([k for k in self.patrol.state['outbox'] if k.startswith('mini-hold:')]), 1)

    def finished_run(self, number):
        r = self.take(req(number=number))
        self.tick(r)
        return self.run_supervisor()

    def test_r3_unknown_append_is_never_repeated_and_does_not_block_others(self):
        from riri.errors import RemoteError
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))

        class Box(FakeMailbox):
            reads = 0
            def find(self, record, body):
                if record['run_id'] == a:
                    Box.reads += 1
                    if Box.reads == 2:
                        raise RemoteError('read refused')
                return super().find(record, body)
            def append(self, record, anchor, body):
                if record['run_id'] == a:
                    self.appends += 1
                    raise RemoteError('timeout', uncertain=True)
                return super().append(record, anchor, body)
        box = Box()
        start = datetime.now(timezone.utc)
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, start)
        self.assertEqual(self.store().read(a)['report_state'], 'unknown')
        for minutes in (5, 10):                                            # 読めない・まだ無い
            lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, start + timedelta(minutes=minutes))
        self.assertEqual((box.appends, self.store().read(a)['report_state']), (1, 'unknown'))
        # 別の run の報告は、a が分からないままでも進む
        b = self.finished_run(2)
        with patch.object(lane, 'now', lambda: start + timedelta(minutes=12)):
            self.tick(req(number=2), box=box)
        self.assertEqual(self.store().read(b)['report_state'], 'received')
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, start + timedelta(minutes=31))
        self.assertEqual(self.store().read(a)['report_state'], 'held')
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, start + timedelta(minutes=40))
        self.assertEqual(box.appends, 2)                                   # a は1回だけ（もう1回は b）
        self.assertEqual(len([k for k in self.patrol.state['outbox'] if k.startswith('mini-report-hold:')]), 1)

    def test_unsafe_answer_holds_only_that_report(self):
        # 1件の報告の失敗が、ほかの報告と次の起動を止めない（R3 と同じ種類）
        r1 = self.take(req(number=1))
        box = FakeMailbox()
        self.tick(r1, box=box)
        a = self.run_supervisor(mode='unsafe_summary')
        r2 = req(number=2)
        lane.intake(self.patrol, r2)
        self.give(r1, r2)
        self.tick(r1, r2, box=box)
        self.tick(r1, r2, box=box)
        self.assertEqual(self.store().read(a)['report_state'], 'held')
        self.assertEqual(box.appends, 0)                                    # 本文は転記しない
        self.assertIn('mini-report-hold:' + a, self.patrol.state['outbox'])
        self.assertEqual(len(self.spawned), 2)                              # 次の依頼は起こせた

    def test_unreadable_result_does_not_stop_later_patrols(self):
        r = self.take(req())
        self.store().update('mini-' + 'e' * 20, lambda x: {'request_id': '💚20990101-9', 'block_id': 'b9', 'requester': 'のあ',
                                                         'fields': {}, 'state': 'launched', 'launch_mark': 'x',
                                                         'end_evidence': {'kind': 'watched', 'mates_empty': True,
                                                                          'exit_code': 0, 'reason': 'exited',
                                                                          'at': '2026-11-25T00:00:00+00:00'}})
        self.tick(r)                                                         # run_dir がなくても止まらない
        broken = self.store().read('mini-' + 'e' * 20)
        self.assertEqual(broken['outcome']['label'], 'できなかった')
        self.assertIn('KeyError', broken['outcome']['reason'])
        self.assertEqual(len(self.spawned), 1)

    def test_report_body_is_fixed_once(self):
        from riri.errors import RemoteError
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))

        class Box(FakeMailbox):
            def append(self, record, anchor, body):
                self.appends += 1
                raise RemoteError('timeout', uncertain=True)
        box = Box()
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol)
        with patch.object(lane, 'DESIGN_VERSION', 'another-version'):       # 途中でコードが変わっても
            lane.deliver_one(self.cfg, self.store(), self.store().read(a), box, self.patrol, datetime.now(timezone.utc))
        self.assertEqual(self.store().read(a)['report_state'], 'unknown')     # 止まらず読み続ける

    def test_r3_explicit_refusal_may_be_retried(self):
        from riri.errors import RemoteError
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))

        class Box(FakeMailbox):
            refuse = True
            def append(self, record, anchor, body):
                if Box.refuse:
                    Box.refuse = False
                    raise RemoteError('400 validation', uncertain=False)
                return super().append(record, anchor, body)
        box = Box()
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol)
        self.assertEqual(self.store().read(a)['report_state'], 'pending')
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol)
        self.assertEqual(self.store().read(a)['report_state'], 'received')


class ReReviewTests(Base):
    """再検証 💚20261125-8 の S1〜S7（前回の一部解消 M1・M4・R1・R3・R6・R7 を含む）。"""

    def tick(self, *rs, box=None):
        return lane.tick(self.patrol, list(rs), mailbox=box or FakeMailbox(), popen=self.popen)

    def spawning(self):
        r = self.take(req())
        self.tick(r)
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['state'], 'spawning')
        return r, rec['run_id']

    # ---- S1
    def test_s1_material_withdrawn_before_the_mark_stops_the_launch(self):
        r, run = self.spawning()
        self.notion.pages[PID] = [block('承認を撤回', 'case-1')]               # 巡回の確認のあと、印の前
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual((rec['not_started']['reason'], rec['not_started']['detail']),
                         ('request recheck: materials', 'case_page'))

    def test_s1_link_target_change_is_a_material_change(self):
        def para(url):
            return {'id': 'case-1', 'type': 'paragraph', 'paragraph': {'rich_text': [
                {'plain_text': '承認済みの要件', 'href': url, 'text': {'content': '承認済みの要件', 'link': {'url': url}}}]}}
        self.notion.pages[PID] = [para('https://example.com/v1')]
        r, run = self.spawning()
        text = (lane.run_base(self.cfg) / run / 'input' / 'case_page.txt').read_text()
        self.assertIn('〈https://example.com/v1〉', text)                      # 写しにリンク先が入る
        self.notion.pages[PID] = [para('https://example.com/v2')]              # 見た目は同じ、行き先だけ違う
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assertEqual(self.store().read(run)['not_started']['reason'], 'request recheck: materials')

    # ---- S2
    def outcome_with(self, reason, exit_code=0, extra_stdout=b''):
        r, run = self.spawning()
        self.run_supervisor()
        rec = self.store().read(run)
        if extra_stdout:
            with open(rec['stdout_path'], 'ab') as out:
                out.write(extra_stdout)
        rec = self.store().update(run, lambda x: {**x, 'outcome': None,
                                                   'end_evidence': {**x['end_evidence'], 'reason': reason, 'exit_code': exit_code}})
        return lane.outcome(self.cfg, rec)

    def test_s2_stop_and_timeout_are_never_complete(self):
        self.assertEqual(self.outcome_with('stop')['label'], '一部完了（STOPで中断）')
        for x in self.store().all_runs():
            (self.store().dir / (x['run_id'] + '.json')).unlink()
        self.assertEqual(self.outcome_with('timeout')['label'], 'できなかった（時間切れ）')

    def test_s2_output_beyond_the_limit_is_not_judged_from_its_head(self):
        with patch.object(lane, 'STDOUT_LIMIT', 2000):
            out = self.outcome_with('exited', extra_stdout=b'{"type": "turn.failed"}\n' * 200)
        self.assertEqual(out['label'], 'できなかった')
        self.assertIn('大きすぎ', out['reason'])

    # ---- S3
    def test_s3_pre_release_and_failed_version_query_are_refused(self):
        for found in ({'codex': 'codex-cli 0.169.0-alpha.11.1', 'codex_exit': 0, 'macos': '27.2.1'},
                      {'codex': 'codex-cli 0.169.0', 'codex_exit': 1, 'macos': '27.2.1'},
                      {'codex': 'codex-cli 0.169.0 extra', 'codex_exit': 0, 'macos': '27.2.1'},
                      {'codex': 'codex-cli 0.169.0', 'codex_exit': 0, 'macos': '27.2.1.1'},
                      {'codex': 'codex-cli 0.169.0', 'codex_exit': None, 'macos': '27.2.1'}):
            with self.subTest(found), patch.object(lane, 'versions', lambda cfg, f=found: f):
                self.assertIsNotNone(lane.version_mismatch(self.cfg))
        self.assertIsNone(lane.version_mismatch(self.cfg))                     # 確かめた組合せは通る

    # ---- S4
    def test_s4_others_status_without_riris_receipt_does_not_launch(self):
        r = req(recipients='うさ、みに')
        self.take(r)
        self.give(req(recipients='うさ、みに', status='対応中'))                # 人が状態だけ変えた（りりの受付記録なし）
        self.tick(r)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.store().all_runs()[0]['state'], 'dropped')
        # りりがほかの宛先に届けた記録があれば、同じ「対応中」でも起こす
        self.store().update(self.store().all_runs()[0]['run_id'],
                            lambda x: {k: v for k, v in x.items() if not k.startswith('dropped')} | {'state': 'reserved'})
        self.patrol.state['requests'][r.id] = {'status': 'posted', 'block_id': r.block_id}
        self.tick(r)
        self.assertEqual(len(self.spawned), 1)

    # ---- S5
    def finished_run(self, number, box=None):
        r = self.take(req(number=number))
        self.tick(r, box=box)
        run = self.run_supervisor()
        lane.settle(self.cfg, self.store(), self.store().read(run))
        return run

    def test_s5_a_pending_report_that_keeps_failing_does_not_block_the_next(self):
        from riri.errors import RemoteError
        failing = set()

        class Box(FakeMailbox):
            def find(self, record, body):
                if record['run_id'] in failing:
                    raise RemoteError('read refused')
                return super().find(record, body)
        box = Box()
        a = self.finished_run(1, box)
        failing.add(a)
        start = datetime.now(timezone.utc)
        b = self.finished_run(2, box)

        def tick_at(minutes):
            lane.tick(self.patrol, [], mailbox=box, popen=self.popen, clock=lambda: start + timedelta(minutes=minutes))
        for minutes in (0, 5):
            tick_at(minutes)
        self.assertEqual(self.store().read(b)['report_state'], 'received')     # 2件目は進む
        self.assertEqual(self.store().read(a)['report_state'], 'pending')
        tick_at(40)
        self.assertEqual(self.store().read(a)['report_state'], 'held')
        self.assertIn('mini-report-hold:' + a, self.patrol.state['outbox'])

    def test_s5_hold_notice_that_failed_is_sent_on_a_later_patrol(self):
        a = self.finished_run(1)
        original = self.patrol.queue

        def broken(*args, **kw):
            raise OSError('queue write failed')
        self.patrol.queue = broken
        lane.hold_report(self.cfg, self.store(), self.store().read(a), self.patrol, datetime.now(timezone.utc), '試験')
        self.assertEqual(self.store().read(a)['report_state'], 'held')
        self.assertNotIn('mini-report-hold:' + a, self.patrol.state['outbox'])
        self.patrol.queue = original
        self.tick(box=FakeMailbox())
        self.assertIn('mini-report-hold:' + a, self.patrol.state['outbox'])
        self.assertEqual(self.store().read(a)['notices']['mini-report-hold:' + a]['state'], 'queued')

    def test_s5_null_line_in_stdout_isolates_only_that_run(self):
        r, run = self.spawning()
        self.run_supervisor()
        with open(self.store().read(run)['stdout_path'], 'ab') as out:
            out.write(b'null\n')
        r2 = req(number=2)
        lane.intake(self.patrol, r2)
        self.give(r, r2)
        for _ in range(2):
            self.tick(r, r2)
        self.assertEqual(self.store().read(run)['outcome']['label'], 'できなかった')
        self.assertEqual(len(self.spawned), 2)                                 # 後ろの依頼も起こせた

    # ---- 同じ種類（再検証の前の詰め）
    def test_supervisor_refuses_a_changed_prompt_or_profile(self):
        for name in ('prompt.txt', 'profile.sb'):
            with self.subTest(name):
                for x in self.store().all_runs():
                    (self.store().dir / (x['run_id'] + '.json')).unlink()
                r, run = self.spawning()
                path = Path(self.store().read(run)['run_dir']) / name
                path.write_text(path.read_text() + '\n追加の指示\n')
                self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
                self.assertEqual(self.store().read(run)['not_started']['reason'], 'request recheck: files')

    def test_a_reservation_that_cannot_be_prepared_is_held_and_the_next_goes(self):
        r1, r2 = req(number=1), req(number=2)
        self.take(r1, r2)
        real = lane.freeze
        calls = {'n': 0}

        def freeze(cfg, run, record, materials, plan):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('想定外')
            return real(cfg, run, record, materials, plan)
        with patch.object(lane, 'freeze', freeze):
            self.tick(r1, r2)
            self.tick(r1, r2)
        states = sorted(x['state'] for x in self.store().all_runs())
        self.assertEqual(states, ['held', 'spawning'])
        self.assertEqual(len(self.spawned), 1)

    def test_an_error_while_announcing_one_run_does_not_stop_the_tick(self):
        r, run = self.spawning()
        self.run_supervisor()
        r2 = req(number=2)
        lane.intake(self.patrol, r2)
        self.give(r, r2)

        def broken(req):
            raise OSError('status write failed')
        self.patrol.lane_only = broken
        self.tick(r, r2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.store().read(run)['report_state'], 'received')

    def test_artifacts_must_be_text(self):
        self.assertFalse(lane._validate({'run_id': 'r', 'final': True, 'result': '完了', 'summary': 's', 'artifacts': [1],
                                         'unverified': '', 'blockers': '', 'next': ''}))
        self.assertTrue(lane._validate({'run_id': 'r', 'final': True, 'result': '完了', 'summary': 's', 'artifacts': ['a'],
                                        'unverified': '', 'blockers': '', 'next': ''}))

    # ---- S7
    def test_s7_incomplete_watcher_evidence_is_not_an_end(self):
        base = {'kind': 'watched', 'mates_empty': True, 'exit_code': 0, 'reason': 'exited', 'at': '2026-11-25T00:00:00+00:00'}
        self.assertTrue(lane.ended({'end_evidence': base}))
        for drop in ('exit_code', 'at', 'reason'):
            with self.subTest(drop):
                self.assertFalse(lane.ended({'end_evidence': {k: v for k, v in base.items() if k != drop}}))
        self.assertFalse(lane.ended({'end_evidence': {**base, 'exit_code': True}}))
        self.assertFalse(lane.ended({'end_evidence': {'kind': 'launch_failed'}}))
        self.assertTrue(lane.ended({'end_evidence': {'kind': 'launch_failed', 'error': 'OSError', 'at': 'x'}}))


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
        # レビュー R1：りり自身が共有の状態を対応中にしても、みにの予約は消えずに起こされる
        rec = self.store().all_runs()[0]
        self.assertEqual((rec['state'], rec['shared']), ('spawning', ['うさ']))
        self.assertEqual(len(spawned), 1)
        state = json.loads((self.root / 'state' / 'state.json').read_text(encoding='utf-8'))
        info = state['requests']['💛20990101-1']
        self.assertIn('みに', info.get('lane_recipients', []))           # みにの報告も完了の条件に入る

    def test_held_mini_part_does_not_block_the_others(self):
        # レビュー R1：続きの依頼でみにの分を保留しても、ほかの宛先には届ける
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        first = notion.blocks[cfg.requests_page][0]
        first['code']['rich_text'][0]['text']['content'] += '元の依頼ID：💛20990101-0\n'
        self.run_patrol(cfg, notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertTrue(any('【りりからのお願い】' in t for t in texts), texts)
        self.assertTrue(any('みにの分は、自動では起こさずに保留' in t for t in texts), texts)
        self.assertFalse(any('処理できません' in t for t in texts), texts)
        self.assertEqual((spawned, self.store().all_runs()), ([], []))

    def test_s4_held_mini_part_keeps_the_request_open(self):
        # 再検証 S4：みにの分を保留しても、うさの報告だけで依頼全体は完了にしない
        from tests.test_patrol_flow import report
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        first = notion.blocks[cfg.requests_page][0]
        first['code']['rich_text'][0]['text']['content'] += '元の依頼ID：💛20990101-0\n'
        self.run_patrol(cfg, notion, slack, clock)
        notion.blocks[cfg.pages['うさ'].split('/')[-1]].insert(0, report(name='うさ'))
        self.run_patrol(cfg, notion, slack, clock)
        self.assertNotIn(('status', 'request-1', '完了'), notion.writes)
        state = json.loads((self.root / 'state' / 'state.json').read_text(encoding='utf-8'))
        self.assertIn('みに', state['requests']['💛20990101-1'].get('lane_recipients', []))

    def test_self_addressed_mini_part_does_not_block_the_others(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        first = notion.blocks[cfg.requests_page][0]
        first['code']['rich_text'][0]['text']['content'] = first['code']['rich_text'][0]['text']['content'].replace(
            '依頼した子：うさ', '依頼した子：みに')
        self.run_patrol(cfg, notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertTrue(any('【りりからのお願い】' in t for t in texts), texts)
        self.assertTrue(any('自分宛て' in t for t in texts), texts)
        self.assertEqual((spawned, self.store().all_runs()), ([], []))

    def test_unsafe_mini_only_request_is_quarantined_like_today(self):
        # レビュー M1：今日の入力保護は、みにだけの依頼にもかかる
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        first = notion.blocks[cfg.requests_page][0]
        text = first['code']['rich_text'][0]['text']['content']
        first['code']['rich_text'][0]['text']['content'] = text.replace('手順を確認する', '手順を確認する ' + FAKE_TOKEN)
        self.run_patrol(cfg, notion, slack, clock)
        self.assertTrue(any('安全チェックに通らない依頼を1件保留' in p[1] for p in slack.posts))
        self.assertEqual((spawned, self.store().all_runs()), ([], []))

    def test_wrong_mini_setting_keeps_the_patrol_running(self):
        # レビュー R6：みにの設定の誤りは、みにの受け口だけを止める
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        cfg = replace(cfg, mini_config_errors=('MINI_AUTHOR_MAP',))
        self.run_patrol(cfg, notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertTrue(any('【りりからのお願い】' in t for t in texts), texts)
        self.assertTrue(any('みにへの依頼があるので' in t for t in texts), texts)   # 今日の道
        self.assertEqual((spawned, self.store().all_runs()), ([], []))

    def test_lane_off_keeps_the_old_notice(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        cfg = replace(cfg, mini_transport='off')
        for _ in range(3):
            self.run_patrol(cfg, notion, slack, clock)
        self.assertTrue(any('みにへの依頼があるので' in p[1] for p in slack.posts))
        self.assertEqual((spawned, self.store().all_runs()), ([], []))


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as root:
            Path(root, '.env').write_text('RIRI_MODE=live\n' + text)
            return Config.load(root)

    def test_wrong_settings_stop_only_the_mini_lane(self):
        # レビュー M5・R6：誤りは丸めずに名前だけ記録し、みにの受け口だけを止める（巡回は止めない）
        cfg = self.load('MINI_TRANSPORT=local\nMINI_STAGE=7\nMINI_REASONING_EFFORT=huge\n'
                        'MINI_WRITE_ROOTS=/a:rel:/b\nMINI_TIMEOUT_MIN=0\n')
        self.assertEqual(set(cfg.mini_config_errors),
                         {'MINI_STAGE', 'MINI_REASONING_EFFORT', 'MINI_WRITE_ROOTS', 'MINI_TIMEOUT_MIN'})
        self.assertEqual(cfg.mini_stage, 0)              # 7 を段2に丸めない
        self.assertFalse(lane.lane_on(cfg))
        good = self.load('MINI_TRANSPORT=local\nMINI_STAGE=2\nMINI_WRITE_ROOTS=/a:/b\nMINI_TIMEOUT_MIN=30\n')
        self.assertEqual((good.mini_config_errors, good.mini_stage, good.mini_write_roots, good.mini_timeout_min),
                         ((), 2, ('/a', '/b'), 30))
        self.assertTrue(lane.lane_on(good))

    def test_unused_bad_author_map_does_not_stop_the_patrol(self):
        cfg = self.load('MINI_TRANSPORT=off\nMINI_AUTHOR_MAP={"x": "のあ"}\nMINI_RIRI_NOTION_IDS=zz\n')
        self.assertEqual(set(cfg.mini_config_errors), {'MINI_AUTHOR_MAP', 'MINI_RIRI_NOTION_IDS'})
        self.assertEqual(cfg.mini_author_map, {})
        self.assertFalse(lane.lane_on(cfg))
        for bad in ('MINI_TRANSPORT=lokal', 'MINI_STAGE=1.0', 'MINI_STAGE=-1', 'MINI_CODEX_BIN=codex',
                    'MINI_VERIFIED_CODEX=latest', 'MINI_LEDGER_IDS=abc'):
            self.assertTrue(self.load('MINI_TRANSPORT=local\nMINI_STAGE=1\n' + bad + '\n').mini_config_errors, bad)

    def test_s6_unreadable_mini_line_stops_only_the_mini_lane(self):
        # 再検証 S6：MINI_* の行の引用符が壊れていても、巡回の設定は読める
        cfg = self.load('MINI_TRANSPORT=off\nMINI_AUTHOR_MAP="{\\"a\\": 1\nMINI_STAGE 2\n')
        self.assertEqual(set(cfg.mini_config_errors), {'MINI_AUTHOR_MAP', 'MINI_STAGE'})
        self.assertEqual(cfg.mode, 'live')
        with self.assertRaises(ValueError):
            self.load('SLACK_BOT_TOKEN="broken\n')                            # ほかの行はこれまでどおり止まる

    def test_author_map(self):
        good = '{"' + 'a' * 32 + '": "たかむね", "' + 'b' * 8 + '-' + 'b' * 24 + '": "のあ"}'
        self.assertEqual(mini_author_map(good), {'a' * 32: 'たかむね', 'b' * 32: 'のあ'})
        for bad in ('{"' + 'a' * 32 + '": "たかむね", "' + 'a' * 32 + '": "のあ"}',   # 1つのIDに2人
                    '{"x": "のあ"}', '{"' + 'a' * 32 + '": "だれか"}', '[1]'):
            with self.assertRaises(AccountMismatch):
                mini_author_map(bad)


if __name__ == '__main__':
    unittest.main()
