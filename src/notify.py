"""Post-run completion notification: a Windows toast + a system sound, used
by the GUI's "완료 후 알림" option so the user doesn't have to be watching
the window to know a long translation run finished.

Uses only the Python stdlib (winsound) and PowerShell's built-in WinRT
toast APIs -- no extra pip dependency (e.g. win10toast/plyer) needed.
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_TOAST_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode({title})) | Out-Null
$textNodes.Item(1).AppendChild($template.CreateTextNode({body})) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("JP-KO Trans").Show($toast)
"""


def _ps_quote(text: str) -> str:
    """Wraps `text` as a single-quoted PowerShell string literal (only '
    needs escaping, as two single quotes)."""
    return "'" + text.replace("'", "''") + "'"


def notify(title: str, body: str) -> str:
    """Best-effort: a toast that fails to show (locked-down system, no
    WinRT, etc.) shouldn't break the pipeline, so failures are swallowed
    and just reported back as a log line instead of raised."""
    if sys.platform != "win32":
        return "이 기능은 Windows에서만 지원됩니다."

    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:  # noqa: BLE001
        pass

    try:
        script = _TOAST_SCRIPT.format(title=_ps_quote(title), body=_ps_quote(body))
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
        )
        return "알림을 보냈습니다."
    except Exception as e:  # noqa: BLE001
        return f"알림 표시에 실패했습니다 (무시하고 계속): {e}"
