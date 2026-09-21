"""RPG Maker MV/MZ -> Korean localizer.

Usage:
    python main.py all --game "C:\\path\\to\\game" --out "C:\\path\\to\\game_KO" ^
        --font "C:\\Windows\\Fonts\\malgun.ttf" --model qwen2.5:7b-instruct

Subcommands (run individually if you want more control):
    detect   - just identify the engine/layout and print it
    fonts    - swap the game's font for a Korean-capable one (works on --out copy)
    extract  - list every unique translatable string to a file (for review)
    translate- translate all strings via the local Ollama model into the cache
    inject   - apply cached translations into the game's JSON files
    all      - copy -> fonts -> extract -> translate -> inject, in one go
"""
from __future__ import annotations

import argparse
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from engine import detect_project, copy_project
from fonts import swap_font
from textwalk import walk_project
from translator import OllamaTranslator
from pipeline import run_all


def cmd_detect(args):
    layout = detect_project(args.game)
    print(f"엔진: {layout.engine}")
    print(f"루트: {layout.root}")
    print(f"데이터 폴더: {layout.data_dir}")
    print(f"폰트 폴더: {layout.fonts_dir}")


def cmd_fonts(args):
    layout = detect_project(args.game)
    changed = swap_font(layout, args.font)
    print(f"폰트 교체 완료 ({len(changed)}개 파일):")
    for c in changed:
        print(f"  {c}")


def cmd_extract(args):
    layout = detect_project(args.game)
    seen: set[str] = set()

    def collect(text: str) -> str:
        seen.add(text)
        return text  # no-op, just collecting

    # extract must not mutate files, so walk a throwaway copy path logic:
    # textwalk writes back what cb returns, and collect() returns the input unchanged.
    walk_project(layout, collect)

    out_path = args.out or "texts.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        for t in sorted(seen):
            f.write(t.replace("\n", "\\n") + "\n")
    print(f"고유 텍스트 {len(seen)}개를 {out_path}에 저장했습니다.")


def cmd_translate(args):
    layout = detect_project(args.game)
    translator = OllamaTranslator(model=args.model, cache_path=args.cache)

    texts: list[str] = []

    def collect(text: str) -> str:
        texts.append(text)
        return text

    walk_project(layout, collect)
    unique = sorted(set(texts))
    todo = [t for t in unique if translator.cache.get(t) is None]
    print(f"총 {len(unique)}개 고유 텍스트, 이 중 {len(todo)}개 새로 번역합니다 (모델: {args.model}).")

    for i, text in enumerate(todo, 1):
        translator.translate(text)
        if i % 10 == 0 or i == len(todo):
            translator.cache.save()
            print(f"  {i}/{len(todo)} 번역 완료...", flush=True)

    translator.cache.save()
    print(f"번역 캐시 저장: {args.cache}")


def cmd_inject(args):
    layout = detect_project(args.game)
    translator = OllamaTranslator(model=args.model, cache_path=args.cache)

    def replace(text: str) -> str:
        cached = translator.cache.get(text)
        return cached if cached is not None else text

    walk_project(layout, replace)
    print("번역 적용 완료.")


def cmd_all(args):
    run_all(
        game=args.game,
        out=args.out,
        font=args.font,
        model=args.model,
        cache_path=args.cache,
        log=print,
        workers=args.workers,
        install_hangul_plugin=not args.no_hangul_plugin,
    )


def main():
    parser = argparse.ArgumentParser(description="RPG Maker MV/MZ Korean localizer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect")
    p.add_argument("--game", required=True)
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("fonts")
    p.add_argument("--game", required=True)
    p.add_argument("--font", required=True, help="한국어를 지원하는 .ttf 경로 (예: C:\\Windows\\Fonts\\malgun.ttf)")
    p.set_defaults(func=cmd_fonts)

    p = sub.add_parser("extract")
    p.add_argument("--game", required=True)
    p.add_argument("--out", default="texts.txt")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("translate")
    p.add_argument("--game", required=True)
    p.add_argument("--model", default="qwen2.5:7b-instruct")
    p.add_argument("--cache", default="translations.json")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("inject")
    p.add_argument("--game", required=True)
    p.add_argument("--model", default="qwen2.5:7b-instruct")
    p.add_argument("--cache", default="translations.json")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("all")
    p.add_argument("--game", required=True, help="원본 게임 폴더")
    p.add_argument("--out", required=True, help="한국어화된 게임을 만들 새 폴더 (기존 폴더와 달라야 함)")
    p.add_argument("--font", default=None, help="한국어를 지원하는 .ttf 경로")
    p.add_argument("--model", default="qwen2.5:7b-instruct")
    p.add_argument("--cache", default="translations.json")
    p.add_argument("--workers", type=int, default=4, help="동시에 보낼 번역 요청 수")
    p.add_argument("--no-hangul-plugin", action="store_true",
                   help="이름 입력창 한글 지원 플러그인을 추가하지 않음")
    p.set_defaults(func=cmd_all)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:  # noqa: BLE001
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
