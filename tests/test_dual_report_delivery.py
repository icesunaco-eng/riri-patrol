"""Offline ownership binding and two-destination report acknowledgement."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import unittest
from unittest.mock import patch

from riri.errors import SafetyError
from riri.models import Report
from riri.parse import parse_requests, replace_status
from riri.report_delivery import (acknowledge_report_bundle, bundle_id,
                                  report_bundle_status, report_refs_pending,
                                  resolve_requester)
from riri.state import initial_state


RID = '💛20261117-1'


def request(requester='みに', recipients='あいす', block='request-block'):
    text = ('【りりへの依頼】\n依頼ID：' + RID + '\n依頼した子：' + requester +
            '\n宛先：@' + recipients + '\n状態：対応中\nやること：確認する\n期限・実行タイミング：いますぐ')
    return parse_requests([{'id': block, 'type': 'code',
                            'code': {'rich_text': [{'text': {'content': text}}]}}])[0]


def report(block='report-block', text='【完了報告】\n結果：完了', reporter='あいす'):
    return Report(RID, reporter, '完了', '確認した', block, text,
                  hashlib.sha256(text.encode()).hexdigest(),
                  work_url='codex://threads/synthetic-thread', work_url_status='valid')


def accepted(req=None):
    req = req or request()
    state = initial_state('practice')
    state['requests'][RID] = {'snapshot': asdict(req), 'block_id': req.block_id,
                              'recipients': req.recipients, 'status': 'posted'}
    return state


def delivery_state(reports=None, requester='みに'):
    reports = reports or [report()]
    state = initial_state('practice')
    ident = bundle_id(reports, requester)
    records = [asdict(r) for r in reports]
    keys = ['report-first:' + ident, 'report-second:' + ident]
    policy = {'version': 'dual-v2', 'purpose': 'requester_report',
              'requester': requester, 'bundle_id': ident,
              'canonical_hash': hashlib.sha256(b'canonical-body').hexdigest(),
              'mention_mode': 'text', 'mention_name': requester, 'mention_user_id': ''}
    state['report_deliveries'] = {ident: {
        'policy_version': 'dual-v2', 'requester': requester, 'reports': records,
        'keys': keys, 'status': 'pending', 'created_at': '2026-11-17T00:00:00+00:00'}}
    for key, channel in zip(keys, ('C0CFM2HRA5V', 'C0CFL6A8QJH')):
        state['outbox'][key] = {'kind': 'reports', 'channel': channel,
                                'status': 'queued', 'report_bundle_id': ident,
                                'delivery_policy': deepcopy(policy), 'reports': deepcopy(records)}
    return state, ident, keys


class RequesterBindingTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_original_requester_is_neither_reporter_nor_transport_sender(self):
        req = request('うさ')
        state = accepted(req)
        state['requests'][RID]['transport_sender'] = 'たかむね'
        self.assertEqual(resolve_requester(report(), [req], state), 'うさ')

    def test_accepted_snapshot_survives_absent_source(self):
        self.assertEqual(resolve_requester(report(), [], accepted()), 'みに')

    def test_unique_current_original_is_fallback_without_acceptance(self):
        self.assertEqual(resolve_requester(report(), [request('のあ')], initial_state('practice')), 'のあ')

    def test_only_explicit_approved_alias_is_canonicalized(self):
        req = request('みに（Codex）')
        self.assertEqual(resolve_requester(report(), [req], accepted(req)), 'みに')
        for name in ('🩵', 'みにちゃん', '@みに', 'みに (Codex)', '', 'たかむね'):
            with self.subTest(name=name), self.assertRaises(SafetyError):
                resolve_requester(report(), [request(name)], initial_state('practice'))

    def test_unknown_or_missing_original_is_not_inferred_from_heart(self):
        with self.assertRaises(SafetyError):
            resolve_requester(report(), [], initial_state('practice'))

    def test_duplicate_current_id_is_rejected_even_with_accepted_snapshot(self):
        for state in (initial_state('practice'), accepted()):
            with self.subTest(accepted=bool(state['requests'])), self.assertRaises(SafetyError):
                resolve_requester(report(), [request(), request(block='other-block')], state)

    def test_acceptance_record_without_snapshot_does_not_fall_back(self):
        state = initial_state('practice')
        state['requests'][RID] = {'status': 'posted'}
        with self.assertRaises(SafetyError):
            resolve_requester(report(), [request()], state)

    def test_reporter_must_be_original_recipient(self):
        with self.assertRaises(SafetyError):
            resolve_requester(report(reporter='かれん'), [request()], accepted())

    def test_status_change_does_not_change_requester_binding(self):
        req = request()
        state = accepted(req)
        req.text = replace_status(req.text, '完了')
        req.status = '完了'
        req.fields['状態'] = '完了'
        self.assertEqual(resolve_requester(report(), [req], state), 'みに')

    def test_replaced_block_or_edited_original_cannot_reassign_requester(self):
        for changed in (request('うさ'), request(block='replacement-block')):
            with self.subTest(block=changed.block_id), self.assertRaises(SafetyError):
                resolve_requester(report(), [changed], accepted())

    def test_snapshot_structure_cannot_override_requester_in_source_text(self):
        state = accepted()
        state['requests'][RID]['snapshot']['requester'] = 'うさ'
        with self.assertRaises(SafetyError):
            resolve_requester(report(), [], state)

    def test_snapshot_block_metadata_and_accepted_recipient_disagreement_rejected(self):
        for field, value in (('block_id', 'other'), ('recipients', ['うさ'])):
            state = accepted()
            state['requests'][RID][field] = value
            with self.subTest(field=field), self.assertRaises(SafetyError):
                resolve_requester(report(), [], state)


class ReportBundleTests(unittest.TestCase):
    def test_identity_is_order_independent_but_requester_and_revision_specific(self):
        a, b = report(), report(block='report-second')
        self.assertEqual(bundle_id([a, b], 'みに'), bundle_id([b, a], 'みに'))
        self.assertNotEqual(bundle_id([a], 'みに'), bundle_id([a], 'うさ'))
        changed = report(text='訂正報告')
        self.assertNotEqual(bundle_id([a], 'みに'), bundle_id([changed], 'みに'))
        self.assertNotEqual(bundle_id([a], 'みに'), bundle_id([a], 'みに', 'future-version'))

    def test_duplicate_reference_and_multiple_versions_same_block_rejected(self):
        for reports in ([report(), report()], [report(), report(text='訂正')]):
            with self.assertRaises(SafetyError):
                bundle_id(reports, 'みに')

    def test_first_receipt_does_not_consume_seen_or_report_event(self):
        state, ident, keys = delivery_state()
        self.assertEqual(report_bundle_status(state, ident), 'pending')
        state['outbox'][keys[0]].update(status='posted', ts='1794912000.000001')
        self.assertEqual(report_bundle_status(state, ident), 'partial')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])
        self.assertEqual(state['seen_reports'], {})
        self.assertEqual(state['seen_report_versions'], {})

    def test_both_receipts_consume_once_and_preserve_full_report(self):
        state, ident, keys = delivery_state()
        for number, key in enumerate(keys):
            state['outbox'][key].update(status='posted', ts='1794912000.00000' + str(number))
        finalized = acknowledge_report_bundle(state, ident)
        self.assertEqual(finalized, [asdict(report())])
        self.assertEqual(state['seen_reports'], {report().block_id: report().digest})
        self.assertEqual(state['seen_report_versions'], {report().block_id: [report().digest]})
        self.assertEqual(state['report_deliveries'][ident]['status'], 'posted')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])

    def test_manual_posted_without_real_timestamp_is_not_delivery_evidence(self):
        state, ident, keys = delivery_state()
        state['outbox'][keys[0]].update(status='posted', ts='1794912000.000001')
        state['outbox'][keys[1]].update(status='posted', manually_confirmed=True)
        self.assertEqual(report_bundle_status(state, ident), 'held')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])
        self.assertFalse(state['seen_reports'])

    def test_exact_practice_receipts_finalize_only_in_isolated_practice_state(self):
        state, ident, keys = delivery_state()
        for key in keys:
            state['outbox'][key].update(status='posted', ts='practice-' + key)
        self.assertEqual(report_bundle_status(state, ident), 'posted')
        self.assertEqual(acknowledge_report_bundle(state, ident), [asdict(report())])

    def test_practice_receipts_cannot_confirm_live_delivery(self):
        state, ident, keys = delivery_state()
        state['mode'] = 'live'
        for key in keys:
            state['outbox'][key].update(status='posted', ts='practice-' + key)
        self.assertEqual(report_bundle_status(state, ident), 'held')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])
        self.assertFalse(state['seen_reports'])

    def test_practice_receipt_from_other_leg_is_not_accepted(self):
        state, ident, keys = delivery_state()
        for key in keys:
            state['outbox'][key].update(status='posted', ts='practice-' + keys[0])
        self.assertEqual(report_bundle_status(state, ident), 'held')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])

    def test_cancelled_and_unknown_side_never_consume_seen(self):
        for status, expected in (('held', 'held'), ('blocked', 'held'), ('cancelled', 'cancelled'),
                                  ('sending', 'partial')):
            state, ident, keys = delivery_state()
            state['outbox'][keys[0]].update(status='posted', ts='1794912000.000001')
            state['outbox'][keys[1]]['status'] = status
            self.assertEqual(report_bundle_status(state, ident), expected)
            self.assertEqual(acknowledge_report_bundle(state, ident), [])
            self.assertFalse(state['seen_reports'])

    def test_restart_preserves_partial_cancelled_and_finished_reservations(self):
        for status in ('queued', 'sending', 'held', 'cancelled', 'posted'):
            state, ident, keys = delivery_state()
            for key in keys:
                state['outbox'][key]['status'] = status
                if status == 'posted':
                    state['outbox'][key]['ts'] = '1794912000.000001'
            restored = json.loads(json.dumps(state))
            self.assertEqual(report_refs_pending(restored), {(report().block_id, report().digest)})
            if status == 'cancelled':
                self.assertEqual(acknowledge_report_bundle(restored, ident), [])
                self.assertFalse(restored['seen_reports'])

    def test_legacy_not_posted_reports_keep_existing_reservations(self):
        state = initial_state('practice')
        state['outbox']['legacy'] = {'kind': 'reports', 'status': 'cancelled', 'reports': [asdict(report())]}
        self.assertEqual(report_refs_pending(state), {(report().block_id, report().digest)})
        state['outbox']['legacy']['status'] = 'posted'
        self.assertEqual(report_refs_pending(state), set())

    def test_malformed_cross_bundle_receipts_are_rejected_without_seen_mutation(self):
        modifications = [
            lambda state, ident, keys: state['outbox'][keys[1]].update(channel='C0CFM2HRA5V'),
            lambda state, ident, keys: state['outbox'][keys[1]].update(report_bundle_id='unrelated'),
            lambda state, ident, keys: state['outbox'][keys[1]]['delivery_policy'].update(requester='うさ'),
            lambda state, ident, keys: state['outbox'][keys[1]]['delivery_policy'].update(canonical_hash='f' * 64),
            lambda state, ident, keys: state['outbox'][keys[1]]['reports'][0].update(digest='other'),
            lambda state, ident, keys: state['outbox'][keys[1]]['reports'][0].update(reporter='うさ'),
            lambda state, ident, keys: state['outbox'][keys[1]]['reports'][0].update(work_url='https://example.invalid/changed'),
            lambda state, ident, keys: state['report_deliveries'][ident].update(keys=[keys[0], keys[0]]),
            lambda state, ident, keys: state['report_deliveries'][ident].update(requester='うさ'),
        ]
        for modify in modifications:
            state, ident, keys = delivery_state()
            for key in keys:
                state['outbox'][key].update(status='posted', ts='1794912000.000001')
            modify(state, ident, keys)
            with self.subTest(modification=modifications.index(modify)), self.assertRaises(SafetyError):
                acknowledge_report_bundle(state, ident)
            self.assertFalse(state['seen_reports'])

    def test_explicit_cancelled_bundle_cannot_be_revived_by_edited_leg_status(self):
        state, ident, keys = delivery_state()
        state['report_deliveries'][ident]['status'] = 'cancelled'
        for key in keys:
            state['outbox'][key].update(status='posted', ts='1794912000.000001')
        self.assertEqual(acknowledge_report_bundle(state, ident), [])
        self.assertEqual(report_bundle_status(state, ident), 'cancelled')


if __name__ == '__main__':
    unittest.main()
