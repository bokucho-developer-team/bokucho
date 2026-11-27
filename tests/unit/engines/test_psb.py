"""PSB: the binary tree KiriKiri .scn scenarios are compiled to, read and written byte for byte."""

from __future__ import annotations

import struct
import zlib

import pytest

from bokucho.engines import psb


def _offset(raw, n):
    # 0 enc, 1 names, 2 strings, 3 string data, 4 chunk offsets, 5 chunk lengths,
    # 6 chunk data, 7 root
    return struct.unpack_from("<I", raw, 8 + 4 * n)[0]


TREE = {
    "ints": [0, 1, -1, 127, 128, -129, 2**40, -(2**63), 2**63 - 1],
    "floats": [0.0, 1.5, psb.Float32(0.25)],
    "flags": [None, True, False],
    "strings": ["", "あい", "aé"],
    "packed": (1, 2, 300),
    "nested": [[], {}, [{"a": [1]}]],
}


def test_every_value_type_round_trips(psb_file):
    root = psb.decode(psb_file(TREE)).root
    assert root == TREE
    assert type(root["floats"][2]) is psb.Float32 and type(root["packed"]) is tuple
    assert [type(v) for v in root["flags"]] == [type(None), bool, bool]


def test_decoding_and_encoding_again_gives_the_same_bytes(psb_file):
    raw = psb_file(TREE)
    assert psb.encode(psb.decode(raw)) == raw


def test_ints_take_the_fewest_bytes(psb_file):
    raw = psb_file([0, 1, -1, 128])
    assert raw[_offset(raw, 7):_offset(raw, 2)] == bytes.fromhex("200d040d0001030504050105ff068000")


@pytest.mark.parametrize("values,packed", [
    (list(range(255)), bytes([0x0D, 255, 0x0D, *range(255)])),
    ([0] * 256, bytes([0x0E, 0x00, 0x01, 0x0D]) + bytes(256)),
    ([255], bytes([0x0D, 1, 0x0D, 255])),
    ([256], bytes([0x0D, 1, 0x0E, 0x00, 0x01])),
    ([70000], bytes([0x0D, 1, 0x0F]) + (70000).to_bytes(3, "little")),
])
def test_packed_arrays_take_the_fewest_bytes_on_either_side_of_one_byte(values, packed):
    assert psb.pack_ints(values) == packed


def test_a_list_past_256_items_round_trips(psb_file):
    raw = psb_file(list(range(300)))
    assert psb.decode(raw).root == list(range(300))
    assert psb.encode(psb.decode(raw)) == raw


def test_equal_items_share_one_offset_and_decode_apart(psb_file):
    raw = psb_file(["x", "x", [1], [1]])
    shared = bytes.fromhex("200d040d000002021500200d010d000501")
    assert raw[_offset(raw, 7):_offset(raw, 2)] == shared
    root = psb.decode(raw).root
    assert root[2] == root[3] and root[2] is not root[3]


def test_strings_are_stored_once_sorted_by_their_utf8_bytes(psb_file):
    raw = psb_file(["b", "a", "あ", "a"])
    assert raw[_offset(raw, 3):_offset(raw, 4)] == b"a\0b\0\xe3\x81\x82\0"


def test_the_file_ends_with_two_empty_chunk_arrays_and_has_a_checksum(psb_file):
    raw = psb_file([1])
    assert raw[_offset(raw, 4):] == bytes.fromhex("0d000d0d000d")
    assert _offset(raw, 6) == len(raw)
    assert _offset(raw, 0) == _offset(raw, 1) == 0x2C
    assert struct.unpack_from("<I", raw, 0x28)[0] == zlib.adler32(raw[8:0x28])


def test_names_with_shared_prefixes_decode(psb_file):
    decoded = psb.decode(psb_file({"ab": 1, "abc": 2, "b": 3}, names=["ab", "abc", "b"]))
    assert decoded.names == ("ab", "abc", "b") and decoded.root == {"ab": 1, "abc": 2, "b": 3}


def test_object_values_follow_the_name_order(psb_file):
    assert list(psb.decode(psb_file({"b": 1, "a": 2}, names=["a", "b"])).root) == ["a", "b"]


def _at_root(raw, data):
    at = _offset(raw, 7)
    return raw[:at] + data + raw[at + len(data):]


@pytest.mark.parametrize("damage,message", [
    (lambda raw: b"PSX\0" + raw[4:], "not a PSB file"),
    (lambda raw: raw[:0x20], "cut short"),
    (lambda raw: raw[:0x24] + struct.pack("<I", len(raw) + 9) + raw[0x28:], "past the end"),
    (lambda raw: _at_root(raw, b"\x30"), "unknown value type 0x30"),
    (lambda raw: _at_root(raw, b"\x19"), "resources"),
])
def test_a_damaged_file_is_refused(psb_file, damage, message):
    with pytest.raises(psb.PsbError, match=message):
        psb.decode(damage(psb_file([1, "a"])))


