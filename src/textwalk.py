"""Find and rewrite translatable strings inside an RPG Maker MV/MZ project.

Walks every data/*.json file, visits the handful of places that hold
player-visible text (message boxes, choices, scrolling text, database
names/descriptions/terms), and calls a callback for each string found.
The same walk is reused for extraction (callback just records the text)
and injection (callback looks up a translation and returns it).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable

from engine import ProjectLayout

# Event command codes that carry player-visible text, and which
# parameter index holds the text (or "list" for a list-of-strings param).
# 401/405 lines are handled separately as whole paragraphs (PARAGRAPH_CODES).
TEXT_COMMANDS = {
    101: [4],         # Show Text (header): parameters[4] is the MZ name-box
                       # speaker name, if present (MV/older MZ has no index 4)
    102: ["list0"],   # Show Choices: parameters[0] is a list of strings
    320: [1],         # Change Name
    324: [1],         # Change Nickname
    325: [1],         # Change Profile
    331: [1],         # (some plugins) misc name-like text - harmless if unused
}

# A message is stored as one command per window line (401 = Show Text line,
# 405 = Show Scrolling Text line). Translating each line on its own cuts
# sentences at the Japanese line breaks, so consecutive lines are handed to
# the callback as one "\n"-joined paragraph and split back afterwards. The
# number of commands never changes -- the event structure must stay intact.
PARAGRAPH_CODES = (401, 405)

DB_TEXT_FIELDS = ["name", "description", "message1", "message2", "message3",
                   "message4", "nickname", "profile"]

DB_FILES = ["Actors.json", "Classes.json", "Skills.json", "Items.json",
            "Weapons.json", "Armors.json", "Enemies.json", "States.json"]

Callback = Callable[[str], str]


def fit_lines(text: str, n: int) -> list[str]:
    """Splits a translated paragraph back onto exactly `n` command lines.
    Extra lines are kept together (as "\\n") on the last command -- the
    message window turns them into a new page -- and missing ones become
    empty lines."""
    lines = text.split("\n")
    if len(lines) <= n:
        return lines + [""] * (n - len(lines))
    return lines[:n - 1] + ["\n".join(lines[n - 1:])]


def _walk_paragraph(block: list, cb: Callback) -> None:
    lines = [c["parameters"][0] for c in block]
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1  # trailing blank lines aren't part of the paragraph's text
    if not end:
        return
    paragraph = "\n".join(lines[:end])
    out = cb(paragraph)
    if out == paragraph:
        return
    for cmd, line in zip(block, fit_lines(out, len(block))):
        cmd["parameters"][0] = line


def _is_paragraph_line(cmd: dict, code: int, indent) -> bool:
    params = cmd.get("parameters")
    return (cmd.get("code") == code and cmd.get("indent") == indent
            and bool(params) and isinstance(params[0], str))


def _walk_command_list(commands: list, cb: Callback) -> None:
    i = 0
    while i < len(commands):
        cmd = commands[i]
        code = cmd.get("code")
        if code in PARAGRAPH_CODES and _is_paragraph_line(cmd, code, cmd.get("indent")):
            j = i + 1
            while j < len(commands) and _is_paragraph_line(commands[j], code, cmd.get("indent")):
                j += 1
            _walk_paragraph(commands[i:j], cb)
            i = j
            continue
        i += 1
        spec = TEXT_COMMANDS.get(code)
        if not spec:
            continue
        params = cmd.get("parameters", [])
        for idx in spec:
            if idx == "list0":
                if params and isinstance(params[0], list):
                    params[0] = [cb(s) if isinstance(s, str) and s.strip() else s
                                  for s in params[0]]
            else:
                if idx < len(params) and isinstance(params[idx], str) and params[idx].strip():
                    params[idx] = cb(params[idx])


def _walk_pages(pages: Iterable[dict], cb: Callback) -> None:
    for page in pages:
        cmds = page.get("list")
        if cmds:
            _walk_command_list(cmds, cb)


def _walk_map_file(path: Path, cb: Callback) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events") or []
    changed = False
    for ev in events:
        if not ev:
            continue
        pages = ev.get("pages") or []
        _walk_pages(pages, cb)
        changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return changed


def _walk_common_events(path: Path, cb: Callback) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        if not entry:
            continue
        cmds = entry.get("list")
        if cmds:
            _walk_command_list(cmds, cb)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _walk_troops(path: Path, cb: Callback) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for troop in data:
        if not troop:
            continue
        for page in troop.get("pages") or []:
            cmds = page.get("list")
            if cmds:
                _walk_command_list(cmds, cb)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _walk_database_file(path: Path, cb: Callback) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        if not entry:
            continue
        for field in DB_TEXT_FIELDS:
            val = entry.get(field)
            if isinstance(val, str) and val.strip():
                entry[field] = cb(val)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _walk_mapinfos(path: Path, cb: Callback) -> None:
    """MapInfos.json is a top-level list (unlike MapNNN.json's dict shape),
    each entry holding a map's display name -- shown in the game's menu on
    some games. Excluded from the Map*.json glob on purpose (see
    walk_project), so it needs its own tiny walker."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        if entry and isinstance(entry.get("name"), str) and entry["name"].strip():
            entry["name"] = cb(entry["name"])
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _walk_system(path: Path, cb: Callback) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data.get("gameTitle"), str) and data["gameTitle"].strip():
        data["gameTitle"] = cb(data["gameTitle"])

    for key in ["equipTypes", "skillTypes", "weaponTypes", "armorTypes", "elements"]:
        lst = data.get(key)
        if isinstance(lst, list):
            data[key] = [cb(s) if isinstance(s, str) and s.strip() else s for s in lst]

    terms = data.get("terms")
    if isinstance(terms, dict):
        for key in ["basic", "commands", "params"]:
            lst = terms.get(key)
            if isinstance(lst, list):
                terms[key] = [cb(s) if isinstance(s, str) and s.strip() else s for s in lst]
        messages = terms.get("messages")
        if isinstance(messages, dict):
            for k, v in list(messages.items()):
                if isinstance(v, str) and v.strip():
                    messages[k] = cb(v)

    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def walk_project(layout: ProjectLayout, cb: Callback) -> None:
    """Run cb(text) -> replacement over every translatable string in the project,
    writing results back to the same files (in place)."""
    data_dir = layout.data_dir

    map_file_re = re.compile(r"^Map\d+\.json$", re.IGNORECASE)
    for f in sorted(data_dir.glob("Map*.json")):
        if map_file_re.match(f.name):  # excludes MapInfos.json (a list, not a map)
            _walk_map_file(f, cb)

    mapinfos = data_dir / "MapInfos.json"
    if mapinfos.exists():
        _walk_mapinfos(mapinfos, cb)

    ce = data_dir / "CommonEvents.json"
    if ce.exists():
        _walk_common_events(ce, cb)

    troops = data_dir / "Troops.json"
    if troops.exists():
        _walk_troops(troops, cb)

    for name in DB_FILES:
        f = data_dir / name
        if f.exists():
            _walk_database_file(f, cb)

    system = data_dir / "System.json"
    if system.exists():
        _walk_system(system, cb)


