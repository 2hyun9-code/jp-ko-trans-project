import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import copy_project, detect_project, find_app_asar  # noqa: E402


def _write_system_json(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "System.json").write_text(
        json.dumps({"gameTitle": "t"}, ensure_ascii=False), encoding="utf-8"
    )


def test_detect_mz_root_layout(tmp_path):
    _write_system_json(tmp_path / "data")
    layout = detect_project(str(tmp_path))
    assert layout.engine == "MZ"
    assert layout.data_dir == tmp_path / "data"


def test_detect_mv_www_layout(tmp_path):
    _write_system_json(tmp_path / "www" / "data")
    layout = detect_project(str(tmp_path))
    assert layout.engine == "MV"
    assert layout.root == tmp_path / "www"


def test_detect_mv_launch_root_is_one_level_above_www(tmp_path):
    """MV keeps data/ inside "www", sitting next to the actual .exe and
    JS-engine runtime (NW.js/Electron) one level up -- launch_root must
    point there, not at "www" itself, or a copy silently drops the exe."""
    (tmp_path / "Game.exe").write_bytes(b"fake exe")
    _write_system_json(tmp_path / "www" / "data")
    layout = detect_project(str(tmp_path))
    assert layout.launch_root == tmp_path


def test_detect_mz_launch_root_equals_root(tmp_path):
    _write_system_json(tmp_path / "data")
    layout = detect_project(str(tmp_path))
    assert layout.launch_root == layout.root == tmp_path


def test_detect_custom_nested_folder_layout(tmp_path):
    """Some games ship data under an arbitrary folder name (seen in the
    wild: "project/data") rather than the standard www/ or root -- the
    breadth-first search should still find it."""
    _write_system_json(tmp_path / "project" / "data")
    layout = detect_project(str(tmp_path))
    assert layout.engine == "MZ"
    assert layout.data_dir == tmp_path / "project" / "data"


def test_detect_mapinfos_does_not_get_mistaken_for_system_json(tmp_path):
    # A stray MapInfos.json in a directory with no System.json must not
    # cause a false-positive detection.
    d = tmp_path / "data"
    d.mkdir()
    (d / "MapInfos.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        detect_project(str(tmp_path))


def test_detect_raises_for_unrelated_folder(tmp_path):
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError):
        detect_project(str(tmp_path))


def test_detect_raises_for_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        detect_project(str(tmp_path / "does_not_exist"))


def test_find_app_asar_direct(tmp_path):
    asar = tmp_path / "resources" / "app.asar"
    asar.parent.mkdir(parents=True)
    asar.write_bytes(b"fake")
    assert find_app_asar(tmp_path) == asar


def test_find_app_asar_one_level_nested(tmp_path):
    asar = tmp_path / "SomeGame_ver1.0" / "resources" / "app.asar"
    asar.parent.mkdir(parents=True)
    asar.write_bytes(b"fake")
    assert find_app_asar(tmp_path) == asar


def test_find_app_asar_none_when_absent(tmp_path):
    assert find_app_asar(tmp_path) is None


def test_detect_project_falls_back_to_asar_marker(tmp_path):
    asar = tmp_path / "resources" / "app.asar"
    asar.parent.mkdir(parents=True)
    asar.write_bytes(b"fake")
    layout = detect_project(str(tmp_path))
    assert layout.engine == "ASAR"
    assert layout.data_dir is None


def test_copy_project_copies_tree_and_rebases_layout(tmp_path):
    src = tmp_path / "src_game"
    _write_system_json(src / "data")
    (src / "data" / "Map001.json").write_text("{}", encoding="utf-8")

    src_layout = detect_project(str(src))
    dst = tmp_path / "out_game"
    out_layout = copy_project(src_layout, str(dst))

    assert out_layout.root == dst
    assert (out_layout.data_dir / "Map001.json").exists()
    assert out_layout.engine == "MZ"


def test_copy_project_preserves_mv_exe_alongside_www(tmp_path):
    """Regression test: copy_project used to shutil.copytree(layout.root, dst)
    where layout.root == ".../www" for MV, so the .exe and NW.js/Electron
    runtime sitting next to "www" silently never made it into the copy."""
    src = tmp_path / "src_game"
    src.mkdir()
    (src / "Game.exe").write_bytes(b"fake exe")
    (src / "nw.dll").write_bytes(b"fake runtime")
    _write_system_json(src / "www" / "data")

    src_layout = detect_project(str(src))
    dst = tmp_path / "out_game"
    out_layout = copy_project(src_layout, str(dst))

    assert (dst / "Game.exe").exists()
    assert (dst / "nw.dll").exists()
    assert out_layout.launch_root == dst
    assert out_layout.root == dst / "www"
    assert (out_layout.data_dir / "System.json").exists()


def test_copy_project_refuses_existing_output(tmp_path):
    src = tmp_path / "src_game"
    _write_system_json(src / "data")
    src_layout = detect_project(str(src))

    dst = tmp_path / "out_game"
    dst.mkdir()

    with pytest.raises(FileExistsError):
        copy_project(src_layout, str(dst))