def test_nesting_past_the_cap_is_refused(psb_file):
    # Written by hand: the encoder refuses this too, before decode would see it.
    root = b"\x20" + psb.pack_ints([])
    for _ in range(psb.MAX_DEPTH - 1):
        root = b"\x20" + psb.pack_ints([0]) + root
    assert psb.decode(_with_root(psb_file([]), root)).root == _nested(psb.MAX_DEPTH)
    deeper = b"\x20" + psb.pack_ints([0]) + root
    with pytest.raises(psb.PsbError, match="deep"):
        psb.decode(_with_root(psb_file([]), deeper))


def _with_root(raw, root):
    """``raw`` with its root value replaced by the encoded bytes ``root``."""
    at, end = _offset(raw, 7), _offset(raw, 2)
    shift = len(root) - (end - at)
    offsets = [_offset(raw, n) + (shift if 2 <= n <= 6 else 0) for n in range(8)]
    return raw[:8] + struct.pack("<8I", *offsets) + raw[0x28:at] + root + raw[end:]


def test_a_small_file_of_many_equal_lines_still_decodes():
    # The encoder writes equal lines once, so this file of a few hundred bytes holds 16,000
    # values: far more than twice its size, and harmless.
    entry = ["", [[None, "あ", 1], [None, "A", 1], [None, "啊", 1]], None, 0, {}]
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    raw = psb.encode(psb.Psb(3, (), names, [entry] * 1000))
    assert len(raw) < 4000
    assert psb.decode(raw).root == [entry] * 1000


def test_shared_offsets_that_would_expand_without_bound_are_refused(psb_file):
    root = b"\x05\x01"  # the int 1
    for _ in range(12):  # ten items sharing one offset, twelve deep: 10**12 values
        root = b"\x20" + psb.pack_ints([0] * 10) + root
    with pytest.raises(psb.PsbError, match="expand"):
        psb.decode(_with_root(psb_file([]), root))


def test_a_packed_array_shared_by_many_offsets_is_refused(psb_file):
    # Two thousand offsets to one packed array of two thousand ints: 4,000,000 ints.
    root = b"\x20" + psb.pack_ints([0] * 2000) + psb.pack_ints([0] * 2000)
    with pytest.raises(psb.PsbError, match="expand"):
        psb.decode(_with_root(psb_file([]), root))


class _Guarded:
    """Bytes whose part past ``header`` fails the test when it is read.

    Not a bytes subclass: a read through the buffer protocol would not pass by here.
    """

    def __init__(self, data: bytes, header: int) -> None:
        self.data, self.header = data, header

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, key):
        read = range(len(self.data))[key]
        last = max(read, default=-1) if isinstance(read, range) else read
        if last >= self.header:
            raise AssertionError("the array's items were read")
        return self.data[key]


@pytest.mark.parametrize("count", [100, 1000])
def test_an_array_past_what_is_left_of_the_budget_is_refused_before_it_is_read(count):
    # One byte or two for the count: both ways of reading it charge the budget first.
    packed = psb.pack_ints([0] * count)
    reader = psb._Reader(_Guarded(packed, len(packed) - count))
    reader.budget = count - 1
    with pytest.raises(psb.PsbError, match="expand"):
        reader.ints(0)


def test_a_string_holding_u0000_is_refused_by_the_encoder():
    # A PSB string ends at its first U+0000, so the rest would be lost without a word.
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    with pytest.raises(psb.PsbError, match=r"U\+0000"):
        psb.encode(psb.Psb(3, (), names, ["A\0B"]))
    assert psb.decode(psb.encode(psb.Psb(3, (), names, ["", "AB"]))).root == ["", "AB"]


def test_a_string_holding_a_lone_surrogate_is_refused_by_the_encoder():
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    with pytest.raises(psb.PsbError, match="UTF-8"):
        psb.encode(psb.Psb(3, (), names, ["A", "\ud800"]))


def _nested(levels: int) -> list:
    tree: list = []
    for _ in range(levels - 1):
        tree = [tree]
    return tree


def test_the_encoder_refuses_what_decode_would_refuse_to_read():
    # Deeper than decode reads runs out of stack, and a list inside itself never ends.
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    looped: list = []
    looped.append(looped)
    for root in (_nested(psb.MAX_DEPTH + 1), looped):
        with pytest.raises(psb.PsbError, match="deep"):
            psb.encode(psb.Psb(3, (), names, root))
    root = [_nested(psb.MAX_DEPTH - 1), "A"]
    assert psb.decode(psb.encode(psb.Psb(3, (), names, root))).root == root


