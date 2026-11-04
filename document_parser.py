"""Authoritative parser for the symbol-based document input format.

The bot recognises exactly three structural markers:

    @  title      (starts a new document)
    #  subtitle   (section heading)
    *  paragraph  (body text)

A marker is structural only when it is actually used as one:

* ``@`` is a title at the start of a line (leading indentation is ignored).
  Inline ``@`` is left alone, so e-mail addresses and mentions such as
  ``user@example.com`` or ``ask @alice`` never split a document.
* ``#`` and ``*`` are markers at the start of a line, or inline when they
  follow whitespace and are followed by non-whitespace.  This makes the
  compact form ``@Windows #Uninstall *Text`` work while leaving ``C#``,
  URL fragments (``https://example.com/#frag``), multiplication (``2 * 3``)
  and Markdown strong emphasis alone.  A marker followed by a space is also
  accepted where it opens a new block after a title/subtitle, so
  ``@Windows #Uninstall * Text`` works too.
* A backslash escapes a marker (``\\@``); the text is preserved verbatim.
* Inside a paragraph, an apostrophe used as a manual line-break marker is
  converted to a newline: ``...instructions'It performs...`` becomes two
  lines of the same paragraph.  Normal apostrophes (``don't``, ``O'Brien``,
  ``students' work``) are preserved.

Everything between two markers is the block content, so line breaks, URLs,
pipes, commands, backslashes and every other character are preserved.

This module is the only place where document-input parsing happens.  Handlers
persist its output; they never inspect the raw text for markers themselves.
"""

from __future__ import annotations

TITLE = "title"
SUBTITLE = "subtitle"
PARAGRAPH = "paragraph"

#: Marker character -> block type.  These are the only structural markers.
MARKERS = {"@": TITLE, "#": SUBTITLE, "*": PARAGRAPH}

#: Block type -> marker character (used for confirmations and documentation).
MARKER_FOR_TYPE = {block_type: marker for marker, block_type in MARKERS.items()}

BLOCK_TYPES = (TITLE, SUBTITLE, PARAGRAPH)

__all__ = [
    "TITLE",
    "SUBTITLE",
    "PARAGRAPH",
    "MARKERS",
    "MARKER_FOR_TYPE",
    "BLOCK_TYPES",
    "parse_document_blocks",
    "group_documents",
    "parse_documents",
    "expand_paragraph_line_breaks",
]


def _is_escaped(text: str, index: int) -> bool:
    """Return True when the character at ``index`` is escaped by a backslash."""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _at_line_start(text: str, index: int) -> bool:
    """Return True when only spaces/tabs precede ``index`` on its line."""
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] == "\n"


def _is_structural_marker(
    text: str, index: int, current_type: str | None
) -> bool:
    """Decide whether ``text[index]`` is being used as a structural marker.

    ``current_type`` is the type of the block that precedes this position.  It
    lets a marker followed by a space open a new block only where that is
    meaningful, so prose such as ``2 * 3`` or ``issue # 5`` stays intact.
    """
    block_type = MARKERS.get(text[index])
    if block_type is None:
        return False

    if _is_escaped(text, index):
        return False

    # "**bold**" at the beginning of a line is emphasis, not a paragraph.
    if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "*":
        return False

    line_start = _at_line_start(text, index)

    if block_type == TITLE:
        # A title always opens a line.  Inline "@" is a mention or an e-mail.
        return line_start

    if line_start:
        return True

    # Inline # / * must be separated from the previous word.
    if text[index - 1] not in " \t":
        return False

    following = text[index + 1] if index + 1 < len(text) else ""
    if not following:
        return False

    if not following.isspace():
        # Attached form: "...#Uninstall...", "...*Paragraph...".
        return True

    # Marker + whitespace inline.  Only accept it where it opens a new block;
    # inside a paragraph this is ordinary text such as "2 * 3" or "issue # 5".
    next_index = _next_nonspace_index(text, index + 1)
    if next_index is None:
        return False
    # "2 * 3" and "issue # 5" are references, not block starts.
    if text[next_index].isdigit():
        return False
    if block_type == PARAGRAPH:
        return current_type in (TITLE, SUBTITLE)
    if block_type == SUBTITLE:
        return current_type == TITLE
    return False


