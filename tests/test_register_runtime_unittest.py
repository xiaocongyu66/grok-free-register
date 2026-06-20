import asyncio
import json
import sys
import tempfile
import types
import unittest

from core.observer import Metrics


playwright_pkg = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
playwright_async_api.async_playwright = lambda: None
sys.modules.setdefault("playwright", playwright_pkg)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

requests_mod = types.ModuleType("requests")
requests_mod.get = lambda *_args, **_kwargs: None
requests_mod.post = lambda *_args, **_kwargs: None
sys.modules.setdefault("requests", requests_mod)

import register


class FakePage:
    def __init__(self, context=None):
        self.context = context
        self.closed = False
        self.url = "about:blank"
        self.goto_calls = []
        self.waits = []
        self.evaluations = []

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
        return None

    async def close(self):
        self.closed = True
        pass


class FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False
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

    async def new_page(self):
        page = FakePage(self.context)
        self.pages.append(page)
        return page

    async def new_context(self):
        context = FakeContext()
        self.contexts.append(context)
        return context


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


class RegisterRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_stop = register.STOP
        self._old_timeout = getattr(register, "C_CONSUME_TIMEOUT", None)
        self._old_verify = register.grpc_verify_code
        self._old_register = register.server_action_register
        self._old_create_code = register.grpc_create_code
        self._old_poll_code = register.poll_code
        self._old_poll_code_async = register._poll_code_async
        self._old_log = register.log
        self._old_target = register.TARGET
        self._old_c_hot_page_pool = getattr(register, "C_HOT_PAGE_POOL", None)
        self._old_c_hot_page_pool_size = getattr(register, "C_HOT_PAGE_POOL_SIZE", None)
        self._old_c_set_cookie_via_request = getattr(register, "C_SET_COOKIE_VIA_REQUEST", None)

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

    async def test_monitor_uses_metrics_snapshot(self):
        register.STOP = asyncio.Event()
        register.TARGET = 1
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

    async def test_default_auto_capacity_is_conservative(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
        )

        self.assertEqual(physical, 4)
        self.assertEqual(s_workers, 6)
        self.assertEqual(c_workers, 6)

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

        self.assertEqual(watermarks["t_high"], 6)
        self.assertEqual(watermarks["t_low"], 3)
        self.assertEqual(bounded["t_high"], 8)

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

    async def test_send_q_request_batch_reuses_one_page_for_multiple_emails(self):
        emails = []

        async def fake_create_code(_page, email):
            emails.append(email)
            return True

        register.grpc_create_code = fake_create_code
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        requests = [
            {"handle": "h1", "email": "a@example.test", "password": "pw1"},
            {"handle": "h2", "email": "b@example.test", "password": "pw2"},
            {"handle": "h3", "email": "c@example.test", "password": "pw3"},
        ]

        results = await register._send_q_request_batch(
            browser, physical_sem, p_send_sem, requests
        )

        self.assertEqual(emails, [item["email"] for item in requests])
        self.assertEqual([item["sent"] for item in results], [True, True, True])
        self.assertEqual(len(browser.pages), 1)
        self.assertTrue(browser.pages[0].closed)
        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(p_send_sem._value, 1)

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

    async def test_default_solver_and_page_latency_profile_matches_accepted_optimization(self):
        self.assertEqual(register.SOLVER_INITIAL_WAIT_MS, 500)
        self.assertTrue(register.SOLVER_FAST_CLICK)
        self.assertEqual(register.PAGE_GOTO_WAIT_UNTIL, "domcontentloaded")
        self.assertEqual(register.PAGE_POST_WAIT_MS, 500)


if __name__ == "__main__":
    unittest.main()
