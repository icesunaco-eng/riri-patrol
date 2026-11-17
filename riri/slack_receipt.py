"""Full receipt comparison across Slack's passive server representations.

Only returned emoji name/Unicode pairs supply alias equivalence. No substring,
Unicode normalization, whitespace folding, or ignored unknown elements can turn
a changed body into a receipt. Identity/app/ts checks belong to SlackClient.
"""
import re
import unicodedata

from .slack_payload import visible_text

_EMOJI_NAME = re.compile(r'[a-zA-Z0-9_+\-]{1,80}')
_HEX_SEQUENCE = re.compile(r'[0-9a-fA-F]{4,6}(?:-[0-9a-fA-F]{4,6}){0,31}')
_TEXT_TOKEN = re.compile(r':[a-zA-Z0-9_+\-]{1,80}:|<https?://[^<>\r\n]+>')


def _emoji_base(codepoint):
    # Emoji-capable symbols, not arbitrary letters, controls or combining marks.
    return (codepoint not in range(0x1F3FB, 0x1F400)
        and codepoint not in range(0x1F1E6, 0x1F200)
        and (0x1F000 <= codepoint <= 0x1FAFF or 0x2190 <= codepoint <= 0x27BF
             or codepoint in {0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139})
        and unicodedata.category(chr(codepoint)) in {'So', 'Sk'})


def _emoji_unicode(value):
    """Decode a bounded emoji scalar/ZWJ sequence without changing its scalars."""
    if not isinstance(value, str) or not _HEX_SEQUENCE.fullmatch(value):
        return None
    points = [int(part, 16) for part in value.split('-')]
    if any(cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF for cp in points):
        return None
    # Flags and keycaps have special grammars. Ordinary emoji components below
    # cannot silently smuggle ASCII, selectors, joins or regional indicators.
    if len(points) == 2 and all(0x1F1E6 <= cp <= 0x1F1FF for cp in points):
        return ''.join(map(chr, points))
    if (points[0] in {*range(0x30, 0x3A), 0x23, 0x2A}
            and (points[1:] == [0x20E3] or points[1:] == [0xFE0F, 0x20E3])):
        return ''.join(map(chr, points))
    segments = [[]]
    for cp in points:
        if cp == 0x200D:
            segments.append([])
        else:
            segments[-1].append(cp)
    for segment in segments:
        if not segment or not _emoji_base(segment[0]):
            return None
        suffix = segment[1:]
        selector = suffix[:1] in ([0xFE0F], [0xFE0E])
        if selector:
            suffix = suffix[1:]
        if suffix and (len(suffix) != 1 or not 0x1F3FB <= suffix[0] <= 0x1F3FF):
            return None
    return ''.join(map(chr, points))


def _literal_link(element):
    if set(element) - {'type', 'url', 'text'}:
        return None
    url = element.get('url')
    if (not isinstance(url, str) or not re.fullmatch(r'https?://[^\s<>|]+', url)
            or ('text' in element and element['text'] != url)):
        return None
    return url


def _read_passive(blocks, permitted_user):
    """Return exact visible text and validated aliases, or reject whole blocks."""
    if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
            or blocks[0].get('type') != 'rich_text'
            or set(blocks[0]) - {'type', 'elements', 'block_id'}):
        return None
    if 'block_id' in blocks[0] and not isinstance(blocks[0]['block_id'], str):
        return None
    sections = blocks[0].get('elements')
    if (not isinstance(sections, list) or len(sections) != 1 or not isinstance(sections[0], dict)
            or sections[0].get('type') != 'rich_text_section'
            or set(sections[0]) != {'type', 'elements'}):
        return None
    elements = sections[0].get('elements')
    if not isinstance(elements, list) or not elements:
        return None
    if permitted_user and elements[0] != {'type': 'user', 'user_id': permitted_user}:
        return None
    output, aliases = [], {}
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            return None
        kind = element.get('type')
        if (kind == 'user' and index == 0 and permitted_user
                and element == {'type': 'user', 'user_id': permitted_user}):
            output.append('<@' + permitted_user + '>')
        elif kind == 'text' and set(element) == {'type', 'text'} and isinstance(element['text'], str):
            output.append(element['text'])
        elif kind == 'emoji' and set(element) == {'type', 'name', 'unicode'}:
            name = element['name']
            glyph = _emoji_unicode(element['unicode'])
            if (not isinstance(name, str) or not _EMOJI_NAME.fullmatch(name) or glyph is None
                    or (name in aliases and aliases[name] != glyph)):
                return None
            aliases[name] = glyph
            output.append(glyph)
        elif kind == 'link':
            url = _literal_link(element)
            if url is None:
                return None
            output.append(url)
        else:
            return None
    return ''.join(output), aliases


def _text_matches(actual, expected, aliases):
    """Compare full strings with contextual alternatives, preserving literal aliases.

    For example an expected literal ':rice:' followed by a real rice emoji is
    distinguishable through the full rich body. Globally replacing every alias
    would corrupt the literal occurrence. Each token here may match its literal
    spelling or the one returned Unicode spelling, at its exact expected offset.
    """
    if actual == expected:
        return True
    offsets, start = {0}, 0
    for match in _TEXT_TOKEN.finditer(actual):
        literal = actual[start:match.start()]
        offsets = {pos + len(literal) for pos in offsets if expected.startswith(literal, pos)}
        if not offsets:
            return False
        token = match.group()
        alternatives = {token}
        if token.startswith(':'):
            glyph = aliases.get(token[1:-1])
            if glyph is not None:
                alternatives.add(glyph)
        else:
            target, separator, label = token[1:-1].partition('|')
            if not separator or label == target:
                alternatives.add(target)
        offsets = {pos + len(value) for pos in offsets for value in alternatives
                   if expected.startswith(value, pos)}
        if not offsets:
            return False
        start = match.end()
    tail = actual[start:]
    return any(expected[pos:] == tail for pos in offsets)


def message_content_matches(message, expected_wire, permitted_user, structured=False):
    """Prove complete content and approved mention across available representations."""
    if (not isinstance(message, dict) or not isinstance(expected_wire, str) or not expected_wire
            or not isinstance(permitted_user, str)
            or (permitted_user and (not re.fullmatch(r'U[A-Z0-9]+', permitted_user)
                or not expected_wire.startswith('<@' + permitted_user + '>\n')))):
        return False
    expected_visible = visible_text(expected_wire)
    remaining = expected_visible[len('<@' + permitted_user + '>\n'):] if permitted_user else expected_visible
    if re.search(r'<\s*[@!#]', remaining):
        return False
    aliases = {}
    has_blocks = 'blocks' in message
    if structured or has_blocks:
        parsed = _read_passive(message.get('blocks'), permitted_user)
        if parsed is None or parsed[0] != expected_visible:
            return False
        aliases = parsed[1]
    if 'text' not in message:
        return has_blocks
    text = message['text']
    if not isinstance(text, str):
        return False
    if not text:
        return has_blocks  # Complete blocks prove an intentionally empty fallback.
    if permitted_user and not text.startswith('<@' + permitted_user + '>\n'):
        return False  # Escaped syntax is literal text, not an official mention.
    if not has_blocks:
        # Without independent full rich content there is no evidence that an
        # entity spelling or alias is a harmless server representation change.
        return text == expected_wire
    return any(_text_matches(text, expected, aliases)
        for expected in {expected_wire, expected_visible})