def test_a_list_at_many_places_is_written_once():
    # Sixteen lists, each holding the one before twice: 2**16 places in a few hundred
    # bytes. Fifty make 2**50 places, which no file decode reads could hold: refused, and in
    # a moment, without writing them out.
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    root: list = ["A"]
    for _ in range(16):
        root = [root, root]
    raw = psb.encode(psb.Psb(3, (), names, root))
    assert len(raw) < 2000
    assert psb.decode(raw).root == root
    for _ in range(34):
        root = [root, root]
    with pytest.raises(psb.PsbError, match="expand"):
        psb.encode(psb.Psb(3, (), names, root))


def test_the_encoder_counts_what_decode_spends_reading_it(psb_file, monkeypatch):
    shared = [1, "あい", (4, 5), {"a": [None]}]
    decoded = psb.decode(psb_file({**TREE, "again": [shared, shared, {"a": shared}]}))
    raw = psb.encode(decoded)
    counted = psb._cost(decoded, psb._strings(decoded.root)[0])
    spent = []
    real = psb._Reader._spend

    def spend(self, count):
        spent.append(count)
        real(self, count)

    monkeypatch.setattr(psb._Reader, "_spend", spend)
    psb.decode(raw)
    assert sum(spent) == counted


def test_a_tree_decode_would_refuse_laid_out_shared_is_written_out_in_full():
    # Laid out shared, these 1100 lists of 1100 ints take a few kilobytes, which decode
    # may expand to about a million values: these are more. Written out in full, they fit.
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    root = [[0] * 1100] * 1100
    raw = psb.encode(psb.Psb(3, (), names, root))
    assert psb.decode(raw).root == root


def test_a_list_at_many_places_is_held_to_the_depth_at_each():
    # Written first near the top, it is not let through again where its inner list would
    # be deeper than decode reads.
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    shared: list = [[]]

    def under(levels: int) -> list:
        tree = shared
        for _ in range(levels):
            tree = [tree]
        return tree

    with pytest.raises(psb.PsbError, match="deep"):
        psb.encode(psb.Psb(3, (), names, [shared, under(psb.MAX_DEPTH - 2)]))
    root = [shared, under(psb.MAX_DEPTH - 3)]
    assert psb.decode(psb.encode(psb.Psb(3, (), names, root))).root == root


def test_names_that_all_spell_one_long_trie_path_are_refused():
    # Node i hangs under node i - 1 by the byte 1, so each tail spells a 1998-byte name.
    length = 2000
    names = (
        psb.pack_ints(list(range(length)))
        + psb.pack_ints([0, *range(length - 1)])
        + psb.pack_ints([length - 1] * 2000)
    )
    with pytest.raises(psb.PsbError, match="expand"):
        psb.decode(psb.encode(psb.Psb(3, (), names, [])))


def test_string_indices_that_all_point_at_one_long_string_are_refused():
    # Two thousand string indices on one 2000-byte string: 4,000,000 bytes decoded.
    count = 2000
    names = psb.pack_ints([0]) + psb.pack_ints([0]) + psb.pack_ints([])
    refs = b"".join(b"\x16" + i.to_bytes(2, "little") for i in range(count))
    root = b"\x20" + psb.pack_ints([3 * i for i in range(count)]) + refs
    table, text, empty = psb.pack_ints([0] * count), b"a" * 2000 + b"\0", psb.pack_ints([])
    names_at, root_at = 0x2C, 0x2C + len(names)
    strings_at = root_at + len(root)
    data_at = strings_at + len(table)
    chunks_at = data_at + len(text)
    head = b"PSB\0" + struct.pack(
        "<HH8I", 3, 0, names_at, names_at, strings_at, data_at, chunks_at,
        chunks_at + len(empty), chunks_at + 2 * len(empty), root_at,
    )
    raw = head + struct.pack("<I", zlib.adler32(head[8:])) + names + root + table + text
    with pytest.raises(psb.PsbError, match="expand"):
        psb.decode(raw + empty + empty)


@pytest.mark.parametrize("version", [2, 4])
def test_a_version_other_than_3_is_refused(psb_file, version):
    raw = psb_file([1])
    with pytest.raises(psb.PsbError, match=f"version {version}"):
        psb.decode(raw[:4] + struct.pack("<H", version) + raw[6:])


def test_the_encryption_flag_is_its_own_refusal(psb_file):
    raw = psb_file([1])
    with pytest.raises(psb.Encrypted):
        psb.decode(raw[:6] + b"\x01\x00" + raw[8:])


@pytest.mark.parametrize(
    "root,names,message", [([2**63], [], "too large"), ({"a": 1}, ["b"], "names")]
)
def test_what_the_encoder_cannot_write_is_refused(root, names, message):
    with pytest.raises(psb.PsbError, match=message):
        psb.encode(psb.Psb(3, tuple(names), b"", root))
