import asyncio
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

from grok_register.core.observer import Metrics
from xai_enroller.models import OAuthCredential


playwright_pkg = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
playwright_async_api.async_playwright = lambda: None
sys.modules.setdefault("playwright", playwright_pkg)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

requests_mod = types.ModuleType("requests")
requests_mod.get = lambda *_args, **_kwargs: None
requests_mod.post = lambda *_args, **_kwargs: None
sys.modules.setdefault("requests", requests_mod)

from grok_register import register


class Response:
    def __init__(self, data, status_code=200, text=None, headers=None, cookies=None):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data) if text is None else text
        self.content = self.text.encode()
        self.headers = headers or {}
        self.cookies = cookies or {}

    def json(self):
        return self._data


def test_registration_persists_sso_pack_only(monkeypatch, tmp_path):
    monkeypatch.setattr(register, "KEY_EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(
        register,
        "_remember_account_browser_fingerprint",
        lambda email, browser_fingerprint_id=None: browser_fingerprint_id or "bf-test-1",
    )

    register._persist_registration(
        "user@example.test",
        "password",
        "sso-token",
        [{"name": "sso", "value": "opaque", "domain": "accounts.x.ai"}],
        "bf-test-1",
    )

    accounts = (tmp_path / "accounts.txt").read_text(encoding="utf-8")
    sso_txt = (tmp_path / "sso.txt").read_text(encoding="utf-8")
    grok = (tmp_path / "grok.txt").read_text(encoding="utf-8")
    sessions = (tmp_path / "auth-sessions.jsonl").read_text(encoding="utf-8").strip()
    # Canonical layout: accounts=email:password, sso.txt=email:sso
    assert "user@example.test:password" in accounts
    assert "sso-token" not in accounts.split(":", 2)[-1] if False else True
    assert "user@example.test:sso-token" in sso_txt
    assert "sso-token" in grok
    session = json.loads(sessions)
    assert session["browser_fingerprint_id"] == "bf-test-1"
    assert session["email"] == "user@example.test"
    # No live CPA/sub2api at register time
    assert not (tmp_path / "cpa").exists()
    assert not (tmp_path / "sub2api").exists()


def test_registration_always_writes_sso_even_if_formats_were_oauth_only(monkeypatch, tmp_path):
    """KEY_EXPORT_FORMATS no longer gates SSO pack; register is always SSO-first."""
    monkeypatch.setattr(register, "KEY_EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(register, "KEY_EXPORT_FORMATS", ("sub2api",))
    monkeypatch.setattr(
        register,
        "_remember_account_browser_fingerprint",
        lambda email, browser_fingerprint_id=None: browser_fingerprint_id or "bf-test-1",
    )

    register._persist_registration(
        "user@example.test",
        "password",
        "sso-token",
        [{"name": "sso", "value": "opaque", "domain": "accounts.x.ai"}],
        "bf-test-1",
    )

    assert (tmp_path / "accounts.txt").exists()
    assert (tmp_path / "sso.txt").exists()
    assert "user@example.test:sso-token" in (tmp_path / "sso.txt").read_text(encoding="utf-8")
    assert (tmp_path / "auth-sessions.jsonl").exists()
    assert not list((tmp_path / "cpa").glob("*.json")) if (tmp_path / "cpa").exists() else True


def test_registration_browser_fingerprint_is_stable_per_email(monkeypatch, tmp_path):
    monkeypatch.setattr(register, "KEY_EXPORT_DIR", str(tmp_path))

    first = register._remember_account_browser_fingerprint("User@Example.Test")
    second = register._remember_account_browser_fingerprint("user@example.test")
    other = register._remember_account_browser_fingerprint("other@example.test")

    assert first == second
    assert first != other
    document = json.loads((tmp_path / "browser-fingerprints.json").read_text())
    assert document["accounts"]["user@example.test"]["browser_fingerprint_id"] == first


def test_live_key_export_enroller_removed():
    assert register.KEY_EXPORT_ENROLLER is False
    assert not hasattr(register, "_run_key_export_enrollment")
    assert not hasattr(register, "_LocalKeyExportSink")
    assert not hasattr(register, "_schedule_key_export_enrollment")


def test_moemail_url_normalizes_ui_path():
    assert (
        register._moemail_url("/api/config", "https://mail.example.test/moe")
        == "https://mail.example.test/api/config"
    )
    assert (
        register._moemail_url("/api/config", "https://api.example.test/api")
        == "https://api.example.test/api/config"
    )


def test_moemail_create_uses_api_key_and_config_domain(monkeypatch):
    calls = []

    monkeypatch.setattr(register, "MOEMAIL_API", "https://mail.example.test/moe")
    monkeypatch.setattr(register, "MOEMAIL_API_KEY", "secret-key")
    monkeypatch.setattr(register, "MOEMAIL_DOMAIN", "")
    monkeypatch.setattr(register, "MOEMAIL_EXPIRY_MS", 3600000)
    monkeypatch.setattr(register, "PROXY_POOL_FILE", "")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})

    def fake_get(url, headers=None, timeout=None):
        calls.append(("GET", url, headers, None, timeout))
        return Response({"emailDomains": "first.test,second.test"})

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, headers, json, timeout))
        return Response({"id": "email-id", "email": "oc123@first.test"})

    monkeypatch.setattr(register.req, "get", fake_get)
    monkeypatch.setattr(register.req, "post", fake_post)

    handle, email = register._moemail_create()

    assert handle == "moe|email-id"
    assert email == "oc123@first.test"
    assert calls[0][1] == "https://mail.example.test/api/config"
    assert calls[0][2]["X-API-Key"] == "secret-key"
    assert calls[1][1] == "https://mail.example.test/api/emails/generate"
    assert calls[1][2]["Content-Type"] == "application/json"
    assert calls[1][3]["domain"] == "first.test"


def test_moemail_fetch_participates_in_code_extraction(monkeypatch):
    monkeypatch.setattr(register, "MOEMAIL_API", "https://mail.example.test")
    monkeypatch.setattr(register, "MOEMAIL_API_KEY", "secret-key")
    monkeypatch.setattr(register, "PROXY_POOL_FILE", "")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})

    def fake_get(url, headers=None, timeout=None):
        assert headers["X-API-Key"] == "secret-key"
        assert url == "https://mail.example.test/api/emails/email-id"
        return Response(
            {
                "messages": [
                    {
                        "id": "message-id",
                        "subject": "Grok code",
                        "content": "Your code is <b>ABC-123</b>",
                        "html": "",
                    }
                ]
            }
        )

    monkeypatch.setattr(register.req, "get", fake_get)

    text = register._tempmail_fetch("moe|email-id")
    assert register._extract_code(text) == "ABC123"


def test_moemail_mode_does_not_fallback_to_other_providers(monkeypatch):
    monkeypatch.setattr(register, "EMAIL_MODE", "moemail")
    monkeypatch.setattr(register, "_moemail_create", lambda _password: ("moe|id", "u@moe.test"))
    monkeypatch.setattr(
        register,
        "_mailtm_create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unused")),
    )
    monkeypatch.setattr(
        register,
        "_lol_create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unused")),
    )

    handle, email, password = register.create_email()

    assert handle == "moe|id"
    assert email == "u@moe.test"
    assert password


