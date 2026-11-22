"""Tests for reading Unity archives without their streamed bulk data."""

import lzma
import re
import struct
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

UnityPy = pytest.importorskip("UnityPy")
lz4_block = pytest.importorskip("lz4.block")

from bokucho.engines import unity_bundle  # noqa: E402
from bokucho.engines.unity_bundle import load_assets, without_streamed_data  # noqa: E402

STORED, LZMA, LZ4 = 0, 1, 2
Node = tuple[str, bytes, int]


def payload(tag: str, size: int) -> bytes:
    return (tag.encode() * size)[:size]


def aligns_header(format_version: int, engine: str) -> bool:
    version = re.match(r"2019\.(\d+)\.(\d+)", engine)
    return format_version >= 7 or bool(version and (int(version[1]), int(version[2])) >= (4, 15))


def build_archive(
    nodes: Sequence[Node],
    *,
    format_version: int = 7,
    engine: str = "2022.3.10f1",
    block_size: int = 64,
    block_methods: Sequence[int] = (LZ4,),
    info_method: int = STORED,
    info_at_end: bool = False,
    extra_flags: int = 0,
    pad_start: bool = False,
    mangle: Callable[[int, bytes], bytes] | None = None,
    empty_block_at: int | None = None,
) -> bytes:
    data = b"".join(body for _, body, _ in nodes)
    blocks = []
    for index, start in enumerate(range(0, len(data), block_size)):
        chunk = data[start : start + block_size]
        method = block_methods[index % len(block_methods)]
        packed = lz4_block.compress(chunk, store_size=False) if method == LZ4 else chunk
        if mangle:
            packed = mangle(index, packed)
        blocks.append((len(chunk), packed, method))
    if empty_block_at is not None:
        blocks.insert(empty_block_at, (0, b"\xff\xff", LZ4))
    info = bytes(16) + struct.pack(">i", len(blocks))
    for size, packed, method in blocks:
        info += struct.pack(">IIH", size, len(packed), method)
    info += struct.pack(">i", len(nodes))
    offset = 0
    for path, body, flags in nodes:
        info += struct.pack(">qqI", offset, len(body), flags) + path.encode() + b"\0"
        offset += len(body)
    packed_info = lz4_block.compress(info, store_size=False) if info_method == LZ4 else info
    flags = info_method | extra_flags
    flags |= 0x80 if info_at_end else 0x40
    flags |= 0x200 if pad_start else 0
    head = b"UnityFS\0" + struct.pack(">I", format_version) + b"5.x.x\0" + engine.encode() + b"\0"
    fixed = len(head) + 20
    pad = -fixed % 16 if aligns_header(format_version, engine) else 0
    body = b"".join(packed for _, packed, _ in blocks)
    if info_at_end:
        gap = -(fixed + pad) % 16 if pad_start else 0
        rest = bytes(gap) + body + packed_info
    else:
        gap = -(fixed + pad + len(packed_info)) % 16 if pad_start else 0
        assert not pad_start or gap
        rest = packed_info + bytes(gap) + body
    total = fixed + pad + len(rest)
    head += struct.pack(">qIII", total, len(packed_info), len(info), flags)
    return head + bytes(pad) + rest


def read_nodes(raw: bytes | bytearray) -> dict[str, tuple[bytes, int]]:
    bundle = UnityPy.Environment().load_file(raw, name="data.unity3d")
    return {name: (bytes(node.bytes), node.flags) for name, node in bundle.files.items()}


def streamed(path: str) -> bool:
    return path.lower().endswith((".ress", ".resource"))


def slim(tmp_path: Path, nodes: Sequence[Node], name: str = "data.unity3d", **options):
    """A built archive reads back as its nodes before it is slimmed."""
    raw = build_archive(nodes, **options)
    assert read_nodes(raw) == {path: (body, flags) for path, body, flags in nodes}
    (tmp_path / name).write_bytes(raw)
    result = without_streamed_data(tmp_path / name)
    return result, [node for node in nodes if not streamed(node[0])]


def slim_without_oracle(tmp_path: Path, nodes: Sequence[Node], **options):
    (tmp_path / "data.unity3d").write_bytes(build_archive(nodes, **options))
    result = without_streamed_data(tmp_path / "data.unity3d")
    return result, [node for node in nodes if not streamed(node[0])]


