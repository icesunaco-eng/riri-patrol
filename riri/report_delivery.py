"""Bind completion notices to their requester and both durable destinations.

These helpers never send, reread Notion, or manufacture Slack identities. A
report version stays reserved after cancellation, while ``seen`` means that
both destinations have a real, recorded Slack timestamp.
"""
from dataclasses import asdict, is_dataclass
import hashlib
import json
import re

from .errors import RequestIncomplete, SafetyError, StatusMissing
from .models import RECIPIENTS, Request
from .parse import parse_requests, replace_status


POLICY_VERSION = 'dual-v2'
REPORT_CHANNELS = frozenset({'C0CFM2HRA5V', 'C0CFL6A8QJH'})
REQUESTER_ALIASES = {'みに（Codex）': 'みに'}


def _data(value):
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    raise SafetyError('完了報告または依頼の記録形式が不正です')


def _requester(value):
    if not isinstance(value, str):
        raise SafetyError('元の依頼者を正式登録名で確認できません')
    name = REQUESTER_ALIASES.get(value.strip(), value.strip())
    if name not in RECIPIENTS:
        raise SafetyError('元の依頼者を正式登録名で確認できません')
    return name


def _source_request(value, report):
    """Check structured routing fields against the immutable original text."""
    data = _data(value)
    try:
        req = Request(**data)
        if (req.id != report['id'] or not isinstance(req.block_id, str) or
                not req.block_id or not isinstance(req.text, str) or
                not isinstance(req.recipients, list) or not req.recipients or
                len(req.recipients) != len(set(req.recipients)) or
                any(name not in RECIPIENTS for name in req.recipients)):
            raise ValueError()
        normalized = replace_status(req.text, '未着手')
        parsed = parse_requests([{'id': req.block_id, 'type': 'code',
                                  'code': {'rich_text': [{'text': {'content': req.text}}]}}])
        if (len(parsed) != 1 or parsed[0].id != req.id or
                _requester(parsed[0].requester) != _requester(req.requester) or
                parsed[0].recipients != req.recipients):
            raise ValueError()
        if report['reporter'] not in req.recipients:
            raise SafetyError('完了報告した子が元の依頼の宛先に含まれていません')
        return req, normalized
    except SafetyError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RequestIncomplete, StatusMissing):
        raise SafetyError('元の依頼の原本と受付記録を照合できません') from None


def resolve_requester(report, requests, state):
    """Return only the original request's exact registered requester name.

    Accepted snapshots take precedence. A unique current source is a fallback
    only for requests with no acceptance record. Missing or contradictory
    acceptance records must not silently become a new ownership assignment.
    """
    report = _data(report)
    if (not isinstance(report.get('id'), str) or
            report.get('reporter') not in RECIPIENTS or report.get('reporter') == 'りり'):
        raise SafetyError('完了報告の依頼IDまたは報告した子を確認できません')
    matches = [req for req in requests if _data(req).get('id') == report['id']]
    if len(matches) > 1:
        raise SafetyError('同じ依頼IDの原本が複数あり元の依頼者を特定できません')
    accepted = state.get('requests', {})
    if not isinstance(accepted, dict):
        raise SafetyError('受付済み依頼の記録形式が不正です')
    info = accepted.get(report['id'])
    if info is None:
        if not matches:
            raise SafetyError('完了報告に対応する元の依頼が見つかりません')
        req, _ = _source_request(matches[0], report)
        return _requester(req.requester)
    if not isinstance(info, dict) or not isinstance(info.get('snapshot'), dict):
        raise SafetyError('受付済み依頼の原本記録が不足しています')
    saved, saved_text = _source_request(info['snapshot'], report)
    if 'block_id' in info and info['block_id'] != saved.block_id:
        raise SafetyError('受付済み依頼の原本ブロックが一致しません')
    if 'recipients' in info and (not isinstance(info['recipients'], list) or
                                 report['reporter'] not in info['recipients']):
        raise SafetyError('完了報告した子が受付済みの宛先に含まれていません')
    if matches:
        current, current_text = _source_request(matches[0], report)
        if current.block_id != saved.block_id or current_text != saved_text:
            raise SafetyError('受付後に元の依頼本文またはブロックが変更されています')
    return _requester(saved.requester)


def _report_records(reports):
    if not isinstance(reports, (list, tuple)) or not reports:
        raise SafetyError('完了報告の配送束に報告がありません')
    records = [_data(report) for report in reports]
    refs = []
    for record in records:
        if any(not isinstance(record.get(key), str) or not record[key]
               for key in ('block_id', 'digest', 'id', 'reporter')):
            raise SafetyError('完了報告の原本参照が不正です')
        refs.append((record['block_id'], record['digest']))
    if len(refs) != len(set(refs)) or len({ref[0] for ref in refs}) != len(refs):
        raise SafetyError('完了報告の配送束に原本参照の重複があります')
    return records


