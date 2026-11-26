"""KiriKiri games: which archives the game loads, and their .ks scenario text."""

from __future__ import annotations

import json
import re
import struct

import pytest

from bokucho.application.contracts import CancellationFlag, ProgressLevel
from bokucho.application.errors import InvalidProject, JobCancelled
from bokucho.engines import psb, xp3
from bokucho.engines.kag import encode_script
from bokucho.engines.kirikiri import (
    KiriKiriAdapter,
    SlotRefused,
    UnsupportedCategory,
    game_folder,
    game_layout,
    patch_slot,
    scenario_archives,
)
from bokucho.infrastructure.factories import make_engine_adapter
from bokucho.patcher import core


def test_extract_reads_lines_and_names(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"scenario/first.ks": "[name fn=春香]\nおはよう\n"})
    units = KiriKiriAdapter(name_attributes=["name.fn"]).extract(game)
    assert [(u.id, u.kind, u.engine_ref["file"]) for u in units] == [
        ("first.ks::0", "dialogue", "first.ks"),
        ("name:春香", "name", "speakers"),
    ]
    assert units[0].context.speaker == "春香"


def test_a_name_used_twice_is_one_unit(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"b.ks": "[name fn=春香]\nい\n", "a.ks": "[name fn=春香]\nあ\n[name fn=秋子]\nう\n"},
    )
    units = KiriKiriAdapter(name_attributes=["name.fn"]).extract(game)
    assert [u.id for u in units] == ["a.ks::0", "a.ks::1", "b.ks::0", "name:春香", "name:秋子"]


def test_the_last_loaded_copy_is_read_and_unnamed_archives_are_ignored(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"scenario/first.ks": "古い\n"},
        extra={
            "sys/scenario.xp3": {"first.ks": encode_script("中\n")},
            "patch.xp3": {"first.ks": encode_script("新しい\n")},
            "other.xp3": {"first.ks": encode_script("無視\n")},
        },
    )
    assert game_layout(game).archives == ("data.xp3", "sys/scenario.xp3", "patch.xp3")
    assert [u.source for u in KiriKiriAdapter().extract(game)] == ["新しい"]


def test_the_patch_loop_archives_are_loaded_in_order(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "古い\n"},
        extra={
            "patch2.xp3": {"a.ks": encode_script("二\n")},
            "patch3.xp3": {"a.ks": encode_script("三\n")},
            "patch5.xp3": {"a.ks": encode_script("五\n")},
        },
    )
    assert game_layout(game).archives == ("data.xp3", "patch2.xp3", "patch3.xp3")
    assert [u.source for u in KiriKiriAdapter().extract(game)] == ["三"]


@pytest.mark.parametrize(
    "present,slot",
    [
        ((), "patch.xp3"),
        (("patch.xp3",), "patch2.xp3"),
        (("Patch.xp3", "PATCH2.XP3"), "patch3.xp3"),
        (("patch2.xp3",), "patch3.xp3"),
    ],
)
def test_the_patch_takes_the_slot_after_the_last_loaded_archive(tmp_path, present, slot):
    for name in present:
        (tmp_path / name).write_bytes(b"")
    assert patch_slot(tmp_path, "loop") == slot


def test_a_patch_archive_after_a_gap_refuses(tmp_path):
    (tmp_path / "patch.xp3").write_bytes(b"")
    (tmp_path / "patch3.xp3").write_bytes(b"")
    with pytest.raises(SlotRefused, match="patch3.xp3"):
        patch_slot(tmp_path, "loop")


def test_a_single_patch_game_refuses_when_one_is_there(tmp_path):
    (tmp_path / "patch.xp3").write_bytes(b"")
    with pytest.raises(SlotRefused, match="patch.xp3"):
        patch_slot(tmp_path, "single")


@pytest.mark.parametrize("slots", ["loop", "single", "none"])
def test_the_startup_script_gives_the_slot_form(tmp_path, kirikiri_game, slots):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, slots=slots)
    assert game_layout(game).slots == {"none": ""}.get(slots, slots)


def test_single_quotes_in_the_startup_script_are_read(tmp_path):
    xp3.write(tmp_path / "data.xp3", {"startup.tjs": b"useArchiveIfExists('patch.xp3');\n"})
    assert game_layout(tmp_path).slots == "single"


