"""Everything behind the review window that isn't drawing widgets: which
entries to list, re-translating a selection with the local model, and
writing accumulated edits into the output game in one go.

Edits are applied in a batch rather than per keystroke because applying
means re-walking every data file of the output game (hundreds of MB for a
big game) -- once for fifty edits, not fifty times.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engine import detect_project
from providers import PROVIDERS
from render_patch import build_translation_map, inject_render_patch
from textwalk import walk_project
from translator import Cache, OllamaTranslator, flag_reason

REVIEW_FILENAME = "_translation_review.json"
META_FILENAME = "_translation_meta.json"

REASON_LABELS = {"fallback": "원문 유지", "length": "번역이 김", "untranslated": "번역 안 됨"}
ORIGIN_FILTERS = {"all": "전체", "api": "API", "local": "로컬", "manual": "직접 수정",
                  "unknown": "이전 버전", "untranslated": "번역 안 됨"}

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")


def origin_kind(origin: Optional[str], translated: Optional[str]) -> str:
    if not translated:
        return "untranslated"
    if origin == "manual":
        return "manual"
    if origin and origin.startswith("api:"):
        return "api"
    if origin and origin.startswith("local:"):
        return "local"
    return "unknown"


def origin_label(origin: Optional[str], translated: Optional[str]) -> str:
    kind = origin_kind(origin, translated)
    if kind == "api":
        pid = origin.split(":", 1)[1]
        cls = PROVIDERS.get(pid)
        return f"API · {cls.short_label if cls else pid}"
    if kind == "local":
        model = origin.split(":", 1)[1].rsplit("/", 1)[-1].split(":")[0]
        return f"로컬 · {model.removesuffix('-gguf')}"
    return ORIGIN_FILTERS[kind]


def review_items(texts: list[str], cache: Cache) -> list[dict]:
    """The post-run review list: translations that fell back to the source,
    came out suspiciously long, or never got translated at all."""
    items = []
    for src in texts:
        reason = _reason_for(src, cache.get(src))
        if reason:
            translated = cache.get(src) or ""
            items.append({"source": src, "translated": translated, "reason": reason,
                          "origin": cache.get_origin(src) if translated else None})
    return items


def _reason_for(src: str, translated: Optional[str]) -> Optional[str]:
    if translated is None:
        return "untranslated" if _JA_RE.search(src) else None
    return flag_reason(src, translated)


def write_meta(game_root: Path, *, game: str, cache_path: str, local_model: str,
               texts: list[str], guarded: set[str], glossary_names: list[str]) -> None:
    meta = {"format": 1, "game": game, "cache_path": cache_path, "local_model": local_model,
            "texts": texts, "guarded": sorted(guarded), "glossary_names": glossary_names}
    (game_root / META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@dataclass
class ReviewSession:
    out_path: Path
    cache: Cache
    texts: list[str]
    flagged: dict[str, str]                # source -> reason
    guarded: set[str] = field(default_factory=set)
    glossary_names: list[str] = field(default_factory=list)
    local_model: str = ""


def open_session(out_path: str, cache_path: str) -> ReviewSession:
    """Works for an output folder from this run or an earlier one. Folders
    made before the meta file existed fall back to the cache's own keys."""
    root = Path(out_path)
    if not Path(cache_path).exists():
        raise FileNotFoundError(f"번역 캐시 파일이 없습니다: {cache_path}")
    meta = _load_json(root / META_FILENAME, {})
    cache = Cache(cache_path)
    flagged = {i["source"]: i["reason"] for i in _load_json(root / REVIEW_FILENAME, [])
               if isinstance(i, dict) and i.get("source")}
    return ReviewSession(
        out_path=root, cache=cache,
        texts=meta.get("texts") or sorted(cache.data),
        flagged=flagged,
        guarded=set(meta.get("guarded") or []),
        glossary_names=meta.get("glossary_names") or [],
        local_model=meta.get("local_model") or "",
    )


