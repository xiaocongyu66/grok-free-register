import argparse
import asyncio

import httpx

from .config import Settings
from .coordinator import EnrollmentCoordinator
from .executors import HTTPProbeExecutor, PlaywrightExecutor
from .protocol import XAIProfile, XAIProtocol
from .sinks import CPAAuthFileSink
from .sources import FileSourceAdapter, SQLiteSourceAdapter


def build_parser():
    parser = argparse.ArgumentParser(description="Bounded xAI OAuth Device Flow enroller")
    parser.add_argument("--source", choices=("file", "sqlite"))
    parser.add_argument("--target", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--retry-attempts", type=int)
    parser.add_argument("--executor", choices=("http", "playwright"))
    parser.add_argument("--sink", choices=("cpa",))
    return parser


async def main_async(args=None):
    parsed = build_parser().parse_args(args)
    env = dict()
    if parsed.source:
        env["XAI_ENROLLER_SOURCE_KIND"] = parsed.source
    if parsed.target is not None:
        env["XAI_ENROLLER_TARGET"] = str(parsed.target)
    if parsed.concurrency is not None:
        env["XAI_ENROLLER_CONCURRENCY"] = str(parsed.concurrency)
    if parsed.retry_attempts is not None:
        env["XAI_ENROLLER_RETRY_ATTEMPTS"] = str(parsed.retry_attempts)
    if parsed.executor:
        env["XAI_ENROLLER_AUTH_EXECUTOR"] = parsed.executor
    if parsed.sink:
        env["XAI_ENROLLER_SINK"] = parsed.sink
    import os

    merged = dict(os.environ)
    merged.update(env)
    settings = Settings.from_environ(merged)
    source = (
        FileSourceAdapter(settings.source_file)
        if settings.source_kind == "file"
        else SQLiteSourceAdapter(settings.source_db, settings.source_salt)
    )
    client = httpx.AsyncClient()
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
    sink_client = None
    if settings.sink == "cpa":
        sink_client = httpx.AsyncClient()
        sink = CPAAuthFileSink(settings.cpa_base_url, settings.cpa_management_secret, sink_client)
    try:
        coordinator = EnrollmentCoordinator(
            source=source,
            protocol=protocol,
            executor=executor,
            sink=sink,
            ledger_path=settings.ledger_path,
            ledger_salt=settings.source_salt,
            concurrency=settings.concurrency,
            timeout=settings.timeout_sec,
            retry_attempts=settings.retry_attempts,
        )
        results = await coordinator.run(settings.target)
        for result in results:
            print(f"{result.source_id}: {result.status.value} ({result.reason_code})")
    finally:
        await client.aclose()
        if sink_client:
            await sink_client.aclose()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
