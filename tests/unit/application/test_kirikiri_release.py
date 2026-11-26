"""Releasing a KiriKiri game: a recipe the player's patcher compiles into its own archive."""

from __future__ import annotations

import io
import json
import shutil
from zipfile import ZipFile

import pytest

from bokucho.application import release
from bokucho.application.assets import save_image_units
from bokucho.application.contracts import CancellationFlag
from bokucho.application.errors import JobCancelled, ReleaseBlocked
from bokucho.application.projects import extract_text
from bokucho.application.release import release_patcher
from bokucho.engines import xp3
from bokucho.engines.kag import decode_script, encode_script
from bokucho.infrastructure.factories import make_engine_adapter
from bokucho.model import StringFile
from bokucho.patcher import core
from bokucho.patcher.payload import read_patch
from bokucho.patcher.recipe import compile_recipe
from bokucho.project import Project


def _project(tmp_path, new_project, game, **engine):
    project = new_project(engine="kirikiri")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(game), "kag_name_attributes": ["name.fn"], **engine}
                )
            }
        )
    )
    project = Project.load(project.root)
    extract_text(project, make_engine_adapter(project))
    return project


def _translate(project, targets):
    for string_file in project.load_string_files():
        for unit in string_file.units:
            unit.target = targets.get(unit.id, "")
        project.save_string_file(string_file, f"{string_file.source_file}.json")


def _release(project, stub):
    return release_patcher(project, make_engine_adapter, project.root / "dist", stub)


@pytest.fixture
def kirikiri_project(tmp_path, new_project, kirikiri_game):
    def build(**game_options):
        game = kirikiri_game(
            tmp_path / "game", {"scenario/first.ks": "[name fn=春香]\nおはよう\n"}, **game_options
        )
        project = _project(tmp_path, new_project, game)
        _translate(project, {"first.ks::0": "早安"})
        stub = tmp_path / "stub.exe"
        stub.write_bytes(b"MZ patcher")
        return project, game, stub

    return build


def test_the_patch_carries_only_translations_and_adds_an_archive(kirikiri_project, tmp_path):
    project, game, stub = kirikiri_project()
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3"]
    with ZipFile(io.BytesIO(patch.recipe)) as recipe:
        names = recipe.namelist()
        meta = json.loads(recipe.read("recipe.json"))
    assert all(
        n == "recipe.json" or (n.startswith("strings/") and n.endswith(".json")) for n in names
    )
    assert meta["engine"] == "kirikiri"
    assert meta["slots"] == "loop"
    assert meta["archives"] == ["data.xp3"]
    assert meta["kag_name_attributes"] == ["name.fn"]
    assert "original_sha256" not in project.project_file.read_text(encoding="utf-8")
    player = tmp_path / "player"
    shutil.copytree(game, player)
    core.apply(player, patch)
    assert (player / "patch.xp3").is_file()
    assert core.inspect(player, patch).state is core.State.PATCHED


def test_a_skipped_label_keeps_its_lines_and_names_in_the_applied_patch(
    tmp_path, new_project, kirikiri_game
):
    script = "*a_jp\n[name fn=春香]\nおはよう\n*a_chh\n[name fn=春香]\n早安\n"
    game = kirikiri_game(tmp_path / "game", {"scenario/first.ks": script})
    project = _project(tmp_path, new_project, game, kag_skip_labels=["*_chh"])
    _translate(project, {"first.ks:a_jp:0": "你好", "name:春香": "小春"})
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    core.apply(game, read_patch(_release(project, stub).executable.read_bytes()))
    (entry,) = xp3.entries(game / "patch.xp3")
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == (
        '*a_jp\n[name fn="小春"]\n你好\n*a_chh\n[name fn=春香]\n早安\n'
    )


def test_the_release_leaves_the_game_folder_as_it_was(kirikiri_project):
    project, game, stub = kirikiri_project()
    before = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    _release(project, stub)
    after = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    assert after == before


def test_a_game_that_loads_no_patch_archive_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project(slots="none")
    with pytest.raises(ReleaseBlocked, match="does not load patch archives"):
        _release(project, stub)


def test_a_game_with_a_bokucho_patch_on_it_is_not_released(kirikiri_project):
    project, game, stub = kirikiri_project()
    (game / core.RECORD).write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseBlocked, match="holds a patch already"):
        _release(project, stub)


def test_a_record_that_is_a_link_pointing_nowhere_still_stops_the_release(
    kirikiri_project, tmp_path, symlink
):
    project, game, stub = kirikiri_project()
    symlink(game / core.RECORD, tmp_path / "gone.json")
    with pytest.raises(ReleaseBlocked, match="holds a patch already"):
        _release(project, stub)


def test_a_publisher_patch_archive_is_checked_with_the_game(kirikiri_project):
    project, _game, stub = kirikiri_project(
        extra={"patch.xp3": {"first.ks": encode_script("[name fn=春香]\nおはよう\n")}}
    )
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3", "patch.xp3"]


def test_an_archive_without_a_script_the_game_reads_is_not_checked(kirikiri_project):
    project, _game, stub = kirikiri_project(extra={"patch.xp3": {"note.txt": b"x"}})
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3"]


