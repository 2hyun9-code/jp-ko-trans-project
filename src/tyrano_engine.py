"""TyranoScript (.ks scenario) support.

A .ks file is line oriented:

    ; comment                      *label|save title
    #speaker  /  #chara_key:face   @tag attr=value
    Text with inline [l][r] tags and [emb exp="f.name"] values[p]
    [iscript] ... JavaScript ... [endscript]

Only text lines, speaker lines and a short list of display attributes
(choice buttons, on-screen text, character display names, ...) are
translated; every tag, label, comment and script block is left exactly as
it was. Speaker lines that name a character registered with
[chara_new name=...] are lookup keys, not display text, and are kept too --
the character's `jname` is what gets translated instead.

The game can live in a plain folder (resources/app, package.nw/, ...), in a
zip -- a `package.nw` file or a zip appended to the NW.js exe -- or in an
Electron app.asar. Folders and zips are patched in place in the output
copy; an asar is extracted by the pipeline first, so it only needs reading
here (for the glossary window, which never touches the game).

The untouched scenario files are kept in the output folder (ORIGINAL_DIR),
so every later apply -- from the review window, say -- regenerates the
translated files from the originals instead of editing translations.
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from asar_tool import _entries as asar_entries, read_asar_files

SCENARIO_PREFIX = "data/scenario/"
ORIGINAL_DIR = "_jpko_original"

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
TAG_RE = re.compile(r"""\[(?:[^\]"'\n]|"[^"\n]*"|'[^'\n]*')*\]""")
_TAG_NAME_RE = re.compile(r"\s*([A-Za-z_][\w-]*)")
_ATTR_RE = re.compile(r"""([A-Za-z_][\w-]*)\s*=\s*("[^"]*"|'[^']*'|[^\s\]"']+)""")
_RUBY_RE = re.compile(r"""\[ruby\s(?:[^\]"'\n]|"[^"\n]*"|'[^'\n]*')*\]""", re.IGNORECASE)

# tag -> attributes whose value is shown on screen
DISPLAY_ATTRS = {
    "glink": {"text"},
    "ptext": {"text"},
    "mtext": {"text"},
    "chara_new": {"jname"},
    "dialog": {"text", "label_ok", "label_cancel"},
    "title": {"name"},
    "button": {"hint"},
    "clickable": {"hint"},
}
_NAME_ATTRS = {("chara_new", "jname")}
_BLOCK_END = {"iscript": "endscript", "html": "endhtml"}
_LINE_START_SWAP = {";": "；", "*": "＊", "#": "＃", "@": "＠", "&": "＆"}
_WS = " \t　"   # TyranoScript trims full-width spaces too


# --------------------------------------------------------------------------
# Line-level parsing
# --------------------------------------------------------------------------
Translate = Callable[[str, str], str]   # (source, kind: "text"|"name") -> result


def tag_name(tag: str) -> str:
    body = tag[1:-1] if tag.startswith("[") else tag.lstrip("@")
    m = _TAG_NAME_RE.match(body)
    return m.group(1).lower() if m else ""


def _unquote(raw: str) -> tuple[str, str]:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1], raw[0]
    return raw, ""


def _quote(value: str, quote: str) -> str:
    value = value.replace("\n", " ").replace("\r", "")
    q = quote or '"'
    value = value.replace(q, "”" if q == '"' else "’")
    return f"{q}{value}{q}"


def _display_values(tag: str):
    """(attr, value) pairs of a tag that are shown on screen and worth
    translating (Japanese, not an `&expression`)."""
    attrs = DISPLAY_ATTRS.get(tag_name(tag))
    if not attrs:
        return
    for m in _ATTR_RE.finditer(tag):
        attr = m.group(1).lower()
        value, _ = _unquote(m.group(2))
        if attr in attrs and not value.startswith("&") and _JA_RE.search(value):
            yield attr, value


def _translate_tag(tag: str, fn: Translate) -> str:
    name = tag_name(tag)
    attrs = DISPLAY_ATTRS.get(name)
    if not attrs:
        return tag

    def repl(m: re.Match) -> str:
        attr = m.group(1).lower()
        value, quote = _unquote(m.group(2))
        if attr not in attrs or value.startswith("&") or not _JA_RE.search(value):
            return m.group(0)
        kind = "name" if (name, attr) in _NAME_ATTRS else "text"
        new = fn(value, kind)
        if new == value:
            return m.group(0)
        return m.group(0)[:m.start(2) - m.start(0)] + _quote(new, quote)

    return _ATTR_RE.sub(repl, tag)


def _sanitize_text(text: str) -> str:
    return text.replace("\r", "").replace("\n", " ").replace("[", "［").replace("]", "］")


def _rebuild(unit: str, new: str) -> Optional[str]:
    """`new` with the tags of `unit` in the same order and nothing else that
    the parser would read as a tag; None if a tag went missing."""
    tags = [m.group(0) for m in TAG_RE.finditer(unit)]
    out, pos = [], 0
    for t in tags:
        i = new.find(t, pos)
        if i < 0:
            return None
        out.append(_sanitize_text(new[pos:i]))
        out.append(t)
        pos = i + len(t)
    out.append(_sanitize_text(new[pos:]))
    return "".join(out)


def _split_tags(s: str) -> list[tuple[bool, str]]:
    """[(is_tag, piece), ...]"""
    parts, pos = [], 0
    for m in TAG_RE.finditer(s):
        if m.start() > pos:
            parts.append((False, s[pos:m.start()]))
        parts.append((True, m.group(0)))
        pos = m.end()
    if pos < len(s):
        parts.append((False, s[pos:]))
    return parts


def _translate_text_line(body: str, fn: Translate, at_line_start: bool) -> str:
    parts = _split_tags(body)
    text_idx = [i for i, (is_tag, p) in enumerate(parts) if not is_tag and p.strip(_WS)]
    if not text_idx:
        return "".join(_translate_tag(p, fn) if is_tag else p for is_tag, p in parts)
    first, last = text_idx[0], text_idx[-1]
    lead = "".join(_translate_tag(p, fn) if t else p for t, p in parts[:first])
    tail = "".join(_translate_tag(p, fn) if t else p for t, p in parts[last + 1:])
    middle_parts = parts[first:last + 1]
    middle = "".join(p for _, p in middle_parts)

    if any(t and any(True for _ in _display_values(p)) for t, p in middle_parts):
        # A choice button or similar sits between text runs: translate each
        # run on its own rather than handing the model a tag with prose inside.
        out = []
        for t, p in middle_parts:
            if t:
                out.append(_translate_tag(p, fn))
            elif _JA_RE.search(p):
                new = fn(p, "text")
                out.append(p if new == p else _sanitize_text(new))
            else:
                out.append(p)
        new_middle = "".join(out)
    else:
        unit = _RUBY_RE.sub("", middle)
        new_middle = middle
        if _JA_RE.search(unit):
            new = fn(unit, "text")
            if new != unit:
                rebuilt = _rebuild(unit, new)
                if rebuilt is not None and rebuilt.strip(_WS):
                    new_middle = rebuilt

    if at_line_start and not lead.strip(_WS) and new_middle is not middle:
        stripped = new_middle.lstrip(_WS)
        if stripped[:1] in _LINE_START_SWAP or stripped.startswith("/*"):
            head = stripped[:1]
            new_middle = (new_middle[:len(new_middle) - len(stripped)]
                          + _LINE_START_SWAP.get(head, "／") + stripped[1:])
    return lead + new_middle + tail


def transform_ks(text: str, fn: Translate, chara_keys: frozenset[str] = frozenset()) -> str:
    """Runs `fn` over every translatable piece of a .ks file and returns the
    file with the results put back. With an identity `fn` the output is the
    input, byte for byte."""
    out = []
    block_end: Optional[str] = None   # "endscript"/"endhtml" while inside one
    in_comment = False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        s = body.lstrip(_WS)
        indent = body[:len(body) - len(s)]

        if in_comment:
            if "*/" in s:
                in_comment = False
            out.append(line)
            continue
        if block_end:
            first = TAG_RE.match(s)
            if (first and tag_name(first.group(0)) == block_end) or \
                    (s.startswith("@") and tag_name(s) == block_end):
                block_end = None
            out.append(line)
            continue
        if not s or s.startswith(";") or s.startswith("*"):
            out.append(line)
            continue
        if s.startswith("/*"):
            in_comment = "*/" not in s[2:]
            out.append(line)
            continue
        if s.startswith("@"):
            name = tag_name(s)
            if name in _BLOCK_END:
                block_end = _BLOCK_END[name]
                out.append(line)
                continue
            out.append(indent + _translate_tag(s, fn) + eol)
            continue
        if s.startswith("#"):
            speaker, sep, face = s[1:].partition(":")
            key = speaker.strip(_WS)
            if key and key not in chara_keys and _JA_RE.search(key):
                new = fn(key, "name")
                if new != key:
                    new = _sanitize_text(new).replace(":", "：").strip()
                    if new:
                        out.append(f"{indent}#{new}{sep}{face}{eol}")
                        continue
            out.append(line)
            continue
        if s.startswith("&"):
            out.append(line)
            continue

        first = TAG_RE.match(s)
        if first and tag_name(first.group(0)) in _BLOCK_END:
            end = _BLOCK_END[tag_name(first.group(0))]
            # "[iscript]" and "[endscript]" on one line is a one-liner.
            if not any(tag_name(m.group(0)) == end for m in TAG_RE.finditer(s)):
                block_end = end
            out.append(line)
            continue
        out.append(indent + _translate_text_line(s, fn, at_line_start=True) + eol)
    return "".join(out)


def chara_keys_of(texts: list[str]) -> frozenset[str]:
    """Names registered with [chara_new name=...]: `#name` lines that use
    one of these refer to the character, they aren't display text."""
    keys = set()
    for text in texts:
        for m in TAG_RE.finditer(text):
            if tag_name(m.group(0)) == "chara_new":
                for a in _ATTR_RE.finditer(m.group(0)):
                    if a.group(1).lower() == "name":
                        keys.add(_unquote(a.group(2))[0])
        for line in text.splitlines():
            s = line.lstrip(_WS)
            if s.startswith("@") and tag_name(s) == "chara_new":
                for a in _ATTR_RE.finditer(s):
                    if a.group(1).lower() == "name":
                        keys.add(_unquote(a.group(2))[0])
    return frozenset(keys)


# --------------------------------------------------------------------------
# File encoding
# --------------------------------------------------------------------------
@dataclass
class _Decoded:
    text: str
    encoding: str
    bom: bytes


def _decode(raw: bytes) -> _Decoded:
    if raw.startswith(b"\xef\xbb\xbf"):
        return _Decoded(raw[3:].decode("utf-8", errors="surrogateescape"), "utf-8", raw[:3])
    try:
        return _Decoded(raw.decode("utf-8"), "utf-8", b"")
    except UnicodeDecodeError:
        pass
    try:
        return _Decoded(raw.decode("cp932"), "cp932", b"")
    except UnicodeDecodeError:
        return _Decoded(raw.decode("utf-8", errors="surrogateescape"), "utf-8", b"")


def _encode(d: _Decoded, text: str) -> bytes:
    if d.encoding == "cp932":
        # Hangul doesn't exist in Shift_JIS -- a translated file has to
        # become UTF-8 (TyranoScript reads UTF-8 by default).
        try:
            return text.encode("cp932")
        except UnicodeEncodeError:
            return text.encode("utf-8")
    return d.bom + text.encode("utf-8", errors="surrogateescape")


# --------------------------------------------------------------------------
# Scan / render over a whole scenario set
# --------------------------------------------------------------------------
@dataclass
class TyranoText:
    texts: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


def scan(files: dict[str, bytes]) -> TyranoText:
    decoded = {rel: _decode(raw).text for rel, raw in sorted(files.items())}
    keys = chara_keys_of(list(decoded.values()))
    result = TyranoText()
    seen: set[str] = set()
    seen_names: set[str] = set()

    def collect(src: str, kind: str) -> str:
        if src not in seen:
            seen.add(src)
            result.texts.append(src)
        if kind == "name" and src not in seen_names:
            seen_names.add(src)
            result.names.append(src)
        return src

    for text in decoded.values():
        transform_ks(text, collect, keys)
    return result


def render(files: dict[str, bytes], translations: dict[str, str]) -> dict[str, bytes]:
    """The translated version of every file that changes."""
    decoded = {rel: _decode(raw) for rel, raw in files.items()}
    keys = chara_keys_of([d.text for d in decoded.values()])

    def lookup(src: str, _kind: str) -> str:
        return translations.get(src, src)

    out = {}
    for rel, d in decoded.items():
        new = transform_ks(d.text, lookup, keys)
        if new != d.text:
            out[rel] = _encode(d, new)
    return out


# --------------------------------------------------------------------------
# Where the game lives
# --------------------------------------------------------------------------
_SKIP_DIRS = {"node_modules", ".git", "tyrano", "locales", "swiftshader", ORIGINAL_DIR}


def _is_tyrano_base(d: Path) -> bool:
    return (d / "data" / "scenario").is_dir() and (
        (d / "tyrano").is_dir() or (d / "data" / "system" / "Config.tjs").is_file()
        or (d / "data" / "scenario" / "first.ks").is_file())


def _zip_prefix(names: list[str]) -> Optional[str]:
    """Path inside a zip to the folder holding data/scenario/ (shortest)."""
    best = None
    for n in names:
        i = n.find(SCENARIO_PREFIX)
        if i < 0 or not n.lower().endswith(".ks") or (i and n[i - 1] != "/"):
            continue
        if best is None or i < len(best):
            best = n[:i]
    return best


@dataclass
class Container:
    kind: str            # "dir" | "zip" | "asar"
    path: Path           # dir: game base folder; zip/asar: the archive (or exe) file
    prefix: str = ""     # zip/asar: path inside the archive to the base folder

    def describe(self) -> str:
        return {"dir": "폴더", "zip": "압축(NW.js)", "asar": "Electron asar"}[self.kind] \
            + f": {self.path.name}"

    def read_scenarios(self) -> dict[str, bytes]:
        """{path relative to data/scenario/ (posix): raw bytes} of every .ks."""
        if self.kind == "dir":
            root = self.path / "data" / "scenario"
            return {p.relative_to(root).as_posix(): p.read_bytes()
                    for p in sorted(root.rglob("*.ks")) if p.is_file()}
        head = self.prefix + SCENARIO_PREFIX
        if self.kind == "asar":
            raw = read_asar_files(self.path, lambda n: n.startswith(head) and n.lower().endswith(".ks"))
            return {n[len(head):]: b for n, b in raw.items()}
        with zipfile.ZipFile(self.path) as zf:
            return {i.filename[len(head):]: zf.read(i) for i in zf.infolist()
                    if i.filename.startswith(head) and i.filename.lower().endswith(".ks")
                    and not i.is_dir()}

    def write_scenarios(self, changes: dict[str, bytes]) -> None:
        if not changes:
            return
        if self.kind == "dir":
            root = self.path / "data" / "scenario"
            for rel, data in changes.items():
                (root / rel).write_bytes(data)
            return
        if self.kind == "zip":
            head = self.prefix + SCENARIO_PREFIX
            _rewrite_zip(self.path, {head + rel: data for rel, data in changes.items()})
            return
        raise RuntimeError("asar 안의 파일은 직접 고칠 수 없습니다 (먼저 압축을 풀어야 해요).")


def _rewrite_zip(path: Path, replace: dict[str, bytes]) -> None:
    """Rewrites a zip -- or a zip appended to an exe, keeping the exe part --
    with some members replaced. Written to a temp file, then swapped in."""
    tmp = path.with_name(path.name + ".jpko_tmp")
    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        stub_len = min((i.header_offset for i in infos), default=0)
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            remaining = stub_len
            while remaining:
                chunk = src.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)
            with zipfile.ZipFile(dst, "w") as zout:
                for info in infos:
                    new = zipfile.ZipInfo(info.filename, info.date_time)
                    new.compress_type = info.compress_type
                    new.external_attr = info.external_attr
                    new.create_system = info.create_system
                    new.comment = info.comment
                    if info.filename in replace:
                        zout.writestr(new, replace[info.filename])
                    elif info.is_dir():
                        zout.writestr(new, b"")
                    else:
                        new.file_size = info.file_size
                        with zin.open(info) as r, zout.open(new, "w", force_zip64=info.file_size > 0x7FFFFFFF) as w:
                            shutil.copyfileobj(r, w, 1 << 20)
    os.replace(tmp, path)


