"""Every Mac-specific absolute path Riri uses, in one place.

Each path can be overridden with an environment variable. The defaults are
the locations the Mac release already uses, so nothing changes unless an
override is set. See docs/paths.md for the variables and for how to move the
Codex work folder safely.
"""
import os
from pathlib import Path

# The account whose home holds the live checkouts on the Mac.
_MAC_HOME = Path('/Users/sunako.jp')


def _path(name, default):
    value = os.environ.get(name, '').strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(name + ' must be an absolute path')
    return path


# Live checkouts of the three local programs.
RIRI_ROOT = _path('RIRI_ROOT', _MAC_HOME / 'riri-patrol')
MIKI_ROOT = _path('RIRI_MIKI_ROOT', _MAC_HOME / 'miki-AI')
MAMU_ROOT = _path('RIRI_MAMU_ROOT', _MAC_HOME / 'mamu-AI')

# Mamu's approved relay policy (ID colour etc.), read by the mamu bridge.
MAMU_RELAY_POLICY = _path('RIRI_MAMU_RELAY_POLICY', MAMU_ROOT / 'data' / 'relay-policy.json')

# Work files created in a Codex session on 2026-11-14: the frozen A-revision
# evidence (local_revisions) and the phase56 project area (project_ops).
# phase56 manifests record absolute paths under this folder and are pinned by
# hash, so move it only when no phase56 job is in flight (docs/paths.md).
WORKSPACE = _path('RIRI_WORKSPACE', _MAC_HOME / 'Documents' / 'Codex' / '2026-11-14' / 'files-pasted-by-the-user-notion')
PROJECT_OPS_AREA = _path('RIRI_PROJECT_OPS_AREA', WORKSPACE / 'work' / 'phase56-ops')
