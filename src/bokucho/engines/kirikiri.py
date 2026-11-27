"""KiriKiri / KAG games: plain ``.xp3`` archives holding ``.ks`` or ``.scn`` scenarios.

The game finds a storage by file name alone, and the archive it loaded last wins, so the
copy of each scenario that counts is the one in the last loaded archive that holds it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressLevel,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.engines import kag, psb, scn, xp3
from bokucho.engines.base import EngineAdapter, unpacked_from, unpacked_stamp
from bokucho.model import Context, TextUnit
from bokucho.patcher.core import added_files
from bokucho.patcher.payload import safe_parts

NAMES_FILE = "speakers"
DATA = "data.xp3"
CATEGORIES = {
    3: "encrypted file contents",
    4: "a publisher's own archive index",
}
_STARTUP_FIRST = "system/initialize.tjs"
_USE_ARCHIVE = re.compile(r"""useArchiveIfExists\(\s*(["'])([^"'\r\n]+)\1\s*\)""")
_PATCH_LOOP = re.compile(r"""(["'])patch\1\s*\+\s*[A-Za-z_]\w*\s*\+\s*(["'])\.xp3""", re.I)
_PATCH = re.compile(r"patch([1-9][0-9]*)?\.xp3", re.I)
_IMAGE_MAGIC = {
    ".png": b"\x89PNG",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".bmp": b"BM",
    ".tlg": b"TLG",
}
IMAGE_FOLDERS = ("image", "system", "rule")
_PICTURES = (".png", ".jpg", ".jpeg", ".bmp")
_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


class UnsupportedCategory(InvalidProject):
    """A KiriKiri game of a kind this version cannot translate yet."""

    def __init__(self, category: int, where: str) -> None:
        super().__init__(
            f"{where}: KiriKiri category {category} ({CATEGORIES[category]}) is not supported yet."
        )
        self.category = category


class SlotRefused(ValueError):
    """No patch archive name the game would load last is free (message for the player)."""


@dataclass(frozen=True, slots=True)
class GameLayout:
    """The archives the game loads, in load order, and how it loads patch archives."""

    folder: Path
    archives: tuple[str, ...]
    slots: str  # "loop", "single", or "" when the game loads no patch archive
    #: Archives loaded in an order Bokucho cannot read; ``archives`` lists them in name order.
    unordered: tuple[str, ...] = ()


class ScriptSource(NamedTuple):
    archive: str
    entry: xp3.Entry


def game_folder(game_path: Path) -> Path:
    """The game's folder, for a configured path that is the folder or its executable."""
    path = Path(game_path)
    return path if path.is_dir() else path.parent


def _parts(relative: str) -> list[str]:
    return [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]


def _is_patch_slot(name: str) -> bool:
    """Whether an archive name is a patch slot: patch.xp3 or patchN.xp3 in the game folder."""
    return bool(_PATCH.fullmatch("/".join(_parts(name))))


def _find(folder: Path, relative: str) -> str | None:
    """``relative`` as the folder spells it, matched in any case; ``None`` when missing."""
    current = folder
    parts: list[str] = []
    for part in _parts(relative):
        if part == "..":
            return None
        try:
            names = sorted(child.name for child in current.iterdir())
        except OSError:
            return None
        if part not in names:
            part = next((name for name in names if name.casefold() == part.casefold()), "")
            if not part:
                return None
        parts.append(part)
        current = current / part
        # A link takes the read outside the game folder (the folder itself may be one).
        if current.is_symlink() or current.is_junction():
            raise InvalidProject(
                f"{folder}: {'/'.join(parts)} is a link, and Bokucho reads a game's archives "
                "only from inside its folder. Move the real file or folder there."
            )
    return "/".join(parts) if parts and current.is_file() else None


def _entries(folder: Path, archive: str) -> list[xp3.Entry]:
    try:
        return xp3.entries(folder / archive)
    except xp3.CustomIndex as error:
        raise UnsupportedCategory(4, archive) from error
    except (xp3.NotAnArchive, OSError) as error:
        raise InvalidProject(f"{archive} cannot be read: {error}") from error


def _read(folder: Path, archive: str, entry: xp3.Entry) -> bytes:
    try:
        return xp3.read(folder / archive, entry)
    except (xp3.NotAnArchive, OSError) as error:
        raise InvalidProject(f"{archive} cannot be read: {error}") from error


def _text(folder: Path, archive: str, entry: xp3.Entry) -> str:
    try:
        return kag.decode_script(_read(folder, archive, entry))
    except kag.ScrambledText as error:
        raise UnsupportedCategory(3, f"{archive}: {entry.name}") from error


def _constants(data: bytes) -> list[str] | None:
    """The string constants of a compiled TJS2 script, or ``None`` when they cannot be read."""
    if data[:4] != b"TJS2" or data[12:16] != b"DATA":
        return None
    try:
        at = 20
        for size in (1, 2, 4, 8, 8):  # bytes, shorts, ints, longs, doubles
            (count,) = struct.unpack_from("<I", data, at)
            at += 4 + count * size
            at += -at % 4
        (count,) = struct.unpack_from("<I", data, at)
        at += 4
        found = []
        for _ in range(count):
            (length,) = struct.unpack_from("<I", data, at)
            text = data[at + 4 : at + 4 + 2 * length]
            if len(text) != 2 * length:
                return None
            found.append(text.decode("utf-16-le"))
            at += 4 + 2 * length + (-2 * length % 4)
        return found
    except (struct.error, UnicodeDecodeError):
        return None


def _startup(folder: Path, data: str) -> tuple[list[str], bool, bool]:
    """The archives the startup script names, whether it runs the patch loop, and whether
    it was compiled. A scrambled script is skipped, and so is a compiled one naming no
    archive, though the startup still counts as compiled when no other script names one."""
    scripts = sorted(
        (entry for entry in _entries(folder, data) if entry.name.lower().endswith(".tjs")),
        key=lambda entry: entry.name.lower() != _STARTUP_FIRST,
    )
    compiled = False
    for entry in scripts:
        raw = _read(folder, data, entry)
        if raw.startswith(b"TJS2"):
            compiled = True
            constants = _constants(raw) or []
            folded = {constant.casefold() for constant in constants}
            names = [c for c in constants if (f := c.casefold()).endswith(".xp3") and f != ".xp3"]
            # A constant can sit in a message too: only a script that mounts archives names
            # the ones it loads, and only one that also asks whether a file exists can loop
            # over numbered patch archives.
            if names and "addautopath" in folded:
                return names, {"patch", ".xp3", "isexistentstorage"} <= folded, True
            continue
        try:
            text = kag.decode_script(raw)
        except kag.ScrambledText:
            continue
        names = [match.group(2) for match in _USE_ARCHIVE.finditer(text)]
        if names:
            return names, bool(_PATCH_LOOP.search(text)), False
    return [], False, compiled


def _refuse_unordered(folder: Path, archives: Sequence[str]) -> None:
    """Refuse a scenario name held by two archives whose load order is unknown."""
    seen: dict[str, tuple[str, str]] = {}
    for archive in archives:
        here = {}
        for entry in _entries(folder, archive):
            if entry.name.lower().endswith((".ks", ".scn")):
                base = entry.name.rsplit("/", 1)[-1]
                here.setdefault(base.casefold(), base)
        for key, base in here.items():
            if key in seen:
                raise InvalidProject(
                    f"{folder}: {seen[key][1]} is in both {seen[key][0]} and {archive}, and the "
                    "order the game loads them in is unknown; this layout is not supported."
                )
            seen[key] = (archive, base)


def _data_archive(folder: Path) -> str:
    data = _find(folder, DATA)
    if data is None:
        raise InvalidProject(f"{folder}: there is no {DATA}, so this is not a KiriKiri game.")
    return data


def game_layout(folder: Path) -> GameLayout:
    """Which archives the game in ``folder`` loads, in the order it loads them."""
    data = _data_archive(folder)
    names, loop, compiled = _startup(folder, data)
    # An archive Bokucho's own patch added holds translations, not the game's text.
    try:
        ours = {name.casefold() for name in added_files(folder)}
    except ValueError as error:
        raise InvalidProject(
            f"{folder}: {error}. Which archive a Bokucho patch added is unknown, so the "
            "game's own files cannot be told from a translation. Restore the game's "
            "original files first."
        ) from None
    archives = [data]

    def add(found: str | None) -> None:
        if found is not None and found.casefold() not in ours | {a.casefold() for a in archives}:
            archives.append(found)

    slotted = any("/".join(_parts(name)).casefold() == "patch.xp3" for name in names)
    unordered: tuple[str, ...] = ()
    if compiled:
        # A compiled startup mounts the archives beside data.xp3 itself (an append list
        # Bokucho cannot read), so every one of them counts, in an order that is unknown.
        # It can also name one in a subfolder. Patch slots come after, in slot order, when
        # the startup loads them as patch slots; otherwise they are like any other archive.
        siblings = sorted(
            (c.name for c in folder.iterdir() if c.is_file() and c.suffix.casefold() == ".xp3"),
            key=str.casefold,
        )
        for name in [*siblings, *names]:
            if not (slotted and _is_patch_slot(name)):
                add(_find(folder, name))
        unordered = tuple(archives[1:])
        _refuse_unordered(folder, unordered)
    else:
        for name in names:
            add(_find(folder, name))
    slots = ""
    if slotted:
        slots = "loop" if loop else "single"
        if compiled:
            add(_find(folder, "patch.xp3"))
    if slots == "loop":
        number = 2
        while (found := _find(folder, f"patch{number}.xp3")) is not None:
            add(found)
            number += 1
    return GameLayout(folder, tuple(archives), slots, unordered)


def patch_slot(folder: Path, slots: str) -> str:
    """The patch archive name the game in ``folder`` would load last, for ``slots``."""
    present: dict[int, str] = {}
    for child in sorted(folder.iterdir()):
        match = _PATCH.fullmatch(child.name)
        if match and child.is_file():
            present.setdefault(int(match.group(1) or 0), child.name)
    if slots == "single":
        if 0 in present:
            raise SlotRefused(
                f"遊戲資料夾裡已經有 {present[0]}。這款遊戲只會載入一個補丁封包，"
                "所以無法再加入翻譯補丁。"
            )
        return "patch.xp3"
    if slots != "loop":
        raise SlotRefused("這款遊戲不會載入補丁封包（patch.xp3），所以無法套用翻譯補丁。")
    numbered = {number: name for number, name in present.items() if number >= 2}
    if 0 not in present and not numbered:
        return "patch.xp3"
    number = 2
    while number in numbered:
        number += 1
    later = sorted(n for n in numbered if n > number)
    if later:
        raise SlotRefused(
            f"遊戲資料夾裡有 {numbered[later[0]]}，但前面缺少 patch{number}.xp3，"
            "所以遊戲目前不會載入它。加入翻譯補丁會讓遊戲開始載入它，因此不套用。"
        )
    return f"patch{number}.xp3"


def scripts(
    folder: Path, archives: Sequence[str], token: CancellationToken = _NEVER_CANCELLED
) -> dict[str, ScriptSource]:
    """Each ``.ks`` and ``.scn`` the game reads, by basename, from the last archive holding it."""
    chosen: dict[str, tuple[str, ScriptSource]] = {}
    for archive in archives:
        token.raise_if_cancelled()
        here: dict[str, str] = {}
        for entry in _entries(folder, archive):
            if not entry.name.lower().endswith((".ks", ".scn")):
                continue
            base = entry.name.rsplit("/", 1)[-1]
            try:
                safe_parts(base)
            except ValueError:
                raise InvalidProject(
                    f"{archive}: {entry.name} is not a plain file name, so its translation "
                    "cannot be written out; this layout is not supported."
                ) from None
            key = base.casefold()
            if key in here:
                raise InvalidProject(
                    f"{archive}: {here[key]} and {entry.name} share one file name, and the "
                    "game finds scenarios by name alone; this layout is not supported."
                )
            here[key] = entry.name
            chosen[key] = (base, ScriptSource(archive, entry))
    return dict(chosen.values())


def _is_scene(base: str) -> bool:
    return base.lower().endswith(".scn")


def scenarios(
    folder: Path, archives: Sequence[str], token: CancellationToken = _NEVER_CANCELLED
) -> tuple[dict[str, ScriptSource], int]:
    """The scripts the adapter reads, and how many scrambled ``.ks`` it left out.

    Beside ``.scn`` scenarios a scrambled ``.ks`` is left out (a later version reads them);
    in a game of ``.ks`` alone it is category 3.
    """
    found = scripts(folder, archives, token)
    any_scene = any(map(_is_scene, found))
    kept: dict[str, ScriptSource] = {}
    skipped = 0
    for base, source in found.items():
        if not _is_scene(base):
            token.raise_if_cancelled()
            try:
                kag.decode_script(_read(folder, *source))
            except kag.ScrambledText as error:
                if not any_scene:
                    where = f"{source.archive}: {source.entry.name}"
                    raise UnsupportedCategory(3, where) from error
                skipped += 1
                continue
        kept[base] = source
    return kept, skipped


def scenario_archives(
    folder: Path, archives: Sequence[str], token: CancellationToken = _NEVER_CANCELLED
) -> list[str]:
    """The archives, in load order, holding any copy of a scenario the adapter reads.

    The patcher reads these alone, so a script left out that has a copy among them has every
    archive holding it listed too: the patcher then finds the copy the game reads, and
    leaves it out as well.
    """
    held: dict[str, set[str]] = {}
    for archive in archives:
        token.raise_if_cancelled()
        held[archive] = {e.name.rsplit("/", 1)[-1].casefold() for e in _entries(folder, archive)}
    names = {base.casefold() for base in scenarios(folder, archives, token)[0]}
    left_out = {base.casefold() for base in scripts(folder, archives, token)} - names
    while True:
        listed = [archive for archive in archives if held[archive] & names]
        more = {base for base in left_out if any(base in held[a] for a in listed)}
        if not more:
            return listed
        names |= more
        left_out -= more


def _scene(folder: Path, archive: str, entry: xp3.Entry, data: bytes | None = None) -> psb.Psb:
    if data is None:
        data = _read(folder, archive, entry)
    try:
        decoded = psb.decode(data)
        same = psb.encode(decoded) == data
    except psb.Encrypted as error:
        raise UnsupportedCategory(3, f"{archive}: {entry.name}") from error
    except psb.PsbError as error:
        raise InvalidProject(f"{archive}: {entry.name}: {error}") from error
    if not same:
        raise InvalidProject(
            f"{archive}: {entry.name}: Bokucho cannot write this .scn back byte for byte, so "
            "this game is not supported yet."
        )
    return decoded


def _scene_pieces(folder: Path, source: ScriptSource, base: str) -> list[scn.Piece]:
    try:
        return scn.pieces(_scene(folder, *source).root, base)
    except scn.UnknownShape as error:
        raise InvalidProject(f"{source.archive}: {source.entry.name}: {error}") from error


def _name_unit(piece_id: str, source: str) -> TextUnit:
    return TextUnit(id=piece_id, source=source, kind="name", engine_ref={"file": NAMES_FILE})


def _check_images(folder: Path, archive: str) -> None:
    for entry in _entries(folder, archive):
        magic = _IMAGE_MAGIC.get(PurePosixPath(entry.name).suffix.lower())
        if magic is not None:
            if not _read(folder, archive, entry).startswith(magic):
                raise UnsupportedCategory(3, f"{archive}: {entry.name}")
            return


class KiriKiriAdapter(EngineAdapter):
    """Extract and inject the text of ``.ks`` and ``.scn`` scenarios in plain ``.xp3`` archives."""

    name = "kirikiri"

    def __init__(
        self,
        skip_branches: Sequence[str] = (),
        name_attributes: Sequence[str] = (),
        archives: Sequence[str] | None = None,
        skip_labels: Sequence[str] = (),
    ) -> None:
        self.rules = kag.KagRules.from_config(skip_branches, name_attributes, skip_labels)
        self.archives = None if archives is None else tuple(archives)

    @classmethod
    def game_root(cls, game_path: Path) -> Path | None:
        """``None``: a KiriKiri game keeps its pictures inside its archives."""
        return None

    @classmethod
    def packed_archive(cls, game_path: Path) -> Path:
        """The data archive, whose name the unpacked pictures' folder takes."""
        folder = game_folder(game_path)
        return folder / _data_archive(folder)

    @classmethod
    def unpack_images(
        cls,
        game_path: Path,
        destination: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
        *,
        image_folders: Sequence[str] | None = None,
        skip_images: Sequence[str] = (),
    ) -> None:
        """Write the pictures of the chosen folders, as the game loads them, to ``destination``.

        The game finds a picture by file name alone, so the copy written is the one in the
        last loaded archive, even when that copy sits at the archive's root. A name whose
        last copies sit in archives loaded in an unknown order is left out with a warning.
        """
        folder = game_folder(game_path)
        layout = game_layout(folder)
        archives = layout.archives
        folders = IMAGE_FOLDERS if image_folders is None else tuple(image_folders)
        globs = tuple(skip_images)
        loaded = [(a, (s := (folder / a).stat()).st_size, s.st_mtime_ns) for a in archives]
        stamp = hashlib.sha256(json.dumps([loaded, folders, globs]).encode()).hexdigest()
        if unpacked_from(destination, stamp):
            return
        wanted = {name.casefold() for name in folders}
        unordered = set(layout.unordered)
        last: dict[str, tuple[str, xp3.Entry]] = {}
        chosen: set[str] = set()
        clash: dict[str, str] = {}
        for archive in archives:
            here: dict[str, str] = {}
            for entry in _entries(folder, archive):
                if PurePosixPath(entry.name).suffix.lower() not in _PICTURES:
                    continue
                key = entry.name.rsplit("/", 1)[-1].casefold()
                earlier = last.get(key)
                if key in here:
                    if here[key].rpartition("/")[0].casefold() != (
                        entry.name.rpartition("/")[0].casefold()
                    ):
                        clash[key] = f"{archive}: {here[key]} and {entry.name} share one file name"
                elif earlier and {earlier[0], archive} <= unordered:
                    clash[key] = (
                        f"{earlier[0]}: {earlier[1].name} and {archive}: {entry.name} share one "
                        "file name, and the order the game loads them in is unknown"
                    )
                else:
                    clash.pop(key, None)  # the last archive holding a name decides it
                here[key] = entry.name
                last[key] = (archive, entry)
                parts = entry.name.split("/")
                if len(parts) > 1 and parts[0].casefold() in wanted:
                    chosen.add(key)
        names = sorted(chosen)
        staging = destination.with_name(f"{destination.name}.partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        def skipped(index: int, detail: str) -> None:
            sink.publish(
                ProgressEvent(
                    "engine.unpack",
                    index,
                    len(names),
                    "engine.unpack.entry.skipped",
                    level=ProgressLevel.WARNING,
                    detail=detail,
                )
            )

        for index, key in enumerate(names, start=1):
            token.raise_if_cancelled()
            archive, entry = last[key]
            if key in clash:
                skipped(index, clash[key])
                continue
            if any(fnmatchcase(key, glob.casefold()) for glob in globs):
                continue
            try:
                parts = safe_parts(entry.name)
            except ValueError:
                skipped(index, f"{archive}: {entry.name} is not a plain file path")
                continue
            data = _read(folder, archive, entry)
            if not data.startswith(_IMAGE_MAGIC[PurePosixPath(entry.name).suffix.lower()]):
                raise UnsupportedCategory(3, f"{archive}: {entry.name}")
            target = staging.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            sink.publish(
                ProgressEvent(
                    "engine.unpack",
                    index,
                    len(names),
                    "engine.unpack.progress",
                    detail=entry.name,
                )
            )
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        unpacked_stamp(destination).write_text(stamp, encoding="ascii")

    def _archives(self, folder: Path) -> tuple[str, ...]:
        if self.archives is None:
            return game_layout(folder).archives
        found = []
        for name in self.archives:
            spelled = _find(folder, name)
            if spelled is None:
                raise InvalidProject(f"{folder}: {name} is missing; the patch was made with it.")
            found.append(spelled)
        return tuple(found)

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        folder = game_folder(game_path)
        archives = self._archives(folder)
        found, skipped = scenarios(folder, archives, token)
        if not found:
            raise InvalidProject(f"{folder}: the game's archives hold no .ks or .scn scenarios.")
        _check_images(folder, archives[0])
        if skipped:
            sink.publish(
                ProgressEvent(
                    "engine.extract",
                    0,
                    None,
                    "engine.extract.scrambled.skipped",
                    level=ProgressLevel.WARNING,
                    detail=f"{skipped} .ks",
                )
            )
        lines: list[TextUnit] = []
        names: dict[str, TextUnit] = {}
        titles: dict[str, TextUnit] = {}
        for base in sorted(found):
            token.raise_if_cancelled()
            if _is_scene(base):
                for scene_piece in _scene_pieces(folder, found[base], base):
                    if scene_piece.kind == "name":
                        names.setdefault(
                            scene_piece.id, _name_unit(scene_piece.id, scene_piece.source)
                        )
                    elif scene_piece.kind == "title":
                        titles.setdefault(
                            scene_piece.id,
                            TextUnit(
                                id=scene_piece.id,
                                source=scene_piece.source,
                                kind="title",
                                engine_ref={"file": scn.TITLES_FILE},
                            ),
                        )
                    else:
                        lines.append(
                            TextUnit(
                                id=scene_piece.id,
                                source=scene_piece.source,
                                kind=scene_piece.kind,
                                context=Context(speaker=scene_piece.speaker),
                                engine_ref={"file": base},
                            )
                        )
                continue
            text = _text(folder, *found[base])
            for piece in kag.pieces(text, base, self.rules):
                if piece.kind == "name":
                    names.setdefault(piece.id, _name_unit(piece.id, piece.source))
                else:
                    lines.append(
                        TextUnit(
                            id=piece.id,
                            source=piece.source,
                            kind="dialogue",
                            context=Context(speaker=piece.speaker),
                            engine_ref={"file": base},
                        )
                    )
        return lines + list(names.values()) + list(titles.values())

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        folder = game_folder(game_path)
        found, _ = scenarios(folder, self._archives(folder), token)
        targets = {unit.id: unit.target for unit in units if unit.target.strip()}
        edits = [u for u in units if u.target.strip() and u.target != u.source]
        files = {u.engine_ref.get("file") for u in edits}
        # Names and titles are shared by every scenario showing them, so they are looked
        # for in the raw bytes, where each string sits whole as UTF-8.
        shared = [u.source.encode("utf-8") for u in edits if u.kind in ("name", "title")]
        out_path.mkdir(parents=True, exist_ok=True)
        for base in sorted(found):
            token.raise_if_cancelled()
            if _is_scene(base):
                source = found[base]
                data = _read(folder, *source)
                # Decoding is nearly all the time a patch takes; one with nothing to change
                # is left unread.
                if base not in files and not any(text in data for text in shared):
                    continue
                decoded = _scene(folder, *source, data)
                try:
                    changed = scn.rewrite(decoded.root, base, targets)
                except kag.UnsafeTarget as error:
                    raise InvalidProject(str(error)) from error
                except scn.UnknownShape as error:
                    where = f"{source.archive}: {source.entry.name}"
                    raise InvalidProject(f"{where}: {error}") from error
                if changed:
                    try:
                        rebuilt = psb.encode(decoded)
                    except psb.PsbError as error:
                        raise InvalidProject(
                            f"{source.archive}: {source.entry.name}: {error}"
                        ) from error
                    (out_path / base).write_bytes(rebuilt)
                continue
            text = _text(folder, *found[base])
            try:
                new = kag.rewrite(text, base, self.rules, targets)
            except kag.UnsafeTarget as error:
                raise InvalidProject(str(error)) from error
            if new != text:
                (out_path / base).write_bytes(kag.encode_script(new))
