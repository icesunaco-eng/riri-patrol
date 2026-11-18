"""The official member roster (config/members.json): names, IDs and heart colours.

A request ID's heart is the colour of the member who made the request, so each
heart belongs to exactly one member. Runtime code does not read this file yet;
tests/test_members.py keeps riri's own lists equal to it and
scripts/check_members.py reports where the other programs differ.
"""
import json
from pathlib import Path

ROSTER = Path(__file__).resolve().parents[1] / 'config' / 'members.json'


def load(path=ROSTER):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not isinstance(data.get('members'), list):
        raise ValueError('unsupported member roster')
    return data['members']


def hearts(members):
    """Heart -> member name for every member who has a colour."""
    return {m['heart']: m['name'] for m in members if m.get('heart')}


def names(members, kind=None):
    return {m['name'] for m in members if kind is None or m['kind'] == kind}


def resolve(members, value):
    """Map a name, ID or alias to the member's name; None when unknown."""
    for m in members:
        if value in (m['name'], m['id'], *m.get('aliases', ())):
            return m['name']
    return None
