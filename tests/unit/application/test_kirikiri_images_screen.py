"""The Images screen lists a KiriKiri game's pictures, unpacked from its archives."""

from __future__ import annotations

import io

from PIL import Image

from bokucho.application.assets import extract_images
from bokucho.application.projects import resolve_game_root
from bokucho.project import Project


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _project(tmp_path, new_project, game, **engine):
    project = new_project(engine="kirikiri")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(game), **engine}
                )
            }
        )
    )
    return Project.load(project.root)


def test_extracting_images_lists_the_unpacked_pictures(tmp_path, new_project, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "あ\n"},
        files={
            "image/bg.png": _png("red"),
            "image/start_chh.png": _png("blue"),
            "rule/op.png": _png("green"),
            "bgimage/sky.png": _png("white"),
        },
    )
    project = _project(tmp_path, new_project, game, kag_skip_images=["*_chh.*"])
    listed = extract_images(project)
    assert [entry["image"] for entry in listed.entries] == ["image/bg.png", "rule/op.png"]
    assert resolve_game_root(project) == project.root / "unpacked" / "data"


def test_an_empty_folder_list_unpacks_nothing(tmp_path, new_project, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, files={"image/bg.png": _png("red")})
    project = _project(tmp_path, new_project, game, kag_image_folders=[])
    assert extract_images(project).entries == ()
