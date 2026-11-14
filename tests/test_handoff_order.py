"""Page reordering must not make already active equivalent work disappear."""
import copy
import unittest

import test_handoff_guards as guards
from riri.handoff_guard import older_active_duplicate
from riri.parse import parse_requests


class HandoffOrder(unittest.TestCase):
    setUp = guards.HandoffGuards.setUp
    tearDown = guards.HandoffGuards.tearDown
    add_request = guards.HandoffGuards.add_request
    run_once = guards.HandoffGuards.run_once
    state = guards.HandoffGuards.state

    def move_first(self, request):
        page = self.notion.blocks[self.cfg.requests_page]
        page.remove(request); page.insert(0, request)

    def test_moved_existing_posted_work_still_blocks_new_duplicate(self):
        older = self.add_request(1); self.run_once(live=True)
        self.add_request(2); self.move_first(older)
        self.run_once(live=True)
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['status'], 'held')
        self.assertEqual(self.state(True)['requests']['💛20990101-2']['duplicate_of'], '💛20990101-1')

    def test_existing_prepared_snapshot_is_prior_to_a_new_request_despite_page_order(self):
        older = self.add_request(1)
        newer = self.add_request(2); self.move_first(older)
        requests = parse_requests(self.notion.blocks[self.cfg.requests_page])
        original, req = requests
        state = {'requests': {original.id: {'status': 'prepared', 'snapshot': copy.deepcopy(original.__dict__),
                                           'first_seen': '2026-11-14T00:00:00+00:00'}}, 'outbox': {}}
        self.assertEqual(older_active_duplicate(req, requests, state)[0], original.id)

    def test_moved_posted_work_blocks_existing_unsent_item_at_final_boundary(self):
        older = self.add_request(1); self.run_once(live=True)
        self.add_request(2); self.move_first(older)
        requests = parse_requests(self.notion.blocks[self.cfg.requests_page])
        original, req = requests
        state = self.state(True)
        state['requests'][req.id] = {'status': 'posting', 'snapshot': copy.deepcopy(req.__dict__),
                                     'first_seen': state['requests'][original.id]['first_seen']}
        self.assertEqual(older_active_duplicate(req, requests, state)[0], original.id)


if __name__ == '__main__':
    unittest.main()