def test_inject_writes_only_changed_scripts_as_utf16(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n", "b.ks": "い\n"})
    units = KiriKiriAdapter().extract(game)
    units[0].target = "甲"
    units[1].target = "  "
    KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["a.ks"]
    assert (tmp_path / "out" / "a.ks").read_bytes() == encode_script("甲\n")


def test_a_name_translation_reaches_every_script(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "[name fn=春香]\nあ\n", "b.ks": "[name fn=春香]\nい\n"}
    )
    adapter = KiriKiriAdapter(name_attributes=["name.fn"])
    units = adapter.extract(game)
    units[-1].target = "春香譯"
    adapter.inject(units, game, tmp_path / "out")
    for name, line in (("a.ks", "あ"), ("b.ks", "い")):
        written = (tmp_path / "out" / name).read_bytes()
        assert written == encode_script(f'[name fn="春香譯"]\n{line}\n')


def test_a_cp932_script_comes_back_as_utf16(tmp_path, kirikiri_startup):
    xp3.write(
        tmp_path / "data.xp3",
        {"system/Initialize.tjs": kirikiri_startup(), "a.ks": "あ\n".encode("cp932")},
    )
    units = KiriKiriAdapter().extract(tmp_path)
    units[0].target = "甲"
    KiriKiriAdapter().inject(units, tmp_path, tmp_path / "out")
    assert (tmp_path / "out" / "a.ks").read_bytes() == encode_script("甲\n")


def test_one_basename_in_two_folders_is_refused(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"x/a.ks": "あ\n", "y/a.ks": "い\n"})
    with pytest.raises(InvalidProject, match="x/a.ks.*y/a.ks"):
        KiriKiriAdapter().extract(game)


# Never inject these before the guard exists: the name would be written outside out/.
@pytest.mark.parametrize("name", ["x/D:evil.ks", "x/con.ks"])
def test_a_script_name_that_is_not_a_plain_file_name_is_refused(tmp_path, kirikiri_game, name):
    game = kirikiri_game(tmp_path / "g", {name: "あ\n"})
    with pytest.raises(InvalidProject, match=re.escape(name)):
        KiriKiriAdapter().extract(game)


@pytest.mark.parametrize(
    "category,files",
    [
        (3, {"a.ks": b"\xfe\xfe\x01x"}),
        (3, {"a.ks": b"\x00\x14\x15x"}),
        (3, {"a.ks": encode_script("あ\n"), "image/bg.png": b"\x13\x37 not a picture"}),
    ],
)
def test_other_categories_are_named(tmp_path, kirikiri_startup, category, files):
    xp3.write(tmp_path / "data.xp3", {"system/Initialize.tjs": kirikiri_startup(), **files})
    with pytest.raises(UnsupportedCategory, match=f"category {category}") as caught:
        KiriKiriAdapter().extract(tmp_path)
    assert caught.value.category == category


# A link would take a read outside the game folder, whether it is the archive or a folder on
# the way to it; the game folder itself may be one.
@pytest.mark.parametrize("linked", ["data.xp3", "sys", "sys/scenario.xp3"])
def test_an_archive_reached_through_a_link_is_refused(tmp_path, kirikiri_game, symlink, linked):
    game = kirikiri_game(
        tmp_path / "game", {"a.ks": "あ\n"},
        extra={"sys/scenario.xp3": {"b.ks": encode_script("い\n")}},
    )
    outside = tmp_path / "outside" / linked.replace("/", "_")
    outside.parent.mkdir()
    (game / linked).rename(outside)
    symlink(game / linked, outside)
    with pytest.raises(InvalidProject, match="is a link"):
        game_layout(game)


def test_a_game_folder_that_is_itself_a_link_is_read_as_usual(tmp_path, kirikiri_game, symlink):
    real = kirikiri_game(tmp_path / "real", {"a.ks": "あ\n"})
    symlink(tmp_path / "game", real)
    assert game_layout(tmp_path / "game").archives == ("data.xp3",)


LOOP = [
    "useArchiveIfExists", "addAutoPath", "isExistentStorage", "patch.xp3", "patch", ".xp3", ".xp3>",
]


def _compiled(tjs2_script, constants=LOOP):
    return {"system/Initialize.tjs": tjs2_script(constants)}


