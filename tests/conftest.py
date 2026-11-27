"""Suite-wide fixtures."""

import errno
import os
import struct
import sys
import zlib
from pathlib import Path

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.engines.rgssad import MAGIC
from bokucho.project import Project


@pytest.fixture(autouse=True)
def _isolated_bokucho_home(tmp_path_factory, monkeypatch):
    """No test may read or write the real ~/.bokucho."""
    monkeypatch.setenv("BOKUCHO_HOME", str(tmp_path_factory.mktemp("bokucho-home")))


@pytest.fixture(autouse=True)
def _isolated_app_data_dir(tmp_path_factory, monkeypatch):
    """Nor the real application-data directory, which is a different path."""
    variable = "LOCALAPPDATA" if sys.platform == "win32" else "XDG_DATA_HOME"
    monkeypatch.setenv(f"_BOKUCHO_REAL_{variable}", os.environ.get(variable, ""))
    monkeypatch.setenv(variable, str(tmp_path_factory.mktemp("app-data")))


@pytest.fixture(autouse=True)
def _dev_mode_off(monkeypatch):
    """A developer who exports BOKUCHO_DEV for the GUI still runs the suite."""
    monkeypatch.delenv("BOKUCHO_DEV", raising=False)


@pytest.fixture
def new_project(tmp_path: Path):
    """Scaffold a project, which is what nearly every test needs first."""

    def build(
        name: str = "project",
        *,
        root: Path | None = None,
        source_lang: str = "ja",
        target_lang: str = "zh-Hant",
        engine: str = "livemaker",
    ) -> Project:
        return create_project(
            CreateProjectRequest(
                root if root is not None else tmp_path / "project",
                name,
                source_lang,
                target_lang,
                engine,
            )
        )

    return build


@pytest.fixture
def no_rehearsal(monkeypatch):
    """Release a pretend game, which the player's compiler cannot open."""
    from bokucho.application import release

    monkeypatch.setattr(release, "_rehearse", lambda patch, folder, work, token: None)


