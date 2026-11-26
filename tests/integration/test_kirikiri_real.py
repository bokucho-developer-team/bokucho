"""End-to-end checks against a real KiriKiri game; nothing is written to its folder."""

from __future__ import annotations

import copy
import fnmatch
import os
import struct
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from bokucho.application.contracts import NeverCancelled, ProgressLevel
from bokucho.application.projects import extract_text
from bokucho.application.release import _kirikiri_patch
from bokucho.engines import psb, scn, xp3
from bokucho.engines.kag import KagRules, decode_script, encode_script, pieces, rewrite
from bokucho.engines.kirikiri import KiriKiriAdapter, game_layout, scenarios
from bokucho.infrastructure.factories import make_engine_adapter
from bokucho.patcher.recipe import compile_recipe
from bokucho.project import Project

GAME = (
    Path(os.environ["BOKUCHO_TEST_KIRIKIRI"]) if os.environ.get("BOKUCHO_TEST_KIRIKIRI") else None
)
SKIP = [s for s in os.environ.get("BOKUCHO_TEST_KIRIKIRI_SKIP", "").split(",") if s]
NAMES = [s for s in os.environ.get("BOKUCHO_TEST_KIRIKIRI_NAMES", "").split(",") if s]
SKIP_IMAGES = [s for s in os.environ.get("BOKUCHO_TEST_KIRIKIRI_SKIP_IMAGES", "").split(",") if s]
RULES = KagRules.from_config(SKIP, NAMES)
COUNTS = dict(
    pair.split("=", 1)
    for pair in os.environ.get("BOKUCHO_TEST_KIRIKIRI_COUNTS", "").split(",")
    if pair
)

pytestmark = pytest.mark.skipif(
    GAME is None, reason="set BOKUCHO_TEST_KIRIKIRI to a KiriKiri game folder"
)


def _snapshot() -> dict[Path, tuple[int, int]]:
    assert GAME is not None
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in GAME.rglob("*")}


@pytest.fixture(autouse=True)
def _game_folder_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "the game folder changed"


def test_every_script_reads_and_rewrites_unchanged():
    layout = game_layout(GAME)
    assert layout.slots in {"loop", "single"}
    found, _ = scenarios(GAME, layout.archives)
    assert found
    for name, source in found.items():
        if name.lower().endswith(".scn"):
            continue
        data = xp3.read(GAME / source.archive, source.entry)
        text = decode_script(data)
        same = {p.id: p.source for p in pieces(text, name, RULES)}
        if data.startswith(b"\xff\xfe"):
            assert encode_script(rewrite(text, name, RULES, same)) == data, name


def _scenes():
    # Every archive at the root, overridden copies included, and those the startup loads
    # from a subfolder.
    names = {p.name.casefold(): p.name for p in GAME.glob("*.xp3")}
    names.update({name.casefold(): name for name in game_layout(GAME).archives})
    for name in sorted(names.values(), key=str.casefold):
        archive = GAME / name
        for entry in xp3.entries(archive):
            if entry.name.lower().endswith(".scn"):
                yield archive, entry, xp3.read(archive, entry)


def test_every_scn_encodes_back_to_the_same_bytes():
    count = 0
    for archive, entry, raw in _scenes():
        assert psb.encode(psb.decode(raw)) == raw, f"{archive.name}: {entry.name}"
        count += 1
    print(f"{count} .scn encode back byte for byte")


def _columns(root):
    for scene in root["scenes"]:
        for text_entry in scene.get("texts") or []:
            for col in text_entry[1] or []:
                if col[1] is not None:
                    yield col


def test_the_column_rule_reproduces_every_stored_column():
    for archive, entry, raw in _scenes():
        for col in _columns(psb.decode(raw).root):
            assert scn.column(col[0], col[1]) == col, f"{archive.name}: {entry.name}"


def test_extract_gives_lines_with_branches_skipped():
    units = KiriKiriAdapter(SKIP, NAMES).extract(GAME)
    assert any(u.kind == "dialogue" for u in units)
    assert not NAMES or any(u.kind == "name" for u in units)
    assert len({u.id for u in units}) == len(units), "ids must be unique"


