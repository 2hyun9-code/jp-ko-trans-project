"""Shared pipeline: copy -> font swap -> extract -> translate -> inject.

Used by both the CLI (main.py) and the desktop GUI (gui.py) so the two
front-ends never drift apart. `log` and `progress` are callbacks so each
front-end can render status its own way (print / log box / progress bar).
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from asar_tool import extract_asar
from engine import detect_project, copy_project, find_app_asar
from fonts import swap_font
from plugin_install import install_hangul_name_plugin
from plugin_text import collect_plugin_strings
from render_patch import build_translation_map, inject_render_patch
from providers import FatalProviderError, Provider, run_api_pass
from refindex import build_reference_index
from review import REVIEW_FILENAME, review_items, write_meta
from textwalk import walk_project, collect_glossary_names, collect_plugin_command_texts
from translator import PROBLEM_LABELS, OllamaTranslator

MODE_LABELS = {"hybrid": "API 우선 + 로컬 보완", "api": "API만", "local": "로컬만"}

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]  # (done, total)


def _translate_batch(
    translator: OllamaTranslator,
    todo: list[str],
    workers: int,
    log: LogFn,
    progress: Optional[ProgressFn],
    should_cancel: Optional[Callable[[], bool]],
    label: str,
) -> None:
    """Runs `translator.translate()` over `todo` with a thread pool, saving
    the cache after every completion. Raises InterruptedError on cancel."""
    total = len(todo)
    done = 0
    done_lock = threading.Lock()
    cancelled = threading.Event()

    def worker(text: str) -> None:
        if cancelled.is_set():
            return
        translator.translate(text)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(worker, text): text for text in todo}
        try:
            for fut in as_completed(futures):
                if should_cancel and should_cancel() and not cancelled.is_set():
                    cancelled.set()
                    for f in futures:
                        f.cancel()
                if fut.cancelled():
                    continue
                fut.result()
                with done_lock:
                    done += 1
                    n = done
                if progress:
                    progress(n, total)
                translator.cache.save()
                if n % 10 == 0 or n == total:
                    log(f"  {label} {n}/{total}")
        finally:
            translator.cache.save()

    if cancelled.is_set():
        log("사용자가 취소했습니다. 지금까지의 번역은 캐시에 저장되었습니다.")
        raise InterruptedError("cancelled")


def _reuse_line_translations(texts: list[str], cache) -> int:
    """Multi-line messages used to be translated one line at a time, so an
    existing cache holds each line separately. Now that they're translated
    as whole paragraphs, stitch those old line translations together
    instead of paying to re-translate the whole game. Returns how many
    paragraphs were filled this way."""
    reused = 0
    for text in texts:
        if "\n" not in text or cache.get(text) is not None:
            continue
        lines = text.split("\n")
        parts = [cache.get(ln) if ln.strip() else ln for ln in lines]
        if any(p is None for p in parts):
            continue
        first = next(ln for ln in lines if ln.strip())
        cache.set(text, "\n".join(parts), origin=cache.get_origin(first))
        reused += 1
    return reused


def _translate_texts(
    todo: list[str],
    translator: OllamaTranslator,
    provider: Optional[Provider],
    mode: str,
    workers: int,
    log: LogFn,
    progress: Optional[ProgressFn],
    should_cancel: Optional[Callable[[], bool]],
    label: str,
) -> None:
    """API first (when the mode uses it), then the local model for whatever
    the API didn't get right. In "api" mode the leftovers stay untranslated
    and show up in the review list instead."""
    remaining = list(todo)
    if provider is not None and mode in ("api", "hybrid") and remaining:
        log(f"{label}: {provider.label}(으)로 {len(remaining)}개 1차 번역합니다.")
        res = run_api_pass(provider, remaining, translator.cache, log, progress, should_cancel,
                           label, split_refused=(mode == "api"))
        remaining = res.failed
        counts: dict[str, int] = {}
        for reason in res.reasons.values():
            counts[reason] = counts.get(reason, 0) + 1
        detail = ", ".join(f"{PROBLEM_LABELS.get(r, r)} {n}" for r, n in
                           sorted(counts.items(), key=lambda kv: -kv[1]))
        log(f"  {label} API 결과: 성공 {res.ok}개, 실패 {len(remaining)}개"
            + (f" ({detail})" if detail else ""))
        for msg, n in list(res.errors.items())[:3]:
            log(f"  API 오류 {n}회: {msg}")
        if res.fatal:
            log(f"  API 사용 중단: {res.fatal}")
            if mode == "api":
                raise RuntimeError(f"API 번역을 계속할 수 없습니다: {res.fatal}")
    if not remaining:
        return
    if mode in ("local", "hybrid"):
        if mode == "hybrid":
            log(f"  {label}: 나머지 {len(remaining)}개를 로컬 모델({translator.model})로 번역합니다.")
        _translate_batch(translator, remaining, workers, log, progress, should_cancel,
                         f"{label} 로컬")
    else:
        log(f"  {label}: API로 번역하지 못한 {len(remaining)}개는 원문 그대로 두고 검토 목록에 올립니다.")


def run_all(
    game: str,
    out: str,
    font: Optional[str],
    model: str,
    cache_path: str,
    log: LogFn = print,
    progress: Optional[ProgressFn] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    workers: int = 4,
    install_hangul_plugin: bool = True,
    mode: str = "local",
    provider: Optional[Provider] = None,
) -> str:
    """Runs the full localization pipeline. Returns the output game path.

    `model` is the local (Ollama) model. `mode` is "local", "api", or
    "hybrid" (API first, local model re-does what the API got wrong);
    the API modes need `provider`."""
    if mode not in MODE_LABELS:
        raise ValueError(f"알 수 없는 번역 방식: {mode}")
    if mode != "local":
        if provider is None:
            raise RuntimeError("API 번역 엔진이 지정되지 않았습니다.")
        try:
            provider.validate()
        except FatalProviderError as e:
            raise RuntimeError(str(e)) from None
    else:
        provider = None

    src_layout = detect_project(game)
    log(f"엔진 감지: {src_layout.engine} ({src_layout.root})")

    out_layout = copy_project(src_layout, out)
    log(f"게임 복사본 생성: {out_layout.root}")
    # The launchable game root (has the .exe/runtime) -- for MZ this is the
    # same as out_layout.root, but for MV it's one level up from "www", and
    # for an asar-packed Electron game out_layout gets reassigned below to
    # point deep inside resources/app/, so this needs to be captured now to
    # report the right folder at the end.
    game_root = out_layout.launch_root

    if out_layout.engine == "MZ-ASAR":
        asar_path = find_app_asar(out_layout.root)
        app_dir = asar_path.parent / "app"
        n = extract_asar(asar_path, app_dir)
        asar_path.rename(asar_path.with_name(asar_path.name + ".bak"))
        log(f"asar 압축 해제 완료 ({n}개 파일) -> {app_dir}")
        out_layout = detect_project(str(app_dir))
        log(f"엔진 재감지: {out_layout.engine} ({out_layout.root})")

    if font:
        changed = swap_font(out_layout, font)
        log(f"폰트 교체 완료 ({len(changed)}개 파일 변경)")
    else:
        log("폰트 파일 미지정: 폰트 교체를 건너뜁니다.")

    if install_hangul_plugin:
        msg = install_hangul_name_plugin(out_layout)
        if msg:
            log(msg)

    translator = OllamaTranslator(model=model, cache_path=cache_path, log=log)
    engine_desc = f"로컬 모델: {model}" if provider is None else (
        f"API: {provider.label}" + ("" if mode == "api" else f", 로컬 모델: {model}"))
    log(f"번역 방식: {MODE_LABELS[mode]} ({engine_desc})")

    # Pass 1: translate proper nouns (actor names, name-box speaker names)
    # first, then feed them back as a glossary so the same name comes out
    # consistently even when it's embedded inside a full sentence later.
    glossary_names = sorted(collect_glossary_names(out_layout))
    glossary_todo = [n for n in glossary_names if translator.cache.get(n) is None]
    if glossary_todo:
        log(f"고유명사 {len(glossary_names)}개 중 {len(glossary_todo)}개를 먼저 번역해 "
            f"용어집을 만듭니다.")
        _translate_texts(glossary_todo, translator, provider, mode, workers, log, None,
                         should_cancel, "용어집")
    glossary = {n: t for n in glossary_names if (t := translator.cache.get(n))}
    translator.set_glossary(glossary)
    if provider is not None:
        provider.set_glossary(glossary)

    # Pass 2: everything else, now with the glossary steering embedded mentions.
    texts: list[str] = []

    def collect(text: str) -> str:
        texts.append(text)
        return text

    walk_project(out_layout, collect)

    # plugins.js text (plugin parameter labels, etc.) isn't stored in
    # data/*.json at all, so walk_project never sees it -- harvest it
    # separately and feed it into the same translate+cache flow. We never
    # write anything back into plugins.js; these translations only ever
    # reach the player through render_patch.py's draw-time substitution.
    plugins_js = out_layout.root / "js" / "plugins.js"
    if plugins_js.exists():
        plugin_strings = collect_plugin_strings(plugins_js)
        if plugin_strings:
            log(f"plugins.js에서 화면 출력 시점 패치용 텍스트 {len(plugin_strings)}개 발견"
                f" (원본 plugins.js는 수정하지 않음).")
        texts.extend(plugin_strings)

    # Same idea, but for "Plugin Command" event lines (code 356/357) -- a
    # HUD-style label can live there instead of plugins.js, e.g. a whole
    # command line like `D_TEXT \ow[5]好感度: \V[43] 31`. Also read-only:
    # only feeds the render-time patch's lookup table.
    plugin_cmd_texts = collect_plugin_command_texts(out_layout)
    if plugin_cmd_texts:
        log(f"Plugin Command 라인에서 화면 출력 시점 패치용 텍스트 {len(plugin_cmd_texts)}개 발견"
            f" (원본 이벤트 데이터는 수정하지 않음).")
    texts.extend(plugin_cmd_texts)

    unique = sorted(set(texts))
    reused = _reuse_line_translations(unique, translator.cache)
    if reused:
        translator.cache.save()
        log(f"기존 줄 단위 번역을 이어붙여 여러 줄 대사 {reused}개를 다시 번역하지 않고 재사용합니다.")
    todo = [t for t in unique if translator.cache.get(t) is None]
    log(f"총 {len(unique)}개 고유 텍스트, 이 중 {len(todo)}개 새로 번역합니다.")

    _translate_texts(todo, translator, provider, mode, workers, log, progress, should_cancel,
                     "본문")

    # Text that the game also uses as a lookup key (a skill named in a note
    # tag, an item compared in a script, ...) stays original in data/*.json
    # so those lookups keep working; its translation still reaches the
    # screen through the render-time patch below.
    refs = build_reference_index(out_layout)
    guarded = {t for t in unique if t.strip() in refs}
    if guarded:
        examples = ", ".join(sorted(guarded, key=len)[:8])
        log(f"게임이 이름으로 찾아 쓰는 값과 같은 텍스트 {len(guarded)}개는 데이터 파일에는 "
            f"원문으로 두고, 화면에 그릴 때만 번역합니다 (예: {examples}).")

    def replace(text: str) -> str:
        if text in guarded:
            return text
        cached = translator.cache.get(text)
        return cached if cached is not None else text

    walk_project(out_layout, replace)

    # Render-time patch: catches text the static data/*.json editing can't
    # reach (plugin-parameter strings, dynamically-built labels, ...) by
    # swapping it in right before RPG Maker draws it on screen, without
    # touching plugins.js or any other source file.
    translation_map = build_translation_map(translator.cache.data)
    patch_path = inject_render_patch(out_layout, translation_map)
    if patch_path:
        log(f"화면 출력 시점 번역 패치 적용 ({len(translation_map)}개 항목) -> "
            f"{patch_path.relative_to(out_layout.root)}")
    else:
        log("index.html을 찾지 못해 화면 출력 시점 패치는 건너뜁니다.")

    # Post-run review list: entries that fell back to the source text after
    # every retry, came out suspiciously long (message-box overflow risk),
    # or never got translated at all (API-only mode, API failed on them).
    flagged = review_items(unique, translator.cache)
    # What the review window needs to re-open this output later: the game's
    # own text list (the cache can hold stale keys), the guarded names, and
    # the glossary names for consistent re-translation.
    write_meta(game_root, game=game, cache_path=cache_path, local_model=model, texts=unique,
               guarded=guarded, glossary_names=glossary_names)
    review_path = game_root / REVIEW_FILENAME
    review_path.write_text(json.dumps(flagged, ensure_ascii=False, indent=2), encoding="utf-8")
    if flagged:
        fallback_n = sum(1 for f in flagged if f["reason"] == "fallback")
        length_n = sum(1 for f in flagged if f["reason"] == "length")
        untranslated_n = sum(1 for f in flagged if f["reason"] == "untranslated")
        log(f"검토가 필요한 항목 {len(flagged)}개 (원문 유지 {fallback_n}개, "
            f"번역이 원문보다 많이 김 {length_n}개, 번역 안 됨 {untranslated_n}개) "
            f"-> {review_path.name}")
    else:
        log("검토가 필요한 항목 없음.")

    log(f"완료! 한국어화된 게임: {game_root}")
    return str(game_root)