def _zip_container(path: Path) -> Optional[Container]:
    try:
        if not zipfile.is_zipfile(path):
            return None
        with zipfile.ZipFile(path) as zf:
            prefix = _zip_prefix(zf.namelist())
    except (OSError, zipfile.BadZipFile, ValueError):
        return None
    return Container("zip", path, prefix) if prefix is not None else None


def find_container(root: Path, include_asar: bool = False, max_depth: int = 3) -> Optional[Container]:
    """A TyranoScript game under `root`: a plain folder (searched a few
    levels down: resources/app, package.nw/, www/, ...), a `package.nw` zip,
    or an NW.js exe with the game zipped onto its end. With `include_asar`
    also looks inside resources/app.asar (read only)."""
    root = Path(root)
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        cur, depth = queue.popleft()
        if _is_tyrano_base(cur):
            return Container("dir", cur)
        if depth >= max_depth:
            continue
        try:
            subdirs = [p for p in cur.iterdir() if p.is_dir() and p.name not in _SKIP_DIRS]
        except OSError:
            continue
        queue.extend((sub, depth + 1) for sub in subdirs)

    for folder in [root] + _subdirs(root):
        nw = folder / "package.nw"
        if nw.is_file() and (c := _zip_container(nw)):
            return c
        try:
            exes = sorted(folder.glob("*.exe"))
        except OSError:
            exes = []
        for exe in exes:
            if c := _zip_container(exe):
                return c
        if include_asar:
            asar = folder / "resources" / "app.asar"
            if asar.is_file():
                try:
                    names = [e[0] for e in asar_entries(asar)[0]]
                except (OSError, ValueError, KeyError):
                    continue
                prefix = _zip_prefix(names)
                if prefix is not None:
                    return Container("asar", asar, prefix)
    return None