def assert_keeps_only(result, kept: Sequence[Node]) -> None:
    assert result is not None
    assert list(read_nodes(result).items()) == [(path, (body, flags)) for path, body, flags in kept]


NODES: list[Node] = [
    ("CAB-1", payload("a", 100), 4),
    ("CAB-1.resS", payload("s", 333), 0),
    ("CAB-2", payload("b", 77), 5),
    ("Movie.resource", payload("r", 50), 0),
    ("CAB-3", payload("c", 129), 4),
]


@pytest.mark.parametrize("block_methods", [(LZ4,), (STORED,), (LZ4, STORED)])
def test_kept_nodes_keep_their_bytes_order_and_flags_when_blocks_straddle_them(
    tmp_path, block_methods
):
    result, kept = slim(tmp_path, NODES, block_methods=block_methods)
    assert_keeps_only(result, kept)
    assert not {path for path in read_nodes(result) if streamed(path)}


def test_streamed_nodes_are_left_out_whatever_the_case_of_their_suffix(tmp_path):
    nodes = [
        ("CAB-1", payload("a", 100), 4),
        ("CAB-1.RESS", payload("s", 120), 0),
        ("Movie.Resource", payload("r", 90), 0),
        ("CAB-2", payload("b", 70), 5),
    ]
    result, kept = slim(tmp_path, nodes)
    assert_keeps_only(result, kept)


def test_a_node_after_a_skipped_resS_node_keeps_its_bytes(tmp_path):
    nodes = [("a.resS", payload("s", 200), 0), ("CAB-1", payload("k", 130), 4)]
    result, kept = slim(tmp_path, nodes)
    assert_keeps_only(result, kept)


def test_an_empty_block_inside_a_kept_node_is_not_decompressed(tmp_path):
    nodes = [("CAB-1", payload("k", 200), 4), ("a.resS", payload("s", 100), 0)]
    result, kept = slim_without_oracle(tmp_path, nodes, empty_block_at=1)
    assert_keeps_only(result, kept)


def test_the_result_is_a_stored_format_7_archive_with_the_original_versions(tmp_path):
    result, _ = slim(tmp_path, NODES, format_version=6, engine="2018.4.1f1")
    bundle = UnityPy.Environment().load_file(result, name="data.unity3d")
    assert (bundle.signature, bundle.version, bundle.version_engine) == ("UnityFS", 7, "2018.4.1f1")
    assert bundle.version_player == "5.x.x"
    assert int(bundle.dataflags) == 0x40
    assert struct.unpack_from(">q", result, 8 + 4 + len("5.x.x\0") + len("2018.4.1f1\0"))[0] == len(
        result
    )


def test_kept_data_over_the_output_block_limit_splits_into_stored_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(unity_bundle, "_MAX_OUTPUT_BLOCK", 100)
    result, kept = slim(tmp_path, NODES)
    assert_keeps_only(result, kept)
    (tmp_path / "result").write_bytes(result)
    with (tmp_path / "result").open("rb") as stream:
        archive = unity_bundle._read_archive(stream, len(result))
    assert [tuple(block) for block in archive.blocks] == [
        (100, 100, 0),
        (100, 100, 0),
        (100, 100, 0),
        (6, 6, 0),
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"format_version": 6, "engine": "5.6.7f1"},
        {"format_version": 6, "engine": "2019.4.40f1"},
        {"format_version": 6, "engine": "2019.4.14f1"},
        {"format_version": 7, "engine": "2018.4.36f1"},
        {"format_version": 7, "engine": "2019.4.40f1", "info_method": LZ4},
        {"format_version": 7, "engine": "2020.3.33f1", "extra_flags": 0x1000},
        {"format_version": 7, "engine": "2020.3.34f1", "pad_start": True},
        {"format_version": 7, "engine": "2021.3.1f1", "extra_flags": 0x1000},
        {"format_version": 7, "engine": "2021.3.2f1", "pad_start": True},
        {"format_version": 7, "engine": "2022.1.0f1", "extra_flags": 0x1000},
        {"format_version": 7, "engine": "2022.1.1f1", "pad_start": True},
        {"format_version": 7, "engine": "6000.0.23f1", "pad_start": True, "info_method": LZ4},
        {"format_version": 7, "engine": "2022.3.10f1", "info_at_end": True},
        {"format_version": 7, "engine": "2022.3.10f1", "info_at_end": True, "info_method": LZ4},
        {"format_version": 6, "engine": "2017.4.40f1", "info_at_end": True},
        {"format_version": 7, "engine": "2022.3.10f1", "info_at_end": True, "pad_start": True},
        {"format_version": 6, "engine": "2022.3.10f1", "pad_start": True},
        {"format_version": 6, "engine": "2022.3.10f1", "pad_start": True, "info_method": LZ4},
        {"format_version": 6, "engine": "2022.3.10f1", "info_at_end": True, "pad_start": True},
        {"format_version": 7, "engine": "2019.4.40f1", "block_size": 1000},
    ],
    ids=lambda options: "-".join(f"{key}={value}" for key, value in options.items()),
)
def test_each_header_layout_UnityPy_reads_is_read_the_same_way(tmp_path, options):
    result, kept = slim(tmp_path, NODES, **options)
    assert_keeps_only(result, kept)


