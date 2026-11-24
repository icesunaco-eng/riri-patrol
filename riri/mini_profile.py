"""Outer sandbox profile for a みに run, generated from one versioned table.

Design: みに wake design v1.0 (Notion), 4-5 (and Y1 / Z1 for the fingerprint).
Nothing in patrol calls this yet.

Rule order matters for sandbox-exec (later rules win), so the profile is always
emitted in the same fixed order from the table below:

1. allow by default (network and reads elsewhere are allowed: decision 5/6)
2. deny writes anywhere under the home folder
3. allow writes to the run's work/out folders, temp, the dedicated Codex home's
   measured writable places, and (stage 2 only) the approved write roots
4. deny reads and writes of the secret places and the production ~/.codex last,
   so nothing earlier can re-open them

The fingerprint replaces only the per-run paths with role names and keeps every
rule in its order, so two runs with the same permissions match and any change
of permissions, order or exceptions does not (Z1).
"""
import hashlib
import os

TABLE_VERSION = 'mini-profile-2026-11-25.1'

# Relative to the real home folder.  Matches the secret row of the 4-5 table.
SECRET_SUBPATHS = ('.ssh', 'ヒロインズ㊙️', '.heroines', '.claude', 'Library/Keychains',
                   'riri-patrol/state')
SECRET_LITERALS = ('riri-patrol/.env', 'mamu-AI/.env', 'miki-AI/.env')
PRODUCTION_CODEX = '.codex'

# Places inside the dedicated Codex home that Codex CLI 0.163 writes.  Measured on
# 2026-11-25 (roadmap 2) with the real model: without installation_id the app-server
# does not start, and without models_cache.json every run logs a cache error.
# cache/ and plugins/ are not here: apps / plugins are switched off (see build_argv).
DEDICATED_WRITABLE_DIRS = ('sessions', 'log', 'logs', 'thread-writer-locks', 'shell_snapshots', 'tmp')
DEDICATED_WRITABLE_FILE_PATTERNS = (r'[^/]*\.sqlite[^/]*', r'history\.jsonl')
DEDICATED_WRITABLE_FILES = ('installation_id', 'models_cache.json')


def _q(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _regex_escape(path):
    out = []
    for ch in str(path):
        out.append('\\' + ch if ch in '.^$*+?()[]{}|\\' else ch)
    return ''.join(out)


def build_profile(*, home, dedicated_home, run_dir, tmp_dirs, write_roots, stage):
    """Return the profile text.  All paths must already be real (symlinks resolved)."""
    if stage not in (1, 2):
        raise ValueError('段は 1 か 2')
    home = os.path.realpath(home)
    dedicated_home = os.path.realpath(dedicated_home)
    run_dir = os.path.realpath(run_dir)
    lines = [
        '(version 1)',
        f';; table {TABLE_VERSION} stage {stage}',
        '(allow default)',
        f'(deny file-write* (subpath {_q(home)}))',
        '(allow file-write*',
        f'  (subpath {_q(os.path.join(run_dir, "work"))})',
        f'  (subpath {_q(os.path.join(run_dir, "out"))})',
    ]
    for tmp in tmp_dirs:
        lines.append(f'  (subpath {_q(os.path.realpath(tmp))})')
    for name in DEDICATED_WRITABLE_DIRS:
        lines.append(f'  (subpath {_q(os.path.join(dedicated_home, name))})')
    for pattern in DEDICATED_WRITABLE_FILE_PATTERNS:
        lines.append(f'  (regex #"^{_regex_escape(dedicated_home)}/{pattern}$")')
    for name in DEDICATED_WRITABLE_FILES:
        lines.append(f'  (literal {_q(os.path.join(dedicated_home, name))})')
    if stage == 2:
        for root in write_roots:
            lines.append(f'  (subpath {_q(os.path.realpath(root))})')
    lines.append(')')
    lines.append('(deny file-read* file-write*')
    lines.append(f'  (subpath {_q(os.path.join(home, PRODUCTION_CODEX))})')
    for rel in SECRET_SUBPATHS:
        lines.append(f'  (subpath {_q(os.path.join(home, rel))})')
    for rel in SECRET_LITERALS:
        lines.append(f'  (literal {_q(os.path.join(home, rel))})')
    lines.append(')')
    return '\n'.join(lines) + '\n'


def fingerprint(profile_text, *, run_dir, tmp_dirs):
    """SHA-256 of the profile with only per-run paths replaced by role names (Z1).

    The rules keep their order; nothing is sorted.
    """
    text = profile_text
    replacements = [(os.path.realpath(run_dir), '<RUN_DIR>')]
    replacements += [(os.path.realpath(t), f'<RUN_TMP_{i}>') for i, t in enumerate(tmp_dirs)]
    for path, role in sorted(replacements, key=lambda x: -len(x[0])):
        text = text.replace(_q(path)[1:-1], role).replace(_regex_escape(path), role)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def profile_hash(profile_text):
    """Hash of the exact profile that was applied (kept in the receipt for audit)."""
    return hashlib.sha256(profile_text.encode('utf-8')).hexdigest()
