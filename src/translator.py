"""Local translation via Ollama, with RPG Maker escape-code protection
and a persistent cache so re-runs don't re-translate the same string."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"

# RPG Maker text codes we must not let the LLM touch: \N[1], \V[2], \C[3],
# \I[4], \P[5], \G, \\, \{, \}, \., \|, \!, \>, \<, \^, \$
_CODE_RE = re.compile(
    r"\\[NVCIPG]\[\d+\]|\\[GgSs\\{}\.\|!><\^\$]",
    re.IGNORECASE,
)

SYSTEM_PROMPT = (
    "당신은 일본어 RPG 게임을 한국어로 현지화하는 전문 번역가입니다. "
    "입력된 텍스트를 자연스러운 한국어 게임 대사체로 번역하세요.\n"
    "규칙:\n"
    "1. __T0__, __T1__ 같은 형태의 토큰은 게임 엔진 제어 코드이므로 "
    "절대 번역하거나 수정하지 말고 원문 그대로, 문맥상 자연스러운 위치에 유지하세요.\n"
    "2. 번역 결과만 출력하세요. 설명, 따옴표, 부가 텍스트를 덧붙이지 마세요.\n"
    "3. 원문이 비어 있거나 기호만 있으면 그대로 반환하세요.\n"
    "4. 출력은 반드시 한글(한국어)로만 작성하세요. 한자, 가나, 러시아어, 영어 "
    "단어를 그대로 베끼거나 섞어 쓰지 말고 전부 실제 한국어 단어로 옮기세요 "
    "(예: '盾' -> '방패', '逃げる' -> '도망가다', 'тонкий' -> '가느다란', "
    "'Formal' -> '격식 있는'). 원문에 없던 영어 단어를 새로 섞어 쓰지 마세요.\n"
    "5. 번역 이유, 판단 과정, '~로 번역했습니다/변경합니다' 같은 메타 발언은 "
    "절대 출력하지 마세요. 게임 화면에 그대로 표시될 텍스트만 출력해야 합니다."
)

_HANGUL_RE = re.compile(r"[가-힣]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_META_PHRASES = ("번역했습니다", "번역합니다", "변경합니다", "위해", "다음과 같이",
                  "라고 번역", "자연스러운 번역", "번역하겠습니다")


def _looks_translated(original: str, translated: str) -> bool:
    """True if translated contains Hangul, or the original had nothing to
    translate in the first place (pure numbers/symbols/latin)."""
    if _HANGUL_RE.search(translated):
        return True
    return not re.search(r"[぀-ヿ一-鿿]", original)  # no kana/CJK in source either


def _has_foreign_leakage(original: str, translated: str) -> bool:
    """True if the translation contains Cyrillic, or a Latin word that
    wasn't already in the source -- both are signs the model answered
    (partly) in the wrong language instead of translating fully to Korean.
    Latin words that were already present in the source (acronyms, proper
    nouns the game itself keeps untranslated) are allowed through."""
    if _CYRILLIC_RE.search(translated):
        return True
    for word in _LATIN_WORD_RE.findall(translated):
        if word not in original:
            return True
    return False


def _looks_like_leaked_explanation(original: str, translated: str) -> bool:
    """Catches cases where the model explains its translation choice instead
    of just giving it (e.g. a short button label coming back as a full
    sentence about why it chose that wording)."""
    if any(p in translated for p in _META_PHRASES):
        return True
    # A short source ballooning into a much longer response is suspicious;
    # real Korean can run a bit longer than the source, but not this much.
    if len(original) <= 20 and len(translated) > max(30, len(original) * 4):
        return True
    return False


def _is_bad_translation(original: str, translated: str) -> bool:
    return (not _looks_translated(original, translated)
            or _has_foreign_leakage(original, translated)
            or _looks_like_leaked_explanation(original, translated))


def flag_reason(original: str, translated: str) -> str | None:
    """Classifies a finished (cached) translation for the post-run review
    list. Returns None if it looks fine, otherwise a short reason code:
    - "fallback": translation == source, i.e. every attempt failed and the
      original text was kept as-is (see OllamaTranslator.translate).
    - "length": the Korean came out much longer than the source, which is a
      common cause of message-box overflow/clipping in-game.
    """
    if not re.search(r"[぀-ヿ一-鿿]", original):
        return None  # nothing translatable in the source to begin with
    if translated.strip() == original.strip():
        return "fallback"
    if len(original) >= 4 and len(translated) > len(original) * 1.8 + 8:
        return "length"
    return None


# Some fine-tuned models (this happens with ja-ko-vn-12b-v2, whose model card
# says it expects a raw completion-style prompt rather than a chat template)
# echo a leftover template marker as its own line, e.g. a trailing "answer"
# line after the real translation. Strip those before any other check runs.
_ARTIFACT_LINE_RE = re.compile(
    r"^\s*(answer|question|input|output|response)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# The model sometimes paraphrases the instruction it was just given ("don't
# add explanations, just output the translation") back as a trailing
# parenthetical, in its own words every time -- so no fixed phrase list can
# catch it (a first attempt at this matched a handful of specific verb
# endings, e.g. "~하세요", and missed the next paraphrase that ended in
# "~옮기세요" instead). Match the *shape* of the echo instead: a parenthetical
# ending in essentially any Korean polite sentence-final form (the broad
# "~세요" / "~니다" families cover "하세요/마세요/옮기세요/작성하세요/..." and
# "됩니다/입니다/습니다/..." respectively) that also mentions
# "설명"(explanation)/"이유"(reason)/"번역"(translation) -- a combination
# that essentially never occurs in real translated game text, which doesn't
# lecture the player about how it was translated.
_INSTRUCTION_ECHO_RE = re.compile(
    r"\s*[\(（][^()（）]{0,150}(?:설명|이유|번역\s*결과)[^()（）]{0,150}"
    r"(?:세요|니다)[.!]?\s*[\)）]\s*$"
)


def strip_template_artifacts(text: str) -> str:
    cleaned = _ARTIFACT_LINE_RE.sub("", text)
    cleaned = _INSTRUCTION_ECHO_RE.sub("", cleaned)
    lines = [ln for ln in cleaned.split("\n")]
    # collapse runs of blank lines left behind by the removal
    out: list[str] = []
    for ln in lines:
        if ln.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(ln)
    return "\n".join(out).strip()


# Onomatopoeia/interjections ("아아아아...", moans, screams, etc.) sometimes
# come back with the trailing vowel repeated far more than any human editor
# would type by hand. This isn't wrong content, just needs trimming -- and
# whatever punctuation/symbol follows the run (♪, ～, ♥, …, ！, closing quote
# marks, ...) is left completely alone since it's a different character and
# so never matches the run itself.
_REPEATED_CHAR_RE = re.compile(r"(.)\1{3,}")


def collapse_repeated_chars(text: str, max_repeat: int = 3) -> str:
    return _REPEATED_CHAR_RE.sub(lambda m: m.group(1) * max_repeat, text)


# Some models publish their own recommended sampling settings -- match on a
# substring of the Ollama model name (works for tags like
# "hf.co/hell0ks/ja-ko-vn-12b-v2-gguf:Q5_K_M" too) and fall back to a
# generic default otherwise.
_MODEL_OPTIONS = {
    "ja-ko-vn": {"temperature": 0.1, "top_k": 95, "repeat_penalty": 1.08},
    # No official tuning from Tencent for this one -- these are community-
    # tested values from https://huggingface.co/tencent/Hy-MT2-7B-GGUF/discussions/3
    # that reduced (but didn't fully eliminate) a reported tag-corruption/
    # repetition-loop issue on long inputs. The num_predict cap below is the
    # actual safety net for the "didn't fully eliminate" part.
    "hy-mt2": {"temperature": 0.2, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05},
}
_DEFAULT_OPTIONS = {"temperature": 0.3}


def _options_for_model(model: str) -> dict:
    for key, opts in _MODEL_OPTIONS.items():
        if key in model:
            return opts
    return _DEFAULT_OPTIONS


# A model with no repeat_penalty tuned for it (i.e. anything not matched in
# _MODEL_OPTIONS above) can fall into a repetition loop and never naturally
# stop generating -- observed in practice as a translation silently taking
# minutes instead of seconds, stalling every worker thread at once with no
# error raised. Capping num_predict bounds the damage regardless of model:
# generous enough for real game-dialogue-length translations, but not
# infinite. Scales with input length since a long source line legitimately
# needs a longer translation.
def _num_predict_for(text: str) -> int:
    return max(64, min(1024, len(text) * 4))


def protect_codes(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def _sub(m: re.Match) -> str:
        token = f"__T{len(mapping)}__"
        mapping[token] = m.group(0)
        return token

    protected = _CODE_RE.sub(_sub, text)
    return protected, mapping


def restore_codes(text: str, mapping: dict[str, str]) -> str:
    for token, code in mapping.items():
        text = text.replace(token, code)
    return text


class Cache:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict[str, str] = {}
        self._lock = threading.Lock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self.data[key] = value

    def save(self) -> None:
        with self._lock:
            snapshot = dict(self.data)
        # Serialize/write outside the lock so a slow disk doesn't block other threads.
        self.path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class OllamaTranslator:
    def __init__(self, model: str, cache_path: str, retries: int = 3,
                 translate_attempts: int = 3):
        self.model = model
        self.cache = Cache(cache_path)
        self.retries = retries  # HTTP-level retries (network/connection failures)
        self.translate_attempts = translate_attempts  # quality-check retries
        self._glossary_block = ""

    def set_glossary(self, glossary: dict[str, str]) -> None:
        """Names/proper nouns translated once up front, fed back into every
        later request's system prompt so the same name gets the same Korean
        rendering whether it's standalone or embedded inside a sentence."""
        pairs = [f"{src} -> {dst}" for src, dst in glossary.items() if src and dst]
        if not pairs:
            self._glossary_block = ""
            return
        self._glossary_block = (
            "\n\n다음 고유명사(인물명 등)는 문장 속에 나오더라도 항상 아래 "
            "번역을 그대로 사용하세요:\n" + "\n".join(pairs)
        )

    def translate(self, text: str) -> str:
        """Thread-safe: safe to call concurrently from multiple worker threads
        (each unique `text` only ever does real work once; a duplicate that
        arrives while another thread is still translating the same string
        just re-translates it, the second write simply overwrites the first
        with an equivalent result)."""
        if not text.strip():
            return text
        cached = self.cache.get(text)
        if cached is not None:
            return cached

        protected, mapping = protect_codes(text)

        prompt = protected
        candidate = ""
        for attempt in range(self.translate_attempts):
            candidate = collapse_repeated_chars(strip_template_artifacts(self._call_model(prompt)))
            if not _is_bad_translation(protected, candidate):
                break
            # Sharpen the nudge each retry based on what's actually wrong,
            # so attempt 2/3 isn't just asking the same question again.
            if not _looks_translated(text, candidate):
                nudge = "\n\n(위 텍스트를 반드시 한글로 번역해서 출력하세요.)"
            else:
                nudge = ("\n\n(설명이나 이유를 덧붙이지 말고, 게임에 표시될 번역 결과 "
                          "텍스트만 짧게 출력하세요.)")
            prompt = protected + nudge

        if _is_bad_translation(protected, candidate) and not _looks_translated(text, candidate):
            # Still not Korean after every attempt (e.g. the model kept
            # answering in Chinese) -- the original source text is less
            # confusing in-game than wrong-language output.
            candidate = protected

        translated = restore_codes(candidate, mapping)
        self.cache.set(text, translated)
        return translated

    def _call_model(self, protected_text: str) -> str:
        options = dict(_options_for_model(self.model))
        options.setdefault("num_predict", _num_predict_for(protected_text))
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + self._glossary_block},
                {"role": "user", "content": protected_text},
            ],
            "stream": False,
            "options": options,
        }
        last_err = None
        for attempt in range(self.retries):
            try:
                resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
                resp.raise_for_status()
                content = resp.json()["message"]["content"].strip()
                return content
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(
            f"Ollama 호출에 실패했습니다 (모델: {self.model}). "
            f"Ollama가 실행 중인지, 'ollama pull {self.model}'로 모델을 받았는지 확인하세요. "
            f"원인: {last_err}"
        )