def test_the_old_flag_set_does_not_treat_0x1000_as_encryption(tmp_path):
    result, kept = slim(tmp_path, NODES, engine="2019.4.40f1", extra_flags=0x1000)
    assert_keeps_only(result, kept)


def test_a_file_that_is_not_a_UnityFS_archive_is_left_to_UnityPy(tmp_path):
    (tmp_path / "level0").write_bytes(payload("x", 500))
    (tmp_path / "web").write_bytes(b"UnityWeb\0" + payload("x", 500))
    (tmp_path / "tiny").write_bytes(b"Unity")
    assert without_streamed_data(tmp_path / "level0") is None
    assert without_streamed_data(tmp_path / "web") is None
    assert without_streamed_data(tmp_path / "tiny") is None


def test_a_split_file_is_left_to_UnityPy(tmp_path):
    (tmp_path / "data.unity3d.split0").write_bytes(build_archive(NODES))
    assert without_streamed_data(tmp_path / "data.unity3d.split0") is None


@pytest.mark.parametrize(
    ("engine", "flag"),
    [
        ("2019.4.40f1", 0x200),
        ("2020.3.33f1", 0x200),
        ("2022.3.10f1", 0x1000),
        ("2022.3.10f1", 0x400),
    ],
)
def test_an_encrypted_archive_is_left_to_UnityPy(tmp_path, engine, flag):
    (tmp_path / "data.unity3d").write_bytes(build_archive(NODES, engine=engine, extra_flags=flag))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


@pytest.mark.parametrize("engine", ["unknown", "0.0.0", ""])
def test_an_unreadable_engine_version_leaves_the_archive_to_UnityPy(tmp_path, engine):
    (tmp_path / "data.unity3d").write_bytes(build_archive(NODES, engine=engine))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_an_archive_without_streamed_nodes_is_left_to_UnityPy(tmp_path):
    kept = [node for node in NODES if not streamed(node[0])]
    (tmp_path / "data.unity3d").write_bytes(build_archive(kept))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


@pytest.mark.parametrize("cut", [10, 40, 120, 400])
def test_an_archive_cut_short_is_left_to_UnityPy(tmp_path, cut):
    raw = build_archive(NODES)
    (tmp_path / "data.unity3d").write_bytes(raw[: len(raw) - cut])
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_a_node_beyond_the_block_data_is_left_to_UnityPy(tmp_path):
    raw = bytearray(build_archive([("a.resS", payload("s", 100), 0), ("b", payload("k", 30), 0)]))
    raw[raw.index(b"b\0") - 20 : raw.index(b"b\0") - 12] = struct.pack(">q", 1000)
    (tmp_path / "data.unity3d").write_bytes(bytes(raw))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_a_large_resS_node_takes_no_memory_in_proportion_to_its_size(tmp_path):
    chunk = 128 * 1024
    nodes = [
        ("CAB-1", payload("a", 100), 4),
        ("CAB-1.resS", bytes(32 * 1024 * 1024), 0),
        ("CAB-2", payload("b", 100), 4),
    ]
    (tmp_path / "data.unity3d").write_bytes(build_archive(nodes, block_size=chunk))
    tracemalloc.start()
    try:
        result = without_streamed_data(tmp_path / "data.unity3d")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert_keeps_only(result, [nodes[0], nodes[2]])
    assert peak < 4 * 1024 * 1024


