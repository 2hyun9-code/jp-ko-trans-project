"""Minimal reader for Electron's .asar archive format.

Format (little-endian, Chromium "Pickle" framing):
  u32  4                       -- constant: size of the payload below (one u32)
  u32  header_pickle_size      -- size in bytes of everything from here through
                                   the end of the (padded) header JSON
  u32  json_len                -- length of the header JSON text, unpadded
  json_len bytes                -- UTF-8 JSON header (file tree: name -> {size, offset} | {files: {...}})
  padding                      -- zero bytes up to the next 4-byte boundary
  <file contents, concatenated, offsets in the header are relative to here>

Entries marked "unpacked" have no offset: Electron keeps them as ordinary
files in `app.asar.unpacked/` next to the archive.

Only extraction is implemented -- packing isn't needed because Electron falls
back to loading an unpacked `resources/app/` folder automatically when
`resources/app.asar` is absent, so a translated game just needs the archive
extracted (and the original renamed aside), never repacked.
"""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Optional


def _read_header(fp: BinaryIO) -> tuple[dict, int]:
    head = fp.read(16)
    if len(head) < 16:
        raise ValueError("파일이 너무 작아서 asar 형식이 아닌 것 같습니다.")
    # Two nested Pickle size-prefixes (offsets 0 and 4), then the header
    # pickle's own payload: a u32 json length (offset 12) followed by the
    # JSON bytes themselves starting at offset 16.
    json_len = struct.unpack_from("<I", head, 12)[0]
    header = json.loads(fp.read(json_len).decode("utf-8"))
    base_offset = 16 + json_len
    base_offset = (base_offset + 3) & ~3  # round up to 4-byte alignment
    return header, base_offset


def _walk(node: dict, prefix: PurePosixPath, out: list[tuple[str, Optional[int], int]]) -> None:
    """Collects (relative_path, offset or None if unpacked, size) for every file."""
    files = node.get("files")
    if files is None:
        return
    for name, entry in files.items():
        rel = prefix / name
        if "files" in entry:
            _walk(entry, rel, out)
        elif "link" in entry:
            continue  # symlink entries: rare in game asars, skipped
        elif entry.get("unpacked"):
            out.append((rel.as_posix(), None, int(entry.get("size", 0))))
        else:
            out.append((rel.as_posix(), int(entry["offset"]), int(entry["size"])))


def _safe_rel(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(parts) and ".." not in parts and not PurePosixPath(rel).is_absolute() \
        and ":" not in parts[0]


def _entries(asar_path: Path) -> tuple[list[tuple[str, Optional[int], int]], int]:
    with open(asar_path, "rb") as fp:
        header, base_offset = _read_header(fp)
    entries: list[tuple[str, Optional[int], int]] = []
    _walk(header, PurePosixPath(), entries)
    return [e for e in entries if _safe_rel(e[0])], base_offset


def read_asar_files(asar_path: str | Path, want: Callable[[str], bool]) -> dict[str, bytes]:
    """{posix relative path: content} for the files `want(path)` accepts,
    read without extracting anything to disk."""
    asar_path = Path(asar_path)
    entries, base_offset = _entries(asar_path)
    unpacked_dir = asar_path.with_name(asar_path.name + ".unpacked")
    out: dict[str, bytes] = {}
    with open(asar_path, "rb") as fp:
        for rel, offset, size in entries:
            if not want(rel):
                continue
            if offset is None:
                try:
                    out[rel] = (unpacked_dir / rel).read_bytes()
                except OSError:
                    continue
            else:
                fp.seek(base_offset + offset)
                out[rel] = fp.read(size)
    return out


def extract_asar(asar_path: str | Path, dest_dir: str | Path) -> int:
    """Extracts every file in the archive into dest_dir, preserving the
    directory structure. Returns the number of files written."""
    asar_path = Path(asar_path)
    dest_dir = Path(dest_dir)
    entries, base_offset = _entries(asar_path)
    unpacked_dir = asar_path.with_name(asar_path.name + ".unpacked")

    dest_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(asar_path, "rb") as fp:
        for rel, offset, size in entries:
            out_path = dest_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if offset is None:
                src = unpacked_dir / rel
                if not src.is_file():
                    continue
                shutil.copyfile(src, out_path)
            else:
                fp.seek(base_offset + offset)
                with open(out_path, "wb") as dst:
                    remaining = size
                    while remaining:
                        chunk = fp.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        dst.write(chunk)
                        remaining -= len(chunk)
            written += 1
    return written
