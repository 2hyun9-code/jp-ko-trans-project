import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
import review  # noqa: E402
from translator import Cache, OllamaTranslator  # noqa: E402


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                    encoding="utf-8")


def _make_game(root: Path) -> Path:
    d = root / "data"
    _write(d / "System.json", {"gameTitle": "テスト", "terms": {
        "basic": [], "commands": [], "params": [], "messages": {}}})
    _write(d / "Items.json", [None, {"id": 1, "name": "ポーション", "description": "", "note": ""}])
    _write(d / "Map001.json", {"events": [None, {"id": 1, "name": "", "note": "", "pages": [{"list": [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 401, "indent": 0, "parameters": ["おはよう。"]},
        {"code": 401, "indent": 0, "parameters": ["失敗する行"]},
        {"code": 355, "indent": 0, "parameters": ['$dataItems.find(i => i.name === "ポーション")']},
        {"code": 0, "indent": 0, "parameters": []},
    ]}]}]})
    for name in ["Actors", "Classes", "Skills", "Weapons", "Armors", "Enemies", "States",
                 "CommonEvents", "Troops", "MapInfos"]:
        _write(d / f"{name}.json", "[null]")
    _write(root / "index.html", '<html><body><script src="js/main.js"></script></body></html>')
    return root


def _fake_model(self, text):
    # The paragraph containing 失敗 never comes back as Korean -> kept as source.
    return "fail" if "失敗" in text else "번역됨"


@pytest.fixture
def translated_game(tmp_path, monkeypatch):
    monkeypatch.setattr(OllamaTranslator, "_call_model", _fake_model)
    out = tmp_path / "g_KO"
    cache_path = tmp_path / "c.json"
    pipeline.run_all(game=str(_make_game(tmp_path / "g")), out=str(out), font=None,
                     model="local-m", cache_path=str(cache_path), log=lambda m: None, workers=1)
    return out, cache_path


PARA = "おはよう。\n失敗する行"


def _map_lines(out: Path) -> list[str]:
    data = json.loads((out / "data" / "Map001.json").read_text(encoding="utf-8"))
    return [c["parameters"][0] for c in data["events"][1]["pages"][0]["list"] if c["code"] == 401]


# ---------------------------------------------------------------- labels

def test_origin_kind_and_label():
    assert review.origin_kind("api:deepl", "가") == "api"
    assert review.origin_kind("local:m", "가") == "local"
    assert review.origin_kind("manual", "가") == "manual"
    assert review.origin_kind(None, "가") == "unknown"
    assert review.origin_kind("api:deepl", "") == "untranslated"
    assert review.origin_label("api:deepl", "가") == "API · DeepL"
    assert review.origin_label("local:hf.co/x/ja-ko-vn-12b:Q5", "가") == "로컬 · ja-ko-vn-12b"


# ---------------------------------------------------------------- session / rows

def test_session_reads_meta_and_review_list(translated_game):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    assert PARA in s.texts
    assert s.flagged == {PARA: "fallback"}
    assert "ポーション" in s.guarded
    assert s.local_model == "local-m"


def test_session_without_meta_falls_back_to_cache_keys(tmp_path):
    cache = Cache(str(tmp_path / "c.json"))
    cache.set("こんにちは", "안녕")
    cache.save()
    s = review.open_session(str(tmp_path), str(tmp_path / "c.json"))
    assert s.texts == ["こんにちは"] and s.flagged == {} and s.guarded == set()


def test_rows_filters(translated_game):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    assert [r["source"] for r in review.rows(s, "flagged")] == [PARA]
    all_rows = review.rows(s, "all")
    assert {r["source"] for r in all_rows} >= {PARA, "ポーション", "テスト"}
    assert all(r["kind"] == "local" for r in review.rows(s, "all", "local"))
    assert [r["source"] for r in review.rows(s, "all", query="ポーション")] == ["ポーション"]
    assert review.rows(s, "all", query="번역됨")  # searches translations too
    assert review.rows(s, "all", "manual") == []
    assert next(r for r in all_rows if r["source"] == "ポーション")["guarded"] is True


# ---------------------------------------------------------------- edits

def test_manual_edit_then_apply_writes_output_and_clears_flag(translated_game):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    pending: dict = {}
    review.set_manual(s, PARA, "좋은 아침.\n직접 고친 줄", pending)
    assert pending == {PARA: PARA}, "output held the source (fallback) before"
    assert s.cache.get_origin(PARA) == "manual"

    assert review.apply_changes(s, pending) == 1
    assert _map_lines(out) == ["좋은 아침.", "직접 고친 줄"]
    assert s.flagged == {}
    on_disk_review = json.loads((out / review.REVIEW_FILENAME).read_text(encoding="utf-8"))
    assert on_disk_review == []


def test_guarded_edit_updates_render_patch_but_not_data(translated_game):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    pending: dict = {}
    review.set_manual(s, "ポーション", "포션", pending)
    assert review.apply_changes(s, pending) == 0
    items = json.loads((out / "data" / "Items.json").read_text(encoding="utf-8"))
    assert items[1]["name"] == "ポーション"
    assert '"ポーション": "포션"' in (out / "js" / "korean_patch.js").read_text(encoding="utf-8")


def test_retranslate_local_then_apply(translated_game, monkeypatch):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    monkeypatch.setattr(OllamaTranslator, "_call_model", lambda self, t: "좋은 아침.\n고쳐진 줄")
    seen = []
    changes, error = review.retranslate_local(s, [PARA], "local-m", workers=1,
                                             progress=lambda d, t: seen.append((d, t)))
    assert error is None
    assert changes == {PARA: PARA}
    assert s.cache.get(PARA) == "좋은 아침.\n고쳐진 줄"
    assert s.cache.get_origin(PARA) == "local:local-m"
    assert seen == [(1, 1)]
    review.apply_changes(s, changes)
    assert _map_lines(out) == ["좋은 아침.", "고쳐진 줄"]


def test_retranslate_local_stops_on_failure_and_keeps_old_values(translated_game, monkeypatch):
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    old = s.cache.get("テスト")

    def boom(self, t):
        raise RuntimeError("Ollama 호출에 실패했습니다")
    monkeypatch.setattr(OllamaTranslator, "_call_model", boom)
    changes, error = review.retranslate_local(s, ["テスト", PARA], "local-m", workers=1)
    assert changes == {}
    assert "Ollama" in error
    assert s.cache.get("テスト") == old, "a failed re-translation must not lose the old value"


def test_apply_matches_paragraph_split_back_with_padding(translated_game):
    """A translation with fewer lines than the message got padded with a
    blank line on disk; the next edit must still find it."""
    out, cache_path = translated_game
    s = review.open_session(str(out), str(cache_path))
    pending: dict = {}
    review.set_manual(s, PARA, "한 줄뿐", pending)
    review.apply_changes(s, pending)
    assert _map_lines(out) == ["한 줄뿐", ""]

    pending = {}
    review.set_manual(s, PARA, "다시\n두 줄", pending)
    review.apply_changes(s, pending)
    assert _map_lines(out) == ["다시", "두 줄"]