def test_a_cancelled_extract_stops_before_reading_the_scripts(tmp_path, kirikiri_startup):
    # Reading the scrambled script would refuse the game as category 3.
    xp3.write(
        tmp_path / "data.xp3",
        {"system/Initialize.tjs": kirikiri_startup(), "a.ks": b"\xfe\xfe\x01x"},
    )
    flag = CancellationFlag()
    flag.cancel()
    with pytest.raises(JobCancelled):
        KiriKiriAdapter().extract(tmp_path, token=flag)


@pytest.mark.parametrize("constants,slots", [
    (LOOP, "loop"), (["addAutoPath", "patch.xp3"], "single"), (["System.exePath"], ""),
])
def test_a_compiled_startup_gives_the_slot_form(
    tmp_path, kirikiri_game, tjs2_script, constants, slots
):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script, constants))
    assert game_layout(game).slots == slots


# A constant can sit in a message too: only a script that mounts archives has a patch slot,
# and only one that also asks whether a file exists loops over the numbered ones.
@pytest.mark.parametrize("constants,slots", [
    (["useArchiveIfExists", "patch.xp3", "patch", ".xp3", "isExistentStorage"], ""),
    (["addAutoPath", "patch.xp3", "patch", ".xp3"], "single"),
])
def test_a_compiled_startup_shows_how_it_loads_patches_by_what_it_calls(
    tmp_path, kirikiri_game, tjs2_script, constants, slots
):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script, constants))
    assert game_layout(game).slots == slots


def test_a_compiled_startup_reads_every_other_archive_then_the_patches(
    tmp_path, kirikiri_game, tjs2_script
):
    names = ("Zeta.xp3", "alpha.xp3", "patch.xp3", "patch2.xp3", "patch4.xp3", "sub/inner.xp3")
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script),
        extra={name: {} for name in names},
    )
    assert game_layout(game).archives == (
        "data.xp3", "alpha.xp3", "Zeta.xp3", "patch.xp3", "patch2.xp3"
    )
    _record(game, "patch2.xp3")
    assert game_layout(game).archives == ("data.xp3", "alpha.xp3", "Zeta.xp3", "patch.xp3")


def test_a_compiled_startup_naming_no_archive_still_reads_every_archive_beside_it(
    tmp_path, kirikiri_game, tjs2_script
):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script, ["System.exePath"]),
        extra={"scenario.xp3": {"b.ks": encode_script("い\n")}},
    )
    layout = game_layout(game)
    assert (layout.archives, layout.slots) == (("data.xp3", "scenario.xp3"), "")


def test_without_a_patch_slot_the_patch_archives_beside_it_are_read_like_any_other(
    tmp_path, kirikiri_game, tjs2_script
):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script, ["System.exePath"]),
        extra={"patch.xp3": {"b.ks": encode_script("い\n")}, "patch2.xp3": {}},
    )
    layout = game_layout(game)
    assert (layout.archives, layout.slots) == (("data.xp3", "patch.xp3", "patch2.xp3"), "")
    _record(game, "patch2.xp3")
    assert game_layout(game).archives == ("data.xp3", "patch.xp3")


# A patch archive in a subfolder is no patch slot: only the game folder's are.
@pytest.mark.parametrize("name", ["sub/scenario.xp3", "sub/patch.xp3"])
def test_a_compiled_startup_also_reads_an_archive_it_names_in_a_subfolder(
    tmp_path, kirikiri_game, tjs2_script, name
):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"},
        files=_compiled(tjs2_script, [name, *LOOP]),
        extra={name: {"b.ks": encode_script("い\n")}},
    )
    assert game_layout(game).archives == ("data.xp3", name)


def test_a_scrambled_startup_script_is_skipped_for_the_layout(tmp_path):
    xp3.write(tmp_path / "data.xp3", {
        "system/Initialize.tjs": b"\xfe\xfe\x01x",
        "startup.tjs": b"useArchiveIfExists('patch.xp3');\n",
        "a.ks": encode_script("あ\n"),
    })
    assert game_layout(tmp_path).slots == "single"


def test_a_scrambled_startup_script_alone_gives_no_patch_slot(tmp_path):
    xp3.write(
        tmp_path / "data.xp3",
        {"system/Initialize.tjs": b"\xfe\xfe\x01x", "a.ks": encode_script("あ\n")},
    )
    layout = game_layout(tmp_path)
    assert (layout.archives, layout.slots) == (("data.xp3",), "")
    assert [u.source for u in KiriKiriAdapter().extract(tmp_path)] == ["あ"]


