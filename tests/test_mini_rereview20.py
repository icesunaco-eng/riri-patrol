"""再検証 💚20261125-20 の U1・U2・U4〜U8（#23 の範囲）。仮のホーム・偽の Notion・偽の Codex だけで通す。"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from riri import mini_lane as lane
from riri import mini_supervisor
from riri.parse import parse_requests
from tests import test_mini_lane
from tests.test_mini_lane import Base, FakeMailbox, req, request_text


def rich_block(parts, block_id='request-1'):
    """parts: [(words, link or None)]。リンクのある所だけ href を付けたコードブロック。"""
    items = []
    for words, link in parts:
        item = {'type': 'text', 'text': {'content': words}, 'plain_text': words}
        if link:
            item['text']['link'] = {'url': link}
            item['href'] = link
        items.append(item)
    return {'id': block_id, 'type': 'code', 'code': {'rich_text': items, 'language': 'plain text'}}


class Helpers(Base):
    def tick(self, *rs, popen=None, mailbox=None):
        return lane.tick(self.patrol, list(rs), mailbox=mailbox or FakeMailbox(), popen=popen or self.popen)

    def spawned_run(self, r=None):
        r = r or self.take(req())
        self.tick(r)
        return self.store().all_runs()[0]['run_id']


class U1Tests(Helpers):
    def test_switched_off_during_the_last_version_query_does_not_launch(self):
        run = self.spawned_run()
        env = self.root / '.env'
        calls = []

        def versions(cfg, *a, **k):
            calls.append(1)
            if len(calls) == 2:          # 起動の直前の照会（最大20秒）の間にオフにされる
                env.write_text(env.read_text().replace('MINI_TRANSPORT=local', 'MINI_TRANSPORT=off'))
            return {'codex': 'codex-cli 0.169.0', 'codex_exit': 0, 'macos': '27.2.1'}
        with patch.object(lane, 'versions', versions):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'lane switched off')

    def test_the_snapshot_reads_the_file_it_checks(self):
        # 確認が通ったあとで AGENTS.md がシンボリックリンクに替わっても、スナップショットは通さない
        run = self.spawned_run()
        agents = self.home / '.codex-riri' / 'AGENTS.md'
        other = self.home / 'other-agents.md'
        other.write_text('別の指示')
        agents.unlink()
        agents.symlink_to(other)
        real = lane.home_problems
        # スナップショットの外の確認（巡回の確認など）だけは通ってしまったことにする
        with patch.object(lane, 'home_problems', lambda cfg, files=None: [] if files is None else real(cfg, files)):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        rec = self.store().read(run)
        self.assertFalse(rec.get('launch_mark'))
        self.assertEqual(rec['not_started']['reason'], 'dedicated home')


class U2Tests(Helpers):
    def test_a_spawn_time_without_time_zone_is_broken_not_a_crash(self):
        self.allow_broken = True
        run = self.spawned_run()
        self.store().update(run, lambda r: {**r, 'spawn': {**r['spawn'], 'at': '2026-11-25T00:00:00', 'pid': 999999}})
        out = self.tick(req())                                           # TypeError で止まらない
        self.assertEqual(out.get('broken'), 1)


class U4Tests(Base):
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol

    def test_a_request_with_a_mention_is_held_before_any_launch(self):
        text = request_text()
        head, tail = text.split('設計', 1)
        blk = {'id': 'request-1', 'type': 'code', 'code': {'rich_text': [
            {'type': 'text', 'text': {'content': head}, 'plain_text': head},
            {'type': 'mention', 'mention': {'type': 'page', 'page': {'id': '3' * 32}}, 'plain_text': '設計'},
            {'type': 'text', 'text': {'content': tail}, 'plain_text': tail}]}}
        r = parse_requests([blk])[0]
        out = lane.intake(self.patrol, r)
        self.assertTrue(out.taken)
        self.assertIn('メンション', out.held)
        self.assertEqual(self.store().all_runs(), [])                    # 起こす前に止まる

    def test_the_handoff_hold_never_asks_for_a_new_id(self):
        from riri.errors import StatusMissing
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        real = notion.set_status

        def set_status(bid, original, status, **k):
            if status == '対応中':
                raise StatusMissing('Status line is missing or ambiguous')
            return real(bid, original, status, **k)
        notion.set_status = set_status
        self.run_patrol(cfg, notion, slack, clock)
        run = spawned[0]

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, run, notion=notion), 0)
        clock.advance()
        self.run_patrol(cfg, notion, slack, clock)
        state = json.loads((self.root / 'state' / 'state.json').read_text(encoding='utf-8'))
        info = state['requests']['💛20990101-1']
        self.assertEqual(info['status_unwritable_stage'], 'handoff')
        text = state['outbox']['status-unwritable:💛20990101-1']['text']
        self.assertNotIn('新しい依頼IDで書き直して', text)
        self.assertIn('checked', text)


class U5Tests(Helpers):
    TEXT = request_text().replace('設計を読んで感想を3行で', '設計を読んで、設計の感想を3行で')

    def blocks(self, first):
        head, rest = self.TEXT.split('設計', 1)
        mid, tail = rest.split('設計', 1)
        url = 'https://example.com/a'
        return rich_block([(head, None), ('設計', url if first else None), (mid, None),
                           ('設計', None if first else url), (tail, None)])

    def test_a_link_moved_to_the_same_words_elsewhere_stops_the_launch(self):
        first = parse_requests([self.blocks(True)])[0]
        lane.intake(self.patrol, first)
        self.notion.requests = [self.blocks(True)]
        self.tick(first)
        run = self.store().all_runs()[0]['run_id']
        self.notion.requests = [self.blocks(False)]                        # 同じリンクを、もう片方の「設計」へ
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        # 受付の決まり方（本文の正規化）にリンクを入れたので、本文が変わったとして先に止まる
        self.assertIn(self.store().read(run)['not_started']['reason'], ('request recheck: drop', 'request recheck: materials'))

    def test_an_unresolvable_mention_in_use_is_a_missing_material(self):
        text = request_text().replace('GitHubプロジェクト：なし\n', 'GitHubプロジェクト：なし\n使うもの：要件のページ\n')
        head, tail = text.split('要件のページ', 1)
        blk = {'id': 'request-1', 'type': 'code', 'code': {'rich_text': [
            {'type': 'text', 'text': {'content': head}, 'plain_text': head},
            {'type': 'text', 'text': {'content': '要件のページ', 'link': {'url': ''}}, 'plain_text': '要件のページ',
             'href': ''},
            {'type': 'text', 'text': {'content': tail}, 'plain_text': tail}]}}
        r = parse_requests([blk])[0]
        record = {'block_id': 'request-1', 'fields': r.fields}
        _, _, missing, _ = lane.gather(self.cfg, record, r.text, self.notion)
        self.assertTrue([m for m in missing if m.startswith('use_')], missing)

    def test_a_link_only_change_after_launch_is_held(self):
        first = parse_requests([self.blocks(True)])[0]
        lane.intake(self.patrol, first)
        run = self.store().all_runs()[0]['run_id']
        self.store().update(run, lambda r: {**r, 'state': 'launched', 'launch_mark': lane.iso()})
        moved = parse_requests([self.blocks(False)])[0]
        self.assertIn('本文だけ', lane.intake(self.patrol, moved).held)   # B6：起動済みの受付のリンクだけ変わった


class U6Tests(Helpers):
    def test_a_stop_after_the_third_handoff_failure_still_tells(self):
        class Crash(BaseException):
            pass
        self.spawned_run()
        run = self.run_supervisor()
        real_queue = self.patrol.queue

        def queue(key, *a, **k):
            if key.startswith('mini-handoff:'):
                raise RuntimeError('slack queue broken')
            return real_queue(key, *a, **k)
        self.patrol.queue = queue
        real_notify = lane.notify
        calls = []

        def notify(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise Crash()                          # 3回目を数えて保存した直後、知らせの前で止まる
            return real_notify(*a, **k)
        with patch.object(lane, 'notify', notify):
            for _ in range(lane.ANNOUNCE_FAIL_NOTICE - 1):
                self.tick(req())
            with self.assertRaises(Crash):
                self.tick(req())
        for _ in range(2):
            self.tick(req())
        self.assertIn('mini-announce:' + run, self.patrol.state['outbox'])


class U7Tests(Helpers):
    def test_mixed_failures_before_the_mark_are_held_after_three(self):
        a, b = req(number=1), req(number=2)
        self.take(a, b)
        oldest = sorted(self.store().all_runs(), key=lambda r: r['reserved_at'])[0]['run_id']
        tries = []

        def popen(argv, *x, **k):
            if argv[-1] == oldest:
                tries.append(1)
                if len(tries) % 3 != 0:
                    raise OSError('cannot start')          # 2回は見張り役を作れない
            return self.popen(argv, *x, **k)
        for _ in range(8):
            self.tick(a, b, popen=popen)
            rec = self.store().read(oldest)
            if rec['state'] == 'spawning':               # 3回目は作れるが、見張り役は起こさずに終わる
                self.store().update(oldest, lambda r: {**r, 'not_started': {'reason': 'global lock busy', 'at': lane.iso()}})
            if rec['state'] == 'held':
                break
        self.assertEqual(self.store().read(oldest)['state'], 'held')
        self.assertLessEqual(len(tries), 3)                                # 起動の印に届かない試みは合わせて3回まで


class U8Tests(Helpers):
    def test_a_broken_last_json_keeps_the_stop_label(self):
        self.spawned_run()
        run = self.run_supervisor()
        rec = self.store().update(run, lambda r: {**r, 'outcome': None,
                                                  'end_evidence': {**r['end_evidence'], 'reason': 'stop'}})
        (Path(rec['run_dir']) / 'out' / 'last.json').write_bytes('{"result": "完了'.encode('utf-8')[:-1])
        self.assertEqual(lane.outcome(self.cfg, rec)['label'], '一部完了（STOPで中断）')


class PerspectiveTests(Helpers):
    """7つの観点を、今回の直しと周りに当てた分。"""

    def test_1_the_binary_moved_during_the_version_query_does_not_launch(self):
        run = self.spawned_run()
        answers = iter(['/a/codex'] * 3 + ['/b/codex'] * 10)              # 照会の最中に別の本体へ
        with patch.object(lane, 'codex_bin', lambda cfg: next(answers)):
            self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assertFalse(self.store().read(run).get('launch_mark'))


class TruncatedFileTests(Helpers):
    """観点⑦：ファイルが文字の途中で切れても、例外で止まらず、起こさない側に倒れる。"""

    def test_a_config_cut_in_the_middle_of_a_character(self):
        run = self.spawned_run()
        conf = self.home / '.codex-riri' / 'config.toml'
        conf.write_bytes(conf.read_bytes() + '# メモ'.encode('utf-8')[:-1])
        self.assertIn('config.toml:unreadable', lane.home_problems(self.cfg))
        self.assertEqual(lane.run_supervisor(self.cfg, run, notion=self.notion), 1)
        self.assertEqual(self.store().read(run)['not_started']['reason'], 'dedicated home')

    def test_a_stdout_cut_in_the_middle_of_a_character(self):
        self.spawned_run()
        run = self.run_supervisor()
        rec = self.store().update(run, lambda r: {**r, 'outcome': None})
        path = Path(rec['stdout_path'])
        path.write_bytes(path.read_bytes() + '途中'.encode('utf-8')[:-1])
        self.assertEqual(lane.outcome(self.cfg, rec)['label'], 'できなかった')   # 形の合わない行がある


class MiniHandoffAdviceTests(Base):
    """観点④：みにだけの依頼の受け渡しの保留で、案内どおりに進めると完了まで進み、みには1回だけ起きる。"""
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol

    def test_following_the_mini_handoff_advice(self):
        from riri.errors import StatusMissing
        from riri.resolve import resolve_held
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        page = cfg.pages['みに'].split('/')[-1]
        notion.blocks[page] = [{'id': 'hdr', 'type': 'heading_1',
                                'heading_1': {'rich_text': [{'text': {'content': '報告一覧（新しい順）'}}]}}]

        def append(method, block_id, children=False, query=None, payload=None):
            blocks = notion.blocks[block_id]
            at = [b['id'] for b in blocks].index(payload['position']['after_block']['id'])
            for n, child in enumerate(payload['children']):
                blocks.insert(at + 1 + n, {**child, 'id': 'appended-%d' % n})
            return {'object': 'list'}
        notion._request = append
        real = notion.set_status
        refused = []

        def set_status(bid, original, status, **k):
            if status == '対応中' and not refused:
                refused.append(1)
                raise StatusMissing('Status line is missing or ambiguous')
            return real(bid, original, status, **k)
        notion.set_status = set_status
        self.run_patrol(cfg, notion, slack, clock)
        run = spawned[0]

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, run, notion=notion), 0)
        clock.advance()
        self.run_patrol(cfg, notion, slack, clock)                           # 受け渡し → 状態を書けずに保留
        state = json.loads((self.root / 'state' / 'state.json').read_text(encoding='utf-8'))
        text = state['outbox']['status-unwritable:💛20990101-1']['text']
        blk = notion.blocks[cfg.requests_page][0]
        blk['code']['rich_text'][0]['text']['content'] = blk['code']['rich_text'][0]['text']['content'].replace(
            '状態：未着手', '状態：対応中', 1)                                 # 案内どおり、手で「対応中」に
        kind, rid, outcome = text.split('--resolve ')[1].split(' で')[0].split()
        self.assertEqual(outcome, 'checked')
        resolve_held(cfg, kind, rid, outcome)
        for _ in range(3):
            clock.advance()
            self.run_patrol(cfg, notion, slack, clock)
        self.assertIn(('status', 'request-1', '完了'), notion.writes)
        self.assertEqual(len(spawned), 1)                                     # みには1回だけ


class _Crash(BaseException):
    """プロセスがその場で止まったことの代わり（except Exception では捕まらない）。"""


class _OneRun(Base):
    def runTest(self):
        pass


class StopAtEverySaveTests(Base):
    """観点⑤：みにの流れの中で、run の記録の保存のどれか1回の直前で止めても、そのあとの巡回で
    黙って止まったままにならず（報告が届くか、保留の知らせが出る）、みには2回起きず、例外も漏れない。"""

    def test_stop_before_every_save(self):
        from datetime import timedelta
        from riri.mini_run import RunStore
        Crash = _Crash
        def flow(t, crash_at):
            calls = [0]
            real = RunStore.update

            def update(store, run_id, change):
                calls[0] += 1
                if calls[0] == crash_at:
                    raise Crash()
                return real(store, run_id, change)
            box = FakeMailbox()
            r = req()
            now = [lane.now()]
            clock = lambda: now[0]
            crashed = []

            def step(fn, supervisor=False):
                try:
                    fn()
                except Crash:
                    crashed.append(calls[0])
                    if supervisor:
                        # the supervisor process died here: in reality its PID is gone (here it is the test's own)
                        for x in t.store().scan()[0]:
                            real(t.store(), x['run_id'], lambda rec: {**rec, **({'supervisor': {**rec['supervisor'], 'pid': 999999}}
                                                                                if isinstance(rec.get('supervisor'), dict) else {}),
                                                                      **({'spawn': {**rec['spawn'], 'pid': 999999}}
                                                                         if isinstance(rec.get('spawn'), dict) else {})})
            t.give(r)                                       # the request is on the page whatever happens to the lane
            with patch.object(RunStore, 'update', update):
                step(lambda: lane.intake(t.patrol, r))
                step(lambda: lane.tick(t.patrol, [r], mailbox=box, popen=t.popen, clock=clock))
                runs = [x for x in t.store().scan()[0] if x.get('state') == 'spawning']
                if runs:
                    step(lambda: t.run_supervisor(), supervisor=True)
                for _ in range(6):
                    now[0] = now[0] + timedelta(minutes=11)        # 見張り役の待ち時間を越える
                    step(lambda: lane.intake(t.patrol, r))
                    step(lambda: lane.tick(t.patrol, [r], mailbox=box, popen=t.popen, clock=clock))
                    runs = [x for x in t.store().scan()[0] if x.get('state') == 'spawning' and not x.get('launch_mark')]
                    if runs:
                        step(lambda: t.run_supervisor(), supervisor=True)
            recs = t.store().scan()[0]
            launched = [x for x in recs if x.get('launch_mark')]
            received = [x for x in recs if x.get('report_state') == 'received']
            notices = [k for k in t.patrol.state['outbox'] if not k.startswith('mini-handoff:')]
            rollouts = list((t.home / '.codex-riri' / 'sessions').rglob('*.jsonl')) if (t.home / '.codex-riri' / 'sessions').exists() else []
            return {'calls': calls[0], 'crashed': crashed, 'launched': len(launched), 'rollouts': len(rollouts),
                    'received': len(received), 'notices': sorted(k.split(':')[0] for k in notices), 'appends': box.appends}


        t = _OneRun(); t.setUp()
        try:
            base = flow(t, crash_at=10 ** 9)
        finally:
            t.allow_broken = True; t.tearDown()
        self.assertEqual((base['launched'], base['received']), (1, 1))
        problems = []
        for k in range(1, base['calls'] + 1):
            t = _OneRun(); t.setUp()
            try:
                out = flow(t, crash_at=k)
            finally:
                t.allow_broken = True
                t.tearDown()
            if out['received'] == 0 and not out['notices']:
                problems.append((k, 'silent', out))
            if out['rollouts'] > 1 or out['launched'] > 1 or out['appends'] > 1:
                problems.append((k, 'twice', out))
        self.assertEqual(problems, [])


class ClockBackTests(Base):
    """新しい観点（時計が戻る）：Mac の時計が後ろへ動くと、保存した時刻が「今」より先になる。
    待ち時間の判定が「経った時間 > 上限」だけだと、時計が追いつくまで（何時間でも）黙って待ってしまう。
    先の時刻は「時計が戻った」として、待たずに次の判断へ進める（安全側：起動の印のない見張り役は
    子を起こしていないので出し直してよい／結果不明の報告は保留して知らせる）。"""

    def finished_run(self, number):
        r = self.take(req(number=number))
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        return self.run_supervisor()

    def test_dead_unmarked_supervisor_with_a_future_spawn_time_is_released(self):
        from datetime import datetime, timedelta, timezone
        r = req()
        self.take(r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        run = self.store().all_runs()[0]['run_id']
        ahead = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()      # 時計が3時間戻った
        self.store().update(run, lambda x: {**x, 'spawn': {**x['spawn'], 'pid': 999999, 'at': ahead}})
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len(self.spawned), 2)                     # 未起動として外れ、もう一度（3時間待たない）
        self.assertEqual(self.store().read(run)['recheck_waits'], 1)

    def test_live_unmarked_supervisor_with_a_future_spawn_time_is_left_alone(self):
        from datetime import datetime, timedelta, timezone
        r = req()
        self.take(r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        run = self.store().all_runs()[0]['run_id']
        ahead = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        self.store().update(run, lambda x: {**x, 'spawn': {**x['spawn'], 'pid': os.getpid(), 'at': ahead}})
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len(self.spawned), 1)                     # 生きている見張り役は時刻にかかわらず待つ
        self.assertIn('spawn', self.store().read(run))

    def test_unknown_report_with_a_future_try_time_is_held_not_left_waiting(self):
        from datetime import datetime, timedelta, timezone
        from riri.errors import RemoteError
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))

        class Box(FakeMailbox):
            appends = 0
            def find(self, record, body):
                return super().find(record, body) if Box.appends == 0 else ('anchor', None)
            def append(self, record, anchor, body):
                Box.appends += 1
                raise RemoteError('timeout', uncertain=True)
        box = Box()
        start = datetime.now(timezone.utc)
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, start)
        self.assertEqual(self.store().read(a)['report_state'], 'unknown')
        back = start - timedelta(hours=3)                          # 次の巡回で時計が3時間戻っている
        lane.deliver(self.cfg, self.store(), self.store().read(a), box, self.patrol, back)
        self.assertEqual(self.store().read(a)['report_state'], 'held')
        self.assertEqual(Box.appends, 1)                           # 書き直さない
        self.assertEqual(len([k for k in self.patrol.state['outbox'] if k.startswith('mini-report-hold:')]), 1)

    def test_failing_pending_report_with_a_future_first_try_is_held(self):
        from datetime import datetime, timedelta, timezone
        from riri.errors import RemoteError
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))

        class Box(FakeMailbox):
            def find(self, record, body):
                raise RemoteError('read refused')
        start = datetime.now(timezone.utc)
        lane.deliver(self.cfg, self.store(), self.store().read(a), Box(), self.patrol, start)
        self.assertEqual(self.store().read(a)['report_state'], 'pending')
        lane.deliver(self.cfg, self.store(), self.store().read(a), Box(), self.patrol, start - timedelta(hours=3))
        self.assertEqual(self.store().read(a)['report_state'], 'held')

    def test_small_backward_step_does_not_hold_or_release(self):
        """数十秒の戻り（時刻合わせ）は「戻った」に数えない。"""
        from datetime import datetime, timedelta, timezone
        r = req()
        self.take(r)
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        run = self.store().all_runs()[0]['run_id']
        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        self.store().update(run, lambda x: {**x, 'spawn': {**x['spawn'], 'pid': 999999, 'at': soon}})
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len(self.spawned), 1)                     # まだ猶予の中：待つ


class RecordTimeTests(Base):
    """観点②（項目の組合せ）の続き：待ち時間を測る時刻は、いつも時差つきの時刻で書いている（iso）。
    形の違う時刻や、結果不明なのに「試した時刻」がない記録は、壊れた記録として断る（壊れた記録の知らせが出る）。
    断らないと、結果不明の報告が時刻なしで黙って待ち続ける。"""

    finished_run = ClockBackTests.finished_run

    def test_wait_times_must_be_aware_times_and_unknown_needs_its_try_time(self):
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))
        good = self.store().read(a)
        self.assertIsNone(lane.lane_record_problem(good))
        for key in lane.LANE_TIMES:
            for odd in ('2026-01-01T00:00:00', 'not-a-time', ''):
                self.assertEqual(lane.lane_record_problem({**good, key: odd}), key, (key, odd))
            self.assertIsNone(lane.lane_record_problem({**good, key: '2026-01-01T00:00:00+00:00'}))
        self.assertEqual(lane.lane_record_problem({**good, 'report_state': 'unknown'}), 'report_try_at')
        self.assertIsNone(lane.lane_record_problem({**good, 'report_state': 'unknown',
                                                    'report_try_at': '2026-01-01T00:00:00+00:00'}))

    def test_unknown_report_without_its_try_time_is_not_left_waiting(self):
        from riri.errors import RemoteError
        from riri.mini_run import RunStore
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))
        RunStore(self.cfg.root).update(a, lambda r: {**r, 'report_state': 'unknown'})   # 手で壊した記録

        class Box(FakeMailbox):
            def find(self, record, body):
                raise RemoteError('read refused')
        for _ in range(2):
            lane.tick(self.patrol, [req()], mailbox=Box(), popen=self.popen)
        notices = [k for k in self.patrol.state['outbox'] if k.startswith(('mini-broken:', 'mini-report-hold:'))]
        self.assertEqual(len(notices), 1, list(self.patrol.state['outbox']))
        self.allow_broken = True


class HoldAdviceTests(Base):
    """観点④の総ざらい（「続けて」の回）：みにの受け口の保留の知らせは、どれも次にすることを書き、
    そのとおりにすると片付く。前は「たかむねが決めてね」だけの知らせが3つあった
    （起こす直前の保留・資料が2回変わった保留・報告の保留）。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol

    def x4_hold(self, recipients):
        cfg, notion, slack, clock, spawned = self.setup_patrol(recipients)
        for _ in range(2 if '、' in recipients else 1):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        run = spawned[0]
        self.store().update(run, lambda r: {**{k: v for k, v in r.items() if k not in ('spawn', 'not_started', 'supervisor')},
                                            'state': 'reserved', 'x4_mismatch': 2, 'x4_changed': ['case_page']})
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        from riri.state import StateStore
        state = StateStore(self.root, 'live').load()
        texts = [v['text'] for k, v in state['outbox'].items() if k.startswith('mini-x4:')]
        self.assertEqual(len(texts), 1)
        return cfg, notion, slack, clock, spawned, run, texts[0]

    def test_x4_notice_says_repost_and_following_it_runs_the_new_request(self):
        from tests.test_patrol_flow import request
        cfg, notion, slack, clock, spawned, run, text = self.x4_hold('@みに')
        self.assertIn('新しい依頼IDで出し直して', text)
        self.assertIn('元の依頼はそのままで大丈夫', text)
        self.assertNotIn('「完了」', text)                       # みにだけの依頼：手で完了にすることはない
        notion.blocks[cfg.requests_page].insert(0, request(number=2, recipients='@みに'))   # 案内どおり（元は残す）
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        runs = {r['request_id']: r['state'] for r in self.store().all_runs()}
        self.assertEqual(runs, {'💛20990101-1': 'held', '💛20990101-2': 'spawning'})   # 同じ中身でも止まらない
        self.assertEqual(len(spawned), 2)
        self.allow_broken = True

    def test_x4_notice_for_a_shared_request_says_repost_only_mini_and_finish_by_hand(self):
        import re
        from riri.state import StateStore
        from tests.test_patrol_flow import report
        cfg, notion, slack, clock, spawned, run, text = self.x4_hold('@うさ、@みに')
        self.assertIn('みにの分だけ', text)
        self.assertIn('「完了」', text)
        notion.blocks[cfg.pages['うさ'].split('/')[-1]].insert(0, report(name='うさ'))
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        blk = [b for b in notion.blocks[cfg.requests_page] if b['id'] == 'request-1'][0]
        blk['code']['rich_text'][0]['text']['content'] = re.sub(
            r'状態：\S+', '状態：完了', blk['code']['rich_text'][0]['text']['content'], count=1)   # 案内どおり手で完了
        before = set(StateStore(self.root, 'live').load()['outbox'])
        for _ in range(3):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        state = StateStore(self.root, 'live').load()
        self.assertEqual(set(state['outbox']) - before, set())      # 黙って終わる（エラー・知らせなし）
        self.assertEqual(self.store().read(run)['state'], 'held')   # みにの分は起こさないまま
        self.assertEqual(len(spawned), 1)
        self.allow_broken = True

    def test_gate_hold_notices_name_the_next_step(self):
        r = self.take(req())
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        run = self.store().all_runs()[0]['run_id']
        texts = {}
        for reason in ('同じ中身の依頼がほかにある', '安全チェックに通らない（本文は転記しない）'):
            self.patrol.state['outbox'].clear()
            self.store().update(run, lambda x: {**{k: v for k, v in x.items() if k not in ('notices', 'held_reason', 'held_at')},
                                                'state': 'reserved'})
            from datetime import datetime, timezone
            lane.gate_failed(self.patrol, self.store(), self.store().read(run), 'hold', reason, datetime.now(timezone.utc))
            texts[reason] = [v['text'] for k, v in self.patrol.state['outbox'].items() if k.startswith('mini-held:')][0]
        self.assertIn('先の同じ依頼', texts['同じ中身の依頼がほかにある'])
        self.assertNotIn('出し直して', texts['同じ中身の依頼がほかにある'])
        self.assertIn('新しい依頼IDで出し直して', texts['安全チェックに通らない（本文は転記しない）'])

    def test_report_hold_notice_says_a_report_on_the_page_finishes_it_and_it_does(self):
        from functools import partial
        from riri.errors import RemoteError
        from riri.state import StateStore
        from tests.test_patrol_flow import report
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        page = cfg.pages['みに'].split('/')[-1]
        notion.blocks[page] = [{'id': 'hdr', 'type': 'heading_1',
                                'heading_1': {'rich_text': [{'text': {'content': '報告一覧（新しい順）'}}]}}]

        def append(method, block_id, children=False, query=None, payload=None):
            raise RemoteError('timeout', uncertain=True)          # 書けたか分からない（実は書けていない）
        notion._request = append
        real_tick = lane.tick
        with patch.object(lane, 'tick', lambda patrol, requests, **k: real_tick(patrol, requests, clock=patrol.clock, **k)):
            self.run_patrol(cfg, notion, slack, clock)

            def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
                return [sys.executable, str(self.codex), '-o', str(last_json)]
            with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                    patch.object(lane, 'child_base_env', lambda c, folder: {
                        'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                        'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
                lane.run_supervisor(cfg, spawned[0], notion=notion)
            for _ in range(9):                                       # 45分
                clock.advance(); self.run_patrol(cfg, notion, slack, clock)
            state = StateStore(self.root, 'live').load()
            texts = [v['text'] for k, v in state['outbox'].items() if k.startswith('mini-report-hold:')]
            self.assertEqual(len(texts), 1)
            self.assertIn('報告が載っていれば', texts[0])
            self.assertIn('自動では書き直さない', texts[0])
            self.assertEqual(self.store().all_runs()[0]['report_state'], 'held')
            # 案内どおり：たかむねが、みにの完了報告ページに報告を手で書く
            notion.blocks[page].insert(1, report(name='みに', id='by-hand'))
            for _ in range(3):
                clock.advance(); self.run_patrol(cfg, notion, slack, clock)
        self.assertIn(('status', 'request-1', '完了'), notion.writes)
        state = StateStore(self.root, 'live').load()
        self.assertEqual(state['requests']['💛20990101-1']['status'], 'done')
        self.assertEqual([k for k in state['outbox'] if k.startswith('error')], [])
        self.allow_broken = True

    def test_lane_back_on_resumes_the_shared_part_it_told_about(self):
        """受け口が止まっている知らせ（mini-off）のあと、受け口を戻すと、待っていたみにの分は次の巡回で進む。"""
        from dataclasses import replace
        from tests.test_mini_lane import FakePatrol
        r1 = req(number=1, recipients='うさ、みに')
        self.take(r1)
        self.patrol.state['requests'][r1.id] = {'status': 'posted', 'block_id': r1.block_id}
        off = FakePatrol(replace(self.cfg, mini_transport='off'), self.notion)
        off.state = self.patrol.state
        lane.tick(off, [r1], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len([k for k in off.state['outbox'] if k.startswith('mini-off:')]), 1)
        self.assertEqual(self.spawned, [])
        lane.tick(self.patrol, [r1], mailbox=FakeMailbox(), popen=self.popen)       # 受け口を戻した
        self.assertEqual(len(self.spawned), 1)


class LaneTroubleTests(Base):
    """新しい観点（書けない・続けて止まる、「どんどん詰めて」の回）：
    ①依頼の前から state/mini が書けない：みにだけの依頼は「書き直されたら」ではなく、フォルダを直せば
    次の巡回で受け付ける（依頼は書き直さない）。ほかの子と一緒の依頼は、みにの分の片付け方を書く。
    ②予約のあとで run の記録が書けなくなる：巡回のたびに途中で止まっても、前はログに1行だけで
    誰にも知らせなかった。3回続いたら1回知らせ、直れば続きから動く。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol

    def lane_clock(self):
        real_tick = lane.tick
        h = patch.object(lane, 'tick', lambda *a, **k: real_tick(*a, **{**k, 'clock': a[0].clock}))
        h.start()
        self.addCleanup(h.stop)

    def unwritable(self, path):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o500)
        self.locked = getattr(self, 'locked', []) + [path]

    def tearDown(self):
        for path in getattr(self, 'locked', []):          # 仮のフォルダを消す前に権限を戻す
            if path.exists():
                os.chmod(path, 0o700)
        super().tearDown()

    def state(self):
        from riri.state import StateStore
        return StateStore(self.root, 'live').load()

    def texts(self, prefix):
        return [v['text'] for k, v in self.state()['outbox'].items() if k.startswith(prefix)]

    def test_mini_only_with_an_unwritable_folder_says_fix_the_folder_and_goes_on_after(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        self.lane_clock()
        mini = self.root / 'state' / 'mini'
        self.unwritable(mini)
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        texts = self.texts('invalid:')
        self.assertEqual(len(texts), 1)
        self.assertIn('state/mini', texts[0])
        self.assertIn('書き直さなくていい', texts[0])
        self.assertNotIn('書き直されたら', texts[0])
        os.chmod(mini, 0o700)                                    # 案内どおり：フォルダを直す（依頼はそのまま）
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(len(self.texts('invalid:')), 1)        # 知らせは増えない

    def test_shared_with_an_unwritable_folder_says_how_to_finish_mini_part(self):
        from tests.test_patrol_flow import request
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        self.lane_clock()
        mini = self.root / 'state' / 'mini'
        self.unwritable(mini)
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        texts = self.texts('mini-part-held:')
        self.assertEqual(len(texts), 1)
        self.assertIn('state/mini', texts[0])
        self.assertIn('みにの分だけ', texts[0])
        self.assertIn('「完了」', texts[0])
        os.chmod(mini, 0o700)                                    # 案内どおり：直してから、みにの分だけ出し直す
        notion.blocks[cfg.requests_page].insert(0, request(number=2, recipients='@みに'))
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual([r['request_id'] for r in self.store().all_runs()], ['💛20990101-2'])
        self.assertEqual(len(spawned), 1)

    def test_each_intake_hold_reason_gets_advice_that_fits_it(self):
        from riri.mini_lane import intake_advice
        default = ''
        self.assertEqual(intake_advice('unsafe', shared=False), default)          # 書き直せば次で確かめる（今の文面）
        self.assertIn('みにの分だけ', intake_advice('unsafe', shared=True))
        for kind in ('continuation',):
            for shared in (False, True):
                text = intake_advice(kind, shared=shared)
                self.assertIn('手で渡して', text)
                self.assertNotIn('出し直して', text)                              # 出し直しても続きはまた保留
        for shared in (False, True):
            text = intake_advice('changed_after_launch', shared=shared)
            self.assertIn('新しい依頼IDで出し直して', text)
            self.assertNotIn('「完了」', text)
            store = intake_advice('store', shared=shared)
            self.assertIn('state/mini', store)
        self.assertIn('書き直さなくていい', intake_advice('store', shared=False))

    def test_lane_failing_every_patrol_is_told_once_and_goes_on_after_the_fix(self):
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        self.lane_clock()
        h = patch.object(lane, 'spawn_supervisor', lambda c, run, popen=None: spawned.append(run) or 999999)
        h.start(); self.addCleanup(h.stop)
        self.run_patrol(cfg, notion, slack, clock); clock.advance()          # 予約・見張り役（すぐ死ぬ）
        runs = self.root / 'state' / 'mini' / 'runs'
        self.unwritable(runs)                                                 # 予約のあとで書けなくなる
        for n in range(6):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
            if n == 1:
                self.assertEqual(self.texts('mini-lane-failing:'), [])       # 2回目まではまだ
        texts = self.texts('mini-lane-failing:')
        self.assertEqual(len(texts), 1)                                       # 続いても1回だけ
        self.assertIn('PermissionError', texts[0])
        self.assertIn('state/mini', texts[0])
        os.chmod(runs, 0o700)                                                 # 案内どおり：直す
        before = len(spawned)
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertGreater(len(spawned), before)                              # 続きから動く
        self.assertEqual(self.state()['mini_lane_failures']['count'], 0)
        os.chmod(runs, 0o500)                                                 # また壊れたら、また1回知らせる
        for _ in range(6):                                                    # 猶予の間は読むだけで失敗しない
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual(len(self.texts('mini-lane-failing:')), 2)
        self.allow_broken = True

    def test_time_limit_and_broken_records_are_not_counted_as_a_failing_lane(self):
        from riri.errors import TimeBudgetExceeded
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        with patch.object(lane, 'tick', side_effect=TimeBudgetExceeded('deadline')):
            for _ in range(4):
                self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual(self.texts('mini-lane-failing:'), [])                # 時間切れは受け口の中で数える
        (self.root / 'state' / 'mini' / 'runs').mkdir(parents=True, exist_ok=True)
        (self.root / 'state' / 'mini' / 'runs' / 'mini-0000000000000000000a.json').write_text('{', encoding='utf-8')
        for _ in range(4):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual(self.texts('mini-lane-failing:'), [])                # 読めない記録は mini-broken が知らせる
        self.assertEqual(len(self.texts('mini-broken:')), 1)
        self.allow_broken = True

    def test_shared_continuation_says_hand_it_over_and_mini_report_finishes_it(self):
        import re
        from tests.test_patrol_flow import block, report
        cfg, notion, slack, clock, spawned = self.setup_patrol('@うさ、@みに')
        blk = notion.blocks[cfg.requests_page][0]
        text = blk['code']['rich_text'][0]['text']['content']
        blk['code']['rich_text'][0]['text']['content'] = text.replace('やること：', '元の依頼ID：💛20990101-0\nやること：', 1)
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        texts = self.texts('mini-part-held:')
        self.assertEqual(len(texts), 1)
        self.assertIn('手で渡して', texts[0])
        self.assertIn('みにの完了報告が出れば', texts[0])
        self.assertEqual(spawned, [])
        # 案内どおり：みにに手で渡し、みにが自分の完了報告ページに書く。うさの報告もそろう
        notion.blocks[cfg.pages['うさ'].split('/')[-1]].insert(0, report(name='うさ'))
        notion.blocks[cfg.pages['みに'].split('/')[-1]].insert(0, report(name='みに', id='mini-by-hand'))
        for _ in range(3):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertIn(('status', 'request-1', '完了'), notion.writes)


class SwitchedOffMidwayTests(Base):
    """新しい観点（実行中に設定を変える、「どんどん詰めて」の回）：みにを起こしたあと、【受け渡し】の前に
    受け口をオフにする（または MINI_* の誤りで止まる）と、前は受付が今までの道に回して「みにへの依頼が
    あるので手で渡して」と知らせていた（みにはもう作業している → 手で渡すと二重）。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol

    def launched_then(self, change):
        from dataclasses import replace
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        page = cfg.pages['みに'].split('/')[-1]
        notion.blocks[page] = [{'id': 'hdr', 'type': 'heading_1',
                                'heading_1': {'rich_text': [{'text': {'content': '報告一覧（新しい順）'}}]}}]

        def append(method, block_id, children=False, query=None, payload=None):
            blocks = notion.blocks[block_id]
            at = [b['id'] for b in blocks].index(payload['position']['after_block']['id'])
            for n, child in enumerate(payload['children']):
                blocks.insert(at + 1 + n, {**child, 'id': 'appended-%d' % n})
            return {'object': 'list'}
        notion._request = append
        self.run_patrol(cfg, notion, slack, clock); clock.advance()

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, spawned[0], notion=notion), 0)
        changed = replace(cfg, **change)
        for _ in range(3):
            self.run_patrol(changed, notion, slack, clock); clock.advance()
        return notion, slack

    def check(self, notion, slack):
        texts = [p[1] for p in slack.posts]
        self.assertFalse([t for t in texts if 'みにへの依頼があるので' in t], texts)   # 手で渡す案内を出さない
        self.assertEqual(sum('【受け渡し】' in t for t in texts), 1)
        self.assertIn(('status', 'request-1', '完了'), notion.writes)                   # 受け口の分として最後まで

    def test_lane_switched_off_after_launch_finishes_as_the_lane(self):
        self.check(*self.launched_then({'mini_transport': 'off'}))

    def test_wrong_mini_setting_after_launch_finishes_as_the_lane(self):
        self.check(*self.launched_then({'mini_config_errors': ('MINI_STAGE',)}))

    def test_stage_zero_after_launch_finishes_as_the_lane(self):
        self.check(*self.launched_then({'mini_stage': 0}))

    def test_reserved_but_not_started_goes_the_old_way_while_off(self):
        """起こしていない予約だけなら、オフの間は今までの道（手で渡す案内）のまま。"""
        from dataclasses import replace
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        h = patch.object(lane, 'spawn_supervisor', lambda c, run, popen=None: spawned.append(run) or 999999)
        h.start(); self.addCleanup(h.stop)
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.run_patrol(replace(cfg, mini_transport='off'), notion, slack, clock)
        self.assertTrue(any('みにへの依頼があるので' in p[1] for p in slack.posts))
        self.allow_broken = True

    def test_broken_record_while_off_holds_instead_of_the_old_way(self):
        """オフで読めない記録があると、この依頼をもう起こしたか分からない：手で渡す案内は出さずに保留して知らせる。"""
        from dataclasses import replace
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        runs = self.root / 'state' / 'mini' / 'runs'
        runs.mkdir(parents=True, exist_ok=True)
        (runs / 'mini-0000000000000000000a.json').write_text('{', encoding='utf-8')
        self.run_patrol(replace(cfg, mini_transport='off'), notion, slack, clock)
        texts = [p[1] for p in slack.posts]
        self.assertFalse([t for t in texts if 'みにへの依頼があるので' in t], texts)
        self.assertTrue([t for t in texts if 'もう起こしたか分からない' in t and 'state/mini' in t], texts)
        self.allow_broken = True

    def test_without_any_record_while_off_goes_the_old_way(self):
        from dataclasses import replace
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        self.run_patrol(replace(cfg, mini_transport='off'), notion, slack, clock)
        self.assertTrue(any('みにへの依頼があるので' in p[1] for p in slack.posts))


