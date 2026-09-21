"""Check/start/stop the local Ollama server, for the GUI's status indicator
and start/stop button. Windows-only start/stop; is_running() works anywhere
Ollama's HTTP API might be reachable."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import requests

OLLAMA_BASE = "http://localhost:11434"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_running(timeout: float = 1.5) -> bool:
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def is_tray_running() -> bool:
    """True if the "ollama app.exe" tray process exists, regardless of
    whether its actual server child is still alive. Used to detect the
    zombie state where the tray app is up but the server behind it died --
    is_running() alone can't tell that apart from "never started"."""
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama app.exe"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        return "ollama app.exe" in result.stdout
    except Exception:  # noqa: BLE001
        return False


def _find_ollama_exe() -> str | None:
    which = shutil.which("ollama")
    if which:
        return which
    candidate = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    return candidate if os.path.exists(candidate) else None


def _find_ollama_app_exe() -> str | None:
    exe = _find_ollama_exe()
    if exe:
        app = os.path.join(os.path.dirname(exe), "ollama app.exe")
        if os.path.exists(app):
            return app
    candidate = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama app.exe")
    return candidate if os.path.exists(candidate) else None


def start() -> str:
    if is_running():
        return "Ollama가 이미 실행 중입니다."

    app_exe = _find_ollama_app_exe()
    if app_exe:
        subprocess.Popen([app_exe], creationflags=CREATE_NO_WINDOW)
        return "Ollama 앱을 실행했습니다."

    exe = _find_ollama_exe()
    if exe:
        subprocess.Popen([exe, "serve"], creationflags=CREATE_NO_WINDOW)
        return "Ollama 서버(ollama serve)를 실행했습니다."

    return "Ollama 실행 파일을 찾지 못했습니다. 직접 실행해주세요."


def stop() -> str:
    if sys.platform != "win32":
        return "이 기능은 Windows에서만 지원됩니다."
    # Killing ollama.exe / "ollama app.exe" alone can leave the llama-server
    # child process (the one actually holding the GPU/VRAM) running as an
    # orphan, so it needs to be killed explicitly too.
    for image in ["ollama.exe", "ollama app.exe", "llama-server.exe"]:
        subprocess.run(["taskkill", "/F", "/IM", image],
                        capture_output=True, creationflags=CREATE_NO_WINDOW)
    return "Ollama와 남은 llama-server 프로세스를 모두 종료했습니다."


def restart() -> str:
    """Full stop + start. Used for auto-recovery from the "tray app alive,
    server dead" zombie state, where a plain start() would think Ollama is
    already up (since a live PID exists) and do nothing."""
    stop()
    time.sleep(1.5)
    return start()