@pytest.mark.parametrize("constants,other", [(LOOP, "beta.xp3"), (["System.exePath"], "patch.xp3")])
def test_one_scenario_in_two_unordered_archives_is_refused(
    tmp_path, kirikiri_game, tjs2_script, constants, other
):
    game = kirikiri_game(tmp_path / "g", {}, files=_compiled(tjs2_script, constants), extra={
        "alpha.xp3": {"scenario/x.ks": encode_script("あ\n")},
        other: {"X.KS": encode_script("い\n")},
    })
    with pytest.raises(InvalidProject, match=f"alpha.xp3.*{other}"):
        game_layout(game)


def test_an_unreadable_archive_beside_a_compiled_startup_is_named(
    tmp_path, kirikiri_game, tjs2_script
):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files=_compiled(tjs2_script))
    (game / "broken.xp3").write_bytes(b"not an archive")
    with pytest.raises(InvalidProject, match="broken.xp3"):
        game_layout(game)


def test_a_publisher_index_is_category_4(tmp_path, xp3_bytes):
    game = tmp_path / "g"
    game.mkdir()
    (game / "data.xp3").write_bytes(
        xp3_bytes([("a.ks", [(True, encode_script("あ\n"))])], extra=[(b"Hxv4", b"\0" * 8)])
    )
    with pytest.raises(UnsupportedCategory) as caught:
        KiriKiriAdapter().extract(game)
    assert caught.value.category == 4


def test_a_game_without_scenarios_is_refused(tmp_path, kirikiri_startup):
    xp3.write(tmp_path / "data.xp3", {"system/Initialize.tjs": kirikiri_startup()})
    with pytest.raises(InvalidProject, match=r"\.ks"):
        KiriKiriAdapter().extract(tmp_path)


def test_a_folder_without_data_xp3_is_refused(tmp_path):
    with pytest.raises(InvalidProject, match="data.xp3"):
        game_layout(tmp_path)


def test_a_damaged_archive_is_named(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"})
    (game / "patch.xp3").write_bytes(b"not an archive")
    with pytest.raises(InvalidProject, match="patch.xp3"):
        KiriKiriAdapter().extract(game)


def test_archive_names_match_in_any_case(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"Patch.xp3": {"b.ks": encode_script("い\n")}}
    )
    (game / "data.xp3").rename(game / "DATA.XP3")
    assert game_layout(game).archives == ("DATA.XP3", "Patch.xp3")


def test_the_given_archives_replace_the_layout(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    assert [u.source for u in KiriKiriAdapter(archives=["data.xp3"]).extract(game)] == ["あ"]
    with pytest.raises(InvalidProject, match="gone.xp3"):
        KiriKiriAdapter(archives=["data.xp3", "gone.xp3"]).extract(game)


def test_an_executable_path_means_its_folder(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"})
    (game / "game.exe").write_bytes(b"MZ")
    assert game_folder(game / "game.exe") == game
    assert game_folder(game) == game
    assert KiriKiriAdapter.game_root(game) is None


def test_the_config_reaches_the_adapter(tmp_path, new_project, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "[en]A[endif]\n[name fn=春香]B\n*x_en\nC\n"})
    project = new_project(engine="kirikiri")
    project.config.engine.kag_skip_branches = ["en"]
    project.config.engine.kag_name_attributes = ["name.fn"]
    project.config.engine.kag_skip_labels = ["*_en"]
    units = make_engine_adapter(project).extract(game)
    assert [(u.id, u.source) for u in units] == [("a.ks::0", "B"), ("name:春香", "春香")]


def _record(game, *names):
    (game / core.RECORD).write_text(
        json.dumps({"added": [{"name": n, "sha256": "0" * 64} for n in names]}),
        encoding="utf-8",
    )


def test_an_archive_bokucho_added_is_not_part_of_the_game(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"scenario/first.ks": "おはよう\n"},
        extra={"patch.xp3": {"first.ks": encode_script("早安\n")}},
    )
    _record(game, "PATCH.xp3")
    assert game_layout(game).archives == ("data.xp3",)
    assert [u.source for u in KiriKiriAdapter().extract(game)] == ["おはよう"]