@pytest.mark.skipif(not COUNTS, reason="set BOKUCHO_TEST_KIRIKIRI_COUNTS to the expected counts")
def test_extract_gives_the_expected_counts_and_warning():
    events = []
    units = KiriKiriAdapter(SKIP, NAMES).extract(GAME, SimpleNamespace(publish=events.append))
    kinds = Counter(u.kind for u in units)
    assert {k: str(kinds[k]) for k in ("dialogue", "name", "choice", "title")} == {
        k: COUNTS[k] for k in ("dialogue", "name", "choice", "title")
    }
    warnings = [e for e in events if e.level is ProgressLevel.WARNING]
    scrambled = int(COUNTS["scrambled"])
    assert [e.detail for e in warnings] == ([f"{scrambled} .ks"] if scrambled else [])


def test_targets_equal_to_sources_write_nothing(tmp_path):
    units = KiriKiriAdapter(SKIP, NAMES).extract(GAME)
    for unit in units:
        unit.target = unit.source
    KiriKiriAdapter(SKIP, NAMES).inject(units, GAME, tmp_path / "out")
    assert not any((tmp_path / "out").iterdir())


def _changes(old, new, path=()):
    """Each differing leaf of two decoded trees, as ``(path, new value)``."""
    if isinstance(old, dict) and isinstance(new, dict):
        assert old.keys() == new.keys(), path
        for key in old:
            yield from _changes(old[key], new[key], (*path, key))
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            assert _is_column(path) and {len(old), len(new)} == {3, 5}, path
        for i in range(max(len(old), len(new))):
            if i < len(old) and i < len(new):
                yield from _changes(old[i], new[i], (*path, i))
            else:
                yield (*path, i), new[i] if i < len(new) else None
    elif type(old) is not type(new) or _bits(old) != _bits(new):
        yield path, new


def _bits(value):
    """A float by its bytes, as the file holds it: NaN is itself, and -0.0 is not 0.0."""
    if isinstance(value, psb.Float32):
        return struct.pack("<f", value)
    if isinstance(value, float):
        return struct.pack("<d", value)
    return value


def _is_column(path):
    return len(path) == 6 and path[0] == "scenes" and path[2] == "texts" and path[4] == 1


def _expected(original, file, marked):
    """The scenario with each marked unit's translation in every slot at its own place, and
    nothing else changed; with the kinds of marked units it holds."""
    target = {unit.id: unit.target for unit in marked.values()}.get
    tree, kinds = copy.deepcopy(original), set()
    for scene in tree["scenes"]:
        label = scene["label"].removeprefix("*")
        title = scene.get("title")
        first = title[0] if isinstance(title, list) and title else title
        if first and (text := target(scn.TITLE_PREFIX + first)):
            scene["title"] = [text] * len(title) if isinstance(title, list) else text
            kinds.add("title")
        for n, entry in enumerate(scene.get("texts") or []):
            columns = entry[1] or []
            speaker = columns[0][0] or entry[0] if entry[0] and columns else None
            if speaker and (text := target(scn.NAME_PREFIX + speaker)):
                for col in columns:
                    col[0] = text
                kinds.add("name")
            # A translated line gets the count of its new text, written out here rather than
            # by the rule under test: a plain target is three items, its length shown.
            if columns and columns[0][1] and (text := target(f"{file}:{label}:{n}")):
                assert "\\" not in text and "[" not in text, "the marks are plain text"
                entry[1] = [[col[0], text, len(text)] for col in columns]
                kinds.add("dialogue")
        for j, select in enumerate(scene.get("selects") or []):
            shown = select.get("text")
            if isinstance(shown, str) and shown and (text := target(f"{file}:{label}:choice{j}")):
                select["text"] = text
                for slot in select.get("language") or []:
                    if isinstance(slot, dict):
                        slot["text"] = text
                kinds.add("choice")
    return tree, kinds


