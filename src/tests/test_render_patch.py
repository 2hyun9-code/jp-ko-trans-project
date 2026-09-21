import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import ProjectLayout  # noqa: E402
from render_patch import PATCH_FILENAME, build_translation_map, inject_render_patch  # noqa: E402


def test_build_translation_map_drops_fallback_entries():
    cache = {
        "こんにちは": "안녕하세요",   # real translation -> keep
        "薄い": "薄い",                # fallback (untranslated) -> drop
        "": "",                        # never-translated empty -> drop
    }
    result = build_translation_map(cache)
    assert result == {"こんにちは": "안녕하세요"}


def _make_layout(root: Path) -> ProjectLayout:
    root.mkdir(parents=True, exist_ok=True)
    return ProjectLayout(root=root, data_dir=root / "data", fonts_dir=root / "fonts",
                          css_dir=root / "css", engine="MZ")


def test_inject_render_patch_writes_js_and_script_tag(tmp_path):
    layout = _make_layout(tmp_path / "game")
    (layout.root / "index.html").write_text(
        "<html><head></head><body><div id=\"game\"></div></body></html>", encoding="utf-8"
    )

    patch_path = inject_render_patch(layout, {"好感度": "호감도"})

    assert patch_path == layout.root / "js" / PATCH_FILENAME
    assert patch_path.exists()
    content = patch_path.read_text(encoding="utf-8")
    assert '"\\uxxxx"' not in content  # sanity: not accidentally double-escaped
    assert "호감도" in content
    assert "好感度" in content
    # all hook points must be present -- Window_Base for standard windows,
    # Bitmap for custom Sprite-drawn UI, and the raw canvas API as a
    # last-resort net for plugins that draw straight onto a 2D context
    # (e.g. a custom outline-width tag needing manual stroke control)
    assert "Window_Base.prototype.drawText" in content
    assert "Bitmap.prototype.drawText" in content
    assert "CanvasRenderingContext2D.prototype.fillText" in content
    assert "CanvasRenderingContext2D.prototype.strokeText" in content
    # substring-fallback lookup must be present so a label baked into a
    # larger, differently-assembled string (e.g. with a live stat value
    # appended) still gets translated even without a whole-string match
    assert "sortedKeys" in content
    # fallback results must be memoized -- fillText/strokeText fire every
    # frame during a message window's typewriter effect, so re-scanning
    # every translation key per repeated call would be wasteful
    assert "_lookupCache" in content

    html = (layout.root / "index.html").read_text(encoding="utf-8")
    assert f'src="js/{PATCH_FILENAME}"' in html
    assert html.index(f'src="js/{PATCH_FILENAME}"') < html.index("</body>")


def test_inject_render_patch_is_idempotent(tmp_path):
    layout = _make_layout(tmp_path / "game")
    (layout.root / "index.html").write_text(
        "<html><body></body></html>", encoding="utf-8"
    )

    inject_render_patch(layout, {"a": "b"})
    inject_render_patch(layout, {"a": "b", "c": "d"})  # re-run, e.g. after a retranslate

    html = (layout.root / "index.html").read_text(encoding="utf-8")
    assert html.count(f'src="js/{PATCH_FILENAME}"') == 1

    # the JS file itself should still get refreshed with the latest map
    content = (layout.root / "js" / PATCH_FILENAME).read_text(encoding="utf-8")
    assert '"d"' in content


def test_inject_render_patch_returns_none_without_index_html(tmp_path):
    layout = _make_layout(tmp_path / "game")
    assert inject_render_patch(layout, {"a": "b"}) is None


def test_inject_render_patch_loads_before_plugins_js():
    """Regression test: loading the patch near </body> (after main.js) is a
    real race against Common-Event-triggered plugin commands that can fire
    text-drawing calls before a late script has even finished loading --
    some labels translate, others right next to them silently don't,
    depending on timing rather than on any actual lookup failure. The
    patch must be placed before plugins.js so its hooks are installed
    before any plugin or game-start code runs at all."""
    layout_html = (
        "<html><head>\n"
        '        <script type="text/javascript" src="js/rpg_core.js"></script>\n'
        '        <script type="text/javascript" src="js/rpg_windows.js"></script>\n'
        '        <script type="text/javascript" src="js/plugins.js"></script>\n'
        '        <script type="text/javascript" src="js/main.js"></script>\n'
        "</head><body></body></html>"
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "game"
        layout = _make_layout(root)
        (root / "index.html").write_text(layout_html, encoding="utf-8")

        inject_render_patch(layout, {"a": "b"})

        html = (root / "index.html").read_text(encoding="utf-8")
        patch_idx = html.index(f'src="js/{PATCH_FILENAME}"')
        plugins_idx = html.index('src="js/plugins.js"')
        main_idx = html.index('src="js/main.js"')
        assert patch_idx < plugins_idx < main_idx
