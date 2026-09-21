"""Harvests translatable Japanese strings out of js/plugins.js.

Plugin parameter values can be arbitrarily nested -- RPG Maker encodes
list/struct-type parameters as JSON *strings* embedded inside the outer
JSON, sometimes more than one level deep. We never modify plugins.js
itself (touching the wrong key could break a plugin outright, e.g. a
color code or a switch ID that happens to look stringy); these strings
just get added to the same translation cache that render_patch.py's
draw-time hook reads from, which is safe because that hook never touches
actual plugin config -- only what gets drawn on screen.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

_HAS_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_PLUGINS_ASSIGN_RE = re.compile(r"\$plugins\s*=\s*(\[.*\])\s*;?\s*$", re.DOTALL)

Callback = Callable[[str], None]


def _try_parse_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _walk_value(value, cb: Callback) -> None:
    if isinstance(value, str):
        nested = None
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            nested = _try_parse_json(value)
        if nested is not None:
            _walk_value(nested, cb)
        elif _HAS_JA_RE.search(value):
            cb(value)
    elif isinstance(value, list):
        for item in value:
            _walk_value(item, cb)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_value(v, cb)


def parse_plugins_js(path: Path) -> list:
    """plugins.js is `var $plugins = [ ...pure JSON array... ];` -- strip
    the JS variable-assignment wrapper and parse the rest as JSON."""
    text = path.read_text(encoding="utf-8")
    m = _PLUGINS_ASSIGN_RE.search(text)
    if not m:
        return []
    data = _try_parse_json(m.group(1))
    return data if isinstance(data, list) else []


def collect_plugin_strings(plugins_path: Path) -> set[str]:
    strings: set[str] = set()
    for plugin in parse_plugins_js(plugins_path):
        if not isinstance(plugin, dict):
            continue
        if plugin.get("status") is False:
            continue  # disabled plugin: never runs, nothing it holds gets drawn
        params = plugin.get("parameters")
        if params:
            _walk_value(params, strings.add)
    return strings
