"""Conservative parsing of the supported Notion block formats. No execution."""
import hashlib
import re

from .errors import RequestIncomplete, StatusMissing
from .models import Request, Report, RECIPIENTS

ID_PATTERN = r'(?:🤍|💚|💙|🩵|❤️|💛)[0-9]{8}-[0-9]+'
ID_RE = re.compile(ID_PATTERN)
FIELDS = {'依頼ID', '依頼した子', '宛先', '状態', '案件ページ', 'GitHubプロジェクト',
          'やること', '手順', '使うもの', 'やってはいけないこと', '期待する結果',
          '期限・実行タイミング', '報告先', 'うまくいかないとき', '報告した子', '結果',
          'やったこと', '証拠', 'できなかったこと・未確認', 'ブロッカー', '次にやること'}
MULTILINE = {'手順', 'やったこと', '証拠', 'できなかったこと・未確認', 'ブロッカー',
             '次にやること', 'やってはいけないこと', '期待する結果', '使うもの'}


def normalize_key(key):
    return re.sub(r'\s+', '', re.split(r'[（(]', key, maxsplit=1)[0])


def block_text(block):
    kind = block.get('type', '')
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', ''))
                   for x in block.get(kind, {}).get('rich_text', []))


def fields_from_text(text):
    fields, active = {}, None
    for line in text.splitlines():
        pair = re.match(r'^\s*([^：:]+)[：:](.*)$', line)
        key = normalize_key(pair[1]) if pair else None
        if key in FIELDS:
            if key in fields:
                raise RequestIncomplete('項目が重複しています')
            fields[key] = pair[2].strip()
            active = key
        elif (pair and not re.match(r'^\s*(?:[0-9]+[.)]|[-*•]|https?://)', line)) or re.match(r'^\d{4}-\d{2}-\d{2}\s.*見回り', line):
            active = None  # old log lines are not instructions
        elif active in MULTILINE:
            fields[active] += '\n' + line
    return {k: v.strip() for k, v in fields.items()}


def _candidates(blocks, kinds):
    excluded_level = None
    for block in blocks:
        if block.get('in_trash') or block.get('archived'):
            continue
        kind, text = block.get('type', ''), block_text(block)
        if kind.startswith('heading_'):
            level = int(kind[-1])
            if excluded_level is not None and level <= excluded_level:
                excluded_level = None
            if re.search(r'記入例|ひな型|雛形|テンプレート|コピー用', text):
                excluded_level = level if excluded_level is None else min(level, excluded_level)
            continue
        if excluded_level is None and kind in kinds:
            yield block, text


