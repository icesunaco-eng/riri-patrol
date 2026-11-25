"""外側サンドボックスの設定作り（riri/mini_profile.py）。設計 v1.0 の 4-5、完成基準 3/4/14/16。

境界の試験は macOS の sandbox-exec が使えるときだけ動く（偽のホームを一時フォルダーに作る）。
期待値は表から作らず、下の CASES に手で書いてある（完成基準14）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from riri.mini_profile import build_profile, fingerprint, profile_hash

HAVE_SANDBOX = sys.platform == 'darwin' and shutil.which('sandbox-exec') is not None


def layout(base):
    base = Path(os.path.realpath(base))
    home = base / 'home'
    paths = {
        'home': home,
        'dedicated': home / '.codex-riri',
        'run': home / '.riri-mini-runs' / 'run1',
        'tmp': base / 'tmp',
        'case': home / 'みに' / '案件' / 'テスト',
        'other_case': home / 'みに' / '案件' / '別',
        'outside': base / 'outside',           # ホームの外（表にない兄弟のフォルダー）
    }
    for p in [paths['dedicated'] / 'sessions', paths['dedicated'] / 'plugins', paths['dedicated'] / 'cache',
              paths['dedicated'] / 'skills', paths['run'] / 'work', paths['run'] / 'out',
              paths['run'] / 'input', paths['tmp'], paths['case'], paths['other_case'],
              home / '.codex', home / '.ssh', home / 'riri-patrol' / 'state', home / 'ヒロインズ㊙️',
              paths['outside']]:
        p.mkdir(parents=True, exist_ok=True)
    for f in [home / '.codex' / 'auth.json', home / '.ssh' / 'id', home / 'riri-patrol' / '.env',
              home / 'riri-patrol' / 'state' / 'state.json', home / 'ヒロインズ㊙️' / 'x',
              paths['dedicated'] / 'config.toml', paths['dedicated'] / 'AGENTS.md',
              paths['dedicated'] / 'auth.json', paths['run'] / 'input' / 'req.txt',
              home / 'みに' / 'AGENTS.md', paths['outside'] / 'x.txt']:
        f.write_text('dummy', encoding='utf-8')
    return paths


def layout_outside(base):
    """run と専用ホームを、設定でホームの外に置いたときの配置（M3）。"""
    p = layout(base)
    base = Path(os.path.realpath(base))
    p['dedicated'] = base / 'elsewhere' / 'codex-riri'
    p['run'] = base / 'elsewhere' / 'runs' / 'run1'
    for d in [p['dedicated'] / 'sessions', p['run'] / 'work', p['run'] / 'out', p['run'] / 'input']:
        d.mkdir(parents=True, exist_ok=True)
    for f in [p['dedicated'] / 'config.toml', p['dedicated'] / 'auth.json', p['run'] / 'input' / 'req.txt',
              p['run'] / 'prompt.txt', p['run'] / 'schema.json']:
        f.write_text('dummy', encoding='utf-8')
    return p


def profile_for(p, stage, roots=()):
    return build_profile(home=p['home'], dedicated_home=p['dedicated'], run_dir=p['run'],
                         tmp_dirs=[p['tmp']], write_roots=list(roots), stage=stage)


class FingerprintTests(unittest.TestCase):
    def test_same_permissions_in_two_runs_share_the_fingerprint(self):
        # 完成基準16（Y1）
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            pa, pb = layout(a), layout(b)
            # 同じホーム・同じ案件で run だけ違う形にそろえる
            pb_same = dict(pa, run=pa['home'] / '.riri-mini-runs' / 'run2', tmp=Path(os.path.realpath(b)) / 'tmp')
            t1 = profile_for(pa, 2, [pa['case']])
            t2 = profile_for(pb_same, 2, [pa['case']])
            self.assertNotEqual(profile_hash(t1), profile_hash(t2))
            self.assertEqual(fingerprint(t1, run_dir=pa['run'], tmp_dirs=[pa['tmp']]),
                             fingerprint(t2, run_dir=pb_same['run'], tmp_dirs=[pb_same['tmp']]))

    def test_any_change_of_permissions_or_order_changes_the_fingerprint(self):
        # 完成基準16（Z1）
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            base = profile_for(p, 2, [p['case']])
            fp = lambda t: fingerprint(t, run_dir=p['run'], tmp_dirs=[p['tmp']])
            self.assertNotEqual(fp(base), fp(profile_for(p, 2, [p['other_case']])))  # 作業先
            self.assertNotEqual(fp(base), fp(profile_for(p, 1)))                     # 段
            lines = base.splitlines()
            deny_home = next(i for i, l in enumerate(lines) if l == '(deny file-write*)')
            allow = next(i for i, l in enumerate(lines) if l.startswith('(allow file-write*'))
            swapped = lines[:]
            swapped[deny_home], swapped[allow] = lines[allow], lines[deny_home]
            self.assertNotEqual(fp(base), fp('\n'.join(swapped) + '\n'))            # 順序

    def test_profile_is_deterministic(self):
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            self.assertEqual(profile_for(p, 2, [p['case']]), profile_for(p, 2, [p['case']]))
            with self.assertRaises(ValueError):
                profile_for(p, 3)


# (説明, 段, 書く/読む, 対象のキーと相対パス, 許されるか) —— 表から作らず手で書いた期待値
CASES = [
    ('秘密の読取り', 1, 'read', ('home', '.ssh/id'), False),
    ('秘密の読取り（段2）', 2, 'read', ('home', 'riri-patrol/.env'), False),
    ('りりの state の読取り', 2, 'read', ('home', 'riri-patrol/state/state.json'), False),
    ('ヒロインズ㊙️の読取り', 2, 'read', ('home', 'ヒロインズ㊙️/x'), False),
    ('本番 ~/.codex の読取り', 1, 'read', ('home', '.codex/auth.json'), False),
    ('本番 ~/.codex への書込み', 2, 'write', ('home', '.codex/new'), False),
    ('専用ホームの設定への書込み', 2, 'write', ('dedicated', 'config.toml'), False),
    ('専用ホームの指示への書込み', 2, 'write', ('dedicated', 'AGENTS.md'), False),
    ('専用ホームの認証への書込み', 2, 'write', ('dedicated', 'auth.json'), False),
    ('専用ホームの認証の読取り（決定5・6）', 1, 'read', ('dedicated', 'auth.json'), True),
    ('専用ホームの会話記録への書込み', 1, 'write', ('dedicated', 'sessions/s.jsonl'), True),
    ('専用ホームの SQLite への書込み', 1, 'write', ('dedicated', 'state_5.sqlite'), True),
    # 2026-11-25 の実測で足した2つ（これが無いと起動しない・毎回エラーが出る）
    ('専用ホームの installation_id への書込み', 1, 'write', ('dedicated', 'installation_id'), True),
    ('専用ホームの models_cache.json への書込み', 1, 'write', ('dedicated', 'models_cache.json'), True),
    ('専用ホームの似た名前への書込み', 1, 'write', ('dedicated', 'models_cache.json.bak'), False),
    ('専用ホームの plugins への書込み', 1, 'write', ('dedicated', 'plugins/x.json'), False),
    ('専用ホームの cache への書込み', 1, 'write', ('dedicated', 'cache/x.json'), False),
    ('専用ホームの skills への書込み', 1, 'write', ('dedicated', 'skills/x.md'), False),
    ('run/input への書込み', 2, 'write', ('run', 'input/req.txt'), False),
    ('run/input の読取り', 1, 'read', ('run', 'input/req.txt'), True),
    ('run/work への書込み', 1, 'write', ('run', 'work/a.txt'), True),
    ('run/out への書込み', 1, 'write', ('run', 'out/last.json'), True),
    ('一時領域への書込み', 1, 'write', ('tmp', 'x'), True),
    ('段1で案件への書込み', 1, 'write', ('case', 'a.txt'), False),
    ('段2で許可した案件への書込み', 2, 'write', ('case', 'a.txt'), True),
    ('段2で許可していない案件への書込み', 2, 'write', ('other_case', 'a.txt'), False),
    ('それ以外のホームの読取り', 1, 'read', ('home', 'みに/AGENTS.md'), True),
    ('それ以外のホームへの書込み', 2, 'write', ('home', 'みに/x.txt'), False),
    # 2026-11-25 みにの独立レビュー M3：ホームの外も、表にない所は書けない
    ('ホームの外の兄弟フォルダーへの書込み', 1, 'write', ('outside', 'y.txt'), False),
    ('ホームの外の既存ファイルへの書込み', 2, 'write', ('outside', 'x.txt'), False),
    ('ホームの外の読取り', 1, 'read', ('outside', 'x.txt'), True),
    ('段2でホームの外に作ろうとする', 2, 'write', ('outside', 'new/z.txt'), False),
]

# run と専用ホームをホームの外に置いたとき（M3）。段2の書込み先に run 全体を入れても、
# 写しと設定は守られること
CASES_OUTSIDE = [
    ('外に置いた run/input への書込み', 1, 'write', ('run', 'input/req.txt'), False),
    ('外に置いた run/input への書込み（run 全体が書込み先）', 2, 'write', ('run', 'input/req.txt'), False),
    ('外に置いた prompt.txt への書込み（run 全体が書込み先）', 2, 'write', ('run', 'prompt.txt'), False),
    ('外に置いた schema.json への書込み', 1, 'write', ('run', 'schema.json'), False),
    ('外に置いた run/work への書込み', 1, 'write', ('run', 'work/a.txt'), True),
    ('外に置いた run/out への書込み', 1, 'write', ('run', 'out/last.json'), True),
    ('外に置いた専用ホームの設定への書込み', 1, 'write', ('dedicated', 'config.toml'), False),
    ('外に置いた専用ホームの認証への書込み', 2, 'write', ('dedicated', 'auth.json'), False),
    ('外に置いた専用ホームの会話記録への書込み', 1, 'write', ('dedicated', 'sessions/s.jsonl'), True),
    ('外に置いた専用ホームのほかの場所への書込み', 1, 'write', ('dedicated', 'other.txt'), False),
]

# シェルが使う装置ファイル（/dev/null への書出しなど）は書ける
SHELL_CASES = [
    ('/dev/null への書出し', 'echo x > /dev/null', True),
    ('標準エラー（/dev/fd/2）への書出し', 'echo x > /dev/fd/2', True),
]


@unittest.skipUnless(HAVE_SANDBOX, 'macOS の sandbox-exec がない')
class BoundaryTests(unittest.TestCase):
    def test_hand_written_cases(self):
        # 完成基準3・4・14
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            profiles = {}
            for stage in (1, 2):
                path = Path(os.path.realpath(a)) / f'p{stage}.sb'
                path.write_text(profile_for(p, stage, [p['case']]), encoding='utf-8')
                profiles[stage] = path
            for name, stage, op, (key, rel), allowed in CASES:
                target = p[key] / rel
                if op == 'read':
                    cmd = ['/bin/cat', str(target)]
                else:
                    cmd = ['/usr/bin/touch', str(target)]
                res = subprocess.run(['sandbox-exec', '-f', str(profiles[stage])] + cmd,
                                     capture_output=True)
                with self.subTest(name):
                    self.assertEqual(res.returncode == 0, allowed,
                                     f'{name}: 終了コード {res.returncode} {res.stderr[-200:]!r}')

    def test_run_and_dedicated_home_placed_outside_home(self):
        # M3：配置にかかわらず、写しと専用ホームの設定は書けない
        with tempfile.TemporaryDirectory() as a:
            p = layout_outside(a)
            profiles = {}
            for stage in (1, 2):
                path = Path(os.path.realpath(a)) / f'q{stage}.sb'
                roots = [p['case'], p['run']] if stage == 2 else []
                path.write_text(profile_for(p, stage, roots), encoding='utf-8')
                profiles[stage] = path
            for name, stage, op, (key, rel), allowed in CASES_OUTSIDE:
                target = p[key] / rel
                cmd = ['/bin/cat', str(target)] if op == 'read' else ['/usr/bin/touch', str(target)]
                res = subprocess.run(['sandbox-exec', '-f', str(profiles[stage])] + cmd, capture_output=True)
                with self.subTest(name):
                    self.assertEqual(res.returncode == 0, allowed,
                                     f'{name}: 終了コード {res.returncode} {res.stderr[-200:]!r}')

    def test_links_do_not_open_protected_places(self):
        # 2026-11-25 再検証前の詰め：作業フォルダに作るリンクで、写し・設定・秘密に届かない
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            path = Path(os.path.realpath(a)) / 'p1.sb'
            path.write_text(profile_for(p, 1), encoding='utf-8')
            work, sess = p['run'] / 'work', p['dedicated'] / 'sessions'
            cases = [
                ('ハードリンク：work → run/input', f"ln {p['run'] / 'input' / 'req.txt'} {work / 'h1'}", False),
                ('ハードリンク：sessions → 専用ホームの設定', f"ln {p['dedicated'] / 'config.toml'} {sess / 'h2'}", False),
                ('ハードリンク：work → 秘密', f"ln {p['home'] / '.ssh' / 'id'} {work / 'h3'}", False),
                ('シンボリックリンク経由で写しに書く', f"ln -s {p['run'] / 'input' / 'req.txt'} {work / 's1'} && echo x >> {work / 's1'}", False),
                ('シンボリックリンク経由で秘密を読む', f"ln -s {p['home'] / '.ssh' / 'id'} {work / 's2'} && cat {work / 's2'}", False),
                ('写しを work へ移す', f"mv {p['run'] / 'input' / 'req.txt'} {work / 'm'}", False),
                ('専用ホームの認証を消す', f"rm {p['dedicated'] / 'auth.json'}", False),
            ]
            for name, script, allowed in cases:
                res = subprocess.run(['sandbox-exec', '-f', str(path), '/bin/sh', '-c', script], capture_output=True)
                with self.subTest(name):
                    self.assertEqual(res.returncode == 0, allowed, f'{name}: {res.returncode} {res.stderr[-200:]!r}')
            for f in (p['run'] / 'input' / 'req.txt', p['dedicated'] / 'config.toml', p['dedicated'] / 'auth.json',
                      p['home'] / '.ssh' / 'id'):
                self.assertEqual(f.read_text(encoding='utf-8'), 'dummy', str(f))

    def test_riri_running_from_another_root_keeps_its_state_and_env_closed(self):
        # 完成基準3：りりが ~/riri-patrol 以外から動いていても、その state と .env は読めず書けない
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            root = Path(os.path.realpath(a)) / 'staging' / 'riri'
            (root / 'state').mkdir(parents=True)
            (root / '.env').write_text('dummy', encoding='utf-8')
            (root / 'state' / 'state.json').write_text('dummy', encoding='utf-8')
            for stage, roots in ((1, []), (2, [p['case'], root])):          # 書込み先に入れても開かない
                path = Path(os.path.realpath(a)) / f'r{stage}.sb'
                path.write_text(build_profile(home=p['home'], dedicated_home=p['dedicated'], run_dir=p['run'],
                                              tmp_dirs=[p['tmp']], write_roots=roots, stage=stage,
                                              riri_root=root), encoding='utf-8')
                for name, cmd in (('.env の読取り', ['/bin/cat', str(root / '.env')]),
                                  ('state の読取り', ['/bin/cat', str(root / 'state' / 'state.json')]),
                                  ('.env の書込み', ['/usr/bin/touch', str(root / '.env')]),
                                  ('state の書込み', ['/usr/bin/touch', str(root / 'state' / 'new.json')])):
                    res = subprocess.run(['sandbox-exec', '-f', str(path)] + cmd, capture_output=True)
                    with self.subTest(stage=stage, case=name):
                        self.assertNotEqual(res.returncode, 0)
                if stage == 2:                                                  # ほかの所は書込み先どおり
                    res = subprocess.run(['sandbox-exec', '-f', str(path), '/usr/bin/touch', str(root / 'ok.txt')],
                                         capture_output=True)
                    self.assertEqual(res.returncode, 0)

    def test_shell_device_files(self):
        with tempfile.TemporaryDirectory() as a:
            p = layout(a)
            path = Path(os.path.realpath(a)) / 'p1.sb'
            path.write_text(profile_for(p, 1), encoding='utf-8')
            for name, script, allowed in SHELL_CASES:
                res = subprocess.run(['sandbox-exec', '-f', str(path), '/bin/sh', '-c', script], capture_output=True)
                with self.subTest(name):
                    self.assertEqual(res.returncode == 0, allowed, f'{name}: {res.returncode} {res.stderr[-200:]!r}')


if __name__ == '__main__':
    unittest.main()
