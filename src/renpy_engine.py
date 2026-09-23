"""Ren'Py support: read a Ren'Py game's Japanese text, and make the game
show Korean without modifying any of its own files.

Reading. Text lives in `.rpy` script source, in compiled `.rpyc` files,
or inside `.rpa` archives bundling either. `.rpyc` and the `.rpa` index
are Python pickles, which would run code if loaded the normal way -- so
they are never loaded: `.rpyc` payloads are only *scanned* opcode by
opcode (pickletools), and archive indexes go through an unpickler that
refuses to construct any class at all. A hostile game file can't execute
anything through this module.

Writing. Ren'Py has official hooks for replacing text at display time.
The output game gets three new files in `game/` and nothing else changes:
  jpko_translation.json  source -> Korean
  zzz_jpko_hook.rpy      wires the map into config.say_menu_text_filter
                         (dialogue, menu choices) and config.replace_text
                         (every other displayed string: names, UI, ...),
                         and swaps fonts for one with Hangul
  jpko_korean.ttf        that font (a copy of the one picked in the GUI)
Deleting those three undoes the translation.
"""
from __future__ import annotations

import codecs
import io
import json
import pickle
import pickletools
import re
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

TRANSLATION_FILE = "jpko_translation.json"
HOOK_FILE = "zzz_jpko_hook.rpy"
FONT_FILE = "jpko_korean.ttf"
# Fonts Ren'Py itself falls back to when a game doesn't name its own.
_ENGINE_DEFAULT_FONTS = ("DejaVuSans.ttf", "SourceHanSansLite.ttf")

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_STRING_OPS = {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE",
               "SHORT_BINSTRING", "BINSTRING", "STRING"}
_PY_STRING_RE = re.compile(r'''(?:^|[^\w])_?\(?\s*(?:u|r|ur)?(["'])((?:\\.|(?!\1).)*)\1''')
_CHARACTER_RE = re.compile(r'''Character\(\s*(?:_\(\s*)?(?:u)?(["'])((?:\\.|(?!\1).)*)\1''')
_FONT_NAME_RE = re.compile(r"[\w ./\\-]+\.(?:ttf|otf|ttc)", re.IGNORECASE)
_CODE_SHAPE_RE = re.compile(r"[=()]|^\s*(?:def|if|for|import|return)\b", re.M)
_KEYWORDS = {"voice", "play", "queue", "stop", "jump", "call", "image", "show", "hide",
             "scene", "define", "default", "style", "transform", "screen", "label", "init",
             "python", "with", "window", "pause", "return", "camera", "at", "translate",
             "old", "new", "text", "add", "use", "key", "timer", "renpy"}
# Say / menu line in .rpy source: [who [attributes...]] "what" [...]
_SAY_RE = re.compile(
    r'''^\s*(?:(?P<who>[A-Za-z_][\w.]*)(?:\s+[\w@-]+)*\s+|"(?P<whostr>(?:\\.|[^"\\])*)"\s+)?'''
    r'''"(?P<what>(?:\\.|[^"\\])*)"(?P<rest>.*)$''')


@dataclass
class RenpyText:
    texts: list[str] = field(default_factory=list)   # everything to translate, in script order
    names: list[str] = field(default_factory=list)   # character display names (glossary)
    fonts: set[str] = field(default_factory=set)     # font names the game refers to
    sources: dict[str, int] = field(default_factory=dict)  # "rpy"/"rpyc"/"rpa" -> files read

    def add(self, text: str, seen: set) -> None:
        if text and _JA_RE.search(text) and text not in seen:
            seen.add(text)
            self.texts.append(text)