@pytest.fixture
def make_font():
    """A face that maps characters to empty glyphs: all a font scan reads."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def build(
        path: Path,
        family: str,
        characters: str,
        style: str = "Regular",
        weights: dict[str, int] | None = None,
    ) -> Path:
        """``weights`` makes it a variable face, one named instance per weight."""
        glyphs = [".notdef", *(f"uni{ord(c):04X}" for c in characters)]
        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(glyphs)
        builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in characters})
        empty = TTGlyphPen(None).glyph()
        builder.setupGlyf({glyph: empty for glyph in glyphs})
        builder.setupHorizontalMetrics({glyph: (500, 0) for glyph in glyphs})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable(
            {"familyName": family, "styleName": style, "fullName": f"{family} {style}"}
        )
        builder.setupOS2()
        builder.setupPost()
        if weights:
            builder.setupFvar(
                axes=[("wght", 100, 400, 900, "Weight")],
                instances=[
                    {"location": {"wght": value}, "stylename": name}
                    for name, value in weights.items()
                ],
            )
            builder.setupGvar({})
        path.parent.mkdir(parents=True, exist_ok=True)
        builder.save(str(path))
        return path

    return build


@pytest.fixture
def livemaker_script():
    """Build a LiveMaker script from the statements it runs."""
    pytest.importorskip("livemaker")
    import itertools
    import re

    from livemaker.lsb import LMScript
    from livemaker.lsb.command import (
        Calc,
        Call,
        If,
        Label,
        LabelReference,
        SetProp,
        TextIns,
        VarNew,
    )
    from livemaker.lsb.core import (
        LiveParser,
        LiveParserArray,
        OpeData,
        OpeDataType,
        OpeFuncType,
        Param,
        ParamType,
    )
    from livemaker.lsb.novel import BreakType, TpWord, TWdOpeReturn, TWdOpeVar

    tokens_of = re.compile(r'"[^"]*"|-?\d+|\+\+|==|!=|>=|<=|[()\[\],=&|]|[^\s"()\[\],=+!<>&|]+')
    number = re.compile(r"-?\d+")
    comparisons = {
        "==": OpeDataType.Equal,
        "!=": OpeDataType.NEqual,
        ">=": OpeDataType.EBig,
        "<=": OpeDataType.ESmall,
    }

    def compile_statement(text: str) -> LiveParser:
        tokens = tokens_of.findall(text)
        entries: list = []
        names = itertools.count()

        def emit(kind, operands, func=None, prefix="____") -> Param:
            name = f"{prefix}{next(names)}"
            entries.append(OpeData(kind, name, func, operands))
            return Param(name, ParamType.Var)

        def element(name: str) -> Param:
            tokens.pop(0)
            index = tokens.pop(0)
            tokens.pop(0)
            index = (
                Param(int(index), ParamType.Int)
                if number.fullmatch(index)
                else Param(index, ParamType.Var)
            )
            return emit(OpeDataType.DimTo, [Param(name, ParamType.Var), index], prefix="____d_")

        def term() -> Param:
            token = tokens.pop(0)
            if token == "(":
                value = logical()
                tokens.pop(0)
                return value
            if token.startswith('"'):
                return emit(OpeDataType.To, [Param(token[1:-1], ParamType.Str)])
            if number.fullmatch(token):
                return Param(int(token), ParamType.Int)
            if tokens and tokens[0] == "(":
                tokens.pop(0)
                args = []
                while tokens[0] != ")":
                    args.append(logical())
                    if tokens[0] == ",":
                        tokens.pop(0)
                tokens.pop(0)
                return emit(OpeDataType.Func, args, OpeFuncType[token])
            if tokens and tokens[0] == "[":
                return element(token)
            return Param(token, ParamType.Var)

        def joined() -> Param:
            value = term()
            while tokens and tokens[0] == "++":
                tokens.pop(0)
                value = emit(OpeDataType.ComboStr, [value, term()])
            return value

        def comparison() -> Param:
            value = joined()
            if tokens and tokens[0] in comparisons:
                value = emit(comparisons[tokens.pop(0)], [value, joined()])
            return value

        def logical() -> Param:
            value = comparison()
            while tokens and tokens[0] in ("&", "|"):
                kind = OpeDataType.And if tokens.pop(0) == "&" else OpeDataType.Or
                value = emit(kind, [value, comparison()])
            return value

        target = "____arg"
        if len(tokens) > 1 and tokens[1] == "=":
            target = tokens.pop(0)
            tokens.pop(0)
        elif len(tokens) > 4 and tokens[1] == "[" and tokens[4] == "=":
            target = element(tokens.pop(0)).value
            tokens.pop(0)
        entries.append(OpeData(OpeDataType.To, target, operands=[logical()]))
        return LiveParser(entries=entries)

    class Script:
        statement = staticmethod(compile_statement)

        def __init__(self, name: str) -> None:
            self.lsb = LMScript(call_name=name, commands=[])

        def add(self, cls, **args):
            command = cls(LineNo=len(self.lsb.commands) + 1)
            command.args.update(args)
            self.lsb.commands.append(command)
            return command

        def runs(self, text: str):
            return self.add(Calc, Calc=compile_statement(text))

        def checks(self, text: str):
            return self.add(If, Calc=compile_statement(text))

        def declares_own(self, name: str, value: str):
            return self.add(VarNew, Name=name, InitVal=compile_statement(value), Scope=2)

        def calls(self, callee: str, *params: str):
            return self.add(
                Call,
                Page=LabelReference(callee, 0),
                Calc=compile_statement("1"),
                Params=LiveParserArray([compile_statement(param) for param in params]),
            )

        def sets(self, prop: int, value: str):
            return self.add(
                SetProp,
                ObjName=compile_statement('"部品"'),
                ObjProp=compile_statement(str(prop)),
                Value=compile_statement(value),
            )

        def shows_in_text(self, var: str):
            self.add(Label, Name="前")
            self.add(Label, Name="後")
            body = [TWdOpeVar(var_name=var), TWdOpeReturn(break_type=BreakType.PAGE)]
            return self.add(TextIns, Text=TpWord(body=body))

    return Script


KEY_SEED = 0x12345678
ARCHIVE_KEY = (KEY_SEED * 9 + 3) & 0xFFFFFFFF


def _keystream(key: int, length: int) -> bytes:
    out = bytearray()
    current = key
    while len(out) < length:
        out += struct.pack("<I", current)
        current = (current * 7 + 3) & 0xFFFFFFFF
    return bytes(out[:length])


@pytest.fixture
def build_archive():
    """Write an RGSSAD v3 archive holding ``files`` ({name: bytes})."""

    def build(path, files):
        body = bytearray()
        table = bytearray()
        header = len(MAGIC) + 1 + 4
        table_len = sum(16 + len(name.encode("utf-8")) for name in files) + 16
        cursor = header + table_len
        key_bytes = struct.pack("<I", ARCHIVE_KEY)
        for index, (name, payload) in enumerate(files.items()):
            file_key = 0xABCD0000 + index
            raw = name.encode("utf-8")
            table += struct.pack(
                "<IIII",
                cursor ^ ARCHIVE_KEY,
                len(payload) ^ ARCHIVE_KEY,
                file_key ^ ARCHIVE_KEY,
                len(raw) ^ ARCHIVE_KEY,
            )
            table += bytes(b ^ key_bytes[i % 4] for i, b in enumerate(raw))
            body += bytes(
                a ^ b for a, b in zip(payload, _keystream(file_key, len(payload)), strict=True)
            )
            cursor += len(payload)
        table += struct.pack("<IIII", ARCHIVE_KEY, 0, 0, 0)
        path.write_bytes(
            MAGIC + bytes([3]) + struct.pack("<I", KEY_SEED) + bytes(table) + bytes(body)
        )
        return path

    return build


@pytest.fixture
def build_archive_v1():
    """Write an RGSSAD v1 archive (XP's .rgssad, VX's .rgss2a) holding ``files``."""

    def build(path, files):
        key = 0xDEADCAFE
        out = bytearray(MAGIC + bytes([1]))

        def step(value):
            return (value * 7 + 3) & 0xFFFFFFFF

        for name, payload in files.items():
            raw = name.encode("utf-8")
            out += struct.pack("<I", len(raw) ^ key)
            key = step(key)
            for byte in raw:
                out.append(byte ^ (key & 0xFF))
                key = step(key)
            out += struct.pack("<I", len(payload) ^ key)
            key = step(key)
            out += bytes(a ^ b for a, b in zip(payload, _keystream(key, len(payload)), strict=True))
        path.write_bytes(bytes(out))
        return path

    return build


@pytest.fixture
def archive_key_bytes() -> bytes:
    """The four bytes an archive built here XORs its entry names with."""
    return struct.pack("<I", ARCHIVE_KEY)


@pytest.fixture
def xp_scripts():
    """Build an XP/VX Scripts file from ``(name, source)`` pairs: plain strings, no ivars."""
    pytest.importorskip("rubymarshal")
    import zlib

    from bokucho.engines.rpgmaker_marshal import dump_marshal

    def build(entries: list[tuple[str, str]]) -> bytes:
        return dump_marshal(
            [
                [i, name.encode(), zlib.compress(src.encode())]
                for i, (name, src) in enumerate(entries)
            ]
        )

    return build


@pytest.fixture
def ace_scripts():
    """Build a VX Ace Scripts file: every string `I"` with ivar E, the symbol linked after one."""
    pytest.importorskip("rubymarshal")
    import io
    import zlib

    from rubymarshal.writer import Writer

    def long(n: int) -> bytes:
        out = io.BytesIO()
        Writer(out).write_long(n)
        return out.getvalue()

    def build(entries: list[tuple[str, str]]) -> bytes:
        out = [b"\x04\x08[" + long(len(entries))]
        ivar = b"\x06:\x06ET"
        for i, (name, src) in enumerate(entries):
            out.append(b"[" + long(3) + b"i" + long(i))
            for raw in (name.encode(), zlib.compress(src.encode())):
                out.append(b'I"' + long(len(raw)) + raw + ivar)
                ivar = b"\x06;\x00T"
        return b"".join(out)

    return build


XP3_MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"


def _xp3_chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<Q", len(body)) + body


@pytest.fixture
def xp3_bytes():
    """Hand-build an XP3 archive with a raw index, independent of ``xp3.write``.

    ``files`` is ``[(name, [(zlib?, data), ...]), ...]``; ``cushion`` writes the 2.28+
    header (a raw empty index block that continues to the real one); ``extra`` adds
    top-level index chunks ``(tag, body)``.
    """

    def build(files, *, cushion=False, extra=()):
        head_len = len(XP3_MAGIC) + 8 + (4 + 1 + 8 + 8 if cushion else 0)
        data = bytearray()
        index = bytearray()
        for name, segments in files:
            segm = bytearray()
            size = stored = 0
            for packed, raw in segments:
                body = zlib.compress(raw) if packed else raw
                segm += struct.pack("<IQQQ", int(packed), head_len + len(data), len(raw), len(body))
                data += body
                size += len(raw)
                stored += len(body)
            encoded = name.encode("utf-16-le")
            info = struct.pack("<IQQH", 0, size, stored, len(encoded) // 2) + encoded
            entry = _xp3_chunk(b"info", info) + _xp3_chunk(b"segm", bytes(segm))
            index += _xp3_chunk(b"File", entry)
        for tag, body in extra:
            index += _xp3_chunk(tag, body)
        index_at = head_len + len(data)
        if cushion:
            cushion_block = b"\x80" + struct.pack("<QQ", 0, index_at)
            head = XP3_MAGIC + struct.pack("<QI", 0x17, 1) + cushion_block
        else:
            head = XP3_MAGIC + struct.pack("<Q", index_at)
        return head + bytes(data) + b"\x00" + struct.pack("<Q", len(index)) + bytes(index)

    return build


def _psb_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*map(_psb_keys, value.values()))
    if isinstance(value, list):
        return set().union(*map(_psb_keys, value))
    return set()


def _psb_names_section(names) -> bytes:
    """A test-only writer of the PSB names trie: three packed arrays (charset, tree, tails).

    A node at position ``p`` gets a fresh block of 256 positions the first time it needs a
    child; the child for byte ``b`` sits at ``charset[p] + b`` and points back with
    ``tree[child] = p``. A name ends in a terminator at ``charset[last] + 0``.
    """
    from bokucho.engines import psb

    charset, tree, tails = [0], [0], []

    def child(p: int, byte: int) -> int:
        if not charset[p]:
            charset[p] = len(charset)
            charset.extend([0] * 256)
            tree.extend([0] * 256)
        at = charset[p] + byte
        tree[at] = p
        return at

    for name in names:
        p = 0
        for byte in name.encode("utf-8"):
            p = child(p, byte)
        tails.append(child(p, 0))
    return psb.pack_ints(charset) + psb.pack_ints(tree) + psb.pack_ints(tails)


@pytest.fixture
def psb_file():
    """Encode an invented PSB v3 file holding ``root``; ``names`` defaults to its sorted keys."""
    from bokucho.engines import psb

    def build(root, names=None) -> bytes:
        names = sorted(_psb_keys(root)) if names is None else list(names)
        return psb.encode(psb.Psb(3, tuple(names), _psb_names_section(names), root))

    return build


@pytest.fixture
def scn_tree():
    """An invented ``.scn`` root with one scene; each line is ``(key, (text0, text1, text2))``."""

    def build(*lines, label="*s", title=None, selects=()):
        scene = {"label": label}
        if title is not None:
            scene["title"] = title
        if lines:
            scene["texts"] = [
                [key, [[None, texts[0], len(texts[0])]]
                 + [[key, text, len(text)] for text in texts[1:]], None, 0, {}]
                for key, texts in lines
            ]
        if selects:
            scene["selects"] = list(selects)
        return {"hash": "0", "name": "x", "outlines": [], "scenes": [scene]}

    return build


_STARTUP = {
    "loop": (
        "// invented startup script\n"
        "function useArchiveIfExists(name)\n"
        "{\n"
        "    var arcname = System.exePath + name;\n"
        '    if(Storages.isExistentStorage(arcname)) Storages.addAutoPath(arcname + ">");\n'
        "}\n"
        'useArchiveIfExists("sys/scenario.xp3");\n'
        'useArchiveIfExists("patch.xp3");\n'
        "for(var i = 2; ; i++)\n"
        "{\n"
        '    var name = System.exePath + "patch" + i + ".xp3";\n'
        "    if(!Storages.isExistentStorage(name)) break;\n"
        '    Storages.addAutoPath(name + ">");\n'
        "}\n"
    ),
    "single": 'useArchiveIfExists("patch.xp3");\n',
    "none": 'Storages.addAutoPath(System.exePath + "data/");\n',
}


def _kirikiri_startup(slots: str = "loop") -> bytes:
    """An invented ``Initialize.tjs`` for the patch form ``slots`` (loop, single, none)."""
    return b"\xff\xfe" + _STARTUP[slots].encode("utf-16-le")


@pytest.fixture
def kirikiri_startup():
    """Build an invented ``Initialize.tjs`` for a patch form."""
    return _kirikiri_startup


def _tjs2(constants):
    """An invented compiled TJS2 script whose DATA chunk holds only ``constants``."""
    pool = struct.pack("<6I", 0, 0, 0, 0, 0, len(constants))
    for text in constants:
        raw = text.encode("utf-16-le")
        # The length counts UTF-16 units: a character past U+FFFF takes two.
        pool += struct.pack("<I", len(raw) // 2) + raw + b"\0" * (-len(raw) % 4)
    data = b"DATA" + struct.pack("<I", len(pool)) + pool
    return b"TJS2100\0" + struct.pack("<I", 12 + len(data)) + data


@pytest.fixture
def tjs2_script():
    """Build an invented compiled startup script holding the string constants given."""
    return _tjs2


_NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}


@pytest.fixture
def symlink():
    """Make ``link`` point at ``target``, or skip where the system allows no symbolic link."""

    def make(link: Path, target: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=target.is_dir())
        except OSError as error:
            # Only a system refusing links skips (1314: Windows without the privilege); a
            # missing folder or a name already taken is the test's own mistake.
            if getattr(error, "winerror", None) != 1314 and error.errno not in _NO_LINKS:
                raise
            pytest.skip("this system does not let the test make a symbolic link")

    return make


@pytest.fixture
def kirikiri_game():
    """Write a plain KiriKiri game folder: ``data.xp3`` and any ``extra`` archives."""
    import random

    from bokucho.engines import xp3

    def build(folder, scripts, *, slots="loop", extra=None, padding=0, files=None):
        folder.mkdir(parents=True, exist_ok=True)
        stored = {
            "system/Initialize.tjs": _kirikiri_startup(slots),
            "image/bg.png": b"\x89PNG\r\n\x1a\n" + bytes(8),
        }
        for name, text in scripts.items():
            stored[name] = b"\xff\xfe" + text.encode("utf-16-le")
        stored.update(files or {})
        if padding:
            stored["pad.bin"] = random.Random(1).randbytes(padding)
        xp3.write(folder / "data.xp3", stored)
        for archive, contents in (extra or {}).items():
            (folder / archive).parent.mkdir(parents=True, exist_ok=True)
            xp3.write(folder / archive, contents)
        return folder

    return build
