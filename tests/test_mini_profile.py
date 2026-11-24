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
    }
    for p in [paths['dedicated'] / 'sessions', paths['dedicated'] / 'plugins', paths['dedicated'] / 'cache',
              paths['dedicated'] / 'skills', paths['run'] / 'work', paths['run'] / 'out',
              paths['run'] / 'input', paths['tmp'], paths['case'], paths['other_case'],
              home / '.codex', home / '.ssh', home / 'riri-patrol' / 'state', home / 'ヒロインズ㊙️']:
        p.mkdir(parents=True, exist_ok=True)
    for f in [home / '.codex' / 'auth.json', home / '.ssh' / 'id', home / 'riri-patrol' / '.env',
              home / 'riri-patrol' / 'state' / 'state.json', home / 'ヒロインズ㊙️' / 'x',
              paths['dedicated'] / 'config.toml', paths['dedicated'] / 'AGENTS.md',
              paths['dedicated'] / 'auth.json', paths['run'] / 'input' / 'req.txt',
              home / 'みに' / 'AGENTS.md']:
        f.write_text('dummy', encoding='utf-8')
    return paths


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
            deny_home = next(i for i, l in enumerate(lines) if l.startswith('(deny file-write* (subpath'))
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


if __name__ == '__main__':
    unittest.main()