_HAS_JA_RE = re.compile(r"[぀-ヿ一-鿿]")


def _extract_plugin_command_text(line: str) -> str | None:
    """MV "Plugin Command" (event code 356) stores the whole typed command
    line as ONE string: "<CommandName> <arg1> <arg2> ...". Some plugins
    take a free-text argument that itself contains spaces (a HUD label
    with its own escape codes, say), so this can't just grab "the 2nd
    token" -- it drops the leading command-name token and a trailing
    purely-numeric token (a common duration/id arg), and returns what's
    left if it still contains actual Japanese text worth translating.
    Read-only: this never rewrites the command, only harvests text for the
    render-time patch's lookup table (see render_patch.py)."""
    tokens = line.split(" ")
    if len(tokens) < 2:
        return None
    body = tokens[1:]
    if body and body[-1].lstrip("-").isdigit():
        body = body[:-1]
    text = " ".join(body).strip()
    if text and _HAS_JA_RE.search(text):
        return text
    return None


_JA_FRAGMENT_RE = re.compile(r"[぀-ヿ一-鿿ー]{2,}")


def _extract_ja_fragments(text: str) -> list[str]:
    """Some HUD/gauge plugins don't draw the harvested line verbatim -- they
    rebuild it at render time from pieces (label + a live \\V[n] value,
    say), so a whole-string cache lookup for the original line can miss
    even though the label text itself never changes. Pulling out each
    contiguous Japanese run (dropping escape codes, brackets, digits) and
    caching those too gives the render patch's substring-fallback lookup
    something to match against even when the full line never appears
    on screen in one piece."""
    return _JA_FRAGMENT_RE.findall(text)


