"""Per-user settings (translation mode, API engine choice, local model) and
API keys, stored under %APPDATA% -- never next to a game or in the repo.

API keys are encrypted with Windows DPAPI (CryptProtectData), which ties
the ciphertext to the current Windows user account: the settings file is
useless if copied to another PC or another account, and no separate master
password is needed. It does NOT protect against other programs running as
the same Windows user -- nothing stored locally can.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path

APP_DIR_NAME = "쯔꾸르_한국어화_도구"
_ENTROPY = b"tsukuru-ko-localizer/api-key/v1"

DEFAULT_SETTINGS = {
    "mode": "hybrid",          # "hybrid" (API first, local fixes) | "api" | "local"
    "provider": "google_free",
    "local_model": "hf.co/hell0ks/ja-ko-vn-12b-v2-gguf:Q5_K_M",
    "workers": 4,
    "providers": {},           # provider id -> {"key": <dpapi b64>, "model": str, "base_url": str}
}


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME / "settings.json"


# --------------------------------------------------------------------------
# DPAPI
# --------------------------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("API 키 암호화는 Windows에서만 지원됩니다.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    in_blob, _in_buf = _to_blob(data)
    ent_blob, _ent_buf = _to_blob(_ENTROPY)
    out_blob = _DataBlob()
    ui_forbidden = 0x1
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
            ui_forbidden, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def encrypt_text(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode("utf-8"), encrypt=True)).decode("ascii")


def decrypt_text(token: str) -> str:
    return _dpapi(base64.b64decode(token), encrypt=False).decode("utf-8")


# --------------------------------------------------------------------------
# Settings file
# --------------------------------------------------------------------------
def load_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in settings})
    if not isinstance(settings.get("providers"), dict):
        settings["providers"] = {}
    return settings


def save_settings(settings: dict, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def provider_config(settings: dict, provider_id: str) -> dict:
    """{"key": <plaintext or "">, "model": str, "base_url": str}. A key that
    can't be decrypted (settings copied from another account/PC) reads as
    empty so the user is simply asked to enter it again."""
    raw = settings.get("providers", {}).get(provider_id, {})
    key = ""
    if raw.get("key"):
        try:
            key = decrypt_text(raw["key"])
        except (OSError, ValueError):
            key = ""
    return {"key": key, "model": raw.get("model", ""), "base_url": raw.get("base_url", "")}


def set_provider_config(settings: dict, provider_id: str, key: str | None,
                        model: str, base_url: str) -> None:
    """`key=None` keeps whatever key is already stored; "" deletes it."""
    entry = settings.setdefault("providers", {}).setdefault(provider_id, {})
    if key is not None:
        if key:
            entry["key"] = encrypt_text(key)
        else:
            entry.pop("key", None)
    entry["model"] = model
    entry["base_url"] = base_url
