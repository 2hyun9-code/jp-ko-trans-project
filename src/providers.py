"""API translation engines for the fast first pass.

Every engine takes a list of *protected* strings (RPG Maker codes already
swapped for __T0__-style tokens by translator.protect_codes) and returns
one translation per input. run_api_pass() batches, checks each result with
translator.api_result_problem(), caches the good ones, and hands back what
failed so the pipeline can give it to the local model instead.

Failure modes, from least to most serious:
- BatchMismatch: an LLM answered but not one-translation-per-input; the
  batch is retried item by item.
- Refused / ProviderError: this batch didn't work (content filter, rate
  limit that outlasted retries, network); its items go to the local model.
- FatalProviderError: retrying won't help (bad key, quota gone, wrong
  model name); the API is dropped for the rest of the run.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from translator import SYSTEM_PROMPT, api_result_problem, protect_codes, restore_codes


class ProviderError(Exception):
    pass


class BatchMismatch(ProviderError):
    pass


class Refused(ProviderError):
    pass


class FatalProviderError(Exception):
    pass


_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(resp: requests.Response) -> str:
    text = resp.text or ""
    if "<html" in text[:200].lower():
        # e.g. Google's "Sorry..." rate-limit page -- the markup is noise.
        text = _TAG_RE.sub(" ", text)
    return " ".join(text.split())[:200]


def _http(method: str, url: str, *, timeout: float, retries: int = 4, **kw) -> requests.Response:
    """requests + the retry/classification every engine needs: 429/5xx and
    network errors are retried with backoff; auth/quota/not-found errors are
    fatal; other 4xx fail just this batch."""
    last = ""
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = f"네트워크 오류: {e}"
            time.sleep(min(2 ** attempt, 30))
            continue
        code = resp.status_code
        if code < 400:
            return resp
        if code in (401, 403):
            raise FatalProviderError(f"인증/권한 오류 (HTTP {code}): {_snippet(resp)}")
        if code == 404:
            raise FatalProviderError(f"주소나 모델 이름을 찾을 수 없습니다 (HTTP 404): {_snippet(resp)}")
        if code == 456:
            raise FatalProviderError("사용량 한도를 다 썼습니다 (HTTP 456).")
        if code == 400 and "api key" in resp.text.lower():
            raise FatalProviderError(f"API 키가 올바르지 않습니다: {_snippet(resp)}")
        if code == 429 or code >= 500:
            last = f"HTTP {code}: {_snippet(resp)}"
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** attempt
            time.sleep(min(max(wait, 1), 30))
            continue
        raise ProviderError(f"HTTP {code}: {_snippet(resp)}")
    raise ProviderError(last or "알 수 없는 오류")


_LLM_BATCH_RULES = (
    "\n\n입력은 JSON 문자열 배열입니다. 각 문자열을 따로 번역해서, 같은 순서·같은 개수의 "
    "JSON 문자열 배열 하나만 출력하세요. 배열 앞뒤에 다른 글자를 쓰지 마세요."
)
_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def _parse_llm_array(text: str, n: int) -> list[str]:
    m = _ARRAY_RE.search(text or "")
    try:
        arr = json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        raise BatchMismatch("JSON 배열로 답하지 않았습니다") from None
    if not isinstance(arr, list) or len(arr) != n:
        got = len(arr) if isinstance(arr, list) else "?"
        raise BatchMismatch(f"응답 개수가 다릅니다 ({got}/{n})")
    return ["" if x is None else str(x) for x in arr]


class Provider:
    id = ""
    label = ""
    short_label = ""
    note = ""
    needs_key = False
    needs_base_url = False
    is_llm = False
    default_model = ""
    default_base_url = ""
    model_hint = ""
    batch_size = 1
    max_batch_chars = 4000
    workers = 4

    def __init__(self, key: str = "", model: str = "", base_url: str = "") -> None:
        self.key = (key or "").strip()
        self.model = (model or self.default_model).strip()
        self.base_url = (base_url or self.default_base_url).strip().rstrip("/")
        self.glossary_block = ""

    def validate(self) -> None:
        if self.needs_key and not self.key:
            raise FatalProviderError(f"{self.label}: API 키를 먼저 입력하세요 (API 설정).")
        if self.is_llm and not self.model:
            raise FatalProviderError(f"{self.label}: 모델 이름을 먼저 입력하세요 (API 설정).")
        if self.needs_base_url and not self.base_url:
            raise FatalProviderError(f"{self.label}: 주소를 먼저 입력하세요 (API 설정).")

    def set_glossary(self, glossary: dict[str, str]) -> None:
        """Only LLM engines can take instructions; plain MT engines ignore it."""
        pairs = [f"{s} -> {d}" for s, d in glossary.items() if s and d]
        if not self.is_llm or not pairs:
            self.glossary_block = ""
            return
        self.glossary_block = ("\n\n다음 고유명사(인물명 등)는 문장 속에 나오더라도 항상 아래 "
                               "번역을 그대로 사용하세요:\n" + "\n".join(pairs))

    def llm_system_prompt(self) -> str:
        return SYSTEM_PROMPT + _LLM_BATCH_RULES + self.glossary_block

    def translate_batch(self, texts: list[str]) -> list[str]:
        raise NotImplementedError


class GoogleFree(Provider):
    id = "google_free"
    label = "구글 번역 (무료, 키 없음)"
    short_label = "구글 무료"
    note = "키 없이 바로 쓸 수 있어요. 비공식 경로라 한 번에 많이 돌리면 잠시 막힐 수 있습니다."
    URL = "https://translate.googleapis.com/translate_a/single"
    batch_size = 1
    workers = 4

    def translate_batch(self, texts: list[str]) -> list[str]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> str:
        if len(text) > 1800:
            raise ProviderError("문장이 너무 길어 무료 경로로 보낼 수 없습니다")
        resp = _http("GET", self.URL, timeout=30,
                     params={"client": "gtx", "sl": "ja", "tl": "ko", "dt": "t", "q": text})
        try:
            return "".join(seg[0] for seg in resp.json()[0] if seg and seg[0])
        except (ValueError, TypeError, IndexError):
            raise ProviderError("구글 응답 형식이 예상과 다릅니다") from None


class GoogleCloud(Provider):
    id = "google_cloud"
    label = "Google Cloud 번역 (공식 API 키)"
    short_label = "Google Cloud"
    note = ("Google Cloud 콘솔의 Cloud Translation API 키. 키에 'HTTP 리퍼러(웹사이트)' 제한이 "
            "걸려 있으면 이 프로그램에서는 막힙니다 — 제한을 '없음'이나 'IP 주소'로 바꾸세요.")
    URL = "https://translation.googleapis.com/language/translate/v2"
    needs_key = True
    batch_size = 100
    max_batch_chars = 5000
    workers = 4

    def translate_batch(self, texts: list[str]) -> list[str]:
        resp = _http("POST", self.URL, timeout=60, params={"key": self.key},
                     json={"q": texts, "source": "ja", "target": "ko", "format": "text"})
        try:
            out = [t["translatedText"] for t in resp.json()["data"]["translations"]]
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Google Cloud 응답 형식이 예상과 다릅니다") from None
        if len(out) != len(texts):
            raise ProviderError("Google Cloud 응답 개수가 다릅니다")
        return out


class DeepL(Provider):
    id = "deepl"
    label = "DeepL"
    short_label = "DeepL"
    note = "deepl.com 에서 API 키 발급 (무료 키는 끝이 ':fx', 월 50만 자까지)."
    needs_key = True
    batch_size = 50
    max_batch_chars = 20000
    workers = 2

    def _url(self) -> str:
        host = "api-free.deepl.com" if self.key.endswith(":fx") else "api.deepl.com"
        return f"https://{host}/v2/translate"

    def translate_batch(self, texts: list[str]) -> list[str]:
        resp = _http("POST", self._url(), timeout=60,
                     headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
                     json={"text": texts, "source_lang": "JA", "target_lang": "KO"})
        try:
            out = [t["text"] for t in resp.json()["translations"]]
        except (ValueError, KeyError, TypeError):
            raise ProviderError("DeepL 응답 형식이 예상과 다릅니다") from None
        if len(out) != len(texts):
            raise ProviderError("DeepL 응답 개수가 다릅니다")
        return out


class OpenAICompatible(Provider):
    id = "openai_compat"
    label = "OpenAI 호환 (OpenAI·OpenRouter·LM Studio 등)"
    short_label = "OpenAI 호환"
    note = ("OpenAI 방식으로 받는 서버면 다 됩니다. 주소는 .../v1 까지만 넣으세요. "
            "키가 필요 없는 로컬 서버는 키 칸을 비워 두세요.")
    needs_base_url = True
    is_llm = True
    default_base_url = "https://api.openai.com/v1"
    model_hint = "그 서비스에서 쓰는 모델 이름 그대로"
    batch_size = 20
    max_batch_chars = 3000
    workers = 2

    def _url(self) -> str:
        b = self.base_url
        if b.endswith("/chat/completions"):
            return b
        return b + ("/chat/completions" if b.endswith("/v1") else "/v1/chat/completions")

    def translate_batch(self, texts: list[str]) -> list[str]:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {"model": self.model, "messages": [
            {"role": "system", "content": self.llm_system_prompt()},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ]}
        resp = _http("POST", self._url(), timeout=180, headers=headers, json=body)
        try:
            choice = resp.json()["choices"][0]
            content = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError(f"응답 형식이 OpenAI와 다릅니다: {_snippet(resp)}") from None
        if choice.get("finish_reason") == "content_filter":
            raise Refused("콘텐츠 필터에 걸렸습니다")
        return _parse_llm_array(content, len(texts))


class ClaudeProvider(Provider):
    id = "claude"
    label = "Claude (Anthropic API 키)"
    short_label = "Claude"
    note = ("console.anthropic.com 에서 키 발급. 거절된 묶음은 서버에서 다른 Claude 모델이 "
            "이어받고, 그래도 안 되면 로컬 모델이 번역합니다.")
    needs_key = True
    is_llm = True
    default_model = "claude-opus-5"
    model_hint = "예: claude-opus-5"
    batch_size = 20
    max_batch_chars = 4000
    workers = 2
    # Server-side refusal fallback ("fallbacks": "default") is offered on these.
    _FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5", "claude-fable-5-1"}
    # Models that accept output_config.effort (older ones reject it with a 400).
    _EFFORT_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                        "claude-sonnet-4-6")

    def __init__(self, key: str = "", model: str = "", base_url: str = "") -> None:
        super().__init__(key, model, base_url)
        self._client = None
        self._client_lock = threading.Lock()

    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.key, max_retries=3, timeout=180.0)
            return self._client

    def translate_batch(self, texts: list[str]) -> list[str]:
        import anthropic

        kwargs = {
            "model": self.model,
            "max_tokens": 16000,
            "system": self.llm_system_prompt(),
            "messages": [{"role": "user", "content": json.dumps(texts, ensure_ascii=False)}],
        }
        if self.model.startswith(self._EFFORT_PREFIXES):
            # Game-line translation isn't reasoning-heavy; medium keeps quality
            # while not paying for the default "high" on tens of thousands of lines.
            kwargs["output_config"] = {"effort": "medium"}
        client = self._get_client()
        try:
            if self.model in self._FALLBACK_MODELS:
                resp = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise FatalProviderError(f"Claude 인증 오류: {e.message}") from None
        except anthropic.NotFoundError as e:
            raise FatalProviderError(f"Claude 모델을 찾을 수 없습니다 ({self.model}): {e.message}") from None
        except anthropic.BadRequestError as e:
            raise FatalProviderError(f"Claude 요청 오류: {e.message}") from None
        except anthropic.RateLimitError as e:
            raise ProviderError(f"Claude 요청 한도 초과: {e.message}") from None
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude 서버 오류 ({e.status_code}): {e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Claude 연결 오류: {e}") from None
        if resp.stop_reason == "refusal":
            raise Refused("Claude가 이 묶음을 거절했습니다")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_llm_array(text, len(texts))


class Gemini(Provider):
    id = "gemini"
    label = "Gemini (Google AI Studio 키)"
    short_label = "Gemini"
    note = "aistudio.google.com 에서 키 발급. 모델 이름은 AI Studio 목록에 있는 그대로 넣으세요."
    BASE = "https://generativelanguage.googleapis.com/v1beta"
    needs_key = True
    is_llm = True
    model_hint = "AI Studio 모델 목록의 이름"
    batch_size = 20
    max_batch_chars = 4000
    workers = 2
    _BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
    _SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in (
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]

    def translate_batch(self, texts: list[str]) -> list[str]:
        body = {
            "systemInstruction": {"parts": [{"text": self.llm_system_prompt()}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(texts, ensure_ascii=False)}]}],
            "safetySettings": self._SAFETY,
        }
        resp = _http("POST", f"{self.BASE}/models/{self.model}:generateContent", timeout=180,
                     headers={"x-goog-api-key": self.key}, json=body)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("Gemini 응답 형식이 예상과 다릅니다") from None
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise Refused("Gemini가 이 묶음을 차단했습니다")
        cands = data.get("candidates") or []
        if not cands or cands[0].get("finishReason") in self._BLOCKED:
            raise Refused("Gemini가 이 묶음을 차단했습니다")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return _parse_llm_array(text, len(texts))


PROVIDERS: dict[str, type[Provider]] = {
    cls.id: cls for cls in (GoogleFree, GoogleCloud, DeepL, OpenAICompatible, ClaudeProvider, Gemini)
}


def create_provider(provider_id: str, key: str = "", model: str = "", base_url: str = "") -> Provider:
    try:
        cls = PROVIDERS[provider_id]
    except KeyError:
        raise FatalProviderError(f"알 수 없는 번역 엔진: {provider_id}") from None
    return cls(key, model, base_url)


# --------------------------------------------------------------------------
# Batch runner
# --------------------------------------------------------------------------
MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class ApiPassResult:
    failed: list[str] = field(default_factory=list)      # sources still untranslated
    reasons: dict[str, str] = field(default_factory=dict)  # source -> PROBLEM_LABELS key
    ok: int = 0
    fatal: Optional[str] = None
    errors: dict[str, int] = field(default_factory=dict)   # distinct error message -> count


def _make_batches(items: list[tuple[str, str, dict]], size: int, max_chars: int) -> list[list]:
    batches: list[list] = []
    cur: list = []
    chars = 0
    for item in items:
        n = len(item[1])
        if cur and (len(cur) >= size or chars + n > max_chars):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += n
    if cur:
        batches.append(cur)
    return batches


def run_api_pass(
    provider: Provider,
    texts: list[str],
    cache,
    log: Callable[[str], None],
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    label: str = "",
    split_refused: bool = False,
) -> ApiPassResult:
    """Translates `texts` with `provider`, caching every result that passes
    api_result_problem() under origin "api:<id>". Everything else is
    returned in `.failed`. `split_refused` retries a refused batch line by
    line (worth it when there's no local model to fall back on)."""
    result = ApiPassResult()
    if not texts:
        return result
    items = [(src, *protect_codes(src)) for src in texts]
    batches = _make_batches(items, provider.batch_size, provider.max_batch_chars)
    stop = threading.Event()
    err_lock = threading.Lock()
    streak = [0]  # consecutive batches that failed with ProviderError

    def note_error(msg: str) -> None:
        with err_lock:
            result.errors[msg] = result.errors.get(msg, 0) + 1

    def note_outage(msg: str) -> None:
        """A blocked endpoint (e.g. Google's free path answering every request
        with a 429 "Sorry" page) would otherwise burn retries on every one of
        tens of thousands of lines before the local model got a turn."""
        with err_lock:
            streak[0] += 1
            if streak[0] >= MAX_CONSECUTIVE_FAILURES and not stop.is_set():
                result.fatal = (f"API 요청이 {streak[0]}번 연속 실패해서 이번 실행에서는 "
                                f"API를 끕니다 (마지막 오류: {msg})")
                stop.set()

    def run(batch: list) -> list[tuple[str, Optional[str], Optional[str]]]:
        if stop.is_set():
            return [(src, None, "api_stopped") for src, _, _ in batch]
        try:
            outs = provider.translate_batch([p for _, p, _ in batch])
            with err_lock:
                streak[0] = 0
        except BatchMismatch as e:
            if len(batch) > 1:
                return [r for item in batch for r in run([item])]
            note_error(str(e))
            return [(batch[0][0], None, "odd")]
        except Refused as e:
            if split_refused and len(batch) > 1:
                return [r for item in batch for r in run([item])]
            note_error(str(e))
            return [(src, None, "refused") for src, _, _ in batch]
        except FatalProviderError as e:
            if not stop.is_set():
                result.fatal = str(e)
            stop.set()
            return [(src, None, "api_stopped") for src, _, _ in batch]
        except ProviderError as e:
            note_error(str(e))
            note_outage(str(e))
            return [(src, None, "api_error") for src, _, _ in batch]
        rows = []
        for (src, protected, mapping), out in zip(batch, outs):
            problem = api_result_problem(protected, out, mapping)
            rows.append((src, None if problem else restore_codes(out, mapping), problem))
        return rows

    total = len(items)
    done = 0
    cancelled = False
    next_log = max(1, total // 20)
    with ThreadPoolExecutor(max_workers=max(1, provider.workers)) as pool:
        futures = [pool.submit(run, b) for b in batches]
        try:
            for fut in as_completed(futures):
                if should_cancel and should_cancel() and not cancelled:
                    cancelled = True
                    stop.set()
                    for f in futures:
                        f.cancel()
                if fut.cancelled():
                    continue
                for src, translated, problem in fut.result():
                    if translated is not None:
                        cache.set(src, translated, origin=f"api:{provider.id}")
                        result.ok += 1
                    else:
                        result.failed.append(src)
                        result.reasons[src] = problem or "api_error"
                    done += 1
                if progress:
                    progress(done, total)
                if done >= next_log or done == total:
                    cache.save()
                    log(f"  {label} API {done}/{total}")
                    next_log = done + max(1, total // 20)
        finally:
            cache.save()

    if cancelled:
        log("사용자가 취소했습니다. 지금까지의 번역은 캐시에 저장되었습니다.")
        raise InterruptedError("cancelled")
    return result
