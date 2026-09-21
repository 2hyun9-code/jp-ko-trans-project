"""Regression test for a real bug: for an asar-packed Electron game,
run_all() used to report/return the folder it extracted the archive into
(deep inside resources/app/...) instead of the top-level game folder that
actually has the .exe -- so "결과 폴더 열기" opened a folder with no way to
launch the game."""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
from translator import OllamaTranslator  # noqa: E402


def _build_fake_asar(entries: dict[str, bytes]) -> bytes:
    file_data = b""
    files_tree: dict = {}
    offset = 0
    for path, content in entries.items():
        parts = path.split("/")
        node = files_tree
        for p in parts[:-1]:
            node = node.setdefault(p, {"files": {}})["files"]
        node[parts[-1]] = {"size": len(content), "offset": str(offset)}
        file_data += content
        offset += len(content)

    header_json = json.dumps({"files": files_tree}).encode("utf-8")
    json_len = len(header_json)
    padded_len = (json_len + 3) & ~3
    padded_json = header_json + b"\x00" * (padded_len - json_len)

    out = struct.pack("<I", 4)
    out += struct.pack("<I", 8 + padded_len)
    out += struct.pack("<I", 4 + padded_len)
    out += struct.pack("<I", json_len)
    out += padded_json
    out += file_data
    return out


def _make_fake_asar_game(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"fake exe")  # marker: this is the launchable folder

    system = {"gameTitle": "テストゲーム", "terms": {"basic": [], "commands": [], "params": [],
                                                    "messages": {}}}
    map1 = {"events": [None, {"id": 1, "pages": [{"list": [
        {"code": 401, "parameters": ["こんにちは"]},
        {"code": 0, "parameters": []},
    ]}]}]}
    actors = [None]
    entries = {
        "project/data/System.json": json.dumps(system, ensure_ascii=False).encode("utf-8"),
        "project/data/Map001.json": json.dumps(map1, ensure_ascii=False).encode("utf-8"),
        "project/data/Actors.json": json.dumps(actors).encode("utf-8"),
        "project/index.html": b"<html></html>",
    }
    for name in ["Classes", "Skills", "Items", "Weapons", "Armors", "Enemies", "States",
                 "CommonEvents", "Troops", "MapInfos"]:
        entries[f"project/data/{name}.json"] = b"[null]"

    asar_bytes = _build_fake_asar(entries)
    (root / "resources").mkdir()
    (root / "resources" / "app.asar").write_bytes(asar_bytes)


def test_run_all_reports_the_launchable_game_root_for_asar_games(tmp_path, monkeypatch):
    # Stub out the network call so this test needs neither Ollama nor a
    # real model -- translate() only cares that _call_model returns *some*
    # valid-looking Korean text.
    monkeypatch.setattr(OllamaTranslator, "_call_model", lambda self, text: "안녕하세요")

    game_root = tmp_path / "MyGame"
    _make_fake_asar_game(game_root)

    out_dir = tmp_path / "MyGame_KO"
    cache_path = tmp_path / "cache.json"

    result = pipeline.run_all(
        game=str(game_root), out=str(out_dir), font=None, model="test-model",
        cache_path=str(cache_path), log=lambda msg: None, workers=1,
    )

    assert Path(result) == out_dir
    assert (Path(result) / "Game.exe").exists(), "the launchable .exe must survive in the reported folder"
    assert (Path(result) / "resources" / "app.asar.bak").exists(), "original asar renamed aside"
    assert not (Path(result) / "resources" / "app.asar").exists(), "no stale app.asar left for Electron to prefer"
    assert (Path(result) / "resources" / "app" / "project" / "data" / "System.json").exists()
