import json
import re
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
import review  # noqa: E402
import tyrano_engine as ty  # noqa: E402
from engine import detect_project  # noqa: E402
from translator import OllamaTranslator, protect_codes  # noqa: E402

SCENARIO = """\
; コメントは訳さない
*start|はじめに
[cm]
@chara_new name="akane" storage="a.png" jname="あかね"
#akane:happy
こんにちは、[emb exp="f.name"]さん。[l][r]
今日は[ruby text="よ"]良い天気ですね。[p]
#通行人
　「おはよう」[p]
[glink text="はい" target="*yes"][glink text='いいえ' target=*no]
[glink text="&f.choice" target="*x"]
@ptext text=テスト layer=0
[iscript]
f.msg = "スクリプト内の文字列";
[endscript]
/*
ブロックコメント
*/
&f.name
"""


def _files(text=SCENARIO, encoding="utf-8", crlf=False, bom=False):
    if crlf:
        text = text.replace("\n", "\r\n")
    raw = text.encode(encoding)
    return {"first.ks": (b"\xef\xbb\xbf" if bom else b"") + raw}


def test_scan_finds_text_names_and_display_attributes_only():
    got = ty.scan(_files())
    assert got.texts == ["あかね", 'こんにちは、[emb exp="f.name"]さん。', "今日は良い天気ですね。",
                         "通行人", "「おはよう」", "はい", "いいえ", "テスト"]
    # "#akane" is a chara_new key (the face lookup), not display text.
    assert got.names == ["あかね", "通行人"]


@pytest.mark.parametrize("crlf,bom", [(False, False), (True, True)])
def test_identity_render_changes_nothing(crlf, bom):
    assert ty.render(_files(crlf=crlf, bom=bom), {}) == {}


def test_render_puts_translations_back_and_keeps_markup():
    tr = {
        "あかね": "아카네",
        'こんにちは、[emb exp="f.name"]さん。': '안녕, [emb exp="f.name"]씨.',
        "今日は良い天気ですね。": "오늘은 날씨가 좋네요.",
        "通行人": "행인",
        "「おはよう」": "「좋은 아침」",
        "はい": '"네"',
        "いいえ": "아니요",
        "テスト": "테스트 문자",
    }
    out = ty.render(_files(crlf=True, bom=True), tr)["first.ks"]
    assert out.startswith(b"\xef\xbb\xbf")
    text = out[3:].decode("utf-8")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert lines[3] == '@chara_new name="akane" storage="a.png" jname="아카네"'
    assert lines[4] == "#akane:happy"
    assert lines[5] == '안녕, [emb exp="f.name"]씨.[l][r]'
    assert lines[6] == "오늘은 날씨가 좋네요.[p]"     # ruby dropped with the Japanese it annotated
    assert lines[7] == "#행인"
    assert lines[8] == "　「좋은 아침」[p]"            # indentation kept
    assert lines[9] == """[glink text="”네”" target="*yes"][glink text='아니요' target=*no]"""
    assert lines[10] == '[glink text="&f.choice" target="*x"]'
    assert lines[11] == '@ptext text="테스트 문자" layer=0'
    assert 'f.msg = "スクリプト内の文字列";' in text
    assert "ブロックコメント" in text and "; コメントは訳さない" in text


def test_translation_that_loses_a_tag_falls_back_to_source():
    src = 'こんにちは、[emb exp="f.name"]さん。'
    assert ty.render(_files(), {src: "안녕하세요."}) == {}


def test_translation_cannot_turn_into_tags_labels_or_comments():
    files = {"a.ks": "文章です[p]\n次の文[p]\n".encode("utf-8")}
    out = ty.render(files, {"文章です": "*라벨처럼 [보임]", "次の文": "; 주석처럼"})["a.ks"].decode("utf-8")
    assert out == "＊라벨처럼 ［보임］[p]\n； 주석처럼[p]\n"


