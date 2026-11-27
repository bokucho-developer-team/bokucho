from __future__ import annotations

import errno
import hashlib
import json
import os
import pickle
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bokucho.patcher import core
from bokucho.patcher.__main__ import main
from bokucho.patcher.core import RECORD, PatchError, apply, default_folder, restore
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


# A process stopped between writes has no chance to roll back. So what the patch adds is in
# the record before it appears, and it appears whole, by a rename, as the record does:
# restore can still take it away, and nothing half written is left for the game to load.
def test_an_added_file_appears_whole_and_only_once_the_record_names_it(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))
    seen: list[tuple[str, bytes, bool, str]] = []
    # A file put in place goes by a rename, or by a hard link where a rename would replace.
    real = {name: getattr(os, name) for name in ("replace", "rename", "link")}

    def watching(name: str):
        def watch(source, target) -> None:
            if Path(target).name in ("font.ttf", RECORD):
                record = game / RECORD
                listed = record.read_text(encoding="utf-8") if record.exists() else ""
                there = Path(target).exists()
                seen.append((Path(target).name, Path(source).read_bytes(), there, listed))
            real[name](source, target)

        return watch

    for name in real:
        monkeypatch.setattr(os, name, watching(name))
    apply(game, patch)
    assert [name for name, *_ in seen] == [RECORD, "font.ttf"]
    assert b'"font.ttf"' in seen[0][1]
    _, data, there, listed = seen[1]
    assert data == b"font" and not there and '"font.ttf"' in listed


# Each file is written under its name plus .bokucho-tmp first; a file already there is
# not Bokucho's to delete.
@pytest.mark.parametrize(
    "name", ["font.ttf.bokucho-tmp", f"{RECORD}.bokucho-tmp", "Game.exe.bokucho-tmp"]
)
def test_a_file_at_a_scratch_name_is_kept_and_named(tmp_path: Path, name: str) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / name).write_bytes(b"not the patch's")
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    with pytest.raises(PatchError, match=re.escape(name)):
        apply(game, patch)

    assert (game / name).read_bytes() == b"not the patch's"
    assert sorted(path.name for path in game.iterdir()) == sorted({"Game.exe", name})


@pytest.mark.parametrize("font_written", [False, True])
def test_a_record_left_by_a_patch_stopped_partway_is_cleared_by_restore(
    tmp_path: Path, font_written: bool
) -> None:
    # The record is written first, so a process stopped before the swap leaves it beside
    # the originals, with or without the files it lists.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    listed = [{"name": "font.ttf", "sha256": hashlib.sha256(b"font").hexdigest()}]
    (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")
    if font_written:
        (game / "font.ttf").write_bytes(b"font")
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    assert restore(game, patch) == []

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_second_patcher_on_the_same_folder_is_refused(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)

    with core._one_at_a_time(game):  # another patcher at work in this folder
        with pytest.raises(PatchError, match="另一個補丁程式"):
            apply(game, _patch())
        with pytest.raises(PatchError, match="另一個補丁程式"):
            restore(game, _patch())
        assert (game / "Game.exe").read_bytes() == ORIGINAL

    apply(game, _patch())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "Game.exe.bak"]


def test_a_record_another_patch_left_beside_the_originals_is_not_restored(
    tmp_path: Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "version.dll").write_bytes(b"MZ another")
    listed = [{"name": "version.dll", "sha256": hashlib.sha256(b"MZ another").hexdigest()}]
    (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    with pytest.raises(PatchError):
        restore(game, patch)

    assert (game / "version.dll").read_bytes() == b"MZ another"
    assert (game / RECORD).exists()


def test_a_record_of_a_file_by_this_name_with_other_content_is_not_restored(
    tmp_path: Path,
) -> None:
    # Another patch's file by the name this patch adds: restore would take it away.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "font.ttf").write_bytes(b"another font")
    listed = [{"name": "font.ttf", "sha256": hashlib.sha256(b"another font").hexdigest()}]
    (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    with pytest.raises(PatchError):
        restore(game, patch)

    assert (game / "font.ttf").read_bytes() == b"another font"
    assert (game / RECORD).exists()


@pytest.mark.parametrize("left", ["file", "folder"])
def test_what_another_program_keeps_at_the_lock_name_is_kept_and_named(
    tmp_path: Path, left: str
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    lock = game / core.LOCK
    if left == "file":
        lock.write_bytes(b"notes")
    else:
        lock.mkdir()

    for act in (apply, restore):
        with pytest.raises(PatchError, match=re.escape(core.LOCK)):
            act(game, _patch())

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    if left == "file":
        assert lock.read_bytes() == b"notes"
    else:
        assert lock.is_dir()


@pytest.mark.parametrize(
    "left", [b"", core._LOCK_MARK[:7], core._LOCK_MARK], ids=["unmarked", "cut-short", "marked"]
)
def test_a_lock_file_a_stopped_patcher_left_is_taken_over(
    tmp_path: Path, monkeypatch, left: bytes
) -> None:
    # Stopped before it marked the file as Bokucho's, partway through, or after.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / core.LOCK).write_bytes(left)
    swap = core._swap
    seen: list[bytes] = []

    def look(folder: Path, patch: Patch, moved: list[Path]) -> None:
        held = core._HELD.get()
        assert held is not None
        os.lseek(held[1], 0, os.SEEK_SET)
        seen.append(os.read(held[1], 1 << 20))
        swap(folder, patch, moved)

    monkeypatch.setattr(core, "_swap", look)
    apply(game, _patch())

    # Marked whole, so a patcher stopped now leaves a file the next one takes over.
    assert seen[0].startswith(core._LOCK_MARK)
    lines = seen[0][len(core._LOCK_MARK) :].splitlines()
    assert [json.loads(line)[0] for line in lines] == ["Game.exe.bokucho-tmp"]
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "Game.exe.bak"]


def test_a_lock_file_with_another_name_too_is_refused_unwritten(tmp_path: Path) -> None:
    # Written through, a file elsewhere sharing it would change.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"")
    try:
        os.link(outside, game / core.LOCK)
    except OSError:
        pytest.skip("this file system has no hard links")

    for act in (apply, restore):
        with pytest.raises(PatchError, match=re.escape(core.LOCK)):
            act(game, _patch())

    assert outside.read_bytes() == b""
    assert (game / core.LOCK).exists()
    assert (game / "Game.exe").read_bytes() == ORIGINAL


@pytest.mark.parametrize("then", ["gone", "replaced"])
def test_a_lock_file_taken_away_before_it_was_locked_is_refused(
    tmp_path: Path, monkeypatch, then: str
) -> None:
    # The patcher that held it took it away, and another may hold a new one by that name.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    other = tmp_path / "another lock file"
    other.write_bytes(b"")
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == game / core.LOCK:
            if then == "gone":
                raise FileNotFoundError(errno.ENOENT, "gone", str(path))
            return real_lstat(other)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(core.os, "lstat", lstat)
    with pytest.raises(PatchError, match="另一個補丁程式"):
        apply(game, _patch())
    monkeypatch.undo()

    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_patcher_refused_while_another_finishes_leaves_no_lock_file(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    working = core._one_at_a_time(game)
    working.__enter__()

    # The one at work finishes while the refused one still has the lock file open.
    def busy(*_args) -> None:
        working.__exit__(None, None, None)
        raise OSError(errno.EACCES, "locked")

    if sys.platform == "win32":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", busy)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", busy)
    with pytest.raises(PatchError, match="另一個補丁程式"):
        apply(game, _patch())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_record_that_is_a_link_is_neither_read_nor_used(
    tmp_path: Path, symlink, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "font.ttf").write_bytes(b"font")
    elsewhere = tmp_path / "elsewhere.json"
    listed = [{"name": "font.ttf", "sha256": hashlib.sha256(b"font").hexdigest()}]
    elsewhere.write_text(json.dumps({"added": listed}), encoding="utf-8")
    symlink(game / RECORD, elsewhere)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))
    real_read_text = Path.read_text

    def read_text(self: Path, *args, **kwargs) -> str:
        assert self.resolve() != elsewhere.resolve(), "the record was read through the link"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert core.inspect(game, patch) == core.FolderState(core.State.LINKED, False, RECORD)
    # Extract reads the record too, with no inspect before it.
    with pytest.raises(ValueError, match=re.escape(RECORD)):
        core.added_files(game)
    for act in (apply, restore):
        with pytest.raises(PatchError, match=re.escape(RECORD)):
            act(game, patch)

    assert (game / "font.ttf").read_bytes() == b"font"
    assert (game / RECORD).is_symlink()


