import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402


def _stub_winsound(monkeypatch):
    """notify() imports winsound lazily -- stub it out via sys.modules so
    tests don't actually play a system sound."""
    fake = types.SimpleNamespace(MessageBeep=lambda *a, **k: None, MB_ICONASTERISK=0)
    monkeypatch.setitem(sys.modules, "winsound", fake)


def test_ps_quote_wraps_in_single_quotes():
    assert notify._ps_quote("hello") == "'hello'"


def test_ps_quote_escapes_embedded_single_quote():
    assert notify._ps_quote("it's done") == "'it''s done'"


def test_notify_swallows_subprocess_failure(monkeypatch):
    _stub_winsound(monkeypatch)

    def boom(*a, **k):
        raise OSError("no powershell")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    result = notify.notify("title", "body")
    assert "실패" in result


def test_notify_calls_powershell_with_quoted_args(monkeypatch):
    _stub_winsound(monkeypatch)
    calls = []
    monkeypatch.setattr(notify.subprocess, "run",
                         lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    result = notify.notify("제목", "본문")
    assert "알림" in result
    assert len(calls) == 1
    cmd = calls[0][0]
    assert cmd[0] == "powershell"
    assert "'제목'" in cmd[-1]
    assert "'본문'" in cmd[-1]
