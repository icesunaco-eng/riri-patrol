"""Report versions for みに's completion reports (design v1.0, 5-2).  Not wired in yet.

* ``body_hash`` (Y5 / Z4): UTF-8, LF line ends, drop only the version header
  lines at the very top (版 / 前の版 / 本文ハッシュ), strip trailing spaces of
  each line and leading/trailing blank lines.  Spaces inside a line, and any
  「状態：」 or 「版：」 line further down (evidence), are kept.
* ``current_report`` walks the chain from version 1 (no header) through the
  versions whose 前の版 hash matches the previous body hash.  Two blocks with
  the same version, a skipped version or a broken hash means "hold".
"""
import hashlib
import re

HEADER_KEYS = ('版', '前の版', '本文ハッシュ')
_HEADER_RE = re.compile(r'^\s*(版|前の版|本文ハッシュ)\s*[：:]\s*(.*?)\s*$')


def split_header(text):
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    header, i = {}, 0
    while i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if not m or m[1] in header:
            break
        header[m[1]] = m[2]
        i += 1
    return header, lines[i:]


def body_hash(text):
    _, body = split_header(text)
    body = [line.rstrip(' \t') for line in body]
    while body and not body[0].strip(' \t'):
        body.pop(0)
    while body and not body[-1].strip(' \t'):
        body.pop()
    return hashlib.sha256('\n'.join(body).encode('utf-8')).hexdigest()


def _version_number(header):
    if not header:
        return 1
    m = re.fullmatch(r'第(\d+)版', header.get('版', ''))
    return int(m[1]) if m else None


def _prev_hash(header):
    m = re.search(r'([0-9a-f]{64})', header.get('前の版', ''))
    return m[1] if m else None


def current_report(blocks):
    """``blocks``: list of (block_id, text) for one request ID, oldest first.

    Returns ``{'state': 'ok', 'version': n, 'block_id': ..., 'hash': ...}`` or
    ``{'state': 'hold', 'reason': ...}``.
    """
    by_version = {}
    for block_id, text in blocks:
        header, _ = split_header(text)
        n = _version_number(header)
        if n is None:
            return {'state': 'hold', 'reason': '版の書き方が不正'}
        if header and header.get('本文ハッシュ') and header['本文ハッシュ'] != body_hash(text):
            return {'state': 'hold', 'reason': f'第{n}版の本文ハッシュが本文と合わない'}
        if n in by_version:
            return {'state': 'hold', 'reason': f'第{n}版が2つある'}
        by_version[n] = (block_id, text, header)
    if not by_version:
        return {'state': 'none'}
    if sorted(by_version) != list(range(1, max(by_version) + 1)):
        return {'state': 'hold', 'reason': '版が飛んでいる'}
    prev = None
    for n in range(1, max(by_version) + 1):
        block_id, text, header = by_version[n]
        if n > 1 and _prev_hash(header) != prev:
            return {'state': 'hold', 'reason': f'第{n}版の前の版のハッシュが合わない'}
        prev = body_hash(text)
    return {'state': 'ok', 'version': max(by_version), 'block_id': by_version[max(by_version)][0], 'hash': prev}


def new_version_text(body, version, prev_block_id, prev_hash):
    """Text for version N (N >= 2) with the three header lines."""
    if version < 2:
        raise ValueError('第2版から見出しをつける')
    h = body_hash(body)
    return f'版：第{version}版\n前の版：{prev_block_id}・{prev_hash}\n本文ハッシュ：{h}\n{body}'


def notice_id(request_id, version, target):
    """One notice per version and target (5-2)."""
    return f'mini-report:{request_id}:v{version}:{target}'