def test_a_publisher_archive_still_counts_beside_a_bokucho_record(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    _record(game, "patch2.xp3")
    assert game_layout(game).archives == ("data.xp3", "patch.xp3")


def test_a_damaged_record_is_refused_rather_than_read_past(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    (game / core.RECORD).write_text('{"added": [{"name": "patch.xp3", "sha', encoding="utf-8")
    with pytest.raises(InvalidProject, match=core.RECORD):
        game_layout(game)
    with pytest.raises(InvalidProject, match=core.RECORD):
        KiriKiriAdapter().extract(game)


@pytest.mark.parametrize(
    "record",
    ['{"added": null}', '{"added": [{"name": "patch.xp3"}]}', '{"added": ["patch.xp3"]}', "[]"],
)
def test_a_record_in_a_shape_bokucho_never_writes_is_refused(tmp_path, kirikiri_game, record):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    (game / core.RECORD).write_text(record, encoding="utf-8")
    with pytest.raises(InvalidProject, match=core.RECORD):
        game_layout(game)


def test_a_record_listing_nothing_still_reads(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    (game / core.RECORD).write_text('{"added": []}', encoding="utf-8")
    assert game_layout(game).archives == ("data.xp3", "patch.xp3")


class _Events(list):
    def publish(self, event):
        self.append(event)


def _scn_game(tmp_path, kirikiri_game, files, scripts=None, **options):
    return kirikiri_game(tmp_path / "g", scripts or {}, files=files, **options)


def test_extract_reads_a_scn(tmp_path, kirikiri_game, psb_file, scn_tree):
    tree = scn_tree(
        ("春香", ("おはよう", "Hi", "早")), title="始まり",
        selects=[{"language": [None, {"text": "Go"}], "storage": "", "target": "", "text": "行く"}],
    )
    game = _scn_game(tmp_path, kirikiri_game, {"scenario/a.txt.scn": psb_file(tree)})
    units = KiriKiriAdapter().extract(game)
    assert [(u.id, u.kind, u.engine_ref["file"]) for u in units] == [
        ("a.txt.scn:s:0", "dialogue", "a.txt.scn"),
        ("a.txt.scn:s:choice0", "choice", "a.txt.scn"),
        ("name:春香", "name", "speakers"),
        ("title:始まり", "title", "scene-titles"),
    ]
    assert units[0].context.speaker == "春香"


def test_inject_writes_every_column_of_only_the_changed_scn(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    game = _scn_game(tmp_path, kirikiri_game, {
        "a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊")))),
        "b.txt.scn": psb_file(scn_tree((None, ("い", "I", "咿")))),
    })
    units = KiriKiriAdapter().extract(game)
    units[0].target = "早安"
    KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["a.txt.scn"]
    root = psb.decode((tmp_path / "out" / "a.txt.scn").read_bytes()).root
    assert [c[1] for c in root["scenes"][0]["texts"][0][1]] == ["早安"] * 3


def test_scrambled_ks_beside_a_scn_are_skipped_with_one_warning(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    game = _scn_game(tmp_path, kirikiri_game, {
        "a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊")))),
        "x.ks": b"\xfe\xfe\x01x",
        "y.ks": b"\xfe\xfe\x01y",
    })
    events = _Events()
    units = KiriKiriAdapter().extract(game, events)
    assert {u.engine_ref["file"] for u in units} == {"a.txt.scn"}
    (warning,) = [e for e in events if e.level is ProgressLevel.WARNING]
    assert (warning.message_key, warning.detail) == ("engine.extract.scrambled.skipped", "2 .ks")


def test_every_copy_of_a_script_left_out_beside_a_listed_one_is_listed_too(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    # Without patch.xp3 and patch2.xp3 the patcher would take the readable a.ks and b.ks
    # for the copies the game reads, and could write them over the scrambled ones.
    game = _scn_game(
        tmp_path, kirikiri_game,
        {"a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊"))))}, {"a.ks": "古い\n"},
        extra={
            "patch.xp3": {"a.ks": b"\xfe\xfe\x01x", "b.ks": encode_script("古い\n")},
            "patch2.xp3": {"b.ks": b"\xfe\xfe\x01y"},
            "patch3.xp3": {"x.ks": b"\xfe\xfe\x01z", "v.ogg": b"OggS"},
        },
    )
    archives = game_layout(game).archives
    assert scenario_archives(game, archives) == ["data.xp3", "patch.xp3", "patch2.xp3"]


