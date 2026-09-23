import hashlib
import json
import pickle
import struct
import sys
import textwrap
import types
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
import renpy_engine  # noqa: E402
import review  # noqa: E402
from engine import detect_project  # noqa: E402
from translator import OllamaTranslator, protect_codes  # noqa: E402

# ---------------------------------------------------------------- fakes

# Stand-in renpy.ast classes, so pickles look like real .rpyc payloads
# (pickle records "renpy.ast.Say" etc.; the scanner never loads them).
_ast = types.ModuleType("renpy.ast")
_renpy = types.ModuleType("renpy")
_renpy.ast = _ast
sys.modules.setdefault("renpy", _renpy)
sys.modules.setdefault("renpy.ast", _ast)


def _node(name):
    cls = type(name, (), {"__module__": "renpy.ast",
                          "__init__": lambda self, **kw: self.__dict__.update(kw)})
    setattr(_ast, name, cls)
    return cls


Say, Menu, Define, PyCode = _node("Say"), _node("Menu"), _node("Define"), _node("PyCode")


def make_rpyc(stmts, protocol=2) -> bytes:
    payload = zlib.compress(pickle.dumps(({"version": 1}, stmts), protocol=protocol))
    start = 10 + 12 * 2
    return (b"RENPY RPC2" + struct.pack("<III", 1, start, len(payload))
            + struct.pack("<III", 0, 0, 0) + payload)


def make_rpa(path: Path, files: dict[str, bytes], key: int = 0x42424242, index=None) -> None:
    body = b""
    offsets = {}
    base = 34  # len of the RPA-3.0 header line
    for name, data in files.items():
        offsets[name] = (base + len(body), len(data))
        body += data
    index_offset = base + len(body)
    if index is None:
        index = {name: [(off ^ key, ln ^ key, b"")] for name, (off, ln) in offsets.items()}
    header = b"RPA-3.0 %016x %08x\n" % (index_offset, key)
    assert len(header) == base
    path.write_bytes(header + body + zlib.compress(pickle.dumps(index, protocol=2)))


SCRIPT_STMTS = [
    Define(code=PyCode(source='Character("アリス", color="#ffc")')),
    Say(who="a", what="おはよう、[player]。{w}今日もいい天気だね。", with_=None),
    Say(who="a", what="ダンジョンに行こう！", with_=None),
    Say(who=None, what="静かな朝だった。", with_=None),
    Menu(items=[("はい", "True", []), ("いいえ", "True", [])]),
    Say(who="a", what="ダンジョンに行こう！", with_=None),  # repeated -> memo reference
    PyCode(source='renpy.notify("セーブしました")'),
    PyCode(source="x = 1\n# 初期化処理\ny = 2"),  # Japanese only in a comment
]
EXPECTED = {"おはよう、[player]。{w}今日もいい天気だね。", "ダンジョンに行こう！", "静かな朝だった。",
            "はい", "いいえ", "セーブしました", "アリス"}


# ---------------------------------------------------------------- .rpyc

@pytest.mark.parametrize("protocol", [2, 5])  # Ren'Py 7 (py2-era) and Ren'Py 8 pickles
def test_rpyc_scan_finds_dialogue_menu_and_code_strings(tmp_path, protocol):
    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpyc").write_bytes(make_rpyc(SCRIPT_STMTS, protocol))
    got = renpy_engine.extract(game)
    assert set(got.texts) == EXPECTED
    assert got.names == ["アリス"]
    assert got.sources == {"rpyc": 1}


def test_rpyc_payload_rejects_garbage():
    assert renpy_engine.rpyc_payload(b"RENPY RPC2" + b"\x00" * 30) is None
    assert renpy_engine.rpyc_payload(b"not zlib at all") is None


# ---------------------------------------------------------------- .rpy

def test_renpy_unescape_matches_renpy_lexer():
    assert renpy_engine.renpy_unescape(r'He said \"hi\"\nOK') == 'He said "hi"\nOK'
    assert renpy_engine.renpy_unescape("a   b") == "a b"
    assert renpy_engine.renpy_unescape(r"back\\slash") == "back\\slash"