def _subdirs(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name not in _SKIP_DIRS)
    except OSError:
        return []


# --------------------------------------------------------------------------
# Output folder bookkeeping
# --------------------------------------------------------------------------
def save_originals(game_root: Path, files: dict[str, bytes]) -> Path:
    dest = game_root / ORIGINAL_DIR
    for rel, data in files.items():
        p = dest / PurePosixPath(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return dest


def load_originals(game_root: Path) -> dict[str, bytes]:
    src = game_root / ORIGINAL_DIR
    return {p.relative_to(src).as_posix(): p.read_bytes()
            for p in sorted(src.rglob("*.ks")) if p.is_file()}


def container_meta(game_root: Path, c: Container) -> dict:
    return {"kind": c.kind, "path": c.path.relative_to(game_root).as_posix() or ".",
            "prefix": c.prefix}


def container_from_meta(game_root: Path, meta: dict) -> Container:
    return Container(meta["kind"], game_root / meta["path"], meta.get("prefix", ""))


def apply(game_root: Path, container: Container, translations: dict[str, str]) -> int:
    """Regenerates the translated scenario files from the kept originals.
    Files whose translation disappeared are restored too. Returns how many
    files were written."""
    originals = load_originals(game_root)
    rendered = render(originals, translations)
    current = container.read_scenarios()
    changes = {}
    for rel, orig in originals.items():
        want = rendered.get(rel, orig)
        if current.get(rel) != want:
            changes[rel] = want
    container.write_scenarios(changes)
    return len(changes)