_FONT = {"name": "font.ttf", "sha256": hashlib.sha256(b"font").hexdigest()}


@pytest.mark.parametrize(
    "record",
    [
        {"added": []},
        {"added": [_FONT], "recipe": "0" * 64},
        {"added": [_FONT, {"name": "other.dll"}]},
    ],
)
def test_a_record_this_patch_did_not_write_is_not_restored(
    tmp_path: Path, record: dict
) -> None:
    # Empty, a recipe's, or not in the shape Bokucho writes: another patch's, or none,
    # though it may list this patch's file.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / RECORD).write_text(json.dumps(record), encoding="utf-8")
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    with pytest.raises(PatchError):
        restore(game, patch)

    assert json.loads((game / RECORD).read_text(encoding="utf-8")) == record


def test_restore_never_takes_away_a_game_file_a_record_lists(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(TRANSLATED)
    (game / "Game.exe.bak").write_bytes(ORIGINAL)
    listed = [{"name": "Game.exe", "sha256": hashlib.sha256(ORIGINAL).hexdigest()}]
    (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")

    restore(game, _patch())

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


class _CutShort:
    """A file being written that stops partway, as a full disk or a pulled drive stops it."""

    def __init__(self, stream, path: Path) -> None:
        self.stream, self.path = stream, path

    def __enter__(self) -> _CutShort:
        return self

    def __exit__(self, *_exc) -> None:
        self.stream.close()

    def fileno(self) -> int:
        return self.stream.fileno()

    def close(self) -> None:
        self.stream.close()

    def write(self, data: bytes) -> int:
        self.stream.write(data[:1])
        self.stream.flush()
        raise PermissionError(errno.EACCES, "denied", str(self.path))


def _cut_short(monkeypatch, name: str, after: int = 0) -> list[Path]:
    """Each file the patcher makes by ``name`` stops partway, past the first ``after``."""
    made: list[Path] = []
    real_open = Path.open

    def open_(self: Path, mode: str = "r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if mode == "xb" and self.name == name:
            made.append(self)
            if len(made) > after:
                return _CutShort(stream, self)
        return stream

    monkeypatch.setattr(Path, "open", open_)
    return made


def _font_patch() -> Patch:
    return replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))


def _two_added() -> Patch:
    return replace(
        _patch(), added=(PatchAdded("font.ttf", b"font"), PatchAdded("extra.dll", b"MZ extra"))
    )


def _stopped_before_the_swap(game: Path) -> bytes:
    """What this patch's patcher left, stopped before the swap: its record, and its font."""
    (game / "font.ttf").write_bytes(b"font")
    record = json.dumps({"added": [_FONT]}).encode()
    (game / RECORD).write_bytes(record)
    return record


def test_a_record_that_could_not_be_replaced_is_left_as_it_was(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    before = _stopped_before_the_swap(game)

    _cut_short(monkeypatch, f"{RECORD}.bokucho-tmp")
    with pytest.raises(PatchError):
        apply(game, _two_added())
    monkeypatch.undo()

    assert (game / RECORD).read_bytes() == before
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_record_put_back_after_a_failure_is_never_left_cut_short(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    before = _stopped_before_the_swap(game)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        raise PatchError("stopped after the record")

    # The new record goes out whole; putting the old one back stops partway.
    made = _cut_short(monkeypatch, f"{RECORD}.bokucho-tmp", after=1)
    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError):
        apply(game, _two_added())
    monkeypatch.undo()

    assert len(made) == 2, "the record before is put back"
    extra = {"name": "extra.dll", "sha256": hashlib.sha256(b"MZ extra").hexdigest()}
    assert json.loads((game / RECORD).read_bytes()) in (
        json.loads(before), {"added": [_FONT, extra], "patch": core._identity(_two_added())}
    )
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]


@pytest.mark.parametrize("stuck", ["font.ttf", RECORD])
def test_a_rollback_step_that_fails_keeps_the_error_that_stopped_the_install(
    tmp_path: Path, monkeypatch, stuck: str
) -> None:
    # The rest still goes back, and the record stays while a file it lists does, for restore.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == stuck:
            raise PermissionError(errno.EACCES, "in use")
        real_unlink(self, missing_ok=missing_ok)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        raise OSError(errno.EIO, "the swap failed")

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError, match="the swap failed"):
        apply(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == sorted({"Game.exe", RECORD, stuck})
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    restore(game, _two_added())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_record_that_cannot_be_put_back_keeps_the_error_that_stopped_the_install(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    before = _stopped_before_the_swap(game)
    rewrite = core._rewrite

    def rewrite_but_not_back(path: Path, data: bytes, **kwargs) -> os.stat_result:
        if path.name == RECORD and data == before:
            raise PatchError("the record could not be put back")
        return rewrite(path, data, **kwargs)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        raise OSError(errno.EIO, "the swap failed")

    monkeypatch.setattr(core, "_rewrite", rewrite_but_not_back)
    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError, match="the swap failed"):
        apply(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]
    restore(game, _two_added())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


@pytest.mark.parametrize("swap_fails", [False, True], ids=["applied", "stopped"])
def test_a_list_that_cannot_be_cleared_at_the_end_changes_nothing_said(
    tmp_path: Path, monkeypatch, swap_fails: bool
) -> None:
    # What was done is not reported as failed, nor is why an apply stopped hidden; the list
    # stays for the next patcher.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    settle = core._settle
    calls: list[Path] = []

    def settle_once(folder: Path, lock: int) -> list[str]:
        calls.append(folder)
        if len(calls) > 1:
            raise OSError(errno.EIO, "the list could not be read")
        return settle(folder, lock)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        raise OSError(errno.EIO, "the swap failed")

    monkeypatch.setattr(core, "_settle", settle_once)
    if swap_fails:
        monkeypatch.setattr(core, "_swap", swap)
        with pytest.raises(PatchError, match="the swap failed"):
            apply(game, _patch())
    else:
        apply(game, _patch())
    monkeypatch.undo()

    assert len(calls) == 2
    assert (game / core.LOCK).read_bytes().startswith(core._LOCK_MARK)
    assert ((game / "Game.exe").read_bytes() == TRANSLATED) is not swap_fails
    (apply if swap_fails else restore)(game, _patch())
    expected = ["Game.exe", "Game.exe.bak"] if swap_fails else ["Game.exe"]
    assert sorted(path.name for path in game.iterdir()) == expected


@pytest.mark.parametrize(
    "record,said",
    [({"added": [{"name": "version.dll", "sha256": hashlib.sha256(b"MZ").hexdigest()}]},
      "其他補丁"),
     ({"added": None}, "已損壞")],
    ids=["another-patchs", "damaged"],
)
def test_apply_beside_a_record_this_patch_did_not_write_changes_nothing(
    tmp_path: Path, record: dict, said: str
) -> None:
    # Merged into this patch's, the other patch's entries would be taken away by this
    # patch's restore, and by a rollback that cannot tell them apart.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "version.dll").write_bytes(b"MZ")
    (game / RECORD).write_text(json.dumps(record), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in game.iterdir()}

    with pytest.raises(PatchError, match=said) as refused:
        apply(game, _font_patch())

    assert RECORD in str(refused.value)
    assert {path.name: path.read_bytes() for path in game.iterdir()} == before


def test_a_file_already_there_by_the_same_content_is_not_recorded_and_stays(
    tmp_path: Path,
) -> None:
    # It was there before the patch, so restoring the patch leaves it.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "font.ttf").write_bytes(b"font")

    apply(game, _two_added())
    listed = json.loads((game / RECORD).read_text(encoding="utf-8"))["added"]
    assert [entry["name"] for entry in listed] == ["extra.dll"]
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]

    apply(game, _font_patch())
    assert not (game / RECORD).exists()
    restore(game, _font_patch())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]


