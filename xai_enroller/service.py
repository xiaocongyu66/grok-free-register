"""Interactive composition for the asynchronous local authentication service."""

import asyncio
import os
import secrets
import shlex
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .models import SourceRecord
from .inventory import InventoryError
from .remote_stream import parse_session_document


DEFAULT_LOCAL_AUTH_DIR = Path.home() / "Downloads" / "grok-free-register-auth"
AUTHENTICATED_DIRNAME = "authenticated"
CLAIMED_DIRNAME = "claimed"


class AuthServiceConfigurationError(ValueError):
    pass


def resolve_auth_log_mode(argv=None, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    unknown = [argument for argument in argv if argument != "--debug"]
    if unknown:
        raise AuthServiceConfigurationError(
            f"unsupported auth service argument: {unknown[0]}"
        )
    mode = (env.get("XAI_AUTH_SERVICE_LOG_MODE") or "user").strip().lower()
    if "--debug" in argv:
        mode = "debug"
    if mode not in {"user", "debug"}:
        raise AuthServiceConfigurationError(
            "XAI_AUTH_SERVICE_LOG_MODE must be user or debug"
        )
    return mode


def prepare_local_service_environment(env=None):
    """Create private local persistence defaults without storing secrets in the repo."""
    merged = dict(os.environ if env is None else env)
    destination = Path(
        merged.get("XAI_ENROLLER_LOCAL_AUTH_DIR", DEFAULT_LOCAL_AUTH_DIR)
    ).expanduser()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    merged["XAI_ENROLLER_SINK"] = "local"
    merged["XAI_ENROLLER_LOCAL_AUTH_DIR"] = str(destination)
    merged.setdefault(
        "XAI_ENROLLER_LEDGER_PATH", str(destination / "enrollment-ledger.db")
    )
    if not merged.get("XAI_ENROLLER_SOURCE_SALT"):
        salt_file = destination / ".ledger-salt"
        try:
            salt = salt_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            salt = secrets.token_hex(32)
            fd, temporary_name = tempfile.mkstemp(
                prefix=".ledger-salt.", suffix=".tmp", dir=destination, text=True
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(salt + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, salt_file)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        os.chmod(salt_file, 0o600)
        merged["XAI_ENROLLER_SOURCE_SALT"] = salt
    return merged


@dataclass(frozen=True)
class AuthServiceSettings:
    ssh_host: str
    remote_root: str
    identity_file: str | None
    sync_seconds: int = 30
    retry_seconds: int = 60
    min_authorization_interval_seconds: float = 10.0

    @classmethod
    def from_environ(cls, env=None):
        env = dict(os.environ if env is None else env)
        ssh_host = (env.get("XAI_AUTH_SERVICE_SSH_HOST") or "").strip()
        if not ssh_host:
            raise AuthServiceConfigurationError(
                "XAI_AUTH_SERVICE_SSH_HOST is required"
            )
        remote_root = (
            env.get("XAI_AUTH_SERVICE_REMOTE_ROOT") or "/opt/grok-free-register"
        ).strip()
        identity_file = (env.get("XAI_AUTH_SERVICE_SSH_IDENTITY") or "").strip() or None
        try:
            sync_seconds = int(env.get("XAI_AUTH_SERVICE_SYNC_SEC", "30"))
            retry_seconds = int(env.get("XAI_AUTH_SERVICE_RETRY_SEC", "60"))
            min_authorization_interval_seconds = float(
                env.get("XAI_AUTH_SERVICE_MIN_INTERVAL_SEC", "10")
            )
        except ValueError as exc:
            raise AuthServiceConfigurationError(
                "auth service intervals must be numeric"
            ) from exc
        if not 5 <= sync_seconds <= 3600:
            raise AuthServiceConfigurationError(
                "XAI_AUTH_SERVICE_SYNC_SEC must be between 5 and 3600"
            )
        if not 30 <= retry_seconds <= 86400:
            raise AuthServiceConfigurationError(
                "XAI_AUTH_SERVICE_RETRY_SEC must be between 30 and 86400"
            )
        if not 0 <= min_authorization_interval_seconds <= 3600:
            raise AuthServiceConfigurationError(
                "XAI_AUTH_SERVICE_MIN_INTERVAL_SEC must be between 0 and 3600"
            )
        return cls(
            ssh_host,
            remote_root,
            identity_file,
            sync_seconds,
            retry_seconds,
            min_authorization_interval_seconds,
        )


def parse_registered_accounts(lines):
    """Parse legacy ``source:discarded-password:sso`` records."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            source_id, _discarded_password, sso_token = raw.rsplit(":", 2)
        except ValueError as exc:
            raise ValueError(f"invalid registered account line {line_number}") from exc
        if not source_id or not sso_token or source_id in seen:
            continue
        seen.add(source_id)
        yield SourceRecord(source_id, sso_token)


def parse_exported_records(lines):
    """Parse the historical tab-delimited redacted exporter format."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            source_id, sso_token = raw.split("\t", 1)
        except ValueError as exc:
            raise ValueError(f"invalid remote export line {line_number}") from exc
        if not source_id or not sso_token or source_id in seen:
            continue
        seen.add(source_id)
        yield SourceRecord(source_id, sso_token)


def parse_exported_sessions(lines):
    """Parse exact redacted session documents while preserving Cookie scope."""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            record = parse_session_document(raw)
        except ValueError as exc:
            raise ValueError(f"invalid registered session line {line_number}") from exc
        if record.source_id in seen:
            continue
        seen.add(record.source_id)
        yield record


class SSHRegisteredSource:
    """Compatibility facade for the former one-shot account exporter."""

    def __init__(
        self,
        host,
        *,
        remote_root="/opt/grok-free-register",
        identity_file=None,
        process_factory=asyncio.create_subprocess_exec,
    ):
        self.host = host
        self.remote_root = remote_root
        self.identity_file = identity_file
        self.process_factory = process_factory

    async def fetch(self):
        command = (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 scripts/export_registered_sso.py keys/accounts.txt"
        )
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.identity_file:
            args.extend(["-i", self.identity_file])
        args.extend([self.host, command])
        process = await self.process_factory(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("registered account sync failed")
        return list(parse_exported_records(stdout.decode("utf-8").splitlines()))


class AuthService:
    """Compatibility facade for the former polling enrollment service."""

    def __init__(self, source, enroller, emit):
        self.source = source
        self.enroller = enroller
        self.emit = emit

    async def run_cycle(self):
        records = await self.source.fetch()
        fresh = [
            record
            for record in records
            if not self.enroller.ledger.has_imported(record.source_id)
        ]
        if not fresh:
            return []
        self.emit(("sync", {"new": len(fresh)}))
        results = await self.enroller.run_records(fresh)
        for result in results:
            self.emit(
                (
                    "result",
                    {
                        "source_id": result.source_id,
                        "status": result.status.value,
                        "reason": result.reason_code,
                    },
                )
            )
        return results


class EventTerminal:
    """Mode-aware aggregate renderer; identifiers and credentials stay hidden."""

    _SAFE_REASONS = {
        "already_imported",
        "authorization_transport_failed",
        "browser_error",
        "challenge_required",
        "confirmation_timeout",
        "device_flow_failed",
        "device_flow_invalid",
        "device_flow_refresh_failed",
        "imported",
        "internal_error",
        "oauth_denied",
        "oauth_expired",
        "oauth_rejected",
        "operator_cancelled",
        "rate_limited",
        "sink_failed",
        "sink_unconfigured",
        "snapshot_sync_failed",
        "token_failed",
        "token_transport_failed",
        "unknown_page",
    }
    _TRANSIENT_REASONS = {
        "authorization_transport_failed",
        "browser_error",
        "challenge_required",
        "confirmation_timeout",
        "device_flow_failed",
        "device_flow_refresh_failed",
        "sink_failed",
        "token_failed",
        "token_transport_failed",
        "unknown_page",
    }

    def __init__(self, mode="user", output=None):
        if mode not in {"user", "debug"}:
            raise ValueError("terminal mode must be user or debug")
        self.mode = mode
        self.output = output or self._print

    @staticmethod
    def _print(message):
        print(message, flush=True)

    @staticmethod
    def _percentage(value):
        return f"{100.0 * float(value):.1f}%"

    @staticmethod
    def _rate(value):
        return "—" if value is None else f"{float(value):.2f}/分"

    @staticmethod
    def _pending(value):
        return "—" if value is None else str(value)

    def _format_user(self, kind, data):
        if kind == "startup":
            return (
                "[✓] 本地认证服务已启动 | 来源 等待同步 | "
                f"输出 {data['destination']} | 待处理 — | 可用 {data['available']}"
            )
        if kind == "service_started":
            return "[▶] 认证流水线运行中"
        if kind == "service_stopped":
            return "[■] 认证服务已停止"
        if kind == "source_connected":
            return "[✓] 账号来源已连接 | 本地快照可用"
        if kind == "source_updated":
            return f"[↻] 发现新账号 {data['new']} | 快照共 {data['total']}"
        if kind == "source_disconnected":
            return "[!] 账号来源暂时断开 | 继续使用上一份有效快照"
        if kind == "source_record_rejected":
            return "[!] 已忽略一条无效来源记录"
        if kind == "rate_limited":
            return f"[⏸] 触发限流 | {data['wait_seconds']}秒后单次探测"
        if kind == "rate_limit_cleared":
            return f"[▶] 限流解除 | 实际等待 {data['elapsed_seconds']}秒"
        if kind == "authorization_started":
            return (
                f"[→] 开始认证 #{data['task_number']} | "
                f"待处理 {self._pending(data.get('pending_total'))}"
            )
        if kind == "result":
            if data.get("status") == "imported":
                return (
                    f"[✓] 认证成功 #{data.get('task_number', '—')} | "
                    f"近5分钟 {self._rate(data.get('five_minute_imports_per_minute'))} | "
                    f"累计 {data['imported_unique']} | 可用 {data.get('available', '—')}"
                )
            reason = data.get("reason")
            if reason == "rate_limited":
                return None
            action = "暂时失败，将自动重试" if reason in self._TRANSIENT_REASONS else "已跳过"
            return f"[✗] 认证未完成 #{data.get('task_number', '—')} | {action}"
        if kind == "control":
            states = {
                "paused": "[⏸] 认证服务已暂停",
                "running": "[▶] 认证服务已恢复",
                "cancelling": "[■] 正在取消当前任务",
                "idle": "[•] 当前没有可取消的任务",
                "stopping": "[■] 正在安全退出",
            }
            return states.get(data.get("state"), "[•] 控制命令已处理")
        if kind == "status":
            state = {"running": "运行中", "paused": "已暂停", "stopping": "停止中"}.get(
                data.get("state"), "未知"
            )
            cooldown = (
                f"限流冷却 {data.get('cooldown_remaining_seconds', 0):.0f}秒"
                if data.get("cooldown")
                else "不限流"
            )
            return (
                f"[•] 状态 {state} | 待处理 {self._pending(data.get('pending_total'))} | "
                f"阶段 {data.get('active_stage', 'idle')} | "
                f"近5分钟 {self._rate(data.get('five_minute_imports_per_minute'))} | "
                f"累计 {data.get('imported_unique', 0)} | "
                f"可用 {data.get('available', 0)} | 已取用 {data.get('claimed', 0)} | {cooldown}"
            )
        if kind == "inventory_taken":
            return (
                f"[✓] 已取用 {data['moved']} 个凭据 | "
                f"可用 {data['available']} | 批次 {data['batch_id']}"
            )
        if kind == "inventory_error":
            return (
                f"[!] 凭据取用失败 | 可用 {data['available']} | "
                f"处理中 {data['claiming']} | 已取用 {data['claimed']}"
            )
        if kind == "pipeline_error":
            return f"[!] 认证流水线异常 | 阶段 {data.get('stage', 'unknown')}"
        return None

    def _format_debug(self, kind, data):
        if kind == "service_started":
            return (
                "• service: running; "
                f"min_interval={data['min_authorization_interval_seconds']:.1f}s"
            )
        if kind == "service_stopped":
            return "• service: stopped"
        if kind == "source_connected":
            return "• source: local snapshot updated"
        if kind == "source_updated":
            return f"• source: new={data['new']}; total={data['total']}"
        if kind == "source_disconnected":
            return "⚠ source: snapshot_sync_failed; keeping previous snapshot"
        if kind == "source_record_rejected":
            return "⚠ source: invalid record rejected"
        if kind == "rate_limited":
            return (
                "⏸ authentication rate limited; "
                f"next single probe in {data['wait_seconds']}s"
            )
        if kind == "rate_limit_cleared":
            return (
                "▶ authentication rate limit cleared after "
                f"{data['elapsed_seconds']}s"
            )
        if kind == "authorization_started":
            return (
                "→ authentication: next task started; "
                f"task={data['task_number']}; attempt={data['attempt_number']}; "
                f"queued={data['source_queue']}; "
                f"pending={self._pending(data.get('pending_total'))}"
            )
        if kind == "result":
            if data.get("status") == "imported":
                return (
                    "✓ authentication: imported; "
                    f"task={data.get('task_number')}; total={data['imported_unique']}; "
                    f"5m_rate={self._rate(data.get('five_minute_imports_per_minute'))}; "
                    f"attempt_success={self._percentage(data['attempt_success'])}; "
                    f"eventual_success={self._percentage(data['eventual_success'])}"
                )
            reason = data.get("reason")
            safe_reason = reason if reason in self._SAFE_REASONS else "unknown"
            return (
                f"⚠ authentication: {data.get('status', 'unknown')} ({safe_reason}); "
                f"task={data.get('task_number')}; attempt={data.get('attempt_number')}"
            )
        if kind == "control":
            state = data.get("state", "unknown")
            safe_state = state if str(state).replace(" ", "").isalnum() else "unknown"
            return f"• service: {safe_state}"
        if kind == "status":
            rate = data.get("five_minute_imports_per_minute")
            rate_text = "unknown" if rate is None else f"{rate:.2f}/min"
            return (
                f"• status: {data['state']}; "
                f"queues={data['source_queue']}/{data['prepared_queue']}/"
                f"{data['completion_queue']}; active={data['active_stage']}; "
                f"retry_waiting={data['retry_waiting']}; "
                f"next_retry={data['next_retry_seconds']:.1f}s; "
                f"started={data['authorization_starts']}; "
                f"cooldown={str(data['cooldown']).lower()}; "
                f"cooldown_remaining={data['cooldown_remaining_seconds']:.1f}s; "
                f"probe={str(data['probe_in_flight']).lower()}; "
                f"min_interval={data['min_authorization_interval_seconds']:.1f}s; "
                f"pace_remaining={data['pacing_remaining_seconds']:.1f}s; "
                f"imported={data['imported_unique']}; attempted={data['attempted_unique']}; "
                f"rate_limited={data['rate_limited']}; 5m_rate={rate_text}; "
                f"lifetime_rate={data['lifetime_imports_per_minute']:.2f}/min; "
                f"available={data['available']}; claiming={data['claiming']}; "
                f"claimed={data['claimed']}"
            )
        if kind == "inventory_taken":
            return (
                f"✓ inventory: claimed={data['moved']}; "
                f"available={data['available']}; batch={data['batch_id']}"
            )
        if kind == "inventory_error":
            return (
                f"⚠ inventory: available={data['available']}; "
                f"claiming={data['claiming']}; claimed={data['claimed']}"
            )
        if kind == "pipeline_error":
            reason = data.get("reason")
            safe_reason = reason if reason in self._SAFE_REASONS else "unknown"
            stage = data.get("stage", "unknown")
            safe_stage = stage if str(stage).replace("_", "").isalnum() else "unknown"
            return f"⚠ service: {safe_stage} stage stopped ({safe_reason})"
        known = self._format_user(kind, data)
        if known is not None:
            return known
        reason = data.get("reason")
        suffix = f" reason={reason}" if reason in self._SAFE_REASONS else ""
        safe_kind = kind if kind.replace("_", "").isalnum() else "unknown"
        return f"• debug event={safe_kind}{suffix}"

    def emit(self, event):
        kind, data = event
        try:
            message = (
                self._format_debug(kind, data)
                if self.mode == "debug"
                else self._format_user(kind, data)
            )
            if message is not None:
                self.output(message)
        except Exception:
            return


class AuthPipelineRunner:
    """Map interactive controls onto the persistent pipeline lifecycle."""

    def __init__(self, pipeline, emit, *, interval_seconds=None, inventory=None):
        self.pipeline = pipeline
        self.emit = emit
        self.inventory = inventory
        self.paused = False
        self.current_cycle = None

    async def handle_command(self, command):
        command = command.strip().lower()
        if command == "p":
            self.paused = True
            self.pipeline.pause()
            self.emit(("control", {"state": "paused"}))
        elif command == "r":
            self.paused = False
            self.pipeline.resume()
            self.emit(("control", {"state": "running"}))
        elif command == "s":
            status = self.pipeline.status()
            status.update(self.pipeline.ledger.inventory_counts())
            self.emit(("status", status))
        elif command.startswith("take "):
            parts = command.split()
            if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
                self.emit(("control", {"state": "usage: take <positive-count>"}))
                return True
            if self.inventory is None:
                self.emit(("control", {"state": "inventory unavailable"}))
                return True
            try:
                batch = await asyncio.to_thread(self.inventory.take, int(parts[1]))
            except InventoryError as exc:
                self.emit(
                    (
                        "inventory_error",
                        {
                            "reason": str(exc),
                            **self.pipeline.ledger.inventory_counts(),
                        },
                    )
                )
                return True
            counts = self.pipeline.ledger.inventory_counts()
            self.emit(
                (
                    "inventory_taken",
                    {
                        "batch_id": batch.batch_id,
                        "directory": str(batch.directory),
                        "moved": batch.moved,
                        **counts,
                    },
                )
            )
        elif command == "c":
            cancelled = await self.pipeline.cancel_active()
            self.emit(
                ("control", {"state": "cancelling" if cancelled else "idle"})
            )
        elif command in {"q", "quit", "exit"}:
            self.pipeline.request_stop()
            self.emit(("control", {"state": "stopping"}))
            return False
        return True

    async def run(self):
        self.current_cycle = asyncio.create_task(self.pipeline.run())
        try:
            await self.current_cycle
        finally:
            self.current_cycle = None


class AuthServiceRunner:
    """Compatibility runner for the former polling service API."""

    def __init__(self, service, emit, *, interval_seconds):
        self.service = service
        self.emit = emit
        self.interval_seconds = interval_seconds
        self.paused = False
        self.current_cycle = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._wake = asyncio.Event()
        self._stopping = False

    async def handle_command(self, command):
        command = command.strip().lower()
        if command == "p":
            self.paused = True
            self._resume.clear()
            self.emit(("control", {"state": "paused"}))
        elif command == "r":
            self.paused = False
            self._resume.set()
            self._wake.set()
            self.emit(("control", {"state": "running"}))
        elif command == "s":
            active = self.current_cycle is not None and not self.current_cycle.done()
            self.emit(
                (
                    "status",
                    {
                        "state": "paused" if self.paused else "running",
                        "active": active,
                    },
                )
            )
        elif command == "c":
            if self.current_cycle is not None and not self.current_cycle.done():
                self.current_cycle.cancel()
                self.emit(("control", {"state": "cancelling"}))
        elif command in {"q", "quit", "exit"}:
            self._stopping = True
            self._resume.set()
            self._wake.set()
            if self.current_cycle is not None and not self.current_cycle.done():
                self.current_cycle.cancel()
            self.emit(("control", {"state": "stopping"}))
            return False
        return True

    async def run(self):
        while not self._stopping:
            await self._resume.wait()
            if self._stopping:
                break
            self.current_cycle = asyncio.create_task(self.service.run_cycle())
            try:
                await self.current_cycle
            except asyncio.CancelledError:
                if self._stopping:
                    break
            except Exception:
                self.emit(("error", {"reason": "sync_failed"}))
            finally:
                self.current_cycle = None
            if self._stopping:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                pass


async def _run_interactive(runner):
    try:
        print(
            "命令：s 状态 | take N 取用凭据 | p 暂停 | r 恢复 | c 取消当前任务 | q 退出",
            flush=True,
        )
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()
    stdin_fd = sys.stdin.fileno()

    def stdin_ready():
        line = sys.stdin.readline()
        if line == "":
            loop.remove_reader(stdin_fd)
            commands.put_nowait(None)
            return
        commands.put_nowait(line)

    loop.add_reader(stdin_fd, stdin_ready)
    worker = asyncio.create_task(runner.run())
    try:
        while not worker.done():
            command_task = asyncio.create_task(commands.get())
            done, _pending = await asyncio.wait(
                {worker, command_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                command_task.cancel()
                with suppress(asyncio.CancelledError):
                    await command_task
                await worker
                break
            command = command_task.result()
            if command is None:
                command = "q"
            if not await runner.handle_command(command):
                break
    except asyncio.CancelledError:
        runner.pipeline.request_stop()
        raise
    finally:
        loop.remove_reader(stdin_fd)
        runner.pipeline.request_stop()
        await asyncio.gather(worker, return_exceptions=True)


async def main_async(*, log_mode="user"):
    import httpx

    from .auth_pipeline import AuthPipeline
    from .config import Settings
    from .executors import PlaywrightExecutor
    from .inventory import CredentialInventory
    from .ledger import Ledger
    from .protocol import XAIProfile, XAIProtocol
    from .remote_stream import DiskSnapshotSource, SSHSnapshotSynchronizer
    from .sinks import LocalAuthFileSink

    service_settings = AuthServiceSettings.from_environ()
    merged = prepare_local_service_environment()
    merged["XAI_ENROLLER_SOURCE_KIND"] = "remote"
    merged["XAI_ENROLLER_AUTH_EXECUTOR"] = "playwright"
    merged["XAI_ENROLLER_CONCURRENCY"] = "1"
    settings = Settings.from_environ(merged)
    terminal = EventTerminal(mode=log_mode)
    client = httpx.AsyncClient()
    pipeline = None
    try:
        ledger = Ledger(settings.ledger_path, settings.source_salt)
        snapshot_path = Path(settings.local_auth_dir) / "source-snapshot.jsonl"
        synchronizer = SSHSnapshotSynchronizer(
            service_settings.ssh_host,
            snapshot_path,
            remote_root=service_settings.remote_root,
            identity_file=service_settings.identity_file,
            fingerprint=ledger.fingerprint,
        )
        source = DiskSnapshotSource(
            snapshot_path,
            synchronizer=synchronizer,
            sync_seconds=service_settings.sync_seconds,
            event_callback=lambda kind, data: terminal.emit((kind, data)),
            fingerprint=ledger.fingerprint,
        )
        protocol = XAIProtocol(
            client,
            XAIProfile.default(),
            default_poll_interval=settings.poll_interval,
        )
        executor = PlaywrightExecutor(concurrency=1)
        sink = LocalAuthFileSink(
            Path(settings.local_auth_dir) / AUTHENTICATED_DIRNAME,
            name_secret=settings.source_salt,
        )
        inventory = CredentialInventory(
            ledger,
            Path(settings.local_auth_dir) / AUTHENTICATED_DIRNAME,
            Path(settings.local_auth_dir) / CLAIMED_DIRNAME,
        )
        recovered = await asyncio.to_thread(inventory.recover)
        if recovered:
            terminal.emit(("control", {"state": f"recovered {recovered} claims"}))
        terminal.emit(
            (
                "startup",
                {
                    "destination": f"{AUTHENTICATED_DIRNAME}/",
                    **ledger.inventory_counts(),
                },
            )
        )
        pipeline = AuthPipeline(
            source=source,
            protocol=protocol,
            executor=executor,
            sink=sink,
            ledger=ledger,
            timeout=settings.timeout_sec,
            min_authorization_interval=(
                service_settings.min_authorization_interval_seconds
            ),
            event_callback=lambda kind, data: terminal.emit((kind, data)),
        )
        pipeline.rate_gate.COOLDOWN_SECONDS = float(service_settings.retry_seconds)
        runner = AuthPipelineRunner(pipeline, terminal.emit, inventory=inventory)
        await _run_interactive(runner)
    finally:
        if pipeline is not None:
            pipeline.request_stop()
        await client.aclose()


def _known_configuration_key(error):
    message = str(error)
    keys = (
        "XAI_AUTH_SERVICE_LOG_MODE",
        "XAI_AUTH_SERVICE_SSH_HOST",
        "XAI_AUTH_SERVICE_SYNC_SEC",
        "XAI_AUTH_SERVICE_RETRY_SEC",
        "XAI_AUTH_SERVICE_MIN_INTERVAL_SEC",
        "XAI_ENROLLER_TARGET",
        "XAI_ENROLLER_RETRY_ATTEMPTS",
        "XAI_ENROLLER_TIMEOUT_SEC",
        "XAI_ENROLLER_POLL_SEC",
        "XAI_ENROLLER_AUTH_EXECUTOR",
        "XAI_ENROLLER_SINK",
        "XAI_ENROLLER_CPA_BASE_URL",
        "XAI_ENROLLER_CPA_MANAGEMENT_SECRET",
        "XAI_ENROLLER_LOCAL_AUTH_DIR",
        "XAI_ENROLLER_SOURCE_SALT",
    )
    explicit = next((key for key in keys if key in message), None)
    if explicit is not None:
        return explicit
    message_keys = {
        "target must be between 1 and 100": "XAI_ENROLLER_TARGET",
        "retry attempts must be between 0 and 3": "XAI_ENROLLER_RETRY_ATTEMPTS",
        "timeout must be positive": "XAI_ENROLLER_TIMEOUT_SEC",
        "poll interval must be positive": "XAI_ENROLLER_POLL_SEC",
        "executor must be http or playwright": "XAI_ENROLLER_AUTH_EXECUTOR",
        "sink must be cpa or local": "XAI_ENROLLER_SINK",
        "CPA base URL must use HTTPS": "XAI_ENROLLER_CPA_BASE_URL",
        "CPA base URL and management secret are required": (
            "XAI_ENROLLER_CPA_BASE_URL/XAI_ENROLLER_CPA_MANAGEMENT_SECRET"
        ),
        "local auth directory is required": "XAI_ENROLLER_LOCAL_AUTH_DIR",
        "source salt is required": "XAI_ENROLLER_SOURCE_SALT",
    }
    return message_keys.get(message)


def _print_unexpected_service_failure(error, *, log_mode):
    if log_mode == "debug":
        detail = f"异常类别 {type(error).__name__}"
    else:
        detail = "使用 bash auth-service.sh --debug 查看异常类别"
    print(f"[✗] 认证服务异常终止 | {detail}", file=sys.stderr)


def main(argv=None):
    mode = "user"
    try:
        mode = resolve_auth_log_mode(argv)
        asyncio.run(main_async(log_mode=mode))
        return 0
    except AuthServiceConfigurationError as error:
        key = _known_configuration_key(error) or "认证服务参数"
        print(
            f"[✗] 配置错误：{key} | 教程 docs/guides/auth-service.md#配置远端同步",
            file=sys.stderr,
        )
        return 2
    except ValueError as error:
        key = _known_configuration_key(error)
        if key is not None:
            print(
                f"[✗] 配置错误：{key} | 教程 docs/guides/auth-service.md#配置远端同步",
                file=sys.stderr,
            )
            return 2
        _print_unexpected_service_failure(error, log_mode=mode)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _print_unexpected_service_failure(error, log_mode=mode)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
