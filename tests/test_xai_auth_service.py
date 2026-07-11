import asyncio
import subprocess
import sys

from xai_enroller.models import JobResult, JobStatus, SourceRecord
from xai_enroller.service import (
    AuthService,
    AuthServiceRunner,
    AuthServiceSettings,
    SSHRegisteredSource,
    parse_registered_accounts,
)
from xai_enroller.ledger import Ledger


def test_registered_account_parser_keeps_only_email_and_sso():
    records = list(parse_registered_accounts([
        "one@example.test:discarded-password:sso-one\n",
        "two@example.test:another-password:sso-two\n",
    ]))

    assert records == [
        SourceRecord("one@example.test", "sso-one"),
        SourceRecord("two@example.test", "sso-two"),
    ]
    assert "discarded-password" not in repr(records)


def test_service_is_silent_without_new_records_and_emits_lifecycle_events():
    class Source:
        async def fetch(self):
            return [SourceRecord("done@example.test", "sso-done"), SourceRecord("new@example.test", "sso-new")]

    class Ledger:
        def has_imported(self, source_id):
            return source_id == "done@example.test"

    class Enroller:
        ledger = Ledger()

        async def run_records(self, records):
            assert records == [SourceRecord("new@example.test", "sso-new")]
            return [JobResult("new@example.test", JobStatus.IMPORTED, "imported")]

    events = []
    service = AuthService(Source(), Enroller(), events.append)

    results = asyncio.run(service.run_cycle())

    assert [item.status for item in results] == [JobStatus.IMPORTED]
    assert events == [
        ("sync", {"new": 1}),
        ("result", {"source_id": "new@example.test", "status": "imported", "reason": "imported"}),
    ]


def test_ledger_recognizes_previously_imported_sources(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"test-salt")
    imported = ledger.start("done@example.test")
    ledger.finish(imported, JobStatus.IMPORTED, "imported")
    failed = ledger.start("retry@example.test")
    ledger.finish(failed, JobStatus.SINK_FAILED, "sink_failed")

    assert ledger.has_imported("done@example.test") is True
    assert ledger.has_imported("retry@example.test") is False


def test_registered_sso_exporter_never_outputs_account_passwords(tmp_path):
    accounts = tmp_path / "accounts.txt"
    accounts.write_text(
        "one@example.test:password-one:sso-one\n"
        "two@example.test:password-two:sso-two\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "scripts/export_registered_sso.py", str(accounts)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "one@example.test\tsso-one",
        "two@example.test\tsso-two",
    ]
    assert "password-" not in result.stdout


def test_ssh_source_imports_only_the_exported_email_and_sso():
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"one@example.test\tsso-one\n", b""

    async def process_factory(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    source = SSHRegisteredSource(
        "ubuntu@example.test",
        identity_file="/tmp/test-key",
        process_factory=process_factory,
    )

    records = asyncio.run(source.fetch())

    assert records == [SourceRecord("one@example.test", "sso-one")]
    assert calls[0][0][-1].endswith("scripts/export_registered_sso.py keys/accounts.txt")
    assert "password" not in repr(records)


def test_interactive_runner_reports_controls_and_cancels_the_active_cycle():
    class Service:
        async def run_cycle(self):
            await asyncio.Event().wait()

    async def scenario():
        events = []
        runner = AuthServiceRunner(Service(), events.append, interval_seconds=30)
        assert await runner.handle_command("p") is True
        assert runner.paused is True
        assert await runner.handle_command("s") is True
        assert await runner.handle_command("r") is True
        assert runner.paused is False
        runner.current_cycle = asyncio.create_task(asyncio.Event().wait())
        assert await runner.handle_command("c") is True
        await asyncio.sleep(0)
        assert runner.current_cycle.cancelled()
        assert await runner.handle_command("q") is False
        return events

    events = asyncio.run(scenario())

    assert events == [
        ("control", {"state": "paused"}),
        ("status", {"state": "paused", "active": False}),
        ("control", {"state": "running"}),
        ("control", {"state": "cancelling"}),
        ("control", {"state": "stopping"}),
    ]


def test_auth_service_settings_requires_an_ssh_host_and_bounds_polling():
    settings = AuthServiceSettings.from_environ(
        {
            "XAI_AUTH_SERVICE_SSH_HOST": "ubuntu@example.test",
            "XAI_AUTH_SERVICE_SYNC_SEC": "45",
        }
    )

    assert settings.ssh_host == "ubuntu@example.test"
    assert settings.sync_seconds == 45
    assert settings.remote_root == "/opt/grok-free-register"
