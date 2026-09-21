"""One-off cleanup: strip leaked template artifacts (e.g. trailing "answer"
lines) from an existing translation cache, and drop any entry that's still
bad afterwards (foreign-language leakage, not actually Korean) so the next
`inject`/`all` run retranslates just those with the fixed logic."""
import json
import sys

from translator import (
    collapse_repeated_chars,
    strip_template_artifacts,
    _has_foreign_leakage,
    _looks_translated,
)

cache_path = sys.argv[1]
data = json.load(open(cache_path, encoding="utf-8"))

stripped_count = 0
dropped_count = 0
cleaned = {}

for src, translated in data.items():
    new_val = collapse_repeated_chars(strip_template_artifacts(translated))
    changed = new_val != translated
    if changed:
        stripped_count += 1

    if not _looks_translated(src, new_val) or _has_foreign_leakage(src, new_val):
        dropped_count += 1
        continue  # drop -> will be retranslated on next run

    cleaned[src] = new_val

json.dump(cleaned, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print(f"총 {len(data)}개 항목 검사")
print(f"  artifact 제거(정리): {stripped_count}개")
print(f"  재번역 필요로 캐시에서 제거: {dropped_count}개")
print(f"  캐시에 남은 항목: {len(cleaned)}개")