class RequestGoneTests(SwitchedOffMidwayTests.__mro__[1]):
    """新しい観点（起こしたあとで依頼を消す・IDを変える、「どんどん詰めて」の回）：みにだけの依頼は
    【受け渡し】のときに巡回の記録に入る。その前に依頼のブロックが消える（取り下げ）か依頼IDが変わると、
    前は黙って記録を飛ばし、みにの報告は依頼した子に届かないまま残った（--status にだけ出る）。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    launched_then = SwitchedOffMidwayTests.launched_then

    def gone(self, change):
        orig = self.run_patrol

        def first_then(cfg, notion, slack, clock):
            if not getattr(self, 'changed', False) and lane.store_of(cfg).scan()[0] and \
                    lane.store_of(cfg).scan()[0][0].get('launch_mark'):
                change(notion.blocks[cfg.requests_page])
                self.changed = True
            return orig(cfg, notion, slack, clock)
        self.run_patrol = first_then
        notion, slack = self.launched_then({})
        self.notion_after = notion
        return [p[1] for p in slack.posts if '【受け渡し】' in p[1]]

    def test_request_deleted_before_the_handoff_is_said_in_the_handoff(self):
        texts = self.gone(lambda blocks: blocks.__setitem__(slice(None), [b for b in blocks if b['id'] != 'request-1']))
        self.assertEqual(len(texts), 1)
        self.assertIn('見つからない', texts[0])
        self.assertIn('みにの完了報告ページ', texts[0])

    def test_request_id_changed_before_the_handoff_is_said_in_the_handoff(self):
        def change(blocks):
            b = [b for b in blocks if b['id'] == 'request-1'][0]
            b['code']['rich_text'][0]['text']['content'] = b['code']['rich_text'][0]['text']['content'].replace(
                '💛20990101-1', '💛20990101-7', 1)
        texts = self.gone(change)
        self.assertEqual(len(texts), 1)
        self.assertIn('見つからない', texts[0])

    def test_ordinary_handoff_text_is_unchanged(self):
        from tests.test_mini_rereview20 import SwitchedOffMidwayTests as S
        notion, slack = self.launched_then({})
        texts = [p[1] for p in slack.posts if '【受け渡し】' in p[1]]
        self.assertEqual(len(texts), 1)
        self.assertNotIn('見つからない', texts[0])
        self.assertIn(('status', 'request-1', '完了'), notion.writes)

    def test_status_changed_by_hand_before_the_handoff_is_said_and_not_completed(self):
        def change(blocks):
            b = [b for b in blocks if b['id'] == 'request-1'][0]
            b['code']['rich_text'][0]['text']['content'] = b['code']['rich_text'][0]['text']['content'].replace(
                '状態：未着手', '状態：対応中', 1)
        texts = self.gone(change)
        self.assertEqual(len(texts), 1)
        self.assertIn('自動では完了にしない', texts[0])
        self.assertNotIn(('status', 'request-1', '完了'), self.notion_after.writes)   # 書いたとおり


class LineSeparatorTests(Base):
    """新しい観点（みにの答えの中の改行の仲間、「どんどん詰めて」の回）：Python の splitlines() は
    \\u2028・\\u2029・\\x85 なども改行として切る。Codex の JSONL・会話の記録・ps の出力の1行の中に
    それらがあると、1行が2つに切れて読めなくなっていた（正常に終わった run が「できなかった」・
    最終の裏付けなし・仲間のプロセスの見落とし）。代筆の1行の欄に入ると、偽の「結果：」行ができていた。"""

    SEPS = (' ', ' ', '\x85', '\r')

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol

    def test_formal_result_and_final_signal_read_json_lines_with_separators_inside(self):
        for sep in ('\u2028', '\u2029', '\x85'):          # JSON が文字のまま持てるもの（\r は \\r に逃がされる）
            answer = json.dumps({'run_id': 'mini-00000000000000000001', 'final': True, 'result': '完了',
                                 'summary': 'まとめ' + sep + '続き', 'artifacts': [], 'unverified': 'なし',
                                 'blockers': 'なし', 'next': 'なし'}, ensure_ascii=False)
            stdout = '\n'.join(json.dumps(e, ensure_ascii=False) for e in [
                {'type': 'thread.started', 'thread_id': 'th-1'}, {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {'id': 'i0', 'type': 'agent_message', 'text': answer}},
                {'type': 'turn.completed', 'usage': {}}]) + '\n'
            self.assertIn(sep, stdout)                      # 文字のまま（逃がさない）で入っている
            got = mini_supervisor.formal_result(stdout, answer, 'mini-00000000000000000001', lane._validate, 0)
            self.assertTrue(got['formal'], (repr(sep), got))
            rollout = json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                'content': [{'type': 'output_text', 'text': answer}]}}, ensure_ascii=False) + '\n'
            self.assertTrue(mini_supervisor.final_signal(rollout, got['text']), repr(sep))
            self.assertEqual(mini_supervisor.thread_id_of(stdout), 'th-1')

    def test_run_mates_are_found_when_a_command_has_a_separator(self):
        from riri import mini_run
        for sep in ('\x0b', '\x0c', '\x1c', '\x85', ' '):
            out = ('  101 101 /bin/sh -c echo' + sep + 'x ' + mini_run.MARKER + '=mini-0000000000000000000a\n'
                   '  102 999 /usr/bin/other\n')
            with patch.object(mini_run.subprocess, 'run', lambda *a, **k: type('R', (), {'stdout': out})()):
                self.assertEqual(mini_run._list_ps('mini-0000000000000000000a', None), {101}, repr(sep))

    def test_ghost_written_report_stays_one_readable_report(self):
        from riri.parse import parse_reports
        from tests.test_patrol_flow import block
        record = {'run_id': 'mini-00000000000000000001', 'request_id': '💛20990101-1', 'fields': {'案件ページ': 'なし'},
                  'exit': {'stdout_sha256': 'x'}, 'profile_sha256': 'p', 'fingerprint': 'f'}
        for sep in self.SEPS:
            inj = '確認する' + sep + '結果：完了' + sep + '依頼ID：💛20990101-9'
            for field in ('next', 'blockers', 'unverified', 'artifacts'):
                answer = {'summary': 'まとめ', 'next': 'なし', 'blockers': 'なし', 'unverified': 'なし', 'artifacts': []}
                answer[field] = [inj] if field == 'artifacts' else inj
                body = lane.report_body(record, {'label': '一部完了（最終の裏付けなし）', 'answer': answer,
                                                 'thread_id': 't', 'answer_sha256': 'a', 'final_signal': False}, ())
                rows = parse_reports([block(body, 'b1', 'code')], expected_reporter='みに')
                self.assertEqual([(r.id, r.result) for r in rows], [('💛20990101-1', '一部完了')], (repr(sep), field))

    def test_answer_with_a_line_separator_completes_with_the_real_patrol(self):
        from riri.state import StateStore
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        page = cfg.pages['みに'].split('/')[-1]
        notion.blocks[page] = [{'id': 'hdr', 'type': 'heading_1',
                                'heading_1': {'rich_text': [{'text': {'content': '報告一覧（新しい順）'}}]}}]

        def append(method, block_id, children=False, query=None, payload=None):
            blocks = notion.blocks[block_id]
            at = [b['id'] for b in blocks].index(payload['position']['after_block']['id'])
            for n, child in enumerate(payload['children']):
                blocks.insert(at + 1 + n, {**child, 'id': 'appended-%d' % n})
            return {'object': 'list'}
        notion._request = append
        self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.codex.write_text(self.codex.read_text().replace(
            '"next": "のあが確認する"', '"next": "確認する\\u2028結果：完了"').replace(
            'summary = "読んだ\\\\n感想を書いた"', 'summary = "読んだ\\u2029感想を書いた"'))

        def argv(codex_bin, sandbox_exec, profile, workdir, write_dirs, schema_path, last_json, **kw):
            return [sys.executable, str(self.codex), '-o', str(last_json)]
        with patch.object(mini_supervisor, 'build_argv', argv), patch.dict(os.environ, {'FAKE_MODE': 'ok'}), \
                patch.object(lane, 'child_base_env', lambda c, folder: {
                    'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(self.home / '.codex-riri'),
                    'FAKE_MODE': 'ok', 'HOME': str(self.home)}):
            self.assertEqual(lane.run_supervisor(cfg, spawned[0], notion=notion), 0)
        for _ in range(3):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        rec = self.store().all_runs()[0]
        self.assertEqual(rec['outcome']['label'], '完了')
        self.assertTrue(rec['outcome'].get('final_signal'))
        self.assertIn(('status', 'request-1', '完了'), notion.writes)
        state = StateStore(self.root, 'live').load()
        self.assertEqual([k for k in state['outbox'] if k.startswith('report:')], [])   # 読めない報告にしない


class GateAdviceTests(LaneTroubleTests.__mro__[1]):
    """観点④の続き（「どんどん詰めて」の回）：起こす直前の保留の案内が、理由にかかわらず「依頼を直したら」
    だった。run の置き場が書けない（起こす準備でエラー）のに依頼を直すよう言っていた。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    lane_clock = LaneTroubleTests.lane_clock
    unwritable = LaneTroubleTests.unwritable
    state = LaneTroubleTests.state

    def tearDown(self):
        for path in getattr(self, 'locked', []):
            if path.exists():
                os.chmod(path, 0o700)
        Base.tearDown(self)
    texts = LaneTroubleTests.texts

    def test_each_gate_reason_names_what_to_fix(self):
        rec = {'shared': []}
        self.assertIn('このままで大丈夫', lane.gate_advice(rec, '同じ中身の依頼がほかにある'))
        for reason in ('起こす準備でエラー（PermissionError）', '起こす直前の確認でエラー（KeyError）'):
            text = lane.gate_advice(rec, reason)
            self.assertIn('state/mini', text)
            self.assertIn('run の置き場', text)
            self.assertNotIn('依頼を直したら', text)
        self.assertIn('logs', lane.gate_advice(rec, 'みにを起こそうとして、起動の印まで届かないことが3回続いた（最後の理由：x）'))
        self.assertIn('資料のページ', lane.gate_advice(rec, '資料に秘密の値が入っている（本文は転記しない）'))
        self.assertIn('依頼を直したら', lane.gate_advice(rec, '安全チェックに通らない（本文は転記しない）'))
        self.assertIn('みにの分だけ', lane.gate_advice({'shared': ['うさ']}, '起こす準備でエラー（OSError）'))

    def test_unwritable_run_folder_says_fix_it_and_a_repost_then_runs(self):
        from tests.test_patrol_flow import request
        cfg, notion, slack, clock, spawned = self.setup_patrol('@みに')
        self.lane_clock()
        folder = lane.run_base(cfg)
        self.unwritable(folder)
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        texts = self.texts('mini-held:')
        self.assertEqual(len(texts), 1)
        self.assertIn('run の置き場', texts[0])
        self.assertNotIn('依頼を直したら', texts[0])
        os.chmod(folder, 0o700)                                               # 案内どおり：直して、出し直す
        notion.blocks[cfg.requests_page].insert(0, request(number=2, recipients='@みに'))
        for _ in range(2):
            self.run_patrol(cfg, notion, slack, clock); clock.advance()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(sorted(r['state'] for r in self.store().all_runs()), ['held', 'spawning'])