# --------------------------------------------------------------------------
# .rpy source
# --------------------------------------------------------------------------
def renpy_unescape(literal: str) -> str:
    """What Ren'Py's own lexer does to a string literal in script: runs of
    whitespace collapse to one space, then backslash escapes resolve. The
    result is exactly the string say_menu_text_filter will receive."""
    s = re.sub(r"[ \t\n\r]+", " ", literal)
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append({"n": "\n", '"': '"', "'": "'", "\\": "\\", " ": " "}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scan_rpy(source: str, result: RenpyText, seen: set) -> None:
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for m in _CHARACTER_RE.finditer(line):
            name = renpy_unescape(m.group(2))
            if _JA_RE.search(name) and name not in result.names:
                result.names.append(name)
        for m in _FONT_NAME_RE.finditer(line):
            result.fonts.add(m.group(0).strip().replace("\\", "/"))
        say = _SAY_RE.match(raw)
        if say and (say.group("who") or "") not in _KEYWORDS:
            if say.group("whostr"):
                result.add(renpy_unescape(say.group("whostr")), seen)
            result.add(renpy_unescape(say.group("what")), seen)
            rest = say.group("rest")
        else:
            rest = raw
        # Any other string literal on the line: screen text, textbuttons,
        # _() strings, notify calls... displayed ones get replace_text'd.
        for m in _PY_STRING_RE.finditer(rest):
            result.add(renpy_unescape(m.group(2)), seen)


# --------------------------------------------------------------------------
# .rpyc compiled script (scanned, never unpickled)
# --------------------------------------------------------------------------
def rpyc_payload(data: bytes) -> Optional[bytes]:
    """The zlib-compressed pickle inside a .rpyc: slot 1 of the "RENPY RPC2"
    container, or (older Ren'Py) the whole file."""
    try:
        if data.startswith(b"RENPY RPC2"):
            pos = 10
            while pos + 12 <= len(data):
                slot, start, length = struct.unpack("<III", data[pos:pos + 12])
                if slot == 0:
                    break
                if slot == 1:
                    return zlib.decompress(data[start:start + length])
                pos += 12
            return None
        return zlib.decompress(data)
    except (zlib.error, struct.error):
        return None


_MEMO_PUT_OPS = {"PUT", "BINPUT", "LONG_BINPUT"}
_MEMO_GET_OPS = {"GET", "BINGET", "LONG_BINGET"}
_NO_VALUE_OPS = {"FRAME", "PROTO", "STOP"}


def _pickle_strings(payload: bytes) -> list[Optional[str]]:
    """Every value the pickle would push, in order: the string for string
    opcodes, None for anything else. Nothing is constructed or executed.

    Pickle writes a repeated string once and refers back to it through the
    memo (BINGET) afterwards -- the 'what' key of every dialogue node after
    the first arrives that way -- so memo references are resolved too."""
    tokens: list[Optional[str]] = []
    memo: dict[int, Optional[str]] = {}
    last: Optional[str] = None
    try:
        for op, arg, _pos in pickletools.genops(payload):
            name = op.name
            if name in _STRING_OPS:
                last = arg.decode("utf-8", errors="replace") if isinstance(arg, bytes) else arg
                tokens.append(last)
            elif name == "MEMOIZE":
                memo[len(memo)] = last
            elif name in _MEMO_PUT_OPS:
                memo[int(arg)] = last
            elif name in _MEMO_GET_OPS:
                last = memo.get(int(arg))
                tokens.append(last)
            elif name not in _NO_VALUE_OPS:
                last = None
                tokens.append(None)
    except Exception:  # noqa: BLE001 -- a truncated/odd pickle: keep what was read
        pass
    return tokens


def _scan_rpyc(payload: bytes, result: RenpyText, seen: set) -> None:
    tokens = _pickle_strings(payload)
    for i, tok in enumerate(tokens):
        if tok is None:
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok == "what" and nxt is not None:
            result.add(nxt, seen)                           # Say node: 'what' -> text
        elif nxt == "True" and _JA_RE.search(tok):
            result.add(tok, seen)                           # Menu item: (label, "True", block)
        elif _JA_RE.search(tok):
            if '"' in tok or "'" in tok:                    # Python/screen source code
                for m in _CHARACTER_RE.finditer(tok):
                    name = renpy_unescape(m.group(2))
                    if name not in result.names:
                        result.names.append(name)
                for m in _PY_STRING_RE.finditer(tok):
                    result.add(renpy_unescape(m.group(2)), seen)
            elif not ("\n" in tok and _CODE_SHAPE_RE.search(tok)):
                # (a multi-line code block whose only Japanese is a comment is skipped)
                result.add(tok, seen)
        for m in _FONT_NAME_RE.finditer(tok):
            result.fonts.add(m.group(0).strip().replace("\\", "/"))


# --------------------------------------------------------------------------
# .rpa archives
# --------------------------------------------------------------------------
def _pickled_bytes(*args) -> bytes:
    """`bytes()` as Python 3 pickles an empty bytes value at protocol 2 --
    and nothing else (no bytes(10**12) allocation tricks)."""
    if not args:
        return b""
    if isinstance(args[0], (bytes, bytearray)):
        return bytes(args[0])
    raise pickle.UnpicklingError("unexpected bytes() arguments")


def _pickled_encode(text, encoding="utf-8") -> bytes:
    """`_codecs.encode(str, "latin1")`, how Python 3 pickles non-empty bytes."""
    if not isinstance(text, str) or encoding not in ("latin1", "latin-1"):
        raise pickle.UnpicklingError("unexpected _codecs.encode arguments")
    return codecs.encode(text, encoding)


class _NoClassesUnpickler(pickle.Unpickler):
    """Constructs plain values only. The only callables allowed are the two
    ways Python 3 writes a `bytes` value at pickle protocol 2 -- Ren'Py 8
    archives store each entry's prefix that way -- each narrowed to exactly
    that use."""

    _ALLOWED = {("_codecs", "encode"): _pickled_encode,
                ("__builtin__", "bytes"): _pickled_bytes,
                ("builtins", "bytes"): _pickled_bytes}

    def find_class(self, module, name):
        try:
            return self._ALLOWED[module, name]
        except KeyError:
            raise pickle.UnpicklingError(f"refusing to load {module}.{name}") from None


def rpa_entries(path: Path) -> dict[str, tuple[int, int, bytes]]:
    """{name: (offset, length, prefix)} for an RPA-2.0/3.0 archive; empty if
    the file isn't one this can read."""
    with open(path, "rb") as fh:
        header = fh.readline()
        parts = header.split()
        if len(parts) < 2 or parts[0] not in (b"RPA-3.0", b"RPA-2.0"):
            return {}
        offset = int(parts[1], 16)
        key = int(parts[2], 16) if parts[0] == b"RPA-3.0" and len(parts) > 2 else 0
        fh.seek(offset)
        try:
            raw = _NoClassesUnpickler(io.BytesIO(zlib.decompress(fh.read())),
                                      encoding="bytes").load()
        except Exception:  # noqa: BLE001
            return {}
    entries = {}
    for name, chunks in (raw.items() if isinstance(raw, dict) else []):
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        if not chunks:
            continue
        chunk = chunks[0]
        off, length = chunk[0] ^ key, chunk[1] ^ key
        prefix = chunk[2] if len(chunk) > 2 else b""
        if isinstance(prefix, str):
            prefix = prefix.encode("latin-1")
        entries[name] = (off, length, prefix)
    return entries


def rpa_read(path: Path, entry: tuple[int, int, bytes]) -> bytes:
    off, length, prefix = entry
    with open(path, "rb") as fh:
        fh.seek(off)
        return prefix + fh.read(length - len(prefix))


# --------------------------------------------------------------------------
# Whole game
# --------------------------------------------------------------------------
def _script_members(game_dir: Path) -> Iterable[tuple[str, str, bytes]]:
    """(kind, name, bytes) for every script in the game: loose files first,
    then archive members. A .rpyc is preferred over its .rpy (it holds the
    exact runtime strings); the .rpy is used only when no .rpyc exists."""
    def wanted(name: str) -> bool:
        # tl/<language>/ holds the game's own translations into other
        # languages (Chinese would even pass the kanji check) -- not source text.
        return (name.endswith((".rpy", ".rpyc")) and not name.startswith("tl/")
                and Path(name).stem != Path(HOOK_FILE).stem)

    loose: dict[str, Path] = {}
    for f in sorted(game_dir.rglob("*.rpy*")):
        rel = f.relative_to(game_dir).as_posix()
        if wanted(rel):
            loose[rel] = f
    archived: dict[str, tuple[Path, tuple]] = {}
    for rpa in sorted(game_dir.rglob("*.rpa")):
        for name, entry in rpa_entries(rpa).items():
            name = name.removeprefix("game/")
            if wanted(name):
                archived.setdefault(name, (rpa, entry))

    def pick(names: set[str]):
        for name in sorted(names):
            if name.endswith(".rpy") and (name + "c") in names:
                continue
            yield name

    all_names = set(loose) | set(archived)
    for name in pick(all_names):
        if name in loose:
            yield ("rpyc" if name.endswith("c") else "rpy"), name, loose[name].read_bytes()
        else:
            rpa, entry = archived[name]
            yield "rpa", name, rpa_read(rpa, entry)


def extract(game_dir: Path) -> RenpyText:
    result = RenpyText()
    seen: set[str] = set()
    for kind, name, data in _script_members(game_dir):
        result.sources[kind] = result.sources.get(kind, 0) + 1
        if name.endswith(".rpyc"):
            payload = rpyc_payload(data)
            if payload:
                _scan_rpyc(payload, result, seen)
        else:
            _scan_rpy(data.decode("utf-8-sig", errors="replace"), result, seen)
    for f in game_dir.rglob("*"):
        if f.suffix.lower() in (".ttf", ".otf", ".ttc") and f.name != FONT_FILE:
            result.fonts.add(f.relative_to(game_dir).as_posix())
            result.fonts.add(f.name)
    for rpa in game_dir.rglob("*.rpa"):
        for name in rpa_entries(rpa):
            if name.lower().endswith((".ttf", ".otf", ".ttc")):
                result.fonts.add(name.removeprefix("game/"))
    result.fonts.update(_ENGINE_DEFAULT_FONTS)
    return result


_HOOK_TEMPLATE = '''\
# Generated by jp-ko-trans-project -- regenerated on every run, don't edit.
# Adds a Korean translation without changing any of the game's own files.
# To undo: delete this file, jpko_translation.json and jpko_korean.ttf.
init 999 python:
    import json as _jpko_json

    def _jpko_load():
        try:
            return _jpko_json.loads(renpy.file("jpko_translation.json").read().decode("utf-8"))
        except Exception:
            return {}

    _jpko_map = _jpko_load()

    def _jpko_wrap(previous):
        def _jpko_filter(s):
            try:
                s = _jpko_map.get(s, s)
            except TypeError:
                pass
            return previous(s) if previous is not None else s
        return _jpko_filter

    config.say_menu_text_filter = _jpko_wrap(config.say_menu_text_filter)
    config.replace_text = _jpko_wrap(config.replace_text)

    if renpy.loadable("jpko_korean.ttf"):
        for _jpko_font in __FONTS__:
            for _jpko_bold in (False, True):
                for _jpko_italic in (False, True):
                    config.font_replacement_map[_jpko_font, _jpko_bold, _jpko_italic] = (
                        "jpko_korean.ttf", _jpko_bold, _jpko_italic)
        style.default.font = "jpko_korean.ttf"
'''


def write_translation(game_dir: Path, translations: dict[str, str]) -> Path:
    path = game_dir / TRANSLATION_FILE
    path.write_text(json.dumps(translations, ensure_ascii=False), encoding="utf-8")
    return path


def apply(game_dir: Path, translations: dict[str, str], font_path: Optional[str],
          fonts: set[str]) -> list[Path]:
    """Writes the three hook files into the (copied) game's `game/` folder."""
    written = [write_translation(game_dir, translations)]
    hook = game_dir / HOOK_FILE
    hook.write_text(_HOOK_TEMPLATE.replace("__FONTS__", json.dumps(sorted(fonts), ensure_ascii=False)),
                    encoding="utf-8")
    written.append(hook)
    stale = game_dir / (HOOK_FILE + "c")
    if stale.exists():
        stale.unlink()  # force Ren'Py to recompile the regenerated hook
    if font_path:
        dst = game_dir / FONT_FILE
        shutil.copyfile(font_path, dst)
        written.append(dst)
    return written
