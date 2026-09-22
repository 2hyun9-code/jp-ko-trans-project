import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
from engine import detect_project  # noqa: E402
from refindex import build_reference_index  # noqa: E402
from translator import OllamaTranslator  # noqa: E402


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _make_game(root: Path) -> Path:
    d = root / "data"
    _write(d / "System.json", {
        "gameTitle": "テスト", "elements": ["", "火", "水"],
        "switches": ["", "ボス撃破"], "variables": ["", "好感度"],
        "terms": {"basic": [], "commands": [], "params": [], "messages": {}},
    })
    _write(d / "Items.json", [None,
                              {"id": 1, "name": "ポーション", "description": "", "note": ""},
                              {"id": 2, "name": "エーテル", "description": "", "note": ""}])
    _write(d / "Skills.json", [None, {"id": 1, "name": "ファイア", "description": "",
                                      "note": "<Element Rate: 火 50%>\n<Cost:マナ,10>"}])
    _write(d / "Animations.json", [None, {"id": 1, "name": "ウェーブ"}])
    _write(d / "Map001.json", {"note": "", "events": [None, {
        "id": 1, "name": "宝箱イベント", "note": "", "pages": [{"list": [
            {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
            {"code": 401, "indent": 0, "parameters": ["ポーションを手に入れた！"]},
            {"code": 355, "indent": 0, "parameters": [
                '$gameParty.hasItem($dataItems.find(i => i.name === "ポーション"))']},
            {"code": 356, "indent": 0, "parameters": ["Spawn ゴブリン 3"]},
            {"code": 0, "indent": 0, "parameters": []},
        ]}]}]})
    _write(d / "CommonEvents.json", [None, {"id": 1, "name": "初期化", "list": [
        {"code": 357, "indent": 0, "parameters": ["Plugin", "Give", "", {"item": "ゴールドキー"}]},
        {"code": 0, "indent": 0, "parameters": []}]}])
    for name in ["Actors", "Classes", "Weapons", "Armors", "Enemies", "States", "Troops",
                 "MapInfos", "Tilesets"]:
        _write(d / f"{name}.json", "[null]")
    _write(root / "js" / "plugins.js", "var $plugins = " + json.dumps([
        {"name": "Shop", "status": True, "parameters": {"ItemName": "マナ", "HelpText": "ようこそ"}},
        {"name": "Off", "status": False, "parameters": {"Target": "無効プラグイン"}},
    ], ensure_ascii=False) + ";")
    _write(root / "js" / "plugins" / "Foo.js",
           "/*:\n * @help 'コメント内'\n */\n// 'これもコメント'\nvar k = '防御'; var u = \"http://x\";\n")
    _write(root / "index.html", '<html><body><script src="js/main.js"></script></body></html>')
    return root


def test_reference_index_collects_every_kind_of_reference(tmp_path):
    refs = build_reference_index(detect_project(str(_make_game(tmp_path / "g"))))
    assert "ポーション" in refs        # string compared in a Script command
    assert "火" in refs and "マナ" in refs  # values inside note tags
    assert "ウェーブ" in refs           # Animations.json name
    assert "ボス撃破" in refs and "好感度" in refs  # switch / variable names
    assert "宝箱イベント" in refs       # map event name
    assert "ゴブリン" in refs           # MV plugin command argument
    assert "ゴールドキー" in refs       # MZ plugin command argument
    assert "防御" in refs               # literal in plugin source code


def test_reference_index_skips_display_text_comments_and_disabled_plugins(tmp_path):
    refs = build_reference_index(detect_project(str(_make_game(tmp_path / "g"))))
    assert "ようこそ" not in refs       # plugin param under a display-text key
    assert "コメント内" not in refs and "これもコメント" not in refs  # in comments
    assert "無効プラグイン" not in refs  # disabled plugin
    assert "エーテル" not in refs       # an item nothing refers to
    assert "ポーションを手に入れた！" not in refs  # ordinary dialogue


def test_referenced_names_stay_original_in_data_but_are_translated_on_screen(tmp_path, monkeypatch):
    game = _make_game(tmp_path / "g")
    monkeypatch.setattr(OllamaTranslator, "_call_model", lambda self, t: "번역됨")
    logs: list[str] = []
    out = tmp_path / "g_KO"
    pipeline.run_all(game=str(game), out=str(out), font=None, model="m",
                     cache_path=str(tmp_path / "c.json"), log=logs.append, workers=1)

    items = json.loads((out / "data" / "Items.json").read_text(encoding="utf-8"))
    assert items[1]["name"] == "ポーション", "referenced by a script: must stay original"
    assert items[2]["name"] == "번역됨", "unreferenced item names are still translated"
    system = json.loads((out / "data" / "System.json").read_text(encoding="utf-8"))
    assert system["elements"][1] == "火"

    patch = (out / "js" / "korean_patch.js").read_text(encoding="utf-8")
    assert '"ポーション": "번역됨"' in patch, "still shown in Korean via the render patch"
    assert any("화면에 그릴 때만 번역" in line for line in logs)
