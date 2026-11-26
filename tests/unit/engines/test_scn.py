"""The translatable positions of a decoded .scn scenario, and writing them in every column."""

from __future__ import annotations

import re

import pytest

from bokucho.engines import scn
from bokucho.engines.kag import UnsafeTarget

FILE = "a.txt.scn"


def _root():
    return {"hash": "0", "scenes": [
        {"label": "*start", "title": ["始まり", "Start", "開始"], "texts": [
            ["春香", [["春香", "おはよう", 4], ["Haruka", "Morning", 7], ["春香", "早", 1]],
             "v1", 0, {}],
            [None, [[None, "風が吹く", 4], [None, "Wind", 4], [None, "風", 1]], None, 0, {}],
            ["少女", [["小春", r"うん\nそう", 4, "うんそう", "うんそう"], ["Koharu", "Yes", 3],
                      ["小春", "嗯", 1]], None, 0, {}],
            ["春香", [[None, "", 0], ["Haruka", "", 0], ["春香", "", 0]], None, 0, {}],
        ], "selects": [{"language": [None, {"text": "Go"}, {"text": "去"}, {"text": "去"}],
                        "storage": "b.txt.scn", "target": "*next", "text": "行く"}]},
        {"label": "*next", "title": "始まり"},
    ]}


def test_pieces_are_lines_names_choices_and_titles():
    assert [(p.id, p.kind, p.source, p.speaker) for p in scn.pieces(_root(), FILE)] == [
        ("title:始まり", "title", "始まり", None),
        ("name:春香", "name", "春香", None),
        ("a.txt.scn:start:0", "dialogue", "おはよう", "春香"),
        ("a.txt.scn:start:1", "dialogue", "風が吹く", None),
        ("name:小春", "name", "小春", None),
        ("a.txt.scn:start:2", "dialogue", r"うん\nそう", "小春"),
        ("name:春香", "name", "春香", None),
        ("a.txt.scn:start:choice0", "choice", "行く", None),
        ("title:始まり", "title", "始まり", None),
    ]


def _written(targets):
    root = _root()
    assert scn.rewrite(root, FILE, targets)
    return root


def _texts(root, n):
    return root["scenes"][0]["texts"][n][1]


def test_a_line_goes_into_every_column_and_nothing_else_changes():
    root = _written({"a.txt.scn:start:0": "早安"})
    expected = _root()
    expected["scenes"][0]["texts"][0][1] = [
        ["春香", "早安", 2], ["Haruka", "早安", 2], ["春香", "早安", 2]
    ]
    assert root == expected


def test_a_line_break_becomes_markup_with_the_plain_text_beside_it():
    columns = _texts(_written({"a.txt.scn:start:1": "嗯\r\n對"}), 1)
    assert [c[1:] for c in columns] == [[r"嗯\n對", 2, "嗯對", "嗯對"]] * 3


def test_a_line_without_markup_has_three_items():
    assert _texts(_written({"a.txt.scn:start:2": "好"}), 2) == [
        ["小春", "好", 1], ["Koharu", "好", 1], ["小春", "好", 1]
    ]


def test_a_name_reaches_every_column_a_null_one_too_and_the_key_stays():
    root = _written({"name:春香": "小春香"})
    for n in (0, 3):
        assert [c[0] for c in _texts(root, n)] == ["小春香"] * 3
        assert root["scenes"][0]["texts"][n][0] == "春香"
    assert _texts(root, 2) == _texts(_root(), 2)


def test_a_set_column_0_name_is_the_name_source():
    root = _written({"name:小春": "Ko"})
    assert [c[0] for c in _texts(root, 2)] == ["Ko"] * 3
    assert root["scenes"][0]["texts"][2][0] == "少女"


def test_a_choice_reaches_every_language():
    (select,) = _written({"a.txt.scn:start:choice0": "走"})["scenes"][0]["selects"]
    assert select == {"language": [None, {"text": "走"}, {"text": "走"}, {"text": "走"}],
                      "storage": "b.txt.scn", "target": "*next", "text": "走"}


