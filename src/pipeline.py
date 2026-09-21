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
from textwalk import walk_project, collect_glossary_names, collect_plugin_command_texts
from translator import OllamaTranslator, flag_reason

REVIEW_FILENAME = "_translation_review.json"

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
) -> str:
    """Runs the full localization pipeline. Returns the output game path."""
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

    translator = OllamaTranslator(model=model, cache_path=cache_path)

    # Pass 1: translate proper nouns (actor names, name-box speaker names)
    # first, then feed them back as a glossary so the same name comes out
    # consistently even when it's embedded inside a full sentence later.
    glossary_names = sorted(collect_glossary_names(out_layout))
    glossary_todo = [n for n in glossary_names if translator.cache.get(n) is None]
    if glossary_todo:
        log(f"고유명사 {len(glossary_names)}개 중 {len(glossary_todo)}개를 먼저 번역해 "
            f"용어집을 만듭니다.")
        _translate_batch(translator, glossary_todo, workers, log, None,
                          should_cancel, "용어집 번역 중...")
    glossary = {n: translator.cache.get(n) for n in glossary_names}
    translator.set_glossary(glossary)

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
    todo = [t for t in unique if translator.cache.get(t) is None]
    log(f"총 {len(unique)}개 고유 텍스트, 이 중 {len(todo)}개 새로 번역합니다 "
        f"(모델: {model}, 동시 요청: {workers}개).")

    _translate_batch(translator, todo, workers, log, progress, should_cancel, "번역 중...")

    def replace(text: str) -> str:
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
    # every retry, or came out suspiciously long (message-box overflow risk).
    flagged = []
    for src in unique:
        translated = translator.cache.get(src)
        if translated is None:
            continue
        reason = flag_reason(src, translated)
        if reason:
            flagged.append({"source": src, "translated": translated, "reason": reason})
    review_path = game_root / REVIEW_FILENAME
    review_path.write_text(json.dumps(flagged, ensure_ascii=False, indent=2), encoding="utf-8")
    if flagged:
        fallback_n = sum(1 for f in flagged if f["reason"] == "fallback")
        length_n = sum(1 for f in flagged if f["reason"] == "length")
        log(f"검토가 필요한 항목 {len(flagged)}개 (원문 유지 {fallback_n}개, "
            f"번역이 원문보다 많이 김 {length_n}개) -> {review_path.name}")
    else:
        log("검토가 필요한 항목 없음.")

    log(f"완료! 한국어화된 게임: {game_root}")
    return str(game_root)
