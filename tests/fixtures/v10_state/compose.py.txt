"""Deterministic, lossless instruction rendering with fail-closed safety checks."""
import html
import re

from .errors import SafetyError, UnsafeContent
from .models import REPORT_PAGES, RECIPIENTS
from .parse import ID_RE, validate_request

CLOSINGS = ('よろしくお願いします🍚🔥 ( ᴗ ̫ ᴗ )', 'よろしくお願いします🍚🔥',
            'よろしくお願いします ( ᴗ ̫ ᴗ )')


def assert_safe(text, secrets=()):
    if not isinstance(text, str):
        raise UnsafeContent('文面が文字列ではありません')
    decoded = text
    for _ in range(3):
        decoded = html.unescape(decoded)
    if (re.search(r'<\s*[@!#]|@(?:here|channel|everyone)\b|xox|ntn_|secret_', decoded, re.I)
            or any(secret and (secret in decoded or secret in text) for secret in secrets)
            or any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in decoded)
            or any(c in decoded for c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')):
        raise UnsafeContent('安全でないメンションまたは秘密情報を検出しました')
    return text


def assert_request_safe(req, secrets=()):
    # Only these fields are rendered/sent to the model. Unused source notes and
    # historical requests must not block unrelated outgoing work.
    rendered = ('やること', '期限・実行タイミング', 'GitHubプロジェクト', '案件ページ',
                '手順', '使うもの', '期待する結果', 'やってはいけないこと')
    for value in [req.id, req.requester, *req.recipients,
                  *(req.fields.get(key, '') for key in rendered)]:
        assert_safe(value, secrets)


def _value(value):
    assert_safe(value)
    # Slack renders these entities literally rather than interpreting control syntax.
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\n  ')


def instruction(req, recipients, report_pages=REPORT_PAGES):
    validate_request(req)
    assert_request_safe(req)
    recipients = list(recipients)
    if (not recipients or len(set(recipients)) != len(recipients) or
            any(n not in req.recipients or n not in report_pages or n == 'りり' for n in recipients)):
        raise SafetyError('指示の宛先が不正です')
    pages = []
    for name in recipients:
        url = report_pages[name]
        # Configuration may contain page IDs or full Notion links.
        if re.fullmatch(r'[0-9a-fA-F-]{32,36}', url):
            url = 'https://app.notion.com/p/' + url.replace('-', '')
        if not re.fullmatch(r'https://(?:app\.)?notion\.(?:com|so)/[^\s<>]+', url):
            raise SafetyError('完了報告ページのURLが不正です')
        pages.append(name + ' ' + url)
    f = req.fields
    rows = [
        ' '.join('@' + n for n in recipients), '【りりからのお願い】',
        '依頼ID：' + _value(req.id), '宛先：' + '／'.join(recipients),
        '元の依頼：' + _value(req.requester) + 'から',
        '何を：' + _value(f['やること']),
        'いつまでに：' + _value(f['期限・実行タイミング']),
        '報告先：各自の完了報告ページ（' + '、'.join(pages) + '）＋「たかむねりりあいす」',
        'GitHubプロジェクト：' + _value(f.get('GitHubプロジェクト') or 'なし'),
        '案件ページ：' + _value(f.get('案件ページ') or 'なし'),
        'くわしい内容：' + _value(f.get('手順') or 'なし'),
        '使うもの：' + _value(f.get('使うもの') or 'なし'),
        '期待する結果：' + _value(f.get('期待する結果') or 'なし'),
        'やってはいけないこと：' + _value(f.get('やってはいけないこと') or 'なし'),
        '困ったとき：「たかむねりりあいす」に相談してね', CLOSINGS[0],
    ]
    text = '\n'.join(rows)
    assert_safe(text)
    if len(text) > 1500:
        raise UnsafeContent('必須内容を保った指示が1500文字を超えます')
    return text


def report_summary(reports):
    lines = []
    for report in reports:
        excerpt = re.sub(r'\s+', ' ', report.summary)[:40]
        for value in (report.id, report.reporter, report.result, excerpt):
            assert_safe(value)
        lines.append(f'・{_value(report.reporter)}：{_value(report.id)} → {_value(report.result)}（{_value(excerpt)}）')
    return assert_safe('\n'.join(lines))


def _notice_id(req_id):
    assert_safe(req_id)
    if not ID_RE.fullmatch(req_id):
        raise SafetyError('通知の依頼IDが不正です')
    return req_id


def safe_reference(request_id, reporter=None):
    """Identify quarantined content without quoting any untrusted source text."""
    ident = request_id if isinstance(request_id, str) and ID_RE.fullmatch(request_id) else "形式を確認してください"
    result = "依頼ID：" + ident
    if reporter is not None:
        result += "／報告した子：" + (reporter if reporter in RECIPIENTS else "名前を確認してください")
    return result


def mini_notice(req_id):
    return f'みにへの依頼があるので、あいすかたかむねから伝えてください（依頼ID：{_notice_id(req_id)}）🍚'


def riri_notice(req_id):
    return f'{_notice_id(req_id)} はりり自身あての依頼なので、見回りプログラムでは扱えません。飛ばします🍚'
