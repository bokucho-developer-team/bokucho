"""Unity asset archives read without their streamed bulk data.

Textures, meshes and sounds keep their bulk data in the ``.resS`` and ``.resource``
nodes of a UnityFS archive; no text object reads those nodes.
"""

from __future__ import annotations

import bisect
import struct
from pathlib import Path
from typing import IO, Any, NamedTuple

_SIGNATURE = b"UnityFS\0"
_STREAMED_SUFFIXES = (b".ress", b".resource")
_COMPRESSION_MASK = 0x3F
_BLOCKS_INFO_AT_END = 0x80
_BLOCK_INFO_PADDING_AT_START = 0x200
_OLD_ENCRYPTION = 0x200
_NEW_ENCRYPTION = 0x1400
_STORED_ARCHIVE = 0x40
_STORED_FORMAT = 7
_ALIGNMENT = 16
_MAX_OUTPUT_BLOCK = 1 << 30
# No LZ4 block decodes to more than 255 times its compressed size; an archive declaring
# more for a block that must be read is left to UnityPy.
_MAX_EXPANSION = 255


class _Block(NamedTuple):
    uncompressed_size: int
    compressed_size: int
    flags: int


class _Node(NamedTuple):
    offset: int
    size: int
    flags: int
    path: bytes


class _Archive(NamedTuple):
    player: bytes
    engine: bytes
    blocks: list[_Block]
    nodes: list[_Node]
    data_start: int


def _align(position: int) -> int:
    return position + -position % _ALIGNMENT


def _c_string(stream: IO[bytes]) -> bytes:
    text = bytearray()
    while (char := stream.read(1)) != b"\0":
        if not char:
            raise ValueError("unterminated string")
        text += char
    return bytes(text)


def _decompress(data: bytes, size: int, flags: int) -> bytes | None:
    from UnityPy.helpers.CompressionHelper import DECOMPRESSION_MAP

    method = DECOMPRESSION_MAP.get(flags & _COMPRESSION_MASK)
    if method is None:
        return None
    try:
        return bytes(method(data, size))
    except NotImplementedError:
        return None


def _read_archive(stream: IO[bytes], length: int) -> _Archive | None:
    """The blocks and nodes of a UnityFS archive, or None when it is not one this reads."""
    from UnityPy.helpers.UnityVersion import UnityVersion

    if stream.read(len(_SIGNATURE)) != _SIGNATURE:
        return None
    try:
        (format_version,) = struct.unpack(">I", stream.read(4))
        player = _c_string(stream)
        engine = _c_string(stream)
        _size, blocks_info_size, blocks_info_uncompressed, flags = struct.unpack(
            ">qIII", stream.read(20)
        )
        version = UnityVersion.from_str(engine.decode("utf-8", "surrogateescape"))
        if version.major == 0:
            return None
        old_flags = (
            version < (2020,)
            or (version[0] == 2020 and version < (2020, 3, 34))
            or (version[0] == 2021 and version < (2021, 3, 2))
            or (version[0] == 2022 and version < (2022, 1, 1))
        )
        if flags & (_OLD_ENCRYPTION if old_flags else _NEW_ENCRYPTION):
            return None
        start = stream.tell()
        if format_version >= 7 or (version[0] == 2019 and version >= (2019, 4, 15)):
            start = _align(start)
        at_end = bool(flags & _BLOCKS_INFO_AT_END)
        info_start = length - blocks_info_size if at_end else start
        if info_start < start or info_start + blocks_info_size > length:
            return None
        stream.seek(info_start)
        info = _decompress(stream.read(blocks_info_size), blocks_info_uncompressed, flags)
        if info is None:
            return None
        data_start = start if at_end else start + blocks_info_size
        if not old_flags and flags & _BLOCK_INFO_PADDING_AT_START:
            data_start = _align(data_start)

        (block_count,) = struct.unpack_from(">i", info, 16)
        if block_count < 0:
            return None
        blocks = [
            _Block(*struct.unpack_from(">IIH", info, 20 + 10 * index))
            for index in range(block_count)
        ]
        position = 20 + 10 * block_count
        (node_count,) = struct.unpack_from(">i", info, position)
        if node_count < 0:
            return None
        position += 4
        nodes = []
        for _ in range(node_count):
            offset, size, node_flags = struct.unpack_from(">qqI", info, position)
            position += 20
            end = info.index(b"\0", position)
            nodes.append(_Node(offset, size, node_flags, info[position:end]))
            position = end + 1
    except (struct.error, ValueError):
        return None
    return _Archive(player, engine, blocks, nodes, data_start)