def _recipients(fields):
    if '宛先' in fields:
        names = [x for x in re.split(r'[@＠\s、,／/]+', fields['宛先']) if x]
        # Unknown names remain present so validation cannot silently drop a recipient.
        return list(dict.fromkeys(names)), '欄'
    text = fields.get('やること', '') + '\n' + fields.get('手順', '')
    # URLs (including credentials, path/query @ signs) are data, not recipients.
    text = re.sub(r'https?://[^\s<>「」（）、，]+', ' ', text, flags=re.I)
    # Do not treat ordinary email addresses as recipient declarations.
    text = re.sub(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', ' ', text)
    # Preserve unknown mention tokens too: validate_request rejects the entire
    # request instead of silently dispatching only to the recognized subset.
    names = re.findall(r'(?<![A-Za-z0-9_.+-])[@＠]([^\s、，,／/：:。！？!?（）()「」『』<>]+)', text)
    return list(dict.fromkeys(names)), '@名前'


def parse_requests(blocks):
    out = []
    for block, text in _candidates(blocks, {'code'}):
        if not text.lstrip().startswith('【りりへの依頼】'):
            continue
        fields = fields_from_text(text)
        request_id = fields.get('依頼ID', '')
        if not request_id or 'YYYYMMDD' in request_id:
            continue
        if re.search(r'記入例|コピー用ひな型', text.splitlines()[0]):
            continue
        names, source = _recipients(fields)
        out.append(Request(id=request_id, requester=fields.get('依頼した子', ''),
                           recipients=names, recipients_source=source,
                           status=fields.get('状態', ''), block_id=block['id'], text=text,
                           fields=fields, block_type='code'))
    return out


def validate_request(req):
    if req.status not in {'未着手', '対応中', '完了'}:
        raise StatusMissing('状態が書かれていません')
    if not ID_RE.fullmatch(req.id):
        raise RequestIncomplete('依頼IDの形式が不正です')
    if (not req.requester or not req.recipients or
            any(n not in RECIPIENTS for n in req.recipients) or
            not req.fields.get('やること') or not req.fields.get('期限・実行タイミング')):
        raise RequestIncomplete('依頼の必須項目が不足、または宛先が不正です')


def replace_status(text, status):
    if status not in {'未着手', '対応中', '完了'}:
        raise StatusMissing('不正な状態です')
    pattern = re.compile(r'^(\s*状態[ \t　]*[：:][ \t　]*)([^\r\n]*)(\r?)$', re.MULTILINE)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise StatusMissing('状態の行を一意に特定できません')
    m = matches[0]
    if m[2].strip() not in {'未着手', '対応中', '完了'}:
        raise StatusMissing('元の状態が不正です')
    trailing = re.search(r'[ \t　]*$', m[2])[0]
    return text[:m.start(2)] + status + trailing + text[m.end(2):]


def _report_field_values(text, name):
    """Read labeled references without resolving ambiguous duplicate fields."""
    for line in text.splitlines():
        pair = re.match(r'^\s*([^：:]+)[：:](.*)$', line)
        if pair and normalize_key(pair[1]) == name:
            yield pair[2].strip()


def report_references(text):
    """Retain exact IDs and ambiguity; never guess a malformed ID's target."""
    values = list(_report_field_values(text, '依頼ID'))
    matches = [re.match('^(' + ID_PATTERN + r')(?=$|\s|[（(])', value) for value in values]
    return {match[1] for match in matches if match}, not matches or any(match is None for match in matches)


def validate_report(fields, expected_reporter=None):
    if not {'依頼ID', '報告した子', '結果'} <= fields.keys():
        raise RequestIncomplete('完了報告の必須項目が不足しています')
    match = re.match('^(' + ID_PATTERN + r')(?=$|\s|[（(])', fields['依頼ID'])
    reporter = fields['報告した子'].lstrip('@＠')
    if (not match or reporter not in RECIPIENTS or reporter == 'りり' or
            fields['結果'] not in {'完了', '一部完了', 'できなかった'} or
            (expected_reporter is not None and reporter != expected_reporter)):
        raise RequestIncomplete('完了報告のID・報告者・結果を確認してください')
    return match[1], reporter


def parse_reports(blocks, expected_reporter=None, *, on_invalid=None):
    """Strict by default; patrol may isolate each malformed source report.

    Only a source-format RequestIncomplete is recoverable here. The callback,
    transport, STOP, deadline and unexpected failures propagate normally.
    """
    out = []
    for block, text in _candidates(blocks, {'code', 'paragraph'}):
        first_line = text.lstrip().split('\n', 1)[0]
        if re.search(r'記入例|コピー用ひな型', first_line):
            continue
        raw_ids = list(_report_field_values(text, '依頼ID'))
        candidate = (text.lstrip().startswith('【完了報告】') or
                     (any(raw_ids) and bool(list(_report_field_values(text, '報告した子')))))
        if not candidate:
            continue
        # Empty ID fields and placeholder IDs identify the copy template.
        # A header with no ID field at all is truncated, not a template.
        if len(raw_ids) == 1 and (not raw_ids[0] or 'YYYYMMDD' in raw_ids[0]):
            continue
        try:
            fields = fields_from_text(text)
            request_id, reporter = validate_report(fields, expected_reporter)
        except RequestIncomplete as exc:
            if on_invalid is None:
                raise
            on_invalid(block, text, exc)
            continue
        out.append(Report(id=request_id, reporter=reporter, result=fields['結果'],
                          summary=fields.get('やったこと', ''), block_id=block['id'], text=text,
                          digest=hashlib.sha256(text.encode('utf-8')).hexdigest()))
    return out