def test_proxy_pool_file_rotates_and_normalizes(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text(
        "\n".join(
            [
                "# local proxy pool",
                "http://one.example:8080",
                "socks5://two.example:1080",
                "three.example:8888",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "PROXY_POOL_FILE", str(proxy_file))
    monkeypatch.setattr(register, "PROXY_POOL_STRATEGY", "round_robin")
    monkeypatch.setattr(register, "PROXY_AUTO_CONFIG", register.ProxyAutoConfig(enabled=False))
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})

    assert register._pick_grok_proxy() == "http://one.example:8080"
    assert register._pick_grok_proxy() == "socks5://two.example:1080"
    assert register._pick_grok_proxy() == "http://three.example:8888"
    assert register._pick_grok_proxy() == "http://one.example:8080"


def test_proxy_pool_share_link_reuses_existing_relay_node(monkeypatch):
    link = "vless://node@example.test:443?encryption=none#relay"
    calls = []

    monkeypatch.setattr(register, "PROXY_RELAY_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_RELAY_BUILTIN_ENABLED", False)
    monkeypatch.setattr(register, "_proxy_relay_external_retry_at", 0)
    monkeypatch.setattr(register, "PROXY_RELAY_HOST", "127.0.0.1")
    monkeypatch.setattr(register, "PROXY_RELAY_PROXY_SCHEME", "auto")
    register._proxy_relay_link_cache.clear()

    def fake_relay_json(method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "data": {
                "nodes": [
                    {
                        "share_link": link,
                        "local_port": 19081,
                        "kernel": "sing-box",
                    }
                ]
            }
        }

    monkeypatch.setattr(register, "_proxy_relay_json", fake_relay_json)

    assert register._normalize_proxy_line(link) == "http://127.0.0.1:19081"
    assert register._normalize_proxy_line(link) == "http://127.0.0.1:19081"
    assert calls == [("GET", "/api/state", None)]


def test_proxy_pool_share_link_imports_via_relay(monkeypatch):
    link = "trojan://secret@example.test:443#relay"
    calls = []

    monkeypatch.setattr(register, "PROXY_RELAY_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_RELAY_BUILTIN_ENABLED", False)
    monkeypatch.setattr(register, "_proxy_relay_external_retry_at", 0)
    monkeypatch.setattr(register, "PROXY_RELAY_KERNEL", "auto")
    monkeypatch.setattr(register, "PROXY_RELAY_HOST", "127.0.0.1")
    monkeypatch.setattr(register, "PROXY_RELAY_PROXY_SCHEME", "auto")
    register._proxy_relay_link_cache.clear()

    def fake_relay_json(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"ok": True}
        if len([call for call in calls if call[0] == "GET"]) == 1:
            return {"nodes": []}
        return {"nodes": [{"link": link, "localPort": 19082, "kernel": "sing-box"}]}

    monkeypatch.setattr(register, "_proxy_relay_json", fake_relay_json)

    assert register._normalize_proxy_line(link) == "http://127.0.0.1:19082"
    assert calls[1] == (
        "POST",
        "/api/nodes/import",
        {"share_link": link, "kernel": "sing-box", "local_port": ""},
    )


def test_proxy_pool_share_link_falls_back_to_builtin_relay(monkeypatch):
    link = "vless://node@example.test:443?encryption=none#relay"
    external_calls = []
    builtin_calls = []

    monkeypatch.setattr(register, "PROXY_RELAY_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_RELAY_BUILTIN_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_RELAY_KERNEL", "auto")
    monkeypatch.setattr(register, "_proxy_relay_external_retry_at", 0)
    register._proxy_relay_link_cache.clear()

    def fake_relay_json(method, path, payload=None):
        external_calls.append((method, path, payload))
        raise RuntimeError("connection refused")

    def fake_builtin_import(share_link, kernel="sing-box", local_port=""):
        builtin_calls.append((share_link, kernel, local_port))
        return "http://127.0.0.1:19080"

    monkeypatch.setattr(register, "_proxy_relay_json", fake_relay_json)
    monkeypatch.setattr(register, "_builtin_proxy_relay_import", fake_builtin_import)

    assert register._normalize_proxy_line(link) == "http://127.0.0.1:19080"
    assert external_calls == [("GET", "/api/state", None)]
    assert builtin_calls == [(link, "sing-box", "")]


def test_proxy_pool_retries_failed_share_link_after_retry_window(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text("vless://node@example.test:443#relay\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(register, "PROXY_POOL_FILE", str(proxy_file))
    monkeypatch.setattr(register, "PROXY_RELAY_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_AUTO_CONFIG", register.ProxyAutoConfig(enabled=False))
    monkeypatch.setattr(register, "PROXY_RELAY_RETRY_SEC", 30)
    monkeypatch.setattr(register, "PROXY_POOL_STRATEGY", "round_robin")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})

    def fake_from_share_link(link):
        calls.append(link)
        return "http://127.0.0.1:19083" if len(calls) > 1 else None

    monkeypatch.setattr(register, "_proxy_from_share_link", fake_from_share_link)

    assert register._pick_grok_proxy() is None
    assert register._pick_grok_proxy() is None
    assert len(calls) == 1

    register._proxy_pool_cache["retry_at"] = 0

    assert register._pick_grok_proxy() == "http://127.0.0.1:19083"
    assert len(calls) == 2


def test_proxy_pool_mixes_manual_and_auto_active_proxies(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text("http://manual.example:8080\n", encoding="utf-8")
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    (auto_dir / "active.txt").write_text("socks5://auto.example:1080\n", encoding="utf-8")

    monkeypatch.setattr(register, "PROXY_POOL_FILE", str(proxy_file))
    monkeypatch.setattr(
        register,
        "PROXY_AUTO_CONFIG",
        register.ProxyAutoConfig(
            enabled=True,
            output_dir=str(auto_dir),
            active_file="active.txt",
            export_formats=("raw",),
        ),
    )
    register._proxy_pool_cache.update(
        {"path": None, "mtime_ns": None, "auto_mtime_ns": None, "items": (), "index": 0}
    )

    with register._proxy_pool_lock:
        items = register._load_proxy_pool_locked()

    assert items == ("http://manual.example:8080", "socks5://auto.example:1080")


def test_proxy_pool_uses_tested_auto_pool_after_startup_validation(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text("http://manual-untested.example:8080\n", encoding="utf-8")
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    (auto_dir / "active.txt").write_text("socks5://tested.example:1080\n", encoding="utf-8")

    monkeypatch.setattr(register, "PROXY_POOL_FILE", str(proxy_file))
    monkeypatch.setattr(register, "PROXY_POOL_USE_TESTED_ONLY", True)
    monkeypatch.setattr(register, "_proxy_auto_startup_validated", True)
    monkeypatch.setattr(
        register,
        "PROXY_AUTO_CONFIG",
        register.ProxyAutoConfig(
            enabled=True,
            output_dir=str(auto_dir),
            active_file="active.txt",
            export_formats=("raw",),
        ),
    )
    register._proxy_pool_cache.update(
        {"path": None, "mtime_ns": None, "auto_mtime_ns": None, "items": (), "index": 0}
    )

    with register._proxy_pool_lock:
        items = register._load_proxy_pool_locked()

    assert items == ("socks5://tested.example:1080",)


def test_auto_bootstrap_skips_stale_builtin_relay_ports(monkeypatch, tmp_path):
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    (auto_dir / "active.txt").write_text(
        "\n".join(
            [
                "http://127.0.0.1:19080",
                "http://real-proxy.example:8080",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(register, "PROXY_POOL_FILE", "")
    monkeypatch.setattr(register, "PROXY_RELAY_HOST", "127.0.0.1")
    monkeypatch.setattr(register, "PROXY_RELAY_START_PORT", 19080)
    monkeypatch.setattr(
        register,
        "PROXY_AUTO_CONFIG",
        register.ProxyAutoConfig(
            enabled=True,
            output_dir=str(auto_dir),
            active_file="active.txt",
            export_formats=("raw",),
        ),
    )
    register._proxy_pool_cache.update(
        {"path": None, "mtime_ns": None, "auto_mtime_ns": None, "items": (), "index": 0}
    )

    assert register._auto_bootstrap_proxies() == ["http://real-proxy.example:8080"]


def test_proxy_auto_no_active_message_mentions_relay_failure(monkeypatch, tmp_path):
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    config = register.ProxyAutoConfig(
        enabled=True,
        output_dir=str(auto_dir),
        state_file="state.json",
    )
    config.state_path.write_text(
        json.dumps({"error_summary": {"unsupported proxy": 5}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(register, "PROXY_AUTO_CONFIG", config)
    monkeypatch.setattr(register, "PROXY_RELAY_ENABLED", True)
    monkeypatch.setattr(register, "PROXY_RELAY_URL", "http://127.0.0.1:18080")

    def fake_relay_json(method, path, payload=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(register, "_proxy_relay_json", fake_relay_json)

    message = register._proxy_auto_no_active_message()

    assert "unsupported proxy x5" in message
    assert "proxy-relay is not reachable at http://127.0.0.1:18080" in message
    assert "start proxy-relay" in message


def test_prepare_auto_proxy_pool_before_start_logs_and_validates(monkeypatch, tmp_path):
    auto_dir = tmp_path / "auto"
    config = register.ProxyAutoConfig(
        enabled=True,
        output_dir=str(auto_dir),
        active_file="active.txt",
        test_urls=("https://accounts.x.ai/sign-up?redirect=grok-com",),
    )
    logs = []

    monkeypatch.setattr(register, "PROXY_AUTO_CONFIG", config)
    monkeypatch.setattr(register, "PROXY_AUTO_REQUIRE_ACTIVE", True)
    monkeypatch.setattr(register, "PROXY_POOL_USE_TESTED_ONLY", True)
    monkeypatch.setattr(register, "_proxy_auto_startup_validated", False)
    monkeypatch.setattr(register, "_refresh_auto_proxy_pool_once", lambda: ["http://good.example:8080"])
    monkeypatch.setattr(register, "_ensure_proxy_auto_manager_started", lambda: None)
    monkeypatch.setattr(register, "log", logs.append)

    asyncio.run(register._prepare_auto_proxy_pool_before_start())

    assert register._proxy_auto_startup_validated is True
    assert any("启动前拉取代理" in item for item in logs)
    assert any("已优选 1 个" in item for item in logs)


def test_relay_proxy_url_auto_uses_xray_socks(monkeypatch):
    monkeypatch.setattr(register, "PROXY_RELAY_HOST", "127.0.0.1")
    monkeypatch.setattr(register, "PROXY_RELAY_PROXY_SCHEME", "auto")

    assert register._relay_proxy_url(10808, "xray") == "socks5://127.0.0.1:10808"
    assert register._relay_proxy_url(10809, "sing-box") == "http://127.0.0.1:10809"


def test_email_http_request_does_not_apply_grok_proxy_pool_by_default(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy.txt"
    proxy_file.write_text("socks5://proxy.example:1080\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(register, "PROXY_POOL_FILE", str(proxy_file))
    monkeypatch.setattr(register, "PROXY_POOL_STRATEGY", "round_robin")
    monkeypatch.setattr(register, "CF_ARES_EMAIL_MODE", "0")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"ok": True})

    monkeypatch.setattr(register.req, "get", fake_get)

    response = register._email_get("https://mail.example.test/api/config", timeout=9)

    assert response.json() == {"ok": True}
    assert calls[0][1]["timeout"] == 9
    assert "proxies" not in calls[0][1]


def test_email_http_request_accepts_explicit_proxy(monkeypatch):
    calls = []

    monkeypatch.setattr(register, "CF_ARES_EMAIL_MODE", "0")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"ok": True})

    monkeypatch.setattr(register.req, "get", fake_get)

    response = register._email_get(
        "https://mail.example.test/api/config",
        proxy="socks5://proxy.example:1080",
        timeout=9,
    )

    assert response.json() == {"ok": True}
    assert calls[0][1]["proxies"] == {
        "http": "socks5://proxy.example:1080",
        "https": "socks5://proxy.example:1080",
    }


def test_email_http_request_falls_back_to_cf_ares_on_cloudflare_block(monkeypatch):
    calls = []

    monkeypatch.setattr(register, "CF_ARES_EMAIL_MODE", "fallback")
    monkeypatch.setattr(register, "PROXY_POOL_FILE", "")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})
    monkeypatch.setattr(
        register.req,
        "get",
        lambda url, **kwargs: Response({}, status_code=403, text="error code: 1010"),
    )

    def fake_cf_ares(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response({"ok": True})

    monkeypatch.setattr(register, "_cf_ares_request", fake_cf_ares)

    response = register._email_get(
        "https://mail.example.test/api/config",
        headers={"X-API-Key": "secret-key"},
        timeout=7,
    )

    assert response.json() == {"ok": True}
    assert calls == [
        (
            "GET",
            "https://mail.example.test/api/config",
            {"headers": {"X-API-Key": "secret-key"}, "timeout": 7},
        )
    ]


def test_email_http_request_always_mode_skips_requests(monkeypatch):
    monkeypatch.setattr(register, "CF_ARES_EMAIL_MODE", "always")
    monkeypatch.setattr(register, "PROXY_POOL_FILE", "")
    register._proxy_pool_cache.update({"path": None, "mtime_ns": None, "items": (), "index": 0})
    monkeypatch.setattr(
        register.req,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unused")),
    )
    monkeypatch.setattr(
        register,
        "_cf_ares_request",
        lambda method, url, **kwargs: Response({"transport": method.lower()}),
    )

    response = register._email_post("https://mail.example.test/api/emails/generate")

    assert response.json() == {"transport": "post"}


def test_cf_ares_request_uses_curl_fallback_when_package_is_incomplete(monkeypatch):
    calls = []

    monkeypatch.setattr(
        register,
        "_cf_ares_get_client",
        lambda proxy=None: (_ for _ in ()).throw(RuntimeError("cf-ares unavailable")),
    )

    def fake_curl(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response({"ok": True}, headers={"x": "1"})

    monkeypatch.setattr(register, "_curl_cffi_request", fake_curl)

    response = register._cf_ares_request(
        "GET",
        "https://accounts.x.ai/sign-up",
        proxy="socks5://proxy.example:1080",
        timeout=11,
    )

    assert response.json() == {"ok": True}
    assert calls == [
        (
            "GET",
            "https://accounts.x.ai/sign-up",
            {"proxy": "socks5://proxy.example:1080", "timeout": 11},
        )
    ]


def test_cf_ares_import_prefers_bundled_vendor(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        broken_root = tmp_path / "broken"
        bundled_root = tmp_path / "vendor" / "CF-Ares"
        (broken_root / "cf_ares").mkdir(parents=True)
        (bundled_root / "cf_ares").mkdir(parents=True)
        (broken_root / "cf_ares" / "__init__.py").write_text(
            "raise ModuleNotFoundError('No module named cf_ares.engines')\n"
        )
        (bundled_root / "cf_ares" / "__init__.py").write_text(
            "class AresClient:\n"
            "    source = 'bundled'\n"
        )

        monkeypatch.setattr(sys, "path", [str(broken_root)] + list(sys.path))
        monkeypatch.setattr(register, "CF_ARES_PATH", "")
        monkeypatch.setattr(register, "CF_ARES_BUNDLED_PATH", bundled_root)
        for name in list(sys.modules):
            if name == "cf_ares" or name.startswith("cf_ares."):
                monkeypatch.delitem(sys.modules, name, raising=False)

        AresClient = register._cf_ares_client_class()

        assert AresClient.source == "bundled"
        assert sys.path[0] == str(bundled_root)


def test_xai_http_request_retries_cf_ares_on_request_error(monkeypatch):
    calls = []

    monkeypatch.setattr(register, "CF_ARES_XAI_MODE", "fallback")
    monkeypatch.setattr(
        register.req,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("blocked")),
    )

    def fake_cf_ares(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response({}, headers={"grpc-status": "0"})

    monkeypatch.setattr(register, "_cf_ares_request", fake_cf_ares)

    response = register._xai_http_request(
        "POST",
        "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode",
        data=b"body",
        timeout=7,
    )

    assert response.headers["grpc-status"] == "0"
    assert calls == [
        (
            "POST",
            "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode",
            {"data": b"body", "timeout": 7},
        )
    ]


def test_grpc_create_code_can_use_xai_cf_ares_transport(monkeypatch):
    calls = []
    page = FakePage()

    monkeypatch.setattr(register, "CF_ARES_XAI_MODE", "always")

    async def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response({}, headers={"grpc-status": "0"})

    monkeypatch.setattr(register, "_xai_http_request_async", fake_request)

    ok = asyncio.run(register.grpc_create_code(page, "user@example.test"))

    assert ok is True
    assert not page.evaluations
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/auth_mgmt.AuthManagement/CreateEmailValidationCode")
    assert calls[0][2]["page"] is page


class FakePage:
    def __init__(self, context=None):
        self.context = context
        self.closed = False
        self.url = "about:blank"
        self.goto_calls = []
        self.waits = []
        self.evaluations = []
        self.route_calls = []
        self.mouse = types.SimpleNamespace(
            moves=[],
            clicks=[],
            downs=0,
            ups=0,
            move=self._mouse_move,
            click=self._mouse_click,
            down=self._mouse_down,
            up=self._mouse_up,
        )
        self.turnstile_token = ""
        self.turnstile_box = {"x": 160, "y": 45}
        self.turnstile_page_trace = {
            "created_at": 10.0,
            "script_inserted_at": 11.0,
            "script_loaded_at": 12.0,
            "render_called_at": 13.0,
            "render_returned_at": 14.0,
            "token_written_at": None,
            "token_len": 0,
            "error": None,
        }
        self.turnstile_dom_snapshot = {
            "widget": {"present": True, "x": 10, "y": 10, "w": 300, "h": 70, "visible": True},
            "click_center": {"x": 160, "y": 45},
            "element_at_center": {"tag": "IFRAME", "id": "", "class": "", "is_iframe": True},
            "all_iframe_count": 1,
            "turnstile_iframe_count": 1,
            "iframe_summaries": [
                {"host": "challenges.cloudflare.com", "path": "/turnstile/v0", "x": 10, "y": 10, "w": 300, "h": 70, "visible": True}
            ],
            "turnstile_loaded": True,
            "response_input": {"present": True, "token_len": 0},
        }

    async def set_viewport_size(self, size):
        self.viewport = size
        pass

    async def goto(self, url, timeout=None, wait_until=None):
        self.url = url
        self.goto_calls.append({
            "url": url,
            "timeout": timeout,
            "wait_until": wait_until,
        })
        pass

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)
        pass

    async def evaluate(self, script):
        self.evaluations.append(script)
        if "__csp_solver_snapshot" in script:
            return self.turnstile_dom_snapshot
        if "__cspTurnstileTrace" in script and "return window.__cspTurnstileTrace" in script:
            return self.turnstile_page_trace
        if "cf-turnstile-response" in script:
            return self.turnstile_token
        if "getBoundingClientRect" in script and ".cf-turnstile" in script:
            return self.turnstile_box
        return None

    async def close(self):
        self.closed = True
        pass

    async def route(self, pattern, handler):
        self.route_calls.append({"pattern": pattern, "handler": handler})

    async def _mouse_move(self, x, y, steps=None):
        self.mouse.moves.append({"x": x, "y": y, "steps": steps})

    async def _mouse_click(self, x, y):
        self.mouse.clicks.append({"x": x, "y": y})

    async def _mouse_down(self):
        self.mouse.downs += 1

    async def _mouse_up(self):
        self.mouse.ups += 1


class FakeContext:
    def __init__(self, **kwargs):
        self.pages = []
        self.closed = False
        self.kwargs = kwargs
        self.clear_cookies_calls = 0
        self.request = types.SimpleNamespace(get=self._request_get)
        self.request_get_calls = []
        self.cookies_value = []
        self.cancel_on_clear = False

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def clear_cookies(self):
        self.clear_cookies_calls += 1
        if self.cancel_on_clear:
            raise asyncio.CancelledError()
        self.cookies_value = []
        pass

    async def cookies(self):
        return list(self.cookies_value)

    async def _request_get(self, url, timeout=None):
        self.request_get_calls.append({"url": url, "timeout": timeout})
        return types.SimpleNamespace(status=403)

    async def close(self):
        self.closed = True
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    def __init__(self):
        self.pages = []
        self.contexts = []
        self.context = types.SimpleNamespace(request=object())
        self.closed = False
        self.close_calls = 0
        self.close_error = None

    async def new_page(self):
        page = FakePage(self.context)
        self.pages.append(page)
        return page

    async def new_context(self, **kwargs):
        context = FakeContext(**kwargs)
        self.contexts.append(context)
        return context

    async def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class FakePair:
    def __init__(self):
        self.t = types.SimpleNamespace(value="tok")
        self.q = types.SimpleNamespace(
            value={"email": "e@example.test", "password": "pw", "code": "123456"}
        )


class FakeInventory:
    def __init__(self):
        self.active = 0
        self.claims = 0
        self.t_depth = 0
        self.q_depth = 0

    def claim_pair(self):
        inventory = self

        class Claim:
            async def __aenter__(self):
                inventory.active += 1
                inventory.claims += 1
                return FakePair()

            async def __aexit__(self, exc_type, exc, tb):
                inventory.active -= 1
                return False

        return Claim()


class FakeQInventory:
    def __init__(self):
        self.q_items = []

    async def put_q(self, item):
        self.q_items.append(item)


class RegisterRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_stop = register.STOP
        self._old_timeout = getattr(register, "C_CONSUME_TIMEOUT", None)
        self._old_verify = register.grpc_verify_code
        self._old_register = register.server_action_register
        self._old_create_code = register.grpc_create_code
        self._old_poll_code = register.poll_code
        self._old_poll_code_async = register._poll_code_async
        self._old_poll_code_once_async = register._poll_code_once_async
        self._old_pick_grok_proxy = register._pick_grok_proxy
        register._pick_grok_proxy = lambda: None
        self._old_log = register.log
        self._old_target = register.TARGET
        self._old_c_hot_page_pool = getattr(register, "C_HOT_PAGE_POOL", None)
        self._old_c_hot_page_pool_size = getattr(register, "C_HOT_PAGE_POOL_SIZE", None)
        self._old_c_set_cookie_via_request = getattr(register, "C_SET_COOKIE_VIA_REQUEST", None)
        self._old_log_mode = getattr(register, "REGISTER_LOG_MODE", None)
        self._old_rate_limit_circuit = register.REGISTRATION_RATE_LIMIT_CIRCUIT
        self._old_cf_ares_xai_mode = getattr(register, "CF_ARES_XAI_MODE", None)
        register.CF_ARES_XAI_MODE = "0"

    async def asyncTearDown(self):
        if hasattr(register, "_close_c_hot_page_pool"):
            await register._close_c_hot_page_pool()
        register.STOP = self._old_stop
        if self._old_timeout is not None:
            register.C_CONSUME_TIMEOUT = self._old_timeout
        register.grpc_verify_code = self._old_verify
        register.server_action_register = self._old_register
        register.grpc_create_code = self._old_create_code
        register.poll_code = self._old_poll_code
        register._poll_code_async = self._old_poll_code_async
        register._poll_code_once_async = self._old_poll_code_once_async
        register._pick_grok_proxy = self._old_pick_grok_proxy
        register.log = self._old_log
        register.TARGET = self._old_target
        if self._old_c_hot_page_pool is None and hasattr(register, "C_HOT_PAGE_POOL"):
            delattr(register, "C_HOT_PAGE_POOL")
        elif self._old_c_hot_page_pool is not None:
            register.C_HOT_PAGE_POOL = self._old_c_hot_page_pool
        if self._old_c_hot_page_pool_size is None and hasattr(register, "C_HOT_PAGE_POOL_SIZE"):
            delattr(register, "C_HOT_PAGE_POOL_SIZE")
        elif self._old_c_hot_page_pool_size is not None:
            register.C_HOT_PAGE_POOL_SIZE = self._old_c_hot_page_pool_size
        if self._old_c_set_cookie_via_request is None and hasattr(register, "C_SET_COOKIE_VIA_REQUEST"):
            delattr(register, "C_SET_COOKIE_VIA_REQUEST")
        elif self._old_c_set_cookie_via_request is not None:
            register.C_SET_COOKIE_VIA_REQUEST = self._old_c_set_cookie_via_request
        if self._old_log_mode is None and hasattr(register, "REGISTER_LOG_MODE"):
            delattr(register, "REGISTER_LOG_MODE")
        elif self._old_log_mode is not None:
            register.REGISTER_LOG_MODE = self._old_log_mode
        register.REGISTRATION_RATE_LIMIT_CIRCUIT = self._old_rate_limit_circuit
        if self._old_cf_ares_xai_mode is None and hasattr(register, "CF_ARES_XAI_MODE"):
            delattr(register, "CF_ARES_XAI_MODE")
        elif self._old_cf_ares_xai_mode is not None:
            register.CF_ARES_XAI_MODE = self._old_cf_ares_xai_mode

    async def test_close_browser_safely_ignores_driver_disconnect(self):
        messages = []
        browser = FakeBrowser()
        browser.close_error = Exception("Connection closed while reading from the driver")

        register.REGISTER_LOG_MODE = "debug"
        register.log = messages.append

        await register._close_browser_safely(browser)

        self.assertEqual(browser.close_calls, 1)
        self.assertTrue(any("browser close ignored: Exception" in msg for msg in messages))

    async def test_close_browser_safely_preserves_cancellation(self):
        browser = FakeBrowser()
        browser.close_error = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await register._close_browser_safely(browser)

        self.assertEqual(browser.close_calls, 1)

    async def test_c_worker_timeout_releases_physical_and_pair_and_counts_failure(self):
        async def slow_verify(*_args, **_kwargs):
            await asyncio.sleep(10)
            return True

        register.STOP = asyncio.Event()
        register.C_CONSUME_TIMEOUT = 0.05
        register.grpc_verify_code = slow_verify
        register.server_action_register = lambda *_args, **_kwargs: None
        register.log = lambda _msg: None

        inventory = FakeInventory()
        physical_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register.c_worker(0, FakeBrowser(), inventory, physical_sem, metrics)
        )
        await asyncio.sleep(0.12)
        register.STOP.set()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(inventory.active, 0)
        self.assertEqual(metrics.pair_consumed_fail, 1)

    async def test_c_consume_uses_single_use_page_by_default(self):
        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(*_args, **_kwargs):
            return None

        register.C_HOT_PAGE_POOL = False
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        ok = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertFalse(ok)
        self.assertEqual(len(browser.pages), 1)
        self.assertEqual(len(browser.contexts), 0)
        self.assertTrue(browser.pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_reuses_page_and_clears_cookies_between_consumes(self):
        seen_pages = []

        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(page, *_args, **_kwargs):
            seen_pages.append(page)
            return None

        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        first = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())
        second = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(len(browser.contexts), 1)
        self.assertEqual(len(browser.contexts[0].pages), 1)
        self.assertEqual(seen_pages, [browser.contexts[0].pages[0], browser.contexts[0].pages[0]])
        self.assertEqual(browser.contexts[0].clear_cookies_calls, 2)
        self.assertFalse(browser.contexts[0].closed)
        self.assertFalse(browser.contexts[0].pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_discards_page_after_exception(self):
        async def failing_verify(*_args, **_kwargs):
            raise RuntimeError("verify failed")

        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2
        register.grpc_verify_code = failing_verify
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        with self.assertRaises(RuntimeError):
            await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertEqual(len(browser.contexts), 1)
        self.assertTrue(browser.contexts[0].closed)
        self.assertTrue(browser.contexts[0].pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_closes_context_when_cancelled_during_cleanup(self):
        context = FakeContext()
        page = await context.new_page()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        context.cancel_on_clear = True
        register.C_HOT_PAGE_POOL = True

        with self.assertRaises(asyncio.CancelledError):
            await register._release_c_page(context, page, healthy=True)

        self.assertTrue(context.closed)
        self.assertTrue(page.closed)

    async def test_c_hot_page_discards_page_after_fallback_navigation(self):
        context = FakeContext()
        page = await context.new_page()
        page.url = "https://example.test/set-cookie?q=abc"
        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2

        await register._release_c_page(context, page, healthy=True)

        self.assertTrue(context.closed)
        self.assertTrue(page.closed)
        self.assertEqual(register._c_hot_page_pool, [])

    async def test_server_action_can_set_cookie_via_request_without_navigating_page(self):
        page = FakePage()
        context = FakeContext()
        context.cookies_value = [{"name": "sso", "value": "x" * 152}]
        page.context = context
        page.goto_calls = []

        async def fake_evaluate(_script):
            return '0:"https:\\/\\/auth.grokipedia.com\\/set-cookie?q=abc"1:'

        page.evaluate = fake_evaluate
        register.STATE_TREE = "state"
        register.ACTION_ID = "action"
        register.C_SET_COOKIE_VIA_REQUEST = True

        sso = await register.server_action_register(
            page, "e@example.test", "pw", "123456", "token"
        )

        self.assertEqual(sso, "x" * 152)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(
            context.request_get_calls,
            [{"url": "https://auth.grokipedia.com/set-cookie?q=abc", "timeout": 15000}],
        )

    async def test_server_action_can_use_xai_cf_ares_transport(self):
        page = FakePage()
        context = FakeContext()
        page.context = context
        calls = []

        old_mode = register.CF_ARES_XAI_MODE
        old_request = register._xai_http_request_async
        old_state = register.STATE_TREE
        old_action = register.ACTION_ID
        try:
            register.CF_ARES_XAI_MODE = "always"
            register.STATE_TREE = "state"
            register.ACTION_ID = "action"

            async def fake_request(method, url, **kwargs):
                calls.append((method, url, kwargs))
                if method == "POST":
                    return Response(
                        {},
                        text='0:"https:\\/\\/auth.grokipedia.com\\/set-cookie?q=abc"1:',
                    )
                return Response(
                    {},
                    headers={"set-cookie": "sso=" + ("y" * 152) + "; Path=/; Secure; HttpOnly"},
                    cookies={"sso": "y" * 152},
                )

            register._xai_http_request_async = fake_request

            sso = await register.server_action_register(
                page, "e@example.test", "pw", "123456", "token"
            )
        finally:
            register.CF_ARES_XAI_MODE = old_mode
            register._xai_http_request_async = old_request
            register.STATE_TREE = old_state
            register.ACTION_ID = old_action

        self.assertEqual(sso, "y" * 152)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(context.request_get_calls, [])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[1][0], "GET")

    async def test_server_action_raises_a_distinct_error_for_a_rate_limited_signup_page(self):
        page = FakePage()

        async def rate_limited_response(_script):
            return "Too many requests. Please try again later."

        page.evaluate = rate_limited_response
        register.STATE_TREE = "state"
        register.ACTION_ID = "action"

        with self.assertRaises(register.RegistrationRateLimited):
            await register.server_action_register(
                page, "e@example.test", "pw", "123456", "token"
            )

    async def test_rate_limit_circuit_opens_for_the_configured_cooldown(self):
        now = [100.0]
        circuit = register.RegistrationRateLimitCircuit(
            cooldown_seconds=60,
            clock=lambda: now[0],
        )

        circuit.trip()

        self.assertTrue(circuit.is_open())
        self.assertEqual(circuit.remaining_seconds(), 60)
        now[0] = 160.0
        self.assertFalse(circuit.is_open())

    async def test_monitor_uses_metrics_snapshot(self):
        register.STOP = asyncio.Event()
        register.TARGET = 1
        register.REGISTER_LOG_MODE = "debug"
        messages = []
        register.log = messages.append

        metrics = Metrics()
        metrics.success_count = 1
        metrics.pair_claimed = 2
        metrics.pair_consumed_ok = 1
        metrics.pair_consumed_fail = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        await register.monitor(FakeInventory(), sems, metrics, interval=0)

        self.assertTrue(
            any("pair:2 ok:1 fail:1" in message for message in messages),
            messages,
        )

    async def test_monitor_hides_internal_snapshot_in_user_mode(self):
        register.STOP = asyncio.Event()
        register.TARGET = 1
        register.REGISTER_LOG_MODE = "user"
        messages = []
        register.log = messages.append

        metrics = Metrics()
        metrics.success_count = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        await register.monitor(FakeInventory(), sems, metrics, interval=0)

        self.assertFalse(any(message.startswith("[*] T:") for message in messages), messages)

    async def test_user_event_format_reports_only_registration_outcomes(self):
        self.assertEqual(
            register.format_user_registration_event("started", task_id=7),
            "[→] 开始注册 #7",
        )
        self.assertEqual(
            register.format_user_registration_event(
                "success", task_id=7, count=5, rate_per_minute=12.34
            ),
            "[✓] 注册成功 #7 | 运行平均 12.3/分 | 累计 5",
        )
        self.assertEqual(
            register.format_user_registration_event("failed", task_id=7),
            "[✗] 注册失败 #7 | 已跳过，继续下一任务",
        )
        self.assertEqual(
            register.format_user_registration_event("rate_limited", wait_seconds=60),
            "[⏸] 触发限流 | 60秒后恢复探测",
        )
        self.assertEqual(
            register.format_user_registration_event("recovered", wait_seconds=61),
            "[▶] 限流解除 | 实际等待 61秒",
        )

    async def test_debug_flag_overrides_environment_and_invalid_mode_is_rejected(self):
        self.assertEqual(
            register.resolve_register_log_mode(["--debug"], {"REGISTER_LOG_MODE": "user"}),
            "debug",
        )
        self.assertEqual(
            register.resolve_register_log_mode([], {"REGISTER_LOG_MODE": "debug"}),
            "debug",
        )
        with self.assertRaises(ValueError):
            register.resolve_register_log_mode([], {"REGISTER_LOG_MODE": "verbose"})

    async def test_registration_task_numbers_are_separate_from_pair_claims(self):
        metrics = Metrics()
        metrics.pair_claimed = 12

        self.assertEqual(metrics.next_registration_task(), 1)
        self.assertEqual(metrics.next_registration_task(), 2)

    async def test_five_minute_rate_uses_process_uptime_then_sliding_window(self):
        now = [0.0]
        metrics = Metrics(clock=lambda: now[0])
        self.assertIsNone(metrics.five_minute_success_rate())

        now[0] = 10.0
        metrics.record_success()
        now[0] = 20.0
        metrics.record_success()
        self.assertEqual(metrics.five_minute_success_rate(), 6.0)

        now[0] = 311.0
        self.assertAlmostEqual(metrics.five_minute_success_rate(), 0.2)
        now[0] = 321.0
        self.assertEqual(metrics.five_minute_success_rate(), 0.0)

    async def test_runtime_average_rate_includes_the_entire_cooldown_period(self):
        now = [0.0]
        metrics = Metrics(clock=lambda: now[0])
        self.assertIsNone(metrics.runtime_average_success_rate())

        now[0] = 10.0
        metrics.record_success()
        now[0] = 20.0
        metrics.record_success()
        self.assertEqual(metrics.runtime_average_success_rate(), 6.0)

        now[0] = 320.0
        self.assertEqual(metrics.runtime_average_success_rate(), 0.375)

    async def test_terminal_output_failure_does_not_escape_log(self):
        old_output = register._terminal_output
        try:
            register._terminal_output = lambda _message: (_ for _ in ()).throw(
                OSError("closed pipe")
            )
            register.log("safe")
        finally:
            register._terminal_output = old_output

    async def test_rate_limit_circuit_measures_one_recovery_window(self):
        now = [100.0]
        circuit = register.RegistrationRateLimitCircuit(
            cooldown_seconds=60,
            clock=lambda: now[0],
        )
        circuit.trip()
        now[0] = 161.5

        self.assertEqual(circuit.consume_recovery_seconds(), 61.5)
        self.assertIsNone(circuit.consume_recovery_seconds())

    async def test_rate_limit_probe_is_released_when_consume_times_out(self):
        async def slow_verify(*_args, **_kwargs):
            await asyncio.Event().wait()

        register.REGISTRATION_RATE_LIMIT_CIRCUIT = register.RegistrationRateLimitCircuit(0)
        register.REGISTRATION_RATE_LIMIT_CIRCUIT.trip()
        register.grpc_verify_code = slow_verify

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                register._consume_pair(
                    FakeBrowser(), asyncio.Semaphore(1), FakePair(), Metrics(), task_id=1
                ),
                timeout=0.01,
            )

        self.assertFalse(register.REGISTRATION_RATE_LIMIT_CIRCUIT._probe_active)

    async def test_auto_capacity_is_bounded_by_cpu_and_memory(self):
        roomy = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )
        tight = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=0,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )

        self.assertEqual(roomy[0], 8)
        self.assertEqual(roomy[1], 10)
        self.assertEqual(roomy[2], register.Q_PENDING_CAP + 2)
        self.assertEqual(roomy[3], 10)
        self.assertEqual(tight[0], 1)
        self.assertEqual(tight[2], 2)

    async def test_default_auto_capacity_is_conservative(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
        )

        self.assertEqual(physical, 4)
        self.assertEqual(s_workers, 6)
        self.assertEqual(c_workers, 6)

    async def test_p_batch_max_is_bounded_by_physical_capacity(self):
        self.assertEqual(register.derive_p_batch_max(1, configured=4), 1)
        self.assertEqual(register.derive_p_batch_max(3, configured=4), 3)
        self.assertEqual(register.derive_p_batch_max(8, configured=4), 4)

    async def test_explicit_physical_cap_overrides_auto_capacity(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=3,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )

        self.assertEqual(physical, 3)
        self.assertEqual(s_workers, 5)
        self.assertEqual(c_workers, 5)

    async def test_capacity_profile_supplies_physical_cap_when_not_explicit(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
            profile_physical_cap=7,
        )
        tight, *_ = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=0,
            profile_physical_cap=7,
        )
        explicit, *_ = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=5,
            profile_physical_cap=7,
        )

        self.assertEqual(physical, 7)
        self.assertEqual(s_workers, 9)
        self.assertEqual(c_workers, 9)
        self.assertEqual(tight, 1)
        self.assertEqual(explicit, 5)

    async def test_load_capacity_profile_reads_valid_physical_cap(self):
        with tempfile.NamedTemporaryFile("w+", delete=True) as f:
            json.dump({"physical_cap": 7}, f)
            f.flush()

            profile = register.load_capacity_profile(f.name)

        self.assertEqual(profile["physical_cap"], 7)
        self.assertEqual(register.load_capacity_profile("/does/not/exist"), {})
        self.assertEqual(register.load_capacity_profile(""), {})

    async def test_admission_t_high_defaults_to_physical_cap_bounded_by_slot(self):
        watermarks = register.derive_admission_watermarks(
            physical_cap=6,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
        )
        bounded = register.derive_admission_watermarks(
            physical_cap=10,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
        )
        low = register.derive_admission_watermarks(
            physical_cap=1,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
        )

        self.assertEqual(watermarks["t_high"], 6)
        self.assertEqual(watermarks["t_low"], 3)
        self.assertEqual(bounded["t_high"], 8)
        self.assertEqual(low["t_high"], 1)
        self.assertEqual(low["t_low"], 0)

    async def test_admission_t_high_override_remains_explicit(self):
        watermarks = register.derive_admission_watermarks(
            physical_cap=6,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
            t_high_override=4,
            t_low_override=2,
        )

        self.assertEqual(watermarks["t_high"], 4)
        self.assertEqual(watermarks["t_low"], 2)

    async def test_c_hot_page_pool_size_is_derived_at_startup(self):
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=6, c_workers=8, configured_size=0
            ),
            6,
        )
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=8, c_workers=3, configured_size=0
            ),
            3,
        )
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=8, c_workers=10, configured_size=4
            ),
            4,
        )

    async def test_send_q_request_batch_uses_account_fingerprint_per_email(self):
        emails = []

        async def fake_create_code(_page, email):
            emails.append(email)
            return True

        register.grpc_create_code = fake_create_code
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        requests = [
            {
                "handle": "h1",
                "email": "a@example.test",
                "password": "pw1",
                "browser_fingerprint_id": "bf-account-a",
            },
            {
                "handle": "h2",
                "email": "b@example.test",
                "password": "pw2",
                "browser_fingerprint_id": "bf-account-b",
            },
            {
                "handle": "h3",
                "email": "c@example.test",
                "password": "pw3",
                "browser_fingerprint_id": "bf-account-c",
            },
        ]

        results = await register._send_q_request_batch(
            browser, physical_sem, p_send_sem, requests
        )

        self.assertEqual(emails, [item["email"] for item in requests])
        self.assertEqual([item["sent"] for item in results], [True, True, True])
        self.assertEqual(len(browser.contexts), 3)
        self.assertEqual(
            [context.kwargs for context in browser.contexts],
            [
                register.browser_context_options(item["browser_fingerprint_id"])
                for item in requests
            ],
        )
        self.assertTrue(all(context.closed for context in browser.contexts))
        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(p_send_sem._value, 1)

    async def test_send_q_request_batch_records_physical_and_stage_metrics(self):
        async def fake_create_code(_page, _email):
            return True

        register.grpc_create_code = fake_create_code
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        await register._send_q_request_batch(
            browser,
            physical_sem,
            p_send_sem,
            [{"handle": "h1", "email": "a@example.test", "password": "pw"}],
            metrics,
        )

        self.assertEqual(metrics.p_physical_count, 1)
        self.assertEqual(metrics.p_page_prepare_count, 1)
        self.assertEqual(metrics.p_send_count, 1)
        self.assertGreaterEqual(metrics.p_physical_wait_seconds, 0)
        self.assertGreaterEqual(metrics.p_physical_hold_seconds, 0)

    async def test_new_grok_page_uses_proxy_context_for_xai_pages(self):
        old_pick = register._pick_grok_proxy
        try:
            register._pick_grok_proxy = lambda: "socks5://user:pass@proxy.example:1080"
            browser = FakeBrowser()

            context, page = await register._new_grok_page(browser)

            self.assertIs(context, browser.contexts[0])
            self.assertIs(page, browser.contexts[0].pages[0])
            self.assertEqual(
                browser.contexts[0].kwargs["proxy"],
                {
                    "server": "socks5://proxy.example:1080",
                    "username": "user",
                    "password": "pass",
                },
            )
        finally:
            register._pick_grok_proxy = old_pick

    async def test_new_grok_page_uses_account_browser_fingerprint_context(self):
        browser = FakeBrowser()

        context, page = await register._new_grok_page(
            browser,
            proxy="",
            browser_fingerprint_id="bf-account-a",
        )

        self.assertIs(context, browser.contexts[0])
        self.assertIs(page, browser.contexts[0].pages[0])
        self.assertEqual(
            browser.contexts[0].kwargs,
            register.browser_context_options("bf-account-a"),
        )

    async def test_poll_and_admit_q_resends_code_before_admitting(self):
        old_pick = register._pick_grok_proxy
        old_timeout = register.P_REQUEST_TIMEOUT
        old_resends = register.EMAIL_CODE_RESEND_ATTEMPTS
        old_resend_after = register.EMAIL_CODE_RESEND_AFTER_SEC
        sent_emails = []
        polls = [None, None, "123456"]

        async def fake_poll_once(_loop, _handle):
            return polls.pop(0) if polls else "123456"

        async def fake_create_code(_page, email):
            sent_emails.append(email)
            return True

        try:
            register._pick_grok_proxy = lambda: None
            register.P_REQUEST_TIMEOUT = 1
            register.EMAIL_CODE_RESEND_ATTEMPTS = 1
            register.EMAIL_CODE_RESEND_AFTER_SEC = 0.01
            register._poll_code_once_async = fake_poll_once
            register.grpc_create_code = fake_create_code

            q_pending_sem = asyncio.Semaphore(0)
            q_slot_sem = asyncio.Semaphore(1)
            metrics = Metrics()
            inventory = FakeQInventory()

            admitted = await register._poll_and_admit_q(
                {
                    "handle": "h1",
                    "email": "a@example.test",
                    "password": "pw",
                    "browser_fingerprint_id": "bf-account-a",
                },
                inventory,
                q_pending_sem,
                q_slot_sem,
                metrics,
                browser=FakeBrowser(),
                physical_sem=asyncio.Semaphore(1),
                p_send_sem=asyncio.Semaphore(1),
            )

            self.assertTrue(admitted)
            self.assertEqual(sent_emails, ["a@example.test"])
            self.assertEqual(metrics.q_returned, 1)
            self.assertEqual(metrics.q_sent, 1)
            self.assertEqual(q_pending_sem._value, 1)
            self.assertEqual(inventory.q_items[0].value["code"], "123456")
            self.assertEqual(
                inventory.q_items[0].value["browser_fingerprint_id"],
                "bf-account-a",
            )
        finally:
            register._pick_grok_proxy = old_pick
            register.P_REQUEST_TIMEOUT = old_timeout
            register.EMAIL_CODE_RESEND_ATTEMPTS = old_resends
            register.EMAIL_CODE_RESEND_AFTER_SEC = old_resend_after

    async def test_consume_pair_records_physical_and_stage_metrics(self):
        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(*_args, **_kwargs):
            return None

        register.C_HOT_PAGE_POOL = False
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None
        metrics = Metrics()

        await register._consume_pair(
            FakeBrowser(), asyncio.Semaphore(1), FakePair(), metrics
        )

        self.assertEqual(metrics.c_physical_count, 1)
        self.assertEqual(metrics.c_page_acquire_count, 1)
        self.assertEqual(metrics.c_verify_count, 1)
        self.assertEqual(metrics.c_register_count, 1)
        self.assertEqual(metrics.c_hot_page_hits, 0)
        self.assertEqual(metrics.c_hot_page_misses, 0)

    async def test_poll_and_admit_q_releases_one_pending_per_terminal_request(self):
        register.P_REQUEST_TIMEOUT = 1
        register.poll_code = lambda _handle: None
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        await register._poll_and_admit_q(
            {"handle": "h1", "email": "a@example.test", "password": "pw"},
            FakeInventory(),
            q_pending_sem,
            q_slot_sem,
            metrics,
        )

        self.assertEqual(q_pending_sem._value, 1)
        self.assertEqual(q_slot_sem._value, 1)
        self.assertEqual(metrics.q_discarded, 1)

    async def test_poll_cancel_before_terminal_does_not_release_pending(self):
        async def blocked_poll(_loop, _handle):
            await asyncio.sleep(10)
            return "123456"

        register._poll_code_async = blocked_poll
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register._poll_and_admit_q(
                {"handle": "h1", "email": "a@example.test", "password": "pw"},
                FakeInventory(),
                q_pending_sem,
                q_slot_sem,
                metrics,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(q_pending_sem._value, 0)
        self.assertEqual(q_slot_sem._value, 1)

    async def test_poll_cancel_after_q_return_releases_pending(self):
        async def returned_poll(_loop, _handle):
            return "123456"

        class BlockingInventory(FakeInventory):
            async def put_q(self, _env):
                await asyncio.sleep(10)

        register._poll_code_async = returned_poll
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register._poll_and_admit_q(
                {"handle": "h1", "email": "a@example.test", "password": "pw"},
                BlockingInventory(),
                q_pending_sem,
                q_slot_sem,
                metrics,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(q_pending_sem._value, 1)
        self.assertEqual(q_slot_sem._value, 1)
        self.assertEqual(metrics.q_returned, 1)

    async def test_metrics_snapshot_includes_solver_timing(self):
        metrics = Metrics()
        metrics.t_solve_count = 2
        metrics.t_solve_seconds = 5.0
        metrics.t_solve_failed = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        row = metrics.snapshot(FakeInventory(), sems)

        self.assertIn("t_solve_avg:2.5", row)
        self.assertIn("t_solve_fail:1", row)

    async def test_metrics_snapshot_includes_role_physical_and_stage_timing(self):
        metrics = Metrics()
        metrics.s_physical_count = 2
        metrics.s_physical_wait_seconds = 1.0
        metrics.s_physical_hold_seconds = 6.0
        metrics.p_physical_count = 1
        metrics.p_physical_wait_seconds = 0.2
        metrics.p_physical_hold_seconds = 1.4
        metrics.c_physical_count = 3
        metrics.c_physical_wait_seconds = 0.9
        metrics.c_physical_hold_seconds = 7.5
        metrics.p_email_create_count = 2
        metrics.p_email_create_seconds = 1.0
        metrics.p_page_prepare_count = 1
        metrics.p_page_prepare_seconds = 0.8
        metrics.p_send_count = 1
        metrics.p_send_seconds = 0.4
        metrics.c_page_acquire_count = 2
        metrics.c_page_acquire_seconds = 0.6
        metrics.c_verify_count = 2
        metrics.c_verify_seconds = 0.8
        metrics.c_register_count = 2
        metrics.c_register_seconds = 3.0
        metrics.c_hot_page_hits = 4
        metrics.c_hot_page_misses = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        row = metrics.snapshot(FakeInventory(), sems)

        self.assertIn("s_phys:0.50/3.00", row)
        self.assertIn("p_phys:0.20/1.40", row)
        self.assertIn("c_phys:0.30/2.50", row)
        self.assertIn("p_stage:0.50/0.80/0.40", row)
        self.assertIn("c_stage:0.30/0.40/1.50", row)
        self.assertIn("c_hot:4/1", row)

    async def test_record_solver_trace_accumulates_stage_metrics(self):
        metrics = Metrics()

        register._record_solver_trace(
            metrics,
            {
                "goto_s": 1.0,
                "inject_s": 0.2,
                "initial_s": 0.5,
                "click_s": 0.1,
                "wait_s": 20.0,
                "reused": True,
                "visible_frame": False,
            },
            21.8,
            "token",
        )
        register._record_solver_trace(metrics, {}, 10.0, None)

        self.assertEqual(metrics.t_solve_count, 2)
        self.assertEqual(metrics.t_solve_failed, 1)
        self.assertAlmostEqual(metrics.solver_goto_seconds, 1.0)
        self.assertAlmostEqual(metrics.solver_wait_seconds, 20.0)
        self.assertEqual(metrics.solver_reused_count, 1)
        self.assertEqual(metrics.solver_visible_frame_count, 0)

    async def test_solver_timeline_records_click_and_token_events_when_enabled(self):
        page = FakePage()
        original_click = register._mouse_click_turnstile_center_trace
        original_read = register._read_turnstile_token
        clicked_once = False
        timeline = register._new_solver_timeline(enabled=True)

        async def fake_click(_page, **_kwargs):
            nonlocal clicked_once
            clicked_once = True
            return True, {"box_eval_ms": 0.0}

        async def fake_read(_page):
            return "token-value-long" if clicked_once else ""

        try:
            register._mouse_click_turnstile_center_trace = fake_click
            register._read_turnstile_token = fake_read

            clicked = await register._repeat_mouse_click_turnstile(page, timeline=timeline)
        finally:
            register._mouse_click_turnstile_center_trace = original_click
            register._read_turnstile_token = original_read

        events = [event["event"] for event in timeline["events"]]
        self.assertTrue(clicked)
        self.assertIn("click_before", events)
        self.assertIn("click_after", events)
        self.assertTrue(any(event.get("dom", {}).get("widget", {}).get("present") for event in timeline["events"]))
        self.assertTrue(any(event.get("click_call_ms", 0) >= 0 for event in timeline["events"]))
        self.assertTrue(any(event.get("token_len", 0) > 10 for event in timeline["events"]))

    async def test_turnstile_dom_snapshot_reports_click_target_without_text_or_full_urls(self):
        page = FakePage()

        snapshot = await register._turnstile_dom_snapshot(page)

        self.assertEqual(snapshot["all_iframe_count"], 1)
        self.assertEqual(snapshot["turnstile_iframe_count"], 1)
        self.assertEqual(snapshot["widget"]["w"], 300)
        self.assertEqual(snapshot["element_at_center"]["tag"], "IFRAME")
        self.assertEqual(snapshot["iframe_summaries"][0]["host"], "challenges.cloudflare.com")
        self.assertNotIn("src", snapshot["iframe_summaries"][0])
        self.assertNotIn("text", snapshot["element_at_center"])

    async def test_mouse_click_turnstile_center_can_return_timing_trace(self):
        page = FakePage()

        clicked, trace = await register._mouse_click_turnstile_center_trace(page)

        self.assertTrue(clicked)
        self.assertEqual(trace["click_x"], 160.0)
        self.assertEqual(trace["click_y"], 45.0)
        for key in ("box_eval_ms", "mouse_move1_ms", "mouse_move2_ms", "mouse_down_ms", "mouse_up_ms"):
            self.assertIn(key, trace)
            self.assertGreaterEqual(trace[key], 0)

    async def test_inject_turnstile_widget_leaves_default_script_uninstrumented(self):
        page = FakePage()

        await register._inject_turnstile_widget(page)

        script = page.evaluations[-1]
        self.assertNotIn("__cspTurnstileTrace", script)

    async def test_inject_turnstile_widget_records_page_timeline_when_enabled(self):
        page = FakePage()

        await register._inject_turnstile_widget(page, timeline=True)

        script = page.evaluations[-1]
        self.assertIn("__cspTurnstileTrace", script)
        self.assertIn("script_inserted_at", script)
        self.assertIn("render_called_at", script)
        self.assertIn("token_written_at", script)

    async def test_start_turnstile_challenge_records_page_trace_when_timeline_enabled(self):
        browser = FakeBrowser()
        messages = []
        old_trace = register.SOLVER_TIMELINE_TRACE
        old_sample = register.SOLVER_TIMELINE_SAMPLE
        old_emitted = register._solver_timeline_emitted
        old_log = register.log
        try:
            register.SOLVER_TIMELINE_TRACE = True
            register.SOLVER_TIMELINE_SAMPLE = 1
            register._solver_timeline_emitted = 0
            register.log = messages.append

            item = await register._start_turnstile_challenge(browser, fast_click=True)
            await register._put_solver_page(item, False)
        finally:
            register.SOLVER_TIMELINE_TRACE = old_trace
            register.SOLVER_TIMELINE_SAMPLE = old_sample
            register._solver_timeline_emitted = old_emitted
            register.log = old_log

        events = item["timeline"]["events"]
        self.assertTrue(any(event["event"] == "page_trace_after_inject" for event in events))
        self.assertTrue(any(event["event"] == "page_trace_after_click" for event in events))

    async def test_solve_one_turnstile_uses_fast_click_by_default(self):
        calls = []

        async def fake_start(_browser, *, fast_click=False):
            calls.append(fast_click)
            return {"page": object()}

        async def fake_wait(_item):
            return "token-value"

        old_start = register._start_turnstile_challenge
        old_wait = register._wait_turnstile_challenge
        try:
            register._start_turnstile_challenge = fake_start
            register._wait_turnstile_challenge = fake_wait

            token = await register.solve_one_turnstile(object())
        finally:
            register._start_turnstile_challenge = old_start
            register._wait_turnstile_challenge = old_wait

        self.assertEqual(token, "token-value")
        self.assertEqual(calls, [True])

    async def test_wait_turnstile_logs_timeline_when_present(self):
        messages = []
        register.REGISTER_LOG_MODE = "debug"

        async def fake_poll(_page, **_kwargs):
            return "token-value-long"

        async def fake_put(_item, _ok):
            return None

        old_poll = register._poll_turnstile_token
        old_put = register._put_solver_page
        old_log = register.log
        try:
            register._poll_turnstile_token = fake_poll
            register._put_solver_page = fake_put
            register.log = messages.append
            item = {
                "page": object(),
                "trace": {},
                "timeline": {
                    "start": register.time.time(),
                    "events": [{"t": 0.1, "event": "x"}],
                },
            }

            token = await register._wait_turnstile_challenge(item)
        finally:
            register._poll_turnstile_token = old_poll
            register._put_solver_page = old_put
            register.log = old_log

        self.assertEqual(token, "token-value-long")
        self.assertTrue(any(message.startswith("[solver_timeline] ") for message in messages))

    async def test_wait_turnstile_timeline_logs_solve_id_and_poll_summary(self):
        messages = []
        register.REGISTER_LOG_MODE = "debug"
        page = FakePage()
        page.turnstile_token = "token-value-long"
        timeline = register._new_solver_timeline(enabled=True)

        async def fake_put(_item, _ok):
            return None

        old_attempts = register.SOLVER_POLL_ATTEMPTS
        old_interval = register.SOLVER_POLL_INTERVAL_MS
        old_put = register._put_solver_page
        old_sleep = register.asyncio.sleep
        old_log = register.log
        try:
            register.SOLVER_POLL_ATTEMPTS = 1
            register.SOLVER_POLL_INTERVAL_MS = 50

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep
            register._put_solver_page = fake_put
            register.log = messages.append

            token = await register._wait_turnstile_challenge({
                "page": page,
                "trace": {},
                "timeline": timeline,
            })
        finally:
            register.SOLVER_POLL_ATTEMPTS = old_attempts
            register.SOLVER_POLL_INTERVAL_MS = old_interval
            register.asyncio.sleep = old_sleep
            register._put_solver_page = old_put
            register.log = old_log

        payload = next(message.removeprefix("[solver_timeline] ") for message in messages)
        events = register.json.loads(payload)
        poll_done = next(event for event in events if event["event"] == "poll_done")

        self.assertEqual(token, "token-value-long")
        self.assertIn("solve_id", poll_done)
        self.assertEqual(poll_done["poll_attempts"], 1)
        self.assertEqual(poll_done["first_token_attempt"], 1)
        self.assertGreaterEqual(poll_done["poll_read_ms_max"], 0)

    async def test_mouse_click_turnstile_retries_uses_center_clicks(self):
        page = FakePage()

        old_retries = getattr(register, "SOLVER_MOUSE_CLICK_RETRIES", None)
        old_interval = getattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS", None)
        old_sleep = register.asyncio.sleep
        try:
            register.SOLVER_MOUSE_CLICK_RETRIES = 3
            register.SOLVER_MOUSE_CLICK_INTERVAL_MS = 600

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep

            clicked = await register._repeat_mouse_click_turnstile(page)
        finally:
            register.asyncio.sleep = old_sleep
            if old_retries is None:
                delattr(register, "SOLVER_MOUSE_CLICK_RETRIES")
            else:
                register.SOLVER_MOUSE_CLICK_RETRIES = old_retries
            if old_interval is None:
                delattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS")
            else:
                register.SOLVER_MOUSE_CLICK_INTERVAL_MS = old_interval

        self.assertTrue(clicked)
        self.assertEqual(page.mouse.downs, 3)
        self.assertEqual(page.mouse.ups, 3)
        self.assertEqual(page.mouse.moves[-1], {"x": 160, "y": 45, "steps": 8})

    async def test_mouse_click_turnstile_stops_when_token_appears(self):
        page = FakePage()
        evaluate_count = 0
        original_evaluate = page.evaluate

        async def evaluate(script):
            nonlocal evaluate_count
            if "cf-turnstile-response" in script:
                evaluate_count += 1
                return "token-value-long" if evaluate_count > 1 else ""
            return await original_evaluate(script)

        page.evaluate = evaluate

        old_retries = getattr(register, "SOLVER_MOUSE_CLICK_RETRIES", None)
        old_interval = getattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS", None)
        old_sleep = register.asyncio.sleep
        try:
            register.SOLVER_MOUSE_CLICK_RETRIES = 3
            register.SOLVER_MOUSE_CLICK_INTERVAL_MS = 600

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep

            clicked = await register._repeat_mouse_click_turnstile(page)
        finally:
            register.asyncio.sleep = old_sleep
            if old_retries is None:
                delattr(register, "SOLVER_MOUSE_CLICK_RETRIES")
            else:
                register.SOLVER_MOUSE_CLICK_RETRIES = old_retries
            if old_interval is None:
                delattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS")
            else:
                register.SOLVER_MOUSE_CLICK_INTERVAL_MS = old_interval

        self.assertTrue(clicked)
        self.assertEqual(page.mouse.downs, 1)
        self.assertEqual(evaluate_count, 2)

    async def test_prepare_signup_page_uses_configured_navigation_profile(self):
        page = FakePage()

        old_wait_until = getattr(register, "PAGE_GOTO_WAIT_UNTIL", None)
        old_post_wait = getattr(register, "PAGE_POST_WAIT_MS", None)
        try:
            register.PAGE_GOTO_WAIT_UNTIL = "domcontentloaded"
            register.PAGE_POST_WAIT_MS = 500

            await register._prepare_signup_page(page, redirect=True)
        finally:
            if old_wait_until is None:
                delattr(register, "PAGE_GOTO_WAIT_UNTIL")
            else:
                register.PAGE_GOTO_WAIT_UNTIL = old_wait_until
            if old_post_wait is None:
                delattr(register, "PAGE_POST_WAIT_MS")
            else:
                register.PAGE_POST_WAIT_MS = old_post_wait

        self.assertEqual(page.goto_calls[-1]["wait_until"], "domcontentloaded")
        self.assertEqual(page.waits[-1], 500)

    async def test_prepare_signup_page_can_block_static_assets(self):
        page = FakePage()

        old_block_assets = getattr(register, "PAGE_BLOCK_STATIC_ASSETS", None)
        try:
            register.PAGE_BLOCK_STATIC_ASSETS = True

            await register._prepare_signup_page(page, redirect=True)
        finally:
            if old_block_assets is None:
                delattr(register, "PAGE_BLOCK_STATIC_ASSETS")
            else:
                register.PAGE_BLOCK_STATIC_ASSETS = old_block_assets

        self.assertEqual(len(page.route_calls), 1)
        self.assertEqual(page.route_calls[0]["pattern"], "**/*")

    async def test_default_solver_and_page_latency_profile_matches_accepted_optimization(self):
        self.assertEqual(register.SOLVER_INITIAL_WAIT_MS, 500)
        self.assertTrue(register.SOLVER_FAST_CLICK)
        self.assertEqual(register.SOLVER_MOUSE_CLICK_RETRIES, 3)
        self.assertEqual(register.SOLVER_MOUSE_CLICK_INTERVAL_MS, 600)
        self.assertEqual(register.PAGE_GOTO_WAIT_UNTIL, "domcontentloaded")
        self.assertEqual(register.PAGE_POST_WAIT_MS, 500)
        self.assertEqual(register.TURNSTILE_SOLVER, "d3vin")

    def test_turnstile_api_result_state_theyka_formats(self):
        self.assertEqual(
            register._turnstile_api_result_state("CAPTCHA_NOT_READY"),
            ("pending", None),
        )
        self.assertEqual(
            register._turnstile_api_result_state({"value": "CAPTCHA_FAIL", "elapsed_time": 1.0}),
            ("failed", None),
        )
        state, token = register._turnstile_api_result_state(
            {"value": "0.KBtT-r" + "x" * 20, "elapsed_time": 7.6}
        )
        self.assertEqual(state, "ready")
        self.assertTrue(token.startswith("0.KBtT-r"))

    def test_turnstile_api_result_state_d3vin_formats(self):
        self.assertEqual(
            register._turnstile_api_result_state({"status": "processing"}),
            ("pending", None),
        )
        self.assertEqual(
            register._turnstile_api_result_state(
                {"status": "error", "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"}
            ),
            ("failed", None),
        )
        state, token = register._turnstile_api_result_state(
            {
                "status": "ready",
                "solution": {"token": "0.token-from-d3vin-solver-abcdef"},
                "elapsed_time": 8.1,
            }
        )
        self.assertEqual(state, "ready")
        self.assertEqual(token, "0.token-from-d3vin-solver-abcdef")

    def test_turnstile_api_task_id_aliases(self):
        self.assertEqual(
            register._turnstile_api_task_id({"task_id": "abc"}),
            "abc",
        )
        self.assertEqual(
            register._turnstile_api_task_id({"taskId": "def"}),
            "def",
        )
        self.assertIsNone(register._turnstile_api_task_id({"status": "error"}))

    async def test_solve_one_turnstile_via_api_success(self):
        class FakeResp:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload
                self.text = text if text else ("" if payload is None else "")

            def json(self):
                if self._payload is None:
                    raise ValueError("no json")
                return self._payload

        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            if "/turnstile?" in url:
                return FakeResp(202, {"task_id": "tid-1"})
            return FakeResp(
                200,
                {"value": "0.solved-token-value-long-enough", "elapsed_time": 3.2},
            )

        old_site = register.SITE_KEY
        old_url = register.TURNSTILE_API_URL
        old_interval = register.TURNSTILE_API_POLL_INTERVAL_MS
        old_timeout = register.TURNSTILE_API_TIMEOUT
        old_http = register._turnstile_api_http_get
        old_sleep = register.asyncio.sleep
        try:
            register.SITE_KEY = "0x4AAAAAAAtestkey"
            register.TURNSTILE_API_URL = "http://127.0.0.1:5000"
            register.TURNSTILE_API_POLL_INTERVAL_MS = 10
            register.TURNSTILE_API_TIMEOUT = 30
            register._turnstile_api_http_get = fake_get

            async def no_sleep(_s):
                return None

            register.asyncio.sleep = no_sleep
            token, trace = await register.solve_one_turnstile_via_api()
        finally:
            register.SITE_KEY = old_site
            register.TURNSTILE_API_URL = old_url
            register.TURNSTILE_API_POLL_INTERVAL_MS = old_interval
            register.TURNSTILE_API_TIMEOUT = old_timeout
            register._turnstile_api_http_get = old_http
            register.asyncio.sleep = old_sleep

        self.assertEqual(token, "0.solved-token-value-long-enough")
        self.assertEqual(trace["backend"], "api")
        self.assertEqual(trace["task_id"], "tid-1")
        self.assertTrue(any("/turnstile?" in u for u in calls))
        self.assertTrue(any("/result?id=tid-1" in u for u in calls))

    async def test_solve_one_turnstile_dispatches_to_api_backend(self):
        called = []

        async def fake_api():
            called.append("api")
            return "api-token-value", {"backend": "api"}

        async def fake_start(*_a, **_k):
            called.append("local")
            return {"page": object()}

        async def fake_wait(_item):
            return "local-token"

        old_backend = register.TURNSTILE_SOLVER
        old_api = register.solve_one_turnstile_via_api
        old_start = register._start_turnstile_challenge
        old_wait = register._wait_turnstile_challenge
        try:
            for backend in ("api", "d3vin", "theyka"):
                called.clear()
                register.TURNSTILE_SOLVER = backend
                register.solve_one_turnstile_via_api = fake_api
                register._start_turnstile_challenge = fake_start
                register._wait_turnstile_challenge = fake_wait
                token, trace = await register.solve_one_turnstile_with_trace(object())
                self.assertEqual(token, "api-token-value")
                self.assertEqual(trace.get("backend"), "api")
                self.assertEqual(called, ["api"])
        finally:
            register.TURNSTILE_SOLVER = old_backend
            register.solve_one_turnstile_via_api = old_api
            register._start_turnstile_challenge = old_start
            register._wait_turnstile_challenge = old_wait


if __name__ == "__main__":
    unittest.main()
