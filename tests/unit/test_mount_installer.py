"""RPG Maker patches install and restore without editing scripts by hand."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.release import build_patch, release_patcher
from bokucho.engines.rpgmaker import RpgMakerAdapter
from bokucho.model import StringFile
from bokucho.patcher.__main__ import main
from bokucho.patcher.core import PatchError, State, inspect
from bokucho.patcher.cs2_mount import restore_cs2
from bokucho.patcher.payload import read_patch
from bokucho.patcher.renpy_mount import restore_renpy
from bokucho.patcher.unity_mount import restore_unity
from bokucho.project import Project


@pytest.fixture(params=["mv", "mz"])
def mount_release(tmp_path, request):
    game = tmp_path / "game"
    base = game / "www" if request.param == "mv" else game
    (base / "data").mkdir(parents=True)
    (base / "js").mkdir()
    (base / "data" / "System.json").write_text(
        json.dumps({"gameTitle": "Original", "terms": {}}), encoding="utf-8"
    )
    if request.param == "mz":
        (base / "js" / "rmmz_core.js").write_bytes(b"// core")
    registry = base / "js" / "plugins.js"
    registry.write_bytes(b'\xef\xbb\xbfvar $plugins = [{"name":"Other","status":true}];\r\n')
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "mount", "en", "zh-Hant", "rpgmaker")
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(game)}),
            }
        )
    )
    units = RpgMakerAdapter().extract(game)
    units[0].target = "翻譯"
    project.save_string_file(StringFile(source_file="System", units=units), "system.json")
    image = base / "img" / "pictures" / "title.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"original picture")
    translated = project.root / "out" / base.relative_to(game) / "img" / "pictures" / "title.png"
    translated.parent.mkdir(parents=True)
    translated.write_bytes(b"translated picture")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    return Project.load(project.root), game, base.relative_to(game), stub


def _snapshot(folder):
    return {
        path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()
    }


def _release(mount_release, tmp_path, stub=None):
    project, game, base, original_stub = mount_release
    before = _snapshot(game)
    built = build_patch(project, stub=stub or original_stub)
    assert built.executable.name == "mount-en2zh-tw-patcher.exe"
    assert _snapshot(game) == before
    player = tmp_path / "玩家 & %TEMP% ! (copy)"
    shutil.copytree(game, player)
    return built.executable, player, base


def test_the_executable_registers_the_plugin_and_restores_exact_bytes(mount_release, tmp_path):
    executable, player, base = _release(mount_release, tmp_path)
    before = _snapshot(player)
    registry = player / base / "js" / "plugins.js"
    original = registry.read_bytes()
    assert main([str(executable), "--apply", str(player)]) == 0
    assert registry.read_bytes().startswith(original)
    assert b"$plugins.push" in registry.read_bytes()
    assert (player / base / "js" / "plugins" / "Bokucho.js").is_file()
    assert (player / base / "bokucho" / "img" / "pictures" / "title.png").read_bytes() == (
        b"translated picture"
    )
    assert not any(player.rglob("*.bat"))
    assert not any((player / ".bokucho-backup").glob("*.exe"))
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.PATCHED
    assert main([str(executable), "--apply", str(player)]) == 1
    assert main([str(executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before
    assert not (player / ".bokucho-backup").exists()


def test_one_click_release_publishes_the_same_installer(mount_release, tmp_path):
    project, game, _, stub = mount_release
    before = _snapshot(game)
    result = release_patcher(
        project, lambda current: RpgMakerAdapter(), project.root / "dist", stub
    )
    assert list(result.artifact_dir.iterdir()) == [result.executable]
    player = tmp_path / "one-click player"
    shutil.copytree(game, player)
    assert main([str(result.executable), "--apply", str(player)]) == 0
    assert main([str(result.executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before
    assert _snapshot(game) == before


def test_restore_keeps_later_plugin_settings_and_edited_assets(mount_release, tmp_path):
    executable, player, base = _release(mount_release, tmp_path)
    registry = player / base / "js" / "plugins.js"
    before = registry.read_bytes()
    assert main([str(executable), "--apply", str(player)]) == 0
    later = b'\r\n$plugins.push({"name":"Later","status":true});\r\n'
    registry.write_bytes(registry.read_bytes() + later)
    plugin = player / base / "js" / "plugins" / "Bokucho.js"
    plugin.write_bytes(b"player edited this")
    assert main([str(executable), "--restore", str(player)]) == 0
    assert registry.read_bytes() == before + later
    assert plugin.read_bytes() == b"player edited this"


@pytest.mark.parametrize("collision", ["plugin", "backup", "wrong game"])
def test_preflight_refusals_preserve_all_player_files(mount_release, tmp_path, collision):
    executable, player, base = _release(mount_release, tmp_path)
    if collision == "plugin":
        path = player / base / "js" / "plugins" / "Bokucho.js"
    elif collision == "backup":
        path = player / ".bokucho-backup" / "other.txt"
    elif collision == "wrong game":
        path = player / base / "data" / "System.json"
    else:
        path = player / collision
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"keep this file")
    before = _snapshot(player)
    assert main([str(executable), "--apply", str(player)]) == 1
    assert _snapshot(player) == before


def test_a_failed_registry_swap_rolls_back_the_whole_install(mount_release, tmp_path, monkeypatch):
    executable, player, base = _release(mount_release, tmp_path)
    before = _snapshot(player)
    replace = os.replace

    def fail_registry(source, target):
        if Path(target) == player / base / "js" / "plugins.js":
            raise OSError("simulated registry failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_registry)
    assert main([str(executable), "--apply", str(player)]) == 1
    assert _snapshot(player) == before


def test_permission_denial_keeps_files_and_asks_for_a_writable_folder(
    mount_release,
    tmp_path,
    monkeypatch,
    capsys,
):
    executable, player, _ = _release(mount_release, tmp_path)
    before = _snapshot(player)

    def no_permission(path, data):
        raise PermissionError("simulated write denial")

    monkeypatch.setattr("bokucho.patcher.mount._write_registry", no_permission)
    assert main([str(executable), "--apply", str(player)]) == 1
    assert "沒有權限寫入" in capsys.readouterr().err
    assert _snapshot(player) == before


@pytest.mark.parametrize("damage", ["registration", "backup"])
def test_restore_refuses_damage_without_changing_files(mount_release, tmp_path, damage):
    executable, player, base = _release(mount_release, tmp_path)
    assert main([str(executable), "--apply", str(player)]) == 0
    if damage == "registration":
        path = player / base / "js" / "plugins.js"
        path.write_bytes(path.read_bytes().replace(b"status: true", b"status: false"))
    else:
        path = player / ".bokucho-backup" / "registry.bin"
        path.write_bytes(b"damaged backup")
    before = _snapshot(player)
    assert main([str(executable), "--restore", str(player)]) == 1
    assert _snapshot(player) == before


_NESTED = b"[" * 100_000 + b"]" * 100_000


def test_a_restore_record_nested_past_what_the_parser_follows_reads_as_damaged(
    mount_release, tmp_path, capsys
):
    executable, player, base = _release(mount_release, tmp_path)
    assert main([str(executable), "--apply", str(player)]) == 0
    patch = read_patch(executable.read_bytes())
    state = player / ".bokucho-backup" / "state.json"
    said = []
    for record in (b"{", _NESTED):
        state.write_bytes(record)
        before = _snapshot(player)
        assert inspect(player, patch).state is State.UNKNOWN
        for action, refusal in (
            ("--apply", "不是補丁對應的原版，或已套用補丁。"),
            ("--restore", "還原失敗，請保留 .bokucho-backup 再試一次。"),
        ):
            capsys.readouterr()
            assert main([str(executable), action, str(player)]) == 1
            heard = capsys.readouterr().err
            assert refusal in heard
            said.append((action, heard.split("（")[0]))
        assert _snapshot(player) == before
    assert said[:2] == said[2:]


@pytest.mark.parametrize(
    "restore", [restore_unity, restore_cs2, restore_renpy], ids=["unity", "cs2", "renpy"]
)
def test_another_kinds_restore_record_nested_past_what_the_parser_follows_reads_as_damaged(
    tmp_path, restore
):
    state = tmp_path / ".bokucho-backup" / "state.json"
    state.parent.mkdir()
    said = []
    for record in (b"{", _NESTED):
        state.write_bytes(record)
        with pytest.raises(PatchError) as caught:
            restore(tmp_path)
        assert state.read_bytes() == record
        said.append(str(caught.value).split("（")[0])
    assert said[0] == said[1]


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file sharing")
def test_restore_can_retry_a_failed_deletion_with_later_settings(mount_release, tmp_path):
    import _winapi

    executable, player, base = _release(mount_release, tmp_path)
    registry = player / base / "js" / "plugins.js"
    before = registry.read_bytes()
    assert main([str(executable), "--apply", str(player)]) == 0
    later = b'\r\n$plugins.push({"name":"Later","status":true});\r\n'
    registry.write_bytes(registry.read_bytes() + later)
    plugin = player / base / "js" / "plugins" / "Bokucho.js"
    handle = _winapi.CreateFile(
        str(plugin), _winapi.GENERIC_READ, 1, 0, _winapi.OPEN_EXISTING, 0, 0
    )
    try:
        assert main([str(executable), "--restore", str(player)]) == 1
    finally:
        _winapi.CloseHandle(handle)
    assert main([str(executable), "--restore", str(player)]) == 0
    assert registry.read_bytes() == before + later


@pytest.mark.skipif(not os.environ.get("BOKUCHO_TEST_PATCHER"), reason="needs a rebuilt patcher")
@pytest.mark.parametrize("damaged_backup", [False, True])
def test_the_same_executable_applies_and_restores(mount_release, tmp_path, damaged_backup):
    executable, player, _ = _release(
        mount_release, tmp_path, Path(os.environ["BOKUCHO_TEST_PATCHER"])
    )
    before = _snapshot(player)
    completed = subprocess.run([str(executable), "--apply", str(player)], timeout=120)
    assert completed.returncode == 0
    assert not any(player.rglob("*.bat"))
    assert not any((player / ".bokucho-backup").glob("*.exe"))
    if damaged_backup:
        (player / ".bokucho-backup/registry.bin").write_bytes(b"damaged backup")
        before = _snapshot(player)
    completed = subprocess.run(
        [str(executable), "--restore", str(player)],
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == (1 if damaged_backup else 0), (
        completed.stdout,
        completed.stderr,
    )
    assert _snapshot(player) == before
    assert executable.is_file()