def test_a_record_that_is_a_link_pointing_nowhere_is_not_taken_for_none(
    tmp_path: Path, symlink
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    symlink(game / RECORD, tmp_path / "gone.json")

    with pytest.raises(ValueError, match=re.escape(RECORD)):
        core.added_files(game)
    for act in (apply, restore):
        with pytest.raises(PatchError, match=re.escape(RECORD)):
            act(game, _font_patch())
    assert (game / RECORD).is_symlink() and not (tmp_path / "gone.json").exists()


def test_a_scratch_file_swapped_for_a_link_once_written_is_not_published(
    tmp_path: Path, symlink, monkeypatch
) -> None:
    # Each file is written through the handle that made it, and the file put in place must
    # be that one: a link put at the scratch name since is not followed, nor taken away.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    write_new = core._write_new

    def swapped(path: Path, data: bytes) -> os.stat_result:
        own = write_new(path, data)
        if path.name == f"{RECORD}.bokucho-tmp":
            os.replace(path, tmp_path / "moved")
            symlink(path, outside)
        return own

    monkeypatch.setattr(core, "_write_new", swapped)
    with pytest.raises(PatchError, match=re.escape(f"{RECORD}.bokucho-tmp")):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert outside.read_bytes() == b"outside"
    assert (game / f"{RECORD}.bokucho-tmp").is_symlink()
    assert not (game / RECORD).exists()
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_staged_file_swapped_for_a_link_does_not_go_in_place_of_the_game_file(
    tmp_path: Path, symlink, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"outside")
    staged = game / "Game.exe.bokucho-tmp"
    rewrite = core._rewrite

    def then_swapped(path: Path, data: bytes, **kwargs) -> os.stat_result:
        own = rewrite(path, data, **kwargs)
        if path.name == "font.ttf":  # written after the game file is staged
            os.replace(staged, tmp_path / "moved")
            symlink(staged, outside)
        return own

    monkeypatch.setattr(core, "_rewrite", then_swapped)
    with pytest.raises(PatchError, match=re.escape(staged.name)):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert not (game / "Game.exe").is_symlink()
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert outside.read_bytes() == b"outside"
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", staged.name]


def test_a_link_put_at_an_added_files_name_since_the_checks_is_refused_unread(
    tmp_path: Path, symlink, monkeypatch
) -> None:
    # Read through, a file outside the game folder by the same content would pass as one
    # there before the patch, and stay unrecorded.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside.ttf"
    outside.write_bytes(b"font")
    commit = core._commit_install

    def linked_first(folder: Path, patch: Patch, staged: list) -> None:
        symlink(folder / "font.ttf", outside)
        commit(folder, patch, staged)

    monkeypatch.setattr(core, "_commit_install", linked_first)
    with pytest.raises(PatchError, match="連結"):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert outside.read_bytes() == b"font"
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]


def test_a_file_this_patcher_could_not_take_away_is_left_listed_for_the_next(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    scratch = game / f"{RECORD}.bokucho-tmp"
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs) -> None:
        if self.name == scratch.name:
            raise PermissionError(errno.EACCES, "in use", str(self))
        real_unlink(self, *args, **kwargs)

    _cut_short(monkeypatch, scratch.name)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PatchError):
        apply(game, _font_patch())
    monkeypatch.undo()
    assert scratch.exists()
    assert scratch.name.encode() in (game / core.LOCK).read_bytes()

    apply(game, _font_patch())
    restore(game, _font_patch())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


_STOPPED = """
import os, pickle, sys
from pathlib import Path
from bokucho.patcher import core

game, saved, point = sys.argv[1:]
patch = pickle.loads(Path(saved).read_bytes())
journal = core._journal

def journal_then_stop(path, own):
    journal(path, own)
    if path.name == point:
        os._exit(3)

def stop(folder, patch, moved):
    os._exit(3)

replace = os.replace

def replace_then_stop(source, destination):
    replace(source, destination)
    if str(destination).endswith(".bak"):  # the original aside, its translation not yet in
        os._exit(3)

if point == "swap":
    core._swap = stop
elif point == "renamed":
    os.replace = replace_then_stop
else:
    core._journal = journal_then_stop
core.apply(Path(game), patch)
"""


def _stopped_at(tmp_path: Path, patch: Patch, point: str) -> Path:
    """A game folder an apply was stopped in at ``point``, as a closed window stops it."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    saved = tmp_path / "patch.pickle"
    saved.write_bytes(pickle.dumps(patch))
    done = subprocess.run(
        [sys.executable, "-c", _STOPPED, str(game), str(saved), point],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert done.returncode == 3, done.stderr
    assert list(game.glob("*.bokucho-tmp")), "the stopped patcher left what it was writing"
    return game


@pytest.mark.parametrize(
    "point,first",
    [
        (f"{RECORD}.bokucho-tmp", "apply"),
        ("font.ttf.bokucho-tmp", "apply"),
        ("Game.exe.bokucho-tmp", "apply"),
        ("swap", "apply"),
        ("font.ttf.bokucho-tmp", "restore"),
        ("swap", "restore"),
        ("renamed", "restore"),
    ],
)
def test_what_a_stopped_patcher_was_writing_is_cleared_by_the_next(
    tmp_path: Path, point: str, first: str
) -> None:
    game = _stopped_at(tmp_path, _font_patch(), point)

    if first == "apply":
        apply(game, _font_patch())
        assert (game / "Game.exe").read_bytes() == TRANSLATED
    restore(game, _font_patch())

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_file_at_a_scratch_name_since_a_finished_apply_is_not_the_patchers(
    tmp_path: Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _font_patch())
    (game / "font.ttf.bokucho-tmp").write_bytes(b"not the patch's")

    restore(game, _font_patch())

    assert (game / "font.ttf.bokucho-tmp").read_bytes() == b"not the patch's"


def test_a_file_put_at_a_stopped_patchers_scratch_name_since_is_kept_and_named(
    tmp_path: Path,
) -> None:
    game = _stopped_at(tmp_path, _font_patch(), "swap")
    scratch = game / "Game.exe.bokucho-tmp"
    other = tmp_path / "other"
    other.write_bytes(b"not the patch's")
    os.replace(other, scratch)

    with pytest.raises(PatchError, match=re.escape(scratch.name)):
        apply(game, _font_patch())

    assert scratch.read_bytes() == b"not the patch's"


def test_a_stopped_patchers_file_that_cannot_be_removed_is_named_and_kept_for_the_next(
    tmp_path: Path, monkeypatch
) -> None:
    game = _stopped_at(tmp_path, _font_patch(), "swap")
    scratch = game / "Game.exe.bokucho-tmp"
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs) -> None:
        if self.name == scratch.name:
            raise PermissionError(errno.EACCES, "in use", str(self))
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    for act in (apply, restore):
        with pytest.raises(PatchError, match=re.escape(scratch.name)):
            act(game, _font_patch())
    monkeypatch.undo()
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert (game / RECORD).exists()

    apply(game, _font_patch())
    restore(game, _font_patch())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


@pytest.mark.parametrize("entry", ["Game.exe", "../beside.bokucho-tmp", "sub/x.bokucho-tmp"])
def test_a_lock_file_naming_what_no_patcher_wrote_there_removes_nothing(
    tmp_path: Path, request, entry: str
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.bokucho-tmp").write_bytes(b"outside")
    (tmp_path / "beside.bokucho-tmp").write_bytes(b"beside")
    if entry.startswith("sub/"):
        request.getfixturevalue("symlink")(game / "sub", outside)
    named = os.stat(game / entry)
    line = json.dumps([entry, named.st_dev, named.st_ino]).encode() + b"\n"
    (game / core.LOCK).write_bytes(core._LOCK_MARK + line)

    apply(game, _patch())
    restore(game, _patch())

    assert (outside / "x.bokucho-tmp").read_bytes() == b"outside"
    assert (tmp_path / "beside.bokucho-tmp").read_bytes() == b"beside"
    assert (game / "Game.exe").read_bytes() == ORIGINAL


@pytest.mark.parametrize(
    "held",
    [b"notes", core._LOCK_MARK + b'["Game.exe.bokucho-tmp", 1, 2]\n'],
    ids=["another-programs", "a-stopped-patchers-list"],
)
def test_a_lock_file_held_elsewhere_is_left_where_it_is(
    tmp_path: Path, monkeypatch, held: bytes
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    lock = game / core.LOCK
    lock.write_bytes(held)

    def busy(*_args) -> None:
        raise OSError(errno.EACCES, "locked")

    if sys.platform == "win32":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", busy)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", busy)
    with pytest.raises(PatchError, match="另一個補丁程式"):
        apply(game, _patch())
    monkeypatch.undo()

    assert lock.read_bytes() == held


def _listed(game: Path, name: str) -> bytes:
    """The lock file's line for ``name`` in ``game``, as a patcher writes it."""
    made = os.lstat(game / name)
    return json.dumps([name, made.st_dev, made.st_ino]).encode() + b"\n"


