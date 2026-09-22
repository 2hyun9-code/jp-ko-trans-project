import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers  # noqa: E402
from providers import (  # noqa: E402
    BatchMismatch,
    ClaudeProvider,
    DeepL,
    FatalProviderError,
    Gemini,
    GoogleCloud,
    GoogleFree,
    OpenAICompatible,
    Provider,
    ProviderError,
    Refused,
    _make_batches,
    _parse_llm_array,
    create_provider,
    run_api_pass,
)
from translator import Cache  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)


def fake_http(monkeypatch, responses):
    """Makes requests.request return `responses` in order and records calls."""
    calls = []
    it = iter(responses)

    def request(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        return next(it)

    monkeypatch.setattr(providers.requests, "request", request)
    return calls


# ---------------------------------------------------------------- helpers

def test_make_batches_respects_count_and_char_limits():
    items = [(f"s{i}", "x" * 10, {}) for i in range(7)]
    assert [len(b) for b in _make_batches(items, size=3, max_chars=1000)] == [3, 3, 1]
    assert [len(b) for b in _make_batches(items, size=100, max_chars=25)] == [2, 2, 2, 1]


def test_make_batches_keeps_an_oversized_item_alone_instead_of_dropping_it():
    items = [("a", "x" * 50, {}), ("b", "y", {})]
    assert [len(b) for b in _make_batches(items, size=10, max_chars=10)] == [1, 1]


def test_parse_llm_array_accepts_array_wrapped_in_prose_or_fences():
    assert _parse_llm_array('```json\n["가", "나"]\n```', 2) == ["가", "나"]


def test_parse_llm_array_rejects_wrong_count_or_non_json():
    with pytest.raises(BatchMismatch):
        _parse_llm_array('["가"]', 2)
    with pytest.raises(BatchMismatch):
        _parse_llm_array("번역할 수 없습니다", 1)


# ---------------------------------------------------------------- _http

def test_http_auth_error_is_fatal(monkeypatch):
    fake_http(monkeypatch, [FakeResp(403, text="API_KEY_HTTP_REFERRER_BLOCKED")])
    with pytest.raises(FatalProviderError, match="REFERRER"):
        providers._http("GET", "https://x", timeout=1)


def test_http_retries_rate_limit_then_succeeds(monkeypatch):
    calls = fake_http(monkeypatch, [FakeResp(429, text="slow down"), FakeResp(200, {"ok": 1})])
    assert providers._http("GET", "https://x", timeout=1).json() == {"ok": 1}
    assert len(calls) == 2


def test_http_gives_up_after_retries_with_batch_level_error(monkeypatch):
    fake_http(monkeypatch, [FakeResp(503, text="down")] * 4)
    with pytest.raises(ProviderError, match="503"):
        providers._http("GET", "https://x", timeout=1)


def test_http_bad_api_key_400_is_fatal_but_other_400_is_not(monkeypatch):
    fake_http(monkeypatch, [FakeResp(400, text='{"error": "API key not valid"}')])
    with pytest.raises(FatalProviderError):
        providers._http("GET", "https://x", timeout=1)
    fake_http(monkeypatch, [FakeResp(400, text="text too long")])
    with pytest.raises(ProviderError):
        providers._http("GET", "https://x", timeout=1)


# ---------------------------------------------------------------- engines

def test_google_free_joins_segments(monkeypatch):
    calls = fake_http(monkeypatch, [FakeResp(200, [[["안녕", "こん"], ["하세요", "にちは"]]])])
    assert GoogleFree().translate_batch(["こんにちは"]) == ["안녕하세요"]
    assert calls[0]["params"]["tl"] == "ko"


def test_google_cloud_sends_key_and_batch(monkeypatch):
    calls = fake_http(monkeypatch, [FakeResp(200, {"data": {"translations": [
        {"translatedText": "가"}, {"translatedText": "나"}]}})])
    out = GoogleCloud(key="K").translate_batch(["あ", "い"])
    assert out == ["가", "나"]
    assert calls[0]["params"] == {"key": "K"}
    assert calls[0]["json"]["q"] == ["あ", "い"]


def test_deepl_picks_free_or_pro_host_from_key(monkeypatch):
    calls = fake_http(monkeypatch, [FakeResp(200, {"translations": [{"text": "가"}]})] * 2)
    DeepL(key="abc:fx").translate_batch(["あ"])
    DeepL(key="abc").translate_batch(["あ"])
    assert "api-free.deepl.com" in calls[0]["url"]
    assert "//api.deepl.com" in calls[1]["url"]
    assert calls[0]["headers"]["Authorization"] == "DeepL-Auth-Key abc:fx"


def test_openai_compat_url_building_and_no_auth_header_without_key(monkeypatch):
    calls = fake_http(monkeypatch, [FakeResp(200, {"choices": [
        {"message": {"content": '["가"]'}, "finish_reason": "stop"}]})])
    p = OpenAICompatible(model="m", base_url="http://localhost:11434/v1/")
    assert p.translate_batch(["あ"]) == ["가"]
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in calls[0]["headers"]


def test_openai_compat_content_filter_is_refusal(monkeypatch):
    fake_http(monkeypatch, [FakeResp(200, {"choices": [
        {"message": {"content": ""}, "finish_reason": "content_filter"}]})])
    with pytest.raises(Refused):
        OpenAICompatible(model="m").translate_batch(["あ"])


def test_gemini_blocked_candidate_is_refusal(monkeypatch):
    fake_http(monkeypatch, [FakeResp(200, {"candidates": [{"finishReason": "SAFETY"}]})])
    with pytest.raises(Refused):
        Gemini(key="k", model="g").translate_batch(["あ"])


def test_gemini_skips_thought_parts(monkeypatch):
    fake_http(monkeypatch, [FakeResp(200, {"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": "생각중...", "thought": True}, {"text": '["가"]'}]}}]})])
    assert Gemini(key="k", model="g").translate_batch(["あ"]) == ["가"]