def test_a_cancelled_scan_for_the_release_stops_before_reading_the_scripts(
    tmp_path, kirikiri_game
):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"})
    flag = CancellationFlag()
    flag.cancel()
    with pytest.raises(JobCancelled):
        scenario_archives(game, game_layout(game).archives, flag)


def test_a_cancel_stops_the_release_scan_of_scn_alone_before_the_next_index(
    tmp_path, kirikiri_game, psb_file, scn_tree, monkeypatch
):
    raw = psb_file(scn_tree((None, ("あ", "A", "啊"))))
    extra = {"patch.xp3": {"b.txt.scn": raw}}
    game = _scn_game(tmp_path, kirikiri_game, {"a.txt.scn": raw}, extra=extra)
    archives = game_layout(game).archives
    assert len(archives) == 2
    flag = CancellationFlag()
    read = []
    entries = xp3.entries

    def entries_then_cancel(path):
        read.append(path.name)
        flag.cancel()
        return entries(path)

    monkeypatch.setattr(xp3, "entries", entries_then_cancel)
    with pytest.raises(JobCancelled):
        scenario_archives(game, archives, flag)
    assert read == ["data.xp3"]


def test_an_encrypted_scn_is_category_3(tmp_path, kirikiri_game, psb_file, scn_tree):
    raw = psb_file(scn_tree((None, ("あ", "A", "啊"))))
    game = _scn_game(tmp_path, kirikiri_game, {"a.txt.scn": raw[:6] + b"\x01\x00" + raw[8:]})
    with pytest.raises(UnsupportedCategory) as caught:
        KiriKiriAdapter().extract(game)
    assert caught.value.category == 3


def test_a_damaged_scn_is_refused_naming_it(tmp_path, kirikiri_game):
    game = _scn_game(tmp_path, kirikiri_game, {"scenario/a.scn": b"PSB\0"})
    with pytest.raises(InvalidProject, match="a.scn") as caught:
        KiriKiriAdapter().extract(game)
    assert not isinstance(caught.value, UnsupportedCategory)


def _strings_reordered(raw):
    """``raw`` with its string offsets reversed: a valid PSB in an order no encoder writes."""
    at = struct.unpack_from("<I", raw, 0x10)[0]
    count, width = raw[at + 1], raw[at + 2] - 0x0C
    assert raw[at] == 0x0D and count > 1
    items = [raw[at + 3 + i * width : at + 3 + (i + 1) * width] for i in range(count)]
    return raw[: at + 3] + b"".join(reversed(items)) + raw[at + 3 + count * width :]


def test_a_scn_bokucho_cannot_write_back_exactly_is_refused(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    raw = _strings_reordered(psb_file(scn_tree((None, ("あ", "A", "啊")))))
    with pytest.raises(InvalidProject, match="byte for byte"):
        KiriKiriAdapter().extract(_scn_game(tmp_path, kirikiri_game, {"a.txt.scn": raw}))


def test_a_scn_the_encoder_cannot_write_is_refused_naming_it(
    tmp_path, kirikiri_game, psb_file, scn_tree, monkeypatch
):
    game = _scn_game(tmp_path, kirikiri_game, {
        "scenario/a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊")))),
    })
    units = KiriKiriAdapter().extract(game)
    units[0].target = "早安"

    real_encode = psb.encode

    def refuse(decoded):
        # Reading the scenario writes it back once to check it; the translation fails.
        if "早安" not in repr(decoded.root):
            return real_encode(decoded)
        raise psb.PsbError("its values expand far past what a PSB file of its size may hold")

    monkeypatch.setattr(psb, "encode", refuse)
    with pytest.raises(InvalidProject, match=r"scenario/a\.txt\.scn: its values expand"):
        KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert not (tmp_path / "out" / "a.txt.scn").exists()


def test_inject_leaves_a_scn_with_nothing_to_change_unread(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    clean = {"a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊"))))}
    units = KiriKiriAdapter().extract(_scn_game(tmp_path / "x", kirikiri_game, clean))
    units[0].target = "早安"
    unwritable = _strings_reordered(psb_file(scn_tree((None, ("い", "I", "咿")))))
    game = _scn_game(tmp_path, kirikiri_game, {**clean, "b.txt.scn": unwritable})
    KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["a.txt.scn"]


