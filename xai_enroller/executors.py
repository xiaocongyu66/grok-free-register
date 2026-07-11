import asyncio
import glob
import importlib
import os
from urllib.parse import urlparse

import httpx

from .models import AuthorizationResult, AuthorizationStatus, DeviceFlow, SourceRecord


class HTTPProbeExecutor:
    def __init__(self, client: httpx.AsyncClient, max_body=64 * 1024):
        self.client = client
        self.max_body = max_body

    async def confirm(self, source: SourceRecord, flow: DeviceFlow):
        parsed = urlparse(flow.verification_url)
        if parsed.scheme != "https" or parsed.hostname != "accounts.x.ai":
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "invalid_url")
        response = await self.client.get(
            flow.verification_url,
            headers={"Cookie": f"sso={source.sso_token}"},
            follow_redirects=False,
        )
        body = await response.aread()
        if len(body) > self.max_body:
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "response_too_large")
        text = body.decode("utf-8", errors="replace").lower()
        if response.status_code == 403 and any(
            marker in text for marker in ("challenge", "cf-chl", "captcha")
        ):
            return AuthorizationResult(AuthorizationStatus.NEEDS_BROWSER, "challenge")
        if response.status_code in {301, 302, 303, 307, 308}:
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "redirect")
        return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "http_probe_inconclusive")


class PlaywrightExecutor:
    ALLOWED_CONTROLS = frozenset({"Authorize", "Allow", "Continue", "Confirm"})

    def __init__(self, concurrency=1, playwright_factory=None, executable_path=None):
        self.concurrency = concurrency
        self.playwright_factory = playwright_factory
        self.executable_path = executable_path or self._find_executable_path()
        self._playwright = None
        self._browser = None
        self._semaphore = asyncio.Semaphore(concurrency)

    @staticmethod
    def _find_executable_path():
        configured = os.environ.get("XAI_ENROLLER_BROWSER_EXECUTABLE")
        if configured:
            return configured
        candidates = glob.glob(
            os.path.expanduser("~/.cloakbrowser/chromium-*/chrome")
        )
        return sorted(candidates)[-1] if candidates else None

    async def start(self):
        if self._browser is not None:
            return
        factory = self.playwright_factory
        if factory is None:
            module = importlib.import_module("playwright.async_api")
            factory = module.async_playwright
        self._playwright = await factory().start()
        options = {"headless": True}
        if self.executable_path:
            options["executable_path"] = self.executable_path
        self._browser = await self._playwright.chromium.launch(**options)

    async def close(self):
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def confirm(self, source: SourceRecord, flow: DeviceFlow):
        await self.start()
        async with self._semaphore:
            context = None
            page = None
            try:
                context = await self._browser.new_context()
                page = await context.new_page()
                await context.add_cookies(
                    [{
                        "name": "sso",
                        "value": source.sso_token,
                        "domain": "accounts.x.ai",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }]
                )
                await page.goto(flow.verification_url, wait_until="domcontentloaded")
                text = (await page.locator("body").inner_text()).lower()
                if any(marker in text for marker in ("sign in", "login", "mfa", "captcha", "challenge")):
                    return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "blocking_page")
                for name in self.ALLOWED_CONTROLS:
                    button = page.get_by_role("button", name=name)
                    if await button.count():
                        await button.first.click()
                        return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")
                return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "unknown_page")
            except asyncio.CancelledError:
                raise
            except Exception:
                return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "browser_error")
            finally:
                if page is not None:
                    await page.close()
                if context is not None:
                    await context.close()