def test_blocks_holding_only_streamed_data_are_not_read_as_valid_data(tmp_path):
    nodes = [
        ("CAB-1", payload("a", 64), 4),
        ("CAB-1.resS", payload("s", 640), 0),
        ("CAB-2", payload("b", 64), 4),
    ]
    garbage = b"\xff" * 20

    def mangle(index: int, packed: bytes) -> bytes:
        return garbage if 0 < index < 11 else packed

    with pytest.raises(lz4_block.LZ4BlockError):
        lz4_block.decompress(garbage, 64)
    raw = build_archive(nodes, mangle=mangle)
    (tmp_path / "data.unity3d").write_bytes(raw)
    assert_keeps_only(without_streamed_data(tmp_path / "data.unity3d"), [nodes[0], nodes[2]])
    with pytest.raises(lz4_block.LZ4BlockError):
        read_nodes(raw)


def test_only_the_blocks_holding_kept_nodes_are_decompressed(tmp_path, monkeypatch):
    nodes = [
        ("CAB-1", payload("a", 64), 4),
        ("CAB-1.resS", payload("s", 640), 0),
        ("CAB-2", payload("b", 64), 4),
    ]
    (tmp_path / "data.unity3d").write_bytes(build_archive(nodes))
    sizes: list[int] = []
    decompress = unity_bundle._decompress

    def spy(data: bytes, size: int, flags: int):
        sizes.append(size)
        return decompress(data, size, flags)

    monkeypatch.setattr(unity_bundle, "_decompress", spy)
    assert_keeps_only(without_streamed_data(tmp_path / "data.unity3d"), [nodes[0], nodes[2]])
    assert sizes[1:] == [64, 64]


def test_a_stored_block_whose_packed_size_differs_from_its_size_is_left_to_UnityPy(tmp_path):
    raw = build_archive(
        NODES, block_methods=(STORED,), mangle=lambda index, packed: packed + b"x" * (index == 0)
    )
    (tmp_path / "data.unity3d").write_bytes(raw)
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_a_block_that_decompresses_to_another_size_is_left_to_UnityPy(tmp_path):
    short = lz4_block.compress(b"z" * 10, store_size=False)
    raw = build_archive(NODES, mangle=lambda index, packed: short if index == 0 else packed)
    (tmp_path / "data.unity3d").write_bytes(raw)
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_a_kept_block_in_a_compression_UnityPy_cannot_read_is_left_to_UnityPy(tmp_path):
    lzham = 4
    (tmp_path / "data.unity3d").write_bytes(build_archive(NODES, block_methods=(lzham,)))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def peak_of_without_streamed_data(path: Path) -> tuple[bytes | bytearray | None, int]:
    tracemalloc.start()
    try:
        result = without_streamed_data(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return result, peak


def declare_first_block_and_node_size(raw: bytes, size: int) -> bytes:
    """The archive with the uncompressed size of its first block and the size of its first
    node declared as ``size``; the stored block info starts after the aligned header."""
    patched = bytearray(raw)
    engine_end = patched.index(b"\0", patched.index(b"\0", 12) + 1)
    fixed = engine_end + 1 + 20
    info = fixed + -fixed % 16
    (block_count,) = struct.unpack_from(">i", patched, info + 16)
    struct.pack_into(">I", patched, info + 20, size)
    struct.pack_into(">q", patched, info + 20 + 10 * block_count + 4 + 8, size)
    return bytes(patched)


def test_kept_nodes_that_overlap_beyond_the_blocks_holding_them_are_left_to_UnityPy(tmp_path):
    nodes = [
        ("CAB-A", payload("a", 64), 4),
        ("CAB-A.resS", payload("s", 640), 0),
        ("CAB-C", payload("c", 64), 4),
    ]
    raw = bytearray(build_archive(nodes))
    at = raw.index(b"CAB-C\0") - 20
    raw[at : at + 8] = struct.pack(">q", 0)
    (tmp_path / "data.unity3d").write_bytes(bytes(raw))
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_a_kept_block_declaring_more_than_255_times_its_packed_size_is_not_allocated(tmp_path):
    nodes = [("CAB-1", payload("a", 64), 4), ("CAB-1.resS", payload("s", 64), 0)]
    raw = declare_first_block_and_node_size(build_archive(nodes), 64 * 1024 * 1024)
    (tmp_path / "data.unity3d").write_bytes(raw)
    result, peak = peak_of_without_streamed_data(tmp_path / "data.unity3d")
    assert result is None
    assert peak < 1024 * 1024


def test_a_kept_block_in_a_compression_missing_from_UnityPy_is_not_allocated(tmp_path):
    unknown = 5
    nodes = [("CAB-1", payload("a", 4 * 1024 * 1024), 4), ("CAB-1.resS", payload("s", 64), 0)]
    raw = build_archive(nodes, block_size=4 * 1024 * 1024, block_methods=(unknown,))
    (tmp_path / "data.unity3d").write_bytes(raw)
    result, peak = peak_of_without_streamed_data(tmp_path / "data.unity3d")
    assert result is None
    assert peak < 1024 * 1024


def lzma_block(data: bytes) -> bytes:
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}]
    packed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
    return struct.pack("<BI", (2 * 5 + 0) * 9 + 3, 1 << 20) + packed


