"""The translatable text of a decoded KiriKiri ``.scn`` scenario (see ``psb``).

A scenario is a list of scenes. Each scene has a label, an optional title, ``texts``
entries ``[speaker key, [column, ...], ...]`` whose columns are the game's language
slots ``[name, text, count, (reading form, base form)]``, and optional ``selects``
(choices). Column 0 is the source; a translation is written into every column.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import partial

from bokucho.engines.kag import LINE_BREAK, NAME_PREFIX, UnsafeTarget, check_writable

TITLES_FILE = "scene-titles"
TITLE_PREFIX = "title:"

class UnknownShape(ValueError):
    """The scenario is not laid out the way Bokucho has seen ``.scn`` files laid out."""


@dataclass(frozen=True, slots=True)
class Piece:
    id: str
    kind: str
    source: str
    speaker: str | None


def _spans(text: str) -> Iterator[tuple[str, str]]:
    """A line's text as the game reads it, in ``(markup, characters)`` pairs.

    An escape ``\\X`` has no characters; a ruby tag ``[reading,n]`` has the next n+1
    characters, taken as they are; a lone ``[`` or a last ``\\`` is markup and its own
    character; any other character has no markup.
    """
    i = 0
    # The next "]", the last "," before it and the count after that comma are found once
    # for every "[" before that "]": found afresh at each "[", many brackets would make a
    # line quadratic.
    close, comma, count = -1, -1, None
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            yield text[i:i + 2], ""
            i += 2
            continue
        if ch == "[":
            if close < i:
                close = text.find("]", i)
                if close == -1:
                    close, count = len(text), None  # no "]" is left, so no ruby either
                else:
                    comma = text.rfind(",", i + 1, close)
                    # A ruby over ten thousand characters is none (and int() refuses 4301
                    # digits).
                    n = text[comma + 1:close] if comma != -1 and close - comma <= 5 else ""
                    count = int(n) if n.isascii() and n.isdigit() else None
            if count is not None and comma > i:
                end = close + 2 + count
                yield text[i:close + 1], text[close + 1:end]
                i = end
                continue
        yield ("" if ch not in "[\\" else ch), ch
        i += 1


def _is_ruby(markup: str) -> bool:
    return len(markup) > 2 and markup[0] == "["


def _forms(text: str) -> tuple[int, str, str]:
    """The shown length, the reading form and the base form of a line's text.

    ``\\n`` shows nothing; any other ``\\X`` shows X (``\\&`` is stored as ``${$38}``); a
    ruby tag ``[reading,n]`` covers the next n+1 characters, which count and form the base.
    """
    reading, base, length = [], [], 0
    for markup, chars in _spans(text):
        if _is_ruby(markup):
            reading.append(markup[1:-1].rpartition(",")[0])
            base.append(chars)
            length += len(chars)
        elif len(markup) == 2:
            if markup[1] != "n":
                shown = "${$38}" if markup[1] == "&" else markup[1]
                reading.append(shown)
                base.append(shown)
                length += 1
        else:
            reading.append(chars)
            base.append(chars)
            length += 1
    return length, "".join(reading), "".join(base)


def column(name: str | None, text: str) -> list[object]:
    """A language column for ``text``: five items when it holds markup, three otherwise."""
    length, reading, base = _forms(text)
    if "\\" in text or "[" in text:
        return [name, text, length, reading, base]
    return [name, text, length]


def _list(value: object, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnknownShape(f"{what} is not a list")
    return value


def _text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _write_name(columns: list, target: str) -> None:
    for col in columns:
        col[0] = target


def _write_line(columns: list, target: str) -> None:
    columns[:] = [column(col[0], target) for col in columns]


def _write_choice(select: dict, target: str) -> None:
    select["text"] = target
    for slot in _list(select.get("language"), "a choice's languages"):
        if isinstance(slot, dict):
            slot["text"] = target


def _write_title(scene: dict, target: str) -> None:
    if isinstance(scene["title"], str):
        scene["title"] = target
    else:
        scene["title"][:] = [target] * len(scene["title"])


Write = Callable[[str], None]


def _scene(scene: object, file: str) -> Iterator[tuple[Piece, Write]]:
    if not isinstance(scene, dict) or not isinstance(scene.get("label"), str):
        raise UnknownShape("a scene has no label")
    label = scene["label"].removeprefix("*")
    title = scene.get("title")
    if not (_text(title) or isinstance(title, list) and all(map(_text, title))):
        raise UnknownShape("a scene's title is neither text nor a list of text")
    first = title[0] if isinstance(title, list) and title else title
    if first:
        yield Piece(TITLE_PREFIX + first, "title", first, None), partial(_write_title, scene)
    for n, entry in enumerate(_list(scene.get("texts"), "a scene's texts")):
        if not isinstance(entry, list) or len(entry) < 2 or not _text(entry[0]):
            raise UnknownShape("a text entry has no speaker key and columns")
        columns = _list(entry[1], "a text entry's columns")
        for col in columns:
            if not (isinstance(col, list) and len(col) in (3, 5) and all(map(_text, col[:2]))):
                raise UnknownShape("a language column is not [name, text, count, ...]")
        speaker = None
        # Without a column there is nowhere to write a name.
        if entry[0] and columns:
            speaker = columns[0][0] if columns and columns[0][0] else entry[0]
            yield Piece(NAME_PREFIX + speaker, "name", speaker, None), partial(_write_name, columns)
        if columns and columns[0][1]:
            line = Piece(f"{file}:{label}:{n}", "dialogue", columns[0][1], speaker)
            yield line, partial(_write_line, columns)
    for j, select in enumerate(_list(scene.get("selects"), "a scene's choices")):
        if not isinstance(select, dict):
            raise UnknownShape("a choice is not an object")
        for slot in _list(select.get("language"), "a choice's languages"):
            if slot is not None and not isinstance(slot, dict):
                raise UnknownShape("a choice's language slot is not an object")
        text = select.get("text")
        if isinstance(text, str) and text:
            choice = Piece(f"{file}:{label}:choice{j}", "choice", text, None)
            yield choice, partial(_write_choice, select)


def _walk(root: object, file: str) -> list[tuple[Piece, Write]]:
    if not isinstance(root, dict) or not isinstance(root.get("scenes"), list):
        raise UnknownShape("the scenario has no list of scenes")
    found = [found for scene in root["scenes"] for found in _scene(scene, file)]
    # Two scenes with one label would give two lines one id, and one translation.
    ids = set()
    for piece, _ in found:
        if piece.kind in ("dialogue", "choice"):
            if piece.id in ids:
                raise UnknownShape(f"two scenes share one label, so {piece.id} names two lines")
            ids.add(piece.id)
    return found


def pieces(root: object, file: str) -> list[Piece]:
    """Per scene: its title, then each entry's name and line, then its choices."""
    return [piece for piece, _ in _walk(root, file)]


def _checked(piece: Piece, target: str) -> str:
    check_writable(piece.id, target)
    if "\0" in target:
        # A PSB string ends at its first NUL: the game would show only the text before it.
        raise UnsafeTarget(f"{piece.id}: the translation holds U+0000, which ends the text there.")
    if piece.kind == "dialogue":
        lines = LINE_BREAK.split(target)
        for line in lines[:-1]:
            # A lone last "\" escapes what follows it: the "\" of the "\n" a break becomes,
            # which the game would then show as an "n".
            if deque(_spans(line), maxlen=1) == deque([("\\", "\\")]):
                raise UnsafeTarget(
                    f"{piece.id}: a lone \"\\\" right before a line break would hide the break."
                )
        target = "\\n".join(lines)
    elif LINE_BREAK.search(target):
        raise UnsafeTarget(f"{piece.id}: a {piece.kind} cannot hold a line break.")
    allowed = {markup for markup, _ in _spans(piece.source) if markup}
    if piece.kind == "dialogue":
        allowed.add("\\n")
    for markup, chars in _spans(target):
        if markup and markup not in allowed:
            raise UnsafeTarget(
                f"{piece.id}: the translation has {markup!r}, which is not markup of the "
                "original line."
            )
        if not _is_ruby(markup):
            continue
        # The base is taken as it is, so markup in it would be shown, or cut in two.
        covered = int(markup[1:-1].rpartition(",")[2]) + 1
        if len(chars) < covered or "\\" in chars or "[" in chars:
            raise UnsafeTarget(
                f"{piece.id}: the ruby {markup!r} covers the next {covered} characters, "
                "but fewer plain characters follow it."
            )
    return target


def rewrite(root: object, file: str, targets: Mapping[str, str]) -> bool:
    """Write each set target (not blank, not its source) into ``root``; True if any was."""
    writes = []
    for piece, write in _walk(root, file):
        target = targets.get(piece.id)
        if target is None or not target.strip() or target == piece.source:
            continue
        writes.append(partial(write, _checked(piece, target)))
    for write in writes:
        write()
    return bool(writes)
