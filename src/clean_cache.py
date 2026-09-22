"""One-off cleanup: strip leaked template artifacts (e.g. trailing "answer"
lines) from an existing translation cache, and drop any entry that's still
bad afterwards (foreign-language leakage, not actually Korean) so the next
`inject`/`all` run retranslates just those with the fixed logic."""
import sys

from translator import (
    Cache,
    collapse_repeated_chars,
    strip_template_artifacts,
    _has_foreign_leakage,
    _looks_translated,
)

cache = Cache(sys.argv[1])
total = len(cache.data)

stripped_count = 0
dropped_count = 0

for src, translated in list(cache.data.items()):
    new_val = collapse_repeated_chars(strip_template_artifacts(translated))
    if not _looks_translated(src, new_val) or _has_foreign_leakage(src, new_val):
        dropped_count += 1
        cache.delete(src)  # -> will be retranslated on next run
        continue
    if new_val != translated:
        stripped_count += 1
        cache.set(src, new_val, origin=cache.get_origin(src))

cache.save()

print(f"총 {total}개 항목 검사")
print(f"  artifact 제거(정리): {stripped_count}개")
print(f"  재번역 필요로 캐시에서 제거: {dropped_count}개")
print(f"  캐시에 남은 항목: {len(cache.data)}개")
