import asyncio
import threading

from xai_enroller.auth_pipeline import AuthPipeline
from xai_enroller.ledger import Ledger
from xai_enroller.models import (
    AuthorizationResult,
    AuthorizationStatus,
    DeviceFlow,
    JobStatus,
    OAuthCredential,
    PipelineState,
    SinkReceipt,
    SourceRecord,
)


class EmptySource:
    async def records(self):
        if False:
            yield None

    async def close(self):
        return None


class NoopExecutor:
    async def close(self):
        return None


def _pipeline(tmp_path):
    return AuthPipeline(
        source=EmptySource(),
        protocol=object(),
        executor=NoopExecutor(),
        sink=object(),
        ledger=Ledger(tmp_path / "ledger.db", b"salt"),
    )


def test_cancel_active_is_idempotent_while_cleanup_is_in_progress(tmp_path):
    async def scenario():
        pipeline = _pipeline(tmp_path)

        async def wait_forever():
            await asyncio.Future()

        task = asyncio.create_task(wait_forever())
        pipeline._authorization_task = task
        pipeline._authorization_cancellable = True
        assert await pipeline.cancel_active()
        assert not await pipeline.cancel_active()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


class SingleSource:
    async def records(self):
        yield SourceRecord("stock", "opaque")

    async def close(self):
        return None


class ImmediateProtocol:
    async def start_device_flow(self):
        return DeviceFlow(
            "device",
            "code",
            "https://accounts.x.ai/oauth2/device",
            600,
            0,
        )

    async def poll_token(self, **_kwargs):
        return OAuthCredential(
            "access",
            "refresh",
            None,
            "Bearer",
            3600,
            "later",
            "now",
            "subject",
            "https://auth.x.ai/oauth2/token",
        )


class ImmediateExecutor:
    def __init__(self):
        self.calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def confirm(self, *_args):
        self.calls += 1
        return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")


class BlockingThreadSink:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.pipeline = None
        self.calls = 0

    def _store(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return SinkReceipt("opaque")

    async def store(self, _credential):
        receipt = await asyncio.to_thread(self._store)
        self.pipeline.request_stop()
        return receipt


def test_sink_and_ledger_commit_cannot_be_cancelled_mid_commit(tmp_path):
    async def scenario():
        executor = ImmediateExecutor()
        sink = BlockingThreadSink()
        ledger = Ledger(tmp_path / "ledger.db", b"salt")
        pipeline = AuthPipeline(
            source=SingleSource(),
            protocol=ImmediateProtocol(),
            executor=executor,
            sink=sink,
            ledger=ledger,
            timeout=2,
        )
        sink.pipeline = pipeline
        run = asyncio.create_task(pipeline.run())
        assert await asyncio.to_thread(sink.entered.wait, 1)
        assert not await pipeline.cancel_active()
        sink.release.set()
        await asyncio.wait_for(run, 2)
        assert executor.calls == 1
        assert sink.calls == 1
        assert ledger.aggregate_counts()["imported_unique"] == 1

    asyncio.run(scenario())


def test_queued_source_imported_while_paused_is_not_authorized(tmp_path):
    async def scenario():
        executor = ImmediateExecutor()
        ledger = Ledger(tmp_path / "ledger.db", b"salt")
        pipeline = AuthPipeline(
            source=EmptySource(),
            protocol=ImmediateProtocol(),
            executor=executor,
            sink=object(),
            ledger=ledger,
            timeout=2,
        )
        source = SourceRecord("stock", "opaque")
        fingerprint = ledger.fingerprint(source.source_id)

        assert await pipeline.admit(source)
        pipeline.pause()
        await pipeline.rate_gate.rate_limited()
        run = asyncio.create_task(pipeline.run())
        try:
            async with asyncio.timeout(1):
                while pipeline._authorization_task is None:
                    await asyncio.sleep(0)

            external_job_id = ledger.start_fingerprint(fingerprint)
            ledger.finish(
                external_job_id,
                JobStatus.IMPORTED,
                "imported",
                "external-receipt",
            )
            pipeline.resume()

            async with asyncio.timeout(1):
                while (
                    executor.calls == 0
                    and pipeline._states.get(fingerprint) is not PipelineState.IMPORTED
                ):
                    await asyncio.sleep(0)

            assert executor.calls == 0
            assert pipeline._states[fingerprint] is PipelineState.IMPORTED
        finally:
            pipeline.request_stop()
            await asyncio.wait_for(run, 2)

    asyncio.run(scenario())