def test_the_recipe_compiles_into_scratch_only(tmp_path, new_project):
    project = new_project(engine="kirikiri", root=tmp_path / "project")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={
                        "game_path": str(GAME),
                        "kag_skip_branches": SKIP,
                        "kag_name_attributes": NAMES,
                    }
                )
            }
        )
    )
    project = Project.load(project.root)
    extract_text(project, make_engine_adapter(project))
    # The first dialogue, name, choice and title, where the game has them.
    marked = {}
    for string_file in project.load_string_files():
        for unit in string_file.units:
            if unit.kind in ("dialogue", "name", "choice", "title") and unit.kind not in marked:
                unit.target = f"BOKUCHO TEST {len(marked) + 1}"
                marked[unit.kind] = unit
        project.save_string_file(string_file, f"{string_file.source_file}.json")
    assert "dialogue" in marked
    work = tmp_path / "work"
    work.mkdir()
    patch = _kirikiri_patch(project, work, NeverCancelled())
    rebuilt, added = compile_recipe(GAME, patch, tmp_path / "root")
    assert rebuilt == {}
    (archive,) = added
    built = tmp_path / archive.name
    built.write_bytes(archive.data)
    entries = {entry.name: entry for entry in xp3.entries(built)}
    found, _ = scenarios(GAME, game_layout(GAME).archives)
    script = marked["dialogue"].engine_ref["file"]
    assert script in entries and set(entries) <= set(found)
    written = []
    for name, entry in entries.items():
        data = xp3.read(built, entry)
        if name.lower().endswith(".scn"):
            tree = psb.decode(data).root
            # Every column, rewritten or not, keeps the count and forms the game expects.
            for col in _columns(tree):
                assert scn.column(col[0], col[1]) == col, name
            written += scn.pieces(tree, name)
        else:
            written += pieces(decode_script(data), name, RULES)
    # Every scenario the game loads: one holding a marked unit is in the patch as expected,
    # and one holding none is left out.
    hits = set()
    for name, source in found.items():
        if name.lower().endswith(".scn"):
            original = psb.decode(xp3.read(GAME / source.archive, source.entry)).root
            expected, kinds = _expected(original, name, marked)
            hits |= kinds
            assert (name in entries) == any(_changes(original, expected)), name
            tree = psb.decode(xp3.read(built, entries[name])).root if name in entries else original
            assert not list(_changes(expected, tree)), name
    line = marked["dialogue"]
    assert any(p.id == line.id and p.source == line.target for p in written)
    # A rewritten name or title has a new id, so those are found by kind and text.
    for unit in marked.values():
        assert any(p.kind == unit.kind and p.source == unit.target for p in written), unit.kind
        # A title is only in scenarios; a line or choice there names its scenario.
        if unit.kind == "title" or unit.engine_ref["file"].lower().endswith(".scn"):
            assert unit.kind in hits, unit.kind


def test_pictures_unpack_from_the_chosen_folders_only(tmp_path):
    KiriKiriAdapter.unpack_images(GAME, tmp_path / "pictures", skip_images=SKIP_IMAGES)
    names = [
        p.relative_to(tmp_path / "pictures").as_posix()
        for p in (tmp_path / "pictures").rglob("*")
        if p.is_file()
    ]
    # The game finds a picture by file name alone, so the copy unpacked may be a later
    # archive's, outside the chosen folders; its name is still one a chosen folder holds.
    chosen = {
        entry.name.rsplit("/", 1)[-1].casefold()
        for archive in game_layout(GAME).archives
        for entry in xp3.entries(GAME / archive)
        if "/" in entry.name and entry.name.split("/")[0].casefold() in {"image", "system", "rule"}
    }
    assert {n.rsplit("/", 1)[-1].casefold() for n in names} <= chosen
    # Nothing unpacked is right only when no PNG, JPG or BMP is left after the skip globs,
    # as in a game keeping its pictures as TLG.
    eligible = [
        n
        for n in chosen
        if n.endswith((".png", ".jpg", ".jpeg", ".bmp"))
        and not any(fnmatch.fnmatchcase(n, g.casefold()) for g in SKIP_IMAGES)
    ]
    assert names or not eligible
    assert not [
        n
        for n in names
        if any(
            fnmatch.fnmatchcase(n.rsplit("/", 1)[-1].casefold(), g.casefold()) for g in SKIP_IMAGES
        )
    ]
    print(f"unpacked {len(names)} pictures")
