"""本地注册账号同步与认证服务的事件驱动核心。"""

import asyncio
import os
import shlex
from dataclasses import dataclass

from .models import SourceRecord


@dataclass(frozen=True)
class AuthServiceSettings:
    ssh_host: str
    remote_root: str
    identity_file: str | None
    sync_seconds: int

    @classmethod
    def from_environ(cls, env=None):
        env = dict(os.environ if env is None else env)
        ssh_host = (env.get("XAI_AUTH_SERVICE_SSH_HOST") or "").strip()
        if not ssh_host:
            raise ValueError("XAI_AUTH_SERVICE_SSH_HOST is required")
        remote_root = (
            env.get("XAI_AUTH_SERVICE_REMOTE_ROOT") or "/opt/grok-free-register"
        ).strip()
        identity_file = (env.get("XAI_AUTH_SERVICE_SSH_IDENTITY") or "").strip() or None
        try:
            sync_seconds = int(env.get("XAI_AUTH_SERVICE_SYNC_SEC", "30"))
        except ValueError as exc:
            raise ValueError("XAI_AUTH_SERVICE_SYNC_SEC must be an integer") from exc
        if not 5 <= sync_seconds <= 3600:
            raise ValueError("XAI_AUTH_SERVICE_SYNC_SEC must be between 5 and 3600")
        return cls(ssh_host, remote_root, identity_file, sync_seconds)


def parse_registered_accounts(lines):
    """将注册机的 ``email:password:sso`` 输出转换为认证输入。"""
    seen = set()
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip("\r\n")
        if not raw:
            continue
        try:
            source_id, _password, sso_token = raw.rsplit(":", 2)
        except ValueError as exc:
            raise ValueError(f"invalid registered account line {line_number}") from exc
        if not source_id or not sso_token or source_id in seen:
            continue
        seen.add(source_id)
        yield SourceRecord(source_id, sso_token)


def parse_exported_records(lines):
    """解析远端导出器的 ``email<TAB>sso`` 输出。"""
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


class SSHRegisteredSource:
    """从注册机拉取已经脱敏为邮箱与 SSO 的账户记录。"""

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
    """在发现新账号或认证结果变化时才产生事件。"""

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
            self.emit(("result", {
                "source_id": result.source_id,
                "status": result.status.value,
                "reason": result.reason_code,
            }))
        return results


class AuthServiceRunner:
    """认证服务的常驻控制循环；仅在命令或生命周期事件时输出。"""

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
            self.emit(("status", {"state": "paused" if self.paused else "running", "active": active}))
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
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


class EventTerminal:
    """将认证生命周期事件输出为简洁、无敏感凭证的终端日志。"""

    def emit(self, event):
        kind, data = event
        if kind == "sync":
            message = f"✓ sync: found {data['new']} new registered account(s)"
        elif kind == "device_flow":
            message = (
                f"🔑 device flow: {data['source_id']} code={data['user_code']} "
                f"url={data['verification_url']}"
            )
        elif kind == "result":
            symbol = "✓" if data["status"] == "imported" else "⚠"
            message = (
                f"{symbol} {data['source_id']}: {data['status']} "
                f"({data['reason']})"
            )
        elif kind == "control":
            message = f"• service: {data['state']}"
        elif kind == "status":
            message = f"• status: {data['state']}; active={str(data['active']).lower()}"
        else:
            message = "⚠ sync: failed"
        print(message, flush=True)


async def _run_interactive(runner):
    print("commands: s=status, p=pause, r=resume, c=cancel batch, q=quit", flush=True)
    worker = asyncio.create_task(runner.run())
    try:
        while True:
            command = await asyncio.to_thread(input)
            if not command:
                command = "q"
            if not await runner.handle_command(command):
                break
    finally:
        await runner.handle_command("q")
        await worker


async def main_async():
    import httpx

    from .config import Settings
    from .coordinator import EnrollmentCoordinator
    from .executors import HTTPProbeExecutor, PlaywrightExecutor
    from .protocol import XAIProfile, XAIProtocol
    from .sinks import CPAAuthFileSink

    service_settings = AuthServiceSettings.from_environ()
    merged = dict(os.environ)
    merged["XAI_ENROLLER_SOURCE_KIND"] = "remote"
    merged.setdefault("XAI_ENROLLER_AUTH_EXECUTOR", "playwright")
    settings = Settings.from_environ(merged)
    terminal = EventTerminal()
    source = SSHRegisteredSource(
        service_settings.ssh_host,
        remote_root=service_settings.remote_root,
        identity_file=service_settings.identity_file,
    )
    client = httpx.AsyncClient()
    sink_client = None
    try:
        protocol = XAIProtocol(
            client,
            XAIProfile.default(),
            default_poll_interval=settings.poll_interval,
        )
        executor = (
            HTTPProbeExecutor(client)
            if settings.executor == "http"
            else PlaywrightExecutor(settings.concurrency)
        )
        sink = None
        if settings.sink == "cpa":
            sink_client = httpx.AsyncClient()
            sink = CPAAuthFileSink(
                settings.cpa_base_url,
                settings.cpa_management_secret,
                sink_client,
            )
        coordinator = EnrollmentCoordinator(
            source=None,
            protocol=protocol,
            executor=executor,
            sink=sink,
            ledger_path=settings.ledger_path,
            ledger_salt=settings.source_salt,
            concurrency=settings.concurrency,
            timeout=settings.timeout_sec,
            retry_attempts=settings.retry_attempts,
            event_callback=lambda kind, data: terminal.emit((kind, data)),
        )
        service = AuthService(source, coordinator, terminal.emit)
        runner = AuthServiceRunner(
            service,
            terminal.emit,
            interval_seconds=service_settings.sync_seconds,
        )
        await _run_interactive(runner)
    finally:
        await client.aclose()
        if sink_client is not None:
            await sink_client.aclose()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
