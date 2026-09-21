"""Windows shutdown scheduling for the GUI's post-run auto-shutdown option.

A thin wrapper around the OS's own `shutdown` command, which already handles
the on-screen countdown notification and its own cancel path (`shutdown
/a`) -- no need to reimplement either of those.
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def schedule_shutdown(delay_seconds: int, message: str) -> str:
    if sys.platform != "win32":
        return "이 기능은 Windows에서만 지원됩니다."
    subprocess.run(
        ["shutdown", "/s", "/t", str(delay_seconds), "/c", message],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    return (f"{delay_seconds}초 후 PC가 종료됩니다 "
            f"(화면 알림을 클릭하면 취소할 수 있어요).")


def cancel_shutdown() -> str:
    if sys.platform != "win32":
        return "이 기능은 Windows에서만 지원됩니다."
    subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    return "예약된 PC 종료를 취소했습니다."