def test_shift_jis_file_becomes_utf8_when_hangul_is_added():
    files = {"a.ks": "テキスト[p]\n".encode("cp932")}
    assert ty.scan(files).texts == ["テキスト"]
    assert ty.render(files, {"テキスト": "텍스트"})["a.ks"] == "텍스트[p]\n".encode("utf-8")


def test_tyrano_tags_with_attributes_are_protected_from_the_model():
    protected, mapping = protect_codes('今日は[emb exp="f.name"]と[font size=30]会う[resetfont]')
    assert sorted(mapping.values()) == ['[emb exp="f.name"]', "[font size=30]", "[resetfont]"]
    assert "[" not in protected


# --------------------------------------------------------------------------
# Game layouts
# --------------------------------------------------------------------------
def _tyrano_tree(base: Path) -> None:
    (base / "data" / "scenario").mkdir(parents=True)
    (base / "data" / "system").mkdir(parents=True)
    (base / "data" / "system" / "Config.tjs").write_bytes(";System.title = ゲーム\n".encode("utf-8"))
    (base / "tyrano").mkdir()
    (base / "data" / "scenario" / "first.ks").write_bytes(SCENARIO.encode("utf-8"))
    (base / "data" / "scenario" / "sub").mkdir()
    (base / "data" / "scenario" / "sub" / "ch1.ks").write_bytes("第一章[p]\n".encode("utf-8"))
    (base / "index.html").write_text("<html></html>", encoding="utf-8")


def _zip_bytes(prefix: str = "") -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + "package.json", '{"main":"index.html"}')
        zf.writestr(prefix + "index.html", "<html></html>")
        zf.writestr(prefix + "data/scenario/first.ks", SCENARIO.encode("utf-8"))
        zf.writestr(prefix + "data/scenario/sub/ch1.ks", "第一章[p]\n".encode("utf-8"))
        zf.writestr(prefix + "data/bgimage/bg.png", bytes(range(256)) * 50)
    return buf.getvalue()


def test_detects_folder_game_under_resources_app(tmp_path):
    root = tmp_path / "Game"
    _tyrano_tree(root / "resources" / "app")
    (root / "Game.exe").write_bytes(b"MZ")
    layout = detect_project(str(root))
    assert layout.engine == "TYRANO"
    assert layout.root == root / "resources" / "app" and layout.launch_root == root
    c = ty.find_container(root)
    assert c.kind == "dir" and sorted(c.read_scenarios()) == ["first.ks", "sub/ch1.ks"]


def test_nw_exe_with_appended_zip_is_read_and_rewritten_keeping_the_exe(tmp_path):
    stub = b"MZ" + b"\x90" * 5000
    exe = tmp_path / "Game.exe"
    exe.write_bytes(stub + _zip_bytes())
    assert detect_project(str(tmp_path)).engine == "TYRANO"
    c = ty.find_container(tmp_path)
    assert (c.kind, c.path, c.prefix) == ("zip", exe, "")

    c.write_scenarios({"sub/ch1.ks": "제1장[p]\n".encode("utf-8")})
    data = exe.read_bytes()
    assert data.startswith(stub)
    with zipfile.ZipFile(exe) as zf:
        assert zf.testzip() is None
        assert zf.read("data/scenario/sub/ch1.ks").decode("utf-8") == "제1장[p]\n"
        assert zf.read("data/bgimage/bg.png") == bytes(range(256)) * 50
        assert zf.read("data/scenario/first.ks").decode("utf-8") == SCENARIO
        assert min(i.header_offset for i in zf.infolist()) == len(stub)
    assert not exe.with_name(exe.name + ".jpko_tmp").exists()


def test_package_nw_zip_with_subfolder_prefix(tmp_path):
    (tmp_path / "package.nw").write_bytes(_zip_bytes("www/"))
    c = ty.find_container(tmp_path)
    assert (c.kind, c.prefix) == ("zip", "www/")
    assert sorted(c.read_scenarios()) == ["first.ks", "sub/ch1.ks"]


