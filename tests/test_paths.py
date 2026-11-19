"""Mac-specific paths live only in riri/paths.py and can be overridden."""
import importlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import riri.paths as paths

# What every module saw when it imported riri.paths (real environment and file).
AT_IMPORT = {name: getattr(paths, name) for name in (
    'RIRI_ROOT', 'MIKI_ROOT', 'MAMU_ROOT', 'MAMU_RELAY_POLICY', 'WORKSPACE', 'PROJECT_OPS_AREA')}
ROOT = Path(__file__).resolve().parents[1]
VARS = ('RIRI_ROOT', 'RIRI_MIKI_ROOT', 'RIRI_MAMU_ROOT', 'RIRI_MAMU_RELAY_POLICY',
        'RIRI_WORKSPACE', 'RIRI_PROJECT_OPS_AREA', 'HEROINES_PATHS')
NO_SHARED_FILE = str(ROOT / 'tests' / 'no-such-heroines-paths.json')


def reload_with(env):
    """Reload riri.paths with only `env` set; a real ~/.heroines file is never read."""
    clean = {k: v for k, v in os.environ.items() if k not in VARS}
    clean['HEROINES_PATHS'] = NO_SHARED_FILE
    clean.update(env)
    with patch.dict(os.environ, clean, clear=True):
        return importlib.reload(paths)


class PathsTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(paths)  # back to the real environment for later tests

    def test_defaults_keep_the_current_mac_locations(self):
        p = reload_with({})
        home = p._MAC_HOME
        self.assertEqual(p.RIRI_ROOT, home / 'riri-patrol')
        self.assertEqual(p.MIKI_ROOT, home / 'miki-AI')
        self.assertEqual(p.MAMU_ROOT, home / 'mamu-AI')
        self.assertEqual(p.MAMU_RELAY_POLICY, home / 'mamu-AI/data/relay-policy.json')
        self.assertEqual(p.WORKSPACE, home / 'Documents/Codex/2026-11-14/files-pasted-by-the-user-notion')
        self.assertEqual(p.PROJECT_OPS_AREA, p.WORKSPACE / 'work/phase56-ops')

    def test_modules_use_the_shared_paths(self):
        from riri import local_revisions, local_tasks, mamu_bridge, project_ops
        p = AT_IMPORT
        self.assertEqual(local_tasks.ROOT, p['RIRI_ROOT'])
        self.assertEqual(local_tasks.MIKI, p['MIKI_ROOT'])
        self.assertEqual(local_tasks.MAMU, p['MAMU_ROOT'])
        self.assertEqual(mamu_bridge.RIRI_ROOT, p['RIRI_ROOT'])
        self.assertEqual(mamu_bridge.POLICY_PATH, p['MAMU_RELAY_POLICY'])
        self.assertEqual(local_revisions.WORKSPACE, p['WORKSPACE'])
        self.assertEqual(project_ops.AREA, p['PROJECT_OPS_AREA'])

    def test_environment_overrides_each_path(self):
        p = reload_with({'RIRI_ROOT': '/srv/riri', 'RIRI_MIKI_ROOT': '/srv/miki',
                         'RIRI_MAMU_ROOT': '/srv/mamu', 'RIRI_WORKSPACE': '/srv/work'})
        self.assertEqual(p.RIRI_ROOT, Path('/srv/riri'))
        self.assertEqual(p.MIKI_ROOT, Path('/srv/miki'))
        self.assertEqual(p.MAMU_ROOT, Path('/srv/mamu'))
        self.assertEqual(p.MAMU_RELAY_POLICY, Path('/srv/mamu/data/relay-policy.json'))
        self.assertEqual(p.WORKSPACE, Path('/srv/work'))
        self.assertEqual(p.PROJECT_OPS_AREA, Path('/srv/work/work/phase56-ops'))
        p = reload_with({'RIRI_WORKSPACE': '/srv/work', 'RIRI_PROJECT_OPS_AREA': '/srv/ops',
                         'RIRI_MAMU_RELAY_POLICY': '/srv/policy.json'})
        self.assertEqual(p.PROJECT_OPS_AREA, Path('/srv/ops'))
        self.assertEqual(p.MAMU_RELAY_POLICY, Path('/srv/policy.json'))

    def test_blank_value_keeps_default_and_relative_path_is_refused(self):
        default = reload_with({}).WORKSPACE
        self.assertEqual(reload_with({'RIRI_WORKSPACE': '  '}).WORKSPACE, default)
        with self.assertRaises(ValueError):
            reload_with({'RIRI_WORKSPACE': 'relative/work'})

    def shared_file(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'paths.json'
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
        return str(path)

    def test_shared_file_sets_paths_and_environment_still_wins(self):
        shared = self.shared_file({'_about': 'comment', 'riri_root': '/srv/shared-riri',
                                   'riri_workspace': '/srv/shared-work', 'other_program': '/srv/x'})
        p = reload_with({'HEROINES_PATHS': shared})
        self.assertEqual(p.RIRI_ROOT, Path('/srv/shared-riri'))
        self.assertEqual(p.PROJECT_OPS_AREA, Path('/srv/shared-work/work/phase56-ops'))
        self.assertEqual(p.MIKI_ROOT, p._MAC_HOME / 'miki-AI')
        p = reload_with({'HEROINES_PATHS': shared, 'RIRI_ROOT': '/srv/env-riri'})
        self.assertEqual(p.RIRI_ROOT, Path('/srv/env-riri'))

    def test_other_programs_entries_are_left_alone(self):
        data = json.loads('{"keiba": {"root": "/srv/keiba", "pdf": {"dir": "relative/ok-for-keiba"}}, "keiba_list": [1, 2]}')
        data['riri_root'] = '/srv/shared-riri'
        p = reload_with({'HEROINES_PATHS': self.shared_file(data)})
        self.assertEqual(p.RIRI_ROOT, Path('/srv/shared-riri'))
        self.assertEqual(p.read_shared_paths({'HEROINES_PATHS': self.shared_file(json.loads('{"keiba": {"root": "/srv/keiba", "pdf": {"dir": "relative/ok-for-keiba"}}, "keiba_list": [1, 2]}'))}), {})

    def test_malformed_shared_file_stops_instead_of_guessing(self):
        for data in ('{not json', '["riri_root"]', {'riri_root': 'relative/riri'}, {'riri_root': 3}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                reload_with({'HEROINES_PATHS': self.shared_file(data)})

    def test_no_mac_path_outside_paths_module(self):
        pattern = re.compile(r'/Users/[^/\s\'"]+|Documents/Codex')
        found = []
        for path in sorted([ROOT / 'patrol.py', *(ROOT / 'riri').glob('*.py'), *(ROOT / 'scripts').glob('*')]):
            if path.name == 'paths.py' or path.suffix not in ('.py', '.sh'):
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if pattern.search(line):
                    found.append('%s:%d' % (path.relative_to(ROOT), number))
        self.assertEqual(found, [], 'move Mac paths into riri/paths.py')


if __name__ == '__main__':
    unittest.main()