def test_a_title_alone_reaches_the_scn_that_shows_it(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    game = _scn_game(tmp_path, kirikiri_game, {
        "a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊")))),
        "b.txt.scn": psb_file(scn_tree((None, ("い", "I", "咿")), title="始まり")),
    })
    units = KiriKiriAdapter().extract(game)
    (title,) = [u for u in units if u.kind == "title"]
    title.target = "開始"
    KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["b.txt.scn"]
    assert psb.decode((tmp_path / "out" / "b.txt.scn").read_bytes()).root["scenes"][0]["title"] \
        == "開始"


def test_a_game_with_ks_and_scn_gives_both(tmp_path, kirikiri_game, psb_file, scn_tree):
    game = _scn_game(
        tmp_path, kirikiri_game, {"b.txt.scn": psb_file(scn_tree((None, ("い", "I", "咿"))))},
        scripts={"a.ks": "あ\n"},
    )
    assert [u.id for u in KiriKiriAdapter().extract(game)] == ["a.ks::0", "b.txt.scn:s:0"]


def test_a_name_in_a_ks_and_a_scn_is_one_unit_written_to_both(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    game = _scn_game(
        tmp_path, kirikiri_game, {"b.txt.scn": psb_file(scn_tree(("春香", ("い", "I", "咿"))))},
        scripts={"a.ks": "[name fn=春香]\nあ\n"},
    )
    adapter = KiriKiriAdapter(name_attributes=["name.fn"])
    units = adapter.extract(game)
    (name,) = [u for u in units if u.kind == "name"]
    name.target = "春香譯"
    adapter.inject(units, game, tmp_path / "out")
    assert (tmp_path / "out" / "a.ks").read_bytes() == encode_script('[name fn="春香譯"]\nあ\n')
    root = psb.decode((tmp_path / "out" / "b.txt.scn").read_bytes()).root
    assert [c[0] for c in root["scenes"][0]["texts"][0][1]] == ["春香譯"] * 3


def test_a_translation_with_markup_the_line_lacks_is_refused(
    tmp_path, kirikiri_game, psb_file, scn_tree
):
    game = _scn_game(
        tmp_path, kirikiri_game, {"a.txt.scn": psb_file(scn_tree((None, ("あ", "A", "啊"))))}
    )
    units = KiriKiriAdapter().extract(game)
    units[0].target = r"早\x"
    with pytest.raises(InvalidProject, match=re.escape("a.txt.scn:s:0")):
        KiriKiriAdapter().inject(units, game, tmp_path / "out")


def test_a_record_that_is_a_link_is_refused_unread_for_what_was_compiled(tmp_path, symlink):
    from bokucho.engines import base

    game = tmp_path / "game"
    game.mkdir()
    (game / "data.xp3").write_bytes(b"XP3")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"compiled": {"data.xp3": "0" * 64}}), encoding="utf-8")
    assert base._compiled(game / "data.xp3") is None
    (game / core.RECORD).write_bytes(elsewhere.read_bytes())
    assert base._compiled(game / "data.xp3") == "0" * 64
    for kept in ({"added": []}, {"added": [], "compiled": {}}):  # a record naming no archive
        (game / core.RECORD).write_text(json.dumps(kept), encoding="utf-8")
        assert base._compiled(game / "data.xp3") is None
    # Taken for no record, the patched archive would be unpacked as the game's own.
    odd = [{"compiled": None}, {"compiled": []}, {"compiled": {"data.xp3": None}}]
    odd.append({"compiled": {"data.xp3": 5}})
    nested = b"[" * 100_000 + b"]" * 100_000  # past what the parser follows
    for damaged in (b"{", b"[]", nested, *(json.dumps(record).encode() for record in odd)):
        (game / core.RECORD).write_bytes(damaged)
        with pytest.raises(InvalidProject, match=re.escape(core.RECORD)):
            base.holds_original(tmp_path / "cache", game / "data.xp3", "1" * 64)
    (game / core.RECORD).unlink()
    for target in (elsewhere, tmp_path / "gone.json"):
        symlink(game / core.RECORD, target)
        with pytest.raises(InvalidProject, match=re.escape(core.RECORD)):
            base.holds_original(tmp_path / "cache", game / "data.xp3", "1" * 64)
        (game / core.RECORD).unlink()
