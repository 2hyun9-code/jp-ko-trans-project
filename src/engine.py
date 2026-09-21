"""RPG Maker MV/MZ project detection and layout helpers."""
from __future__ import annotations

import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ProjectLayout:
    root: Path                    # folder containing data/, fonts/, css/ ("www" for MV, same as launch_root for MZ)
    data_dir: Optional[Path]       # folder containing Map001.json, System.json, etc.
    fonts_dir: Optional[Path]      # folder containing font files
    css_dir: Optional[Path]        # folder containing gamefont.css (MZ) if present
    engine: str                    # "MV", "MZ", or "MZ-ASAR" (packed, needs extraction first)
    launch_root: Path = field(default=None)  # top-level folder holding the .exe / runtime

    def __post_init__(self):
        if self.launch_root is None:
            # MZ/MZ-ASAR: the launchable folder *is* root. MV is the only
            # case where data lives one level down (in "www"), inside a
            # folder that also holds the actual .exe + JS-engine runtime
            # (NW.js/Electron) that a copy must not lose -- detect_project
            # sets launch_root explicitly for that case.
            self.launch_root = self.root


_SKIP_DIR_NAMES = {"node_modules", ".git"}


def _find_data_dir(root: Path, max_depth: int = 3) -> Optional[Path]:
    """Breadth-first search for a folder containing data/System.json under
    `root`, up to max_depth levels deep. Covers the standard "www" (MV) and
    root (MZ) layouts as well as custom ones some games use (e.g. a
    "project" folder)."""
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        cur, depth = queue.popleft()
        if (cur / "data" / "System.json").exists():
            return cur / "data"
        if depth >= max_depth:
            continue
        try:
            subdirs = [p for p in cur.iterdir() if p.is_dir() and p.name not in _SKIP_DIR_NAMES]
        except (PermissionError, OSError):
            continue
        for sub in subdirs:
            queue.append((sub, depth + 1))
    return None


def find_app_asar(root: Path) -> Optional[Path]:
    """Looks for an Electron-packed `resources/app.asar`, directly under
    root or one level down (covers a bundle that ships several game folders
    side by side, each with its own exe + resources/)."""
    direct = root / "resources" / "app.asar"
    if direct.exists():
        return direct
    if root.is_dir():
        try:
            subdirs = [p for p in root.iterdir() if p.is_dir()]
        except (PermissionError, OSError):
            subdirs = []
        for sub in subdirs:
            candidate = sub / "resources" / "app.asar"
            if candidate.exists():
                return candidate
    return None


def detect_project(game_folder: str) -> ProjectLayout:
    root = Path(game_folder)
    if not root.exists():
        raise FileNotFoundError(f"게임 폴더를 찾을 수 없습니다: {root}")

    data_dir = _find_data_dir(root)
    if data_dir is not None:
        base = data_dir.parent
        fonts_dir = base / "fonts"
        css_dir = base / "css"
        engine = "MV" if base.name == "www" else "MZ"
        # MV ships data/ inside "www", sitting alongside the actual .exe and
        # JS-engine runtime (NW.js/Electron) one level up -- that outer
        # folder is what a copy needs to preserve so the result stays
        # launchable, not just base ("www") itself.
        launch_root = base.parent if engine == "MV" else base
        return ProjectLayout(root=base, data_dir=data_dir, fonts_dir=fonts_dir,
                              css_dir=css_dir, engine=engine, launch_root=launch_root)

    if find_app_asar(root) is not None:
        # Packed into an Electron .asar archive -- pipeline.run_all extracts
        # it into the copy and re-detects before doing anything else.
        return ProjectLayout(root=root, data_dir=None, fonts_dir=None, css_dir=None,
                              engine="MZ-ASAR", launch_root=root)

    raise ValueError(
        "RPG Maker MV/MZ 프로젝트가 아닌 것 같습니다 (data/System.json을 찾지 못했습니다). "
        "VX Ace나 2000/2003 포맷은 아직 지원하지 않습니다."
    )


def copy_project(layout: ProjectLayout, out_root: str) -> ProjectLayout:
    """Copy the whole launchable game folder (not just the data-holding
    subfolder -- for MV that would silently drop the .exe/runtime) to
    out_root, and return a layout pointing at the copy."""
    dst = Path(out_root)
    if dst.exists():
        raise FileExistsError(f"출력 폴더가 이미 존재합니다: {dst}. 다른 경로를 지정해주세요.")
    shutil.copytree(layout.launch_root, dst)

    def _rebase(p: Optional[Path]) -> Optional[Path]:
        if p is None:
            return None
        if p == layout.launch_root:
            return dst
        return dst / p.relative_to(layout.launch_root)

    return ProjectLayout(
        root=_rebase(layout.root),
        data_dir=_rebase(layout.data_dir),
        fonts_dir=_rebase(layout.fonts_dir),
        css_dir=_rebase(layout.css_dir),
        engine=layout.engine,
        launch_root=dst,
    )
