import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import translator  # noqa: E402
from translator import (  # noqa: E402
    Cache,
    OllamaTranslator,
    _has_foreign_leakage,
    _looks_like_leaked_explanation,
    _looks_translated,
    _num_predict_for,
    _options_for_model,
    collapse_repeated_chars,
    flag_reason,
    protect_codes,
    restore_codes,
    strip_template_artifacts,
)


def test_protect_and_restore_round_trip():
    text = "こんにちは、\\N[1]さん！\\C[2]赤い\\C[0]文字"
    protected, mapping = protect_codes(text)
    assert "\\N[1]" not in protected
    assert "\\C[2]" not in protected
    restored = restore_codes("__T0__ 안녕하세요! __T1__빨간__T2__글자", mapping)
    assert restored == "\\N[1] 안녕하세요! \\C[2]빨간\\C[0]글자"


def test_protect_codes_empty_text():
    protected, mapping = protect_codes("")
    assert protected == ""
    assert mapping == {}


def test_looks_translated_detects_hangul():
    assert _looks_translated("こんにちは", "안녕하세요") is True
    assert _looks_translated("こんにちは", "Hello") is False


def test_looks_translated_allows_non_translatable_source():
    # Source has no kana/CJK to translate in the first place (numbers/symbols).
    assert _looks_translated("100%", "100%") is True


def test_has_foreign_leakage_cyrillic():
    assert _has_foreign_leakage("薄い", "тонкий") is True


def test_has_foreign_leakage_new_latin_word():
    assert _has_foreign_leakage("フォーマル", "Formal하게") is True


def test_has_foreign_leakage_allows_latin_already_in_source():
    # "HP" appears in the source, so keeping it isn't leakage.
    assert _has_foreign_leakage("HPが回復した", "HP가 회복됐다") is False


def test_looks_like_leaked_explanation_meta_phrase():
    assert _looks_like_leaked_explanation("柴乃", "시바노로 번역했습니다") is True


def test_looks_like_leaked_explanation_length_balloon():
    short_source = "はい"
    balloon = "이것은 매우 길고 자세한 설명이 붙은 번역 결과입니다 정말로 길어요"
    assert _looks_like_leaked_explanation(short_source, balloon) is True


def test_looks_like_leaked_explanation_normal_translation_ok():
    assert _looks_like_leaked_explanation("はい", "예") is False


def test_strip_template_artifacts_removes_answer_line():
    text = "안녕하세요\n\nanswer\n오늘도 좋은 날\nanswer"
    cleaned = strip_template_artifacts(text)
    assert "answer" not in cleaned.lower()
    assert "안녕하세요" in cleaned
    assert "오늘도 좋은 날" in cleaned


def test_strip_template_artifacts_leaves_normal_text_alone():
    text = "그냥 평범한 번역문입니다."
    assert strip_template_artifacts(text) == text


def test_strip_template_artifacts_removes_paraphrased_instruction_echo():
    """The model sometimes paraphrases its own "don't add explanations"
    instruction back as a trailing parenthetical, in different wording each
    time, instead of leaking a fixed template string -- these are real
    examples pulled from the review UI."""
    cases = {
        "//패턴2\n\n(설명을 붙이지 않고, 실제 화면에 표시될 내용만을 간단히 작성하세요.)": "//패턴2",
        "BGM 볼륨\n\n(설명이나 이유를 덧붙이지 말고, 게임에 표시될 번역 결과를 그대로 복사해서 붙여 넣으세요.)": "BGM 볼륨",
        "최대 HP\n\n(설명이나 이유를 덧붙이지 마세요. 단순히 한국어로 옮기면 됩니다.)": "최대 HP",
    }
    for raw, expected in cases.items():
        assert strip_template_artifacts(raw) == expected


def test_strip_template_artifacts_removes_instruction_echo_with_unlisted_verb_ending():
    # A verb ending ("옮기세요") that a fixed list of specific endings would
    # miss -- this is exactly the case that slipped through the first pass
    # of _INSTRUCTION_ECHO_RE (it only listed 하세요/마세요/됩니다/...).
    raw = "최대 HP\n\n(설명이나 이유를 덧붙이지 말고, 단순히 한국어로 옮기세요.)"
    assert strip_template_artifacts(raw) == "최대 HP"


