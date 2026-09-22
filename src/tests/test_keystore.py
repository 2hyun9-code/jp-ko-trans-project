import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keystore  # noqa: E402

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@windows_only
def test_encrypt_round_trip_and_ciphertext_hides_key():
    token = keystore.encrypt_text("sk-secret-키")
    assert "sk-secret" not in token
    assert keystore.decrypt_text(token) == "sk-secret-키"


def test_load_settings_defaults_when_file_missing(tmp_path):
    s = keystore.load_settings(tmp_path / "nope.json")
    assert s["mode"] == "hybrid"
    assert s["providers"] == {}


def test_load_settings_ignores_unknown_and_garbage(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert keystore.load_settings(p)["mode"] == "hybrid"
    p.write_text(json.dumps({"mode": "api", "evil": 1}), encoding="utf-8")
    s = keystore.load_settings(p)
    assert s["mode"] == "api" and "evil" not in s


@windows_only
def test_provider_key_is_encrypted_on_disk_and_reads_back(tmp_path):
    p = tmp_path / "s.json"
    s = keystore.load_settings(p)
    keystore.set_provider_config(s, "deepl", "abc:fx", model="", base_url="")
    keystore.save_settings(s, p)

    assert "abc:fx" not in p.read_text(encoding="utf-8")
    cfg = keystore.provider_config(keystore.load_settings(p), "deepl")
    assert cfg["key"] == "abc:fx"


@windows_only
def test_set_provider_config_none_keeps_key_and_empty_deletes_it(tmp_path):
    s = keystore.load_settings(tmp_path / "s.json")
    keystore.set_provider_config(s, "claude", "k1", model="m", base_url="")
    keystore.set_provider_config(s, "claude", None, model="m2", base_url="")
    assert keystore.provider_config(s, "claude") == {"key": "k1", "model": "m2", "base_url": ""}
    keystore.set_provider_config(s, "claude", "", model="m2", base_url="")
    assert keystore.provider_config(s, "claude")["key"] == ""


def test_undecryptable_key_reads_as_empty(monkeypatch):
    def boom(_token):
        raise OSError("wrong user")
    monkeypatch.setattr(keystore, "decrypt_text", boom)
    s = {"providers": {"deepl": {"key": "garbage", "model": "", "base_url": ""}}}
    assert keystore.provider_config(s, "deepl")["key"] == ""


@windows_only
def test_corrupted_key_string_reads_as_empty():
    s = {"providers": {"deepl": {"key": "not-base64!!", "model": "", "base_url": ""}}}
    assert keystore.provider_config(s, "deepl")["key"] == ""
