"""
Web control plane + dashboard for grok-free-register.

Read-only by default; optional process start/stop when CONTROL_PLANE_ALLOW_ACTIONS=1.

Auth (optional, via env):
  DASHBOARD_PASSWORD / CONTROL_PLANE_PASSWORD  — enable HTTP Basic (user default admin)
  DASHBOARD_USER / CONTROL_PLANE_USER          — Basic username (default: admin)
  CONTROL_PLANE_TOKEN / DASHBOARD_TOKEN        — Bearer token alternative

  python -m grok_register.dashboard
  bash start.sh --dashboard
  open http://127.0.0.1:8787/
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from grok_register.runtime_status import (
    process_alive,
    read_pid,
    read_status,
    status_path,
)
from grok_register.config_catalog import GROUPS, catalog_public, presets_public
from grok_register.env_store import (
    apply_preset,
    delete_env_keys,
    load_config_view,
    read_env_raw,
    update_env_values,
    write_env_raw,
)
from grok_register import account_inventory as inv
from grok_register import account_convert as acct_convert
from grok_register import proxy_batch_test as proxy_batch
from grok_register import cliproxyapi as cpa_sync
from grok_register import job_store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = (os.environ.get("DASHBOARD_HOST") or "127.0.0.1").strip()
DEFAULT_PORT = int(os.environ.get("DASHBOARD_PORT") or "8787")
# Control plane: config editable + start/stop workers. Progress is always readable.
ALLOW_ACTIONS = (os.environ.get("CONTROL_PLANE_ALLOW_ACTIONS") or "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _env_first(*keys: str, default: str = "") -> str:
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return default


# Password gate: any of these non-empty enables auth
DASHBOARD_USER = _env_first("DASHBOARD_USER", "CONTROL_PLANE_USER", default="admin")
DASHBOARD_PASSWORD = _env_first(
    "DASHBOARD_PASSWORD",
    "CONTROL_PLANE_PASSWORD",
    "PANEL_PASSWORD",
)
CONTROL_PLANE_TOKEN = _env_first(
    "CONTROL_PLANE_TOKEN",
    "DASHBOARD_TOKEN",
    "PANEL_TOKEN",
)
# Paths that stay public (HF Space / k8s health probes hit GET / and expect HTTP 200).
# HTML shell is public; JSON APIs and downloads still require auth when configured.
AUTH_PUBLIC_PATHS = frozenset({
    "/",
    "/index.html",
    "/api/health",
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/favicon.ico",
})

_action_lock = threading.Lock()
_last_action: dict = {"action": None, "ok": None, "message": "", "at": 0}
_last_probe: dict = {"ok": None, "at": 0, "message": "", "results": []}
_LAST_ACTION_PATH = PROJECT_ROOT / "logs" / "dashboard-last-action.json"
_LAST_PROBE_PATH = PROJECT_ROOT / "logs" / "dashboard-last-probe.json"


def auth_required() -> bool:
    """True when password or token is configured."""
    return bool(DASHBOARD_PASSWORD or CONTROL_PLANE_TOKEN)


def public_dashboard_url(bind_host: str | None = None, bind_port: int | None = None) -> str:
    """Human-facing URL for logs / status.

    HF Space sets SPACE_ID=owner/name → https://owner-name.hf.space
    (optional SPACE_HOST / DASHBOARD_PUBLIC_URL override).
    Local binds on 0.0.0.0 are shown as 127.0.0.1 for clickability.
    """
    for key in ("DASHBOARD_PUBLIC_URL", "PUBLIC_URL", "SPACE_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw:
            return raw if "://" in raw else f"https://{raw}"

    space_host = (os.environ.get("SPACE_HOST") or "").strip()
    if not space_host:
        space_id = (os.environ.get("SPACE_ID") or "").strip()
        if space_id:
            # Murasame52/open-webui → Murasame52-open-webui.hf.space
            space_host = space_id.replace("/", "-")
    if space_host:
        host = space_host
        if host.startswith("https://"):
            host = host[len("https://") :]
        elif host.startswith("http://"):
            host = host[len("http://") :]
        host = host.strip("/").split("/")[0]
        if not host.endswith(".hf.space"):
            host = f"{host}.hf.space"
        return f"https://{host}/"

    host = (bind_host if bind_host is not None else DEFAULT_HOST) or "127.0.0.1"
    port = int(bind_port if bind_port is not None else DEFAULT_PORT)
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}/"


def _test_moemail() -> dict:
    """Probe MoeMail config endpoint with current env (no file access needed)."""
    import urllib.error
    import urllib.request

    api = (os.environ.get("MOEMAIL_API") or "https://moemail.app").strip().rstrip("/")
    key = (os.environ.get("MOEMAIL_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "message": "MOEMAIL_API_KEY empty", "api": api}
    req = urllib.request.Request(
        api + "/api/config",
        headers={"X-API-Key": key, "Accept": "application/json", "User-Agent": "grok-register-panel"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()[:800]
            return {
                "ok": resp.status < 400,
                "status": resp.status,
                "api": api,
                "body": body.decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as exc:
        try:
            b = exc.read()[:400].decode("utf-8", errors="replace")
        except Exception:
            b = ""
        return {"ok": False, "status": exc.code, "api": api, "body": b, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "api": api, "message": str(exc)}


def _test_turnstile_api() -> dict:
    """Health-check configured Turnstile solver URL."""
    url = (os.environ.get("TURNSTILE_API_URL") or "http://127.0.0.1:5080").rstrip("/")
    try:
        from grok_register.turnstile_solver import health_check

        ok = health_check(url, timeout=2.5)
        return {"ok": ok, "url": url, "message": "healthy" if ok else "unreachable"}
    except Exception as exc:
        return {"ok": False, "url": url, "message": str(exc)}


def _const_eq(a: str, b: str) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(
        hashlib.sha256(a.encode("utf-8")).digest(),
        hashlib.sha256(b.encode("utf-8")).digest(),
    )


def _check_basic_auth(header_val: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return False
    if not header_val or not header_val.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header_val.split(" ", 1)[1].strip()).decode("utf-8")
    except Exception:
        return False
    if ":" not in raw:
        return False
    user, _, password = raw.partition(":")
    return _const_eq(user, DASHBOARD_USER) and _const_eq(password, DASHBOARD_PASSWORD)


def _check_bearer_auth(header_val: str) -> bool:
    if not CONTROL_PLANE_TOKEN:
        return False
    if not header_val:
        return False
    val = header_val.strip()
    token = ""
    if val.lower().startswith("bearer "):
        token = val[7:].strip()
    elif val.lower().startswith("token "):
        token = val[6:].strip()
    else:
        # allow raw token in Authorization for simple clients
        token = val
    return _const_eq(token, CONTROL_PLANE_TOKEN)


def _check_query_token(qs: dict) -> bool:
    if not CONTROL_PLANE_TOKEN:
        return False
    for key in ("token", "access_token", "api_token"):
        vals = qs.get(key) or []
        if vals and _const_eq(str(vals[0]), CONTROL_PLANE_TOKEN):
            return True
    return False


def _load_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json_file(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _record_last_action(action: str, result: dict) -> None:
    _last_action.update(
        {
            "action": action,
            "ok": result.get("ok"),
            "message": result.get("message") or "",
            "at": time.time(),
            "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )
    _save_json_file(_LAST_ACTION_PATH, dict(_last_action))


# restore durable UI state after refresh / process restart
_loaded_action = _load_json_file(_LAST_ACTION_PATH)
if _loaded_action:
    _last_action.update(_loaded_action)
_loaded_probe = _load_json_file(_LAST_PROBE_PATH)
if _loaded_probe:
    _last_probe.update(_loaded_probe)


def _accounts_count() -> int:
    try:
        return inv.inventory_summary()["total"]
    except Exception:
        path = PROJECT_ROOT / "keys" / "accounts.txt"
        try:
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0


def build_accounts_payload(*, limit: int = 500, status: str = "", fmt: str = "") -> dict:
    """Account list + summary for control plane."""
    records = inv.scan_accounts()
    status_f = (status or "").strip().lower()
    fmt_f = (fmt or "").strip().lower()
    if status_f:
        records = [r for r in records if r.status == status_f]
    if fmt_f:
        records = [r for r in records if fmt_f in r.formats]
    total = len(records)
    limit = max(1, min(int(limit or 500), 2000))
    items = [r.to_dict() for r in records[:limit]]
    # strip absolute paths for UI safety (relative under KEY_EXPORT_DIR)
    root = inv.key_export_dir()
    for item in items:
        paths = item.get("paths") or {}
        rel = {}
        for k, p in paths.items():
            try:
                rel[k] = str(Path(p).resolve().relative_to(root.resolve()))
            except Exception:
                rel[k] = Path(p).name
        item["paths"] = rel
    summary = inv.inventory_summary()
    return {
        "ok": True,
        "summary": summary,
        "total": total,
        "returned": len(items),
        "limit": limit,
        "filter": {"status": status_f, "format": fmt_f},
        "accounts": items,
        "downloads": {
            "legacy": "/api/download?format=legacy",
            "sub2api": "/api/download?format=sub2api",
            "cpa_zip": "/api/download?format=cpa_zip",
            "recovery_zip": "/api/download?format=recovery",
            "accounts_txt": "/api/download?format=accounts_txt",
            "auth_sessions": "/api/download?format=auth_sessions",
            "browser_fingerprints": "/api/download?format=browser_fingerprints",
            "grok_txt": "/api/download?format=grok_txt",
            "protocol_log": "/api/download?format=protocol_log",
        },
        "recovery_files": inv.list_recovery_files(),
    }


def _scraper_stats() -> dict:
    report = PROJECT_ROOT / "logs" / "proxy-scraper-report.json"
    candidates = PROJECT_ROOT / "logs" / "proxy-scraper-candidates.txt"
    out = {"candidates_file": str(candidates), "candidates": 0, "report": None}
    try:
        out["candidates"] = sum(
            1 for line in candidates.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    except OSError:
        pass
    try:
        out["report"] = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        pass
    return out


def _proxy_active_count() -> int:
    path = PROJECT_ROOT / "logs" / "proxy-auto-active.txt"
    try:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError:
        return 0


def _env_public() -> dict:
    keys = (
        "EMAIL_MODE",
        "TURNSTILE_SOLVER",
        "TURNSTILE_API_URL",
        "PROXY_AUTO_FETCH_ENABLED",
        "PROXY_WORKER_ENGINE",
        "TARGET",
        "PHYSICAL_CAP",
        "S_WORKERS",
    )
    return {k: os.environ.get(k, "") for k in keys}


def build_overview() -> dict:
    status = read_status()
    pid = read_pid()
    alive = process_alive(pid)
    metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
    try:
        from grok_register.polyglot import stack_status

        polyglot = stack_status()
    except Exception as exc:
        polyglot = {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "time": time.time(),
        "public_url": public_dashboard_url(),
        "space_id": (os.environ.get("SPACE_ID") or "").strip() or None,
        "polyglot": polyglot,
        "register": {
            "running": alive,
            "pid": pid if alive else None,
            "status_file": str(status_path()),
            "status_age_sec": (
                round(time.time() - float(status.get("updated_at") or 0), 1)
                if status.get("updated_at")
                else None
            ),
            "snapshot": status if status else None,
        },
        "accounts": _accounts_overview_block(),
        "proxies": {
            "active": _proxy_active_count(),
            "scraper": _scraper_stats(),
            "batch_job": proxy_batch.job_status(),
            "use_public_default": (
                (os.environ.get("DASHBOARD_USE_PUBLIC_PROXIES") or "0").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
        },
        "config": _env_public(),
        "config_full": load_config_view(reveal_secrets=False),
        "config_groups": [{"id": g, "label": lab} for g, lab in GROUPS],
        "config_presets": presets_public(),
        "auth_required": auth_required(),
        "actions_enabled": ALLOW_ACTIONS,
        "last_action": _last_action,
        "engines": {
            "register": (os.environ.get("REGISTER_ENGINE") or "python").strip().lower(),
            "proxy_worker": (os.environ.get("PROXY_WORKER_ENGINE") or "go").strip().lower(),
            "inventory": (os.environ.get("INVENTORY_ENGINE") or "rust").strip().lower(),
            "turnstile": (os.environ.get("TURNSTILE_SOLVER") or "hybrid").strip().lower(),
        },
        "summary": {
            "success": (metrics.get("success_count") if metrics else status.get("success_count")),
            "starts": (metrics.get("registration_starts") if metrics else None),
            "t_depth": (metrics.get("t") or {}).get("depth") if metrics else None,
            "q_depth": (metrics.get("q") or {}).get("depth") if metrics else None,
            "pair_ok": (metrics.get("pair") or {}).get("ok") if metrics else None,
            "pair_fail": (metrics.get("pair") or {}).get("fail") if metrics else None,
            "t_prod": (metrics.get("t") or {}).get("produced") if metrics else None,
            "rate": metrics.get("rate_per_min") if metrics else None,
        },
        "products": {
            "sub2api": "/api/download?format=sub2api",
            "cpa_zip": "/api/download?format=cpa_zip",
            "legacy": "/api/download?format=legacy",
            "recovery_zip": "/api/download?format=recovery",
            "accounts_txt": "/api/download?format=accounts_txt",
            "auth_sessions": "/api/download?format=auth_sessions",
            "browser_fingerprints": "/api/download?format=browser_fingerprints",
            "grok_txt": "/api/download?format=grok_txt",
            "protocol_log": "/api/download?format=protocol_log",
            "register_log": "/api/download?format=register_log",
            "register_fail": "/api/download?format=register_fail",
            "register_logs_zip": "/api/download?format=register_logs_zip",
        },
        "recovery_files": inv.list_recovery_files(),
        "run_logs": _run_logs_block(),
        "xai_probe": {
            "last": _last_probe if _last_probe.get("at") else None,
        },
        "convert_job": acct_convert.job_status(),
        "cliproxyapi": cpa_sync.job_status(),
    }


def _run_logs_block() -> dict:
    try:
        from grok_register.run_log import list_run_logs, recent_fail_summary, tail_text, register_dashboard_log_path

        files = list_run_logs()
        fails = recent_fail_summary(limit=6)
        tail = tail_text(register_dashboard_log_path(), max_bytes=3500, max_lines=25)
        return {
            "files": files,
            "recent_fails": fails,
            "log_tail": tail,
            "downloads": {
                "register_log": "/api/download?format=register_log",
                "register_fail": "/api/download?format=register_fail",
                "register_logs_zip": "/api/download?format=register_logs_zip",
            },
        }
    except Exception as exc:
        return {"files": [], "recent_fails": [], "log_tail": "", "error": str(exc)[:200]}


def _accounts_overview_block() -> dict:
    try:
        summary = inv.inventory_summary()
        return {
            "count": summary.get("total", 0),
            "by_status": summary.get("by_status") or {},
            "by_format": summary.get("by_format") or {},
            "artifacts": summary.get("artifacts") or {},
            "export_dir": summary.get("export_dir") or "",
        }
    except Exception as exc:
        return {"count": _accounts_count(), "error": str(exc)}


def _tail_log(path: Path, *, max_bytes: int = 2500) -> str:
    try:
        from grok_register.run_log import tail_text

        return tail_text(path, max_bytes=max_bytes, max_lines=20)
    except Exception:
        pass
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-12:])


def _spawn_register(args: list[str] | None = None, *, engine: str | None = None) -> dict:
    if process_alive():
        return {"ok": False, "message": "register already running"}
    cmd = [sys.executable, "-m", "grok_register.register"]
    if args:
        cmd.extend(args)
    try:
        from grok_register.run_log import register_dashboard_log_path, append_fail, append_dashboard_note

        log_path = register_dashboard_log_path()
    except Exception:
        log_path = PROJECT_ROOT / "logs" / "register-dashboard.log"
        append_fail = None  # type: ignore
        append_dashboard_note = None  # type: ignore
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    eng = (engine or env.get("REGISTER_ENGINE") or "protocol").strip().lower()
    if eng in {"protocol", "http", "python", "go"}:
        env["REGISTER_ENGINE"] = "protocol" if eng == "http" else eng
    # detach — browser only displays progress via runtime-status + register-job.json
    spawn_hdr = (
        f"\n===== spawn {time.strftime('%Y-%m-%dT%H:%M:%S')} engine={env.get('REGISTER_ENGINE')} "
        f"cmd={' '.join(cmd)} =====\n"
    )
    if append_dashboard_note:
        try:
            append_dashboard_note(spawn_hdr)
        except Exception:
            pass
    with open(log_path, "ab", buffering=0) as logf:
        if not append_dashboard_note:
            logf.write(spawn_hdr.encode())
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    if append_fail:
        try:
            append_fail(
                "spawn",
                f"dashboard spawned pid={proc.pid} engine={env.get('REGISTER_ENGINE')}",
                level="info",
                engine=env.get("REGISTER_ENGINE") or "",
                extra={"cmd": cmd},
            )
        except Exception:
            pass
    # proot (tmoe) may leave children in tracing-stop; poke CONT
    try:
        time.sleep(0.15)
        os.kill(proc.pid, signal.SIGCONT)
    except Exception:
        pass
    # Detect instant crash (missing deps / config) so panel does not show "running" then offline
    time.sleep(0.85)
    early_code = proc.poll()
    if early_code is not None:
        tail = _tail_log(log_path)
        msg = (
            f"注册进程秒退 exit={early_code} engine={env.get('REGISTER_ENGINE')}。"
            f" 下载：↓ 运行日志 / ↓ 失败事件"
        )
        if append_fail:
            try:
                append_fail(
                    "early_exit",
                    msg,
                    engine=env.get("REGISTER_ENGINE") or "",
                    exit_code=early_code,
                    extra={"log_tail": tail[-1500:]},
                )
            except Exception:
                pass
        job_store.write_register_job(
            running=False,
            engine=env.get("REGISTER_ENGINE") or "protocol",
            pid=proc.pid,
            started_at=time.time(),
            finished_at=time.time(),
            message=msg,
            error=tail[:500],
            log=str(log_path),
        )
        return {
            "ok": False,
            "message": msg + (f"\n--- log ---\n{tail}" if tail else ""),
            "pid": proc.pid,
            "exit_code": early_code,
            "log": str(log_path),
            "log_tail": tail,
            "log_download": "/api/download?format=register_log",
            "fail_download": "/api/download?format=register_fail",
            "logs_zip": "/api/download?format=register_logs_zip",
            "engine": env.get("REGISTER_ENGINE") or "protocol",
        }
    try:
        from grok_register.runtime_status import write_pid

        write_pid(proc.pid)
    except Exception:
        pass
    target = 0
    if args:
        for i, a in enumerate(args):
            if a == "--target" and i + 1 < len(args):
                try:
                    target = int(args[i + 1])
                except ValueError:
                    pass
    job_store.write_register_job(
        running=True,
        engine=env.get("REGISTER_ENGINE") or "protocol",
        pid=proc.pid,
        started_at=time.time(),
        finished_at=0,
        message=f"注册已启动 pid={proc.pid} engine={env.get('REGISTER_ENGINE')}（浏览器只读进度）",
        error="",
        target=target,
        total=target,
        success=0,
        ok=0,
        fail=0,
        log=str(log_path),
    )
    return {
        "ok": True,
        "message": (
            f"注册已启动 pid={proc.pid} engine={env.get('REGISTER_ENGINE')} "
            f"· 进度 logs/runtime-status.json / logs/register-dashboard.log"
        ),
        "pid": proc.pid,
        "log": str(log_path),
        "engine": env.get("REGISTER_ENGINE") or "protocol",
    }


def _stop_register() -> dict:
    pid = read_pid()
    # also try register-job.json pid
    job = job_store.read_register_job()
    job_pid = job.get("pid")
    killed = []
    for p in {pid, job_pid}:
        if not p:
            continue
        try:
            if job_store.pid_alive(p):
                os.kill(int(p), signal.SIGTERM)
                killed.append(int(p))
        except OSError:
            pass
    job_store.write_register_job(
        running=False,
        finished_at=time.time(),
        message="已发送停止信号" + (f" → {killed}" if killed else "（未找到运行中进程）"),
    )
    if not killed:
        return {"ok": False, "message": "register not running"}
    return {"ok": True, "message": f"sent SIGTERM to {killed}"}


def _run_scrape(data: dict | None = None) -> dict:
    """
    Start public proxy scrape in background.
    By default scrape_to_files auto-starts Go batch x.ai test after writing candidates.
    """
    log_path = PROJECT_ROOT / "logs" / "scrape-dashboard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "grok_register.proxy_scraper", "scrape"]
    data = data or {}
    if data.get("github") or data.get("use_github"):
        cmd.append("--github")
    # allow panel to force/disable auto-test
    if data.get("no_auto_test") or data.get("auto_test") is False:
        cmd.append("--no-auto-test")
    elif data.get("auto_test") is True:
        cmd.append("--auto-test")
    env = os.environ.copy()
    # sensible defaults for auto-test after scrape
    env.setdefault("PROXY_SCRAPER_AUTO_TEST", "1")
    env.setdefault("PROXY_SCRAPER_AUTO_TEST_MAX", "2000")
    env.setdefault("PROXY_SCRAPER_AUTO_TEST_WORKERS", "128")
    env.setdefault("PROXY_SCRAPER_AUTO_TEST_TIMEOUT", "5")
    with open(log_path, "ab", buffering=0) as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    auto = env.get("PROXY_SCRAPER_AUTO_TEST", "1") != "0" and "--no-auto-test" not in cmd
    job_store.write_scrape_job(
        running=True,
        engine="python",
        pid=proc.pid,
        started_at=time.time(),
        finished_at=0,
        message=f"爬取运行中 pid={proc.pid}" + (" · 完成后自动测活" if auto else ""),
        error="",
        auto_test=auto,
        log=str(log_path),
    )
    return {
        "ok": True,
        "message": (
            f"爬取已启动 pid={proc.pid} · 完成后将自动测活 x.ai "
            f"(可用 PROXY_SCRAPER_AUTO_TEST=0 关闭)"
        ),
        "pid": proc.pid,
        "log": str(log_path),
        "auto_test": auto,
    }


_XAI_PROBE_URLS = (
    "https://accounts.x.ai/sign-up?redirect=grok-com",
    "https://x.ai/",
)


def _probe_proxy_candidates(limit: int = 3) -> list[str]:
    """Pick a few proxies from active pool / 代理.txt for probe-via-proxy."""
    paths = [
        PROJECT_ROOT / "logs" / "proxy-auto-active.txt",
        PROJECT_ROOT / "代理.txt",
        PROJECT_ROOT / "proxy.txt",
    ]
    raw_pool = (os.environ.get("PROXY_POOL_FILE") or "").strip()
    if raw_pool:
        p = Path(raw_pool).expanduser()
        paths.insert(0, p if p.is_absolute() else PROJECT_ROOT / p)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line in seen:
                    continue
                seen.add(line)
                out.append(line)
                if len(out) >= limit:
                    return out
        except OSError:
            continue
    return out


def _http_probe(url: str, *, timeout: float, proxies: dict | None = None) -> dict:
    import requests

    started = time.monotonic()
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            proxies=proxies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
            allow_redirects=True,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        code = int(resp.status_code)
        ok = 200 <= code < 500  # reachable even if 403/429
        return {
            "url": url,
            "ok": ok,
            "reachable": True,
            "status_code": code,
            "latency_ms": latency_ms,
            "final_url": str(resp.url)[:200],
            "error": "" if ok else f"status {code}",
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "url": url,
            "ok": False,
            "reachable": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "final_url": "",
            "error": str(exc)[:240],
        }


def probe_xai_access(
    *,
    timeout: float | None = None,
    via_proxy: bool = True,
    proxy_limit: int = 3,
) -> dict:
    """
    Test whether this host (and optional proxies) can reach x.ai / accounts.x.ai.
    Always allowed from control plane (read-only network check).
    """
    timeout = float(timeout if timeout is not None else (os.environ.get("XAI_PROBE_TIMEOUT") or "12"))
    timeout = max(3.0, min(timeout, 60.0))
    urls = list(_XAI_PROBE_URLS)
    extra = (os.environ.get("PROXY_AUTO_TEST_URLS") or "").strip()
    if extra:
        for u in extra.split(","):
            u = u.strip()
            if u and u not in urls:
                urls.append(u)

    results: list[dict] = []
    # 1) direct
    for url in urls:
        r = _http_probe(url, timeout=timeout, proxies=None)
        r["via"] = "direct"
        r["proxy"] = ""
        results.append(r)

    # 2) via sample proxies
    proxy_rows = []
    if via_proxy:
        for cand in _probe_proxy_candidates(proxy_limit):
            # requests wants scheme:// for both http and https keys
            proxies = {"http": cand, "https": cand}
            # only first URL via each proxy to keep probe fast
            r = _http_probe(urls[0], timeout=timeout, proxies=proxies)
            r["via"] = "proxy"
            r["proxy"] = cand[:120]
            results.append(r)
            proxy_rows.append(r)

    direct_ok = any(r.get("ok") for r in results if r.get("via") == "direct")
    proxy_ok = any(r.get("ok") for r in proxy_rows) if proxy_rows else None
    any_ok = any(r.get("ok") for r in results)

    if any_ok and direct_ok:
        message = "可访问 x.ai（直连成功）"
        if proxy_ok:
            message += " · 部分代理也可用"
        elif proxy_rows:
            message += " · 采样代理未通过"
    elif any_ok and proxy_ok:
        message = "直连失败，但采样代理可访问 x.ai（注册请开代理池）"
    else:
        message = "无法访问 x.ai / accounts.x.ai，请检查网络或代理"

    out = {
        "ok": any_ok,
        "direct_ok": direct_ok,
        "proxy_ok": proxy_ok,
        "message": message,
        "timeout_sec": timeout,
        "results": results,
        "at": time.time(),
        "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _last_probe.clear()
    _last_probe.update(out)
    # persist without huge result bodies if needed — keep last 20
    disk = dict(out)
    if isinstance(disk.get("results"), list) and len(disk["results"]) > 20:
        disk["results"] = disk["results"][:20]
    _save_json_file(_LAST_PROBE_PATH, disk)
    return out


def _spawn_go_register(data: dict) -> dict:
    """Start Go register-worker if binary exists."""
    if process_alive():
        return {"ok": False, "message": "register already running"}
    candidates = [PROJECT_ROOT / "native" / "register-worker" / "register-worker"]
    raw = (os.environ.get("REGISTER_WORKER_BIN") or "").strip()
    if raw:
        candidates.insert(0, Path(raw).expanduser())
    binary = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if binary is None:
        return {
            "ok": False,
            "message": "register-worker not found; run bash scripts/build-native.sh",
        }
    workers = str(data.get("workers") or os.environ.get("GO_REGISTER_WORKERS") or "4")
    target = str(data.get("target") or os.environ.get("TARGET") or "0")
    cmd = [str(binary), "run", "--workers", workers]
    if target and target != "0":
        cmd.extend(["--target", target])
    cfg = data.get("config")
    cfg_path = None
    if isinstance(cfg, dict) and cfg:
        import tempfile

        fd, cfg_path = tempfile.mkstemp(
            prefix="go-register-", suffix=".json", dir=str(PROJECT_ROOT / "logs")
        )
        os.close(fd)
        Path(cfg_path).write_text(json.dumps(cfg), encoding="utf-8")
        cmd.extend(["--config", cfg_path])
    log_path = PROJECT_ROOT / "logs" / "register-go-dashboard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    try:
        from grok_register.runtime_status import write_pid

        write_pid(proc.pid)
    except Exception:
        pass
    return {
        "ok": True,
        "message": f"go register-worker started pid={proc.pid}",
        "pid": proc.pid,
        "log": str(log_path),
        "config": cfg_path,
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>grok-free-register</title>
<style>
  :root {
    --bg:#0b1220; --panel:#121a2b; --panel2:#182238; --text:#e8eefc; --muted:#93a4c7;
    --ok:#3ddc97; --bad:#ff6b6b; --warn:#ffd166; --accent:#6ea8fe; --border:#243352;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:ui-sans-serif,system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:radial-gradient(1200px 600px at 10% -10%,#1a2744 0%,var(--bg) 55%);color:var(--text);min-height:100vh}
  header{display:flex;justify-content:space-between;align-items:center;padding:16px 22px;border-bottom:1px solid var(--border);position:sticky;top:0;background:rgba(11,18,32,.9);backdrop-filter:blur(8px);z-index:10;gap:12px;flex-wrap:wrap}
  h1{margin:0;font-size:17px}
  .meta{color:var(--muted);font-size:12px}
  .header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .lang-switch{display:inline-flex;border:1px solid var(--border);border-radius:999px;overflow:hidden;background:#152038}
  .lang-switch button{border:0;background:transparent;color:var(--muted);padding:6px 12px;cursor:pointer;font-weight:700;font-size:12px}
  .lang-switch button.active{background:#27407a;color:var(--text)}
  nav{display:flex;gap:8px;margin:14px 22px 0;flex-wrap:wrap}
  nav button{border:1px solid var(--border);background:#152038;color:var(--text);border-radius:999px;padding:7px 14px;cursor:pointer;font-weight:600}
  nav button.active{background:#27407a;border-color:#3d63b8}
  main{padding:16px 22px 40px;max-width:1280px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  @media(max-width:960px){.grid{grid-template-columns:repeat(2,1fr)}}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--border);border-radius:14px;padding:14px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
  .card h3{margin:0 0 8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
  .value{font-size:26px;font-weight:700}
  .sub{margin-top:5px;color:var(--muted);font-size:12px}
  .badge{display:inline-flex;gap:6px;align-items:center;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid var(--border)}
  .badge.ok{color:var(--ok);border-color:rgba(61,220,151,.3)}
  .badge.bad{color:var(--bad);border-color:rgba(255,107,107,.3)}
  .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  .row{display:grid;grid-template-columns:1.1fr .9fr;gap:12px;margin-top:12px}
  @media(max-width:900px){.row{grid-template-columns:1fr}}
  button.act{border:1px solid var(--border);background:#1b2740;color:var(--text);border-radius:10px;padding:8px 12px;font-weight:600;cursor:pointer}
  button.act.primary{background:#27407a;border-color:#3d63b8}
  button.act.danger{background:#4a1f2a;border-color:#8a3a4d}
  button.act:disabled{opacity:.45;cursor:not-allowed}
  .actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
  .note{color:var(--muted);font-size:12px;margin-top:8px}
  pre{margin:0;background:#0a101c;border:1px solid var(--border);border-radius:12px;padding:12px;overflow:auto;max-height:380px;font-size:12px;color:#cfe0ff}
  table.cfg{width:100%;border-collapse:collapse;font-size:13px}
  table.cfg th,table.cfg td{padding:8px 6px;border-bottom:1px solid rgba(36,51,82,.55);vertical-align:top}
  table.cfg th{color:var(--muted);font-weight:600;text-align:left;font-size:11px;text-transform:uppercase}
  table.cfg input,table.cfg select{width:100%;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px}
  .group-title{margin:18px 0 8px;font-size:14px;font-weight:700}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#1a2740;color:var(--muted);font-size:11px;margin-left:6px}
  .hidden{display:none}
  .tag{display:inline-block;padding:2px 7px;border-radius:6px;font-size:11px;font-weight:600;margin-right:4px;background:#1a2740;border:1px solid var(--border)}
  .tag.ok{color:var(--ok);border-color:rgba(61,220,151,.35)}
  .tag.warn{color:var(--warn);border-color:rgba(255,209,102,.35)}
  .tag.muted{color:var(--muted)}
  table.acc{width:100%;border-collapse:collapse;font-size:12px}
  table.acc th,table.acc td{padding:8px 6px;border-bottom:1px solid rgba(36,51,82,.55);text-align:left;vertical-align:top}
  table.acc th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}
  .dl-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
  a.dl{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border-radius:10px;border:1px solid var(--border);background:#1b2740;color:var(--text);text-decoration:none;font-weight:600;font-size:13px}
  a.dl:hover{border-color:#3d63b8;background:#27407a}
  .filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}
  .filters select,.filters input{background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px}
  .cfg-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0 8px}
  .cfg-toolbar input[type=search]{flex:1;min-width:180px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px}
  .cfg-mode{display:inline-flex;border:1px solid var(--border);border-radius:999px;overflow:hidden}
  .cfg-mode button{border:0;background:#152038;color:var(--muted);padding:7px 14px;cursor:pointer;font-weight:600}
  .cfg-mode button.active{background:#27407a;color:var(--text)}
  .cfg-presets{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin:8px 0 14px}
  .cfg-preset{border:1px solid var(--border);border-radius:12px;padding:12px;background:rgba(10,16,28,.45)}
  .cfg-label{font-weight:600;font-size:13px}
  .cfg-key{font-size:11px;color:var(--muted);margin-top:2px}
  .cfg-card{border:1px solid rgba(36,51,82,.55);border-radius:12px;padding:12px;margin:8px 0;background:rgba(10,16,28,.35)}
  .cfg-card .row-fields{display:grid;grid-template-columns:1.2fr 1fr;gap:10px;align-items:start}
  @media(max-width:800px){.cfg-card .row-fields{grid-template-columns:1fr}}
  .cfg-card select,.cfg-card input{width:100%;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px}
  .cfg-help{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.45}
  .cfg-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .cfg-empty{color:var(--muted);padding:20px;text-align:center}
  #login-overlay{position:fixed;inset:0;z-index:1000;background:rgba(6,10,18,.88);display:none;align-items:center;justify-content:center;padding:20px}
  #login-overlay.show{display:flex}
  #login-box{width:min(400px,100%);background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px 20px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
  #login-box h2{margin:0 0 6px;font-size:18px}
  #login-box .note{margin-bottom:14px}
  #login-box label{display:block;margin:10px 0 4px;font-size:12px;color:var(--muted)}
  #login-box input{width:100%;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px;box-sizing:border-box}
  #login-err{color:var(--bad);min-height:1.2em;margin-top:8px;font-size:13px}
  #btn-logout{display:none}
</style>
</head>
<body>
<div id="login-overlay" aria-hidden="true">
  <div id="login-box">
    <h2 data-i18n="login_title">面板登录</h2>
    <div class="note" data-i18n="login_hint">Space 已启用 DASHBOARD_PASSWORD。页面本身可打开；API 需登录后使用。</div>
    <label data-i18n="login_user">用户名</label>
    <input id="login-user" type="text" autocomplete="username" value="admin" />
    <label data-i18n="login_pass">密码</label>
    <input id="login-pass" type="password" autocomplete="current-password" />
    <div class="actions" style="margin-top:14px">
      <button class="act primary" id="btn-login" data-i18n="login_btn">登录</button>
    </div>
    <div id="login-err"></div>
  </div>
</div>
<header>
  <div>
    <h1 data-i18n="title">Grok 免费注册 · 控制面板</h1>
    <div class="meta" data-i18n="subtitle">运行时 · 账号 · CPA/sub2api · 配置</div>
  </div>
  <div class="header-right">
    <div class="lang-switch" role="group" aria-label="Language">
      <button type="button" id="lang-zh" class="active" data-lang="zh">中文</button>
      <button type="button" id="lang-en" data-lang="en">EN</button>
    </div>
    <button type="button" class="act" id="btn-logout" data-i18n="logout_btn">退出</button>
    <div id="run-badge" class="badge bad"><span class="dot"></span><span data-i18n="offline">离线</span></div>
  </div>
</header>
<nav>
  <button class="active" data-tab="overview" data-i18n="tab_overview">总览</button>
  <button data-tab="accounts" data-i18n="tab_accounts">账号</button>
  <button data-tab="config" data-i18n="tab_config">全部配置</button>
  <button data-tab="raw" data-i18n="tab_raw">原始 JSON</button>
</nav>
<main>
  <section id="tab-overview">
    <div class="grid" id="kpis"></div>
    <div class="row">
      <div class="card">
        <h3 data-i18n="card_register">注册控制</h3>
        <div id="register-body"></div>
        <div class="filters" style="margin-top:10px">
          <label><span data-i18n="reg_target">成功目标</span>
            <input type="number" id="reg-target" min="0" step="1" placeholder="0=不限" style="width:100px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px"/>
          </label>
          <label><span data-i18n="reg_engine_pick">引擎</span>
            <select id="reg-engine" style="background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px">
              <option value="protocol" selected>协议 HTTP（推荐）</option>
              <option value="python">浏览器 Python</option>
              <option value="go">Go worker</option>
            </select>
          </label>
        </div>
        <div class="actions" id="actions"></div>
        <div class="note" id="action-note"></div>
      </div>
      <div class="card">
        <h3 data-i18n="card_xai_probe">x.ai 连通性 / 代理测活</h3>
        <div id="xai-probe-body" class="note" data-i18n="xai_probe_hint">测试本机与代理池能否访问 accounts.x.ai / x.ai（支持高并发与自定义参数）</div>
        <div class="filters" style="margin-top:8px;gap:10px">
          <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="chk-use-public" />
            <span data-i18n="chk_use_public">使用公共节点</span>
          </label>
          <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="chk-use-manual" checked />
            <span data-i18n="chk_use_manual">手动池</span>
          </label>
          <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="chk-use-active" checked />
            <span data-i18n="chk_use_active">已测活池</span>
          </label>
        </div>
        <div class="filters" style="margin-top:8px">
          <label><span data-i18n="batch_workers">并发</span>
            <input type="number" id="batch-workers" min="1" max="2048" value="128" style="width:90px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px"/>
          </label>
          <label><span data-i18n="batch_timeout">超时秒</span>
            <input type="number" id="batch-timeout" min="2" max="120" value="5" style="width:80px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px"/>
          </label>
          <label><span data-i18n="batch_max">最多测</span>
            <input type="number" id="batch-max" min="1" max="40000" value="200" style="width:100px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px"/>
          </label>
          <label><span data-i18n="batch_max_active">保留可用</span>
            <input type="number" id="batch-max-active" min="0" max="40000" value="0" title="0=不限制" style="width:90px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 8px"/>
          </label>
        </div>
        <div style="margin-top:8px">
          <label class="note" data-i18n="batch_urls_label">测试 URL（逗号或换行，默认 x.ai）</label>
          <textarea id="batch-urls" rows="2" style="width:100%;margin-top:4px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px;font-size:12px;resize:vertical" placeholder="https://accounts.x.ai/sign-up?redirect=grok-com&#10;https://x.ai/"></textarea>
        </div>
        <div style="margin-top:8px">
          <label class="note" data-i18n="batch_custom_label">自定义代理列表（可选，一行一个）</label>
          <textarea id="batch-custom" rows="3" style="width:100%;margin-top:4px;background:#0d1524;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px;font-size:12px;resize:vertical" placeholder="http://user:pass@host:port&#10;socks5://host:1080"></textarea>
        </div>
        <div class="note" data-i18n="public_hint">公共节点来自 logs/proxy-scraper-candidates.txt；自定义列表优先。并发走 Go proxy-worker 协程池（不可用时 Python 线程池）。</div>
        <div class="actions" style="margin-top:10px">
          <button class="act primary" id="btn-batch-proxies" data-i18n="btn_batch_proxies">批量测代理→x.ai</button>
          <button class="act" id="btn-probe-xai" data-i18n="btn_probe_xai">快速探测</button>
          <button class="act" id="btn-probe-xai-direct" data-i18n="btn_probe_xai_direct">仅直连</button>
          <button class="act" id="btn-scrape-public" data-i18n="btn_scrape_public">爬取公共节点</button>
        </div>
        <div class="note" id="xai-probe-note"></div>
        <div class="note" id="batch-proxy-note"></div>
        <pre id="xai-probe-detail" style="margin-top:10px;max-height:200px;display:none"></pre>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="card" style="grid-column:1/-1">
        <h3 data-i18n="card_run_logs">运行 / 失败日志</h3>
        <div class="note" data-i18n="run_logs_hint">注册秒退、Turnstile 超时、worker fail 会写入这里。可下载完整日志到本地排查（HF 无 SSH 时用）。</div>
        <div class="dl-row" id="run-log-downloads" style="margin-top:8px"></div>
        <div class="note" id="run-log-meta" style="margin-top:6px"></div>
        <pre id="run-log-tail" class="note" style="margin-top:8px;max-height:220px;overflow:auto;white-space:pre-wrap;background:#0d1524;border:1px solid var(--border);border-radius:8px;padding:10px;font-size:12px"></pre>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="card">
        <h3 data-i18n="card_products">成品下载</h3>
        <div id="products-body" class="note" data-i18n="products_hint">扫描 keys/ 中的 legacy / sub2api / cpa 成品</div>
        <div class="dl-row" id="product-downloads"></div>
        <div style="margin-top:14px">
          <div class="group-title" data-i18n="card_recovery">账号恢复包</div>
          <div class="note" data-i18n="recovery_hint">导出 accounts.txt / auth-sessions / 指纹 / grok.txt / protocol.log，换机或重建 Space 后解压到 keys/ 即可续跑。</div>
          <div class="dl-row" id="recovery-downloads" style="margin-top:8px"></div>
          <div class="note" id="recovery-meta" style="margin-top:6px"></div>
        </div>
        <div class="actions" style="margin-top:12px">
          <button class="act primary" id="btn-rebuild-bundles" data-i18n="btn_rebuild">重建合并包</button>
        </div>
        <div class="note" id="product-note"></div>
      </div>
      <div class="card">
        <h3 data-i18n="card_engines">引擎</h3>
        <div id="engines"></div>
        <div style="margin-top:14px">
          <h3 data-i18n="card_account_status">账号状态</h3>
          <div id="account-status-body"></div>
        </div>
      </div>
    </div>
  </section>
  <section id="tab-accounts" class="hidden">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <h3 style="margin:0" data-i18n="card_inventory">账号库存</h3>
        <div class="actions" style="margin:0">
          <button class="act" id="btn-refresh-accounts" data-i18n="btn_refresh">刷新</button>
          <a class="dl" href="/api/download?format=sub2api">↓ sub2api</a>
          <a class="dl" href="/api/download?format=cpa_zip">↓ xai-singles.zip</a>
          <a class="dl" href="/api/download?format=legacy">↓ legacy</a>
          <a class="dl" href="/api/download?format=recovery" data-i18n="dl_recovery_zip">↓ 恢复包.zip</a>
        </div>
      </div>
      <div class="actions" style="margin-top:10px">
        <button class="act primary" id="btn-convert-sub2api" data-i18n="btn_convert_sub2api">一键转 sub2api</button>
        <button class="act primary" id="btn-convert-cpa" data-i18n="btn_convert_cpa">一键转 CPA</button>
        <button class="act" id="btn-convert-both" data-i18n="btn_convert_both">转 sub2api + CPA</button>
        <button class="act" id="btn-convert-pending" data-i18n="btn_convert_pending">仅转换待 OAuth</button>
      </div>
      <div class="note" id="convert-note" data-i18n="convert_hint">一键转 CPA/sub2api = 已有 OAuth 文件互转（秒级，默认不走浏览器）。「仅转换待 OAuth」才用 SSO 浏览器（慢，建议少量）。</div>
      <div class="actions" style="margin-top:12px">
        <button class="act primary" id="btn-cpa-sync" data-i18n="btn_cpa_sync">同步 CLIProxyAPI</button>
        <button class="act" id="btn-cpa-refresh" data-i18n="btn_cpa_refresh">刷新过期 Token</button>
        <button class="act" id="btn-cpa-worker-start" data-i18n="btn_cpa_worker_start">启动自动同步</button>
        <button class="act danger" id="btn-cpa-worker-stop" data-i18n="btn_cpa_worker_stop">停止自动同步</button>
      </div>
      <div class="note" id="cpa-sync-note" data-i18n="cpa_sync_hint">仅导入单账号 xai-*.json（type=xai）。不使用任何合并包。</div>
      <div class="filters">
        <label><span data-i18n="filter_status">状态</span>
          <select id="flt-status">
            <option value="" data-i18n="filter_all">全部</option>
            <option value="oauth_ready">oauth_ready</option>
            <option value="oauth_pending">oauth_pending</option>
            <option value="legacy_sso">legacy_sso</option>
            <option value="unknown">unknown</option>
          </select>
        </label>
        <label><span data-i18n="filter_format">格式</span>
          <select id="flt-format">
            <option value="" data-i18n="filter_all">全部</option>
            <option value="sub2api">sub2api</option>
            <option value="cpa">cpa</option>
            <option value="legacy">legacy</option>
          </select>
        </label>
        <span class="note" id="accounts-meta"></span>
      </div>
      <div style="overflow:auto;max-height:520px">
        <table class="acc" id="accounts-table">
          <thead><tr>
            <th data-i18n="th_email">邮箱</th>
            <th data-i18n="th_status">状态</th>
            <th data-i18n="th_formats">格式</th>
            <th data-i18n="th_tokens">令牌</th>
            <th data-i18n="th_ledger">台账</th>
            <th data-i18n="th_fingerprint">指纹</th>
            <th data-i18n="th_updated">更新时间</th>
          </tr></thead>
          <tbody id="accounts-tbody"><tr><td colspan="7" data-i18n="loading">加载中…</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>
  <section id="tab-config" class="hidden">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <h3 style="margin:0" data-i18n="card_config">配置</h3>
        <div class="actions" style="margin:0;flex-wrap:wrap">
          <button class="act primary" id="btn-save-config" data-i18n="btn_save">保存修改</button>
          <button class="act primary" id="btn-save-restart" data-i18n="btn_save_restart">保存并重启注册</button>
          <button class="act" id="btn-reload-config" data-i18n="btn_reload">重新加载</button>
          <button class="act" id="btn-export-env" data-i18n="btn_export_env">导出 .env</button>
          <button class="act" id="btn-import-env" data-i18n="btn_import_env">导入 .env</button>
        </div>
      </div>
      <div class="note" id="config-note" data-i18n="config_hint">无法 SSH 时在此改全部后台项。保存写 .env；「保存并重启」会停旧进程再拉起。密钥掩码，留空不改。</div>
      <div class="cfg-toolbar">
        <div class="cfg-mode">
          <button type="button" id="cfg-mode-simple" class="active" data-mode="simple" data-i18n="cfg_mode_simple">常用配置</button>
          <button type="button" id="cfg-mode-all" data-mode="all" data-i18n="cfg_mode_all">全部配置</button>
        </div>
        <input type="search" id="cfg-search" data-i18n-placeholder="cfg_search_ph" placeholder="搜索：邮箱、代理、导出…" />
      </div>
      <div class="cfg-presets-wrap">
        <div class="group-title" data-i18n="cfg_presets">一键预设</div>
        <div class="note" data-i18n="cfg_presets_hint">按场景批量写入常用键，不会清空其它配置。可「应用」或「应用并重启注册」。</div>
        <div id="cfg-presets" class="cfg-presets"></div>
      </div>
      <div class="cfg-custom card" style="margin:12px 0;padding:12px">
        <div class="group-title" data-i18n="cfg_custom">自定义环境变量</div>
        <div class="row-fields" style="grid-template-columns:1fr 1.4fr auto auto;gap:8px">
          <input type="text" id="cfg-custom-key" placeholder="KEY_NAME" spellcheck="false" />
          <input type="text" id="cfg-custom-val" placeholder="value" spellcheck="false" />
          <button class="act primary" id="btn-cfg-custom-add" data-i18n="btn_cfg_add">添加/更新</button>
          <button class="act danger" id="btn-cfg-custom-del" data-i18n="btn_cfg_del">删除键</button>
        </div>
        <div class="note" data-i18n="cfg_custom_hint">目录外的键也可写。删除会从 .env 移除该键（进程环境同步 pop）。</div>
      </div>
      <div class="cfg-test card" style="margin:12px 0;padding:12px">
        <div class="group-title" data-i18n="cfg_test">连通性快速测试</div>
        <div class="actions" style="margin:0">
          <button class="act" id="btn-test-moemail" data-i18n="btn_test_moemail">测 MoeMail</button>
          <button class="act" id="btn-test-xai" data-i18n="btn_test_xai_cfg">测 x.ai</button>
          <button class="act" id="btn-test-turnstile" data-i18n="btn_test_turnstile">测 Turnstile API</button>
        </div>
        <pre id="cfg-test-out" class="note" style="margin-top:8px;white-space:pre-wrap;max-height:160px;overflow:auto"></pre>
      </div>
      <div id="config-editor"></div>
      <textarea id="cfg-import-box" class="hidden" rows="12" style="width:100%;margin-top:10px;background:#0d1524;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px;font-family:ui-monospace,monospace" placeholder="# paste .env here"></textarea>
      <div class="actions hidden" id="cfg-import-actions" style="margin-top:8px">
        <button class="act primary" id="btn-import-merge" data-i18n="btn_import_merge">合并导入</button>
        <button class="act danger" id="btn-import-replace" data-i18n="btn_import_replace">整文件替换</button>
        <button class="act" id="btn-import-cancel" data-i18n="btn_import_cancel">取消</button>
      </div>
    </div>
  </section>
  <section id="tab-raw" class="hidden">
    <div class="card"><h3 data-i18n="card_raw">原始状态</h3><pre id="raw" data-i18n="loading">加载中…</pre></div>
  </section>
</main>
<script>
const I18N = {
  zh: {
    title: "Grok 免费注册 · 控制面板",
    subtitle: "运行时 · 账号 · CPA/sub2api · 配置",
    tab_overview: "总览",
    tab_accounts: "账号",
    tab_config: "配置",
    tab_raw: "原始 JSON",
    offline: "离线",
    running: "运行中",
    login_title: "面板登录",
    login_hint: "Space 已启用密码。页面可打开；操作 API 需登录。",
    login_user: "用户名",
    login_pass: "密码",
    login_btn: "登录",
    logout_btn: "退出",
    login_need: "请输入密码",
    login_fail: "登录失败：用户名或密码错误",
    card_register: "注册控制",
    card_products: "成品下载",
    card_engines: "引擎",
    card_account_status: "账号状态",
    card_inventory: "账号库存",
    card_config: "配置",
    card_raw: "原始状态",
    products_hint: "扫描 keys/ 中的 legacy / sub2api / cpa 成品",
    products_dir: "导出目录：{dir} · 可直接导入 sub2api / CPA 面板",
    card_run_logs: "运行 / 失败日志",
    run_logs_hint: "注册秒退、Turnstile 超时、worker fail 会写入这里。可下载完整日志到本地排查（HF 无 SSH 时用）。",
    run_logs_meta: "日志文件 {ok}/{n} · 最近失败 {fails} 条",
    run_logs_empty: "暂无运行日志。启动一次注册后这里会显示尾部输出。",
    dl_register_log: "↓ 完整运行日志",
    dl_register_fail: "↓ 失败事件.jsonl",
    dl_register_logs_zip: "↓ 日志包.zip",
    card_recovery: "账号恢复包",
    recovery_hint: "导出 accounts.txt / auth-sessions / 指纹 / grok.txt / protocol.log，换机或重建 Space 后解压到 keys/ 即可续跑。",
    recovery_meta: "恢复源文件：存在 {ok}/{n} · 合计 {size}",
    dl_recovery_zip: "↓ 恢复包.zip",
    btn_rebuild: "重建合并包",
    btn_refresh: "刷新",
    btn_save: "保存修改",
    btn_reload: "重新加载",
    btn_convert_sub2api: "一键转 sub2api",
    btn_convert_cpa: "一键转 CPA",
    btn_convert_both: "转 sub2api + CPA",
    btn_convert_pending: "仅转换待 OAuth",
    convert_hint: "一键转 CPA/sub2api = 已有 OAuth 文件互转（秒级，默认不走浏览器）。「仅转换待 OAuth」才用 SSO 浏览器（慢，建议少量）。",
    convert_starting: "正在启动转换…",
    convert_running: "转换进行中…",
    convert_done: "转换完成",
    btn_cpa_sync: "同步 CLIProxyAPI",
    btn_cpa_refresh: "刷新过期 Token",
    btn_cpa_worker_start: "启动自动同步",
    btn_cpa_worker_stop: "停止自动同步",
    cpa_sync_hint: "仅导入单账号 xai-*.json（type=xai）。不使用任何合并包。",
    cpa_sync_running: "CLIProxyAPI 同步中…",
    cpa_sync_done: "CLIProxyAPI 同步完成",
    btn_start: "启动协议注册",
    btn_start_py: "启动浏览器注册",
    btn_start_browser: "启动浏览器注册",
    btn_start_go: "启动 Go 注册",
    btn_stop: "停止注册",
    btn_scrape: "爬取代理",
    btn_probe_xai: "快速探测",
    btn_probe_xai_direct: "仅直连",
    btn_batch_proxies: "批量测代理→x.ai",
    btn_scrape_public: "爬取并自动测活",
    chk_use_public: "使用公共节点",
    chk_use_manual: "手动池",
    chk_use_active: "已测活池",
    public_hint: "测活前会把 vless/ss/带认证 SOCKS 经 sing-box 转成本地 HTTP 再交给 Go 测。优先测订阅分享链接，而不是裸 HTTP 公共代理。关页面不影响进度。",
    batch_max: "最多测",
    batch_workers: "并发",
    batch_timeout: "超时秒",
    batch_max_active: "保留可用",
    batch_urls_label: "测试 URL（逗号或换行，默认 x.ai）",
    batch_custom_label: "自定义代理列表（可选，一行一个）",
    batch_starting: "正在启动批量测活…",
    batch_running: "批量测活进行中…",
    batch_done: "批量测活完成",
    card_xai_probe: "x.ai 连通性 / 代理测活",
    xai_probe_hint: "测试本机与代理池能否访问 accounts.x.ai / x.ai（支持高并发与自定义）",
    xai_probe_running: "正在探测…",
    xai_probe_last: "上次探测：",
    reg_target: "成功目标",
    reg_engine_pick: "引擎",
    reg_target_ph: "0=不限",
    filter_status: "状态",
    filter_format: "格式",
    filter_all: "全部",
    th_email: "邮箱",
    th_status: "状态",
    th_formats: "格式",
    th_tokens: "令牌",
    th_ledger: "台账",
    th_fingerprint: "指纹",
    th_updated: "更新时间",
    loading: "加载中…",
    no_accounts: "暂无账号",
    config_hint: "无法 SSH 时在此改全部后台项。保存写 .env；「保存并重启」无改动也会重启注册。密钥掩码，留空不改。",
    btn_save_restart: "保存并重启注册",
    btn_export_env: "导出 .env",
    btn_import_env: "导入 .env",
    btn_import_merge: "合并导入",
    btn_import_replace: "整文件替换",
    btn_import_cancel: "取消",
    btn_cfg_add: "添加/更新",
    btn_cfg_del: "删除键",
    cfg_presets: "一键预设",
    cfg_presets_hint: "按场景批量写入常用键，不会清空其它配置。可「应用」或「应用并重启注册」。",
    cfg_custom: "自定义环境变量",
    cfg_custom_hint: "目录外的键也可写。删除会从 .env 移除该键。",
    cfg_test: "连通性快速测试",
    btn_test_moemail: "测 MoeMail",
    btn_test_xai_cfg: "测 x.ai",
    btn_test_turnstile: "测 Turnstile API",
    cfg_apply_preset: "应用",
    cfg_apply_preset_restart: "应用并重启",
    cfg_export_ok: "已下载 .env 备份",
    cfg_import_need: "请先粘贴 .env 内容",
    cfg_custom_need: "请填写合法 KEY（字母/数字/下划线）",
    cfg_delete_confirm: "确定从 .env 删除键 {key}？",
    actions_disabled: "操作已禁用。请在配置里打开「允许面板操作」，或设置 CONTROL_PLANE_ALLOW_ACTIONS=1",
    last_action: "上次：{action} → {message}",
    kpi_success: "成功数",
    kpi_success_sub: "累计注册成功",
    kpi_starts: "启动次数",
    kpi_starts_sub: "注册流程启动",
    kpi_token_t: "Token 队列 T",
    kpi_token_t_sub: "已产出 {n}",
    kpi_code_q: "验证码队列 Q",
    kpi_code_q_sub: "成功/失败 {ok}/{fail}",
    kpi_rate: "速率/分",
    kpi_rate_sub: "注册速度",
    kpi_accounts: "账号数",
    kpi_accounts_sub: "就绪 {ready} · 待转换 {pending}",
    kpi_proxies: "可用代理",
    kpi_proxies_sub: "测活通过",
    kpi_scraper: "爬取候选",
    kpi_scraper_sub: "代理候选池",
    reg_status_age: "状态年龄：",
    reg_email: "邮箱",
    reg_turnstile: "Turnstile",
    reg_engine: "引擎",
    reg_rate_limit: "限流：",
    reg_rate_open: "开启",
    reg_rate_closed: "关闭",
    eng_register: "注册引擎：",
    eng_proxy: "代理测活：",
    eng_inventory: "库存引擎：",
    eng_turnstile: "Turnstile：",
    eng_polyglot: "多语言栈：",
    eng_note: "硬性要求 Python+Go+Rust。Go 注册设 REGISTER_ENGINE=go；库存默认 Rust；面板为 Python。",
    acc_total: "总计",
    acc_formats: "格式：",
    acc_bundle: "合并包：",
    meta_show: "显示 {shown}/{total} · 库存 {inv}",
    cfg_key: "配置项",
    cfg_value: "值",
    cfg_desc: "说明",
    cfg_restart: "需重启",
    cfg_yes: "是",
    cfg_no: "否",
    cfg_on: "开启",
    cfg_off: "关闭",
    cfg_default: "（默认）",
    cfg_masked: "（已掩码 — 输入新值以替换）",
    cfg_file: "文件：{path} · 显示 {shown} 项 / 共 {n} 项 · 额外 {e}",
    cfg_no_changes: "没有修改",
    cfg_mode_simple: "常用配置",
    cfg_mode_all: "全部配置",
    cfg_search_ph: "搜索：邮箱、代理、导出…",
    cfg_empty: "没有匹配的配置项",
    cfg_source_file: "已写入 .env",
    cfg_source_process: "进程环境",
    cfg_source_default: "使用默认值",
    cfg_need_restart: "改后需重启注册",
    status_oauth_ready: "OAuth 就绪",
    status_oauth_pending: "待转 OAuth",
    status_legacy_sso: "仅 SSO",
    status_unknown: "未知",
  },
  en: {
    title: "grok-free-register · Control",
    subtitle: "runtime · accounts · CPA/sub2api · config",
    tab_overview: "Overview",
    tab_accounts: "Accounts",
    tab_config: "Config",
    tab_raw: "Raw JSON",
    offline: "offline",
    running: "running",
    login_title: "Panel login",
    login_hint: "Password is required for APIs. The page shell loads without auth (HF health).",
    login_user: "Username",
    login_pass: "Password",
    login_btn: "Sign in",
    logout_btn: "Sign out",
    login_need: "Enter password",
    login_fail: "Login failed: bad username or password",
    card_register: "Register control",
    card_products: "Product downloads",
    card_engines: "Engines",
    card_account_status: "Account status",
    card_inventory: "Account inventory",
    card_config: "Configuration",
    card_raw: "Raw status",
    products_hint: "Scan finished products in keys/ (legacy / sub2api / cpa)",
    products_dir: "Export dir: {dir} · import into sub2api / CPA panels",
    card_run_logs: "Run / fail logs",
    run_logs_hint: "Instant exits, Turnstile timeouts, worker fails. Download full logs when SSH is unavailable.",
    run_logs_meta: "log files {ok}/{n} · recent fails {fails}",
    run_logs_empty: "No run log yet. Start register once to capture output.",
    dl_register_log: "↓ full run log",
    dl_register_fail: "↓ fail events.jsonl",
    dl_register_logs_zip: "↓ logs.zip",
    card_recovery: "Account recovery pack",
    recovery_hint: "Export accounts.txt / auth-sessions / fingerprints / grok.txt / protocol.log. Unpack into keys/ to resume after rebuild.",
    recovery_meta: "Recovery sources: {ok}/{n} present · {size} total",
    dl_recovery_zip: "↓ recovery.zip",
    btn_rebuild: "Rebuild bundles",
    btn_refresh: "Refresh",
    btn_save: "Save changes",
    btn_reload: "Reload",
    btn_convert_sub2api: "Convert → sub2api",
    btn_convert_cpa: "Convert → CPA",
    btn_convert_both: "Convert → sub2api + CPA",
    btn_convert_pending: "Convert pending OAuth only",
    convert_hint: "One-click CPA/sub2api = OAuth file transform (seconds, no browser). “Pending OAuth only” uses SSO browser (slow).",
    convert_starting: "Starting convert…",
    convert_running: "Convert running…",
    convert_done: "Convert finished",
    btn_cpa_sync: "Sync CLIProxyAPI",
    btn_cpa_refresh: "Refresh expired tokens",
    btn_cpa_worker_start: "Start auto-sync",
    btn_cpa_worker_stop: "Stop auto-sync",
    cpa_sync_hint: "Imports single xai-*.json only (type=xai). No merge bundle.",
    cpa_sync_running: "CLIProxyAPI sync running…",
    cpa_sync_done: "CLIProxyAPI sync done",
    btn_start: "Start protocol register",
    btn_start_py: "Start browser register",
    btn_start_browser: "Start browser register",
    btn_start_go: "Start Go",
    btn_stop: "Stop register",
    btn_scrape: "Scrape proxies",
    btn_probe_xai: "Quick probe",
    btn_probe_xai_direct: "Direct only",
    btn_batch_proxies: "Batch test proxies→x.ai",
    btn_scrape_public: "Scrape + auto-test",
    chk_use_public: "Use public nodes",
    chk_use_manual: "Manual pool",
    chk_use_active: "Active pool",
    public_hint: "Before testing, vless/ss/auth-SOCKS are converted to local HTTP via sing-box. Share links preferred over plain public HTTP. Page close does not stop Go job.",
    batch_max: "Max test",
    batch_workers: "Workers",
    batch_timeout: "Timeout s",
    batch_max_active: "Keep OK",
    batch_urls_label: "Test URLs (comma/newline, default x.ai)",
    batch_custom_label: "Custom proxies (optional, one per line)",
    batch_starting: "Starting batch test…",
    batch_running: "Batch testing…",
    batch_done: "Batch test done",
    card_xai_probe: "x.ai connectivity / proxy test",
    xai_probe_hint: "Probe host and proxy pool (concurrent + customizable)",
    xai_probe_running: "Probing…",
    xai_probe_last: "Last probe: ",
    reg_target: "Target",
    reg_engine_pick: "Engine",
    reg_target_ph: "0=unlimited",
    filter_status: "Status",
    filter_format: "Format",
    filter_all: "All",
    th_email: "Email",
    th_status: "Status",
    th_formats: "Formats",
    th_tokens: "Tokens",
    th_ledger: "Ledger",
    th_fingerprint: "Fingerprint",
    th_updated: "Updated",
    loading: "loading…",
    no_accounts: "No accounts",
    config_hint: "Edit all backend settings without SSH. Save writes .env; Save+Restart relaunches even with no dirty fields. Secrets masked; leave blank to keep.",
    btn_save_restart: "Save + restart register",
    btn_export_env: "Export .env",
    btn_import_env: "Import .env",
    btn_import_merge: "Merge import",
    btn_import_replace: "Replace file",
    btn_import_cancel: "Cancel",
    btn_cfg_add: "Add / update",
    btn_cfg_del: "Delete key",
    cfg_presets: "Presets",
    cfg_presets_hint: "Apply common key sets without wiping other keys. Use Apply or Apply+Restart.",
    cfg_custom: "Custom env var",
    cfg_custom_hint: "Keys outside the catalog. Delete removes from .env.",
    cfg_test: "Connectivity tests",
    btn_test_moemail: "Test MoeMail",
    btn_test_xai_cfg: "Test x.ai",
    btn_test_turnstile: "Test Turnstile API",
    cfg_apply_preset: "Apply",
    cfg_apply_preset_restart: "Apply + restart",
    cfg_export_ok: "Downloaded .env backup",
    cfg_import_need: "Paste .env content first",
    cfg_custom_need: "Enter a valid KEY (letters/digits/_)",
    cfg_delete_confirm: "Delete key {key} from .env?",
    actions_disabled: "Actions disabled. Enable “Allow panel actions” or set CONTROL_PLANE_ALLOW_ACTIONS=1",
    last_action: "last: {action} → {message}",
    kpi_success: "Success",
    kpi_success_sub: "Total successes",
    kpi_starts: "Starts",
    kpi_starts_sub: "Registration starts",
    kpi_token_t: "Token T",
    kpi_token_t_sub: "produced {n}",
    kpi_code_q: "Code Q",
    kpi_code_q_sub: "ok/fail {ok}/{fail}",
    kpi_rate: "Rate/min",
    kpi_rate_sub: "Throughput",
    kpi_accounts: "Accounts",
    kpi_accounts_sub: "ready {ready} · pending {pending}",
    kpi_proxies: "Active proxies",
    kpi_proxies_sub: "Passed health checks",
    kpi_scraper: "Scraper cands",
    kpi_scraper_sub: "Proxy candidates",
    reg_status_age: "status age: ",
    reg_email: "email",
    reg_turnstile: "turnstile",
    reg_engine: "engine",
    reg_rate_limit: "rate limit: ",
    reg_rate_open: "OPEN",
    reg_rate_closed: "closed",
    eng_register: "Register engine: ",
    eng_proxy: "Proxy test: ",
    eng_inventory: "Inventory: ",
    eng_turnstile: "Turnstile: ",
    eng_polyglot: "Polyglot: ",
    eng_note: "Requires Python+Go+Rust. REGISTER_ENGINE=go for Go register; inventory defaults to Rust; panel is Python.",
    acc_total: "total",
    acc_formats: "formats: ",
    acc_bundle: "bundle: ",
    meta_show: "showing {shown}/{total} · inventory {inv}",
    cfg_key: "Setting",
    cfg_value: "Value",
    cfg_desc: "Description",
    cfg_restart: "Restart",
    cfg_yes: "yes",
    cfg_no: "no",
    cfg_on: "On",
    cfg_off: "Off",
    cfg_default: "(default)",
    cfg_masked: "(masked — type to replace)",
    cfg_file: "file: {path} · showing {shown}/{n} · extras {e}",
    cfg_no_changes: "no changes",
    cfg_mode_simple: "Simple",
    cfg_mode_all: "All settings",
    cfg_search_ph: "Search email, proxy, export…",
    cfg_empty: "No matching settings",
    cfg_source_file: "from .env",
    cfg_source_process: "process env",
    cfg_source_default: "default",
    cfg_need_restart: "needs register restart",
    status_oauth_ready: "oauth ready",
    status_oauth_pending: "oauth pending",
    status_legacy_sso: "legacy sso",
    status_unknown: "unknown",
  }
};

const LANG_KEY = "gfr_dashboard_lang";
const CFG_MODE_KEY = "gfr_cfg_mode";
const AUTH_KEY = "gfr_dashboard_basic";
const state = { status:null, config:null, dirty:{}, accounts:null, lang:"zh", cfgMode:"simple", cfgSearch:"", authRequired:false };

function detectLang(){
  // 强制默认中文；仅用户主动切到 EN 才记英文
  try{
    const saved = localStorage.getItem(LANG_KEY);
    if(saved === "en") return "en";
    if(saved === "zh") return "zh";
  }catch(e){}
  return "zh";
}
function detectCfgMode(){
  try{
    const m = localStorage.getItem(CFG_MODE_KEY);
    if(m === "all" || m === "simple") return m;
  }catch(e){}
  return "simple";
}
function t(key, vars){
  const pack = I18N[state.lang] || I18N.zh;
  let s = pack[key] ?? I18N.zh[key] ?? key;
  if(vars){
    Object.keys(vars).forEach(k=>{
      s = s.replace(new RegExp("\\{"+k+"\\}","g"), String(vars[k]));
    });
  }
  return s;
}
function $(id){return document.getElementById(id)}
function getBasicAuth(){
  try{ return sessionStorage.getItem(AUTH_KEY) || ""; }catch(e){ return ""; }
}
function setBasicAuth(user, pass){
  const token = btoa(unescape(encodeURIComponent((user||"admin")+":"+(pass||""))));
  try{ sessionStorage.setItem(AUTH_KEY, token); }catch(e){}
  return token;
}
function clearBasicAuth(){
  try{ sessionStorage.removeItem(AUTH_KEY); }catch(e){}
}
function showLogin(msg){
  const ov=$("login-overlay");
  if(!ov) return;
  ov.classList.add("show");
  ov.setAttribute("aria-hidden","false");
  const err=$("login-err");
  if(err) err.textContent = msg || "";
  const lo=$("btn-logout");
  if(lo) lo.style.display="none";
}
function hideLogin(){
  const ov=$("login-overlay");
  if(!ov) return;
  ov.classList.remove("show");
  ov.setAttribute("aria-hidden","true");
  const lo=$("btn-logout");
  if(lo && getBasicAuth()) lo.style.display="inline-flex";
}
async function api(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers||{});
  const basic = getBasicAuth();
  if(basic && !headers.Authorization && !headers.authorization){
    headers.Authorization = "Basic "+basic;
  }
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  let data = null;
  try{ data = await r.json(); }catch(e){ data = {ok:false, error:"bad json", status:r.status}; }
  if(r.status === 401){
    state.authRequired = true;
    showLogin(t("login_fail"));
    data = data || {};
    data.ok = false;
    data.error = data.error || "unauthorized";
    data._http = 401;
  }
  return data;
}
async function downloadAuthed(url, fallbackName){
  const headers = {};
  const basic = getBasicAuth();
  if(basic) headers.Authorization = "Basic "+basic;
  const r = await fetch(url, {headers});
  if(r.status === 401){
    state.authRequired = true;
    showLogin(t("login_fail"));
    return;
  }
  if(!r.ok){
    let msg = "download failed "+r.status;
    try{ const j=await r.json(); msg=j.error||j.message||msg; }catch(e){}
    alert(msg);
    return;
  }
  const cd = r.headers.get("Content-Disposition")||"";
  let name = fallbackName || "download.bin";
  const m = /filename=\"?([^\";]+)\"?/i.exec(cd);
  if(m) name = m[1];
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
document.addEventListener("click",(e)=>{
  const a = e.target && e.target.closest ? e.target.closest("a.dl") : null;
  if(!a || !a.getAttribute("href")) return;
  const href = a.getAttribute("href");
  if(!href.startsWith("/api/download")) return;
  e.preventDefault();
  downloadAuthed(href, a.textContent.replace(/^↓\s*/,"").trim()||"download");
});
function fmt(v){if(v===null||v===undefined||v==='')return '—'; if(typeof v==='number') return Number.isInteger(v)?String(v):v.toFixed(2); return String(v)}
function shortTime(iso){
  if(!iso) return '—';
  try{ const d=new Date(iso); return d.toISOString().replace('T',' ').slice(0,19)+'Z'; }catch(e){ return String(iso).slice(0,19); }
}
function applyStaticI18n(){
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
  document.title = t("title");
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const key = el.getAttribute("data-i18n");
    if(!key) return;
    if(el.tagName === "OPTION"){
      if(el.value === "") el.textContent = t(key);
      else return;
    } else {
      el.textContent = t(key);
    }
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el=>{
    const key = el.getAttribute("data-i18n-placeholder");
    if(key) el.placeholder = t(key);
  });
  document.querySelectorAll("#flt-status option[value=''], #flt-format option[value='']").forEach(o=>{
    o.textContent = t("filter_all");
  });
  $("lang-zh").classList.toggle("active", state.lang === "zh");
  $("lang-en").classList.toggle("active", state.lang === "en");
  $("cfg-mode-simple")?.classList.toggle("active", state.cfgMode === "simple");
  $("cfg-mode-all")?.classList.toggle("active", state.cfgMode === "all");
}
function setLang(lang){
  state.lang = (lang === "en") ? "en" : "zh";
  try{ localStorage.setItem(LANG_KEY, state.lang); }catch(e){}
  applyStaticI18n();
  if(state.status) refreshStatus(true);
  if(state.config){
    renderConfig(state.config);
    updateConfigNote();
  }
  if(state.accounts && !$("tab-accounts").classList.contains("hidden")) loadAccounts();
  else if(state.accounts){
    const sum=state.accounts.summary||{};
    $("accounts-meta").textContent = t("meta_show",{
      shown:state.accounts.returned||0,
      total:state.accounts.total||0,
      inv:sum.total||0
    });
  }
}
function setCfgMode(mode){
  state.cfgMode = mode === "all" ? "all" : "simple";
  try{ localStorage.setItem(CFG_MODE_KEY, state.cfgMode); }catch(e){}
  applyStaticI18n();
  if(state.config) renderConfig(state.config);
  updateConfigNote();
}
document.querySelectorAll(".lang-switch button").forEach(b=>{
  b.onclick = ()=> setLang(b.dataset.lang);
});
document.querySelectorAll("nav button[data-tab]").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("nav button[data-tab]").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    ["overview","accounts","config","raw"].forEach(tab=>{
      $(`tab-${tab}`).classList.toggle("hidden", tab !== b.dataset.tab);
    });
    if(b.dataset.tab === "accounts") loadAccounts();
  };
});
function kpi(title,value,sub){
  const d=document.createElement("div"); d.className="card";
  d.innerHTML=`<h3>${title}</h3><div class="value">${fmt(value)}</div><div class="sub">${sub||""}</div>`;
  return d;
}
function statusLabel(st){
  const map = {
    oauth_ready: "status_oauth_ready",
    oauth_pending: "status_oauth_pending",
    legacy_sso: "status_legacy_sso",
    unknown: "status_unknown",
  };
  return t(map[st] || "status_unknown");
}
function statusTag(st){
  const cls = st==="oauth_ready"?"ok":(st==="oauth_pending"?"warn":"muted");
  const label = statusLabel(st || "unknown");
  return `<span class="tag ${cls}" title="${st||""}">${label}</span>`;
}
function formatTags(fmts){
  return (fmts||[]).map(f=>`<span class="tag">${f}</span>`).join("");
}
async function refreshStatus(fromCache){
  const data = fromCache && state.status ? state.status : await api("/api/status");
  if(!fromCache) state.status = data;
  const reg=data.register||{}, s=data.summary||{}, acc=data.accounts||{};
  const badge=$("run-badge");
  badge.className="badge "+(reg.running?"ok":"bad");
  const runText = reg.running ? t("running") : t("offline");
  badge.innerHTML=`<span class="dot"></span><span>${runText}${reg.pid?" · pid "+reg.pid:""}</span>`;
  const k=$("kpis"); k.innerHTML="";
  const oauthReady = (acc.by_status||{}).oauth_ready || 0;
  const oauthPending = (acc.by_status||{}).oauth_pending || 0;
  [
    [t("kpi_success"), s.success, t("kpi_success_sub")],
    [t("kpi_starts"), s.starts, t("kpi_starts_sub")],
    [t("kpi_token_t"), s.t_depth, t("kpi_token_t_sub",{n:fmt(s.t_prod)})],
    [t("kpi_code_q"), s.q_depth, t("kpi_code_q_sub",{ok:fmt(s.pair_ok),fail:fmt(s.pair_fail)})],
    [t("kpi_rate"), s.rate, t("kpi_rate_sub")],
    [t("kpi_accounts"), acc.count, t("kpi_accounts_sub",{ready:oauthReady,pending:oauthPending})],
    [t("kpi_proxies"), data.proxies?.active, t("kpi_proxies_sub")],
    [t("kpi_scraper"), data.proxies?.scraper?.candidates, t("kpi_scraper_sub")],
  ].forEach(([a,b,c])=>k.appendChild(kpi(a,b,c)));
  const snap=reg.snapshot||{};
  const rl = snap.rate_limit_open ? t("reg_rate_open") : t("reg_rate_closed");
  const rlExtra = snap.rate_limit_open ? ("("+fmt(snap.rate_limit_remaining_sec)+"s)") : "";
  const regBanner = reg.running
    ? `<div style="margin-bottom:8px;padding:8px 10px;border-radius:10px;border:1px solid rgba(61,220,151,.35);background:rgba(61,220,151,.08);color:var(--ok);font-weight:700">▶ ${t("running")}${reg.pid?" · pid "+reg.pid:""} · success ${fmt(s.success)} · starts ${fmt(s.starts)}</div>`
    : `<div style="margin-bottom:8px;padding:8px 10px;border-radius:10px;border:1px solid rgba(255,107,107,.25);background:rgba(255,107,107,.06);color:var(--muted);font-weight:600">■ ${t("offline")}</div>`;
  $("register-body").innerHTML=`
    ${regBanner}
    <div>${t("reg_status_age")}<b>${fmt(reg.status_age_sec)}s</b></div>
    <div style="margin-top:6px">${t("reg_email")} <b>${fmt(snap.email_mode||data.config?.EMAIL_MODE)}</b>
      · ${t("reg_turnstile")} <b>${fmt(snap.turnstile_solver||data.config?.TURNSTILE_SOLVER)}</b>
      · ${t("reg_engine")} <b>${fmt(data.engines?.register)}</b></div>
    <div style="margin-top:6px">${t("reg_rate_limit")}<b>${rl}</b> ${rlExtra}</div>`;
  const eng=data.engines||{};
  const pg=data.polyglot||{};
  const pgMark = pg.ok ? "✓" : "✗";
  $("engines").innerHTML=`
    <div>${t("eng_polyglot")}<b>${pgMark} Python + Go + Rust</b></div>
    <div style="margin-top:6px">${t("eng_register")}<b>${fmt(eng.register)}</b></div>
    <div style="margin-top:6px">${t("eng_proxy")}<b>${fmt(eng.proxy_worker)}</b></div>
    <div style="margin-top:6px">${t("eng_inventory")}<b>${fmt(eng.inventory)}</b></div>
    <div style="margin-top:6px">${t("eng_turnstile")}<b>${fmt(eng.turnstile)}</b></div>
    <div class="note">${t("eng_note")}</div>`;
  const art=acc.artifacts||{};
  $("account-status-body").innerHTML=`
    <div>${t("acc_total")} <b>${fmt(acc.count)}</b></div>
    <div style="margin-top:6px">oauth_ready <b>${fmt(oauthReady)}</b> · oauth_pending <b>${fmt(oauthPending)}</b></div>
    <div style="margin-top:6px">${t("acc_formats")}sub2api <b>${fmt((acc.by_format||{}).sub2api)}</b>
      · cpa <b>${fmt((acc.by_format||{}).cpa)}</b>
      · legacy <b>${fmt((acc.by_format||{}).legacy)}</b></div>
    <div style="margin-top:8px" class="note">
      ${t("acc_bundle")}sub2api ${art.sub2api_bundle?"✓":"—"}
      · cpa singles ${fmt(art.cpa_singles||0)}
      · accounts.txt ${art.legacy_accounts_txt?"✓":"—"}
    </div>`;
  const prods=data.products||{};
  $("product-downloads").innerHTML=`
    <a class="dl" href="${prods.sub2api||"/api/download?format=sub2api"}">↓ accounts.sub2api.json</a>
    <a class="dl" href="${prods.cpa_zip||"/api/download?format=cpa_zip"}">↓ xai-singles.zip</a>
    <a class="dl" href="${prods.legacy||"/api/download?format=legacy"}">↓ accounts.txt</a>
    <span class="note" style="margin-left:8px">CPA 仅 keys/cpa/xai-*.json 单文件</span>`;
  const recFiles = data.recovery_files || [];
  const recRoot = $("recovery-downloads");
  if(recRoot){
    const zipHref = prods.recovery_zip || "/api/download?format=recovery";
    let html = `<a class="dl" href="${zipHref}" style="border-color:rgba(61,220,151,.45)">${t("dl_recovery_zip")}</a>`;
    recFiles.forEach(f=>{
      if(!f || !f.name) return;
      const href = f.download || (`/api/download?format=${f.id||f.name}`);
      const mark = f.exists ? "" : " (missing)";
      const dim = f.exists ? "" : "opacity:.45;pointer-events:none";
      html += `<a class="dl" href="${href}" style="${dim}" title="${f.desc||""}">↓ ${f.name}${mark}</a>`;
    });
    recRoot.innerHTML = html;
  }
  const recMeta = $("recovery-meta");
  if(recMeta){
    const ok = recFiles.filter(f=>f && f.exists).length;
    const bytes = recFiles.reduce((s,f)=>s+((f&&f.exists)?(f.size||0):0),0);
    const size = bytes>=1048576 ? (bytes/1048576).toFixed(1)+" MB" : (bytes>=1024 ? (bytes/1024).toFixed(1)+" KB" : bytes+" B");
    recMeta.textContent = t("recovery_meta",{ok, n: recFiles.length||5, size});
  }
  $("products-body").textContent = t("products_dir",{dir: acc.export_dir||"keys"});
  // run / fail logs (downloadable)
  const rl = data.run_logs || {};
  const rlFiles = rl.files || [];
  const rlRoot = $("run-log-downloads");
  if(rlRoot){
    const dls = rl.downloads || {};
    let html = `<a class="dl" href="${dls.register_logs_zip||"/api/download?format=register_logs_zip"}" style="border-color:rgba(255,209,102,.45)">${t("dl_register_logs_zip")}</a>`;
    html += `<a class="dl" href="${dls.register_log||"/api/download?format=register_log"}">${t("dl_register_log")}</a>`;
    html += `<a class="dl" href="${dls.register_fail||"/api/download?format=register_fail"}">${t("dl_register_fail")}</a>`;
    rlFiles.forEach(f=>{
      if(!f || !f.name || f.id==="register_log" || f.id==="register_fail") return;
      const href = f.download || (`/api/download?format=${f.id}`);
      const dim = f.exists ? "" : "opacity:.45;pointer-events:none";
      html += `<a class="dl" href="${href}" style="${dim}" title="${f.desc||""}">↓ ${f.name}</a>`;
    });
    rlRoot.innerHTML = html;
  }
  const rlMeta = $("run-log-meta");
  if(rlMeta){
    const ok = rlFiles.filter(f=>f && f.exists && (f.size||0)>0).length;
    const fails = (rl.recent_fails||[]).length;
    rlMeta.textContent = t("run_logs_meta",{ok, n: rlFiles.length||3, fails});
  }
  const rlTail = $("run-log-tail");
  if(rlTail){
    const fails = rl.recent_fails || [];
    let body = rl.log_tail || "";
    if(fails.length){
      const failLines = fails.map(f=>{
        const ts = f.ts_iso || "";
        return `[${ts}] ${f.kind||"?"}: ${f.message||""}`;
      }).join("\n");
      body = (body ? body+"\n\n--- recent fails ---\n" : "") + failLines;
    }
    rlTail.textContent = body || t("run_logs_empty");
  }
  // engine select default from server
  const engSel=$("reg-engine");
  if(engSel && !engSel.dataset.touched){
    const re = (data.engines?.register || data.config?.REGISTER_ENGINE || "protocol")+"";
    engSel.value = ["protocol","python","go"].includes(re) ? re : "protocol";
  }
  const tgt=$("reg-target");
  if(tgt && !tgt.dataset.touched){
    const tv = data.config?.TARGET || data.config_full?.items?.find?.(i=>i.key==="TARGET")?.value;
    if(tv!==undefined && tv!==null && String(tv)!=="") tgt.placeholder = String(tv);
  }
  const actions=$("actions"); actions.innerHTML="";
  const enabled=!!data.actions_enabled;
  const mk=(label,action,cls,payload,always)=>{
    const b=document.createElement("button"); b.className="act "+(cls||""); b.textContent=label;
    if(!enabled && !always && action!=="refresh") b.disabled=true;
    b.onclick=async()=>{
      if(action==="refresh"){await refreshStatus();return}
      b.disabled=true;
      try{
        const body=Object.assign({action}, payload||{});
        if(action==="start"){
          body.engine = ($("reg-engine")?.value)||body.engine||"protocol";
          const tval = ($("reg-target")?.value||"").trim();
          if(tval!==""){
            body.target = tval;
            body.args = ["--target", tval];
          }
        }
        const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        let note = r.message||JSON.stringify(r);
        if(r.log_tail) note += "\n"+r.log_tail;
        $("action-note").textContent=note;
        await refreshStatus();
        if(action==="rebuild_bundles") loadAccounts();
      }catch(e){$("action-note").textContent=String(e)}
      finally{b.disabled=(!enabled && !always && action!=="refresh")}
    };
    return b;
  };
  actions.append(mk(t("btn_start"),"start","primary",{engine:"protocol"}));
  actions.append(mk(t("btn_start_browser"),"start","",{engine:"python"}));
  actions.append(mk(t("btn_stop"),"stop","danger"));
  actions.append(mk(t("btn_refresh"),"refresh","","",true));
  actions.append(mk(t("btn_scrape"),"scrape"));
  // last x.ai probe summary
  const lastProbe = data.xai_probe?.last;
  if(lastProbe && lastProbe.message){
    const mark = lastProbe.ok ? "✓" : "✗";
    $("xai-probe-body").innerHTML = `<b>${mark}</b> ${lastProbe.message}<div class="note" style="margin-top:6px">${t("xai_probe_last")}${lastProbe.at_iso||""} · direct=${lastProbe.direct_ok?"✓":"✗"} · proxy=${lastProbe.proxy_ok===null?"—":(lastProbe.proxy_ok?"✓":"✗")}</div>`;
  }
  // public checkbox default once
  const chk=$("chk-use-public");
  if(chk && !chk.dataset.touched){
    chk.checked = !!data.proxies?.use_public_default;
  }
  // batch job status — always restore from server (survives refresh)
  const bj = data.proxies?.batch_job || {};
  renderBatchJob(bj);
  // resume poller after refresh if still running
  if(bj.running) ensureBatchPoller();
  // convert job restore
  const cj = data.convert_job || {};
  const cnote=$("convert-note");
  if(cnote && (cj.running || cj.finished_at || cj.message)){
    if(cj.running){
      cnote.textContent = `${t("convert_running")} ${cj.ok||0}✓ / ${cj.fail||0}✗ · ${cj.message||""}`;
      ensureConvertPoller();
    } else if(cj.message){
      cnote.textContent = `${t("convert_done")} · ${cj.message}`;
    }
  }
  // CLIProxyAPI sync status (display-only; survives refresh)
  const cpa = data.cliproxyapi || {};
  const cpaNote=$("cpa-sync-note");
  if(cpaNote){
    const bits=[
      (cpa.worker_alive||cpa.running) ? "worker✓" : "worker—",
      `files=${cpa.files||0}`,
      `refreshed=${cpa.refreshed||0}`,
      `import=${cpa.imported||0}`,
      cpa.revoked?`revoked=${cpa.revoked}`:"",
      cpa.message||""
    ].filter(Boolean);
    cpaNote.textContent = bits.join(" · ") || t("cpa_sync_hint");
  }
  if(!enabled) $("action-note").textContent=t("actions_disabled");
  else if(data.last_action?.message) $("action-note").textContent=t("last_action",{action:data.last_action.action,message:data.last_action.message});
  $("raw").textContent=JSON.stringify(data,null,2);
}
function renderBatchJob(bj){
  const bnote=$("batch-proxy-note");
  const detail=$("xai-probe-detail");
  if(!bnote || !bj) return;
  if(bj.running){
    const tested = bj.tested||0, total=bj.total||0;
    const shard = bj.shard ? ` · 分片 ${bj.shard}/${bj.shards||"?"}` : "";
    bnote.textContent = `${t("batch_running")} ${tested}/${total} · ${bj.ok||0}✓ / ${bj.fail||0}✗ · 并发${bj.workers||"?"}${shard} · ${bj.message||""}`;
    if(detail){
      if((bj.top||[]).length){
        detail.style.display="block";
        detail.textContent=(bj.top||[]).slice(0,15).map(x=>
          `OK ${x.latency_ms??"?"}ms [${x.source||"?"}] ${(x.proxy||"").slice(0,80)}`
        ).join("\n");
      } else {
        // clear stale "本轮 8" from previous small runs while current job is large
        detail.style.display="block";
        detail.textContent=`进行中：已完成 ${tested}/${total}，本分片结束后数字会跳变。\n并发 ${bj.workers||"?"} · 超时 ${bj.timeout_sec||"?"}s · 死节点会占满超时，属正常。`;
      }
    }
  } else if(bj.finished_at && bj.message){
    bnote.textContent = `${t("batch_done")} · ${bj.message}`;
    if(detail && (bj.top||[]).length){
      detail.style.display="block";
      detail.textContent=(bj.top||[]).slice(0,15).map(x=>
        `OK ${x.latency_ms??"?"}ms [${x.source||"?"}] ${(x.proxy||"").slice(0,80)}`
      ).join("\n");
    } else if(detail && (bj.fail||0)>0 && !(bj.top||[]).length){
      detail.style.display="block";
      detail.textContent=`本轮 ${bj.fail||0}/${bj.total||0} 失败，无可用节点。\n公共免费代理多数已死属正常，可增大「最多测」或换手动代理。`;
    }
  } else if(bj.error){
    bnote.textContent = `✗ ${bj.error}`;
  } else if(bj.message){
    bnote.textContent = bj.message;
  }
}
// single shared pollers so refresh doesn't lose progress UI
let _batchPollTimer=null;
let _convertPollTimer=null;
function ensureBatchPoller(){
  if(_batchPollTimer) return;
  let n=0;
  _batchPollTimer=setInterval(async()=>{
    n+=1;
    try{
      const st=await api("/api/status");
      const job=st.proxies?.batch_job||{};
      renderBatchJob(job);
      if(state.status) state.status.proxies = st.proxies;
      if(!job.running){
        clearInterval(_batchPollTimer);
        _batchPollTimer=null;
        await refreshStatus();
      }
    }catch(e){}
    if(n>600){ clearInterval(_batchPollTimer); _batchPollTimer=null; }
  }, 1000);
}
function ensureConvertPoller(){
  if(_convertPollTimer) return;
  let n=0;
  _convertPollTimer=setInterval(async()=>{
    n+=1;
    try{
      const st=await api("/api/status");
      const job=st.convert_job||{};
      const note=$("convert-note");
      if(note){
        if(job.running) note.textContent=`${t("convert_running")} ${job.ok||0}✓ / ${job.fail||0}✗ · ${job.message||""}`;
        else if(job.message) note.textContent=`${t("convert_done")} · ${job.message}`;
      }
      if(!job.running){
        clearInterval(_convertPollTimer);
        _convertPollTimer=null;
        await loadAccounts();
        await refreshStatus();
      }
    }catch(e){}
    if(n>300){ clearInterval(_convertPollTimer); _convertPollTimer=null; }
  }, 2000);
}
async function runXaiProbe(viaProxy){
  const note=$("xai-probe-note");
  const detail=$("xai-probe-detail");
  const btn=$("btn-probe-xai");
  const btn2=$("btn-probe-xai-direct");
  note.textContent=t("xai_probe_running");
  detail.style.display="none";
  btn.disabled=true; btn2.disabled=true;
  try{
    const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"probe_xai",via_proxy:!!viaProxy})});
    note.textContent=(r.ok?"✓ ":"✗ ")+(r.message||JSON.stringify(r));
    const lines=(r.results||[]).map(x=>{
      const via=x.via==="proxy"?("proxy "+(x.proxy||"").slice(0,40)):"direct";
      return `${x.ok?"OK":"FAIL"} [${via}] ${x.status_code??"-"} ${x.latency_ms}ms ${x.url} ${x.error||""}`;
    });
    if(lines.length){
      detail.style.display="block";
      detail.textContent=lines.join("\n");
    }
    await refreshStatus();
  }catch(e){
    note.textContent=String(e);
  }finally{
    btn.disabled=false; btn2.disabled=false;
  }
}
async function loadAccounts(){
  const st=$("flt-status").value||"";
  const fm=$("flt-format").value||"";
  const q=new URLSearchParams();
  if(st) q.set("status", st);
  if(fm) q.set("format", fm);
  q.set("limit","500");
  const data = await api("/api/accounts?"+q.toString());
  state.accounts=data;
  const sum=data.summary||{};
  $("accounts-meta").textContent = t("meta_show",{shown:data.returned||0,total:data.total||0,inv:sum.total||0});
  const tb=$("accounts-tbody"); tb.innerHTML="";
  (data.accounts||[]).forEach(a=>{
    const tr=document.createElement("tr");
    const tokens=[a.has_sso?"sso":"", a.has_access_token?"at":"", a.has_refresh_token?"rt":""].filter(Boolean).join(" · ")||"—";
    tr.innerHTML=`
      <td><code>${a.email||""}</code></td>
      <td>${statusTag(a.status)}</td>
      <td>${formatTags(a.formats)}</td>
      <td>${tokens}</td>
      <td>${a.ledger_state||"—"}</td>
      <td><code style="font-size:11px">${a.fingerprint||"—"}</code></td>
      <td>${shortTime(a.updated_at)}</td>`;
    tb.appendChild(tr);
  });
  if(!(data.accounts||[]).length){
    tb.innerHTML=`<tr><td colspan="7">${t("no_accounts")}</td></tr>`;
  }
}
$("btn-refresh-accounts").onclick=()=>loadAccounts();
$("flt-status").onchange=$("flt-format").onchange=()=>loadAccounts();
$("btn-rebuild-bundles").onclick=async()=>{
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"rebuild_bundles"})});
  $("product-note").textContent=r.message||JSON.stringify(r);
  await refreshStatus();
  await loadAccounts();
};
function sourceLabel(src){
  if(src==="file") return t("cfg_source_file");
  if(src==="process") return t("cfg_source_process");
  return t("cfg_source_default");
}
function matchConfigItem(it, q){
  if(!q) return true;
  const blob = [it.key, it.label, it.desc, it.group, ...(it.options||[]).map(o=>o.label||o.value||"")].join(" ").toLowerCase();
  return blob.includes(q);
}
function makeConfigInput(it){
  let input;
  const cur = it.value ?? "";
  const curStr = String(cur);
  const isBoolOn = ["1","true","yes","on"].includes(curStr.toLowerCase());
  const isBoolOff = ["0","false","no","off"].includes(curStr.toLowerCase());
  if(it.type==="bool"){
    input=document.createElement("select");
    [[ "", t("cfg_default") ],[ "1", t("cfg_on") ],[ "0", t("cfg_off") ]].forEach(([v,l])=>{
      const o=document.createElement("option"); o.value=v; o.textContent=l;
      if(v==="1" && isBoolOn) o.selected=true;
      else if(v==="0" && isBoolOff) o.selected=true;
      else if(v==="" && !isBoolOn && !isBoolOff) o.selected=true;
      input.appendChild(o);
    });
  } else if(it.type==="choice" && Array.isArray(it.options) && it.options.length){
    input=document.createElement("select");
    const hasExact = it.options.some(op => String(op.value) === curStr);
    if(!hasExact && curStr){
      const o=document.createElement("option"); o.value=curStr; o.textContent=curStr+" *"; o.selected=true; input.appendChild(o);
    }
    it.options.forEach(op=>{
      const o=document.createElement("option");
      o.value = op.value;
      o.textContent = op.label || op.value;
      if(String(op.value) === curStr || (!hasExact && !curStr && String(op.value)===String(it.default||""))) o.selected=true;
      if(String(op.value) === curStr) o.selected=true;
      input.appendChild(o);
    });
  } else {
    input=document.createElement("input");
    input.type = it.type==="secret" ? "password" : (it.type==="int" ? "number" : "text");
    if(it.type==="int") input.step="1";
    input.value = curStr;
    input.placeholder = it.type==="secret" ? t("cfg_masked") : (it.placeholder || it.default || "");
    if(it.type==="secret") input.autocomplete="new-password";
  }
  input.dataset.key=it.key;
  input.onchange=input.oninput=()=>{ state.dirty[it.key]=input.value; };
  // restore dirty edit if any
  if(Object.prototype.hasOwnProperty.call(state.dirty, it.key)){
    input.value = state.dirty[it.key];
  }
  return input;
}
function renderConfig(view){
  state.config=view;
  const root=$("config-editor"); root.innerHTML="";
  const q = (state.cfgSearch||"").trim().toLowerCase();
  let items = (view.items||[]).slice();
  if(state.cfgMode === "simple" && !q){
    items = items.filter(it => it.simple);
  }
  items = items.filter(it => matchConfigItem(it, q));
  const extras = (view.extras||[]).filter(it => matchConfigItem(it, q) && state.cfgMode === "all");
  const groups={};
  items.forEach(it=>{ (groups[it.group]=groups[it.group]||[]).push(it); });
  if(extras.length) groups.extra = extras;
  const labels=Object.fromEntries((state.status?.config_groups||[]).map(g=>[g.id,g.label]));
  labels.extra = state.lang==="zh" ? "额外 .env 项" : "Extra .env keys";
  // preserve group order from GROUPS
  const order = (state.status?.config_groups||[]).map(g=>g.id).concat(["extra"]);
  const gids = order.filter(g => groups[g] && groups[g].length);
  Object.keys(groups).forEach(g => { if(!gids.includes(g)) gids.push(g); });
  let shown = 0;
  gids.forEach(gid=>{
    const list = groups[gid]||[];
    if(!list.length) return;
    const title=document.createElement("div"); title.className="group-title";
    title.innerHTML=`${labels[gid]||gid}<span class="pill">${list.length}</span>`;
    root.appendChild(title);
    list.forEach(it=>{
      shown += 1;
      const card=document.createElement("div"); card.className="cfg-card";
      const head=document.createElement("div");
      head.innerHTML=`<div class="cfg-label">${it.label||it.key}</div><div class="cfg-key"><code>${it.key}</code></div>`;
      const fields=document.createElement("div"); fields.className="row-fields";
      const left=document.createElement("div");
      left.appendChild(makeConfigInput(it));
      const help=document.createElement("div"); help.className="cfg-help"; help.textContent=it.desc||"";
      left.appendChild(help);
      const right=document.createElement("div");
      right.className="cfg-meta";
      right.innerHTML = `
        <span class="tag muted">${sourceLabel(it.source)}</span>
        ${it.restart?`<span class="tag warn">${t("cfg_need_restart")}</span>`:""}
        ${it.simple?`<span class="tag ok">${state.lang==="zh"?"常用":"simple"}</span>`:""}
      `;
      fields.append(left, right);
      card.append(head, fields);
      root.appendChild(card);
    });
  });
  if(!shown){
    root.innerHTML=`<div class="cfg-empty">${t("cfg_empty")}</div>`;
  }
  state._cfgShown = shown;
  updateConfigNote();
}
function updateConfigNote(){
  if(!state.config) return;
  const view=state.config;
  $("config-note").textContent=t("cfg_file",{
    path:view.path,
    shown:state._cfgShown||0,
    n:view.items?.length||0,
    e:view.extras?.length||0
  });
}
async function loadConfig(){
  const view=await api("/api/config");
  renderConfig(view);
  renderPresets();
}
function renderPresets(){
  const root=$("cfg-presets"); if(!root) return;
  const list = state.status?.config_presets || state.config?.presets || [];
  root.innerHTML="";
  if(!list.length){ root.innerHTML=`<span class="note">—</span>`; return; }
  list.forEach(p=>{
    const card=document.createElement("div");
    card.className="cfg-preset";
    card.innerHTML=`<div style="font-weight:700">${p.label||p.id}</div>
      <div class="note" style="margin:4px 0 8px">${p.desc||""}</div>
      <div class="note" style="font-size:11px">${(p.keys||[]).slice(0,8).join(", ")}${(p.keys||[]).length>8?"…":""}</div>`;
    const row=document.createElement("div");
    row.className="actions"; row.style.marginTop="8px";
    const b=document.createElement("button");
    b.className="act"; b.textContent=t("cfg_apply_preset");
    const br=document.createElement("button");
    br.className="act primary"; br.textContent=t("cfg_apply_preset_restart");
    const apply=async(restart)=>{
      b.disabled=true; br.disabled=true;
      try{
        const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({action: restart?"apply_preset_restart":"apply_preset",preset:p.id})});
        $("config-note").textContent=r.message||JSON.stringify(r);
        state.dirty={};
        await loadConfig();
        await refreshStatus();
      }finally{ b.disabled=false; br.disabled=false; }
    };
    b.onclick=()=>apply(false);
    br.onclick=()=>apply(true);
    row.append(b, br);
    card.appendChild(row);
    root.appendChild(card);
  });
}
async function saveConfigUpdates(updates, {restart=false}={}){
  updates = updates || {};
  if(!restart && !Object.keys(updates).length){
    $("config-note").textContent=t("cfg_no_changes");
    return null;
  }
  // restart with empty updates still relaunches register (after preset / import)
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action: restart?"save_config_restart":"save_config", updates})});
  $("config-note").textContent=r.message||JSON.stringify(r);
  state.dirty={};
  await loadConfig();
  await refreshStatus();
  return r;
}
$("btn-reload-config").onclick=()=>{state.dirty={}; loadConfig()};
$("btn-save-config").onclick=()=>saveConfigUpdates(state.dirty, {restart:false});
$("btn-save-restart").onclick=()=>saveConfigUpdates(state.dirty, {restart:true});
$("btn-export-env").onclick=async()=>{
  const r=await api("/api/config/export");
  if(!r.ok){ $("config-note").textContent=r.error||JSON.stringify(r); return; }
  const blob=new Blob([r.text||""],{type:"text/plain;charset=utf-8"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="grok-register.env";
  a.click();
  URL.revokeObjectURL(a.href);
  $("config-note").textContent=t("cfg_export_ok")+" · "+(r.path||"");
};
$("btn-import-env").onclick=()=>{
  $("cfg-import-box").classList.remove("hidden");
  $("cfg-import-actions").classList.remove("hidden");
  $("cfg-import-box").focus();
};
$("btn-import-cancel").onclick=()=>{
  $("cfg-import-box").classList.add("hidden");
  $("cfg-import-actions").classList.add("hidden");
};
async function doImport(replace){
  const text=$("cfg-import-box").value||"";
  if(!text.trim()){ $("config-note").textContent=t("cfg_import_need"); return; }
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"import_env", text, replace:!!replace})});
  $("config-note").textContent=r.message||JSON.stringify(r);
  $("cfg-import-box").classList.add("hidden");
  $("cfg-import-actions").classList.add("hidden");
  await loadConfig();
  await refreshStatus();
}
$("btn-import-merge").onclick=()=>doImport(false);
$("btn-import-replace").onclick=()=>{
  if(!confirm(state.lang==="zh"?"将用粘贴内容整文件替换 .env，确定？":"Replace entire .env with paste?")) return;
  doImport(true);
};
$("btn-cfg-custom-add").onclick=async()=>{
  const key=($("cfg-custom-key").value||"").trim();
  const val=$("cfg-custom-val").value??"";
  if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)){
    $("config-note").textContent=t("cfg_custom_need");
    return;
  }
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"save_config",updates:{[key]:val}})});
  $("config-note").textContent=r.message||JSON.stringify(r);
  $("cfg-custom-key").value="";
  $("cfg-custom-val").value="";
  await loadConfig();
  await refreshStatus();
};
$("btn-cfg-custom-del").onclick=async()=>{
  const key=($("cfg-custom-key").value||"").trim();
  if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)){
    $("config-note").textContent=t("cfg_custom_need");
    return;
  }
  if(!confirm(t("cfg_delete_confirm",{key}))) return;
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"delete_env_keys",keys:[key]})});
  $("config-note").textContent=r.message||JSON.stringify(r);
  $("cfg-custom-key").value="";
  $("cfg-custom-val").value="";
  await loadConfig();
  await refreshStatus();
};
function setTestOut(msg){ const el=$("cfg-test-out"); if(el) el.textContent=msg; }
$("btn-test-moemail").onclick=async()=>{
  setTestOut("…");
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"test_moemail"})});
  setTestOut(JSON.stringify(r,null,2));
};
$("btn-test-xai").onclick=async()=>{
  setTestOut("…");
  const r=await runXaiProbe(false);
  setTestOut(typeof r==="string"?r:JSON.stringify(r,null,2));
};
$("btn-test-turnstile").onclick=async()=>{
  setTestOut("…");
  const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"test_turnstile"})});
  setTestOut(JSON.stringify(r,null,2));
};
$("cfg-mode-simple").onclick=()=>setCfgMode("simple");
$("cfg-mode-all").onclick=()=>setCfgMode("all");
$("cfg-search").oninput=(e)=>{ state.cfgSearch=e.target.value||""; if(state.config) renderConfig(state.config); };
$("btn-probe-xai").onclick=()=>runXaiProbe(true);
$("btn-probe-xai-direct").onclick=()=>runXaiProbe(false);
$("reg-engine")?.addEventListener("change",()=>{ $("reg-engine").dataset.touched="1"; });
$("reg-target")?.addEventListener("input",()=>{ $("reg-target").dataset.touched="1"; });
$("chk-use-public")?.addEventListener("change",()=>{ $("chk-use-public").dataset.touched="1"; });

function clampInt(v, lo, hi, def){
  let n=parseInt(v,10);
  if(!Number.isFinite(n)) n=def;
  if(n<lo) n=lo;
  if(n>hi) n=hi;
  return n;
}
function readBatchOptions(){
  const usePublic = !!$("chk-use-public")?.checked;
  const useManual = $("chk-use-manual") ? !!$("chk-use-manual").checked : true;
  const useActive = $("chk-use-active") ? !!$("chk-use-active").checked : true;
  const workers = clampInt($("batch-workers")?.value, 1, 2048, 128);
  const timeout = clampInt($("batch-timeout")?.value, 2, 120, 5);
  const maxC = clampInt($("batch-max")?.value, 1, 40000, 200);
  const maxActive = clampInt($("batch-max-active")?.value, 0, 40000, 0);
  const urls = ($("batch-urls")?.value||"").trim();
  const custom = ($("batch-custom")?.value||"").trim();
  // persist last settings
  try{
    localStorage.setItem("gfr_batch_opts", JSON.stringify({
      usePublic, useManual, useActive, workers, timeout, maxC, maxActive, urls, custom
    }));
  }catch(e){}
  return {usePublic, useManual, useActive, workers, timeout, maxC, maxActive, urls, custom};
}
function restoreBatchOptions(){
  try{
    const raw=localStorage.getItem("gfr_batch_opts");
    if(!raw) return;
    const o=JSON.parse(raw);
    if($("chk-use-public") && typeof o.usePublic==="boolean"){ $("chk-use-public").checked=o.usePublic; $("chk-use-public").dataset.touched="1"; }
    if($("chk-use-manual") && typeof o.useManual==="boolean") $("chk-use-manual").checked=o.useManual;
    if($("chk-use-active") && typeof o.useActive==="boolean") $("chk-use-active").checked=o.useActive;
    if($("batch-workers") && o.workers) $("batch-workers").value=o.workers;
    if($("batch-timeout") && o.timeout) $("batch-timeout").value=o.timeout;
    if($("batch-max") && o.maxC) $("batch-max").value=o.maxC;
    if($("batch-max-active") && o.maxActive!==undefined) $("batch-max-active").value=o.maxActive;
    if($("batch-urls") && o.urls) $("batch-urls").value=o.urls;
    if($("batch-custom") && o.custom) $("batch-custom").value=o.custom;
  }catch(e){}
}
async function runBatchProxyTest(){
  const note=$("batch-proxy-note");
  const o=readBatchOptions();
  if(!o.usePublic && !o.useManual && !o.useActive && !o.custom){
    if(note) note.textContent="请至少勾选：使用公共节点 / 手动池 / 已测活池，或填写自定义代理";
    return;
  }
  if(note) note.textContent=`${t("batch_starting")} · workers=${o.workers} · timeout=${o.timeout}s · max=${o.maxC}`;
  try{
    const body={
      action:"batch_test_proxies",
      use_public: o.usePublic,
      use_manual: o.useManual,
      use_active: o.useActive,
      max_candidates: o.maxC,
      workers: o.workers,
      timeout: o.timeout,
      max_active: o.maxActive,
      background: true,
    };
    if(o.urls) body.test_urls = o.urls;
    if(o.custom) body.custom_proxies = o.custom;
    const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(note) note.textContent=r.message||JSON.stringify(r);
    if(r.job) renderBatchJob(r.job);
    if(r.ok===false && !r.job?.running) return;
    // shared poller survives page refresh via server-side job file
    ensureBatchPoller();
  }catch(e){
    if(note) note.textContent=String(e);
  }
}
$("btn-batch-proxies")?.addEventListener("click",()=>runBatchProxyTest());
$("btn-scrape-public")?.addEventListener("click",async()=>{
  const note=$("batch-proxy-note");
  if(note) note.textContent="正在爬取公共节点，完成后将自动启动 Go 测活…";
  try{
    const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"scrape",auto_test:true})});
    if(note) note.textContent=r.message||JSON.stringify(r);
    // auto-enable public after scrape intent
    const chk=$("chk-use-public");
    if(chk){ chk.checked=true; chk.dataset.touched="1"; }
    // poll batch job — scrape finishes later then auto-test starts
    ensureBatchPoller();
  }catch(e){ if(note) note.textContent=String(e); }
});

async function startConvert(formats, onlyPending, opts){
  const note=$("convert-note");
  if(!note) return;
  note.textContent=t("convert_starting");
  opts = opts || {};
  // one-click CPA/sub2api: pure OAuth file transform (fast). Browser enroll only for「待 OAuth」.
  const allowEnroll = opts.allow_enroll != null ? !!opts.allow_enroll : !!onlyPending;
  try{
    const body={
      action:"convert",
      formats: formats,
      only_pending: !!onlyPending,
      allow_enroll: allowEnroll,
      background: true,
      limit: opts.limit != null ? opts.limit : 2000,
    };
    const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    note.textContent=r.message||JSON.stringify(r);
    // shared poller + disk job file → survives page refresh
    ensureConvertPoller();
  }catch(e){
    note.textContent=String(e);
  }
}
$("btn-convert-sub2api")?.addEventListener("click",()=>startConvert(["sub2api"], false, {allow_enroll:false, limit:2000}));
$("btn-convert-cpa")?.addEventListener("click",()=>startConvert(["cpa"], false, {allow_enroll:false, limit:2000}));
$("btn-convert-both")?.addEventListener("click",()=>startConvert(["sub2api","cpa"], false, {allow_enroll:false, limit:2000}));
$("btn-convert-pending")?.addEventListener("click",()=>startConvert(["sub2api","cpa"], true, {allow_enroll:true, limit:20}));

async function runCpaAction(action){
  const note=$("cpa-sync-note");
  if(note) note.textContent=t("cpa_sync_running");
  try{
    const r=await api("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
    if(note) note.textContent=`${t("cpa_sync_done")} · ${r.message||JSON.stringify(r)}`;
    await refreshStatus();
  }catch(e){
    if(note) note.textContent=String(e);
  }
}
$("btn-cpa-sync")?.addEventListener("click",()=>runCpaAction("cliproxy_sync"));
$("btn-cpa-refresh")?.addEventListener("click",()=>runCpaAction("cliproxy_refresh"));
$("btn-cpa-worker-start")?.addEventListener("click",()=>runCpaAction("cliproxy_worker_start"));
$("btn-cpa-worker-stop")?.addEventListener("click",()=>runCpaAction("cliproxy_worker_stop"));

state.lang = detectLang();
state.cfgMode = detectCfgMode();
applyStaticI18n();
restoreBatchOptions();
$("btn-login")?.addEventListener("click", async()=>{
  const user = ($("login-user")?.value || "admin").trim() || "admin";
  const pass = $("login-pass")?.value || "";
  if(!pass){ $("login-err").textContent=t("login_need"); return; }
  setBasicAuth(user, pass);
  $("login-err").textContent="…";
  try{
    const r = await api("/api/status");
    if(r && r.ok !== false && !r._http){
      hideLogin();
      $("login-err").textContent="";
      await refreshStatus();
      await loadConfig();
    }else{
      clearBasicAuth();
      showLogin(t("login_fail"));
    }
  }catch(e){
    clearBasicAuth();
    showLogin(String(e));
  }
});
$("login-pass")?.addEventListener("keydown",(e)=>{ if(e.key==="Enter") $("btn-login")?.click(); });
$("btn-logout")?.addEventListener("click",()=>{
  clearBasicAuth();
  showLogin("");
});
// Probe health (public) then status (may need auth)
(async()=>{
  try{
    const h = await fetch("/api/health").then(r=>r.json()).catch(()=>({}));
    state.authRequired = !!h.auth_required;
  }catch(e){}
  if(state.authRequired && !getBasicAuth()){
    showLogin("");
  }else{
    hideLogin();
  }
  refreshStatus();
  loadConfig();
})();
setInterval(()=>refreshStatus(), 2000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "grok-dashboard/0.1"

    def log_message(self, fmt, *args):
        # quieter default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _send_file(self, file_path: Path, media_type: str, download_name: str):
        if not file_path.is_file():
            self._json(404, {"ok": False, "error": f"file not found: {download_name}"})
            return
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{download_name}"',
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _unauthorized(self, *, for_browser: bool = False) -> None:
        # Never challenge HTML with WWW-Authenticate: HF Space health checks
        # probe GET / and treat 401 as "starting forever".
        body = json.dumps(
            {
                "ok": False,
                "error": "unauthorized",
                "auth_required": True,
                "message": "需要登录：在面板登录框输入 DASHBOARD_USER/PASSWORD，"
                "或 Authorization: Basic / Bearer <CONTROL_PLANE_TOKEN>",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Intentionally omit WWW-Authenticate so fetch() can show in-page login
        # instead of a browser modal that breaks HF readiness probes.
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, path: str, qs: dict | None = None) -> bool:
        if not auth_required():
            return True
        if path in AUTH_PUBLIC_PATHS:
            return True
        auth = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        if DASHBOARD_PASSWORD and _check_basic_auth(auth):
            return True
        if CONTROL_PLANE_TOKEN and (
            _check_bearer_auth(auth) or _check_query_token(qs or {})
        ):
            return True
        # X-API-Key / X-Control-Token headers
        if CONTROL_PLANE_TOKEN:
            for hk in ("X-API-Key", "X-Control-Token", "X-Dashboard-Token"):
                hv = self.headers.get(hk) or ""
                if hv and _const_eq(hv.strip(), CONTROL_PLANE_TOKEN):
                    return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        # Health / readiness — always 200 (HF Space readiness probe)
        if path in {"/api/health", "/health", "/healthz", "/ready", "/readyz"}:
            self._json(
                200,
                {
                    "ok": True,
                    "ready": True,
                    "auth_required": auth_required(),
                    "public_url": public_dashboard_url(),
                    "space_id": (os.environ.get("SPACE_ID") or "").strip() or None,
                },
            )
            return
        # HTML shell always 200 so platform probes and first paint succeed
        if path in {"/", "/index.html"}:
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._authorized(path, qs):
            self._unauthorized(for_browser=False)
            return
        if path == "/api/status":
            self._json(200, build_overview())
            return
        if path in {"/api/probe/xai", "/api/xai/probe"}:
            qs = parse_qs(urlparse(self.path).query)
            via = (qs.get("via_proxy") or ["1"])[0].lower() not in {"0", "false", "no"}
            try:
                timeout = float((qs.get("timeout") or ["12"])[0])
            except ValueError:
                timeout = 12.0
            self._json(200, probe_xai_access(timeout=timeout, via_proxy=via))
            return
        if path == "/api/status/raw":
            self._json(200, read_status() or {"empty": True})
            return
        if path == "/api/accounts":
            try:
                limit = int((qs.get("limit") or ["500"])[0])
            except ValueError:
                limit = 500
            status = (qs.get("status") or [""])[0]
            fmt = (qs.get("format") or [""])[0]
            self._json(200, build_accounts_payload(limit=limit, status=status, fmt=fmt))
            return
        if path == "/api/accounts/summary":
            try:
                self._json(200, {"ok": True, **inv.inventory_summary()})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/api/download":
            fmt = (qs.get("format") or qs.get("fmt") or ["sub2api"])[0]
            rebuild = (qs.get("rebuild") or ["0"])[0].lower() in {"1", "true", "yes"}
            try:
                if rebuild:
                    inv.ensure_bundles(rebuild=True)
                file_path, media, name = inv.download_spec(fmt)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            self._send_file(file_path, media, name)
            return
        if path == "/api/config":
            reveal = (qs.get("reveal") or ["0"])[0] in {"1", "true", "yes"}
            # secrets reveal only when actions enabled
            if reveal and not ALLOW_ACTIONS:
                reveal = False
            self._json(
                200,
                {
                    "ok": True,
                    **load_config_view(reveal_secrets=reveal),
                    "catalog": catalog_public(),
                    "presets": presets_public(),
                },
            )
            return
        if path == "/api/config/export":
            # full .env text for backup (auth already required)
            self._json(200, read_env_raw())
            return
        if path == "/api/config/presets":
            self._json(200, {"ok": True, "presets": presets_public()})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/action":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        # Auth before any action (token may also be in JSON body)
        qs_extra: dict = {}
        if isinstance(data, dict):
            for key in ("token", "access_token", "api_token"):
                if data.get(key):
                    qs_extra[key] = [str(data.get(key))]
        if not self._authorized(path, qs_extra):
            self._unauthorized(for_browser=False)
            return
        action = str((data or {}).get("action") or "").strip().lower()
        # Safe/read-side actions: no CONTROL_PLANE_ALLOW_ACTIONS required
        if action == "rebuild_bundles":
            try:
                paths = inv.ensure_bundles(rebuild=True)
                result = {
                    "ok": True,
                    "message": f"rebuilt {len(paths)} artifacts",
                    "paths": paths,
                }
            except Exception as exc:
                result = {"ok": False, "message": str(exc)}
            _record_last_action(action, result)
            self._json(200, result)
            return
        if action in {"probe_xai", "probe", "test_xai"}:
            via_proxy = (data or {}).get("via_proxy", True)
            if isinstance(via_proxy, str):
                via_proxy = via_proxy.strip().lower() not in {"0", "false", "no", "off"}
            try:
                timeout = (data or {}).get("timeout")
                result = probe_xai_access(
                    timeout=float(timeout) if timeout not in (None, "") else None,
                    via_proxy=bool(via_proxy),
                    proxy_limit=int((data or {}).get("proxy_limit") or 3),
                )
            except Exception as exc:
                result = {"ok": False, "message": str(exc), "results": []}
            _record_last_action("probe_xai", result)
            self._json(200, result)
            return
        if action in {"convert", "convert_accounts", "to_cpa", "to_sub2api"}:
            # format aliases
            raw_fmt = (data or {}).get("formats") or (data or {}).get("format") or []
            if isinstance(raw_fmt, str):
                formats = [x.strip() for x in raw_fmt.split(",") if x.strip()]
            else:
                formats = list(raw_fmt or [])
            if action == "to_cpa" and not formats:
                formats = ["cpa"]
            if action == "to_sub2api" and not formats:
                formats = ["sub2api"]
            emails = (data or {}).get("emails")
            if isinstance(emails, str):
                emails = [e.strip() for e in emails.split(",") if e.strip()]
            only_pending = bool((data or {}).get("only_pending"))
            # Default: OAuth file transform only (fast). Enroll is opt-in.
            if "allow_enroll" in (data or {}):
                allow_enroll = (data or {}).get("allow_enroll")
            else:
                allow_enroll = bool(only_pending)
            if isinstance(allow_enroll, str):
                allow_enroll = allow_enroll.strip().lower() not in {"0", "false", "no", "off"}
            try:
                default_limit = 20 if allow_enroll else 2000
                limit = int((data or {}).get("limit") or default_limit)
            except (TypeError, ValueError):
                limit = 20 if allow_enroll else 2000
            background = (data or {}).get("background", True)
            if isinstance(background, str):
                background = background.strip().lower() not in {"0", "false", "no", "off"}
            try:
                if background:
                    result = acct_convert.start_convert_job(
                        emails,
                        formats,
                        only_pending=only_pending,
                        allow_enroll=allow_enroll,
                        limit=limit,
                    )
                else:
                    result = acct_convert.convert_accounts(
                        emails,
                        formats,
                        only_pending=only_pending,
                        allow_enroll=allow_enroll,
                        rebuild=True,
                        limit=limit,
                    )
            except Exception as exc:
                result = {"ok": False, "message": str(exc)}
            _record_last_action("convert", result)
            self._json(200, result)
            return
        if action in {"convert_status", "convert_job"}:
            self._json(200, {"ok": True, "job": acct_convert.job_status()})
            return
        if action in {
            "cliproxy_sync",
            "cliproxyapi_sync",
            "cpa_sync",
            "sync_cliproxy",
            "cliproxy_refresh",
            "cliproxyapi_refresh",
            "refresh_tokens",
            "cliproxy_worker_start",
            "cliproxy_worker_stop",
            "cliproxy_status",
        }:
            try:
                if action in {"cliproxy_status"}:
                    result = {"ok": True, "job": cpa_sync.job_status()}
                elif action in {"cliproxy_worker_start"}:
                    result = cpa_sync.start_worker()
                elif action in {"cliproxy_worker_stop"}:
                    result = cpa_sync.stop_worker()
                elif action in {"cliproxy_refresh", "cliproxyapi_refresh", "refresh_tokens"}:
                    # panel button forces refresh of every account that has refresh_token
                    force_raw = (data or {}).get("force")
                    if force_raw is None:
                        force = True
                    elif isinstance(force_raw, str):
                        force = force_raw.strip().lower() not in {"0", "false", "no", "off"}
                    else:
                        force = bool(force_raw)
                    limit = (data or {}).get("limit")
                    try:
                        limit_i = int(limit) if limit not in (None, "") else None
                    except (TypeError, ValueError):
                        limit_i = None
                    import_raw = (data or {}).get("import", True)
                    if isinstance(import_raw, str):
                        do_import = import_raw.strip().lower() not in {"0", "false", "no", "off"}
                    else:
                        do_import = bool(import_raw)
                    result = cpa_sync.run_once(
                        refresh=True,
                        import_files=do_import,
                        force_refresh=force,
                        limit=limit_i,
                        proxy=str((data or {}).get("proxy") or ""),
                    )
                    result["message"] = result.get("message") or "refresh done"
                else:
                    # full sync: refresh due + import singles
                    force = bool((data or {}).get("force"))
                    limit = (data or {}).get("limit")
                    try:
                        limit_i = int(limit) if limit not in (None, "") else None
                    except (TypeError, ValueError):
                        limit_i = None
                    result = cpa_sync.run_once(
                        refresh=True,
                        import_files=True,
                        force_refresh=force,
                        limit=limit_i,
                        proxy=str((data or {}).get("proxy") or ""),
                    )
            except Exception as exc:
                result = {"ok": False, "message": str(exc)}
            _record_last_action(action, result)
            self._json(200, result)
            return
        if action in {"batch_test_proxies", "batch_proxy_test", "test_proxies_xai"}:
            def _b(key, default=False):
                v = (data or {}).get(key, default)
                if isinstance(v, str):
                    return v.strip().lower() not in {"0", "false", "no", "off"}
                return bool(v)

            def _i(key, default=None):
                v = (data or {}).get(key, default)
                if v in (None, ""):
                    return default
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return default

            use_public = _b("use_public", False)
            use_manual = _b("use_manual", True)
            use_active = _b("use_active", True)
            background = _b("background", True)
            max_candidates = _i("max_candidates", 200) or 200
            max_candidates = max(1, min(int(max_candidates), 40000))
            workers = _i("workers", None)
            timeout = _i("timeout", None)
            max_active = _i("max_active", None)
            if max_active is not None:
                max_active = max(0, min(int(max_active), 40000))
            test_urls = (data or {}).get("test_urls") or (data or {}).get("urls")
            custom_proxies = (
                (data or {}).get("custom_proxies")
                or (data or {}).get("proxies")
                or (data or {}).get("custom")
            )
            custom_file = (data or {}).get("custom_file") or (data or {}).get("proxy_file")
            try:
                if background:
                    result = proxy_batch.start_batch_job(
                        use_public=use_public,
                        use_manual=use_manual,
                        use_active=use_active,
                        max_candidates=max_candidates,
                        workers=workers,
                        timeout=timeout,
                        test_urls=test_urls,
                        custom_proxies=custom_proxies,
                        custom_file=custom_file,
                        max_active=max_active,
                    )
                else:
                    result = proxy_batch.run_batch_xai_test(
                        use_public=use_public,
                        use_manual=use_manual,
                        use_active=use_active,
                        max_candidates=max_candidates,
                        workers=workers,
                        timeout=timeout,
                        test_urls=test_urls,
                        custom_proxies=custom_proxies,
                        custom_file=custom_file,
                        max_active=max_active,
                        write_active=True,
                    )
            except Exception as exc:
                result = {"ok": False, "message": str(exc)}
            _record_last_action("batch_test_proxies", result)
            self._json(200, result)
            return
        if action in {"batch_test_status", "proxy_batch_status"}:
            self._json(200, {"ok": True, "job": proxy_batch.job_status()})
            return
        if not ALLOW_ACTIONS:
            self._json(403, {"ok": False, "error": "actions disabled (CONTROL_PLANE_ALLOW_ACTIONS=0)"})
            return
        with _action_lock:
            if action == "start":
                engine = str(
                    (data or {}).get("engine")
                    or os.environ.get("REGISTER_ENGINE")
                    or "protocol"
                ).lower()
                args = list((data or {}).get("args") or [])
                target = (data or {}).get("target")
                if target not in (None, "") and "--target" not in args:
                    args = ["--target", str(target), *args]
                if engine == "go":
                    payload = dict(data or {})
                    if target not in (None, ""):
                        payload["target"] = target
                    result = _spawn_go_register(payload)
                else:
                    # protocol / python / http all use Python entry; protocol skips browser path
                    result = _spawn_register(args, engine=engine)
            elif action == "stop":
                result = _stop_register()
            elif action == "scrape":
                result = _run_scrape(data or {})
            elif action == "save_config":
                updates = (data or {}).get("updates") or {}
                result = update_env_values(updates, allow_unknown=True)
                result["message"] = (
                    f"updated {len(result.get('changed') or [])} keys"
                    + ("; restart register to apply" if result.get("needs_restart") else "")
                )
            elif action in {"save_config_restart", "apply_and_restart"}:
                updates = (data or {}).get("updates") or {}
                save = update_env_values(updates, allow_unknown=True)
                # restart register with current REGISTER_ENGINE / TARGET
                engine = (
                    str((data or {}).get("engine") or os.environ.get("REGISTER_ENGINE") or "protocol")
                    .strip()
                    .lower()
                )
                target = (data or {}).get("target")
                if target in (None, ""):
                    target = os.environ.get("TARGET") or ""
                stop = _stop_register()
                args: list[str] = []
                if target not in (None, ""):
                    args = ["--target", str(target)]
                if engine == "go":
                    start = _spawn_go_register({"target": target} if target not in (None, "") else {})
                else:
                    start = _spawn_register(args, engine=engine)
                result = {
                    "ok": bool(save.get("ok")) and bool(start.get("ok")),
                    "save": save,
                    "stop": stop,
                    "start": start,
                    "message": (
                        f"config: {len(save.get('changed') or [])} keys · "
                        f"restart: {start.get('message') or stop.get('message')}"
                    ),
                }
            elif action == "apply_preset":
                pid = str((data or {}).get("preset") or (data or {}).get("id") or "").strip()
                result = apply_preset(pid)
            elif action == "apply_preset_restart":
                pid = str((data or {}).get("preset") or (data or {}).get("id") or "").strip()
                save = apply_preset(pid)
                engine = (
                    str((data or {}).get("engine") or os.environ.get("REGISTER_ENGINE") or "protocol")
                    .strip()
                    .lower()
                )
                target = (data or {}).get("target")
                if target in (None, ""):
                    target = os.environ.get("TARGET") or ""
                stop = _stop_register()
                args: list[str] = []
                if target not in (None, ""):
                    args = ["--target", str(target)]
                if engine == "go":
                    start = _spawn_go_register({"target": target} if target not in (None, "") else {})
                else:
                    start = _spawn_register(args, engine=engine)
                result = {
                    "ok": bool(save.get("ok")) and bool(start.get("ok")),
                    "save": save,
                    "stop": stop,
                    "start": start,
                    "message": (
                        f"{save.get('message') or save.get('label') or pid} · "
                        f"restart: {start.get('message') or stop.get('message')}"
                    ),
                }
            elif action == "import_env":
                text = str((data or {}).get("text") or "")
                replace = bool((data or {}).get("replace"))
                result = write_env_raw(text, merge=not replace)
                result["message"] = (
                    f"import {'replace' if replace else 'merge'}: "
                    f"{len(result.get('changed') or [])} keys"
                    + (
                        f", removed {len(result.get('removed') or [])}"
                        if result.get("removed")
                        else ""
                    )
                )
            elif action == "delete_env_keys":
                keys = (data or {}).get("keys") or []
                if isinstance(keys, str):
                    keys = [keys]
                result = delete_env_keys(list(keys))
                result["message"] = f"removed {len(result.get('removed') or [])} keys"
            elif action == "test_moemail":
                result = _test_moemail()
            elif action == "test_turnstile":
                result = _test_turnstile_api()
            else:
                result = {"ok": False, "message": f"unknown action: {action}"}
            _record_last_action(action, result)
        self._json(200, result)


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _auto_start_register_enabled() -> bool:
    """HF / Docker: start protocol register with the panel (no UI click needed)."""
    if "AUTO_START_REGISTER" in os.environ:
        return _env_truthy("AUTO_START_REGISTER", "0")
    # default on for Hugging Face Spaces / /data deployments
    if (os.environ.get("SPACE_ID") or "").strip():
        return True
    if (os.environ.get("KEY_EXPORT_DIR") or "").startswith("/data"):
        return True
    return False


def _auto_restart_register_enabled() -> bool:
    if "AUTO_RESTART_REGISTER" in os.environ:
        return _env_truthy("AUTO_RESTART_REGISTER", "0")
    # follow auto-start default
    return _auto_start_register_enabled()


_register_supervisor_stop = threading.Event()
_register_supervisor_thread: threading.Thread | None = None


def _build_auto_start_args() -> list[str]:
    args: list[str] = []
    target = (os.environ.get("TARGET") or os.environ.get("AUTO_START_TARGET") or "").strip()
    if target and target not in {"0", ""}:
        args.extend(["--target", target])
    return args


def _print_register_log_tail(*, title: str = "register log tail", max_lines: int = 40) -> None:
    """Mirror register file log into container stdout (HF Logs tab)."""
    try:
        from grok_register.run_log import (
            register_dashboard_log_path,
            register_fail_log_path,
            tail_text,
            recent_fail_summary,
        )

        path = register_dashboard_log_path()
        tail = tail_text(path, max_bytes=6000, max_lines=max_lines)
        print(f"[*] --- {title}: {path} ---", flush=True)
        if tail:
            for line in tail.splitlines():
                print(f"[register] {line}", flush=True)
        else:
            print("[register] (empty)", flush=True)
        fails = recent_fail_summary(limit=5)
        if fails:
            print("[*] --- recent fail events ---", flush=True)
            for f in fails:
                print(
                    f"[fail] {f.get('ts_iso','')} {f.get('kind','')}: {f.get('message','')[:300]}",
                    flush=True,
                )
            print(f"[*] fail log file: {register_fail_log_path()}", flush=True)
        print("[*] --- end log tail ---", flush=True)
    except Exception as exc:
        print(f"[!] could not print register log: {exc}", flush=True)


def _maybe_auto_start_register(*, reason: str = "boot") -> dict:
    """Spawn register once at panel boot (and for supervisor restarts)."""
    if process_alive():
        return {"ok": True, "message": "register already running", "skipped": True}
    engine = (os.environ.get("REGISTER_ENGINE") or "protocol").strip().lower()
    args = _build_auto_start_args()
    print(
        f"[*] AUTO_START_REGISTER ({reason}): engine={engine} args={args or '[]'}",
        flush=True,
    )
    try:
        if engine == "go":
            payload: dict = {}
            if args and "--target" in args:
                try:
                    payload["target"] = args[args.index("--target") + 1]
                except (ValueError, IndexError):
                    pass
            result = _spawn_go_register(payload)
        else:
            result = _spawn_register(args, engine=engine)
    except Exception as exc:
        result = {"ok": False, "message": f"auto-start failed: {exc}"}
    try:
        from grok_register.run_log import append_fail

        append_fail(
            "auto_start",
            result.get("message") or str(result),
            level="info" if result.get("ok") else "error",
            engine=engine,
            extra={"reason": reason, "ok": bool(result.get("ok"))},
        )
    except Exception:
        pass
    _record_last_action("auto_start", result if isinstance(result, dict) else {"ok": False})
    msg = (result or {}).get("message") if isinstance(result, dict) else str(result)
    print(f"[*] AUTO_START_REGISTER result: {msg}", flush=True)
    if not (result or {}).get("ok"):
        _print_register_log_tail(title="auto-start failed")
        return result if isinstance(result, dict) else {"ok": False, "message": str(result)}
    # re-check after a few seconds — many failures happen after first health delay
    if not _register_supervisor_stop.wait(4.0):
        if not process_alive():
            print("[!] register exited within ~4s after spawn — dumping log", flush=True)
            _print_register_log_tail(title="died shortly after start")
            try:
                from grok_register.run_log import append_fail

                append_fail(
                    "early_exit",
                    "register died within 4s of auto-start",
                    engine=engine,
                    level="error",
                )
            except Exception:
                pass
        else:
            print(f"[*] register still alive pid={read_pid()}", flush=True)
    return result if isinstance(result, dict) else {"ok": False, "message": str(result)}


def _register_supervisor_loop() -> None:
    """If register exits, dump log to container stdout and optionally respawn."""
    # initial delay so /api/health is up first
    delay = max(1.0, float(os.environ.get("AUTO_START_DELAY_SEC") or "3"))
    if _register_supervisor_stop.wait(delay):
        return
    if _auto_start_register_enabled():
        _maybe_auto_start_register(reason="boot")
    interval = max(5.0, float(os.environ.get("AUTO_RESTART_INTERVAL_SEC") or "15"))
    was_alive = process_alive()
    while not _register_supervisor_stop.is_set():
        if _register_supervisor_stop.wait(interval):
            break
        alive = process_alive()
        if was_alive and not alive:
            print("[!] register process gone — dumping log to container stdout", flush=True)
            _print_register_log_tail(title="register exited")
        elif alive and not was_alive:
            print(f"[*] register running again pid={read_pid()}", flush=True)
        was_alive = alive
        if not _auto_restart_register_enabled():
            continue
        if alive:
            continue
        # avoid thrashing if last exit was instant crash
        try:
            from grok_register.run_log import recent_fail_summary

            recent = recent_fail_summary(limit=1)
            if recent:
                last = recent[0]
                age = time.time() - float(last.get("ts") or 0)
                if last.get("kind") in {"early_exit", "crash"} and age < 8:
                    continue
        except Exception:
            pass
        _maybe_auto_start_register(reason="supervisor")


def start_register_supervisor() -> None:
    global _register_supervisor_thread
    if not _auto_start_register_enabled() and not _auto_restart_register_enabled():
        return
    if _register_supervisor_thread and _register_supervisor_thread.is_alive():
        return
    _register_supervisor_stop.clear()
    _register_supervisor_thread = threading.Thread(
        target=_register_supervisor_loop,
        name="register-supervisor",
        daemon=True,
    )
    _register_supervisor_thread.start()
    print(
        f"[*] register supervisor on "
        f"(auto_start={_auto_start_register_enabled()} "
        f"auto_restart={_auto_restart_register_enabled()})",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    from grok_register.polyglot import PolyglotError, print_stack_banner, require_polyglot_stack

    p = argparse.ArgumentParser(description="Web control plane for grok-free-register")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--no-auto-register",
        action="store_true",
        help="Disable AUTO_START_REGISTER even if env enables it",
    )
    args = p.parse_args(argv)
    # auto-start CLIProxyAPI import/refresh worker when enabled
    try:
        cpa_sync.ensure_worker_if_enabled()
    except Exception:
        pass
    try:
        require_polyglot_stack()
    except PolyglotError as exc:
        print(f"[✗] {exc}", file=sys.stderr)
        return 2
    print_stack_banner()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    public_url = public_dashboard_url(args.host, args.port)
    print(f"[*] dashboard {public_url}")
    if args.host in {"0.0.0.0", "::", "[::]"} or public_url.startswith("https://"):
        print(f"[*] listen {args.host}:{args.port}")
    print(f"[*] status file: {status_path()}")
    print(f"[*] actions: {'ENABLED' if ALLOW_ACTIONS else 'disabled (CONTROL_PLANE_ALLOW_ACTIONS=0)'}")
    if auth_required():
        modes = []
        if DASHBOARD_PASSWORD:
            modes.append(f"Basic user={DASHBOARD_USER!r}")
        if CONTROL_PLANE_TOKEN:
            modes.append("Bearer token")
        print(f"[*] auth: REQUIRED ({', '.join(modes)})")
    else:
        print(
            "[*] auth: OFF — set DASHBOARD_PASSWORD or CONTROL_PLANE_TOKEN "
            "to protect the panel (recommended on public / HF Space)"
        )
    if not args.no_auto_register:
        start_register_supervisor()
    else:
        print("[*] AUTO_START_REGISTER disabled via --no-auto-register", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] dashboard stopped")
        _register_supervisor_stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
