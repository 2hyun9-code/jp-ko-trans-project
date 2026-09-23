import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asar_tool import extract_asar, read_asar_files  # noqa: E402


def _build_fake_asar(entries: dict[str, bytes]) -> bytes:
    """Hand-builds a minimal, valid .asar buffer (see asar_tool.py's module
    docstring for the format) so extraction can be tested without needing a
    real multi-megabyte Electron archive."""
    file_data = b""
    files_tree: dict = {}
    offset = 0
    for path, content in entries.items():
        parts = path.split("/")
        node = files_tree
        for p in parts[:-1]:
            node = node.setdefault(p, {"files": {}})["files"]
        node[parts[-1]] = {"size": len(content), "offset": str(offset)}
        file_data += content
        offset += len(content)

    header_json = json.dumps({"files": files_tree}).encode("utf-8")
    json_len = len(header_json)
    padded_len = (json_len + 3) & ~3
    padded_json = header_json + b"\x00" * (padded_len - json_len)

    out = struct.pack("<I", 4)               # offset 0 (unused by the reader)
    out += struct.pack("<I", 8 + padded_len)  # offset 4 (unused by the reader)
    out += struct.pack("<I", 4 + padded_len)  # offset 8 (unused by the reader)
    out += struct.pack("<I", json_len)        # offset 12: real json length
    out += padded_json                        # offset 16: json text (+padding)
    out += file_data
    return out


def test_extract_asar_flat_files(tmp_path):
    data = _build_fake_asar({"index.html": b"<html></html>", "package.json": b'{"a":1}'})
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(data)

    dest = tmp_path / "out"
    n = extract_asar(asar_path, dest)

    assert n == 2
    assert (dest / "index.html").read_bytes() == b"<html></html>"
    assert (dest / "package.json").read_bytes() == b'{"a":1}'


def test_extract_asar_nested_directories(tmp_path):
    data = _build_fake_asar({
        "project/data/System.json": b'{"gameTitle":"t"}',
        "project/index.html": b"<html></html>",
        "js/main.js": b"console.log(1)",
    })
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(data)

    dest = tmp_path / "out"
    n = extract_asar(asar_path, dest)

    assert n == 3
    assert (dest / "project" / "data" / "System.json").read_bytes() == b'{"gameTitle":"t"}'
    assert (dest / "js" / "main.js").read_bytes() == b"console.log(1)"


def test_extract_asar_preserves_binary_content(tmp_path):
    binary = bytes(range(256)) * 4
    data = _build_fake_asar({"img/pic.png": binary})
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(data)

    dest = tmp_path / "out"
    extract_asar(asar_path, dest)

    assert (dest / "img" / "pic.png").read_bytes() == binary


def test_extract_asar_rejects_too_small_file(tmp_path):
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(b"tiny")
    with pytest.raises(ValueError):
        extract_asar(asar_path, tmp_path / "out")


def test_unpacked_entries_come_from_the_side_folder(tmp_path):
    data = _build_fake_asar({"a.txt": b"packed"})
    header_len = struct.unpack_from("<I", data, 12)[0]
    header = json.loads(data[16:16 + header_len])
    header["files"]["native.node"] = {"size": 3, "unpacked": True}
    body = json.dumps(header).encode("utf-8")
    padded = body + b"\x00" * (((len(body) + 3) & ~3) - len(body))
    rebuilt = struct.pack("<IIII", 4, 8 + len(padded), 4 + len(padded), len(body)) + padded + b"packed"
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(rebuilt)
    (tmp_path / "app.asar.unpacked").mkdir()
    (tmp_path / "app.asar.unpacked" / "native.node").write_bytes(b"bin")

    n = extract_asar(asar_path, tmp_path / "out")
    assert n == 2
    assert (tmp_path / "out" / "native.node").read_bytes() == b"bin"
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"packed"


def test_entries_escaping_the_destination_are_skipped(tmp_path):
    data = _build_fake_asar({"../evil.txt": b"x", "ok.txt": b"y"})
    asar_path = tmp_path / "app.asar"
    asar_path.write_bytes(data)
    assert extract_asar(asar_path, tmp_path / "out" / "inner") == 1
    assert not (tmp_path / "out" / "evil.txt").exists()
    assert read_asar_files(asar_path, lambda n: True) == {"ok.txt": b"y"}
