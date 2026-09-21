import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import ProjectLayout  # noqa: E402
from textwalk import (  # noqa: E402
    _extract_ja_fragments,
    _extract_plugin_command_text,
    collect_glossary_names,
    collect_plugin_command_texts,
    walk_project,
)


def _make_mz_project(root: Path) -> ProjectLayout:
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    system = {
        "gameTitle": "テストゲーム",
        "terms": {"basic": ["レベル"], "commands": [], "params": [], "messages": {}},
    }
    (data_dir / "System.json").write_text(json.dumps(system, ensure_ascii=False), encoding="utf-8")

    map1 = {
        "events": [
            None,
            {
                "id": 1,
                "pages": [
                    {
                        "list": [
                            {"code": 101, "parameters": ["", 0, 0, 0, "柴乃"]},
                            {"code": 401, "parameters": ["こんにちは、\\N[1]さん！"]},
                            {"code": 102, "parameters": [["はい", "いいえ"], 0]},
                            {"code": 0, "parameters": []},
                        ]
                    }
                ],
            },
        ]
    }
    (data_dir / "Map001.json").write_text(json.dumps(map1, ensure_ascii=False), encoding="utf-8")

    mapinfos = [None, {"id": 1, "name": "柴乃の村", "expanded": False}]
    (data_dir / "MapInfos.json").write_text(json.dumps(mapinfos, ensure_ascii=False), encoding="utf-8")

    actors = [None, {"id": 1, "name": "柴乃", "nickname": "", "profile": "", "note": ""}]
    (data_dir / "Actors.json").write_text(json.dumps(actors, ensure_ascii=False), encoding="utf-8")

    items = [None, {"id": 1, "name": "柴乃の剣", "description": "", "note": ""}]
    (data_dir / "Items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    for name in ["Classes", "Skills", "Weapons", "Armors", "Enemies", "States",
                 "CommonEvents", "Troops"]:
        (data_dir / f"{name}.json").write_text("[null]", encoding="utf-8")

    return ProjectLayout(root=root, data_dir=data_dir, fonts_dir=root / "fonts",
                          css_dir=root / "css", engine="MZ")


def test_walk_project_collects_expected_strings(tmp_path):
    layout = _make_mz_project(tmp_path)

    seen: set[str] = set()

    def collect(text: str) -> str:
        seen.add(text)
        return text

    walk_project(layout, collect)

    assert "柴乃" in seen                     # name-box speaker (code 101)
    assert "こんにちは、\\N[1]さん！" in seen   # dialogue line (code 401)
    assert "はい" in seen and "いいえ" in seen  # choice list (code 102)
    assert "柴乃の村" in seen                  # MapInfos.json map display name
    assert "柴乃の剣" in seen                  # Items.json name field
    assert "テストゲーム" in seen               # System.json gameTitle
    assert "レベル" in seen                    # System.json terms.basic


def test_walk_project_ignores_mapinfos_as_a_map_file(tmp_path):
    """MapInfos.json is a top-level list, not a map's {"events": [...]} shape
    -- walking it as if it were a map file should not blow up."""
    layout = _make_mz_project(tmp_path)
    walk_project(layout, lambda t: t)  # would raise if MapInfos.json were mishandled


def test_walk_project_replace_round_trip(tmp_path):
    layout = _make_mz_project(tmp_path)

    translations = {
        "柴乃": "시바노",
        "こんにちは、\\N[1]さん！": "안녕하세요, \\N[1]님!",
        "柴乃の村": "시바노 마을",
        "柴乃の剣": "시바노의 검",
    }

    def replace(text: str) -> str:
        return translations.get(text, text)

    walk_project(layout, replace)

    map1 = json.loads((layout.data_dir / "Map001.json").read_text(encoding="utf-8"))
    commands = map1["events"][1]["pages"][0]["list"]
    assert commands[0]["parameters"][4] == "시바노"
    assert commands[1]["parameters"][0] == "안녕하세요, \\N[1]님!"

    mapinfos = json.loads((layout.data_dir / "MapInfos.json").read_text(encoding="utf-8"))
    assert mapinfos[1]["name"] == "시바노 마을"

    items = json.loads((layout.data_dir / "Items.json").read_text(encoding="utf-8"))
    assert items[1]["name"] == "시바노의 검"


def test_extract_plugin_command_text_drops_command_name_and_trailing_number():
    line = "D_TEXT \\ow[5]好感度: \\V[43] 31"
    assert _extract_plugin_command_text(line) == "\\ow[5]好感度: \\V[43]"


def test_extract_plugin_command_text_returns_none_for_non_japanese_command():
    # A plugin command with no Japanese text anywhere isn't a translation
    # candidate (also covers commands with only numeric/ASCII args).
    assert _extract_plugin_command_text("ChangeMap 3 5 5") is None


def test_extract_plugin_command_text_returns_none_for_single_token():
    assert _extract_plugin_command_text("SomeCommand") is None


def test_collect_plugin_command_texts_from_common_event(tmp_path):
    layout = _make_mz_project(tmp_path)
    ce_path = layout.data_dir / "CommonEvents.json"
    ce = [None, {"id": 1, "list": [
        {"code": 356, "parameters": ["D_TEXT \\ow[5]行動力: \\V[41]/5 37"]},
        {"code": 356, "parameters": ["ChangeMap 3 5 5"]},  # no JA text: not a candidate
        {"code": 401, "parameters": ["こんにちは"]},          # not a plugin command: ignored here
    ]}]
    ce_path.write_text(json.dumps(ce, ensure_ascii=False), encoding="utf-8")

    texts = collect_plugin_command_texts(layout)
    # The whole line is harvested, PLUS its Japanese fragment on its own --
    # some HUD/gauge plugins rebuild the on-screen text from pieces at draw
    # time, so the whole-line string may never appear verbatim; caching the
    # bare label too gives the render patch's substring fallback something
    # to match against in that case.
    assert texts == {"\\ow[5]行動力: \\V[41]/5", "行動力"}


def test_extract_ja_fragments_drops_escape_codes_and_digits():
    assert _extract_ja_fragments("\\ow[5]行動力: \\V[41]/5") == ["行動力"]
    assert _extract_ja_fragments("ChangeMap 3 5 5") == []
    assert _extract_ja_fragments("\\ow[5]好感度: \\V[43]") == ["好感度"]


def test_collect_glossary_names_is_consistent_with_walk(tmp_path):
    layout = _make_mz_project(tmp_path)
    names = collect_glossary_names(layout)

    assert "柴乃" in names       # Actors.json name + name-box, same string
    assert "柴乃の村" in names   # MapInfos.json name
    assert "柴乃の剣" in names   # Items.json name
    # Plain dialogue lines are not proper nouns and shouldn't be in the glossary.
    assert "こんにちは、\\N[1]さん！" not in names