def test_a_speaker_name_with_a_quote_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _translate(project, {"first.ks::0": "早安", "name:春香": '春"香'})
    with pytest.raises(ReleaseBlocked, match="name:春香"):
        _release(project, stub)


def test_a_release_with_nothing_translated_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _translate(project, {})
    with pytest.raises(ReleaseBlocked, match="Nothing to patch"):
        _release(project, stub)


def _picture(project, relative, data=b"\x89PNG\r\n\x1a\nX"):
    path = project.root / "out" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _recipe_names(project, stub):
    patch = read_patch(_release(project, stub).executable.read_bytes())
    with ZipFile(io.BytesIO(patch.recipe)) as recipe:
        return recipe.namelist()


def test_the_recipe_carries_the_pictures_and_not_their_text(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _picture(project, "image/title.png")
    save_image_units(project, StringFile(source_file="images", units=[]))
    (project.root / "out" / "first.ks").write_bytes(b"\xff\xfe")
    names = _recipe_names(project, stub)
    assert "out/image/title.png" in names
    assert "strings/images.json" not in names
    assert "out/first.ks" not in names


def test_pictures_alone_are_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _translate(project, {})
    _picture(project, "rule/op.png")
    assert "out/rule/op.png" in _recipe_names(project, stub)


def test_two_pictures_with_one_name_are_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _picture(project, "image/a.png")
    _picture(project, "rule/A.png")
    with pytest.raises(ReleaseBlocked, match="image/a.png"):
        _release(project, stub)


LOOP = [
    "useArchiveIfExists", "addAutoPath", "isExistentStorage", "patch.xp3", "patch", ".xp3", ".xp3>",
]


def test_a_scn_game_checks_only_the_archives_holding_what_it_reads(
    tmp_path, new_project, kirikiri_game, tjs2_script, psb_file, scn_tree
):
    game = kirikiri_game(
        tmp_path / "game", {},
        files={"system/Initialize.tjs": tjs2_script(LOOP), "x.ks": b"\xfe\xfe\x01x"},
        extra={"story.xp3": {"a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊"))))},
               "voice.xp3": {"v.ogg": b"OggS"}},
    )
    project = _project(tmp_path, new_project, game)
    _translate(project, {"a.txt.scn:s:0": "早安"})
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["story.xp3"]
    with ZipFile(io.BytesIO(patch.recipe)) as recipe:
        assert not [n for n in recipe.namelist() if n.lower().endswith(".scn")]
        meta = json.loads(recipe.read("recipe.json"))
    assert (meta["archives"], meta["slots"]) == (["story.xp3"], "loop")


def test_a_scrambled_script_over_a_readable_copy_is_checked_and_left_as_it_is(
    tmp_path, new_project, kirikiri_game, psb_file, scn_tree
):
    # Replaying against data.xp3 alone, the patcher would take its readable a.ks for the
    # script the game reads, and write the translated name into that older copy.
    game = kirikiri_game(
        tmp_path / "game", {"a.ks": "[name fn=春香]\nおはよう\n"},
        files={"s.txt.scn": psb_file(scn_tree(("春香", ("あ", "A", "啊"))))},
        extra={"patch.xp3": {"a.ks": b"\xfe\xfe\x01x"}},
    )
    project = _project(tmp_path, new_project, game)
    _translate(project, {"name:春香": "小春"})
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3", "patch.xp3"]
    _rebuilt, (added,) = compile_recipe(game, patch, tmp_path / "scratch")
    (tmp_path / "added.xp3").write_bytes(added.data)
    assert [e.name for e in xp3.entries(tmp_path / "added.xp3")] == ["s.txt.scn"]


def test_a_cancel_while_hashing_stops_before_the_next_archive(kirikiri_project, monkeypatch):
    project, _game, stub = kirikiri_project(
        extra={"patch.xp3": {"first.ks": encode_script("[name fn=春香]\nおはよう\n")}}
    )
    flag = CancellationFlag()
    hashed = []

    def hash_then_cancel(path):
        hashed.append(path.name)
        flag.cancel()
        return "0" * 64

    monkeypatch.setattr(release, "sha256_of", hash_then_cancel)
    with pytest.raises(JobCancelled):
        release_patcher(project, make_engine_adapter, project.root / "dist", stub, token=flag)
    assert hashed == ["data.xp3"]


@pytest.mark.parametrize("record", ["file", "link pointing nowhere"])
def test_an_rpg_maker_game_with_a_record_beside_it_is_not_released(tmp_path, symlink, record):
    from pathlib import PurePath
    from types import SimpleNamespace

    game = tmp_path / "game"
    game.mkdir()
    if record == "file":
        (game / core.RECORD).write_text("{}", encoding="utf-8")
    else:
        symlink(game / core.RECORD, tmp_path / "gone.json")
    layout = SimpleNamespace(root=game, relative_data=PurePath("Data"), variant=None)
    with pytest.raises(ReleaseBlocked, match="holds a patch already"):
        release._refuse_a_patched_folder([], layout, [])