@pytest.mark.parametrize(
    "name",
    ["a\0b.bokucho-tmp", "a?b.bokucho-tmp", "x" * 300 + ".bokucho-tmp"],
    ids=["nul", "wildcard", "too-long"],
)
def test_a_listed_name_no_file_can_have_goes_unheeded(tmp_path: Path, name: str) -> None:
    # Nothing can be there to take away; the rest of the list is cleared, and the next
    # patcher is not held up by it.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    scratch = game / "Game.exe.bokucho-tmp"
    scratch.write_bytes(b"half")
    odd = json.dumps([name, 0, 0]).encode() + b"\n"
    (game / core.LOCK).write_bytes(core._LOCK_MARK + odd + _listed(game, scratch.name))

    apply(game, _patch())

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "Game.exe.bak"]


def test_a_listed_line_nested_past_what_the_parser_follows_goes_unheeded(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    scratch = game / "Game.exe.bokucho-tmp"
    scratch.write_bytes(b"half")
    nested = b"[" * 100_000 + b"]" * 100_000 + b"\n"
    (game / core.LOCK).write_bytes(core._LOCK_MARK + nested + _listed(game, scratch.name))

    apply(game, _patch())

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "Game.exe.bak"]


def test_a_record_nested_past_what_the_parser_follows_reads_as_damaged(tmp_path: Path) -> None:
    # As any record that is not JSON: told, and the game left as it is.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    nested = b"[" * 100_000 + b"]" * 100_000
    said: dict[str, set[str]] = {"apply": set(), "restore": set()}

    for record in (b"{", nested):
        (game / RECORD).write_bytes(record)
        for act in (apply, restore):
            with pytest.raises(PatchError) as refused:
                act(game, _font_patch())
            said[act.__name__].add(str(refused.value))
        assert (game / RECORD).read_bytes() == record

    assert len(said["apply"]) == len(said["restore"]) == 1
    assert "已損壞" in said["apply"].pop()
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_list_past_its_first_mebibyte_is_cleared_whole(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    scratch = game / "Game.exe.bokucho-tmp"
    scratch.write_bytes(b"half")
    long = json.dumps(["x" * (1 << 20), 0, 0]).encode() + b"\n"
    (game / core.LOCK).write_bytes(core._LOCK_MARK + long + _listed(game, scratch.name))

    apply(game, _patch())

    assert (game / "Game.exe").read_bytes() == TRANSLATED
    assert not scratch.exists()
    assert not (game / core.LOCK).exists()


def test_a_listed_file_that_cannot_be_looked_at_stays_listed(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    scratch = game / "Game.exe.bokucho-tmp"
    scratch.write_bytes(b"half")
    (game / "font.ttf.bokucho-tmp").write_bytes(b"gone since")
    gone = _listed(game, "font.ttf.bokucho-tmp")
    (game / "font.ttf.bokucho-tmp").unlink()
    # Left untouched while an entry stays: a rewrite cut short could lose it.
    listed = core._LOCK_MARK + gone + _listed(game, scratch.name)
    (game / core.LOCK).write_bytes(listed)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == scratch:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(PatchError, match=re.escape(scratch.name) + ".*中斷"):
        apply(game, _patch())
    monkeypatch.undo()

    assert (game / core.LOCK).read_bytes() == listed
    apply(game, _patch())
    assert not scratch.exists()


def test_the_list_is_whole_however_little_each_write_takes(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data)[:7]))
    seen: list[bytes] = []

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        held = core._HELD.get()
        assert held is not None
        os.lseek(held[1], 0, os.SEEK_SET)
        seen.append(os.read(held[1], 1 << 20))
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError, match="I/O error"):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert seen[0].startswith(core._LOCK_MARK)
    lines = seen[0][len(core._LOCK_MARK) :].splitlines()
    assert sorted(json.loads(line)[0] for line in lines) == sorted(
        ["Game.exe.bokucho-tmp", f"{RECORD}.bokucho-tmp", "font.ttf.bokucho-tmp"]
    )
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_file_added_in_a_folder_the_game_lacks_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("Fonts/extra.ttf", b"font"),))

    def forbidden(path: Path, data: bytes) -> None:
        raise AssertionError(f"{path} was written")

    def not_staged(folder: Path, item: PatchedFile, prepared, staged) -> None:
        raise AssertionError(f"{item.name} was staged")

    monkeypatch.setattr(core, "_write_new", forbidden)
    monkeypatch.setattr(core, "_stage", not_staged)
    with pytest.raises(PatchError, match="找不到「Fonts」資料夾"):
        apply(game, patch)
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]
    (game / "Fonts").mkdir()
    apply(game, patch)
    assert (game / "Fonts" / "extra.ttf").read_bytes() == b"font"


@pytest.mark.parametrize(
    "second",
    ["font.ttf", "FONT.ttf", "font.ttf.", "font.ttf "],
    ids=["same", "case", "dot", "space"],
)
def test_two_additions_at_one_name_are_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch, second: str
) -> None:
    # Both would be recorded, and the last written would stand for both.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"one"), PatchAdded(second, b"two")))

    def forbidden(*args) -> None:
        raise AssertionError("written")

    monkeypatch.setattr(core, "_write_new", forbidden)
    monkeypatch.setattr(core, "_stage", forbidden)
    with pytest.raises(PatchError, match=re.escape(core._DAMAGED)):
        apply(game, patch)
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


@pytest.mark.parametrize(
    "name",
    [
        RECORD,
        RECORD.upper(),
        f"{RECORD}.",
        f"{RECORD} ",
        f"{RECORD}:stream",
        core.LOCK,
        "font.ttf.bokucho-tmp",
        "Game.exe.bak",
    ],
)
def test_an_addition_at_a_name_the_patcher_keeps_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    # Its record, its lock, what it stages or the original it keeps would be overwritten:
    # Windows reads a name with a dot or space at its end as the name without it, and one
    # with a colon as a stream of the file before it.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"), PatchAdded(name, b"x")))

    def forbidden(*args) -> None:
        raise AssertionError("written")

    monkeypatch.setattr(core, "_write_new", forbidden)
    monkeypatch.setattr(core, "_stage", forbidden)
    with pytest.raises(PatchError, match=re.escape(core._DAMAGED)):
        apply(game, patch)
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


