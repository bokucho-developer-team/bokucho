from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from bokucho.patcher.__main__ import main
from bokucho.patcher.core import RECORD, apply, default_folder, restore
from bokucho.patcher.delta import make_delta
from bokucho.patcher.payload import FileState, Patch, PatchAdded, PatchedFile, patch_executable

ORIGINAL = b"original game " * 4096
TRANSLATED = b"translated game " * 4096


def _state(data: bytes) -> FileState:
    return FileState(len(data), hashlib.sha256(data).hexdigest())


def _patch() -> Patch:
    delta, window = make_delta(ORIGINAL, TRANSLATED)
    return Patch(
        "測試", (PatchedFile("Game.exe", _state(ORIGINAL), _state(TRANSLATED), delta, window),)
    )


def _write_patch(tmp_path: Path) -> Path:
    path = tmp_path / "測試-ja2zh-tw-patcher.exe"
    path.write_bytes(patch_executable(b"MZ stub", _patch()))
    return path


def test_apply_without_the_window_patches_the_folder(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)

    assert main([str(_write_patch(tmp_path)), "--apply", str(game)]) == 0
    assert (game / "Game.exe").read_bytes() == TRANSLATED


def test_a_refusal_is_exit_code_one(tmp_path: Path, capsys) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(b"something else")

    assert main([str(_write_patch(tmp_path)), "--apply", str(game)]) == 1
    assert "不是這個補丁對應的原版" in capsys.readouterr().err
    assert (game / "Game.exe").read_bytes() == b"something else"


def test_a_damaged_download_says_to_download_again(tmp_path: Path, capsys) -> None:
    patch = _write_patch(tmp_path)
    patch.write_bytes(patch.read_bytes()[:-5])
    game = tmp_path / "game"
    game.mkdir()

    assert main([str(patch), "--apply", str(game)]) == 1
    assert "請重新下載" in capsys.readouterr().err


def test_the_folder_the_patch_sits_in_is_offered_when_the_game_is_there(tmp_path: Path) -> None:
    assert default_folder(tmp_path, _patch()) is None
    (tmp_path / "Game.exe").write_bytes(ORIGINAL)
    assert default_folder(tmp_path, _patch()) == tmp_path


def test_apply_and_restore_report_their_work(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = _patch()
    log = []

    apply(game, patch, log.append)
    restore(game, patch, log.append)

    assert log == [
        "正在檢查遊戲檔案。",
        "正在套用翻譯檔案。",
        "正在檢查還原備份。",
        "正在還原原版遊戲檔案。",
    ]


def test_a_restore_cut_short_can_be_finished(tmp_path: Path) -> None:
    data, translated_data = b"data " * 4096, b"daten " * 4096
    files = []
    for name, original, translated in (
        ("Game.exe", ORIGINAL, TRANSLATED),
        ("data.bin", data, translated_data),
    ):
        delta, window = make_delta(original, translated)
        files.append(PatchedFile(name, _state(original), _state(translated), delta, window))
    patch = Patch("測試", tuple(files))
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "data.bin").write_bytes(data)
    apply(game, patch)
    os.replace(game / "Game.exe.bak", game / "Game.exe")

    restore(game, patch)

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert (game / "data.bin").read_bytes() == data
    assert not list(game.glob("*.bak"))


@pytest.mark.parametrize(
    "record",
    ['{"added": [{"name": "font.ttf", "sha', '{"added": null}', "[]"],
    ids=["cut", "null", "not-an-object"],
)
def test_a_damaged_record_still_lets_the_originals_back(tmp_path: Path, record: str) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))
    apply(game, patch)
    (game / RECORD).write_text(record, encoding="utf-8")
    said: list[str] = []

    restore(game, patch, said.append)

    #: The backups are whole, so the game goes back; what was added can no longer be told.
    assert {path.name: path.read_bytes() for path in game.iterdir()} == {
        "Game.exe": ORIGINAL,
        "font.ttf": b"font",
    }
    assert any(RECORD in line for line in said)


def test_restore_without_the_window_says_the_record_was_damaged(tmp_path: Path, capsys) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))
    executable = tmp_path / "測試-ja2zh-tw-patcher.exe"
    executable.write_bytes(patch_executable(b"MZ stub", patch))
    apply(game, patch)
    (game / RECORD).write_text('{"added": null}', encoding="utf-8")

    assert main([str(executable), "--restore", str(game)]) == 0
    #: The record is gone after this, so the warning is the only trace of what may be left.
    assert RECORD in capsys.readouterr().out
    assert (game / "Game.exe").read_bytes() == ORIGINAL