def bundle_id(reports, requester, policy_version=POLICY_VERSION):
    """A report-order-independent identity for exactly these source versions."""
    records = _report_records(reports)
    requester = _requester(requester)
    if not isinstance(policy_version, str) or not policy_version:
        raise SafetyError('完了報告の配送方針が不正です')
    identity = [policy_version, requester,
                sorted((record['block_id'], record['digest']) for record in records)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def report_refs_pending(state):
    """Reserve all dual-bundle versions, including cancelled/finished bundles.

    Legacy non-posted intents also keep their original versions reserved. This
    does not alter their prior receipts or manufacture a second destination.
    """
    refs = set()
    for item in state.get('outbox', {}).values():
        if item.get('kind') == 'reports' and item.get('status') != 'posted':
            refs.update((record['block_id'], record['digest'])
                        for record in _report_records(item.get('reports')))
    bundles = state.get('report_deliveries', {})
    if not isinstance(bundles, dict):
        raise SafetyError('完了報告の配送束記録が不正です')
    for bundle in bundles.values():
        if not isinstance(bundle, dict):
            raise SafetyError('完了報告の配送束記録が不正です')
        refs.update((record['block_id'], record['digest'])
                    for record in _report_records(bundle.get('reports')))
    return refs


def _bundle_items(state, identifier):
    bundle = state.get('report_deliveries', {}).get(identifier)
    if not isinstance(bundle, dict):
        raise SafetyError('完了報告の配送束が見つかりません')
    records = _report_records(bundle.get('reports'))
    requester = _requester(bundle.get('requester'))
    if (bundle.get('policy_version') != POLICY_VERSION or
            bundle_id(records, requester) != identifier):
        raise SafetyError('完了報告の配送束と原本参照が一致しません')
    keys = bundle.get('keys')
    if (not isinstance(keys, list) or len(keys) != 2 or
            any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != 2):
        raise SafetyError('完了報告の配送先記録は異なる2件が必要です')
    items = [state.get('outbox', {}).get(key) for key in keys]
    if any(not isinstance(item, dict) for item in items):
        raise SafetyError('完了報告の配送先記録が不足しています')
    if {item.get('channel') for item in items} != REPORT_CHANNELS:
        raise SafetyError('完了報告の配送先が指定の2か所と一致しません')
    policy = items[0].get('delivery_policy')
    if (not isinstance(policy, dict) or policy.get('version') != POLICY_VERSION or
            policy.get('purpose') != 'requester_report' or
            policy.get('requester') != requester or policy.get('bundle_id') != identifier or
            not isinstance(policy.get('canonical_hash'), str) or
            not re.fullmatch(r'[0-9a-f]{64}', policy['canonical_hash'])):
        raise SafetyError('完了報告の配送方針と依頼者が一致しません')
    expected_reports = sorted(records, key=lambda record: (record['block_id'], record['digest']))
    for item in items:
        if (item.get('kind') != 'reports' or item.get('report_bundle_id') != identifier or
                item.get('delivery_policy') != policy or
                sorted(_report_records(item.get('reports')),
                       key=lambda record: (record['block_id'], record['digest'])) != expected_reports):
            raise SafetyError('完了報告の配送先記録が別の配送束と混在しています')
        if item.get('status') not in {'queued', 'sending', 'posted', 'held', 'blocked', 'cancelled'}:
            raise SafetyError('完了報告の配送先状態が不正です')
    return bundle, records, items


def _posted(item, mode, key):
    if item.get('status') != 'posted' or not isinstance(item.get('ts'), str):
        return False
    if mode == 'practice' and item['ts'] == 'practice-' + key:
        return True
    return bool(re.fullmatch(r'[0-9]+\.[0-9]+', item['ts']))


def report_bundle_status(state, identifier):
    """Compute aggregate delivery without confusing manual claims with receipts."""
    bundle, _, items = _bundle_items(state, identifier)
    receipts = [_posted(item, state.get('mode'), key)
                for key, item in zip(bundle['keys'], items)]
    if bundle.get('status') == 'cancelled' or any(item['status'] == 'cancelled' for item in items):
        return 'cancelled'
    if any(item['status'] in {'held', 'blocked'} or
           (item['status'] == 'posted' and not posted) for item, posted in zip(items, receipts)):
        return 'held'
    posted = sum(receipts)
    return 'posted' if posted == 2 else 'partial' if posted else 'pending'


def acknowledge_report_bundle(state, identifier):
    """Consume these versions once, only after both real delivery receipts exist."""
    bundle, records, _ = _bundle_items(state, identifier)
    status = report_bundle_status(state, identifier)
    bundle['status'] = status
    if status != 'posted' or bundle.get('notified'):
        return []
    for report in records:
        state.setdefault('seen_reports', {})[report['block_id']] = report['digest']
        versions = state.setdefault('seen_report_versions', {}).setdefault(report['block_id'], [])
        if report['digest'] not in versions:
            versions.append(report['digest'])
    bundle['notified'] = True
    return [dict(report) for report in records]