class Hung(Exception):
    """試験の打ち切り（OSError の仲間ではないので、読み手の except OSError に拾われない）。"""


class ChildFileTests(ClockBackTests.__mro__[1]):
    """新しい観点（子が書ける場所のファイル、「どんどん詰めて」の回）：会話の記録（専用ホームの sessions）と
    out/last.json は子が書ける。会話の記録の名前で FIFO を置かれると、結果を決めるところの読み込みで
    止まり、巡回の時間切れまで待つことを毎回くり返した。リンクはたどり、大きさの上限もなかった。
    last.json がリンクなら「ない」扱いになり、5-1 の5番目の照合を飛ばせた。"""

    finished_run = ClockBackTests.finished_run

    def settled(self):
        a = self.finished_run(1)
        lane.settle(self.cfg, self.store(), self.store().read(a))
        rec = self.store().read(a)
        from riri.mini_supervisor import find_rollout
        return rec, find_rollout(lane.codex_home(self.cfg), rec['outcome']['thread_id'])

    def outcome_within(self, rec, seconds=3):
        import signal

        def alarm(*a):
            raise Hung()
        old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(seconds)
        try:
            return lane._outcome(self.cfg, rec, rec['end_evidence'])
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_fifo_in_place_of_the_rollout_does_not_hang(self):
        rec, rollout = self.settled()
        self.assertEqual(rec['outcome']['label'], '完了')
        os.unlink(rollout)
        os.mkfifo(rollout)
        out = self.outcome_within(rec)                          # 止まらずに戻る（Hung にならない）
        self.assertFalse(out['final_signal'])
        self.assertEqual(out['label'], '一部完了（最終の裏付けなし）')

    def test_link_in_place_of_the_rollout_is_not_followed(self):
        rec, rollout = self.settled()
        target = Path(rollout).with_name('elsewhere.txt')
        os.replace(rollout, target)
        os.symlink(target, rollout)
        self.assertFalse(self.outcome_within(rec)['final_signal'])

    def test_last_json_that_is_not_a_small_plain_file_fails_the_fifth_check(self):
        rec, _ = self.settled()
        last = Path(rec['run_dir']) / 'out' / 'last.json'
        saved = last.read_bytes()
        cases = {'link': lambda: (last.unlink(), last.with_name('real.json').write_bytes(saved),
                                  os.symlink(last.with_name('real.json'), last)),
                 'fifo': lambda: (last.unlink(), os.mkfifo(last)),
                 'huge': lambda: last.write_bytes(saved + b' ' * (lane.LAST_JSON_LIMIT + 1))}
        for name, make in cases.items():
            for p in (last, last.with_name('real.json')):
                if os.path.lexists(p):
                    os.unlink(p)
            last.write_bytes(saved)
            make()
            out = self.outcome_within(rec)
            self.assertEqual(out['label'], 'できなかった', name)       # 「ない」扱いで照合を飛ばさない
            self.assertIn('last.json', out['reason'], name)

    def test_read_plain_does_not_wait_on_a_fifo_and_has_a_limit(self):
        d = Path(self.tmp.name) / 'plain'
        d.mkdir()
        os.mkfifo(d / 'f')
        import signal

        def alarm(*a):
            raise Hung()
        old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(3)
        try:
            self.assertEqual(lane._read_plain(d / 'f'), (None, 'not a plain file'))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        (d / 'big').write_bytes(b'x' * 11)
        self.assertEqual(lane._read_plain(d / 'big', limit=10), (None, 'too large'))
        self.assertEqual(lane._read_plain(d / 'big', limit=11), (b'x' * 11, None))


