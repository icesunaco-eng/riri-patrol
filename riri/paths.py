"""Every Mac-specific absolute path Riri uses, in one place.

Each path is chosen in this order:
1. its environment variable (RIRI_ROOT etc.),
2. the shared file ~/.heroines/paths.json, which miki and mamu also read
   (HEROINES_PATHS can point somewhere else),
3. the built-in default, which is the location the Mac release already uses.

Nothing changes unless one of the first two is set. See docs/paths.md.
"""
import json
import os
from pathlib import Path

# The account whose home holds the live checkouts on the Mac.
_MAC_HOME = Path('/Users/sunako.jp')


# The entries of ~/.heroines/paths.json this program reads.
KEYS = ('riri_root', 'miki_root', 'mamu_root', 'mamu_relay_policy', 'riri_workspace', 'riri_project_ops_area')


def read_shared_paths(environ=None, keys=KEYS):
    """This program's paths from ~/.heroines/paths.json (or $HEROINES_PATHS).

    {} when the file is absent. Other programs keep their own entries in the
    same file (for example a "keiba" object), so only `keys` are read and
    checked; everything else is left alone. A used key must hold an absolute
    path, and a file that is not a JSON object stops the program.
    """
    environ = os.environ if environ is None else environ
    location = environ.get('HEROINES_PATHS', '').strip()
    if location:
        path = Path(location).expanduser()
    else:
        try:
            path = Path.home() / '.heroines' / 'paths.json'
        except (KeyError, RuntimeError):
            return {}
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(str(path) + ' must contain a JSON object')
    shared = {}
    for key, value in data.items():
        if key not in keys:
            continue
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ValueError('%s: %s must be an absolute path' % (path, key))
        shared[key] = Path(value).expanduser()
    return shared


_SHARED = read_shared_paths()


def _path(name, key, default):
    value = os.environ.get(name, '').strip()
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(name + ' must be an absolute path')
        return path
    return _SHARED.get(key, default)


# Live checkouts of the three local programs.
RIRI_ROOT = _path('RIRI_ROOT', 'riri_root', _MAC_HOME / 'riri-patrol')
MIKI_ROOT = _path('RIRI_MIKI_ROOT', 'miki_root', _MAC_HOME / 'miki-AI')
MAMU_ROOT = _path('RIRI_MAMU_ROOT', 'mamu_root', _MAC_HOME / 'mamu-AI')

# Mamu's approved relay policy (ID colour etc.), read by the mamu bridge.
MAMU_RELAY_POLICY = _path('RIRI_MAMU_RELAY_POLICY', 'mamu_relay_policy', MAMU_ROOT / 'data' / 'relay-policy.json')

# Work files created in a Codex session on 2026-11-14: the frozen A-revision
# evidence (local_revisions) and the phase56 project area (project_ops).
# phase56 manifests record absolute paths under this folder and are pinned by
# hash, so move it only when no phase56 job is in flight (docs/paths.md).
WORKSPACE = _path('RIRI_WORKSPACE', 'riri_workspace',
                  _MAC_HOME / 'Documents' / 'Codex' / '2026-11-14' / 'files-pasted-by-the-user-notion')
PROJECT_OPS_AREA = _path('RIRI_PROJECT_OPS_AREA', 'riri_project_ops_area', WORKSPACE / 'work' / 'phase56-ops')
