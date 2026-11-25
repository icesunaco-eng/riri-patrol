"""Notion block-only operations with optimistic reread and validated log tables."""
from datetime import datetime
import copy
import json
import re
from urllib.parse import urlencode
from .errors import AccountMismatch, RemoteError, SafetyError, StatusMissing, NotionConflict, StopRequested, TimeBudgetExceeded
from .slack import safe_text
from .eventlog import row_ids

LOG_COLUMNS = ('日時（JST）', '見回った時刻', 'どこまで拾ったか', '依頼 ID', '依頼元', 'やったこと', '結果', '証拠', 'メモ')
STATUS = re.compile(r'(?m)^([ \t\u3000]*状[ \t\u3000]*態[ \t\u3000]*[：:][ \t\u3000]*)([^\r\n]*?)([ \t\u3000]*)(?=\r?$)')


def status_matches(text):
    """The 状態 lines that are the status field (re-review 💚20261125-16 T3).

    A request is read with P3 (``parse.locate_status_line``): only the 状態 lines before
    the 案件ページ line are the field; later ones (quoted in the steps or the evidence) are
    body text.  The write side uses the same rule, so a request the patrol accepted can be
    written.  Without a 案件ページ line the whole text is searched, as before."""
    if not text.lstrip().startswith('【りりへの依頼】'):
        return list(STATUS.finditer(text))
    # Exactly parse.locate_status_line (the same regex too: 「状 態：」 is body text for P3,
    # so the writer must not count it as a second status line).
    from .parse import locate_status_line
    try:
        line = locate_status_line(text)
    except StatusMissing:
        return []
    found = STATUS.match(text, line.start())
    return [found] if found else []

def rich_text(items):
    return ''.join(x.get('text', {}).get('content', x.get('plain_text', '')) for x in items)

def text_chunks(text):
    """Keep each Notion text element within 2000 UTF-16 units and code points."""
    start, units = 0, 0
    for pos, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > 2000:
            yield text[start:pos]
            start, units = pos, 0
        units += width
    if start < len(text):
        yield text[start:]

def _chunks(text):
    return [{'type': 'text', 'text': {'content': chunk}} for chunk in text_chunks(text)] or [{'type': 'text', 'text': {'content': ''}}]