class MovedBlockTests(Base):
    """新しい観点（依頼を別のブロックへ移す、「どんどん詰めて」の回）：処理済みかどうかを（依頼ID・ブロックID）で
    決めていたので、みにを起こしたあと【受け渡し】の前に、人が同じ依頼を新しいブロックへ移すと、
    同じ依頼IDで2回目を起こしていた。"""

    def test_same_id_in_another_block_after_a_launch_is_held_not_launched_again(self):
        from tests.test_mini_lane import block
        r = self.take(req())
        lane.tick(self.patrol, [r], mailbox=FakeMailbox(), popen=self.popen)
        self.run_supervisor()
        moved = parse_requests([block(r.text, 'request-1-moved')])[0]
        self.give(moved)
        out = lane.intake(self.patrol, moved)
        self.assertTrue(out.taken)
        self.assertIn('別のブロック', out.held)
        self.assertIn('新しい依頼IDで出し直して', out.advice)
        self.assertEqual([x['block_id'] for x in self.store().all_runs()], ['request-1'])   # 予約しない
        lane.tick(self.patrol, [moved], mailbox=FakeMailbox(), popen=self.popen)
        self.assertEqual(len(self.spawned), 1)

    def test_same_id_in_another_block_before_any_launch_is_taken_as_usual(self):
        from tests.test_mini_lane import block
        r = self.take(req())
        moved = parse_requests([block(r.text, 'request-1-moved')])[0]
        self.give(moved)
        out = lane.intake(self.patrol, moved)
        self.assertEqual((out.taken, out.held), (True, ''))            # 起こす前なら、ふつうに受け付ける