def test_rpy_scan(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text(textwrap.dedent('''\
        define a = Character("アリス", color="#ffc")
        # コメントは無視
        label start:
            play music "bgm/朝.ogg"
            voice "v/001.ogg"
            a happy "おはよう、[player]。"
            "アリス" "文字列で話す人もいる。"
            "ナレーション\\nの二行目"
            menu:
                "行く":
                    jump go
                "行かない" if False:
                    pass
            $ renpy.notify(_("セーブしました"))
        screen hud():
            textbutton "はじめる" action Start()
        '''), encoding="utf-8")
    got = renpy_engine.extract(game)
    assert set(got.texts) == {"アリス", "おはよう、[player]。", "文字列で話す人もいる。",
                              "ナレーション\nの二行目", "行く", "行かない", "セーブしました", "はじめる",
                              "bgm/朝.ogg"}
    assert "コメントは無視" not in got.texts


def test_rpyc_preferred_over_rpy_and_tl_folder_skipped(tmp_path):
    game = tmp_path / "game"
    (game / "tl" / "chinese").mkdir(parents=True)
    (game / "script.rpy").write_text('"ソース側の文"', encoding="utf-8")
    (game / "script.rpyc").write_bytes(make_rpyc([Say(who=None, what="コンパイル側の文")]))
    (game / "tl" / "chinese" / "script.rpy").write_text('"中文翻译"', encoding="utf-8")
    assert renpy_engine.extract(game).texts == ["コンパイル側の文"]


# ---------------------------------------------------------------- .rpa

def test_rpa_prefix_bytes_as_python3_pickles_them(tmp_path):
    # Empty prefix -> __builtin__.bytes(); non-empty -> _codecs.encode(..., "latin1")
    path = tmp_path / "a.rpa"
    make_rpa(path, {"a.rpyc": b"ABCDEF"}, key=0,
             index={"a.rpyc": [(34 + 2, 4, b"AB")], "b.rpy": [(34, 6, b"")]})
    entries = renpy_engine.rpa_entries(path)
    assert entries["a.rpyc"] == (36, 4, b"AB") and entries["b.rpy"][2] == b""
    assert renpy_engine.rpa_read(path, entries["a.rpyc"]) == b"ABCD"


def test_rpa_archive_members_are_read(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    make_rpa(game / "archive.rpa", {"script.rpyc": make_rpyc(SCRIPT_STMTS),
                                    "fonts/jp.ttf": b"fontdata"})
    got = renpy_engine.extract(game)
    assert set(got.texts) == EXPECTED
    assert got.sources == {"rpa": 1}
    assert "fonts/jp.ttf" in got.fonts


_EXECUTED = []


def _would_be_bad(*args):
    _EXECUTED.append(args)
    return {}


class _Evil:
    def __reduce__(self):
        return (_would_be_bad, ("pwned",))


def test_malicious_rpa_index_is_refused_without_running_code(tmp_path):
    make_rpa(tmp_path / "evil.rpa", {"a.rpyc": b"x"}, index=_Evil())
    assert renpy_engine.rpa_entries(tmp_path / "evil.rpa") == {}
    assert _EXECUTED == [], "the archive index must never be allowed to call anything"


# ---------------------------------------------------------------- protection

def test_renpy_markup_is_protected_but_decorative_brackets_are_not():
    protected, mapping = protect_codes("おはよう、[player]。{w=0.5}{b}元気{/b}？ 「[重要]」 %(n)s")
    assert set(mapping.values()) == {"[player]", "{w=0.5}", "{b}", "{/b}", "%(n)s"}
    assert "[重要]" in protected


# ---------------------------------------------------------------- hook script

def _run_hook(translations: dict, fonts: list[str], previous_filter=None):
    """Executes the generated hook's Python body against stub Ren'Py objects."""
    hook = renpy_engine._HOOK_TEMPLATE.replace("__FONTS__", json.dumps(fonts))
    body = textwrap.dedent(hook.split("init 999 python:\n", 1)[1])
    config = types.SimpleNamespace(say_menu_text_filter=previous_filter, replace_text=None,
                                   font_replacement_map={})
    renpy = types.SimpleNamespace(
        file=lambda name: types.SimpleNamespace(read=lambda: json.dumps(translations).encode()),
        loadable=lambda name: True)
    style = types.SimpleNamespace(default=types.SimpleNamespace(font=None))
    exec(body, {"config": config, "renpy": renpy, "style": style})
    return config, style


def test_hook_translates_say_and_displayed_text_and_swaps_fonts():
    config, style = _run_hook({"こんにちは": "안녕하세요", "アリス": "앨리스"}, ["gui/jp.ttf"])
    assert config.say_menu_text_filter("こんにちは") == "안녕하세요"
    assert config.say_menu_text_filter("未翻訳") == "未翻訳"
    assert config.replace_text("アリス") == "앨리스"
    assert config.font_replacement_map["gui/jp.ttf", True, False] == ("jpko_korean.ttf", True, False)
    assert style.default.font == "jpko_korean.ttf"


def test_hook_keeps_the_games_own_filter_working():
    config, _ = _run_hook({"こんにちは": "안녕하세요"}, [], previous_filter=lambda s: s + "!")
    assert config.say_menu_text_filter("こんにちは") == "안녕하세요!"


# ---------------------------------------------------------------- end to end

def _make_game(root: Path) -> Path:
    (root / "renpy").mkdir(parents=True)
    game = root / "game"
    game.mkdir()
    (game / "script.rpyc").write_bytes(make_rpyc(SCRIPT_STMTS))
    (game / "options.rpy").write_text('define config.name = "テストノベル"\n', encoding="utf-8")
    (root / "Game.exe").write_bytes(b"exe")
    return root


def _fingerprint(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_detects_renpy_from_root_or_game_folder(tmp_path):
    root = _make_game(tmp_path / "Novel")
    assert detect_project(str(root)).engine == "RENPY"
    assert detect_project(str(root / "game")).root == root


def test_run_all_renpy_adds_hook_files_and_leaves_game_files_untouched(tmp_path, monkeypatch):
    root = _make_game(tmp_path / "Novel")
    font = tmp_path / "ko.ttf"
    font.write_bytes(b"korean font")
    def fake_model(self, text):
        if "ダンジョン" in text:
            return "던전에 가자!"
        return "앨리스" if text == "アリス" else "번역문"
    monkeypatch.setattr(OllamaTranslator, "_call_model", fake_model)
    out = tmp_path / "Novel_KO"
    before = _fingerprint(root)
    pipeline.run_all(game=str(root), out=str(out), font=str(font), model="m",
                     cache_path=str(tmp_path / "c.json"), log=lambda m: None, workers=1)

    after = _fingerprint(out)
    for path, digest in before.items():
        assert after[path] == digest, f"{path} must be byte-identical to the original"
    added = set(after) - set(before)
    assert added == {"game/jpko_translation.json", "game/zzz_jpko_hook.rpy", "game/jpko_korean.ttf",
                     review.REVIEW_FILENAME, review.META_FILENAME}

    tl = json.loads((out / "game" / "jpko_translation.json").read_text(encoding="utf-8"))
    assert tl["アリス"] == "앨리스"
    assert tl["ダンジョンに行こう！"] == "던전에 가자!"
    meta = json.loads((out / review.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["engine"] == "RENPY" and meta["glossary_names"] == ["アリス"]


def test_review_apply_regenerates_renpy_translation_file(tmp_path, monkeypatch):
    root = _make_game(tmp_path / "Novel")
    monkeypatch.setattr(OllamaTranslator, "_call_model", lambda self, t: "번역문")
    out = tmp_path / "Novel_KO"
    pipeline.run_all(game=str(root), out=str(out), font=None, model="m",
                     cache_path=str(tmp_path / "c.json"), log=lambda m: None, workers=1)
    s = review.open_session(str(out), str(tmp_path / "c.json"))
    assert s.engine == "RENPY"
    pending: dict = {}
    review.set_manual(s, "静かな朝だった。", "고요한 아침이었다.", pending)
    assert review.apply_changes(s, pending) == 1
    tl = json.loads((out / "game" / "jpko_translation.json").read_text(encoding="utf-8"))
    assert tl["静かな朝だった。"] == "고요한 아침이었다."