class NotionClient:
    def __init__(self, token, transport, version='2026-03-11', *, operational=None):
        if version != '2026-03-11':
            raise SafetyError('Unsupported Notion API version')
        self.token, self.transport, self.version = token, transport, version
        self.operational = operational
        self._tables = {}
        self._versions = {}

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9-]+', value):
            raise SafetyError('Invalid Notion block ID')
        return value

    def _request(self, method, block_id, children=False, query=None, payload=None):
        url = 'https://api.notion.com/v1/blocks/' + self._id(block_id) + ('/children' if children else '')
        if query:
            url += '?' + urlencode(query)
        result = self.transport.request(method, url, headers={'Authorization': 'Bearer ' + self.token,
            'Notion-Version': self.version}, payload=payload, timeout=20)
        if not isinstance(result, dict) or result.get('object') == 'error':
            raise RemoteError('Notion API rejected response', uncertain=method == 'PATCH')
        return result

    def read_page(self, page_id):
        """The page object (properties, parent, last_edited_*) — read only."""
        url = 'https://api.notion.com/v1/pages/' + self._id(page_id)
        result = self.transport.request('GET', url, headers={'Authorization': 'Bearer ' + self.token,
            'Notion-Version': self.version}, payload=None, timeout=20)
        if not isinstance(result, dict) or result.get('object') != 'page':
            raise RemoteError('Notion API rejected page response')
        return result

    def read_blocks(self, page_id):
        rows, seen, cursor = [], set(), None
        for _ in range(10000):
            query = {'page_size': 100}
            if cursor:
                query['start_cursor'] = cursor
            result = self._request('GET', page_id, children=True, query=query)
            batch = result.get('results')
            if not isinstance(batch, list) or any(not isinstance(b, dict) for b in batch):
                raise RemoteError('Incomplete Notion block response')
            rows.extend(b for b in batch if not b.get('in_trash', False))
            for block in batch:
                if block.get('id') and block.get('last_edited_time'):
                    self._versions[block['id'].replace('-', '').lower()] = block['last_edited_time']
            if result.get('has_more') is False:
                return rows
            cursor = result.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RemoteError('Incomplete Notion pagination')
            seen.add(cursor)
        raise RemoteError('Notion pagination limit reached')

    def set_status(self, block_id, original_text, status, *, dry_run=False, audit=None, expected_last_edited_time=None, practice_status=None):
        if practice_status is not None:
            if not dry_run:
                raise SafetyError('Virtual status is only allowed in practice mode')
            if practice_status not in ('未着手', '対応中', '完了'):
                raise SafetyError('Invalid virtual practice status')
        if status not in ('対応中', '完了'):
            raise SafetyError('Disallowed status transition')
        block = self._request('GET', block_id)
        returned_id = block.get('id')
        if (block.get('object') != 'block' or block.get('type') != 'code'
            or block.get('in_trash') is not False or not isinstance(returned_id, str)
            or returned_id.replace('-', '').lower() != block_id.replace('-', '').lower()):
            raise SafetyError('Only the exact active code block status can be changed')
        items = block.get('code', {}).get('rich_text', [])
        if any(x.get('type', 'text') != 'text' or not isinstance(x.get('text', {}).get('content'), str) for x in items):
            raise SafetyError('Unsupported rich text in status block')
        text = rich_text(items)
        before_version = block.get('last_edited_time')
        try:
            self._version_time(before_version)
        except (TypeError, ValueError):
            raise SafetyError('Notion block lacks a valid edit version') from None
        observed_version = expected_last_edited_time or self._versions.get(block_id.replace('-', '').lower())
        audit_entry = {'operation': 'notion_status', 'block_id': block_id, 'dry_run': dry_run,
                       'before_text': text, 'original_text': original_text, 'practice_status': practice_status,
                       'before_last_edited_time': before_version, 'observed_last_edited_time': observed_version}
        if audit:
            audit(dict(audit_entry, phase='before'))
        if observed_version is not None and before_version != observed_version:
            raise NotionConflict('Notion edit version changed before writing; manual review required')
        if text != original_text:
            raise NotionConflict('Notion block changed since reading; manual review required')
        matches = status_matches(text)
        if len(matches) != 1 or matches[0].group(2) not in ('未着手', '対応中', '完了'):
            raise StatusMissing('Status line is missing or ambiguous')
        match = matches[0]
        remote_previous = match.group(2)
        # Only the transition uses the persisted practice state. The complete
        # remote text/version checks above always compare the actual snapshot.
        previous = remote_previous if practice_status is None else practice_status
        order = {'未着手': 0, '対応中': 1, '完了': 2}
        if practice_status is not None and order[practice_status] < order[remote_previous]:
            raise SafetyError('Virtual practice status cannot precede remote status')
        if previous == status:
            return
        if (previous, status) not in (('未着手', '対応中'), ('対応中', '完了')):
            raise SafetyError('Disallowed status transition')
        start, end = match.span(2)
        # Preserve annotations, hyperlinks and every non-status character, including split rich text.
        updated, offset, inserted = [], 0, False
        for item in items:
            content = item['text']['content']
            stop = offset + len(content)
            new = content
            if stop > start and offset < end:
                before = content[:max(0, start-offset)]
                after = content[max(0, end-offset):]
                new = before + (status if not inserted else '') + after
                inserted = True
            for chunk in text_chunks(new):
                out = {k: copy.deepcopy(v) for k, v in item.items() if k in ('type', 'text', 'annotations')}
                out['text']['content'] = chunk
                updated.append(out)
            offset = stop
        if rich_text(updated) != text[:start] + status + text[end:]:
            raise SafetyError('Status patch preservation failed')
        expected_text = rich_text(updated)
        if dry_run:
            if audit:
                audit(dict(audit_entry, phase='practice_validated', expected_text=expected_text))
            return
        patch_uncertain = False
        try:
            result = self._request('PATCH', block_id, payload={'code': {'rich_text': updated}})
        except (StopRequested, TimeBudgetExceeded):
            # Transport raises these before attempting this PATCH. Preserve the
            # control-flow signal and do not perform any further remote reads.
            raise
        except RemoteError as error:
            if not error.uncertain:
                # Explicit server rejection: the mutation is known not to have run.
                raise
            patch_uncertain = True
            result = {}
        except Exception:
            # Unknown transport/timeout/interrupt outcomes cannot authorize resume.
            patch_uncertain = True
            result = {}
        try:
            returned_id = result.get('id')
            confirmed = (result.get('object') == 'block' and result.get('type') == 'code'
                and isinstance(returned_id, str)
                and returned_id.replace('-', '').lower() == block_id.replace('-', '').lower()
                and result.get('in_trash') is False
                and self._response_text(result['code']['rich_text']) == rich_text(updated))
        except (KeyError, TypeError, AttributeError, ValueError):
            confirmed = False
        # Always reread after a returned PATCH, even if its response is malformed.
        # No automatic repairing write is permitted following any conflict.
        try:
            after = self._request('GET', block_id)
            after_kind = after.get('type')
            after_text = self._response_text(after.get(after_kind, {}).get('rich_text'))
            after_version = after.get('last_edited_time')
            response_version = result.get('last_edited_time')
            after_id = after.get('id', '')
            stable = (not patch_uncertain and confirmed and after.get('object') == 'block' and after.get('type') == 'code'
                and isinstance(after_id, str)
                and after_id.replace('-', '').lower() == block_id.replace('-', '').lower()
                and after.get('in_trash') is False and after_text == expected_text
                and after_version == response_version
                and self._version_time(after_version) >= self._version_time(before_version))
            if audit:
                audit(dict(audit_entry, phase='after', after_text=after_text,
                           after_last_edited_time=after_version, patch_last_edited_time=response_version,
                           expected_text=expected_text, confirmed=bool(stable),
                           patch_uncertain=patch_uncertain))
        except (StopRequested, TimeBudgetExceeded) as error:
            # PATCH was already attempted, so stopping verification must retain
            # the write intent as uncertain while preserving STOP/deadline flow.
            error.uncertain = True
            if audit:
                try:
                    audit(dict(audit_entry, phase='after_unavailable', expected_text=expected_text,
                               patch_uncertain=patch_uncertain, verification_uncertain=True,
                               interrupted_by=type(error).__name__))
                except Exception:
                    pass
            raise
        except Exception:
            if audit:
                try:
                    audit(dict(audit_entry, phase='after_unavailable', expected_text=expected_text,
                               patch_uncertain=patch_uncertain))
                except Exception:
                    pass
            raise NotionConflict('Notion post-write verification unavailable; manual review required') from None
        if patch_uncertain:
            raise NotionConflict('Notion PATCH outcome is uncertain; manual review required')
        if not stable:
            raise NotionConflict('Notion post-write content or edit version conflicts; manual review required')
        self._versions[block_id.replace('-', '').lower()] = after_version

    @staticmethod
    def _version_time(value):
        if not isinstance(value, str):
            raise ValueError('Missing edit version')
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('Edit version lacks timezone')
        return result

    @staticmethod
    def _response_text(items):
        if not isinstance(items, list):
            raise ValueError('Invalid rich text')
        contents = []
        for item in items:
            if not isinstance(item, dict) or item.get('type') != 'text':
                raise ValueError('Invalid rich text item')
            value = item.get('text', {}).get('content')
            if not isinstance(value, str):
                raise ValueError('Missing rich text content')
            contents.append(value)
        return ''.join(contents)

    def read_log_rows(self, table_id):
        rows = self.read_blocks(table_id)
        if any(b.get('type') != 'table_row' or len(b.get('table_row', {}).get('cells', [])) != 9 for b in rows):
            raise SafetyError('Invalid nine-column log table rows')
        return rows

    def find_log_table(self, page_id):
        heading = False
        for block in self.read_blocks(page_id):
            kind = block.get('type', '')
            if kind.startswith('heading_'):
                heading = rich_text(block.get(kind, {}).get('rich_text', [])).strip().lstrip('# ').strip() == 'ログ一覧（新しい順）'
            if heading and kind == 'table':
                meta = block.get('table', {})
                if meta.get('table_width') != 9 or meta.get('has_column_header') is not True:
                    raise SafetyError('Log table schema does not match')
                table_id = self._id(block.get('id'))
                rows = self.read_log_rows(table_id)
                if not rows or tuple(rich_text(c) for c in rows[0]['table_row']['cells']) != LOG_COLUMNS:
                    raise SafetyError('Log table column names do not match')
                header_id = self._id(rows[0].get('id'))
                self._tables[table_id] = header_id
                return table_id, header_id, rows
        raise SafetyError('Log table was not found below its heading')

    def append_rows(self, table_id, header_id, rows, *, dry_run=False):
        try:
            if self._tables.get(table_id) != header_id:
                raise SafetyError('Log table must be verified before appending')
            if not rows or len(rows) > 100:
                raise SafetyError('Log append batch must contain 1 to 100 rows')
            current = self.read_log_rows(table_id)
            if not current or current[0].get('id') != header_id or tuple(rich_text(c) for c in current[0]['table_row']['cells']) != LOG_COLUMNS:
                raise SafetyError('Log header changed before append')
            children = []
            for row in rows:
                if isinstance(row, dict):
                    if row.get('type') != 'table_row':
                        raise SafetyError('Only table rows may be appended')
                    cells = row.get('table_row', {}).get('cells', [])
                    values = [rich_text(c) for c in cells]
                else:
                    values = row
                if len(values) != 9 or any(not isinstance(v, str) for v in values):
                    raise SafetyError('Log rows must have nine string cells')
                for value in values:
                    if value:
                        safe_text(value, (self.token,))
                children.append({'object': 'block', 'type': 'table_row', 'table_row': {'cells': [_chunks(v) for v in values]}})
            expected = set()
            for child in children:
                markers = [part[1:] for part in rich_text(child['table_row']['cells'][8]).split()
                           if part.startswith('#e')]
                if (len(markers) != 1 or not re.fullmatch(r'e[A-Za-z0-9_-]+', markers[0])
                        or markers[0] in expected or row_ids([child]) != set(markers)):
                    raise SafetyError('Log rows require one unique event marker each')
                expected.add(markers[0])
        except (SafetyError, RemoteError) as exc:
            # No append request has begun, so retrying after repair is safe.
            exc.not_sent = True
            if isinstance(exc, RemoteError):
                exc.uncertain = False
            raise
        if dry_run:
            return
        # A prior confirmed write is never appended twice, even if callers send
        # a batch which still contains some already-persisted events.
        existing = row_ids(current)
        children = [child for child in children if not row_ids([child]) <= existing]
        if not children:
            return
        result = None
        try:
            result = self._request('PATCH', table_id, children=True, payload={'children': children,
                'position': {'type': 'after_block', 'after_block': {'id': header_id}}})
        except (AccountMismatch, StopRequested, TimeBudgetExceeded):
            # Transport guards run before this request; no follow-up I/O on STOP.
            raise
        except RemoteError as error:
            if not error.uncertain:
                raise
        except Exception:
            # Unknown write outcomes may only be resolved through a full read.
            pass
        try:
            after = self.read_log_rows(table_id)
            if (not after or after[0].get('id') != header_id or
                    tuple(rich_text(c) for c in after[0]['table_row']['cells']) != LOG_COLUMNS):
                raise SafetyError('Log header changed during append verification')
            if not expected <= row_ids(after):
                # Even zero matches might be delayed visibility, not proof that
                # the PATCH did not write. Never authorize retry from absence.
                raise RemoteError('Notion append readback is incomplete', uncertain=True)
        except (AccountMismatch, StopRequested, TimeBudgetExceeded) as error:
            error.uncertain = True
            self._append_shape(result)
            raise
        except Exception:
            self._append_shape(result)
            raise RemoteError('Notion append readback did not confirm all expected events', uncertain=True) from None

    def _append_shape(self, result):
        """Log only fixed schema names and counts; never remote keys or values."""
        if self.operational is None:
            return
        allowed = frozenset(('object', 'id', 'type', 'table_row', 'in_trash', 'archived',
                             'has_children', 'created_time', 'last_edited_time',
                             'created_by', 'last_edited_by', 'parent'))
        result = result if isinstance(result, dict) else {}
        batch = result.get('results')
        first = batch[0] if isinstance(batch, list) and batch else None
        keys = set(first) if isinstance(first, dict) else set()
        more = result.get('has_more')
        shape = {'event': 'notion_append_response_shape',
                 'results_len': len(batch) if isinstance(batch, list) else 'missing_or_invalid',
                 'has_more': more if isinstance(more, bool) else ('missing' if 'has_more' not in result else 'invalid'),
                 'first_known_keys': sorted(key for key in keys if key in allowed),
                 'first_unknown_key_count': len(keys - allowed),
                 'first_has_table_row': isinstance(first, dict) and 'table_row' in first}
        try:
            self.operational(json.dumps(shape, ensure_ascii=True, sort_keys=True))
        except Exception:
            # Logging failure cannot turn an uncertain write into a retryable one.
            pass
