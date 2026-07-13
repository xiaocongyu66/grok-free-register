"""
Helpers to export runtime config for the Go register-worker and optionally launch it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_register_worker_bin() -> Path | None:
    raw = (os.environ.get("REGISTER_WORKER_BIN") or "").strip()
    cands = []
    if raw:
        cands.append(Path(raw).expanduser())
    cands.append(PROJECT_ROOT / "native" / "register-worker" / "register-worker")
    for p in cands:
        try:
            if p.is_file() and os.access(p, os.X_OK):
                return p
        except OSError:
            continue
    return None


def export_go_config(
    *,
    site_key: str,
    action_id: str,
    state_tree: str,
    path: Path | None = None,
) -> Path:
    cfg = {
        "workers": int(os.environ.get("GO_REGISTER_WORKERS") or "4"),
        "target": int(os.environ.get("TARGET") or "0"),
        "turnstile_api_url": (
            os.environ.get("TURNSTILE_API_URL") or "http://127.0.0.1:5080"
        ).rstrip("/"),
        "site_key": site_key,
        "action_id": action_id,
        "state_tree": state_tree,
        "email_mode": (os.environ.get("EMAIL_MODE") or "moemail").strip().lower(),
        "moemail_api": (os.environ.get("MOEMAIL_API") or "https://moemail.app").rstrip("/"),
        "moemail_api_key": os.environ.get("MOEMAIL_API_KEY") or "",
        "moemail_domain": os.environ.get("MOEMAIL_DOMAIN") or "",
        "email_api": (os.environ.get("EMAIL_API") or "http://127.0.0.1:8080").rstrip("/"),
        "email_domain": os.environ.get("EMAIL_DOMAIN") or "",
        "proxy": os.environ.get("REGISTER_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or "",
        "output_file": os.environ.get("GO_REGISTER_OUTPUT") or "keys/accounts.txt",
        "turnstile_timeout_sec": int(os.environ.get("TURNSTILE_API_TIMEOUT") or "120"),
    }
    out = path or (PROJECT_ROOT / "logs" / "go-register-config.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def maybe_run_go_register_from_python(site_key: str, action_id: str, state_tree: str) -> int | None:
    """Run protocol register when REGISTER_ENGINE=go|protocol|http.

    - protocol / http → Python HTTP path (Cloudflare-friendly TLS; default)
    - go → native register-worker (often blocked by CF JA3 on this host)

    Never writes accounts.cpa.json.
    """
    engine = (os.environ.get("REGISTER_ENGINE") or "python").strip().lower()
    if engine not in {"go", "protocol", "http"}:
        return None

    workers = str(os.environ.get("GO_REGISTER_WORKERS") or "8")
    target = str(os.environ.get("TARGET") or "0")
    if not (os.environ.get("TURNSTILE_API_URL") or "").strip():
        os.environ["TURNSTILE_API_URL"] = "http://127.0.0.1:5080"

    # Prefer Python protocol unless engine is explicitly "go"
    if engine in {"protocol", "http"}:
        try:
            from grok_register import job_store

            job_store.write_register_job(
                kind="register",
                running=True,
                engine="python-protocol",
                message=f"protocol workers={workers} target={target}",
                workers=int(workers) if workers.isdigit() else 0,
                started_at=__import__("time").time(),
            )
        except Exception:
            pass
        from grok_register.protocol_register import run_protocol_register

        print(
            f"[*] 协议注册 Python HTTP workers={workers} target={target} "
            f"(不生成 accounts.cpa.json)",
            flush=True,
        )
        code = run_protocol_register(
            site_key=site_key,
            action_id=action_id,
            state_tree=state_tree,
            workers=int(workers) if workers.isdigit() else 8,
            target=int(target) if target.isdigit() else 0,
        )
        try:
            from grok_register import job_store
            import time as _t

            # Keep pid from dashboard spawn so UI can still show last process
            job_store.write_register_job(
                running=False,
                finished_at=_t.time(),
                message=f"python-protocol exit={code}",
                success=0,
            )
        except Exception:
            pass
        return int(code)

    binary = resolve_register_worker_bin()
    if binary is None:
        print(
            "[!] REGISTER_ENGINE=go but register-worker not found; "
            "run bash scripts/build-native.sh — falling back to Python protocol",
            file=sys.stderr,
        )
        from grok_register.protocol_register import run_protocol_register

        return run_protocol_register(
            site_key=site_key,
            action_id=action_id,
            state_tree=state_tree,
        )
    cfg_path = export_go_config(site_key=site_key, action_id=action_id, state_tree=state_tree)
    cmd = [str(binary), "run", "--config", str(cfg_path), "--workers", workers]
    if target and target != "0":
        cmd.extend(["--target", target])
    print(f"[*] 协议注册 Go worker: {' '.join(cmd)}")
    try:
        from grok_register import job_store

        job_store.write_register_job(
            kind="register",
            running=True,
            engine="go-protocol",
            message=f"go workers={workers} target={target}",
            workers=int(workers) if workers.isdigit() else 0,
            started_at=__import__("time").time(),
        )
    except Exception:
        pass
    code = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    try:
        from grok_register import job_store
        import time as _t

        job_store.write_register_job(
            running=False,
            finished_at=_t.time(),
            message=f"go-protocol exit={code}",
        )
    except Exception:
        pass
    return code