def test_a_kept_lzma_block_beyond_255_times_its_packed_size_is_left_to_UnityPy(tmp_path):
    nodes = [("CAB-1", payload("a", 1024 * 1024), 4), ("CAB-1.resS", payload("s", 64), 0)]
    raw = build_archive(
        nodes,
        block_size=1024 * 1024,
        block_methods=(LZMA,),
        mangle=lambda _, chunk: lzma_block(chunk),
    )
    assert len(lzma_block(nodes[0][1])) * 255 < len(nodes[0][1])
    (tmp_path / "data.unity3d").write_bytes(raw)
    assert without_streamed_data(tmp_path / "data.unity3d") is None


def test_corrupt_data_in_a_kept_block_fails_as_it_does_in_UnityPy(tmp_path):
    raw = build_archive(NODES, mangle=lambda index, packed: b"\xff" * 20 if index == 0 else packed)
    (tmp_path / "data.unity3d").write_bytes(raw)
    with pytest.raises(lz4_block.LZ4BlockError):
        without_streamed_data(tmp_path / "data.unity3d")
    with pytest.raises(lz4_block.LZ4BlockError):
        read_nodes(raw)


def serialized_file(path_ids: Sequence[int]) -> bytes:
    """A little-endian version 17 SerializedFile with one TextAsset-typed object per id."""
    out = bytearray(20)
    out += b"2022.3.10f1\0" + struct.pack("<i", 19) + b"\0"
    out += struct.pack("<i", 1) + struct.pack("<i", 49) + b"\0" + struct.pack("<h", -1) + bytes(16)
    out += struct.pack("<i", len(path_ids))
    for index, path_id in enumerate(path_ids):
        out += bytes(-len(out) % 4) + struct.pack("<q", path_id)
        out += struct.pack("<IIi", index * 4, 4, 0)
    out += struct.pack("<i", 0) + struct.pack("<i", 0) + b"\0"
    metadata = len(out) - 20
    out += bytes(-len(out) % 16)
    data_offset = len(out)
    out += bytes(4 * len(path_ids))
    out[:20] = struct.pack(">IIII", metadata, len(out), 17, data_offset) + b"\0\0\0\0"
    return bytes(out)


def object_ids(env) -> list[tuple[str, int]]:
    return [(o.assets_file.name, o.path_id) for o in env.objects]


def test_load_assets_yields_the_objects_of_UnityPy_load_in_the_same_order(tmp_path):
    data = tmp_path / "Game_Data"
    data.mkdir()
    nodes = [
        ("CAB-1", serialized_file([7, 3, 9]), 4),
        ("CAB-1.resS", payload("s", 300), 0),
        ("CAB-2", serialized_file([5, 1]), 4),
    ]
    (data / "a.bundle").write_bytes(build_archive(nodes))
    (data / "level0").write_bytes(serialized_file([2, 8]))
    (data / "z.bundle").write_bytes(build_archive(nodes[:1] + nodes[1:2]))

    expected = UnityPy.load(str(data))
    env = load_assets(data)

    assert len(object_ids(expected)) == 10
    assert object_ids(env) == object_ids(expected)
    assert list(env.files) == list(expected.files)


