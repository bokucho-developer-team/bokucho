"""A KiriKiri patch is compiled on the player's PC into an archive added beside the game's."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from bokucho.application.contracts import CancellationFlag
from bokucho.application.errors import JobCancelled
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.engines import psb, xp3
from bokucho.engines.kag import decode_script, encode_script
from bokucho.engines.kirikiri import KiriKiriAdapter, game_layout
from bokucho.model import StringFile
from bokucho.patcher import core
from bokucho.patcher.payload import FileState, Patch, PatchedFile
from bokucho.patcher.recipe import compile_recipe
from bokucho.project import Project


def _patch(
    project: Project,
    game: Path,
    pictures: dict[str, bytes] | None = None,
    archives: list[str] | None = None,
) -> Patch:
    layout = game_layout(game)
    archives = list(layout.archives) if archives is None else archives
    engine = project.config.engine
    recipe = {
        "engine": "kirikiri",
        "name": project.config.name,
        "source_lang": project.config.source_lang,
        "target_lang": project.config.target_lang,
        "archives": archives,
        "slots": layout.slots,
        "kag_skip_branches": engine.kag_skip_branches or [],
        "kag_name_attributes": engine.kag_name_attributes or [],
    }
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("recipe.json", json.dumps(recipe, ensure_ascii=False))
        for path in sorted(project.strings_dir.glob("*.json")):
            archive.writestr(f"strings/{path.name}", path.read_bytes())
        for path, data in (pictures or {}).items():
            archive.writestr(f"out/{path}", data)
    files = tuple(
        PatchedFile(
            name,
            FileState((game / name).stat().st_size, core.sha256_of(game / name)),
            None,
            b"",
            0,
        )
        for name in archives
    )
    return Patch("test", files, recipe=buffer.getvalue())


def _translated(
    tmp_path: Path, kirikiri_game, scripts: dict[str, str] | None = None, **game_options
) -> tuple[Project, Path]:
    scripts = {"scenario/first.ks": "おはよう\n"} if scripts is None else scripts
    game = kirikiri_game(tmp_path / "game", scripts, **game_options)
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Sample", "ja", "zh-Hant", "kirikiri")
    )
    project.config.engine.game_path = str(game)
    units = KiriKiriAdapter().extract(game)
    units[0].target = "早安"
    project.save_string_file(StringFile(source_file="first.ks", units=units), "first.ks.json")
    return project, game


@pytest.fixture
def translated(tmp_path, kirikiri_game):
    return _translated(tmp_path, kirikiri_game)


def _untranslate(project: Project) -> None:
    for strings in project.load_string_files():
        for unit in strings.units:
            unit.target = ""
        project.save_string_file(strings, f"{strings.source_file}.json")


def _compiled(game, patch, scratch):
    _rebuilt, (added,) = compile_recipe(game, patch, scratch)
    archive = scratch / "added.xp3"
    archive.write_bytes(added.data)
    return {e.name: xp3.read(archive, e) for e in xp3.entries(archive)}


def test_the_patch_archive_holds_the_pictures_at_its_root(translated, tmp_path):
    project, game = translated
    patch = _patch(project, game, {"image/title.png": b"T", "rule/op.jpg": b"O"})
    files = _compiled(game, patch, tmp_path / "scratch")
    assert sorted(files) == ["first.ks", "op.jpg", "title.png"]
    assert files["title.png"] == b"T" and files["op.jpg"] == b"O"


def test_pictures_alone_make_a_patch(translated, tmp_path):
    project, game = translated
    _untranslate(project)
    files = _compiled(game, _patch(project, game, {"image/title.png": b"T"}), tmp_path / "s")
    assert files == {"title.png": b"T"}


def test_a_cancelled_rehearsal_stops_before_it_injects(translated, tmp_path):
    project, game = translated
    flag = CancellationFlag()
    flag.cancel()
    with pytest.raises(JobCancelled):
        compile_recipe(game, _patch(project, game), tmp_path / "s", token=flag)


def test_nothing_translated_and_no_picture_is_refused(translated, tmp_path):
    project, game = translated
    _untranslate(project)
    with pytest.raises(ValueError, match="Nothing to patch: no translated line or picture."):
        compile_recipe(game, _patch(project, game), tmp_path / "s")


def test_two_pictures_with_one_name_are_refused(translated, tmp_path):
    project, game = translated
    patch = _patch(project, game, {"image/a.png": b"1", "rule/A.png": b"2"})
    with pytest.raises(ValueError, match="image/a.png.*rule/A.png|rule/A.png.*image/a.png"):
        compile_recipe(game, patch, tmp_path / "s")


def test_applying_adds_a_patch_archive_and_leaves_the_game_archive(translated):
    project, game = translated
    before = (game / "data.xp3").read_bytes()
    core.apply(game, _patch(project, game))
    assert (game / "data.xp3").read_bytes() == before
    assert not (game / "data.xp3.bak").exists()
    (entry,) = xp3.entries(game / "patch.xp3")
    assert entry.name == "first.ks"
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == "早安\n"


def test_an_applied_game_reads_as_applied_and_is_not_applied_twice(translated):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    assert core.inspect(game, patch) == core.FolderState(core.State.PATCHED, True)
    with pytest.raises(core.PatchError, match="已經套用"):
        core.apply(game, patch)


def test_a_removed_patch_archive_reads_as_original_again(translated):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    (game / "patch.xp3").unlink()
    assert core.inspect(game, patch).state is core.State.ORIGINAL


def test_a_record_left_without_its_archive_is_cleared_by_restore(translated):
    # The record is written first: a process stopped before the archive leaves only it,
    # and a release refuses a game folder holding one.
    project, game = translated
    names = sorted(p.name for p in game.iterdir())
    patch = _patch(project, game)
    core.apply(game, patch)
    (game / "patch.xp3").unlink()
    assert core.inspect(game, patch) == core.FolderState(core.State.ORIGINAL, True)
    assert core.restore(game, patch) == []
    assert sorted(p.name for p in game.iterdir()) == names


def test_restoring_takes_the_archive_away(translated):
    project, game = translated
    names = sorted(p.name for p in game.iterdir())
    patch = _patch(project, game)
    core.apply(game, patch)
    assert core.restore(game, patch) == []
    assert sorted(p.name for p in game.iterdir()) == names
    assert core.inspect(game, patch).state is core.State.ORIGINAL


def test_an_existing_patch_archive_makes_ours_the_next(tmp_path, kirikiri_game):
    project, game = _translated(tmp_path, kirikiri_game, extra={"patch.xp3": {"note.txt": b"x"}})
    before = (game / "patch.xp3").read_bytes()
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").read_bytes() == before
    (entry,) = xp3.entries(game / "patch2.xp3")
    assert decode_script(xp3.read(game / "patch2.xp3", entry)) == "早安\n"


def test_a_link_at_the_patch_slot_is_refused_and_nothing_lands_outside(
    translated, tmp_path, symlink
):
    project, game = translated
    patch = _patch(project, game)
    outside = tmp_path / "outside" / "patch.xp3"
    outside.parent.mkdir()
    symlink(game / "patch.xp3", outside)
    with pytest.raises(core.PatchError, match="patch.xp3"):
        core.apply(game, patch)
    assert not outside.exists()
    assert not (game / core.RECORD).exists()


def test_a_patch_archive_swapped_for_a_link_is_not_read_through_it(
    translated, tmp_path, symlink, monkeypatch
):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    outside = tmp_path / "outside.xp3"
    outside.write_bytes((game / "patch.xp3").read_bytes())
    (game / "patch.xp3").unlink()
    symlink(game / "patch.xp3", outside)
    real_sha256_of = core.sha256_of

    def sha256_of(path: Path) -> str:
        assert Path(path).resolve() != outside.resolve(), f"{path} was read"
        return real_sha256_of(path)

    monkeypatch.setattr(core, "sha256_of", sha256_of)
    assert core.inspect(game, patch).state is core.State.ORIGINAL
    assert core.restore(game, patch) == ["patch.xp3"]
    assert outside.exists()


def test_a_patch_archive_the_game_does_not_load_refuses_the_apply(translated):
    project, game = translated
    patch = _patch(project, game)
    (game / "patch3.xp3").write_bytes(b"someone else's")
    with pytest.raises(core.PatchError, match="patch3.xp3"):
        core.apply(game, patch)
    assert not (game / "patch.xp3").exists()
    assert not (game / core.RECORD).exists()


# A release hashes only the archives holding scenarios, so another build of the game can
# match them while its own startup loads no patch archive, or loads them another way.
@pytest.mark.parametrize("startup", ["none", "single"])
def test_a_game_whose_startup_loads_patches_another_way_is_refused(
    tmp_path, kirikiri_game, startup
):
    scenario = {"sys/scenario.xp3": {"first.ks": encode_script("おはよう\n")}}
    project, built = _translated(tmp_path, kirikiri_game, {}, extra=scenario)
    patch = _patch(project, built, archives=["sys/scenario.xp3"])
    game = kirikiri_game(tmp_path / "other", {}, slots=startup, extra=scenario)
    names = sorted(p.name for p in game.iterdir())
    with pytest.raises(core.PatchError, match="載入補丁封包的方式"):
        core.apply(game, patch)
    assert sorted(p.name for p in game.iterdir()) == names


# An update often comes as one more patch archive. One the release did not check that holds
# a script this patch rewrites is newer than the translation, and ours, loaded last, would
# hide it.
def test_an_unchecked_archive_holding_a_script_the_patch_writes_is_refused(translated):
    project, game = translated
    patch = _patch(project, game)
    xp3.write(game / "patch.xp3", {"scenario/first.ks": encode_script("おはよう。\n")})
    names = sorted(p.name for p in game.iterdir())
    with pytest.raises(core.PatchError, match="patch.xp3 也有這個補丁要改的劇本"):
        core.apply(game, patch)
    assert sorted(p.name for p in game.iterdir()) == names


# Pictures are left out: the archives they come from are never checked.
def test_an_unchecked_archive_holding_only_what_the_patch_leaves_is_no_obstacle(translated):
    project, game = translated
    patch = _patch(project, game, {"image/title.png": b"T"})
    xp3.write(game / "patch.xp3", {"other.ks": encode_script("こんばんは\n"), "title.png": b"t"})
    core.apply(game, patch)
    entries = {e.name: e for e in xp3.entries(game / "patch2.xp3")}
    assert decode_script(xp3.read(game / "patch2.xp3", entries["first.ks"])) == "早安\n"


def test_a_changed_game_archive_is_refused_with_nothing_written(translated):
    project, game = translated
    patch = _patch(project, game)
    (game / "data.xp3").write_bytes((game / "data.xp3").read_bytes() + b"\0")
    names = sorted(p.name for p in game.iterdir())
    with pytest.raises(core.PatchError, match="不是這個補丁對應的原版"):
        core.apply(game, patch)
    assert sorted(p.name for p in game.iterdir()) == names


def test_a_stray_backup_beside_a_game_archive_does_not_matter(translated):
    project, game = translated
    (game / "data.xp3.bak").write_bytes(b"something else")
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").is_file()
    assert (game / "data.xp3.bak").read_bytes() == b"something else"


@pytest.mark.parametrize("slots", ["loop", "single"])
def test_a_game_holding_an_older_version_is_restored_by_the_newer_one(
    tmp_path, kirikiri_game, slots
):
    project, game = _translated(tmp_path, kirikiri_game, slots=slots)
    names = sorted(p.name for p in game.iterdir())
    older = _patch(project, game)
    (string_file,) = project.load_string_files()
    string_file.units[0].target = "晚安"
    project.save_string_file(string_file, "first.ks.json")
    newer = _patch(project, game)
    core.apply(game, older)

    found = core.inspect(game, newer)
    assert found == core.FolderState(core.State.UNKNOWN, True)
    assert "另一個版本" in core.describe(found, newer)
    with pytest.raises(core.PatchError, match="另一個版本"):
        core.apply(game, newer)
    assert core.restore(game, newer) == []
    assert sorted(p.name for p in game.iterdir()) == names
    core.apply(game, newer)
    (entry,) = xp3.entries(game / "patch.xp3")
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == "晚安\n"


def test_an_older_versions_archive_held_open_at_restore_goes_with_the_next(
    tmp_path, kirikiri_game, monkeypatch
):
    # Left behind, the older archive would still be loaded by the game.
    project, game = _translated(tmp_path, kirikiri_game)
    names = sorted(p.name for p in game.iterdir())
    older = _patch(project, game)
    (string_file,) = project.load_string_files()
    string_file.units[0].target = "晚安"
    project.save_string_file(string_file, "first.ks.json")
    newer = _patch(project, game)
    core.apply(game, older)
    unlink = Path.unlink

    def held(self: Path, *args, **kwargs) -> None:
        if self.name == "patch.xp3":
            raise PermissionError(13, "in use", str(self))
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held)
    with pytest.raises(core.PatchError):
        core.restore(game, newer)
    monkeypatch.undo()

    found = core.inspect(game, newer)
    assert found.restorable and found.state is not core.State.ORIGINAL
    assert core.restore(game, newer) == []
    assert sorted(p.name for p in game.iterdir()) == names


def test_the_disk_check_counts_only_what_is_added(tmp_path, kirikiri_game, monkeypatch):
    project, game = _translated(tmp_path, kirikiri_game, padding=16 << 20)
    monkeypatch.setattr(
        core.shutil, "disk_usage", lambda _: SimpleNamespace(free=core.SPARE_BYTES + (8 << 20))
    )
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").is_file()


@pytest.mark.parametrize(
    "record", ['{"added": [{"name": "patch.xp3", "sha', '{"added": null}'], ids=["cut", "null"]
)
def test_a_damaged_record_stops_restore_before_it_changes_anything(translated, record):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    (game / core.RECORD).write_text(record, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in game.iterdir()}

    core.inspect(game, patch)  # the window asks this as soon as the folder is chosen
    with pytest.raises(core.PatchError, match=core.RECORD):
        core.restore(game, patch)
    assert {path.name: path.read_bytes() for path in game.iterdir()} == before


@pytest.mark.parametrize(
    "record", ['{"added": [{"name": "patch.xp3", "sha', '{"added": null}'], ids=["cut", "null"]
)
def test_a_damaged_record_is_told_before_apply_is_offered(translated, record):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    (game / core.RECORD).write_text(record, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in game.iterdir()}

    found = core.inspect(game, patch)
    #: The window offers 套用 only for ORIGINAL, and 還原 only when restorable.
    assert found.state is not core.State.ORIGINAL and not found.restorable
    assert core.RECORD in core.describe(found, patch)
    with pytest.raises(core.PatchError) as refused:
        core.apply(game, patch)
    assert str(refused.value) == core.describe(found, patch)
    assert {path.name: path.read_bytes() for path in game.iterdir()} == before


def test_a_scn_patch_adds_an_archive_with_the_rewritten_scn_only(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    tree = scn_tree(("春香", ("おはよう", "Hi", "早")))
    game = kirikiri_game(tmp_path / "game", {}, files={"scenario/a.txt.scn": psb_file(tree)})
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Sample", "ja", "zh-Hant", "kirikiri")
    )
    project.config.engine.game_path = str(game)
    units = [u for u in KiriKiriAdapter().extract(game) if u.kind == "dialogue"]
    units[0].target = "早安"
    project.save_string_file(StringFile(source_file="a.txt.scn", units=units), "a.txt.scn.json")
    before = {p.name: p.read_bytes() for p in game.iterdir()}
    patch = _patch(project, game)
    core.apply(game, patch)
    assert {name: (game / name).read_bytes() for name in before} == before
    (entry,) = xp3.entries(game / "patch.xp3")
    assert entry.name == "a.txt.scn"
    root = psb.decode(xp3.read(game / "patch.xp3", entry)).root
    assert [c[1] for c in root["scenes"][0]["texts"][0][1]] == ["早安"] * 3
    assert core.restore(game, patch) == []
    assert sorted(p.name for p in game.iterdir()) == sorted(before)