def test_a_title_reaches_lists_and_strings():
    scenes = _written({"title:始まり": "起點"})["scenes"]
    assert (scenes[0]["title"], scenes[1]["title"]) == (["起點"] * 3, "起點")


def test_targets_equal_to_their_sources_write_nothing():
    root = _root()
    assert not scn.rewrite(root, FILE, {p.id: p.source for p in scn.pieces(root, FILE)})
    assert root == _root()


@pytest.mark.parametrize("unit,target", [
    ("a.txt.scn:start:0", r"早\x"), ("a.txt.scn:start:0", "[よみ,1]早安"),
    ("a.txt.scn:start:0", "早\\"), ("name:春香", "春\n香"), ("title:始まり", r"起\n點"),
])
def test_markup_the_source_lacks_is_refused(unit, target):
    with pytest.raises(UnsafeTarget, match=re.escape(unit)):
        scn.rewrite(_root(), FILE, {unit: target})


@pytest.mark.parametrize("source,target,whole", [
    ("[よみ,1]漢字だ", "[よみ,1]漢字の", True),
    ("[よみ,1]漢字だ", "早安", True),
    ("[よみ,1]漢字だ", "[よみ,1]漢", False),
    ("[よみ,1]漢字だ", "早[よみ,1]", False),
    # A line break there would leave half of its \n in the base.
    ("[よみ,1]漢字だ", "[よみ,1]漢\n字", False),
    # The tags are read as the column rule reads them: \[ is a bracket, and a reading
    # may hold a comma.
    (r"元\[よみ,1]です", r"譯\[よみ,1]", True),
    ("[よ,み,1]漢字だ", "[よ,み,1]漢字", True),
    # A tag without a comma is no ruby, and a bracket the original has may stay.
    ("[3]あいうえ", "[3]四", True),
    ("[注]あい", "[注]好", True),
    # So many digits are no ruby either, nor a bracket the original lacks.
    ("[よみ,1]漢字だ", "[a," + "1" * 4301 + "]あ", False),
])
def test_a_ruby_tag_needs_the_whole_plain_base_it_covers_after_it(
    scn_tree, source, target, whole
):
    root = scn_tree(("k", (source, "x", "y")))
    (line,) = [p for p in scn.pieces(root, FILE) if p.kind == "dialogue"]
    if whole:
        assert scn.rewrite(root, FILE, {line.id: target})
    else:
        with pytest.raises(UnsafeTarget, match=re.escape(line.id)):
            scn.rewrite(root, FILE, {line.id: target})


@pytest.mark.parametrize("unit", ["a.txt.scn:start:0", "name:春香", "title:始まり"])
def test_a_nul_that_would_end_the_stored_string_is_refused(unit):
    root = _root()
    with pytest.raises(UnsafeTarget, match=re.escape(unit)):
        scn.rewrite(root, FILE, {unit: "早\0安"})
    assert root == _root()


@pytest.mark.parametrize("unit", ["a.txt.scn:start:0", "name:春香", "title:始まり"])
def test_a_lone_surrogate_no_encoding_writes_is_refused(unit):
    root = _root()
    with pytest.raises(UnsafeTarget, match=re.escape(unit)):
        scn.rewrite(root, FILE, {unit: "早\ud800安"})
    assert root == _root()


@pytest.mark.parametrize("name,text,expected", [
    (None, "おはよう", [None, "おはよう", 4]),
    ("春", r"うん\nそう", ["春", r"うん\nそう", 4, "うんそう", "うんそう"]),
    (None, r"50\%", [None, r"50\%", 3, "50%", "50%"]),
    (None, r"R\&R", [None, r"R\&R", 3, "R${$38}R", "R${$38}R"]),
    (None, "[よみ,1]漢字だ", [None, "[よみ,1]漢字だ", 3, "よみだ", "漢字だ"]),
    (None, "[よ,み,1]漢字", [None, "[よ,み,1]漢字", 2, "よ,み", "漢字"]),
    (None, r"\[よみ,1]", [None, r"\[よみ,1]", 6, "[よみ,1]", "[よみ,1]"]),
    (None, "[3]あ", [None, "[3]あ", 4, "[3]あ", "[3]あ"]),
])
def test_the_column_rule(name, text, expected):
    assert scn.column(name, text) == expected


