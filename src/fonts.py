"""Swap the in-game font for a Korean-capable one.

RPG Maker MV/MZ loads its UI font ("GameFont") from files in the fonts/
folder. Which files and which CSS declares them varies by game -- some
use css/gamefont.css with mplus-1m-regular.ttf + .woff, others (seen in
the wild) ship only .woff files and a differently-named css/game.css.

Rather than guessing filenames or rewriting CSS (fragile -- breaks if the
CSS file has an unexpected name, as happened here), this keeps every
existing font file's name and extension untouched and just replaces its
*bytes* with the Korean font converted to that same container format:
.ttf/.otf get the raw font data (browsers sniff the actual binary format,
not the extension), .woff/.woff2 get a properly repackaged WOFF/WOFF2
(those ARE format-checked by the browser, so raw TTF bytes under a .woff
name fails to load). Nothing gets deleted and no CSS/JS is touched, so
whatever the game's own loader expects continues to find a file there.
"""
from __future__ import annotations

import io
from pathlib import Path

from fontTools.ttLib import TTFont

from engine import ProjectLayout

_RAW_EXTS = {".ttf", ".otf"}
_WOFF_FLAVORS = {".woff": "woff", ".woff2": "woff2"}


def _convert(ttf_bytes: bytes, flavor: str) -> bytes:
    font = TTFont(io.BytesIO(ttf_bytes))
    font.flavor = flavor
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def swap_font(layout: ProjectLayout, korean_font_path: str) -> list[str]:
    src = Path(korean_font_path)
    if not src.exists():
        raise FileNotFoundError(f"한국어 폰트 파일을 찾을 수 없습니다: {src}")
    ttf_bytes = src.read_bytes()

    if not layout.fonts_dir.is_dir():
        raise FileNotFoundError(f"게임에서 fonts 폴더를 찾지 못했습니다: {layout.fonts_dir}")

    existing = [f for f in layout.fonts_dir.glob("*") if f.is_file()]
    if not existing:
        # Nothing bundled at all: drop a plain ttf under the name the
        # default MV/MZ engine looks for.
        target = layout.fonts_dir / "mplus-1m-regular.ttf"
        target.write_bytes(ttf_bytes)
        return [str(target)]

    changed: list[str] = []
    for f in existing:
        ext = f.suffix.lower()
        try:
            if ext in _RAW_EXTS:
                f.write_bytes(ttf_bytes)
            elif ext in _WOFF_FLAVORS:
                f.write_bytes(_convert(ttf_bytes, _WOFF_FLAVORS[ext]))
            else:
                continue
            changed.append(str(f))
        except Exception as e:  # noqa: BLE001
            changed.append(f"변환 실패: {f} ({e})")
    return changed