def _next_nonspace_index(text: str, index: int) -> int | None:
    """Return the next non-whitespace index at or after ``index``."""
    cursor = index
    while cursor < len(text):
        if not text[cursor].isspace():
            return cursor
        cursor += 1
    return None


def _marker_positions(text: str) -> list[tuple[int, str]]:
    """Return ``(index, block_type)`` for every structural marker, in order."""
    positions: list[tuple[int, str]] = []
    current_type: str | None = None
    for index, char in enumerate(text):
        if char not in MARKERS:
            continue
        if not _is_structural_marker(text, index, current_type):
            continue
        block_type = MARKERS[char]
        positions.append((index, block_type))
        current_type = block_type
    return positions


def _is_manual_line_break(text: str, index: int) -> bool:
    """Return True when the apostrophe at ``index`` is a paragraph break.

    It must sit between two non-space characters, be followed by an uppercase
    letter, and follow a word of at least two characters.  That converts
    ``instructions'It`` while leaving ``don't``, ``O'Brien``, ``students'``
    and ``rock 'n' roll`` untouched.
    """
    if index <= 0 or index + 1 >= len(text):
        return False
    previous = text[index - 1]
    following = text[index + 1]
    if previous.isspace() or following.isspace():
        return False
    if not previous.isalnum() or not following.isupper():
        return False

    cursor = index - 1
    while cursor >= 0 and text[cursor].isalnum():
        cursor -= 1
    return (index - 1) - cursor >= 2


def _expand_manual_line_breaks(text: str) -> str:
    """Replace structural apostrophes in a paragraph with newlines."""
    if "'" not in text:
        return text
    result: list[str] = []
    for index, char in enumerate(text):
        if char == "'" and _is_manual_line_break(text, index):
            result.append("\n")
        else:
            result.append(char)
    return "".join(result)


def expand_paragraph_line_breaks(text: str) -> str:
    """Public form of the paragraph line-break rule.

    Used by the parser when storing new paragraphs and by the database repair
    migration for paragraphs stored before the rule existed.
    """
    return _expand_manual_line_breaks(text)


def parse_document_blocks(text: str) -> list[dict]:
    """Split ``text`` into ordered ``{"type", "content"}`` blocks.

    Each structural marker starts a block that runs until the next marker (or
    the end of the message).  Empty blocks, such as a bare marker directly
    followed by another marker, are dropped.

    >>> parse_document_blocks("@Windows #Uninstall *Qwen")
    [{'type': 'title', 'content': 'Windows'}, \
{'type': 'subtitle', 'content': 'Uninstall'}, \
{'type': 'paragraph', 'content': 'Qwen'}]
    """
    if not text:
        return []

    positions = _marker_positions(text)
    blocks: list[dict] = []
    for position, (index, block_type) in enumerate(positions):
        end = positions[position + 1][0] if position + 1 < len(positions) else len(text)
        content = text[index + 1 : end].strip()
        if content:
            if block_type == PARAGRAPH:
                content = _expand_manual_line_breaks(content)
            blocks.append({"type": block_type, "content": content})
    return blocks


def group_documents(blocks: list[dict]) -> list[dict]:
    """Group blocks into documents.  Every ``@`` title starts a new document.

    Returns ``[{"title": str | None, "blocks": [...]}, ...]``.  ``title`` is
    ``None`` only for a leading group of subtitle/paragraph blocks that has no
    ``@`` title yet (the handler decides whether to append it to an open
    document or reject it).
    """
    documents: list[dict] = []
    current: dict | None = None

    for block in blocks:
        if block["type"] == TITLE:
            current = {"title": block["content"], "blocks": []}
            documents.append(current)
            continue
        if current is None:
            current = {"title": None, "blocks": []}
            documents.append(current)
        current["blocks"].append(block)

    return documents


def parse_documents(text: str) -> list[dict]:
    """Convenience wrapper: ``text`` -> grouped documents."""
    return group_documents(parse_document_blocks(text))
