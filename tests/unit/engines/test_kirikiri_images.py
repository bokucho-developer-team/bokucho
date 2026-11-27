"""KiriKiri pictures: which ones are unpacked from the game's archives, and how."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bokucho.application.contracts import CancellationFlag, ProgressLevel
from bokucho.application.errors import JobCancelled
from bokucho.engines import xp3
from bokucho.engines.base import unpacked_stamp
from bokucho.engines.kirikiri import KiriKiriAdapter, UnsupportedCategory
from bokucho.patcher import core

PNG = b"\x89PNG\r\n\x1a\n" + bytes(8)  # equals the fixture's default image/bg.png
JPG = b"\xff\xd8\xff" + bytes(8)


def _unpack(game, dest, **settings):
    events = []
    KiriKiriAdapter.unpack_images(game, dest, SimpleNamespace(publish=events.append), **settings)
    return events


def _tree(dest):
    return {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()}


def test_only_pictures_of_the_chosen_folders_are_unpacked(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={
            "image/ui/title.png": PNG + b"t",
            "Rule/op.jpg": JPG,
            "rule/mask.gif": b"GIF89a",
            "rule/x.tlg": b"TLG5.0",
            "cg/ev.png": PNG,
            "image2/a.png": PNG,
            "imagefx.png": PNG,
            "system/note.txt": b"x",
        },
    )
    _unpack(game, tmp_path / "out")
    assert _tree(tmp_path / "out") == {
        "image/bg.png": PNG,
        "image/ui/title.png": PNG + b"t",
        "Rule/op.jpg": JPG,
    }


def test_skip_globs_leave_other_language_pictures_out(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={
            "image/start.png": PNG,
            "image/start_chh.png": PNG,
            "image/START_CHK.PNG": PNG,
            "rule/OP_han_g.jpg": JPG,
        },
    )
    _unpack(game, tmp_path / "out", skip_images=["*_chh.*", "*_chk.*", "*_han_*"])
    assert sorted(_tree(tmp_path / "out")) == ["image/bg.png", "image/start.png"]


def test_a_later_archive_replaces_a_picture_even_at_its_root(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={"image/a.png": PNG + b"1"},
        extra={"patch.xp3": {"a.png": PNG + b"2"}},
    )
    _unpack(game, tmp_path / "out")
    assert _tree(tmp_path / "out") == {"image/bg.png": PNG, "a.png": PNG + b"2"}


def test_an_archive_bokucho_added_is_not_unpacked(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={"image/a.png": PNG + b"1"},
        extra={"patch.xp3": {"a.png": PNG + b"2"}},
    )
    (game / core.RECORD).write_text(
        json.dumps({"added": [{"name": "patch.xp3", "sha256": "0" * 64}]}), encoding="utf-8"
    )
    _unpack(game, tmp_path / "out")
    assert _tree(tmp_path / "out")["image/a.png"] == PNG + b"1"


def test_one_name_in_two_folders_of_an_archive_is_left_out_with_a_warning(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, files={"image/a.png": PNG, "bgimage/A.png": PNG}
    )
    events = _unpack(game, tmp_path / "out")
    assert "image/a.png" not in _tree(tmp_path / "out")
    (warning,) = [e for e in events if e.level is ProgressLevel.WARNING]
    assert warning.message_key == "engine.unpack.entry.skipped"
    assert "image/a.png" in warning.detail and "bgimage/A.png" in warning.detail


def test_a_later_archive_with_one_copy_settles_an_earlier_clash(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={"image/a.png": PNG, "bgimage/A.png": PNG},
        extra={"patch.xp3": {"a.png": PNG + b"2"}},
    )
    events = _unpack(game, tmp_path / "out")
    assert _tree(tmp_path / "out")["a.png"] == PNG + b"2"
    assert not [e for e in events if e.level is ProgressLevel.WARNING]


def test_one_name_in_two_archives_loaded_in_an_unknown_order_is_left_out(
    tmp_path, kirikiri_game, tjs2_script
):
    # A compiled startup mounts the archives beside data.xp3 in an order Bokucho cannot read.
    extra = {
        "alpha.xp3": {"image/a.png": PNG + b"1", "image/bg.png": PNG + b"a"},
        "beta.xp3": {"image/a.png": PNG + b"2"},
    }
    loop = ["addAutoPath", "isExistentStorage", "patch.xp3", "patch", ".xp3"]
    startup = {"system/Initialize.tjs": tjs2_script(loop)}
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files=startup, extra=extra)
    events = _unpack(game, tmp_path / "out")
    # data.xp3 holds the startup, so it loads first, and alpha.xp3's copy of bg.png wins.
    assert _tree(tmp_path / "out") == {"image/bg.png": PNG + b"a"}
    (warning,) = [e for e in events if e.level is ProgressLevel.WARNING]
    assert "alpha.xp3" in warning.detail and "beta.xp3" in warning.detail
    # A patch archive loads after them all, so its copy settles it.
    xp3.write(game / "patch.xp3", {"a.png": PNG + b"3"})
    events = _unpack(game, tmp_path / "again")
    assert _tree(tmp_path / "again")["a.png"] == PNG + b"3"
    assert not [e for e in events if e.level is ProgressLevel.WARNING]


def test_the_pictures_folder_is_named_without_reading_the_layout(
    tmp_path, kirikiri_game, tjs2_script
):
    # One scenario in two archives of unknown order refuses the layout, not this name.
    startup = {"system/Initialize.tjs": tjs2_script(["addAutoPath", "patch.xp3"])}
    extra = {"alpha.xp3": {"x.ks": b"\xff\xfeA"}, "beta.xp3": {"x.ks": b"\xff\xfeB"}}
    game = kirikiri_game(tmp_path / "g", {}, files=startup, extra=extra)
    assert KiriKiriAdapter.packed_archive(game) == game / "data.xp3"


def test_a_picture_with_the_wrong_magic_is_category_3(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files={"image/a.png": b"nope"})
    with pytest.raises(UnsupportedCategory) as caught:
        _unpack(game, tmp_path / "out")
    assert caught.value.category == 3


def test_unpacking_again_writes_nothing_until_a_setting_changes(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files={"image/a_chh.png": PNG})
    dest = tmp_path / "out"
    _unpack(game, dest)
    stamp = unpacked_stamp(dest)
    first = (stamp.stat().st_mtime_ns, (dest / "image/a_chh.png").stat().st_mtime_ns)
    _unpack(game, dest)
    assert (stamp.stat().st_mtime_ns, (dest / "image/a_chh.png").stat().st_mtime_ns) == first
    _unpack(game, dest, skip_images=["*_chh.*"])
    assert not (dest / "image/a_chh.png").exists()


def test_a_cancelled_unpack_keeps_the_earlier_copy(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files={"image/a_chh.png": PNG})
    dest = tmp_path / "out"
    _unpack(game, dest)
    flag = CancellationFlag()
    flag.cancel()
    with pytest.raises(JobCancelled):
        KiriKiriAdapter.unpack_images(game, dest, token=flag, skip_images=["*_chh.*"])
    assert (dest / "image/a_chh.png").is_file()
    _unpack(game, dest, skip_images=["*_chh.*"])
    assert not (dest / "image/a_chh.png").exists()