def _collect_plugin_command_texts_from_commands(commands: list, texts: set[str]) -> None:
    for cmd in commands:
        if cmd.get("code") not in (356, 357):
            continue
        for p in cmd.get("parameters", []):
            if isinstance(p, str):
                found = _extract_plugin_command_text(p)
                if found:
                    texts.add(found)
                    texts.update(_extract_ja_fragments(found))


def collect_plugin_command_texts(layout: ProjectLayout) -> set[str]:
    """Read-only pass over Map*.json/CommonEvents.json/Troops.json for
    Japanese text embedded in "Plugin Command" event lines -- text that
    isn't in any of the fields TEXT_COMMANDS knows about, since the whole
    line is one opaque string whose syntax is entirely up to the plugin."""
    texts: set[str] = set()
    data_dir = layout.data_dir

    map_file_re = re.compile(r"^Map\d+\.json$", re.IGNORECASE)
    for f in sorted(data_dir.glob("Map*.json")):
        if not map_file_re.match(f.name):
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        for ev in data.get("events") or []:
            if not ev:
                continue
            for page in ev.get("pages") or []:
                cmds = page.get("list")
                if cmds:
                    _collect_plugin_command_texts_from_commands(cmds, texts)

    ce = data_dir / "CommonEvents.json"
    if ce.exists():
        for entry in json.loads(ce.read_text(encoding="utf-8")):
            if entry and entry.get("list"):
                _collect_plugin_command_texts_from_commands(entry["list"], texts)

    troops = data_dir / "Troops.json"
    if troops.exists():
        for troop in json.loads(troops.read_text(encoding="utf-8")):
            if not troop:
                continue
            for page in troop.get("pages") or []:
                if page.get("list"):
                    _collect_plugin_command_texts_from_commands(page["list"], texts)

    return texts


def _collect_names_from_commands(commands: list, names: set[str]) -> None:
    for cmd in commands:
        if cmd.get("code") != 101:
            continue
        params = cmd.get("parameters", [])
        if len(params) > 4 and isinstance(params[4], str) and params[4].strip():
            names.add(params[4])


def collect_glossary_names(layout: ProjectLayout) -> set[str]:
    """Read-only pass over every "this string is a proper noun" source --
    database entry names (actors, classes, skills, items, weapons, armors,
    enemies, states), map display names, and MZ name-box speaker names --
    so they can all be translated first and fed back as a consistency
    glossary before the main text pass runs."""
    names: set[str] = set()
    data_dir = layout.data_dir

    for db_file in DB_FILES:
        f = data_dir / db_file
        if not f.exists():
            continue
        for entry in json.loads(f.read_text(encoding="utf-8")):
            if entry and isinstance(entry.get("name"), str) and entry["name"].strip():
                names.add(entry["name"])

    mapinfos = data_dir / "MapInfos.json"
    if mapinfos.exists():
        for entry in json.loads(mapinfos.read_text(encoding="utf-8")):
            if entry and isinstance(entry.get("name"), str) and entry["name"].strip():
                names.add(entry["name"])

    map_file_re = re.compile(r"^Map\d+\.json$", re.IGNORECASE)
    for f in sorted(data_dir.glob("Map*.json")):
        if not map_file_re.match(f.name):
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        for ev in data.get("events") or []:
            if not ev:
                continue
            for page in ev.get("pages") or []:
                cmds = page.get("list")
                if cmds:
                    _collect_names_from_commands(cmds, names)

    ce = data_dir / "CommonEvents.json"
    if ce.exists():
        for entry in json.loads(ce.read_text(encoding="utf-8")):
            if entry and entry.get("list"):
                _collect_names_from_commands(entry["list"], names)

    troops = data_dir / "Troops.json"
    if troops.exists():
        for troop in json.loads(troops.read_text(encoding="utf-8")):
            if not troop:
                continue
            for page in troop.get("pages") or []:
                if page.get("list"):
                    _collect_names_from_commands(page["list"], names)

    return names