def test_load_assets_merges_a_split_group_once_when_other_files_come_between_its_parts(
    tmp_path, monkeypatch
):
    from fsspec.implementations.local import LocalFileSystem

    data = tmp_path / "Game_Data"
    data.mkdir()
    (data / "d.assets.split0").write_bytes(payload("split0", 200))
    (data / "m.bundle").write_bytes(build_archive(NODES))
    (data / "d.assets.split1").write_bytes(payload("split1", 90))
    names = ["d.assets.split0", "m.bundle", "d.assets.split1"]
    monkeypatch.setattr(LocalFileSystem, "walk", lambda self, path: iter([(path, [], names)]))
    merged: list[str] = []
    load_split = UnityPy.environment.Environment._load_split_file

    def recording(self, basename: str) -> bytes:
        merged.append(basename)
        return load_split(self, basename)

    monkeypatch.setattr(UnityPy.environment.Environment, "_load_split_file", recording)

    env = load_assets(data)

    assert merged == [str(data) + "/d.assets"]
    assert list(env.files) == [str(data) + "/d.assets", str(data) + "/m.bundle"]
    assert bytes(env.files[str(data) + "/d.assets"].bytes) == payload("split0", 200) + payload(
        "split1", 90
    )


def test_load_assets_sets_file_when_the_folder_holds_one_file_like_UnityPy_load(tmp_path):
    (tmp_path / "a.bundle").write_bytes(build_archive(NODES))
    env = load_assets(tmp_path)
    assert env.file is next(iter(env.files.values()))
    assert hasattr(UnityPy.load(str(tmp_path)), "file")
    (tmp_path / "b.bundle").write_bytes(build_archive(NODES))
    assert not hasattr(load_assets(tmp_path), "file")
    assert not hasattr(UnityPy.load(str(tmp_path)), "file")


def summary(item) -> object:
    files = getattr(item, "files", None)
    if files:
        return {name: summary(sub) for name, sub in files.items() if not streamed(name)}, item.name
    return bytes(item.bytes), getattr(item, "flags", None)


def test_load_assets_matches_UnityPy_load_except_for_streamed_sub_files(tmp_path):
    data = tmp_path / "Game_Data"
    (data / "sub").mkdir(parents=True)
    (data / "a.bundle").write_bytes(build_archive(NODES))
    (data / "b.dat").write_bytes(payload("loose", 300))
    (data / "c.unity3d").write_bytes(build_archive(NODES, engine="2018.4.1f1", info_method=LZ4))
    (data / "d.assets.split0").write_bytes(payload("split0", 200))
    (data / "d.assets.split1").write_bytes(payload("split1", 90))
    plain = [node for node in NODES if not streamed(node[0])]
    (data / "sub" / "e.bundle").write_bytes(build_archive(plain))
    (data / "sub" / "f.bundle").write_bytes(
        build_archive(NODES, format_version=6, engine="5.6.7f1")
    )

    expected = UnityPy.load(str(data))
    env = load_assets(data)

    assert list(env.files) == list(expected.files)
    assert {key: summary(item) for key, item in env.files.items()} == {
        key: summary(item) for key, item in expected.files.items()
    }
    assert [(o.assets_file.name, o.path_id) for o in env.objects] == [
        (o.assets_file.name, o.path_id) for o in expected.objects
    ]
    bundle = next(item for key, item in env.files.items() if key.endswith("a.bundle"))
    assert bundle.name == "a.bundle"
    assert list(bundle.files) == ["CAB-1", "CAB-2", "CAB-3"]
    original = next(item for key, item in expected.files.items() if key.endswith("a.bundle"))
    assert "CAB-1.resS" in original.files


def test_load_assets_of_an_empty_folder_is_an_empty_environment(tmp_path):
    assert load_assets(tmp_path).files == {}