class _FakeClaudeClient:
    def __init__(self, response):
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.calls.append(("messages", kw))
                return response

        class _BetaMessages:
            def create(self, **kw):
                outer.calls.append(("beta", kw))
                return response

        self.messages = _Messages()
        self.beta = types.SimpleNamespace(messages=_BetaMessages())


def _claude_resp(text, stop_reason="end_turn"):
    return types.SimpleNamespace(stop_reason=stop_reason,
                                 content=[types.SimpleNamespace(type="text", text=text)])


def test_claude_opus5_uses_server_side_fallback_and_effort(monkeypatch):
    client = _FakeClaudeClient(_claude_resp('["가"]'))
    p = ClaudeProvider(key="k")
    monkeypatch.setattr(p, "_get_client", lambda: client)
    assert p.model == "claude-opus-5"
    assert p.translate_batch(["あ"]) == ["가"]
    kind, kw = client.calls[0]
    assert kind == "beta"
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"effort": "medium"}


def test_claude_other_model_uses_plain_endpoint_without_effort_when_unsupported(monkeypatch):
    client = _FakeClaudeClient(_claude_resp('["가"]'))
    p = ClaudeProvider(key="k", model="claude-haiku-4-5")
    monkeypatch.setattr(p, "_get_client", lambda: client)
    p.translate_batch(["あ"])
    kind, kw = client.calls[0]
    assert kind == "messages"
    assert "fallbacks" not in kw and "output_config" not in kw


def test_claude_refusal_stop_reason_raises_refused(monkeypatch):
    client = _FakeClaudeClient(_claude_resp("", stop_reason="refusal"))
    p = ClaudeProvider(key="k")
    monkeypatch.setattr(p, "_get_client", lambda: client)
    with pytest.raises(Refused):
        p.translate_batch(["あ"])


def test_validate_reports_missing_config():
    with pytest.raises(FatalProviderError, match="키"):
        DeepL().validate()
    with pytest.raises(FatalProviderError, match="모델"):
        OpenAICompatible(model="").validate()
    GoogleFree().validate()  # needs nothing


def test_glossary_only_reaches_llm_engines():
    llm, mt = OpenAICompatible(model="m"), DeepL(key="k")
    llm.set_glossary({"アリス": "앨리스"})
    mt.set_glossary({"アリス": "앨리스"})
    assert "アリス -> 앨리스" in llm.llm_system_prompt()
    assert mt.glossary_block == ""


def test_create_provider_unknown_id():
    with pytest.raises(FatalProviderError):
        create_provider("nope")


# ---------------------------------------------------------------- runner

class ScriptedProvider(Provider):
    id = "fake"
    label = "fake"
    batch_size = 10
    workers = 1

    def __init__(self, behavior):
        super().__init__()
        self.behavior = behavior
        self.batches = []

    def translate_batch(self, texts):
        self.batches.append(list(texts))
        return self.behavior(texts)


