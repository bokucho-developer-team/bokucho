"""PSB: the binary tree that compiled KiriKiri ``.scn`` scenarios are stored in.

Only unencrypted version 3 files without resources are read. ``encode`` writes values the
way the original encoder does (fewest-byte widths, strings sorted by their UTF-8 bytes,
equal children of one list or object sharing one offset), so a file that is decoded and
encoded again comes back byte for byte; the adapter checks that before it writes anything.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MAGIC = b"PSB\0"
MAX_DEPTH = 64
_HEADER = 0x2C
# struct codes for the packed-array widths it has; widths 3, 5, 6 and 7 go byte by byte.
_FORMATS = {2: "H", 4: "I", 8: "Q"}
_CUT_SHORT = "the file is cut short"
_DOUBLE = struct.Struct("<d")
_ZERO = bytes(8)


class PsbError(ValueError):
    """The data is not a PSB file this module reads, or a value cannot be written."""


class Encrypted(PsbError):
    """The PSB's encryption flag is set."""


class Float32(float):
    """A float stored in four bytes (type ``0x1E``), kept apart so it is written back so."""


@dataclass(frozen=True, slots=True)
class Psb:
    version: int
    names: tuple[str, ...]
    names_section: bytes
    root: object


def _uwidth(value: int) -> int:
    return max(1, (value.bit_length() + 7) // 8)


def _swidth(value: int) -> int:
    return 0 if value == 0 else (~value if value < 0 else value).bit_length() // 8 + 1


def pack_ints(values: Sequence[int]) -> bytes:
    """A packed array of non-negative ints: count width, count, element width, elements."""
    # Nearly every array in a real file has under 256 items, each under 256.
    if len(values) < 256 and 0 <= min(values, default=0) and max(values, default=0) < 256:
        return bytes((0x0D, len(values), 0x0D)) + bytes(values)
    if min(values, default=0) < 0:
        raise PsbError("a packed array cannot hold a negative value")
    count, width = _uwidth(len(values)), _uwidth(max(values, default=0))
    if width > 8:
        raise PsbError("a packed value is too large")
    head = bytes([0x0C + count]) + len(values).to_bytes(count, "little") + bytes([0x0C + width])
    if width == 1:
        return head + bytes(values)
    if width in _FORMATS:
        return head + struct.pack(f"<{len(values)}{_FORMATS[width]}", *values)
    return head + b"".join(v.to_bytes(width, "little") for v in values)


def _budget(size: int) -> int:
    """What decode may spend reading a file of ``size`` bytes (see :meth:`_Reader._spend`)."""
    return 2 * size + (1 << 20)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.strings: dict[int, str] = {}
        # Shared offsets are decoded afresh, so nested sharing could expand a small file
        # without bound. Real .scn spend at most 0.54 of it per byte; the floor lets a small
        # file of many equal lines through, at a million values at most.
        # ponytail: a flat budget; memoize offsets if a real game ever needs more.
        self.budget = _budget(len(data))
        self.keys: tuple[str, ...] = ()
        self.offsets: list[int] = []
        self.string_data = 0

    def need(self, at: int, size: int) -> None:
        if at < 0 or at + size > len(self.data):
            raise PsbError("the file is cut short")

    def uint(self, at: int, width: int) -> int:
        self.need(at, width)
        return int.from_bytes(self.data[at:at + width], "little")

    def ints(self, at: int) -> tuple[list[int], int]:
        data = self.data
        # One-byte count, one-byte items: nearly every array in a real file.
        if data[at:at + 1] == b"\x0d" and data[at + 2:at + 3] == b"\x0d":
            end = at + 3 + data[at + 1]
            if end <= len(data):
                self._spend(data[at + 1])
                return list(data[at + 3:end]), end
        self.need(at, 1)
        count = self.data[at] - 0x0C
        if not 1 <= count <= 8:
            raise PsbError(f"expected a packed array at {at:#x}")
        n = self.uint(at + 1, count)
        at += 1 + count
        self.need(at, 1)
        width = self.data[at] - 0x0C
        if not 1 <= width <= 8:
            raise PsbError(f"expected a packed array at {at:#x}")
        at += 1
        self.need(at, n * width)
        # Paid for before it is read: an offset to a shared array reads it again.
        self._spend(n)
        end = at + n * width
        data = self.data
        if width == 1:
            return list(data[at:end]), end
        if width in _FORMATS:
            return list(struct.unpack_from(f"<{n}{_FORMATS[width]}", data, at)), end
        return [int.from_bytes(data[i:i + width], "little") for i in range(at, end, width)], end

    def names(self, at: int) -> tuple[tuple[str, ...], int]:
        charset, at = self.ints(at)
        tree, at = self.ints(at)
        tails, at = self.ints(at)
        names = []
        for tail in tails:
            out = bytearray()
            try:
                node = tree[tail]
                while node != 0:
                    if len(out) > len(tree):
                        raise IndexError
                    parent = tree[node]
                    out.append(node - charset[parent])
                    node = parent
                names.append(bytes(reversed(out)).decode("utf-8"))
            except (IndexError, ValueError):
                raise PsbError("the names section is damaged") from None
            # Tails can all spell one long path: the walks share the decoding budget.
            self._spend(len(out) + 1)
        return tuple(names), at

    def string(self, index: int) -> str:
        if index not in self.strings:
            if index >= len(self.offsets):
                raise PsbError("a string index is out of range")
            start = self.string_data + self.offsets[index]
            end = self.data.find(b"\0", start)
            if start >= len(self.data) or end < 0:
                raise PsbError("a string runs past the end")
            # Many indices can point at one string's bytes: each is decoded on its own.
            self._spend(end - start)
            try:
                self.strings[index] = self.data[start:end].decode("utf-8")
            except UnicodeDecodeError:
                raise PsbError("a string is not UTF-8") from None
        return self.strings[index]

    def _spend(self, count: int) -> None:
        self.budget -= count
        if self.budget < 0:
            raise PsbError("its shared values expand far past the file's size")

    def children(self, offsets: list[int], base: int, depth: int) -> list[object]:
        """The values at ``offsets``, which :meth:`ints` already counted against the budget."""
        data, size, strings = self.data, len(self.data), self.strings
        out: list[object] = []
        append = out.append
        # Most children are floats, ints, strings and nulls: read here, they save a call each.
        for offset in offsets:
            at = base + offset
            if at >= size:
                raise PsbError("a value points past the end")
            t = data[at]
            if 0x04 <= t <= 0x0C:
                end = at + t - 0x03
                if end > size:
                    raise PsbError(_CUT_SHORT)
                append(int.from_bytes(data[at + 1:end], "little", signed=True))
            elif t == 0x1D:
                append(0.0)
            elif t == 0x1F:
                if at + 9 > size:
                    raise PsbError(_CUT_SHORT)
                append(_DOUBLE.unpack_from(data, at + 1)[0])
            elif 0x15 <= t <= 0x18:
                end = at + t - 0x13
                if end > size:
                    raise PsbError(_CUT_SHORT)
                index = int.from_bytes(data[at + 1:end], "little")
                append(strings[index] if index in strings else self.string(index))
            elif t == 0x01:
                append(None)
            else:
                append(self._typed(at, t, depth))
        return out

    def value(self, at: int, depth: int = 0) -> object:
        self._spend(1)
        self.need(at, 1)
        return self._typed(at, self.data[at], depth)

    def _typed(self, at: int, t: int, depth: int) -> object:
        """The value of type ``t`` at ``at``, already counted against the budget."""
        if t == 0x01:
            return None
        if t in (0x02, 0x03):
            return t == 0x03
        if 0x04 <= t <= 0x0C:
            self.need(at + 1, t - 0x04)
            return int.from_bytes(self.data[at + 1:at + 1 + t - 0x04], "little", signed=True)
        if 0x0D <= t <= 0x14:
            return tuple(self.ints(at)[0])
        if 0x15 <= t <= 0x18:
            return self.string(self.uint(at + 1, t - 0x14))
        if 0x19 <= t <= 0x1C:
            raise PsbError("holds resources, which Bokucho does not read")
        if t == 0x1D:
            return 0.0
        if t == 0x1E:
            self.need(at + 1, 4)
            return Float32(struct.unpack_from("<f", self.data, at + 1)[0])
        if t == 0x1F:
            self.need(at + 1, 8)
            return struct.unpack_from("<d", self.data, at + 1)[0]
        if t not in (0x20, 0x21):
            raise PsbError(f"unknown value type {t:#04x}")
        if depth >= MAX_DEPTH:
            raise PsbError(f"values are nested more than {MAX_DEPTH} deep")
        if t == 0x20:
            offsets, base = self.ints(at + 1)
            return self.children(offsets, base, depth + 1)
        keys, after = self.ints(at + 1)
        offsets, base = self.ints(after)
        if len(keys) != len(offsets) or max(keys, default=-1) >= len(self.keys):
            raise PsbError("an object's keys are damaged")
        values = self.children(offsets, base, depth + 1)
        return dict(zip(map(self.keys.__getitem__, keys), values, strict=True))


def decode(data: bytes) -> Psb:
    """Read a PSB file. Each offset is decoded afresh: no two positions share an object."""
    if data[:4] != MAGIC:
        raise PsbError("not a PSB file")
    if len(data) < _HEADER:
        raise PsbError("the file is cut short")
    version, encrypted = struct.unpack_from("<HH", data, 4)
    if encrypted:
        raise Encrypted("the file is encrypted")
    if version != 3:
        raise PsbError(f"it is PSB version {version}; Bokucho reads version 3 only")
    offsets = struct.unpack_from("<8I", data, 8)
    if any(o > len(data) for o in offsets):
        raise PsbError("the header points past the end")
    _, names_at, strings_at, string_data, chunk_offsets, chunk_lengths, _, root_at = offsets
    reader = _Reader(data)
    names, names_end = reader.names(names_at)
    reader.keys = names
    reader.offsets, _ = reader.ints(strings_at)
    reader.string_data = string_data
    if reader.ints(chunk_offsets)[0] or reader.ints(chunk_lengths)[0]:
        raise PsbError("holds resource chunks, which Bokucho does not read")
    return Psb(version, names, data[names_at:names_end], reader.value(root_at))


_SCALARS = frozenset({int, float, type(None)})


def _strings(root: object) -> tuple[set[str], set[int]]:
    """Every string in ``root``, and the ids of the lists and objects at more than one place.

    Each list or object is looked into once, so one inside itself, or one at many places,
    costs no more than once.
    """
    found: set[str] = set()
    seen: set[int] = set()
    again: set[int] = set()
    stack = [root]
    pop, push, add, see = stack.pop, stack.append, found.add, seen.add
    while stack:
        value = pop()
        if isinstance(value, str):
            add(value)
        elif isinstance(value, (list, dict)):
            key = id(value)
            if key in seen:
                again.add(key)
                continue
            see(key)
            for item in value.values() if isinstance(value, dict) else value:
                kind = type(item)
                if kind is str:
                    add(item)
                elif kind not in _SCALARS:
                    push(item)
    return found, again


def _shared(children: list[bytes], share: bool) -> tuple[bytes, bytes]:
    """Offsets and data for children; sharing, a child equal to an earlier one reuses its offset."""
    first: dict[bytes, int] = {}
    offsets, data = [], bytearray()
    for child in children:
        at = first.get(child)
        if at is None:
            at = len(data)
            if share:
                first[child] = at
            data += child
        offsets.append(at)
    return pack_ints(offsets), bytes(data)


def _string_ref(index: int) -> bytes:
    width = _uwidth(index)
    if width > 4:
        raise PsbError("too many strings: a string index is too large")
    return bytes([0x14 + width]) + index.to_bytes(width, "little")


class _Writer:
    def __init__(
        self, strings: dict[str, int], names: dict[str, int], shared: set[int], share: bool
    ) -> None:
        # Each string's reference and each int's bytes are made once and reused, and so is
        # a list or object at more than one place, which would otherwise be written again
        # at each: a few nested in one another would take forever.
        self.strings = {s: _string_ref(index) for s, index in strings.items()}
        self.ints: dict[int, bytes] = {}
        self.names = names
        self.shared = shared
        self.share = share
        self.written: dict[tuple[int, int], bytes] = {}

    def _int(self, value: int) -> bytes:
        width = _swidth(value)
        if width > 8:
            raise PsbError(f"the int {value} is too large")
        self.ints[value] = bytes([0x04 + width]) + value.to_bytes(width, "little", signed=True)
        return self.ints[value]

    def _children(self, items: Iterable[object], depth: int) -> tuple[bytes, bytes]:
        strings, ints = self.strings, self.ints
        out = []
        for item in items:
            kind = type(item)  # exact types only: bool and Float32 take the long way
            if kind is float:
                packed = _DOUBLE.pack(item)
                out.append(b"\x1d" if packed == _ZERO else b"\x1f" + packed)
            elif kind is int:
                out.append(ints.get(item) or self._int(item))
            elif kind is str:
                out.append(strings[item])
            elif item is None:
                out.append(b"\x01")
            elif kind is list or kind is dict:
                out.append(self._container(item, depth))
            else:
                out.append(self.value(item, depth))
        return _shared(out, self.share)

    def value(self, value: object, depth: int = 0) -> bytes:
        if value is None:
            return b"\x01"
        if isinstance(value, bool):
            return b"\x03" if value else b"\x02"
        if isinstance(value, int):
            return self.ints.get(value) or self._int(value)
        if isinstance(value, tuple):
            return pack_ints(value)
        if isinstance(value, str):
            return self.strings[value]
        if isinstance(value, Float32):
            return b"\x1e" + struct.pack("<f", value)
        if isinstance(value, float):
            packed = struct.pack("<d", value)
            return b"\x1d" if packed == bytes(8) else b"\x1f" + packed
        if isinstance(value, (list, dict)):
            return self._container(value, depth)
        raise PsbError(f"cannot write a {type(value).__name__}")

    def _container(self, value: list | dict, depth: int) -> bytes:
        # As deep as decode reads, and no deeper: one inside itself stops here too.
        if depth >= MAX_DEPTH:
            raise PsbError(f"values are nested more than {MAX_DEPTH} deep")
        if id(value) not in self.shared:
            return self._write(value, depth + 1)
        # Kept by depth too: deeper, the same list may hold one past what decode reads.
        key = (id(value), depth)
        if (written := self.written.get(key)) is None:
            written = self.written[key] = self._write(value, depth + 1)
        return written

    def _write(self, value: list | dict, depth: int) -> bytes:
        if isinstance(value, list):
            offsets, data = self._children(value, depth)
            return b"\x20" + offsets + data
        for key in value:
            if key not in self.names:
                raise PsbError(f"the key {key!r} is not in the names section")
        keys = sorted(value, key=self.names.__getitem__)
        offsets, data = self._children((value[key] for key in keys), depth)
        return b"\x21" + pack_ints([self.names[key] for key in keys]) + offsets + data


#: Past this much reading, a tree is refused rather than written out in full: that would
#: take hundreds of megabytes.
# ponytail: a fixed ceiling, far past any real scenario; raise it if a game needs more.
_FULL_LAYOUT_LIMIT = 1 << 24


def _cost(psb: Psb, strings: Iterable[str]) -> int:
    """What decode spends reading ``psb`` as :func:`encode` writes it, in any layout.

    Each offset is read afresh, so a list or object costs the same wherever it sits, and
    each string is read once.
    """
    reader = _Reader(psb.names_section)
    reader.budget = start = 1 << 62
    reader.names(0)
    strings = list(strings)
    # The names, the string table, each string, and the root.
    spent = start - reader.budget + len(strings) + sum(len(s.encode("utf-8")) for s in strings)
    known: dict[int, int] = {}

    def cost(value: object) -> int:
        if isinstance(value, tuple):
            return len(value)
        if not isinstance(value, (list, dict)):
            return 0
        if id(value) not in known:
            # A list pays for its offsets, an object for its keys and its offsets.
            items = value.values() if isinstance(value, dict) else value
            each = 2 if isinstance(value, dict) else 1
            known[id(value)] = each * len(value) + sum(map(cost, items))
        return known[id(value)]

    return spent + 1 + cost(psb.root)


def encode(psb: Psb) -> bytes:
    """Write ``psb`` as an unencrypted PSB file, laid out as the original encoder does.

    That layout gives equal children one offset, and decode reads each offset afresh: where
    the file comes out too small for what decode may spend reading it, each child is written
    out in full instead, and a tree no layout lets decode read is refused.
    """
    found, shared = _strings(psb.root)
    try:
        strings = sorted(found, key=lambda s: s.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise PsbError(f"a string cannot be written as UTF-8: {error.reason}") from None
    out = _layout(psb, strings, shared, share=True)
    cost = _cost(psb, strings)
    if _budget(len(out)) < cost <= _FULL_LAYOUT_LIMIT:
        out = _layout(psb, strings, shared, share=False)
    if cost > _budget(len(out)):
        raise PsbError("its values expand far past what a PSB file of its size may hold")
    return out


def _layout(psb: Psb, strings: list[str], shared: set[int], share: bool) -> bytes:
    writer = _Writer(
        {s: i for i, s in enumerate(strings)},
        {n: i for i, n in enumerate(psb.names)},
        shared,
        share,
    )
    root = writer.value(psb.root)
    string_offsets, string_data = [], bytearray()
    for s in strings:
        if "\0" in s:
            raise PsbError("a string holds U+0000, where a PSB string ends")
        string_offsets.append(len(string_data))
        string_data += s.encode("utf-8") + b"\0"
    names_at = _HEADER
    root_at = names_at + len(psb.names_section)
    strings_at = root_at + len(root)
    table = pack_ints(string_offsets)
    data_at = strings_at + len(table)
    chunk_offsets = data_at + len(string_data)
    empty = pack_ints([])
    chunk_lengths = chunk_offsets + len(empty)
    chunk_data = chunk_lengths + len(empty)
    head = MAGIC + struct.pack("<HH", psb.version, 0) + struct.pack(
        "<8I", names_at, names_at, strings_at, data_at, chunk_offsets, chunk_lengths, chunk_data,
        root_at,
    )
    checksum = struct.pack("<I", zlib.adler32(head[8:]))
    return head + checksum + psb.names_section + root + table + string_data + empty + empty
