"""Strings the game (or a plugin) looks something up *by*, as opposed to
text it just shows.

A plugin note tag like <Element Rate: 火 50%>, a script condition like
`$dataItems.find(i => i.name === "ポーション")`, or a plugin parameter
naming a skill all refer to a database entry by its exact Japanese name.
Translating that name inside data/*.json makes the lookup silently miss --
wrong damage, a shop that sells nothing, a crash with "... is unknown".
Nothing about the string's *shape* tells a name from a word, so this goes
by *usage*: collect every place the project uses a string as a reference,
and let the pipeline keep matching candidates untranslated in the data
files. They still reach the player in Korean through the render-time patch
(render_patch.py), which swaps text at draw time without touching data.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from engine import ProjectLayout
from plugin_text import parse_plugins_js
from textwalk import DB_FILES

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_JS_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\\n])*)\1""")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
# Only comments that start a line or follow whitespace/punctuation, so the
# "//" in a "http://..." string literal isn't mistaken for one.
_LINE_COMMENT_RE = re.compile(r"(^|[\s;{}()])//[^\n]*", re.M)
_NOTE_SPLIT_RE = re.compile(r"[<>:：,，、=\s]+")
_MAP_FILE_RE = re.compile(r"^Map\d+\.json$", re.IGNORECASE)

# Plugin parameter keys whose values are on-screen wording rather than a
# pointer to something. Everything else is treated as a possible reference.
_DISPLAY_KEY_HINTS = ("text", "message", "msg", "label", "caption", "prompt", "help",
                      "desc", "title", "テキスト", "文字列", "メッセージ", "説明", "表示")

Add = Callable[[object], None]


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _entries(path: Path) -> list[dict]:
    data = _load(path)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _add_js_literals(code: str, add: Add) -> None:
    code = _BLOCK_COMMENT_RE.sub("", code)
    code = _LINE_COMMENT_RE.sub(r"\1", code)
    for m in _JS_STRING_RE.finditer(code):
        add(m.group(2))


def _add_note(note, add: Add) -> None:
    if isinstance(note, str) and note:
        for piece in _NOTE_SPLIT_RE.split(note):
            add(piece)


def _add_nested(value, add: Add) -> None:
    if isinstance(value, str):
        if value.strip()[:1] in ("[", "{"):
            try:
                _add_nested(json.loads(value), add)
                return
            except ValueError:
                pass
        add(value)
    elif isinstance(value, list):
        for v in value:
            _add_nested(v, add)
    elif isinstance(value, dict):
        for v in value.values():
            _add_nested(v, add)


def _scan_commands(commands, add: Add) -> None:
    for cmd in commands or []:
        if not isinstance(cmd, dict):
            continue
        code, params = cmd.get("code"), cmd.get("parameters") or []
        if code in (355, 655) and params and isinstance(params[0], str):
            _add_js_literals(params[0], add)                    # Script
        elif code == 111 and len(params) > 1 and params[0] == 12 and isinstance(params[1], str):
            _add_js_literals(params[1], add)                    # Conditional Branch: Script
        elif code == 122 and len(params) > 4 and params[3] == 4 and isinstance(params[4], str):
            _add_js_literals(params[4], add)                    # Control Variables: Script
        elif code == 356 and params and isinstance(params[0], str):
            for token in params[0].split():                     # MV Plugin Command
                add(token)
        elif code == 357 and len(params) > 3:
            _add_nested(params[3], add)                         # MZ Plugin Command args


def _is_display_key(key: str) -> bool:
    k = (key or "").lower()
    return any(h in k for h in _DISPLAY_KEY_HINTS)


def _add_plugin_params(value, key: str, add: Add) -> None:
    if isinstance(value, str):
        if value.strip()[:1] in ("[", "{"):
            try:
                _add_plugin_params(json.loads(value), key, add)
                return
            except ValueError:
                pass
        if not _is_display_key(key):
            add(value)
    elif isinstance(value, list):
        for v in value:
            _add_plugin_params(v, key, add)
    elif isinstance(value, dict):
        for k, v in value.items():
            _add_plugin_params(v, k, add)


def build_reference_index(layout: ProjectLayout) -> set[str]:
    refs: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str):
            s = value.strip()
            if s and _JA_RE.search(s):
                refs.add(s)

    data_dir = layout.data_dir

    # Named things that are looked up by name and never shown as dialogue.
    for fname in ("Animations.json", "Tilesets.json", "CommonEvents.json", "Troops.json"):
        for entry in _entries(data_dir / fname):
            add(entry.get("name"))
    system = _load(data_dir / "System.json")
    if isinstance(system, dict):
        for key in ("switches", "variables"):
            for name in system.get(key) or []:
                add(name)

    # Note tags on database entries.
    for fname in DB_FILES:
        for entry in _entries(data_dir / fname):
            _add_note(entry.get("note"), add)

    # Maps: map/event notes, event names, and scripts / plugin commands.
    for f in sorted(data_dir.glob("Map*.json")):
        if not _MAP_FILE_RE.match(f.name):
            continue
        data = _load(f)
        if not isinstance(data, dict):
            continue
        _add_note(data.get("note"), add)
        for ev in data.get("events") or []:
            if not isinstance(ev, dict):
                continue
            add(ev.get("name"))
            _add_note(ev.get("note"), add)
            for page in ev.get("pages") or []:
                _scan_commands(page.get("list"), add)
    for entry in _entries(data_dir / "CommonEvents.json"):
        _scan_commands(entry.get("list"), add)
    for troop in _entries(data_dir / "Troops.json"):
        for page in troop.get("pages") or []:
            _scan_commands(page.get("list"), add)

    # Plugin configuration, except keys that are plainly on-screen wording.
    plugins_js = layout.root / "js" / "plugins.js"
    if plugins_js.exists():
        for plugin in parse_plugins_js(plugins_js):
            if isinstance(plugin, dict) and plugin.get("status") is not False:
                _add_plugin_params(plugin.get("parameters") or {}, "", add)

    # String literals hard-coded in plugin source (comments -- including the
    # long Japanese help text -- stripped first).
    plugin_dir = layout.root / "js" / "plugins"
    if plugin_dir.is_dir():
        for f in sorted(plugin_dir.glob("*.js")):
            try:
                _add_js_literals(f.read_text(encoding="utf-8", errors="replace"), add)
            except OSError:
                continue

    return refs