def _run(provider, texts, tmp_path, **kw):
    cache = Cache(str(tmp_path / "cache.json"))
    res = run_api_pass(provider, texts, cache, log=lambda m: None, **kw)
    return res, cache


def test_run_api_pass_caches_good_results_with_origin_and_restores_codes(tmp_path):
    p = ScriptedProvider(lambda ts: [t.replace("こんにちは", "안녕하세요") for t in ts])
    res, cache = _run(p, ["\\N[1]こんにちは"], tmp_path)
    assert res.ok == 1 and res.failed == []
    assert cache.get("\\N[1]こんにちは") == "\\N[1]안녕하세요"
    assert cache.get_origin("\\N[1]こんにちは") == "api:fake"


def test_run_api_pass_sends_bad_results_back_as_failed(tmp_path):
    p = ScriptedProvider(lambda ts: ["안녕하세요" for _ in ts])  # drops the code token
    res, cache = _run(p, ["\\N[1]こんにちは"], tmp_path)
    assert res.failed == ["\\N[1]こんにちは"]
    assert res.reasons["\\N[1]こんにちは"] == "code_lost"
    assert cache.get("\\N[1]こんにちは") is None


def test_run_api_pass_splits_mismatched_batch_into_single_items(tmp_path):
    def behavior(ts):
        if len(ts) > 1:
            raise BatchMismatch("count")
        return ["번역"]
    p = ScriptedProvider(behavior)
    res, _ = _run(p, ["あ", "い", "う"], tmp_path)
    assert res.ok == 3
    assert [len(b) for b in p.batches] == [3, 1, 1, 1]


def test_run_api_pass_refused_goes_to_failed_or_splits_when_asked(tmp_path):
    def behavior(ts):
        if len(ts) > 1 or ts[0] == "だめ":
            raise Refused("no")
        return ["괜찮아"]
    res, _ = _run(ScriptedProvider(behavior), ["いい", "だめ"], tmp_path)
    assert sorted(res.failed) == ["いい", "だめ"]
    assert set(res.reasons.values()) == {"refused"}

    res, _ = _run(ScriptedProvider(behavior), ["いい", "だめ"], tmp_path, split_refused=True)
    assert res.ok == 1 and res.failed == ["だめ"]


def test_run_api_pass_fatal_error_stops_further_batches(tmp_path):
    p = ScriptedProvider(lambda ts: (_ for _ in ()).throw(FatalProviderError("bad key")))
    p.batch_size = 1
    res, _ = _run(p, ["あ", "い", "う"], tmp_path)
    assert res.fatal == "bad key"
    assert len(p.batches) == 1, "no more requests after a fatal error"
    assert sorted(res.failed) == ["あ", "い", "う"]


def test_run_api_pass_gives_up_on_api_after_consecutive_outage(tmp_path):
    def behavior(ts):
        raise ProviderError("HTTP 429: Sorry")
    p = ScriptedProvider(behavior)
    p.batch_size = 1
    texts = [f"文{i}" for i in range(20)]
    res, _ = _run(p, texts, tmp_path)
    assert len(p.batches) == providers.MAX_CONSECUTIVE_FAILURES
    assert "연속 실패" in res.fatal and "429" in res.fatal
    assert sorted(res.failed) == sorted(texts)


def test_run_api_pass_success_resets_outage_streak(tmp_path):
    calls = {"n": 0}

    def behavior(ts):
        calls["n"] += 1
        if calls["n"] % 4 == 0:
            return ["번역"]
        raise ProviderError("flaky")
    p = ScriptedProvider(behavior)
    p.batch_size = 1
    res, _ = _run(p, [f"文{i}" for i in range(12)], tmp_path)
    assert res.fatal is None, "3 failures then a success never reaches the limit"
    assert len(p.batches) == 12


def test_snippet_strips_html_block_pages():
    resp = FakeResp(429, text="<html><head><title>Sorry...</title></head><body><b>blocked</b></body></html>")
    assert providers._snippet(resp) == "Sorry... blocked"


def test_run_api_pass_cancel_raises_interrupted(tmp_path):
    p = ScriptedProvider(lambda ts: ["가" for _ in ts])
    with pytest.raises(InterruptedError):
        _run(p, ["あ"], tmp_path, should_cancel=lambda: True)
