"""One Slack message with lossless literal text, never multiple API sends."""


def needs_blocks(text):
    # Leave space for existing report provenance and use UTF-16 conservatively.
    return len(text.encode('utf-16-le')) // 2 > 3500


def visible_text(text):
    # Decode exactly one layer of the three entities emitted by compose._value.
    # html.unescape would also change unrelated literal entities in URLs/text.
    return text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def rich_blocks(text, permitted_user):
    prefix = f'<@{permitted_user}>' if permitted_user else ''
    elements = []
    if prefix and text.startswith(prefix + '\n'):
        elements.append({'type': 'user', 'user_id': permitted_user})
        text = text[len(prefix):]
    text = visible_text(text)
    # Multiple literal elements remain adjacent inside ONE section/message.
    elements.extend({'type': 'text', 'text': text[i:i + 3000]}
                    for i in range(0, len(text), 3000))
    return [{'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': elements}]}]


def read_rich_blocks(blocks, permitted_user):
    """Accept only the exact passive shape we send, plus a server block ID."""
    try:
        if (not isinstance(blocks, list) or len(blocks) != 1 or
                blocks[0]['type'] != 'rich_text' or
                set(blocks[0]) - {'type', 'elements', 'block_id'}):
            return None
        sections = blocks[0]['elements']
        if len(sections) != 1 or sections[0]['type'] != 'rich_text_section' or set(sections[0]) != {'type', 'elements'}:
            return None
        result = []
        for i, element in enumerate(sections[0]['elements']):
            if (i == 0 and permitted_user and element == {'type': 'user', 'user_id': permitted_user}):
                result.append(f'<@{permitted_user}>')
            elif set(element) == {'type', 'text'} and element['type'] == 'text' and isinstance(element['text'], str):
                result.append(element['text'])
            else:
                return None
        return ''.join(result)
    except (KeyError, TypeError, IndexError):
        return None


def read_known_rich_blocks(blocks, permitted_users):
    """Passive history classification for configured mentions, without authority.

    Delivery receipts still use read_rich_blocks with their one persisted target.
    This only ensures automated owner reports cannot enter human wake routing.
    """
    for user_id in ('', *permitted_users):
        if not isinstance(user_id, str):
            continue
        text = read_rich_blocks(blocks, user_id)
        if text is not None:
            return text
    return None
