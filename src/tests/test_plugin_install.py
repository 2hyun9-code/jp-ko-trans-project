import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import ProjectLayout  # noqa: E402
from plugin_install import install_hangul_name_plugin  # noqa: E402


def _make_layout(root: Path, engine: str = "MZ") -> ProjectLayout:
    root.mkdir(parents=True, exist_ok=True)
    return ProjectLayout(root=root, data_dir=root / "data", fonts_dir=root / "fonts",
                          css_dir=root / "css", engine=engine)


def _write_plugins_js(root: Path, plugins: list) -> Path:
    js_dir = root / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    path = js_dir / "plugins.js"
    path.write_text("var $plugins = " + json.dumps(plugins) + ";\n", encoding="utf-8")
    return path


def test_install_copies_plugin_and_registers_it(tmp_path):
    layout = _make_layout(tmp_path / "game")
    _write_plugins_js(layout.root, [{"name": "SomeOtherPlugin", "status": True,
                                       "description": "", "parameters": {}}])

    msg = install_hangul_name_plugin(layout)

    assert msg is not None
    plugin_file = layout.root / "js" / "plugins" / "SteamB23_HangulNameEdit.js"
    assert plugin_file.exists()
    assert "MIT License" in plugin_file.read_text(encoding="utf-8")

    plugins = json.loads((layout.root / "js" / "plugins.js").read_text(encoding="utf-8")
                          .removeprefix("var $plugins = ").rstrip("\n;"))
    names = [p["name"] for p in plugins]
    assert names[0] == "SteamB23_HangulNameEdit"  # inserted at the top
    assert "SomeOtherPlugin" in names


def test_install_is_idempotent(tmp_path):
    layout = _make_layout(tmp_path / "game")
    _write_plugins_js(layout.root, [])

    first = install_hangul_name_plugin(layout)
    second = install_hangul_name_plugin(layout)

    assert first is not None
    assert second is None  # already installed, nothing more to do

    plugins_text = (layout.root / "js" / "plugins.js").read_text(encoding="utf-8")
    assert plugins_text.count("SteamB23_HangulNameEdit") == 1


def test_install_uses_mv_or_mz_variant(tmp_path):
    mv_layout = _make_layout(tmp_path / "mv_game", engine="MV")
    _write_plugins_js(mv_layout.root, [])
    install_hangul_name_plugin(mv_layout)
    mv_plugin = (mv_layout.root / "js" / "plugins" / "SteamB23_HangulNameEdit.js").read_text(encoding="utf-8")

    mz_layout = _make_layout(tmp_path / "mz_game", engine="MZ")
    _write_plugins_js(mz_layout.root, [])
    install_hangul_name_plugin(mz_layout)
    mz_plugin = (mz_layout.root / "js" / "plugins" / "SteamB23_HangulNameEdit.js").read_text(encoding="utf-8")

    # the MV build overrides Window_NameInput.prototype.initialize; the MZ
    # build doesn't need to (MZ's own initialize signature differs), so the
    # two bundled files are not identical -- installing must pick the one
    # matching the detected engine.
    assert mv_plugin != mz_plugin
    assert "Window_NameInput.prototype.initialize = function (editWindow)" in mv_plugin


def test_install_returns_none_without_plugins_js(tmp_path):
    layout = _make_layout(tmp_path / "game")
    assert install_hangul_name_plugin(layout) is None
