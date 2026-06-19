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
    def __init__(self):
        self.closed = False

    async def set_viewport_size(self, _size):
        pass

    async def goto(self, _url, timeout=None):
        pass

    async def wait_for_timeout(self, _timeout):
        pass

    async def close(self):
        self.closed = True
        pass


class FakeBrowser:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


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

    async def asyncTearDown(self):
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


if __name__ == "__main__":
    unittest.main()