class HandoffTextTests(SwitchedOffMidwayTests.__mro__[1]):
    """新しい観点（知らせに入る人の書いた文字、「続けて」の回・2）：【受け渡し】の知らせに「依頼した子」の
    名前を逃がさずに入れていたので、`<https://…|うさ>` と書くと、りりの知らせに偽のリンクが出た
    （ほかの宛先への【りりからのお願い】は逃がしている）。"""

    setup_patrol = test_mini_lane.PatrolHookTests.setup_patrol
    run_patrol = test_mini_lane.PatrolHookTests.run_patrol
    launched_then = SwitchedOffMidwayTests.launched_then

    def test_requester_markup_is_shown_as_text_in_the_handoff(self):
        orig = self.setup_patrol

        def setup(recipients):
            out = orig(recipients)
            b = out[1].blocks[out[0].requests_page][0]
            b['code']['rich_text'][0]['text']['content'] = b['code']['rich_text'][0]['text']['content'].replace(
                '依頼した子：うさ', '依頼した子：<https://example.com/x|うさ> & co', 1)
            return out
        self.setup_patrol = setup
        notion, slack = self.launched_then({})
        texts = [p[1] for p in slack.posts if '【受け渡し】' in p[1]]
        self.assertEqual(len(texts), 1)
        self.assertNotIn('<https://', texts[0])
        self.assertIn('&lt;https://example.com/x|うさ&gt; &amp; co', texts[0])