def rows(session: ReviewSession, view: str = "flagged", origin_filter: str = "all",
         query: str = "", limit: Optional[int] = None) -> list[dict]:
    """`view` is "flagged" (the review list) or "all" (every text in the
    game). Returns dicts with source/translated/origin/kind/reason."""
    sources = list(session.flagged) if view == "flagged" else session.texts
    q = query.strip().lower()
    out = []
    for src in sources:
        translated = session.cache.get(src)
        if view == "all" and not translated and not _JA_RE.search(src):
            continue  # nothing to translate here (numbers, ASCII labels, ...)
        origin = session.cache.get_origin(src)
        kind = origin_kind(origin, translated)
        if origin_filter != "all" and kind != origin_filter:
            continue
        if q and q not in src.lower() and q not in (translated or "").lower():
            continue
        reason = session.flagged.get(src) if view == "flagged" else _reason_for(src, translated)
        out.append({"source": src, "translated": translated or "", "origin": origin,
                    "kind": kind, "reason": reason, "guarded": src in session.guarded})
        if limit is not None and len(out) >= limit:
            break
    return out


def set_manual(session: ReviewSession, source: str, value: str,
               pending: dict[str, Optional[str]]) -> None:
    """Records a hand edit in the cache; `pending` remembers what the output
    files held before, for apply_changes()."""
    pending.setdefault(source, session.cache.get(source))
    session.cache.set(source, value, origin="manual")
    session.cache.save()


def retranslate_local(
    session: ReviewSession,
    sources: list[str],
    model: str,
    workers: int = 4,
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[dict[str, Optional[str]], Optional[str]]:
    """Re-translates `sources` with the local model, even ones already
    translated. Returns ({source: previous translation}, error message or
    None). Stops at the first failure (e.g. Ollama not running) instead of
    burning retries on every remaining line; finished ones are kept."""
    translator = OllamaTranslator(model=model, cache_path=str(session.cache.path), log=log)
    translator.cache = session.cache
    translator.set_glossary({n: t for n in session.glossary_names if (t := session.cache.get(n))})

    before = {s: session.cache.get(s) for s in sources}
    changed: dict[str, Optional[str]] = {}
    error: Optional[str] = None
    stop = threading.Event()

    def work(src: str) -> str:
        if stop.is_set():
            raise InterruptedError
        translator.translate(src, force=True)
        return src

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, s) for s in sources]
        for fut in as_completed(futures):
            if should_cancel and should_cancel():
                stop.set()
            try:
                src = fut.result()
            except InterruptedError:
                continue
            except Exception as e:  # noqa: BLE001
                if error is None:
                    error = str(e)
                stop.set()
                continue
            changed[src] = before[src]
            done += 1
            if progress:
                progress(done, len(sources))
    session.cache.save()
    return changed, error


def apply_changes(session: ReviewSession, changes: dict[str, Optional[str]]) -> int:
    """Writes the current cache value of each changed source into the output
    game and regenerates the render patch. `changes` maps source -> what the
    output files held before (None: never translated, so the source text).

    Guarded sources (strings a plugin looks up by name) are never written
    into data/*.json -- only the render patch carries them. Returns how many
    changed sources were written into the data files.

    Output files hold translations, not sources, so the swap is by the old
    on-disk text: two different sources that had the identical translation
    both get the new one."""
    layout = detect_project(str(session.out_path))
    swap: dict[str, str] = {}
    written = 0
    for src, old in changes.items():
        new = session.cache.get(src)
        if not new or src in session.guarded:
            continue
        on_disk = old or src
        swap[on_disk] = new
        # Message paragraphs reach the walker without trailing blank lines.
        swap.setdefault(on_disk.rstrip("\n"), new)
        written += 1
    if swap:
        walk_project(layout, lambda t: swap.get(t, t))
    inject_render_patch(layout, build_translation_map(session.cache.data))

    for src in changes:
        reason = _reason_for(src, session.cache.get(src))
        if reason:
            session.flagged[src] = reason
        else:
            session.flagged.pop(src, None)
    items = []
    for src, reason in session.flagged.items():
        translated = session.cache.get(src) or ""
        items.append({"source": src, "translated": translated, "reason": reason,
                      "origin": session.cache.get_origin(src) if translated else None})
    (session.out_path / REVIEW_FILENAME).write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return written