def test_asar_is_readable_for_glossary_without_extracting(tmp_path):
    from test_asar_tool import _build_fake_asar
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "app.asar").write_bytes(_build_fake_asar({
        "data/scenario/first.ks": SCENARIO.encode("utf-8"), "index.html": b"x"}))
    assert ty.find_container(tmp_path) is None
    c = ty.find_container(tmp_path, include_asar=True)
    assert c.kind == "asar" and ty.scan(c.read_scenarios()).names == ["あかね", "通行人"]
    assert list(tmp_path.rglob("*.ks")) == []


# --------------------------------------------------------------------------
# Pipeline + review window
# --------------------------------------------------------------------------
def _fake_model(self, text):
    # Korean only (anything else fails the quality check), control tokens kept.
    return " ".join(["번역됨"] + re.findall(r"__T\d+__", text))


@pytest.mark.parametrize("packed", [False, True])
def test_run_all_translates_scenarios_and_review_apply_regenerates(tmp_path, monkeypatch, packed):
    monkeypatch.setattr(OllamaTranslator, "_call_model", _fake_model)
    game = tmp_path / "Game"
    if packed:
        game.mkdir()
        (game / "Game.exe").write_bytes(b"MZ" + b"\0" * 100 + _zip_bytes())
    else:
        _tyrano_tree(game)
    original_bytes = {p: p.read_bytes() for p in game.rglob("*") if p.is_file()}
    out = tmp_path / "Game_KO"
    cache_path = tmp_path / "cache.json"

    result = pipeline.run_all(str(game), str(out), None, "m", str(cache_path), log=lambda m: None)

    assert result == str(out)
    assert {p: p.read_bytes() for p in game.rglob("*") if p.is_file()} == original_bytes
    c = ty.find_container(out)
    assert c.kind == ("zip" if packed else "dir")
    translated = c.read_scenarios()
    assert translated["sub/ch1.ks"].decode("utf-8") == "번역됨[p]\n"
    first = translated["first.ks"].decode("utf-8")
    assert "#번역됨\n" in first and "#akane:happy" in first
    assert '번역됨 [emb exp="f.name"][l][r]' in first
    assert (out / ty.ORIGINAL_DIR / "first.ks").read_bytes() == SCENARIO.encode("utf-8")

    meta = json.loads((out / review.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["engine"] == "TYRANO" and meta["tyrano"]["kind"] == c.kind

    session = review.open_session(str(out), str(cache_path))
    assert session.engine == "TYRANO"
    pending = {}
    review.set_manual(session, "第一章", "제1장", pending)
    review.apply_changes(session, pending)
    assert ty.find_container(out).read_scenarios()["sub/ch1.ks"].decode("utf-8") == "제1장[p]\n"


def test_run_all_extracts_electron_asar_tyrano_game(tmp_path, monkeypatch):
    from test_asar_tool import _build_fake_asar
    monkeypatch.setattr(OllamaTranslator, "_call_model", _fake_model)
    game = tmp_path / "Game"
    (game / "resources").mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ")
    (game / "resources" / "app.asar").write_bytes(_build_fake_asar({
        "data/scenario/first.ks": SCENARIO.encode("utf-8"),
        "data/system/Config.tjs": b";x\n", "tyrano/libs.js": b"//", "index.html": b"x"}))
    out = tmp_path / "Game_KO"
    result = pipeline.run_all(str(game), str(out), None, "m", str(tmp_path / "c.json"),
                              log=lambda m: None)
    assert result == str(out)
    ks = (out / "resources" / "app" / "data" / "scenario" / "first.ks").read_bytes().decode("utf-8")
    assert "\n번역됨[p]\n" in ks and "今日は" not in ks
    assert (out / "resources" / "app.asar.bak").exists()
    meta = json.loads((out / review.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["tyrano"] == {"kind": "dir", "path": "resources/app", "prefix": ""}
