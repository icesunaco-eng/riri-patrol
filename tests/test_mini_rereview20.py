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
