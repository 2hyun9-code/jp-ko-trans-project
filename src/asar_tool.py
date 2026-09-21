"""Minimal reader for Electron's .asar archive format.

Format (little-endian, Chromium "Pickle" framing):
  u32  4                       -- constant: size of the payload below (one u32)
  u32  header_pickle_size      -- size in bytes of everything from here through
                                   the end of the (padded) header JSON
  u32  json_len                -- length of the header JSON text, unpadded
  json_len bytes                -- UTF-8 JSON header (file tree: name -> {size, offset} | {files: {...}})
  padding                      -- zero bytes up to the next 4-byte boundary
  <file contents, concatenated, offsets in the header are relative to here>

Only extraction is implemented -- packing isn't needed because Electron falls
back to loading an unpacked `resources/app/` folder automatically when
`resources/app.asar` is absent, so a translated game just needs the archive
extracted (and the original renamed aside), never repacked.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path


def _read_header(data: bytes) -> tuple[dict, int]:
    if len(data) < 16:
        raise ValueError("파일이 너무 작아서 asar 형식이 아닌 것 같습니다.")
    # Two nested Pickle size-prefixes (offsets 0 and 4), then the header
    # pickle's own payload: a u32 json length (offset 12) followed by the
    # JSON bytes themselves starting at offset 16.
    json_len = struct.unpack_from("<I", data, 12)[0]
    json_bytes = data[16:16 + json_len]
    header = json.loads(json_bytes.decode("utf-8"))
    base_offset = 16 + json_len
    base_offset = (base_offset + 3) & ~3  # round up to 4-byte alignment
    return header, base_offset


def _walk(node: dict, prefix: Path, out: list[tuple[Path, int, int, bool]]) -> None:
    """Collects (relative_path, offset, size, is_executable) for every file."""
    files = node.get("files")
    if files is None:
        return
    for name, entry in files.items():
        rel = prefix / name
        if "files" in entry:
            _walk(entry, rel, out)
        elif "link" in entry:
            continue  # symlink entries: rare in game asars, skipped
        else:
            offset = int(entry["offset"])
            size = int(entry["size"])
            out.append((rel, offset, size, bool(entry.get("executable"))))


def extract_asar(asar_path: str | Path, dest_dir: str | Path) -> int:
    """Extracts every file in the archive into dest_dir, preserving the
    directory structure. Returns the number of files written."""
    asar_path = Path(asar_path)
    dest_dir = Path(dest_dir)
    data = asar_path.read_bytes()
    header, base_offset = _read_header(data)

    entries: list[tuple[Path, int, int, bool]] = []
    _walk(header, Path("."), entries)

    dest_dir.mkdir(parents=True, exist_ok=True)
    for rel, offset, size, _executable in entries:
        out_path = dest_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        start = base_offset + offset
        out_path.write_bytes(data[start:start + size])
    return len(entries)
