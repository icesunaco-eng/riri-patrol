"""scripts/archive_plan.py only lists old, finished, untouched requests as movable."""
from datetime import date
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('archive_plan', ROOT / 'scripts' / 'archive_plan.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def request(request_id, status, block_id):
    text = ('【りりへの依頼】\n依頼ID：%s\n依頼した子：うさ\n宛先：@あいす\n状態：%s\n'
            'やること（一文で）：確認する\n期限・実行タイミング（いつ）：今日中\n' % (request_id, status))
    return {'id': block_id, 'type': 'code', 'code': {'rich_text': [{'plain_text': text}]}}


class ArchivePlanTest(unittest.TestCase):
    def setUp(self):
        self.blocks = [
            {'id': 'h', 'type': 'heading_2', 'heading_2': {'rich_text': [{'plain_text': '依頼'}]}},
            request('💛20261020-1', '完了', 'b1'),   # movable
            request('💛20261021-1', '対応中', 'b2'),  # Notion not finished
            request('🩵20261022-1', '完了', 'b3'),   # riri not done
            request('💜20261023-1', '完了', 'b4'),   # a post is still held
            request('🧡20261024-1', '完了', 'b5'),   # not in riri's state
            request('🩷20261025-1', '完了', 'b6'),   # mentioned by a relay record
            request('💛20261101-1', '完了', 'b7'),   # newer than the cutoff
        ]
        done = {'status': 'done'}
        self.state = {'requests': {'💛20261020-1': done, '💛20261021-1': {'status': 'posted'},
                                   '🩵20261022-1': {'status': 'posted'}, '💜20261023-1': done,
                                   '🩷20261025-1': done, '💛20261101-1': done},
                      'outbox': {'report:x': {'request_id': '💜20261023-1', 'status': 'held'},
                                 'instruction:💛20261020-1': {'request_id': '💛20261020-1', 'status': 'posted'}}}

    def test_only_finished_untouched_old_requests_can_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Path(tmp) / 'relay'
            relay.mkdir()
            (relay / 'job.json').write_text('{"related": "🩷20261025-1"}', encoding='utf-8')
            bridge = Path(tmp) / 'mamu-bridge.json'
            bridge.write_text('{"requests": {"🧡20261024-1": {}}}', encoding='utf-8')
            result = archive.plan(self.blocks, self.state, date(2026, 11, 1),
                                  [('まむの連携記録', relay), ('りりのまむ連携記録', bridge)])
        self.assertEqual([r['id'] for r in result['movable']], ['💛20261020-1'])
        reasons = {r['id']: '／'.join(r['reasons']) for r in result['stay']}
        self.assertIn('Notionの状態が「対応中」', reasons['💛20261021-1'])
        self.assertIn('りりの記録が「posted」', reasons['🩵20261022-1'])
        self.assertIn('report:x（held）', reasons['💜20261023-1'])
        self.assertIn('状態ファイルに記録がない', reasons['🧡20261024-1'])
        self.assertIn('りりのまむ連携記録', reasons['🧡20261024-1'])
        self.assertIn('まむの連携記録', reasons['🩷20261025-1'])
        self.assertEqual(result['newer'], 1)
        self.assertEqual(result['blocks_total'], 8)

    def test_month_argument_is_strict(self):
        self.assertEqual(archive.cutoff('2026-11'), date(2026, 11, 1))
        for bad in ('2026-13', '202611', '', None):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                archive.cutoff(bad)


if __name__ == '__main__':
    unittest.main()
