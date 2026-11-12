"""Offline log append confirmation from complete readback, not PATCH shape."""
import json
import unittest
from unittest.mock import patch
from riri.errors import AccountMismatch, RemoteError, SafetyError, StopRequested, TimeBudgetExceeded
from riri.notion import NotionClient, LOG_COLUMNS
from tests.test_api import Fake, page, row, append_response

HEADER = row('header', LOG_COLUMNS)
A = ['value'] * 8 + ['#e-synthetic-a']
B = ['value'] * 8 + ['#e-synthetic-b']
RA, RB = row('row-a', A), row('row-b', B)


class NotionAppendReadbackTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def client(self, *responses, operational=None):
        fake = Fake(*responses)
        client = NotionClient('synthetic-notion-token', fake, operational=operational)
        client._tables['table'] = 'header'
        return client, fake

    def test_partial_block_response_confirmed_by_full_read(self):
        c, f = self.client(page([HEADER]), {'results': [{'object': 'block', 'id': 'partial'}]}, page([HEADER, RA]))
        c.append_rows('table', 'header', [A])
        self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH', 'GET'])

    def test_response_length_has_more_and_missing_fields_do_not_decide_success(self):
        for result in ({}, {'results': [], 'has_more': True}, {'results': [None] * 4},
                       {'results': [{'object': 'block'}], 'has_more': 'unknown'},
                       append_response([B]), {'object': 'error'}):
            with self.subTest(result=result):
                c, f = self.client(page([HEADER]), result, page([HEADER, RA]))
                c.append_rows('table', 'header', [A])
                self.assertEqual(sum(x[0] == 'PATCH' for x in f.calls), 1)

    def test_readback_pagination_must_complete_before_success(self):
        c, f = self.client(page([HEADER]), {}, page([HEADER, RA], True, 'next'), page([RB]))
        c.append_rows('table', 'header', [A, B])
        self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH', 'GET', 'GET'])
        self.assertIn('start_cursor=next', f.calls[-1][1])

    def test_partial_event_matches_stay_uncertain(self):
        c, f = self.client(page([HEADER]), append_response([A, B]), page([HEADER, RA]))
        with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A, B])
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(sum(x[0] == 'PATCH' for x in f.calls), 1)

    def test_zero_matches_are_not_proof_of_no_write(self):
        c, _ = self.client(page([HEADER]), {}, page([HEADER]))
        with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A])
        self.assertTrue(caught.exception.uncertain)

    def test_matching_rows_in_incomplete_pagination_do_not_confirm(self):
        c, _ = self.client(page([HEADER]), {}, page([HEADER, RA], True))
        with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A])
        self.assertTrue(caught.exception.uncertain)

    def test_read_error_or_malformed_table_or_changed_header_stays_uncertain(self):
        for after in (TimeoutError('private transport'), page([RA]),
                      page([row('other-header', LOG_COLUMNS), RA]),
                      page([row('header', ['wrong'] * 9), RA]),
                      page([HEADER, {'type': 'paragraph'}]), page([])):
            with self.subTest(after=type(after).__name__):
                c, _ = self.client(page([HEADER]), {}, after)
                with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A])
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn('private', str(caught.exception))

    def test_uncertain_patch_timeout_resolves_only_by_complete_readback(self):
        for error in (RemoteError('unknown', uncertain=True), TimeoutError('private-timeout')):
            c, f = self.client(page([HEADER]), error, page([HEADER, RA]))
            c.append_rows('table', 'header', [A])
            self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH', 'GET'])

    def test_explicit_prewrite_rejection_never_reads_again(self):
        error = RemoteError('HTTP rejected', uncertain=False)
        c, f = self.client(page([HEADER]), error)
        with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A])
        self.assertIs(caught.exception, error)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH'])

    def test_prepatch_stop_deadline_no_followup_read(self):
        for error in (StopRequested('STOP'), TimeBudgetExceeded('deadline')):
            c, f = self.client(page([HEADER]), error)
            with self.assertRaises(type(error)) as caught: c.append_rows('table', 'header', [A])
            self.assertIs(caught.exception, error)
            self.assertFalse(getattr(error, 'uncertain', False))
            self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH'])

    def test_postpatch_stop_deadline_preserve_uncertainty_and_control_flow(self):
        for error in (StopRequested('STOP'), TimeBudgetExceeded('deadline')):
            c, f = self.client(page([HEADER]), {}, error)
            with self.assertRaises(type(error)) as caught: c.append_rows('table', 'header', [A])
            self.assertIs(caught.exception, error)
            self.assertTrue(error.uncertain)
            self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH', 'GET'])

    def test_account_mismatch_at_patch_propagates_without_followup_read(self):
        error = AccountMismatch('account mismatch')
        c, f = self.client(page([HEADER]), error)
        with self.assertRaises(AccountMismatch) as caught:
            c.append_rows('table', 'header', [A])
        self.assertIs(caught.exception, error)
        self.assertFalse(getattr(error, 'uncertain', False))
        self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH'])

    def test_account_mismatch_after_patch_propagates_with_uncertainty(self):
        error = AccountMismatch('account mismatch')
        logs = []
        c, f = self.client(page([HEADER]), {}, error, operational=logs.append)
        with self.assertRaises(AccountMismatch) as caught:
            c.append_rows('table', 'header', [A])
        self.assertIs(caught.exception, error)
        self.assertTrue(error.uncertain)
        self.assertEqual([x[0] for x in f.calls], ['GET', 'PATCH', 'GET'])
        self.assertEqual(len(logs), 1)

    def test_preexisting_event_rows_are_not_duplicated(self):
        c, f = self.client(page([HEADER, RA]), {}, page([HEADER, RB, RA]))
        c.append_rows('table', 'header', [A, B])
        children = f.calls[1][2]['payload']['children']
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['table_row']['cells'][8][0]['text']['content'], B[8])

    def test_all_preexisting_events_return_without_patch(self):
        c, f = self.client(page([HEADER, RA, RB]))
        c.append_rows('table', 'header', [A, B])
        self.assertEqual([x[0] for x in f.calls], ['GET'])

    def test_expected_event_marker_missing_duplicate_or_ambiguous_rejected_prewrite(self):
        for rows in ([['value'] * 9], [A, A], [['value'] * 8 + ['#e']],
                     [['value'] * 8 + ['#e-a #e-b']], [['value'] * 8 + ['#e-a #e-a']]):
            with self.subTest(rows=rows):
                c, f = self.client(page([HEADER]))
                with self.assertRaises(SafetyError): c.append_rows('table', 'header', rows)
                self.assertEqual([x[0] for x in f.calls], ['GET'])

    def test_header_change_before_patch_known_not_written(self):
        c, f = self.client(page([row('wrong', LOG_COLUMNS)]))
        with self.assertRaises(SafetyError) as caught: c.append_rows('table', 'header', [A])
        self.assertFalse(getattr(caught.exception, 'uncertain', False))
        self.assertEqual([x[0] for x in f.calls], ['GET'])

    def test_diagnostics_contain_only_whitelisted_shape_not_untrusted_keys_or_values(self):
        logs = []
        result = {'results': [{'object': 'PRIVATE-OBJECT', 'id': 'PRIVATE-ID',
                              'table_row': {'private': 'PRIVATE-CONTENT'},
                              'PRIVATE-KEY': 'synthetic-notion-token'}],
                  'has_more': 'PRIVATE-MORE'}
        c, _ = self.client(page([HEADER]), result, page([HEADER]), operational=logs.append)
        with self.assertRaises(RemoteError): c.append_rows('table', 'header', [A])
        self.assertEqual(len(logs), 1)
        self.assertNotIn('PRIVATE', logs[0])
        self.assertNotIn('synthetic-notion-token', logs[0])
        data = json.loads(logs[0])
        self.assertEqual(data['results_len'], 1)
        self.assertEqual(data['has_more'], 'invalid')
        self.assertEqual(data['first_known_keys'], ['id', 'object', 'table_row'])
        self.assertEqual(data['first_unknown_key_count'], 1)
        self.assertTrue(data['first_has_table_row'])

    def test_diagnostics_has_more_only_boolean_or_fixed_label(self):
        for result, expected in (({'has_more': True}, True), ({'has_more': False}, False),
                                 ({}, 'missing'), ({'has_more': 1}, 'invalid')):
            logs = []
            c, _ = self.client(page([HEADER]), result, page([HEADER]), operational=logs.append)
            with self.assertRaises(RemoteError): c.append_rows('table', 'header', [A])
            self.assertEqual(json.loads(logs[0])['has_more'], expected)

    def test_diagnostic_callback_failure_does_not_authorize_retry(self):
        def broken(_): raise OSError('private diagnostic error')
        c, _ = self.client(page([HEADER]), {}, page([HEADER]), operational=broken)
        with self.assertRaises(RemoteError) as caught: c.append_rows('table', 'header', [A])
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('private', str(caught.exception))

    def test_dry_run_checks_marker_but_never_patches(self):
        c, f = self.client(page([HEADER]))
        c.append_rows('table', 'header', [A], dry_run=True)
        self.assertEqual([x[0] for x in f.calls], ['GET'])


if __name__ == '__main__': unittest.main()
