"""
Unified account inventory for control plane.

Sources:
  - keys/accounts.txt          legacy email:password:sso
  - keys/sub2api/*.sub2api.json OAuth sub2api exports
  - keys/cpa/xai-*.json         CPA single-account files only (no merge bundle)
  - keys/xai-enroller-ledger.db optional OAuth job / inventory state
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def key_export_dir() -> Path:
    raw = (os.environ.get("KEY_EXPORT_DIR") or "keys").strip() or "keys"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class AccountRecord:
    id: str
    email: str
    status: str  # legacy_sso | oauth_ready | oauth_pending | unknown
    formats: list[str]  # legacy, sub2api, cpa
    has_sso: bool = False
    has_access_token: bool = False
    has_refresh_token: bool = False
    subject: str = ""
    fingerprint: str = ""  # cpa/sub2api file stem e.g. xai-xxx
    created_at: str = ""
    updated_at: str = ""
    paths: dict[str, str] | None = None
    note: str = ""
    ledger_state: str = ""  # available|claimed|...
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["paths"] = self.paths or {}
        return d


def _mtime_iso(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_ledger_maps(root: Path) -> tuple[dict[str, str], dict[str, dict]]:
    """fingerprint -> inventory state; fingerprint -> latest job."""
    db = root / "xai-enroller-ledger.db"
    inv: dict[str, str] = {}
    jobs: dict[str, dict] = {}
    if not db.is_file():
        return inv, jobs
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute(
                "SELECT sink_receipt_fingerprint, state, claimed_at, batch_id, note "
                "FROM credential_inventory"
            ):
                inv[str(row["sink_receipt_fingerprint"])] = str(row["state"] or "")
        except sqlite3.Error:
            pass
        try:
            for row in con.execute(
                "SELECT sink_receipt_fingerprint, status, finished_at, reason_code, job_id "
                "FROM jobs WHERE sink_receipt_fingerprint IS NOT NULL "
                "ORDER BY job_id DESC"
            ):
                fp = str(row["sink_receipt_fingerprint"] or "")
                if fp and fp not in jobs:
                    jobs[fp] = {
                        "status": row["status"],
                        "finished_at": row["finished_at"],
                        "reason_code": row["reason_code"],
                        "job_id": row["job_id"],
                    }
        except sqlite3.Error:
            pass
        con.close()
    except Exception:
        pass
    return inv, jobs


def scan_accounts(root: Path | None = None) -> list[AccountRecord]:
    root = root or key_export_dir()
    by_email: dict[str, AccountRecord] = {}
    inv_map, job_map = _load_ledger_maps(root)

    # accounts.txt — email:password (or legacy email:password:sso)
    accounts_txt = root / "accounts.txt"
    if accounts_txt.is_file():
        try:
            for line in accounts_txt.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                email = parts[0].strip()
                sso = parts[2].strip() if len(parts) >= 3 else ""
                rec = by_email.get(email) or AccountRecord(
                    id=email,
                    email=email,
                    status="legacy_sso" if sso else "unknown",
                    formats=[],
                )
                if "legacy" not in rec.formats:
                    rec.formats.append("legacy")
                rec.has_sso = rec.has_sso or bool(sso)
                rec.paths = rec.paths or {}
                rec.paths["legacy"] = str(accounts_txt)
                rec.updated_at = rec.updated_at or _mtime_iso(accounts_txt)
                rec.source = rec.source or "accounts.txt"
                if sso and rec.status == "unknown":
                    rec.status = "legacy_sso"
                by_email[email] = rec
        except OSError:
            pass

    # keys/sso.txt — canonical email:sso
    sso_txt = root / "sso.txt"
    if sso_txt.is_file():
        try:
            for line in sso_txt.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                email, sso = line.split(":", 1)
                email, sso = email.strip(), sso.strip()
                if not email or "@" not in email or not sso:
                    continue
                rec = by_email.get(email) or AccountRecord(
                    id=email,
                    email=email,
                    status="oauth_pending",
                    formats=[],
                )
                if "sso" not in rec.formats:
                    rec.formats.append("sso")
                rec.has_sso = True
                rec.paths = rec.paths or {}
                rec.paths["sso"] = str(sso_txt)
                rec.updated_at = max(rec.updated_at or "", _mtime_iso(sso_txt))
                if rec.source in {"", "accounts.txt"}:
                    rec.source = "sso.txt"
                if rec.status in {"unknown", "legacy_sso", ""}:
                    rec.status = "oauth_pending"
                by_email[email] = rec
        except OSError:
            pass

    # sub2api
    sub_dir = root / "sub2api"
    if sub_dir.is_dir():
        for path in sorted(sub_dir.glob("*.sub2api.json")):
            if path.name == "accounts.sub2api.json":
                continue
            doc = _read_json(path)
            if not isinstance(doc, dict):
                continue
            for item in doc.get("accounts") or []:
                if not isinstance(item, dict):
                    continue
                creds = item.get("credentials") or {}
                email = str(
                    creds.get("email")
                    or (item.get("extra") or {}).get("email")
                    or item.get("name")
                    or ""
                ).strip()
                if not email:
                    continue
                fp = path.name.removesuffix(".sub2api.json")
                rec = by_email.get(email) or AccountRecord(
                    id=email, email=email, status="oauth_ready", formats=[]
                )
                if "sub2api" not in rec.formats:
                    rec.formats.append("sub2api")
                rec.has_access_token = rec.has_access_token or bool(creds.get("access_token"))
                rec.has_refresh_token = rec.has_refresh_token or bool(creds.get("refresh_token"))
                rec.subject = rec.subject or str(
                    (item.get("extra") or {}).get("subject") or creds.get("sub") or ""
                )
                rec.fingerprint = rec.fingerprint or fp
                rec.paths = rec.paths or {}
                rec.paths["sub2api"] = str(path)
                rec.updated_at = max(rec.updated_at or "", _mtime_iso(path))
                rec.created_at = rec.created_at or str(doc.get("exported_at") or "")
                rec.status = "oauth_ready"
                if fp in inv_map:
                    rec.ledger_state = inv_map[fp]
                by_email[email] = rec

    # cpa singles only (skip any leftover merge filenames)
    cpa_dir = root / "cpa"
    if cpa_dir.is_dir():
        _purge_cpa_merge_files(cpa_dir)
        for path in sorted(cpa_dir.glob("xai-*.json")):
            doc = _read_json(path)
            if not isinstance(doc, dict):
                continue
            email = str(doc.get("email") or "").strip()
            if not email:
                # try name
                email = str(doc.get("name") or path.stem).strip()
            fp = path.stem
            rec = by_email.get(email) or AccountRecord(
                id=email, email=email, status="oauth_ready", formats=[]
            )
            if "cpa" not in rec.formats:
                rec.formats.append("cpa")
            rec.has_access_token = rec.has_access_token or bool(doc.get("access_token"))
            rec.has_refresh_token = rec.has_refresh_token or bool(doc.get("refresh_token"))
            rec.subject = rec.subject or str(doc.get("sub") or "")
            rec.fingerprint = rec.fingerprint or fp
            rec.paths = rec.paths or {}
            rec.paths["cpa"] = str(path)
            rec.updated_at = max(rec.updated_at or "", _mtime_iso(path))
            if rec.status != "oauth_ready":
                rec.status = "oauth_ready" if rec.has_refresh_token or rec.has_access_token else rec.status
            if fp in inv_map:
                rec.ledger_state = inv_map[fp]
            by_email[email] = rec

    # derive oauth_pending: has SSO (sso.txt or legacy) but no OAuth product
    for rec in by_email.values():
        if rec.has_sso and not (rec.has_access_token or rec.has_refresh_token):
            if "sub2api" not in rec.formats and "cpa" not in rec.formats:
                rec.status = "oauth_pending"
        if not rec.formats:
            rec.formats = ["unknown"]
        rec.formats = sorted(set(rec.formats))

    # sort: oauth_ready first, then by updated_at desc
    order = {"oauth_ready": 0, "oauth_pending": 1, "legacy_sso": 2, "unknown": 3}
    records = list(by_email.values())
    records.sort(key=lambda r: (order.get(r.status, 9), -( _ts(r.updated_at)), r.email))
    return records


def _ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def inventory_summary(records: Iterable[AccountRecord] | None = None) -> dict[str, Any]:
    records = list(records if records is not None else scan_accounts())
    by_status: dict[str, int] = {}
    by_format: dict[str, int] = {}
    for r in records:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        for f in r.formats:
            by_format[f] = by_format.get(f, 0) + 1
    root = key_export_dir()
    cpa_dir = root / "cpa"
    _purge_cpa_merge_files(cpa_dir)
    singles = list(cpa_dir.glob("xai-*.json")) if cpa_dir.is_dir() else []
    artifacts = {
        "legacy_accounts_txt": (root / "accounts.txt").is_file(),
        "sub2api_bundle": (root / "sub2api" / "accounts.sub2api.json").is_file(),
        "cpa_singles": len(singles),
        "cpa_bundle_json": False,
        "cpa_bundle_zip": False,
    }
    return {
        "total": len(records),
        "by_status": by_status,
        "by_format": by_format,
        "artifacts": artifacts,
        "export_dir": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def rebuild_sub2api_bundle(root: Path | None = None) -> Path:
    root = root or key_export_dir()
    directory = root / "sub2api"
    directory.mkdir(parents=True, exist_ok=True)
    accounts = []
    seen = set()
    for path in sorted(directory.glob("*.sub2api.json")):
        if path.name == "accounts.sub2api.json":
            continue
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        for item in doc.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            creds = item.get("credentials") or {}
            key = (
                item.get("platform"),
                creds.get("refresh_token"),
                creds.get("access_token"),
                item.get("name"),
            )
            if key in seen:
                continue
            seen.add(key)
            accounts.append(item)
    out = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": accounts,
    }
    target = directory / "accounts.sub2api.json"
    _atomic_json(target, out)
    return target


def _purge_cpa_merge_files(directory: Path | None) -> list[str]:
    """Permanently remove accounts.cpa.json / accounts.cpa.zip if present."""
    if directory is None or not directory.is_dir():
        return []
    removed: list[str] = []
    for name in ("accounts.cpa.json", "accounts.cpa.zip"):
        path = directory / name
        try:
            if path.is_file():
                path.unlink()
                removed.append(name)
        except OSError:
            pass
    return removed


def rebuild_cpa_bundle(root: Path | None = None) -> tuple[Path, Path]:
    """Deprecated no-op: only purges merge bundles. Returns placeholder paths."""
    root = root or key_export_dir()
    directory = root / "cpa"
    directory.mkdir(parents=True, exist_ok=True)
    _purge_cpa_merge_files(directory)
    return directory / "accounts.cpa.json", directory / "accounts.cpa.zip"


def ensure_bundles(*, rebuild: bool = False) -> dict[str, str]:
    """Rebuild product exports; CPA is singles-only (never accounts.cpa.json)."""
    root = key_export_dir()
    _purge_cpa_merge_files(root / "cpa")
    engine = (os.environ.get("INVENTORY_ENGINE") or "go").strip().lower()
    prefer_native = engine not in {"python", "py"}
    if prefer_native and (rebuild or not (root / "sub2api" / "accounts.sub2api.json").is_file()):
        try:
            from grok_register.polyglot import go_inventory_worker_bin, inventory_rebuild_bundles

            if go_inventory_worker_bin() is not None:
                paths = inventory_rebuild_bundles(root)
                out = dict(paths.get("paths") or paths)
                # Drop any merge bundle paths native may still report
                out.pop("cpa_json", None)
                out.pop("cpa_zip", None)
                if "sub2api_json" not in out and (root / "sub2api" / "accounts.sub2api.json").is_file():
                    out["sub2api_json"] = str(root / "sub2api" / "accounts.sub2api.json")
                legacy = root / "accounts.txt"
                if legacy.is_file():
                    out.setdefault("legacy_txt", str(legacy))
                singles = sorted((root / "cpa").glob("xai-*.json")) if (root / "cpa").is_dir() else []
                if singles:
                    out["cpa_dir"] = str(root / "cpa")
                    out["cpa_singles"] = str(len(singles))
                if out.get("sub2api_json"):
                    return {k: str(v) for k, v in out.items() if isinstance(v, (str, Path, int))}
        except Exception:
            pass

    out: dict[str, str] = {}
    sub = root / "sub2api" / "accounts.sub2api.json"
    if rebuild or not sub.is_file():
        sub = rebuild_sub2api_bundle(root)
    out["sub2api_json"] = str(sub)
    rebuild_cpa_bundle(root)  # purge only
    singles = sorted((root / "cpa").glob("xai-*.json")) if (root / "cpa").is_dir() else []
    if singles:
        out["cpa_dir"] = str(root / "cpa")
        out["cpa_singles"] = str(len(singles))
    legacy = root / "accounts.txt"
    if legacy.is_file():
        out["legacy_txt"] = str(legacy)
    return out


# Runtime account files needed to resume registration / re-export OAuth later.
# These live under KEY_EXPORT_DIR (keys/ or /data/keys on HF).
RECOVERY_FILE_SPECS: list[dict[str, str]] = [
    {
        "id": "accounts_txt",
        "name": "accounts.txt",
        "media": "text/plain; charset=utf-8",
        "aliases": "legacy,accounts,txt,accounts_txt,accounts.txt",
        "desc": "email:password 账密（重登用）",
    },
    {
        "id": "sso_txt",
        "name": "sso.txt",
        "media": "text/plain; charset=utf-8",
        "aliases": "sso,sso_txt,sso.txt,sso_file",
        "desc": "规范 SSO：email:sso（convert 源）",
    },
    {
        "id": "auth_sessions",
        "name": "auth-sessions.jsonl",
        "media": "application/x-ndjson; charset=utf-8",
        "aliases": "auth_sessions,sessions,auth-sessions,auth-sessions.jsonl,jsonl",
        "desc": "SSO cookie / 会话备份",
    },
    {
        "id": "browser_fingerprints",
        "name": "browser-fingerprints.json",
        "media": "application/json",
        "aliases": "browser_fingerprints,fingerprints,browser-fingerprints,browser-fingerprints.json,fp",
        "desc": "浏览器指纹绑定",
    },
    {
        "id": "grok_txt",
        "name": "grok.txt",
        "media": "text/plain; charset=utf-8",
        "aliases": "grok,grok_txt,grok.txt",
        "desc": "纯 SSO token 列表（由 sso.txt 生成）",
    },
    {
        "id": "protocol_log",
        "name": "accounts.protocol.log",
        "media": "text/plain; charset=utf-8",
        "aliases": "protocol_log,protocol,accounts.protocol.log,protocol.log",
        "desc": "协议注册成功日志",
    },
]


def _recovery_alias_map() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for spec in RECOVERY_FILE_SPECS:
        for raw in (spec.get("aliases") or "").split(","):
            key = raw.strip().lower()
            if key:
                out[key] = spec
        out[spec["id"].lower()] = spec
        out[spec["name"].lower()] = spec
    return out


def list_recovery_files(root: Path | None = None) -> list[dict[str, Any]]:
    """Presence / size of recovery sources for dashboard + API."""
    root = root or key_export_dir()
    items: list[dict[str, Any]] = []
    for spec in RECOVERY_FILE_SPECS:
        path = root / spec["name"]
        size = 0
        mtime = ""
        exists = path.is_file()
        if exists:
            try:
                st = path.stat()
                size = int(st.st_size)
                mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
            except OSError:
                exists = False
        items.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "desc": spec.get("desc") or "",
                "exists": exists,
                "size": size,
                "updated_at": mtime,
                "download": f"/api/download?format={spec['id']}",
            }
        )
    return items


def pack_recovery_zip(root: Path | None = None) -> Path:
    """Zip accounts.txt + sessions + fingerprints + grok + protocol log for backup."""
    root = root or key_export_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    zip_path = root / f"account-recovery-{stamp}.zip"
    # stable name for latest download link + keep stamped copy when possible
    latest = root / "account-recovery.zip"
    written = 0
    tmp = latest.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for spec in RECOVERY_FILE_SPECS:
            path = root / spec["name"]
            if not path.is_file():
                continue
            try:
                zf.write(path, arcname=spec["name"])
                written += 1
            except OSError:
                continue
        # tiny manifest for restore tooling
        manifest = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "files": [s["name"] for s in RECOVERY_FILE_SPECS if (root / s["name"]).is_file()],
            "note": "Unpack into KEY_EXPORT_DIR (keys/ or /data/keys) to resume",
        }
        zf.writestr(
            "recovery-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
    if written == 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("no recovery files found under keys/ (accounts.txt / sessions / …)")
    tmp.replace(latest)
    # best-effort stamped archive
    try:
        import shutil

        shutil.copy2(latest, zip_path)
    except OSError:
        zip_path = latest
    return latest


def download_spec(fmt: str) -> tuple[Path, str, str]:
    """Return (path, media_type, download_filename) for a finished product format."""
    fmt = (fmt or "").strip().lower()
    # recovery / runtime sources (no bundle rebuild)
    if fmt in {
        "recovery",
        "recovery_zip",
        "backup",
        "backup_zip",
        "account_recovery",
        "resume",
        "resume_zip",
    }:
        p = pack_recovery_zip()
        return p, "application/zip", "account-recovery.zip"
    # register / solver logs (panel download)
    if fmt in {
        "register_log",
        "register_dashboard_log",
        "dashboard_log",
        "run_log",
        "register-dashboard.log",
    }:
        from grok_register.run_log import register_dashboard_log_path

        p = register_dashboard_log_path()
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# no register log yet — start register once\n", encoding="utf-8")
        return p, "text/plain; charset=utf-8", "register-dashboard.log"
    if fmt in {
        "register_fail",
        "fail_log",
        "register_fail_jsonl",
        "register-fail.jsonl",
        "fails",
    }:
        from grok_register.run_log import register_fail_log_path

        p = register_fail_log_path()
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
        return p, "application/x-ndjson; charset=utf-8", "register-fail.jsonl"
    if fmt in {"register_live", "live_log", "register-live.log"}:
        from grok_register.run_log import register_live_log_path

        p = register_live_log_path()
        if not p.is_file():
            raise ValueError("register-live.log not found yet")
        return p, "text/plain; charset=utf-8", "register-live.log"
    if fmt in {
        "register_logs_zip",
        "logs_zip",
        "run_logs",
        "run_logs_zip",
        "debug_logs",
    }:
        from grok_register.run_log import pack_run_logs_zip

        p = pack_run_logs_zip()
        return p, "application/zip", "register-logs.zip"
    rec = _recovery_alias_map().get(fmt)
    if rec is not None:
        p = key_export_dir() / rec["name"]
        if not p.is_file():
            raise ValueError(f"missing {rec['name']} under {key_export_dir()}")
        return p, rec["media"], rec["name"]

    paths = ensure_bundles(rebuild=False)
    if fmt in {"sub2api", "sub2api_json", "sub"}:
        p = Path(paths["sub2api_json"])
        if not p.is_file() or p.stat().st_size < 10:
            p = rebuild_sub2api_bundle()
        return p, "application/json", "accounts.sub2api.json"
    if fmt in {"cpa", "cpa_json", "cpa_zip", "cpa-zip", "cpa_singles"}:
        # Only zip of xai-*.json singles — never accounts.cpa.json
        root = key_export_dir()
        directory = root / "cpa"
        _purge_cpa_merge_files(directory)
        singles = sorted(directory.glob("xai-*.json")) if directory.is_dir() else []
        if not singles:
            raise ValueError("no xai-*.json under keys/cpa/")
        zip_path = directory / "xai-singles.zip"
        tmp = zip_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in singles:
                try:
                    zf.write(path, arcname=path.name)
                except OSError:
                    continue
        tmp.replace(zip_path)
        return zip_path, "application/zip", "xai-singles.zip"
    if fmt in {"legacy", "accounts", "txt"}:
        # kept for back-compat; same as accounts_txt recovery file
        p = key_export_dir() / "accounts.txt"
        return p, "text/plain; charset=utf-8", "accounts.txt"
    raise ValueError(f"unknown format: {fmt}")


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