@pytest.mark.parametrize("when", ["while-written", "after", "edited-in-place"])
def test_a_file_another_program_put_at_an_added_name_is_not_rolled_back(
    tmp_path: Path, monkeypatch, when: str
) -> None:
    # Only what this install published, and still the very file published, as it was
    # published, is taken away: one edited in place keeps its identity.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    rewrite = core._rewrite

    def rewrite_raced(path: Path, data: bytes, **kwargs):
        if path.name == "extra.dll" and when == "while-written":
            path.write_bytes(b"another program's")
            raise OSError(errno.EIO, "the write failed")
        return rewrite(path, data, **kwargs)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        if when == "after":
            # Moved, not deleted: a deleted file's identity could go to the one put there.
            (folder / "extra.dll").rename(tmp_path / "spare.dll")
            (folder / "extra.dll").write_bytes(b"another program's")
        if when == "edited-in-place":
            (folder / "extra.dll").write_bytes(b"another program's")
        raise OSError(errno.EIO, "the swap failed")

    monkeypatch.setattr(core, "_rewrite", rewrite_raced)
    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError, match="failed"):
        apply(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "extra.dll"]
    assert (game / "extra.dll").read_bytes() == b"another program's"
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_file_another_program_puts_at_an_added_name_before_it_goes_in_is_kept(
    tmp_path: Path, monkeypatch
) -> None:
    # Free when the install began, the name is taken by the time the addition goes in.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    record = core._record

    def record_then_raced(folder: Path, patch: Patch, *lists) -> None:
        record(folder, patch, *lists)
        (folder / "font.ttf").write_bytes(b"another program's")

    monkeypatch.setattr(core, "_record", record_then_raced)
    with pytest.raises(PatchError, match=re.escape(core._IN_THE_WAY.format(name="font.ttf"))):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]
    assert (game / "font.ttf").read_bytes() == b"another program's"
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_an_added_file_rollback_cannot_look_at_keeps_the_record_naming_it(
    tmp_path: Path, monkeypatch
) -> None:
    # Neither known to be gone nor known to be another's, it is restore's to take away.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    replace_file, lstat = os.replace, os.lstat

    def unreadable(path, *args, **kwargs):
        if Path(path).name == "font.ttf":
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return lstat(path, *args, **kwargs)

    def swap_fails(source, destination) -> None:
        if Path(destination).name == "Game.exe.bak":
            monkeypatch.setattr(core.os, "lstat", unreadable)
            raise OSError(errno.EIO, "the swap failed")
        replace_file(source, destination)

    monkeypatch.setattr(core.os, "replace", swap_fails)
    with pytest.raises(PatchError, match="the swap failed"):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert (game / "font.ttf").read_bytes() == b"font"
    assert json.loads((game / RECORD).read_bytes())["added"] == [_FONT]
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    restore(game, _font_patch())
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_an_added_file_gone_before_the_rollback_takes_the_record_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    # Gone, it is nothing restore would take away: no record is left naming it.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)

    def swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
        (folder / "extra.dll").rename(tmp_path / "elsewhere.dll")
        raise OSError(errno.EIO, "the swap failed")

    monkeypatch.setattr(core, "_swap", swap)
    with pytest.raises(PatchError, match="the swap failed"):
        apply(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_an_original_that_cannot_be_put_back_keeps_the_record(
    tmp_path: Path, monkeypatch
) -> None:
    # Its translation stays in place; without the record, nothing says it is not the game's.
    game = tmp_path / "game"
    game.mkdir()
    for name in ("Game.exe", "Game.dat"):
        (game / name).write_bytes(ORIGINAL)
    second = replace(_patch().files[0], name="Game.dat")
    patch = replace(_font_patch(), files=(*_patch().files, second))
    replace_file = os.replace

    def stuck(source, destination) -> None:
        if Path(source).name == "Game.dat.bokucho-tmp":
            raise OSError(errno.EIO, "the swap failed")
        if Path(source).name == "Game.exe.bak":
            raise PermissionError(errno.EACCES, "it would not move back", str(source))
        replace_file(source, destination)

    monkeypatch.setattr(core.os, "replace", stuck)
    with pytest.raises(PatchError, match="the swap failed"):
        apply(game, patch)
    monkeypatch.undo()

    assert (game / "Game.exe").read_bytes() == TRANSLATED
    assert (game / "Game.exe.bak").read_bytes() == ORIGINAL
    assert (game / "Game.dat").read_bytes() == ORIGINAL
    assert (game / RECORD).is_file()
    restore(game, patch)
    assert sorted(path.name for path in game.iterdir()) == ["Game.dat", "Game.exe"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


@pytest.mark.parametrize("record", ["another patch's", "damaged", "this patch's, stopped"])
def test_the_status_says_whether_a_record_here_lets_the_patch_apply(
    tmp_path: Path, record: str
) -> None:
    # The window offers apply only for what apply takes: a record this patch's own stopped
    # install left is taken up again; any other is refused, and said so before.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    if record == "damaged":
        (game / RECORD).write_text("{", encoding="utf-8")
    elif record == "another patch's":
        (game / "version.dll").write_bytes(b"MZ")
        listed = [{"name": "version.dll", "sha256": hashlib.sha256(b"MZ").hexdigest()}]
        (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")
    else:
        _stopped_before_the_swap(game)

    found = core.inspect(game, _font_patch())

    if record == "this patch's, stopped":
        assert found == core.FolderState(core.State.ORIGINAL, True)
        apply(game, _font_patch())
        assert (game / "Game.exe").read_bytes() == TRANSLATED
        return
    said = core._DAMAGED_RECORD_FOUND if record == "damaged" else core._OTHER_RECORD
    assert found.state is not core.State.ORIGINAL and not found.restorable
    assert core.describe(found, _font_patch()) == said


def _held_open(monkeypatch, name: str) -> None:
    """The file by ``name`` cannot be taken away: another program holds it open."""
    unlink = Path.unlink

    def held(self: Path, *args, **kwargs) -> None:
        if self.name == name:
            error = PermissionError(errno.EACCES, "in use", str(self))
            error.winerror = core.ERROR_SHARING_VIOLATION
            raise error
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held)


def test_an_added_file_held_open_at_restore_keeps_the_record_for_the_next(
    tmp_path: Path, monkeypatch
) -> None:
    # Taken away later, it would be one no restore knows of once the record is gone.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _two_added())

    _held_open(monkeypatch, "font.ttf")
    with pytest.raises(PatchError, match=re.escape(core._IN_USE)):
        restore(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    # Still this patch's own: apply may take it up again, as after a stopped install.
    assert core.inspect(game, _two_added()) == core.FolderState(core.State.ORIGINAL, True)
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def _older(font: bytes = b"older font") -> Patch:
    """Another version of the patch :func:`_two_added` makes: other text, other files."""
    translated = b"older translation " * 4096
    delta, window = make_delta(ORIGINAL, translated)
    item = PatchedFile("Game.exe", _state(ORIGINAL), _state(translated), delta, window)
    added = (PatchAdded("font.ttf", font), PatchAdded("extra.dll", b"MZ older"))
    return Patch("測試", (item,), added)


@pytest.mark.parametrize("font", [b"older font", b"font"], ids=["other-font", "same-font"])
def test_a_restore_of_another_version_held_open_partway_is_finished_by_the_next(
    tmp_path: Path, monkeypatch, font: bytes
) -> None:
    # The record kept is the other version's: beside the originals this patch would take
    # it for another patch's, and could neither apply nor restore. Whatever it lists, even
    # a file this patch adds too, is the restore's to finish, not an apply's to take up.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _older(font))
    assert core.inspect(game, _two_added()) == core.FolderState(core.State.UNKNOWN, True)

    _held_open(monkeypatch, "font.ttf")
    with pytest.raises(PatchError, match=re.escape(core._IN_USE)):
        restore(game, _two_added())
    monkeypatch.undo()

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    found = core.inspect(game, _two_added())
    assert found == core.FolderState(core.State.FOREIGN, True)
    said = core.describe(found, _two_added())
    assert "另一個版本" in said
    with pytest.raises(PatchError, match=re.escape(said)):
        apply(game, _two_added())
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_damaged_record_a_held_restore_leaves_is_said_at_once_and_finished_next(
    tmp_path: Path, monkeypatch
) -> None:
    # The restore that finishes it reads a record no longer damaged: the warning that files
    # the record lost track of may stay is given by the restore that found it damaged.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _two_added())
    document = json.loads((game / RECORD).read_text(encoding="utf-8"))
    document["added"].append({"name": "lost.dll"})
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")
    said: list[str] = []

    _held_open(monkeypatch, "font.ttf")
    with pytest.raises(PatchError, match=re.escape(core._IN_USE)):
        restore(game, _two_added(), said.append)
    monkeypatch.undo()

    assert core._DAMAGED_RECORD_RESTORING in said
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    # Its list no longer damaged, it still names this patch: marked, it is the restore's to
    # finish, not an apply's to take up.
    assert core.inspect(game, _two_added()) == core.FolderState(core.State.FOREIGN, True)
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_restore_that_cannot_mark_another_versions_record_moves_no_original(
    tmp_path: Path, monkeypatch
) -> None:
    # Marked first or not begun: no original is back beside a record left unmarked.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _older())
    before = {path.name: path.read_bytes() for path in game.iterdir()}
    said = core._IN_THE_WAY.format(name=f"{RECORD}.bokucho-tmp")

    def rewrite(path: Path, data: bytes, **kwargs):
        raise PatchError(said)

    monkeypatch.setattr(core, "_rewrite", rewrite)
    with pytest.raises(PatchError, match=re.escape(said)):
        restore(game, _two_added())
    monkeypatch.undo()

    assert {path.name: path.read_bytes() for path in game.iterdir()} == before
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_restore_stopped_once_the_originals_are_back_is_finished_by_the_next(
    tmp_path: Path, monkeypatch
) -> None:
    # Stopped there, whatever stopped it, it leaves the originals beside the other version's
    # record: unmarked, this patch could neither apply nor restore.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _older())
    replace_ = os.replace

    def replace_then_stop(source, destination) -> None:
        replace_(source, destination)
        if Path(destination).name == "Game.exe":
            raise KeyboardInterrupt

    monkeypatch.setattr(core.os, "replace", replace_then_stop)
    with pytest.raises(KeyboardInterrupt):
        restore(game, _two_added())
    monkeypatch.undo()

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert core.inspect(game, _two_added()) == core.FolderState(core.State.FOREIGN, True)
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_restore_stopped_before_the_originals_go_back_still_says_what_was_compiled(
    tmp_path: Path, monkeypatch
) -> None:
    # Marked first, the record keeps saying the translation in place is one, so Bokucho
    # never reads it as the game's own.
    from bokucho.engines.base import _compiled

    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(TRANSLATED)
    (game / "Game.exe.bak").write_bytes(ORIGINAL)
    compiled = {"Game.exe": hashlib.sha256(TRANSLATED).hexdigest()}
    document = {"added": [], "recipe": "0" * 64, "compiled": compiled}
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")
    replace_ = os.replace

    def stop_before(source, destination) -> None:
        if Path(destination).name == "Game.exe":
            raise KeyboardInterrupt
        replace_(source, destination)

    monkeypatch.setattr(core.os, "replace", stop_before)
    with pytest.raises(KeyboardInterrupt):
        restore(game, _patch())
    monkeypatch.undo()

    marked = json.loads((game / RECORD).read_text(encoding="utf-8"))
    assert marked["restoring"] == {"Game.exe": hashlib.sha256(ORIGINAL).hexdigest()}
    assert _compiled(game / "Game.exe") == compiled["Game.exe"]
    assert restore(game, _patch()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


FILES = {
    "Game.exe": ORIGINAL,
    "Data.bin": b"original data " * 4096,
    "Movie.bin": b"original movie " * 4096,
}


def _versioned(
    text: bytes = b"translated", names: tuple[str, ...] = ("Game.exe", "Data.bin")
) -> Patch:
    """A patch translating ``names`` and adding a font; ``text`` tells versions apart."""
    items = []
    for name in names:
        translated = text + b" " + FILES[name]
        delta, window = make_delta(FILES[name], translated)
        items.append(PatchedFile(name, _state(FILES[name]), _state(translated), delta, window))
    return Patch("測試", tuple(items), (PatchAdded("font.ttf", text),))


def _restore_stopped_at(game: Path, patch: Patch, name: str, monkeypatch) -> None:
    """Restore with ``patch``, stopped as the original of ``name`` was to go back."""
    replace_ = os.replace

    def stop_at(source, destination) -> None:
        if Path(destination).name == name:
            raise KeyboardInterrupt
        replace_(source, destination)

    monkeypatch.setattr(core.os, "replace", stop_at)
    with pytest.raises(KeyboardInterrupt):
        restore(game, patch)
    monkeypatch.undo()


def test_a_restore_stopped_partway_is_finished_only_once_its_originals_are_back(
    tmp_path: Path, monkeypatch
) -> None:
    # Finished by a patch that puts none of them back, the record would go while another
    # version's translation stays in place, read from then on as the game's own.
    game = tmp_path / "game"
    game.mkdir()
    for name in ("Game.exe", "Data.bin"):
        (game / name).write_bytes(FILES[name])
    apply(game, _versioned(b"older"))
    _restore_stopped_at(game, _versioned(), "Data.bin", monkeypatch)

    assert (game / "Game.exe").read_bytes() == ORIGINAL
    before = {path.name: path.read_bytes() for path in game.iterdir()}
    found = core.inspect(game, _patch())
    assert found == core.FolderState(core.State.FOREIGN, False)
    assert core.describe(found, _patch()) == core._OTHER_RECORD
    with pytest.raises(PatchError):
        restore(game, _patch())
    assert {path.name: path.read_bytes() for path in game.iterdir()} == before
    assert restore(game, _versioned()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Data.bin", "Game.exe"]
    assert (game / "Data.bin").read_bytes() == FILES["Data.bin"]


def test_a_restore_putting_back_its_own_keeps_the_record_while_another_original_is_to_go_back(
    tmp_path: Path, monkeypatch
) -> None:
    # Taken away, the record would go while the earlier restore's translation stays, read
    # from then on as the game's own. Marked again, it still names what that one puts back.
    game = tmp_path / "game"
    game.mkdir()
    for name, original in FILES.items():
        (game / name).write_bytes(original)
    apply(game, _versioned(b"older", tuple(FILES)))
    _restore_stopped_at(game, _versioned(), "Data.bin", monkeypatch)

    with pytest.raises(PatchError, match=re.escape(core._OTHER_RECORD)):
        restore(game, _versioned(names=("Movie.bin",)))

    assert (game / "Movie.bin").read_bytes() == FILES["Movie.bin"]
    left = sorted([*FILES, "Data.bin.bak", RECORD, "font.ttf"])
    assert sorted(path.name for path in game.iterdir()) == left
    assert core.inspect(game, _patch()) == core.FolderState(core.State.FOREIGN, False)
    assert restore(game, _versioned()) == []
    assert sorted(path.name for path in game.iterdir()) == sorted(FILES)


def test_a_restore_putting_another_original_where_one_is_still_to_go_back_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    # Named over it, the earlier restore's original would read as back once this one's is:
    # its record could go while its backup and its translation's additions stay.
    game = tmp_path / "game"
    game.mkdir()
    for name, original in FILES.items():
        (game / name).write_bytes(original)
    apply(game, _versioned(b"older", tuple(FILES)))
    _restore_stopped_at(game, _versioned(), "Data.bin", monkeypatch)
    updated = b"updated data " * 4096  # the game updated since: another original there
    (game / "Data.bin").write_bytes(updated)
    items = []
    for name, original in (("Data.bin", updated), ("Movie.bin", FILES["Movie.bin"])):
        delta, window = make_delta(original, b"newer " + original)
        items.append(
            PatchedFile(name, _state(original), _state(b"newer " + original), delta, window)
        )
    before = {path.name: path.read_bytes() for path in game.iterdir()}

    with pytest.raises(PatchError, match=re.escape(core._OTHER_RECORD)):
        restore(game, Patch("測試", tuple(items)))
    assert {path.name: path.read_bytes() for path in game.iterdir()} == before


def test_an_original_an_earlier_restore_put_back_stays_though_the_record_lists_it_as_added(
    tmp_path: Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    for name in ("Game.exe", "Data.bin"):
        (game / name).write_bytes(FILES[name])
    apply(game, _patch())
    digest = hashlib.sha256(FILES["Data.bin"]).hexdigest()
    document = {
        "added": [{"name": "Data.bin", "sha256": digest}],
        "restoring": {"Data.bin": digest},
    }
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")

    assert restore(game, _patch()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Data.bin", "Game.exe"]
    assert (game / "Data.bin").read_bytes() == FILES["Data.bin"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_mark_naming_a_file_outside_the_game_folder_is_not_taken_as_put_back(
    tmp_path: Path,
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (tmp_path / "outside.bin").write_bytes(b"outside")
    digest = hashlib.sha256(b"outside").hexdigest()
    document = {"added": [], "restoring": {"../outside.bin": digest}}
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")

    assert core.inspect(game, _patch()) == core.FolderState(core.State.FOREIGN, False)


@pytest.mark.parametrize("text", ['{"added": [', "[]"], ids=["not-json", "not-an-object"])
def test_a_damaged_record_a_restore_could_not_take_away_is_taken_away_by_the_next(
    tmp_path: Path, monkeypatch, text: str
) -> None:
    # Left as it was beside the originals, it reads as damaged: neither apply nor restore
    # could go on. The warning that what it listed may stay is given at once.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _font_patch())
    (game / RECORD).write_text(text, encoding="utf-8")
    said: list[str] = []

    _held_open(monkeypatch, RECORD)
    with pytest.raises(PatchError):
        restore(game, _font_patch(), said.append)
    monkeypatch.undo()

    assert core._DAMAGED_RECORD_RESTORING in said
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert core.inspect(game, _font_patch()) == core.FolderState(core.State.FOREIGN, True)
    assert restore(game, _font_patch()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]


def test_a_damaged_record_is_said_by_a_restore_whose_original_will_not_go_back(
    tmp_path: Path, monkeypatch
) -> None:
    # Marked, the record reads as whole: the restore that finishes it finds nothing damaged.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _font_patch())
    (game / RECORD).write_text('{"added": [', encoding="utf-8")
    said: list[str] = []
    move = os.replace

    def held(source, target, *args, **kwargs) -> None:
        if Path(source).name == "Game.exe.bak":
            error = PermissionError(errno.EACCES, "in use", str(source))
            error.winerror = core.ERROR_SHARING_VIOLATION
            raise error
        move(source, target, *args, **kwargs)

    monkeypatch.setattr(core.os, "replace", held)
    with pytest.raises(PatchError, match=re.escape(core._IN_USE)):
        restore(game, _font_patch(), said.append)
    monkeypatch.undo()

    assert core._DAMAGED_RECORD_RESTORING in said
    assert (game / "Game.exe").read_bytes() == TRANSLATED
    assert restore(game, _font_patch()) == []
    assert (game / "Game.exe").read_bytes() == ORIGINAL
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]


def test_what_another_versions_record_lists_is_taken_from_one_read(
    tmp_path: Path, monkeypatch
) -> None:
    # Read again, a record that cannot be read lists nothing: the other version's files would
    # stay, with no record of them, for the game to go on loading.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _older())

    monkeypatch.setattr(core, "_recorded", lambda folder: [])
    _held_open(monkeypatch, "font.ttf")
    with pytest.raises(PatchError, match=re.escape(core._IN_USE)):
        restore(game, _two_added())
    monkeypatch.undo()

    # What is left is still listed in the record marked, for the next restore.
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]
    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def _stopped_install_of_two_added(game: Path) -> None:
    """What an install of :func:`_two_added` stopped once the font was in leaves."""
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    entry = {"name": "font.ttf", "sha256": hashlib.sha256(b"font").hexdigest()}
    document = {"added": [entry], "patch": core._identity(_two_added())}
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")
    (game / "font.ttf").write_bytes(b"font")


def test_what_a_stopped_installs_record_lists_is_taken_from_one_read(
    tmp_path: Path, monkeypatch
) -> None:
    # Read again in vain, the record put out would list only what this install adds: what
    # the stopped one put in would stay when the patch is restored.
    game = tmp_path / "game"
    _stopped_install_of_two_added(game)

    monkeypatch.setattr(core, "_recorded", lambda folder: [])
    apply(game, _two_added())
    monkeypatch.undo()

    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


@pytest.mark.parametrize("text", ['{"added": [', '{"added": null}'], ids=["not-json", "not-a-list"])
def test_a_record_damaged_after_the_checks_stops_the_install_unwritten(
    tmp_path: Path, monkeypatch, text: str
) -> None:
    # Read as listing nothing, the record put out would leave out what the stopped install
    # put in.
    game = tmp_path / "game"
    _stopped_install_of_two_added(game)
    stage = core._stage

    def stage_then_damage(folder: Path, *args) -> None:
        stage(folder, *args)
        (folder / RECORD).write_text(text, encoding="utf-8")

    monkeypatch.setattr(core, "_stage", stage_then_damage)
    with pytest.raises(PatchError, match=re.escape(core._DAMAGED_RECORD_FOUND)):
        apply(game, _two_added())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", RECORD, "font.ttf"]
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_record_replaced_between_reads_is_judged_by_the_first(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    reads = iter([None, {"added": []}])
    monkeypatch.setattr(core, "read_record", lambda folder: next(reads))

    assert core._own_record(game, _font_patch()) is False


def test_a_record_field_utf8_cannot_hold_stops_no_restore(tmp_path: Path) -> None:
    # Kept in the marked record as read, it could not be written: the restore would stop
    # before any original went back.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _older())
    text = (game / RECORD).read_text(encoding="utf-8").rstrip()
    (game / RECORD).write_text(text[:-1] + ', "note": "\\ud800"}', encoding="utf-8")

    assert restore(game, _two_added()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_an_entry_utf8_cannot_hold_stops_no_apply_taking_the_record_up(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    core._record(game, _font_patch(), [], [])
    document = json.loads((game / RECORD).read_text(encoding="utf-8"))
    document["added"].append({"name": "old.dat", "sha256": "\ud800"})
    (game / RECORD).write_text(json.dumps(document), encoding="utf-8")

    apply(game, _font_patch())
    assert {"name": "old.dat", "sha256": "\ud800"} in core._recorded(game)
    assert (game / "Game.exe").read_bytes() == TRANSLATED


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_a_folder_not_there_when_the_patcher_starts_is_refused_before_any_check(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    # Unlocked, a folder put there meanwhile would be patched while another patcher works.
    game = tmp_path / "game"
    ran: list[object] = []
    name = f"_{operation}"
    body = getattr(core, name)

    def appears_meanwhile(folder: Path, patch: Patch, log) -> object:
        ran.append(core._HELD.get())
        game.mkdir()
        (game / "Game.exe").write_bytes(ORIGINAL)
        return body(folder, patch, log)

    monkeypatch.setattr(core, name, appears_meanwhile)
    with pytest.raises(PatchError, match=re.escape("這個資料夾裡找不到 Game.exe。")):
        getattr(core, operation)(game, _patch())

    assert ran == []
    assert not game.exists()


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_a_folder_put_there_once_the_patcher_found_none_is_still_said_not_to_be_there(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    # Looked at again, it could be said to hold the originals ready to patch when nothing was
    # done, or a read of it could fail past what the patcher puts into words.
    game = tmp_path / "game"
    is_dir = Path.is_dir

    def put_there_once_looked_for(self: Path, *args, **kwargs) -> bool:
        found = is_dir(self, *args, **kwargs)
        if self == game and not found:
            game.mkdir()
            (game / "Game.exe").write_bytes(ORIGINAL)
        return found

    monkeypatch.setattr(Path, "is_dir", put_there_once_looked_for)
    with pytest.raises(PatchError, match=re.escape("這個資料夾裡找不到 Game.exe。")):
        getattr(core, operation)(game, _patch())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_record_of_another_patch_adding_the_same_file_is_not_taken_up(tmp_path: Path) -> None:
    # Two patches can add one file byte for byte: taken for this patch's, the other's record
    # and its font would go with this patch's restore.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    (game / "Data.bin").write_bytes(b"data " * 4096)
    delta, window = make_delta(b"data " * 4096, b"translated data " * 4096)
    item = PatchedFile(
        "Data.bin", _state(b"data " * 4096), _state(b"translated data " * 4096), delta, window
    )
    other = Patch("其他", (item,), (PatchAdded("font.ttf", b"font"),))
    apply(game, other)
    before = {path.name: path.read_bytes() for path in game.iterdir()}

    found = core.inspect(game, _font_patch())
    assert found == core.FolderState(core.State.FOREIGN, False)
    with pytest.raises(PatchError, match=re.escape(core._OTHER_RECORD)):
        apply(game, _font_patch())
    with pytest.raises(PatchError):
        restore(game, _font_patch())

    assert {path.name: path.read_bytes() for path in game.iterdir()} == before
    assert restore(game, other) == []
    assert sorted(path.name for path in game.iterdir()) == ["Data.bin", "Game.exe"]


def test_a_record_this_patchs_stopped_install_wrote_is_taken_up_again(tmp_path: Path) -> None:
    # Published before the files it lists: what a process stopped there leaves.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    core._record(game, _font_patch(), [], list(_font_patch().added))

    assert core.inspect(game, _font_patch()) == core.FolderState(core.State.ORIGINAL, True)
    assert restore(game, _font_patch()) == []
    assert sorted(path.name for path in game.iterdir()) == ["Game.exe"]


def test_a_different_file_put_at_an_added_name_after_the_checks_is_refused_unwritten(
    tmp_path: Path, monkeypatch
) -> None:
    # Put there while the game file was staged: kept as another's, it would be what the
    # patch reports as installed.
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    stage = core._stage

    def stage_raced(folder: Path, *args) -> None:
        stage(folder, *args)
        (folder / "font.ttf").write_bytes(b"another program's")

    monkeypatch.setattr(core, "_stage", stage_raced)
    with pytest.raises(PatchError, match="「font.ttf」，而且內容不同"):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert sorted(path.name for path in game.iterdir()) == ["Game.exe", "font.ttf"]
    assert (game / "font.ttf").read_bytes() == b"another program's"
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_backup_name_holding_a_link_pointing_nowhere_is_refused(
    tmp_path: Path, symlink
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    symlink(game / "Game.exe.bak", tmp_path / "gone")

    with pytest.raises(PatchError, match=r"Game\.exe\.bak"):
        apply(game, _patch())

    assert (game / "Game.exe.bak").is_symlink()
    assert (game / "Game.exe").read_bytes() == ORIGINAL


@pytest.mark.parametrize("name", ["Game.exe.bokucho-tmp", f"{RECORD}.bokucho-tmp"])
def test_a_scratch_file_whose_identity_cannot_be_read_is_closed(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    made = []
    real_open, real_fstat = Path.open, os.fstat

    def open_(self: Path, mode: str = "r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if mode == "xb" and self.name == name:
            made.append(stream)
        return stream

    def fstat(fd: int):
        if any(not stream.closed and stream.fileno() == fd for stream in made):
            raise OSError(errno.EIO, "I/O error")
        return real_fstat(fd)

    monkeypatch.setattr(Path, "open", open_)
    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(PatchError, match="I/O error"):
        apply(game, _font_patch())
    monkeypatch.undo()

    assert made and all(stream.closed for stream in made)
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_failure_the_patcher_has_no_words_for_is_still_a_patch_error(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)

    def fail(*_args) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(core, "_swap", fail)
    with pytest.raises(PatchError, match="I/O error"):
        apply(game, _patch())
    monkeypatch.undo()
    assert (game / "Game.exe").read_bytes() == ORIGINAL

    apply(game, _patch())
    monkeypatch.setattr(core, "_restore_title", fail)
    with pytest.raises(PatchError, match="I/O error"):
        restore(game, _patch())
    monkeypatch.undo()
    assert (game / "Game.exe").read_bytes() == TRANSLATED


def test_a_game_file_reached_through_a_link_is_neither_read_nor_patched(
    tmp_path: Path, symlink, monkeypatch
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Game.exe").write_bytes(ORIGINAL)
    symlink(game / "bin", outside)
    patch = replace(_patch(), files=(replace(_patch().files[0], name="bin/Game.exe"),))
    real_sha256_of = core.sha256_of

    def sha256_of(path: Path) -> str:
        assert outside not in Path(path).resolve().parents, f"{path} was read"
        return real_sha256_of(path)

    monkeypatch.setattr(core, "sha256_of", sha256_of)

    assert core.inspect(game, patch) == core.FolderState(core.State.LINKED, False, "bin")
    for act in (apply, restore):
        with pytest.raises(PatchError, match="連結"):
            act(game, patch)

    assert sorted(path.name for path in outside.iterdir()) == ["Game.exe"]
    assert (outside / "Game.exe").read_bytes() == ORIGINAL


def test_a_backup_that_is_a_link_is_not_put_in_place(tmp_path: Path, symlink) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(TRANSLATED)
    (tmp_path / "elsewhere").write_bytes(ORIGINAL)
    symlink(game / "Game.exe.bak", tmp_path / "elsewhere")

    with pytest.raises(PatchError):
        restore(game, _patch())

    assert not (game / "Game.exe").is_symlink()
    assert (game / "Game.exe").read_bytes() == TRANSLATED


def test_restore_leaves_a_recorded_file_reached_through_a_link(tmp_path: Path, symlink) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"victim")
    symlink(game / "sub", outside)
    listed = [{"name": "sub/victim.txt", "sha256": hashlib.sha256(b"victim").hexdigest()}]
    (game / RECORD).write_text(json.dumps({"added": listed}), encoding="utf-8")
    patch = replace(_patch(), added=(PatchAdded("sub/victim.txt", b"victim"),))

    assert restore(game, patch) == ["sub/victim.txt"]

    assert (outside / "victim.txt").read_bytes() == b"victim"
    assert not (game / RECORD).exists()


def test_restore_names_a_recorded_file_now_a_link_pointing_nowhere(
    tmp_path: Path, symlink
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    apply(game, _font_patch())
    (game / "font.ttf").unlink()
    symlink(game / "font.ttf", tmp_path / "gone.ttf")

    assert restore(game, _font_patch()) == ["font.ttf"]

    assert (game / "font.ttf").is_symlink()
    assert (game / "Game.exe").read_bytes() == ORIGINAL


# A link pointing nowhere reads as missing, yet a write through it lands where it points.
@pytest.mark.parametrize("name", ["Game.exe.bokucho-tmp", "font.ttf", RECORD])
def test_a_link_the_patch_would_write_through_is_refused_with_nothing_written(
    tmp_path: Path, symlink, name: str
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside" / "file"
    outside.parent.mkdir()
    symlink(game / name, outside)
    patch = replace(_patch(), added=(PatchAdded("font.ttf", b"font"),))

    with pytest.raises(PatchError, match=re.escape(name)):
        apply(game, patch)

    assert not outside.exists()
    assert sorted(path.name for path in game.iterdir()) == sorted({"Game.exe", name})
    assert (game / "Game.exe").read_bytes() == ORIGINAL


def test_a_folder_on_the_way_that_is_a_link_is_refused(tmp_path: Path, symlink) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink(game / "Fonts", outside)
    patch = replace(_patch(), added=(PatchAdded("Fonts/font.ttf", b"font"),))

    with pytest.raises(PatchError, match="Fonts"):
        apply(game, patch)

    assert not list(outside.iterdir())


def test_a_game_folder_reached_through_a_link_still_takes_the_patch(
    tmp_path: Path, symlink
) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(ORIGINAL)
    symlink(tmp_path / "library", game)

    apply(tmp_path / "library", _patch())

    assert (game / "Game.exe").read_bytes() == TRANSLATED