def test_strip_template_artifacts_keeps_legitimate_trailing_parenthetical():
    # A real parenthetical remark in translated game text (no instructional
    # verb ending, no "설명"/"이유" mention) must survive untouched.
    text = "최대 HP (기본값)"
    assert strip_template_artifacts(text) == text


def test_strip_template_artifacts_keeps_polite_ending_without_trigger_words():
    # The "~세요/~니다" ending alone isn't enough to trigger removal --
    # it also needs one of the instruction-echo keywords ("설명"/"이유"/
    # "번역결과"), so ordinary in-game dialogue in polite speech that
    # happens to end in a parenthetical aside must survive untouched.
    text = "이쪽으로 오세요 (작게 속삭이며)"
    assert strip_template_artifacts(text) == text


def test_collapse_repeated_chars_trims_long_run():
    text = "터져버려" + "어" * 30
    assert collapse_repeated_chars(text) == "터져버려어어어"


def test_collapse_repeated_chars_preserves_trailing_symbol():
    text = "오" * 20 + "♪"
    assert collapse_repeated_chars(text) == "오오오♪"


def test_collapse_repeated_chars_leaves_short_runs_alone():
    # 3 or fewer repeats is already natural and shouldn't be touched.
    assert collapse_repeated_chars("정말 좋아아아") == "정말 좋아아아"


def test_collapse_repeated_chars_leaves_normal_text_alone():
    text = "그냥 평범한 번역문입니다."
    assert collapse_repeated_chars(text) == text


def test_flag_reason_fallback():
    assert flag_reason("こんにちは", "こんにちは") == "fallback"


def test_flag_reason_length():
    # flag_reason only applies the length-ratio check once the source is at
    # least 4 characters (short strings are too noisy to judge by ratio).
    assert flag_reason("追加シーン用", "이것은 아주 길게 부풀려진 번역 결과 문장입니다") == "length"


def test_flag_reason_ok_translation():
    assert flag_reason("こんにちは", "안녕하세요") is None


def test_flag_reason_non_translatable_source():
    assert flag_reason("100", "100") is None


def test_options_for_model_matches_known_model():
    opts = _options_for_model("hf.co/hell0ks/ja-ko-vn-12b-v2-gguf:Q5_K_M")
    assert opts["temperature"] == 0.1


def test_options_for_model_default_for_unknown():
    opts = _options_for_model("qwen2.5:14b-instruct")
    assert opts == {"temperature": 0.3}


def test_options_for_model_matches_hy_mt2():
    opts = _options_for_model("kaelri/hy-mt2:7b")
    assert opts == {"temperature": 0.2, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05}


def test_num_predict_scales_with_length_within_bounds():
    assert _num_predict_for("a") == 64  # floor, not near-zero
    assert _num_predict_for("a" * 100) == 400  # 4x, under the cap
    assert _num_predict_for("a" * 1000) == 1024  # ceiling, not 4000


def test_cache_round_trip(tmp_path):
    path = tmp_path / "cache.json"
    cache = Cache(str(path))
    assert cache.get("hello") is None
    cache.set("hello", "안녕")
    cache.save()

    reloaded = Cache(str(path))
    assert reloaded.get("hello") == "안녕"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"hello": "안녕"}


def test_translate_logs_slow_response(tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "_SLOW_RESPONSE_SEC", 0.0)
    times = iter([0.0, 1.0])  # start, then elapsed 1.0s -- always "slow" here
    monkeypatch.setattr(translator.time, "monotonic", lambda: next(times))

    logged = []
    t = OllamaTranslator(model="qwen2.5:14b-instruct",
                          cache_path=str(tmp_path / "cache.json"),
                          log=logged.append)
    monkeypatch.setattr(t, "_call_model", lambda prompt: "안녕하세요")

    result = t.translate("こんにちは")
    assert result == "안녕하세요"
    assert len(logged) == 1
    assert "느린 응답" in logged[0]
    assert "こんにちは" in logged[0]


def test_translate_no_log_when_fast(tmp_path, monkeypatch):
    times = iter([0.0, 0.01])
    monkeypatch.setattr(translator.time, "monotonic", lambda: next(times))

    logged = []
    t = OllamaTranslator(model="qwen2.5:14b-instruct",
                          cache_path=str(tmp_path / "cache.json"),
                          log=logged.append)
    monkeypatch.setattr(t, "_call_model", lambda prompt: "안녕하세요")

    t.translate("こんにちは")
    assert logged == []
