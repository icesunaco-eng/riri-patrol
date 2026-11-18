"""riri's own member lists must match config/members.json."""
import re
import unittest

from riri import members as roster
from riri.local_tasks import ACTORS
from riri.models import RECIPIENTS, REPORT_PAGES
from riri.parse import ID_PATTERN
from riri.report_delivery import REQUESTER_ALIASES

MEMBERS = roster.load()


def pattern_hearts(pattern):
    return set(re.search(r'\(\?:([^)]*)\)', pattern).group(1).split('|'))


class RosterShapeTest(unittest.TestCase):
    def test_ids_names_and_aliases_are_unique(self):
        keys = [k for m in MEMBERS for k in (m['id'], m['name'], *m['aliases'])]
        self.assertEqual(len(keys), len(set(keys)), 'an ID, name or alias is used twice')
        for m in MEMBERS:
            self.assertRegex(m['id'], r'^[a-z][a-z0-9_]*$')
            self.assertIn(m['kind'], ('agent', 'human'))

    def test_each_heart_belongs_to_one_member(self):
        coloured = [m['heart'] for m in MEMBERS if m['heart']]
        duplicates = sorted({h for h in coloured if coloured.count(h) > 1})
        self.assertEqual(duplicates, [], 'a heart colour is shared by two members')

    def test_every_agent_has_a_heart(self):
        self.assertEqual([m['name'] for m in MEMBERS if m['kind'] == 'agent' and not m['heart']], [])

    def test_resolve_accepts_names_ids_and_aliases(self):
        self.assertEqual(roster.resolve(MEMBERS, 'aisu'), 'あいす')
        self.assertEqual(roster.resolve(MEMBERS, 'ice'), 'あいす')
        self.assertEqual(roster.resolve(MEMBERS, 'みに（Codex）'), 'みに')
        self.assertIsNone(roster.resolve(MEMBERS, 'だれか'))


class RiriMatchesRosterTest(unittest.TestCase):
    def test_request_id_hearts_are_the_roster_hearts(self):
        self.assertEqual(pattern_hearts(ID_PATTERN), set(roster.hearts(MEMBERS)))

    def test_recipients_are_the_agents(self):
        self.assertEqual(set(RECIPIENTS), roster.names(MEMBERS, 'agent'))

    def test_report_pages_match(self):
        listed = {m['name']: m['report_page'] for m in MEMBERS if m.get('report_page')}
        self.assertEqual(REPORT_PAGES, listed)

    def test_requester_aliases_point_to_the_right_member(self):
        for alias, name in REQUESTER_ALIASES.items():
            self.assertEqual(roster.resolve(MEMBERS, alias), name, alias)

    def test_local_task_ids_use_the_requesters_heart(self):
        hearts = roster.hearts(MEMBERS)
        for actor, (requester, worker, heart) in ACTORS.items():
            self.assertEqual(hearts[heart], requester, actor)
            self.assertEqual(roster.resolve(MEMBERS, actor), worker)


if __name__ == '__main__':
    unittest.main()