@pytest.mark.parametrize("text", [
    "[" * 1_000_000,
    "[" * 1_000_000 + "]",
    "[," + "[" * 1_000_000 + "1]",
], ids=["no-close", "far-close", "far-close-after-a-comma"])
def test_a_line_of_many_brackets_is_read_in_linear_time(text):
    # Finding the "]" and the "," afresh at every "[" would search the line once per "[".
    searched = []

    class Counted(str):
        def find(self, sub, start=0, end=None):
            end = len(self) if end is None else end
            at = str.find(self, sub, start, end)
            searched.append((end if at == -1 else at + len(sub)) - start)
            return at

        def rfind(self, sub, start=0, end=None):
            end = len(self) if end is None else end
            at = str.rfind(self, sub, start, end)
            searched.append(end - (start if at == -1 else at))
            return at

    assert scn.column(None, Counted(text))[2] == len(text)
    assert searched and sum(searched) <= 3 * len(text)


@pytest.mark.parametrize("target,written", [
    # A lone last "\" escapes what follows it: the "\" of the "\n" the break becomes.
    ("早\\\n安", None),
    ("早\\\\\n安", "早\\\\\\n安"),
    ("早\n\n安", "早\\n\\n安"),
])
def test_a_line_break_right_after_a_lone_backslash_is_refused(scn_tree, target, written):
    root = scn_tree(("k", (r"元\\です", "x", "y")))
    (line,) = [p for p in scn.pieces(root, FILE) if p.kind == "dialogue"]
    if written is None:
        with pytest.raises(UnsafeTarget, match=re.escape(line.id)):
            scn.rewrite(root, FILE, {line.id: target})
    else:
        assert scn.rewrite(root, FILE, {line.id: target})
        assert root["scenes"][0]["texts"][0][1][0][1] == written


def test_an_entry_without_columns_offers_no_name_it_could_not_write():
    root = {"scenes": [{"label": "*a", "texts": [["春香", [], None, 0, {}]]}]}
    assert scn.pieces(root, FILE) == []


def test_an_entry_with_two_columns_and_an_empty_key_gets_both_and_no_name():
    root = {"scenes": [{"label": "*a", "texts": [
        ["", [[None, "あ", 1], [None, "A", 1]], None, 0, {}]
    ]}]}
    assert [(p.id, p.kind, p.speaker) for p in scn.pieces(root, FILE)] == [
        ("a.txt.scn:a:0", "dialogue", None)
    ]
    assert scn.rewrite(root, FILE, {"a.txt.scn:a:0": "啊"})
    assert root["scenes"][0]["texts"][0][1] == [[None, "啊", 1], [None, "啊", 1]]


def test_two_scenes_whose_lines_would_share_ids_are_refused():
    scene = {"label": "*a", "texts": [[None, [[None, "あ", 1]], None, 0, {}]]}
    with pytest.raises(scn.UnknownShape, match=re.escape("a.txt.scn:a:0")):
        scn.pieces({"scenes": [scene, dict(scene, label="a")]}, FILE)


@pytest.mark.parametrize("root", [
    {"scenes": "x"}, {"scenes": [{"label": 1}]},
    {"scenes": [{"label": "*a", "texts": [["k", "not columns"]]}]},
    {"scenes": [{"label": "*a", "selects": ["x"]}]},
])
def test_an_unknown_shape_is_refused(root):
    with pytest.raises(scn.UnknownShape):
        scn.pieces(root, FILE)