def _stored_archive(archive: _Archive, kept: list[_Node], size: int) -> bytearray:
    block_sizes = [
        min(_MAX_OUTPUT_BLOCK, size - done) for done in range(0, size, _MAX_OUTPUT_BLOCK)
    ]
    info = bytearray(16)
    info += struct.pack(">i", len(block_sizes))
    for block_size in block_sizes:
        info += struct.pack(">IIH", block_size, block_size, 0)
    info += struct.pack(">i", len(kept))
    offset = 0
    for node in kept:
        info += struct.pack(">qqI", offset, node.size, node.flags) + node.path + b"\0"
        offset += node.size
    header = bytearray(_SIGNATURE)
    header += struct.pack(">I", _STORED_FORMAT) + archive.player + b"\0" + archive.engine + b"\0"
    info_start = _align(len(header) + 20)
    data_start = info_start + len(info)
    header += struct.pack(">qIII", data_start + size, len(info), len(info), _STORED_ARCHIVE)
    output = bytearray(data_start + size)
    output[: len(header)] = header
    output[info_start:data_start] = info
    return output


def without_streamed_data(path: Path) -> bytes | bytearray | None:
    """The UnityFS archive at ``path`` as an uncompressed archive without its
    ``.resS`` and ``.resource`` nodes, or None when there is nothing to leave out
    or the file is not an archive this can read.

    The other nodes keep their order, paths, flags and bytes. A block holding
    only streamed data is never read or decompressed.
    """
    from UnityPy.enums.BundleFile import CompressionFlags
    from UnityPy.environment import reSplit
    from UnityPy.helpers.CompressionHelper import DECOMPRESSION_MAP

    # UnityPy lists LZHAM among its decompressors but cannot decode it.
    readable = set(DECOMPRESSION_MAP) - {CompressionFlags.LZHAM}
    if reSplit.match(str(path)):
        return None
    with path.open("rb") as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(0)
        archive = _read_archive(stream, length)
        if archive is None:
            return None
        blocks, nodes = archive.blocks, archive.nodes
        total = sum(block.uncompressed_size for block in blocks)
        if (
            archive.data_start + sum(block.compressed_size for block in blocks) > length
            or any(
                block.flags & _COMPRESSION_MASK == 0
                and block.compressed_size != block.uncompressed_size
                for block in blocks
            )
            or any(
                node.offset < 0 or node.size < 0 or node.offset + node.size > total
                for node in nodes
            )
        ):
            return None
        kept = [node for node in nodes if not node.path.lower().endswith(_STREAMED_SUFFIXES)]
        if len(kept) == len(nodes):
            return None

        uncompressed_starts: list[int] = []
        compressed_starts: list[int] = []
        uncompressed = compressed = 0
        for block in blocks:
            uncompressed_starts.append(uncompressed)
            compressed_starts.append(archive.data_start + compressed)
            uncompressed += block.uncompressed_size
            compressed += block.compressed_size

        overlapping: dict[int, list[tuple[_Node, int]]] = {}
        size = 0
        for node in kept:
            if node.size:
                first = bisect.bisect_right(uncompressed_starts, node.offset) - 1
                last = bisect.bisect_left(uncompressed_starts, node.offset + node.size) - 1
                for index in range(first, last + 1):
                    if blocks[index].uncompressed_size:
                        overlapping.setdefault(index, []).append((node, size))
            size += node.size

        if size > sum(blocks[index].uncompressed_size for index in overlapping) or any(
            blocks[index].flags & _COMPRESSION_MASK not in readable
            or blocks[index].uncompressed_size > _MAX_EXPANSION * blocks[index].compressed_size
            for index in overlapping
        ):
            return None
        output = _stored_archive(archive, kept, size)
        data_start = len(output) - size

        for index in sorted(overlapping):
            block = blocks[index]
            stream.seek(compressed_starts[index])
            data = _decompress(
                stream.read(block.compressed_size), block.uncompressed_size, block.flags
            )
            if data is None or len(data) != block.uncompressed_size:
                return None
            view = memoryview(data)
            start = uncompressed_starts[index]
            for node, target in overlapping[index]:
                lower = max(node.offset, start)
                upper = min(node.offset + node.size, start + block.uncompressed_size)
                at = data_start + target + lower - node.offset
                output[at : at + upper - lower] = view[lower - start : upper - start]
            del data, view
        return output


def load_assets(data: Path) -> Any:
    """The UnityPy Environment for the Unity data folder ``data``.

    Archives that keep streamed bulk data load without it; every other file loads as
    UnityPy loads it.
    """
    import UnityPy
    from UnityPy.environment import reSplit

    env = UnityPy.Environment(path=str(data))
    plain: list[str] = []
    merged_splits: set[str] = set()
    for root, _dirs, files in env.fs.walk(str(data)):
        for name in files:
            path = env.fs.sep.join([root, name])
            split = reSplit.match(path)
            if split:
                if split[1] in merged_splits:
                    continue
                merged_splits.add(split[1])
            slim = without_streamed_data(Path(path))
            if slim is None:
                plain.append(path)
                continue
            env.load_files(plain)
            plain = []
            env.load_file(slim, name=path)
    env.load_files(plain)
    if len(env.files) == 1:
        env.file = next(iter(env.files.values()))
    return env
