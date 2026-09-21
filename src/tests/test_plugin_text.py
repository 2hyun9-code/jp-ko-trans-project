import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin_text import collect_plugin_strings, parse_plugins_js  # noqa: E402


def _write_plugins_js(path: Path, plugins: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(plugins, ensure_ascii=False)
    path.write_text(f"var $plugins =\n{body};\n", encoding="utf-8")


def test_parse_plugins_js_strips_js_wrapper(tmp_path):
    p = tmp_path / "plugins.js"
    _write_plugins_js(p, [{"name": "A", "status": True, "parameters": {}}])
    parsed = parse_plugins_js(p)
    assert parsed == [{"name": "A", "status": True, "parameters": {}}]


def test_parse_plugins_js_returns_empty_list_for_garbage(tmp_path):
    p = tmp_path / "plugins.js"
    p.write_text("console.log('not a plugins file');", encoding="utf-8")
    assert parse_plugins_js(p) == []


def test_collect_plugin_strings_finds_flat_japanese_value(tmp_path):
    p = tmp_path / "plugins.js"
    _write_plugins_js(p, [
        {"name": "BB_CustomSaveWindow", "status": True,
         "parameters": {"Item3title": "好感度", "ItemValue3": "3", "MaxItem": "4"}},
    ])
    strings = collect_plugin_strings(p)
    assert "好感度" in strings
    # pure-numeric / ASCII parameter values must not be pulled in as "text"
    assert "3" not in strings
    assert "4" not in strings


def test_collect_plugin_strings_skips_disabled_plugins(tmp_path):
    p = tmp_path / "plugins.js"
    _write_plugins_js(p, [
        {"name": "Disabled", "status": False, "parameters": {"Label": "行動力"}},
    ])
    assert collect_plugin_strings(p) == set()


def test_collect_plugin_strings_recurses_into_nested_json_string_params(tmp_path):
    # RPG Maker plugins commonly encode struct/list-type parameters as a
    # JSON string *inside* the outer JSON -- this must be parsed one level
    # deeper rather than treated as an opaque (untranslatable-looking, or
    # worse, mistranslated-as-a-whole) blob.
    nested = json.dumps([{"CommandName": "えっちな記録", "SomeId": "0"}], ensure_ascii=False)
    p = tmp_path / "plugins.js"
    _write_plugins_js(p, [
        {"name": "SceneGlossary", "status": True,
         "parameters": {"GlossaryInfo": nested, "CompleteMessage": "好感度 \\V[43]"}},
    ])
    strings = collect_plugin_strings(p)
    assert "えっちな記録" in strings
    assert "好感度 \\V[43]" in strings
    assert nested not in strings  # the raw JSON blob itself shouldn't be a "translation candidate"


def test_collect_plugin_strings_ignores_non_japanese_ascii_values(tmp_path):
    p = tmp_path / "plugins.js"
    _write_plugins_js(p, [
        {"name": "Foo", "status": True,
         "parameters": {"Color": "#ffffff", "Image": "UI_03", "Enabled": "true"}},
    ])
    assert collect_plugin_strings(p) == set()
