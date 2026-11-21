"""The release carries UnityPy, and the TextMesh Pro fallback fonts beside the executable."""

import importlib.util
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"bokucho_tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_release_carries_unitypy_and_keeps_the_fonts_out_of_the_executable(tmp_path):
    build = _tool("build")
    arguments = build.pyinstaller_arguments(
        tmp_path / "stub.exe", tmp_path / "winmm.dll", tmp_path / "font"
    )
    assert "--collect-all=UnityPy" in arguments
    assert "--hidden-import=bokucho.engines.unity" in arguments
    # 30 MB a font, unpacked on every start if they were inside the one-file program.
    assert not any("unity-fonts" in argument for argument in arguments)
    assert "unity-fonts" in build.PORTABLE_DIRECTORIES


def test_the_fonts_the_font_tool_builds_are_the_ones_a_release_ships():
    build = _tool("build")
    fonts = _tool("build_unity_fonts")
    assert fonts.DEFAULT_DESTINATION == build.UNITY_FONTS_DIR


def test_a_frozen_bokucho_finds_the_fonts_beside_its_executable(tmp_path, monkeypatch):
    from bokucho.application import release

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Bokucho.exe"))
    assert release.bundled_unity_fonts() == tmp_path.resolve() / "unity-fonts"
