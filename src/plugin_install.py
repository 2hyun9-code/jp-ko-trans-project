"""Optional post-translation step: installs a small bundled RPG Maker
plugin so players can actually type a Korean name in-game once dialogue is
translated -- the stock Name Input scene only lets you type Latin/kana
characters, not Hangul, no matter how translated the rest of the game is.

Plugin: SteamB23_HangulNameEdit (MIT license, see plugins/MV|MZ/*.js for the
full license text). Bundled once per engine because the MV and MZ builds
differ slightly (MZ's Window_NameInput doesn't need the initialize()
override MV's does).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from engine import ProjectLayout

_PLUGIN_NAME = "SteamB23_HangulNameEdit"
# A PyInstaller --onefile build extracts its --add-data bundle to a temp
# dir at sys._MEIPASS at runtime, not next to this .py file's location.
_APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_PLUGINS_SRC_DIR = _APP_DIR / "plugins"

_PLUGIN_ENTRY = {
    "name": _PLUGIN_NAME,
    "status": True,
    "description": "한글 이름 입력창 2.1v",
    "parameters": {"자판 형식": "0"},
}

_PLUGINS_ASSIGN_RE = re.compile(r"^(.*?\$plugins\s*=\s*)(\[.*\])(\s*;?\s*)$", re.DOTALL)


def _bundled_source(engine: str) -> Optional[Path]:
    sub = "MV" if engine == "MV" else "MZ" if engine in ("MZ", "MZ-ASAR") else None
    if sub is None:
        return None
    path = _PLUGINS_SRC_DIR / sub / f"{_PLUGIN_NAME}.js"
    return path if path.exists() else None


def install_hangul_name_plugin(layout: ProjectLayout) -> Optional[str]:
    """Copies the plugin file into js/plugins/ and registers it at the top
    of js/plugins.js so it runs before the game's other plugins. Safe to
    call again on a re-run: it's a no-op if the plugin is already listed.
    Returns a short status message to log, or None if there's nothing to
    do (unsupported engine, or no plugins.js to add to)."""
    src = _bundled_source(layout.engine)
    if src is None:
        return None

    plugins_js = layout.root / "js" / "plugins.js"
    if not plugins_js.exists():
        return None

    text = plugins_js.read_text(encoding="utf-8")
    m = _PLUGINS_ASSIGN_RE.search(text)
    if not m:
        return "plugins.js 형식을 인식하지 못해 한글 이름 입력 플러그인 설치를 건너뜁니다."
    prefix, array_text, suffix = m.group(1), m.group(2), m.group(3)
    try:
        plugins = json.loads(array_text)
    except json.JSONDecodeError:
        return "plugins.js를 읽지 못해 한글 이름 입력 플러그인 설치를 건너뜁니다."
    if not isinstance(plugins, list):
        return "plugins.js 형식을 인식하지 못해 한글 이름 입력 플러그인 설치를 건너뜁니다."

    if any(isinstance(p, dict) and p.get("name") == _PLUGIN_NAME for p in plugins):
        return None  # already installed (re-run on an existing output folder)

    plugins_dir = layout.root / "js" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, plugins_dir / f"{_PLUGIN_NAME}.js")

    plugins.insert(0, dict(_PLUGIN_ENTRY))
    new_text = prefix + json.dumps(plugins, ensure_ascii=False, indent=4) + suffix
    plugins_js.write_text(new_text, encoding="utf-8")
    return "한글 이름 입력 플러그인을 추가했습니다 (이름 입력창에서 한글 타이핑 가능)."
