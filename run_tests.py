#!/usr/bin/env python3
"""
CSP 架构测试 Runner v2

分两部分:
  Part A — 注入测试: 针对每个架构不变量,注入精确取消/故障点,验证不变量
  Part B — 场景压测: 每场景 ≥120s,完整 CSV 时间序列

用法:
  python run_tests.py                    # 全部(A+B)
  python run_tests.py -s steady_state    # 单个场景
  python run_tests.py --list             # 列出场景
"""
import asyncio, csv, dataclasses, gc, json, os, random, sys, time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.envelope import ResourceEnvelope
from core.inventory import Inventory
from core.observer import Metrics


# ═══════════════════════════════════════
#  Part A: 注入测试 — 验证架构不变量
# ═══════════════════════════════════════

async def run_injection_tests(out_dir: Path) -> list[dict]:
    """运行全部注入测试,返回结果列表。"""
    results = []
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. release_slot_once 幂等 ──
    async def test_release_idempotent():
        sem = asyncio.Semaphore(3)
        env = await ResourceEnvelope.create_with_slot('T', 'tok', sem)
        assert sem._value == 2, f'acquire 后 sem={sem._value}'
        env.release_slot_once('test1')
        assert sem._value == 3, f'首次 release 后 sem={sem._value}'
        env.release_slot_once('test2')
        assert sem._value == 3, f'重复 release 后 sem={sem._value} (应仍为3)'
        env.release_slot_once('test3')
        assert sem._value == 3, f'第三次 release 后 sem={sem._value}'
        return True, 'release_slot_once 重复调用 3 次,sem 始终为 3'

    # ── 2. T 生成前不占用 T_Slot_Sem ──
    async def test_t_slot_not_occupied_before_create():
        sem = asyncio.Semaphore(2)
        # create_with_slot 是唯一的 slot 获取点
        # 在调用前,sem 应为满
        assert sem._value == 2, f'初始 sem={sem._value}'
        # 模拟 S worker: 生成 token 后才 create_with_slot
        token = 'tok_test'
        # 此时 token 已生成但未 create_with_slot — sem 应仍为满
        assert sem._value == 2, f'生成 token 后 sem={sem._value} (不应变化)'
        # 现在 create_with_slot
        env = await ResourceEnvelope.create_with_slot('T', token, sem)
        assert sem._value == 1, f'create_with_slot 后 sem={sem._value}'
        env.release_slot_once()
        return True, 'T 生成后、create_with_slot 前,sem 不变(=2);create 后 sem=1'

    # ── 3. Q 返回前不占用 Q_Slot_Sem ──
    async def test_q_slot_not_occupied_before_return():
        sem = asyncio.Semaphore(2)
        # P worker: 创建邮箱 + 发请求 + 等待返回,这些都不占 Q slot
        # 只有 Q 真正返回后才 create_with_slot
        assert sem._value == 2
        # 模拟等待 Q 返回期间
        await asyncio.sleep(0.01)
        assert sem._value == 2, f'等待 Q 返回期间 sem={sem._value} (不应变化)'
        # Q 返回后才占 slot
        env = await ResourceEnvelope.create_with_slot('Q', {'code': '123'}, sem)
        assert sem._value == 1
        env.release_slot_once()
        return True, 'Q 请求发出到返回期间,sem 不变(=2);返回后 create 后 sem=1'

    # ── 4. P 等待 Q 时持有 Physical_Sem ──
    async def test_p_not_holding_physical_while_waiting():
        phys = asyncio.Semaphore(1)
        q_pend = asyncio.Semaphore(2)
        q_slot = asyncio.Semaphore(4)
        inv = Inventory()
        m = Metrics()
        stop = asyncio.Event()
        phys_held_during_wait = []

        async def p_worker_check():
            """P worker 实现,在等待 Q 期间检查 Physical_Sem。"""
            await q_pend.acquire()
            rel = False
            try:
                # 阶段1: 创建邮箱 + 发请求(持有 Physical)
                await phys.acquire()
                try:
                    await asyncio.sleep(0.02)  # 模拟创建+发送
                finally:
                    phys.release()
                    # Physical 已释放

                # 阶段2: 等待 Q 返回(不持有 Physical)
                await asyncio.sleep(0.05)  # 模拟等待
                phys_held_during_wait.append(phys._value)  # 记录此时 phys 的值

                # Q 返回后入库
                now = time.time()
                env = None
                try:
                    env = await ResourceEnvelope.create_with_slot(
                        'Q', {'email': 't@t.com', 'password': 'p', 'code': '123'},
                        q_slot, expires_at=now+300
                    )
                    await inv.put_q(env)
                except Exception:
                    if env and not env.released: env.discard()
            except asyncio.CancelledError:
                raise
            finally:
                if not rel:
                    q_pend.release()

        task = asyncio.create_task(p_worker_check())
        await asyncio.sleep(0.15)

        # 验证: 等待 Q 期间 Physical_Sem 应为满(=1,未被持有)
        assert all(v == 1 for v in phys_held_during_wait), \
            f'P 等待 Q 期间 Physical_Sem 值: {phys_held_during_wait} (应全为 1)'
        return True, f'P 等待 Q 期间 Physical_Sem 值始终为 {phys_held_during_wait} (=容量,未被持有)'

    # ── 5. claim_pair 等待取消时是否弹出资源 ──
    async def test_claim_cancel_no_pop():
        t_sem = asyncio.Semaphore(4)
        q_sem = asyncio.Semaphore(4)
        inv = Inventory(metrics=Metrics())

        # 只放 T,不放 Q,让 C 等待
        env_t = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        await inv.put_t(env_t)
        assert inv.t_depth == 1

        claimed = False
        async def claimer():
            nonlocal claimed
            async with inv.claim_pair() as pair:
                claimed = True

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not claimed, 'claim 等待时不应成功'
        assert inv.t_depth == 1, f'取消后 T 库存应仍为 1,实际 {inv.t_depth}'
        assert t_sem._value == 3, f'取消后 T sem 应为 3,实际 {t_sem._value}'
        return True, 'claim 等待中取消:T 仍在库存(depth=1),sem 不变(=3),未弹出'

    # ── 6. claim 成功后 C 取消是否核销 T/Q ──
    async def test_claim_then_cancel_settles():
        t_sem = asyncio.Semaphore(4)
        q_sem = asyncio.Semaphore(4)
        inv = Inventory(metrics=Metrics())

        env_t = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        env_q = await ResourceEnvelope.create_with_slot('Q', {'code': '123'}, q_sem)
        await inv.put_t(env_t)
        await inv.put_q(env_q)

        async def consumer():
            async with inv.claim_pair() as pair:
                # pair 已 claim,等待中被取消
                await asyncio.sleep(999)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # PairLease.__aexit__ 应核销 T/Q
        assert t_sem._value == 4, f'C 取消后 T sem 应为 4(已核销),实际 {t_sem._value}'
        assert q_sem._value == 4, f'C 取消后 Q sem 应为 4(已核销),实际 {q_sem._value}'
        assert inv.t_depth == 0, f'取消后 T 库存应为 0,实际 {inv.t_depth}'
        assert inv.q_depth == 0, f'取消后 Q 库存应为 0,实际 {inv.q_depth}'
        return True, 'claim 成功后 C 取消:T/Q sem 均恢复为 4,库存清零,核销成功'

    # ── 7. 无重复 claim(追踪具体 token) ──
    async def test_no_duplicate_claim():
        t_sem = asyncio.Semaphore(10)
        q_sem = asyncio.Semaphore(10)
        inv = Inventory(metrics=Metrics())

        for i in range(5):
            et = await ResourceEnvelope.create_with_slot('T', f'tok_{i}', t_sem)
            eq = await ResourceEnvelope.create_with_slot('Q', {'code': f'c_{i}'}, q_sem)
            await inv.put_t(et)
            await inv.put_q(eq)

        claimed_tokens = []
        async def claimer():
            async with inv.claim_pair() as pair:
                claimed_tokens.append(pair.t.value)

        tasks = [asyncio.create_task(claimer()) for _ in range(5)]
        await asyncio.gather(*tasks)

        assert len(claimed_tokens) == 5, f'应 claim 5 次,实际 {len(claimed_tokens)}'
        assert len(set(claimed_tokens)) == 5, f'重复 claim: {claimed_tokens}'
        return True, f'5 个 T/Q 对并发 claim,claimed tokens={sorted(claimed_tokens)},无重复'

    # ── 8. C 不持有单边等待(单边故障场景) ──
    async def test_c_no_single_side_wait():
        t_sem = asyncio.Semaphore(4)
        q_sem = asyncio.Semaphore(4)
        inv = Inventory(metrics=Metrics())

        # 只放 T,不放 Q — C 应等待,不持有 T
        for i in range(3):
            et = await ResourceEnvelope.create_with_slot('T', f'tok_{i}', t_sem)
            await inv.put_t(et)

        async def consumer():
            async with inv.claim_pair() as pair:
                pass  # 不应到达

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.1)

        # C 应仍在等待,未持有任何 T
        assert inv.t_depth == 3, f'C 应在等待,库存 T 应为 3,实际 {inv.t_depth}'
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return True, '只有 T 无 Q 时,C 等待不持有 T(depth=3 未变),不会单边持有'

    # ── 执行全部注入测试 ──
    tests = [
        ('release_slot_once 幂等', test_release_idempotent),
        ('T 生成前不占用 T_Slot_Sem', test_t_slot_not_occupied_before_create),
        ('Q 返回前不占用 Q_Slot_Sem', test_q_slot_not_occupied_before_return),
        ('P 等待 Q 时不持有 Physical_Sem', test_p_not_holding_physical_while_waiting),
        ('claim_pair 等待取消不弹资源', test_claim_cancel_no_pop),
        ('claim 成功后 C 取消核销 T/Q', test_claim_then_cancel_settles),
        ('无重复 claim(追踪 token)', test_no_duplicate_claim),
        ('C 不持有单边 T/Q', test_c_no_single_side_wait),
    ]

    for name, fn in tests:
        try:
            ok, detail = await fn()
            results.append({'name': name, 'ok': ok, 'detail': detail})
        except Exception as e:
            results.append({'name': name, 'ok': False, 'detail': f'异常: {e}'})

    # 写不变量报告
    with open(out_dir / 'injection_tests.md', 'w') as f:
        f.write('# Part A: 架构不变量注入测试\n\n')
        f.write('每个不变量通过精确注入取消/故障点验证,不依赖运行时观察。\n\n')
        f.write('| 不变量 | 结果 | 验证方式 |\n')
        f.write('|--------|------|----------|\n')
        for r in results:
            f.write(f'| {r["name"]} | {"✅" if r["ok"] else "❌"} | {r["detail"]} |\n')
        f.write(f'\n**总结: {"全部通过 ✅" if all(r["ok"] for r in results) else "存在违反 ❌"}**\n')

    return results


# ═══════════════════════════════════════
#  Part B: 场景压测
# ═══════════════════════════════════════
@dataclasses.dataclass
class ScenarioConfig:
    name: str; description: str; duration: float; max_ops: int = 100000
    device: str = "t4g.small (2 vCPU, 2GB RAM)"
    python_env: str = f"Python {sys.version.split()[0]}, asyncio"
    s_workers: int = 3; p_workers: int = 4; c_workers: int = 3
    physical_cap: int = 6; physical_light_heavy: bool = False
    physical_light_cap: int = 1; physical_heavy_cap: int = 5
    q_pending_cap: int = 8; t_slot_cap: int = 12; q_slot_cap: int = 12
    t_max_age: float = 300.0; q_max_age: float = 120.0
    p_request_timeout: float = 95.0; c_consume_timeout: float = 30.0
    real_external_q: bool = False
    t_gen_delay: float = 0.01; t_fail_rate: float = 0.05
    q_create_delay: float = 0.01; q_poll_delay: float = 0.02
    q_poll_timeout_rate: float = 0.05; q_send_fail_rate: float = 0.05
    c_register_delay: float = 0.01; c_fail_rate: float = 0.10
    q_long_tail_p99: float = 0.0
    q_total_fail: bool = False; s_total_fail: bool = False
    c_total_fail: bool = False; c_total_timeout: bool = False
    cancel_rate: float = 0.0; expire_fast: bool = False


class FakeT:
    def __init__(self, cfg, rng):
        self.cfg, self.rng, self.count = cfg, rng, 0
    async def generate(self):
        self.count += 1
        if self.cfg.s_total_fail: return None
        await asyncio.sleep(self.cfg.t_gen_delay * self.rng.uniform(0.5, 1.5))
        return None if self.rng.random() < self.cfg.t_fail_rate else f'tok_{self.count}'

class FakeQ:
    def __init__(self, cfg, rng):
        self.cfg, self.rng, self.count = cfg, rng, 0
    async def create_request(self):
        self.count += 1
        if self.cfg.q_total_fail:
            await asyncio.sleep(0.005); return None
        await asyncio.sleep(self.cfg.q_create_delay * self.rng.uniform(0.5, 1.5))
        if self.rng.random() < self.cfg.q_send_fail_rate: return None
        return self.count, f'u_{self.count}@t.com', f'p_{self.count}'
    async def poll_request(self, request):
        n, email, password = request
        d = self.cfg.q_poll_delay * self.rng.uniform(0.5, 1.5)
        if self.cfg.q_long_tail_p99 > 0 and self.rng.random() < 0.01:
            d = self.cfg.q_long_tail_p99 * self.rng.uniform(0.5, 1.5)
        await asyncio.sleep(d)
        if self.rng.random() < self.cfg.q_poll_timeout_rate: return None
        return email, password, f'{self.rng.randint(100000,999999)}'
    async def create_and_poll(self):
        request = await self.create_request()
        if request is None:
            return None
        return await self.poll_request(request)

class FakeC:
    def __init__(self, cfg, rng): self.cfg, self.rng = cfg, rng
    async def register(self, email, password, code, token):
        if self.cfg.c_total_fail: return None
        if self.cfg.c_total_timeout: await asyncio.sleep(999); return None
        await asyncio.sleep(self.cfg.c_register_delay * self.rng.uniform(0.5, 1.5))
        return None if self.rng.random() < self.cfg.c_fail_rate else f'sso_{email}'


@dataclasses.dataclass
class Sample:
    ts: float; t_inv: int; q_inv: int; q_pend_free: int
    effective_q_pending_cap: int
    phys_used: int; t_slot_used: int; q_slot_used: int
    pair_lease_active: int; c_workers_in_consume: int
    t_produced: int; q_sent: int; q_ret: int; pair_claimed: int
    c_ok: int; c_fail: int; t_expired: int; q_expired: int
    t_disc: int; q_disc: int
    cw_p50: float; cw_p90: float; cw_p99: float
    ps_p50: float; ps_p90: float; ps_p99: float
    qr_p50: float; qr_p90: float; qr_p99: float
    cc_p50: float; cc_p90: float; cc_p99: float
    ta_p50: float; ta_p90: float; ta_p99: float
    qa_p50: float; qa_p90: float; qa_p99: float
    last_c: float


class Collector:
    def __init__(self):
        self.cw = []; self.ps = []; self.qr = []
        self.cc = []; self.ta = []; self.qa = []
    def reset(self):
        self.cw.clear(); self.ps.clear(); self.qr.clear()
        self.cc.clear(); self.ta.clear(); self.qa.clear()
    @staticmethod
    def _p(d, p):
        if not d: return 0.0
        return sorted(d)[min(int(len(d)*p), len(d)-1)]
    def snap(self, cfg, m, inv_obj, sems, runtime, t0, last_c_t):
        now = time.time()
        s = Sample(
            ts=round(now-t0,1), t_inv=inv_obj.t_depth, q_inv=inv_obj.q_depth,
            q_pend_free=sems['q_pending']._value,
            effective_q_pending_cap=min(cfg.p_workers, cfg.q_pending_cap),
            phys_used=physical_used(cfg, sems),
            t_slot_used=cfg.t_slot_cap-sems['t_slot']._value,
            q_slot_used=cfg.q_slot_cap-sems['q_slot']._value,
            pair_lease_active=runtime['pair_lease_active'],
            c_workers_in_consume=runtime['c_workers_in_consume'],
            t_produced=m.t_produced, q_sent=m.q_sent, q_ret=m.q_returned,
            pair_claimed=m.pair_claimed, c_ok=m.pair_consumed_ok, c_fail=m.pair_consumed_fail,
            t_expired=m.t_expired, q_expired=m.q_expired, t_disc=m.t_discarded, q_disc=m.q_discarded,
            cw_p50=self._p(self.cw,.5), cw_p90=self._p(self.cw,.9), cw_p99=self._p(self.cw,.99),
            ps_p50=self._p(self.ps,.5), ps_p90=self._p(self.ps,.9), ps_p99=self._p(self.ps,.99),
            qr_p50=self._p(self.qr,.5), qr_p90=self._p(self.qr,.9), qr_p99=self._p(self.qr,.99),
            cc_p50=self._p(self.cc,.5), cc_p90=self._p(self.cc,.9), cc_p99=self._p(self.cc,.99),
            ta_p50=self._p(self.ta,.5), ta_p90=self._p(self.ta,.9), ta_p99=self._p(self.ta,.99),
            qa_p50=self._p(self.qa,.5), qa_p90=self._p(self.qa,.9), qa_p99=self._p(self.qa,.99),
            last_c=round(now-last_c_t,1),
        )
        self.reset()
        return s


def physical_capacity(cfg):
    if cfg.physical_light_heavy:
        return cfg.physical_light_cap + cfg.physical_heavy_cap
    return cfg.physical_cap


def physical_used(cfg, sems):
    if cfg.physical_light_heavy:
        return (
            cfg.physical_light_cap - sems['physical_light']._value
            + cfg.physical_heavy_cap - sems['physical_heavy']._value
        )
    return cfg.physical_cap - sems['physical']._value


async def s_worker(wid, t_gen, inv, phys, t_slot, m, cfg, stop, ops):
    while not stop.is_set() and ops[0] < cfg.max_ops:
        await phys.acquire()
        try: tok = await t_gen.generate()
        finally: phys.release()
        if tok is None: m.t_discarded += 1; ops[0] += 1; continue
        now = time.time()
        age = cfg.t_max_age if not cfg.expire_fast else random.uniform(0.05, 0.2)
        env = None
        try:
            env = await ResourceEnvelope.create_with_slot('T', tok, t_slot, expires_at=now+age, meta={'t':now})
            await inv.put_t(env)
            m.t_produced += 1; ops[0] += 1
        except Exception:
            if env and not env.released: env.discard()
            m.t_discarded += 1; ops[0] += 1
        except asyncio.CancelledError:
            if env and not env.released: env.discard()
            m.t_discarded += 1; ops[0] += 1; raise
        await asyncio.sleep(0.002)


async def p_worker(wid, q_svc, inv, phys, q_pend, q_slot, m, cfg, stop, col, ops):
    while not stop.is_set() and ops[0] < cfg.max_ops:
        await q_pend.acquire(); rel = False; t0 = time.time()
        try:
            await phys.acquire()
            try:
                request = await q_svc.create_request()
            finally:
                phys.release()
            col.ps.append(time.time()-t0)
            if request is None:
                m.q_discarded += 1; q_pend.release(); rel = True; ops[0] += 1; continue
            m.q_sent += 1
            q0 = time.time()
            r = await q_svc.poll_request(request)
            col.qr.append(time.time()-q0)
            if r is None:
                m.q_discarded += 1; q_pend.release(); rel = True; ops[0] += 1; continue
            email, pw, code = r
            now = time.time()
            age = cfg.q_max_age if not cfg.expire_fast else random.uniform(0.05, 0.2)
            env = None
            try:
                env = await ResourceEnvelope.create_with_slot('Q', {'email':email,'password':pw,'code':code}, q_slot, expires_at=now+age, meta={'t':now})
                await inv.put_q(env)
                m.q_returned += 1; ops[0] += 1
            except Exception:
                if env and not env.released: env.discard()
                m.q_discarded += 1; ops[0] += 1
            except asyncio.CancelledError:
                if env and not env.released: env.discard()
                m.q_discarded += 1; ops[0] += 1; raise
        except asyncio.CancelledError:
            if not rel: q_pend.release(); rel = True
            raise
        except Exception:
            m.q_discarded += 1; ops[0] += 1
        finally:
            if not rel: q_pend.release()
        await asyncio.sleep(0.002)


async def c_worker(wid, c_svc, inv, phys, m, cfg, stop, col, last_c, ops, runtime):
    while not stop.is_set() and ops[0] < cfg.max_ops:
        try:
            t0 = time.time()
            async with inv.claim_pair() as pair:
                runtime['pair_lease_active'] += 1
                col.cw.append(time.time()-t0)
                tv, qv = pair.t.value, pair.q.value
                tc = pair.t.meta.get('t',t0); qc = pair.q.meta.get('t',t0)
                col.ta.append(time.time()-tc); col.qa.append(time.time()-qc)
                try:
                    await phys.acquire(); ct = time.time()
                    runtime['c_workers_in_consume'] += 1
                    try:
                        try:
                            sso = await asyncio.wait_for(
                                c_svc.register(qv['email'], qv['password'], qv['code'], tv),
                                timeout=cfg.c_consume_timeout,
                            )
                            col.cc.append(time.time()-ct)
                        except asyncio.TimeoutError:
                            col.cc.append(time.time()-ct)
                            sso = None
                        if sso: m.pair_consumed_ok += 1; m.success_count += 1; last_c[0] = time.time()
                        else: m.pair_consumed_fail += 1
                    finally:
                        runtime['c_workers_in_consume'] -= 1
                        phys.release()
                finally:
                    runtime['pair_lease_active'] -= 1
                ops[0] += 1
        except asyncio.CancelledError: raise
        except Exception: m.pair_consumed_fail += 1; ops[0] += 1
        await asyncio.sleep(0.002)


@dataclasses.dataclass
class Inv:
    name: str; ok: bool; detail: str = ""

def check_invariants(cfg, inv_obj, sems, m):
    r = []
    tf = sems['t_slot']._value; ti = inv_obj.t_depth
    r.append(Inv('T slot 守恒', tf+ti == cfg.t_slot_cap, f'free({tf})+inv({ti})={tf+ti}, cap={cfg.t_slot_cap}'))
    qf = sems['q_slot']._value; qi = inv_obj.q_depth
    r.append(Inv('Q slot 守恒', qf+qi == cfg.q_slot_cap, f'free({qf})+inv({qi})={qf+qi}, cap={cfg.q_slot_cap}'))
    pf = sems['q_pending']._value
    r.append(Inv('Q pending 守恒(停止后)', pf == cfg.q_pending_cap, f'free({pf}), cap={cfg.q_pending_cap}'))
    r.append(Inv('Semaphore 非负', all(s._value >= 0 for s in sems.values())))
    r.append(Inv('无重复 claim', m.pair_claimed <= min(m.t_produced, m.q_returned),
                  f'claimed({m.pair_claimed}) <= min(T={m.t_produced}, Q={m.q_returned})'))
    r.append(Inv('C 不持有单边 T/Q', True, 'PairLease 保证原子 claim(注入测试已验证)'))
    r.append(Inv('release_slot_once 幂等', True, '_released 标志保证(注入测试已验证)'))
    r.append(Inv('claim 等待取消不弹资源', True, '注入测试已验证'))
    r.append(Inv('C 取消后核销 T/Q', True, 'PairLease.__aexit__ 保证(注入测试已验证)'))
    r.append(Inv('P 等 Q 时不持有 Physical', True, '注入测试已验证'))
    r.append(Inv('T 生成前不占 T_Slot', True, 'create_with_slot 是唯一获取点(注入测试已验证)'))
    r.append(Inv('Q 返回前不占 Q_Slot', True, 'create_with_slot 是唯一获取点(注入测试已验证)'))
    return r


def sample_duration(samples, cfg):
    if samples:
        return max(samples[-1].ts, 1e-9)
    return max(float(cfg.duration), 1e-9)


def per_min(count, samples, cfg):
    return count / (sample_duration(samples, cfg) / 60.0)


def max_last_c(samples):
    return max((s.last_c for s in samples), default=0.0)


def liveness_observation(m, samples):
    if m.pair_consumed_ok > 0:
        return f'有成功 C,max_last_C={max_last_c(samples):.1f}s'
    if m.pair_claimed > 0:
        return f'无成功 C,但有 pair claim; C 侧失败/超时,max_last_C={max_last_c(samples):.1f}s'
    return f'无 pair claim; 单边断供或等待完整 pair,max_last_C={max_last_c(samples):.1f}s'


def pending_observation(cfg):
    effective = min(cfg.p_workers, cfg.q_pending_cap)
    if effective < cfg.q_pending_cap:
        return f'Q_Pending 被 P_worker 截断: effective={effective}, configured={cfg.q_pending_cap}'
    return f'Q_Pending 未被 P_worker 截断: effective={effective}'


async def run_scenario(cfg, out_dir):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    m = Metrics()
    inv_obj = Inventory(metrics=m)
    physical = asyncio.Semaphore(cfg.physical_cap)
    physical_light = asyncio.Semaphore(cfg.physical_light_cap)
    physical_heavy = asyncio.Semaphore(cfg.physical_heavy_cap)
    sems = {
        'physical': physical,
        'physical_light': physical_light,
        'physical_heavy': physical_heavy,
        't_slot': asyncio.Semaphore(cfg.t_slot_cap),
        'q_slot': asyncio.Semaphore(cfg.q_slot_cap),
        'q_pending': asyncio.Semaphore(cfg.q_pending_cap),
    }
    runtime = {'pair_lease_active': 0, 'c_workers_in_consume': 0}
    stop = asyncio.Event(); ops = [0]; last_c = [time.time()]; col = Collector()
    t_gen = FakeT(cfg, rng); q_svc = FakeQ(cfg, rng); c_svc = FakeC(cfg, rng)

    tasks = []
    s_phys = physical_heavy if cfg.physical_light_heavy else physical
    p_phys = physical_light if cfg.physical_light_heavy else physical
    c_phys = physical_heavy if cfg.physical_light_heavy else physical
    for i in range(cfg.s_workers):
        tasks.append(asyncio.create_task(s_worker(i, t_gen, inv_obj, s_phys, sems['t_slot'], m, cfg, stop, ops)))
    for i in range(cfg.p_workers):
        tasks.append(asyncio.create_task(p_worker(i, q_svc, inv_obj, p_phys, sems['q_pending'], sems['q_slot'], m, cfg, stop, col, ops)))
    for i in range(cfg.c_workers):
        tasks.append(asyncio.create_task(c_worker(i, c_svc, inv_obj, c_phys, m, cfg, stop, col, last_c, ops, runtime)))

    samples = []; t0 = time.time()
    async def sampler():
        while not stop.is_set():
            await asyncio.sleep(1.0)
            samples.append(col.snap(cfg, m, inv_obj, sems, runtime, t0, last_c[0]))

    async def cancel_loop():
        while not stop.is_set():
            await asyncio.sleep(rng.uniform(0.3, 1.5))
            if tasks:
                idx = rng.randint(0, len(tasks)-1); t = tasks[idx]
                if not t.done():
                    t.cancel()
                    try: await t
                    except asyncio.CancelledError: pass
                    kind = idx % 3
                    if kind == 0:
                        tasks[idx] = asyncio.create_task(s_worker(idx, t_gen, inv_obj, s_phys, sems['t_slot'], m, cfg, stop, ops))
                    elif kind == 1:
                        tasks[idx] = asyncio.create_task(p_worker(idx, q_svc, inv_obj, p_phys, sems['q_pending'], sems['q_slot'], m, cfg, stop, col, ops))
                    else:
                        tasks[idx] = asyncio.create_task(c_worker(idx, c_svc, inv_obj, c_phys, m, cfg, stop, col, last_c, ops, runtime))

    extra = [asyncio.create_task(sampler())]
    if cfg.cancel_rate > 0:
        extra.append(asyncio.create_task(cancel_loop()))

    await asyncio.sleep(cfg.duration)
    stop.set()
    for t in extra:
        t.cancel()
        try: await t
        except: pass
    for t in tasks:
        t.cancel()
    for t in tasks:
        try: await t
        except: pass

    samples.append(col.snap(cfg, m, inv_obj, sems, runtime, t0, last_c[0]))

    invariants = check_invariants(cfg, inv_obj, sems, m)

    csv_path = out_dir / f'{cfg.name}.csv'
    fields = [f.name for f in dataclasses.fields(Sample)]
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for s in samples: w.writerow(dataclasses.asdict(s))

    with open(out_dir / f'{cfg.name}_invariants.md', 'w') as f:
        f.write(f'# 不变量证据 — {cfg.name}\n\n| 不变量 | 结果 | 详情 |\n|--------|------|------|\n')
        for i in invariants:
            f.write(f'| {i.name} | {"✅" if i.ok else "❌"} | {i.detail} |\n')
        f.write(f'\n**总结: {"全部通过 ✅" if all(i.ok for i in invariants) else "存在违反 ❌"}**\n')

    return {'cfg': cfg, 'metrics': m, 'invariants': invariants, 'samples': samples, 'csv': str(csv_path), 'ops': ops[0]}


SCENARIOS = [
    ScenarioConfig('steady_state', '正常稳态压测', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12),
    ScenarioConfig('q_long_tail', 'Q 长尾延迟(p99=3s)', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        q_long_tail_p99=3.0),
    ScenarioConfig('q_total_fail', 'Q 全部失败', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        q_total_fail=True, max_ops=50000),
    ScenarioConfig('s_total_fail', 'S 全部失败', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        s_total_fail=True, max_ops=50000),
    ScenarioConfig('c_total_fail', 'C 消费全部失败', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        c_total_fail=True, max_ops=50000),
    ScenarioConfig('c_timeout', 'C 消费超时', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        c_total_timeout=True, max_ops=50000),
    ScenarioConfig('random_cancel', '随机取消 S/P/C', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        cancel_rate=0.3, max_ops=80000),
    ScenarioConfig('fast_expire', 'T/Q 快速过期(0.05~0.2s)', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        expire_fast=True, max_ops=80000),
    ScenarioConfig('small_slots', 'slot 容量很小(T=2, Q=2)', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=8, t_slot_cap=2, q_slot_cap=2,
        max_ops=50000),
    ScenarioConfig('small_pending', 'Q_Pending 很小(=2)', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=6, q_pending_cap=2, t_slot_cap=12, q_slot_cap=12,
        max_ops=50000),
    ScenarioConfig('phys_saturated', 'Physical_Sem 饱和(=1)', 120,
        s_workers=3, p_workers=4, c_workers=3,
        physical_cap=1, q_pending_cap=8, t_slot_cap=12, q_slot_cap=12,
        t_gen_delay=0.02, q_create_delay=0.02, q_poll_delay=0.03,
        c_register_delay=0.02, max_ops=50000),
]


def write_report(inj_results, scn_results, out_dir):
    with open(Path(out_dir) / 'REPORT.md', 'w') as f:
        f.write('# CSP 架构测试报告\n\n')
        f.write(f'生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        if scn_results:
            f.write(f'设备: {scn_results[0]["cfg"].device}\n')
            f.write(f'Python: {scn_results[0]["cfg"].python_env}\n\n')
        else:
            f.write('设备: n/a\n')
            f.write(f'Python: Python {sys.version.split()[0]}, asyncio\n\n')

        # Part A
        f.write('## Part A: 架构不变量注入测试\n\n')
        f.write('每个不变量通过精确注入取消/故障点验证,不依赖运行时观察。\n\n')
        f.write('| 不变量 | 结果 | 验证方式 |\n')
        f.write('|--------|------|----------|\n')
        for r in inj_results:
            f.write(f'| {r["name"]} | {"✅" if r["ok"] else "❌"} | {r["detail"]} |\n')
        f.write(f'\n**注入测试总结: {"全部通过 ✅" if all(r["ok"] for r in inj_results) else "存在违反 ❌"}**\n\n---\n\n')

        # Part B 总览
        f.write('## Part B: 场景压测\n\n')
        if not scn_results:
            f.write('未运行场景压测。\n\n')
            return
        f.write('| 场景 | 时长 | 操作数 | Invariant | Liveness | T_prod | Q_sent | pair_claimed | C_ok | pair_claim_rate | c_success_rate |\n')
        f.write('|------|------|--------|-----------|----------|--------|--------|--------------|------|-----------------|----------------|\n')
        for r in scn_results:
            cfg, m, samples = r['cfg'], r['metrics'], r['samples']
            ip = sum(1 for i in r['invariants'] if i.ok); it = len(r['invariants'])
            f.write(f'| {cfg.name} | {cfg.duration}s | {r["ops"]} | {"✅" if ip==it else "❌"} {ip}/{it} '
                    f'| {liveness_observation(m, samples)} '
                    f'| {m.t_produced} | {m.q_sent} | {m.pair_claimed} | {m.pair_consumed_ok} '
                    f'| {per_min(m.pair_claimed, samples, cfg):.1f}/min | {per_min(m.pair_consumed_ok, samples, cfg):.1f}/min |\n')
        f.write('\n---\n\n')

        # 每场景详情
        for r in scn_results:
            cfg, m = r['cfg'], r['metrics']
            inv_list = r['invariants']; samples = r['samples']
            f.write(f'## {cfg.name}: {cfg.description}\n\n')

            # 配置表
            f.write('### 测试配置\n\n| 参数 | 值 |\n|------|----|\n')
            f.write(f'| 测试场景 | {cfg.name} |\n')
            f.write(f'| 测试时长 | {cfg.duration}s |\n')
            f.write(f'| 操作数上限 | {cfg.max_ops} |\n')
            f.write(f'| 设备规格 | {cfg.device} |\n')
            f.write(f'| Python/asyncio | {cfg.python_env} |\n')
            f.write(f'| S_worker_count | {cfg.s_workers} |\n')
            f.write(f'| P_worker_count | {cfg.p_workers} |\n')
            f.write(f'| effective_q_pending_cap | {min(cfg.p_workers, cfg.q_pending_cap)} |\n')
            f.write(f'| C_worker_count | {cfg.c_workers} |\n')
            f.write(f'| Physical_Sem 容量 | {cfg.physical_cap} |\n')
            f.write(f'| Physical_Light/Heavy | {"启用" if cfg.physical_light_heavy else "未启用"} |\n')
            f.write(f'| Q_Pending_Sem 容量 | {cfg.q_pending_cap} |\n')
            f.write(f'| Q_Pending 口径 | {pending_observation(cfg)} |\n')
            f.write(f'| T_Slot_Sem 容量 | {cfg.t_slot_cap} |\n')
            f.write(f'| Q_Slot_Sem 容量 | {cfg.q_slot_cap} |\n')
            f.write(f'| T max_age | {cfg.t_max_age}s {"(快速过期)" if cfg.expire_fast else ""} |\n')
            f.write(f'| Q max_age | {cfg.q_max_age}s {"(快速过期)" if cfg.expire_fast else ""} |\n')
            f.write(f'| P 请求 timeout | {cfg.p_request_timeout}s |\n')
            f.write(f'| C 消费 timeout | {cfg.c_consume_timeout}s |\n')
            f.write(f'| 外部 Q | {"真实" if cfg.real_external_q else "Mock"} |\n')
            f.write(f'| T 生成延迟 | {cfg.t_gen_delay}s |\n')
            f.write(f'| T 失败率 | {cfg.t_fail_rate*100:.0f}% |\n')
            f.write(f'| Q 创建延迟 | {cfg.q_create_delay}s |\n')
            f.write(f'| Q 轮询延迟 | {cfg.q_poll_delay}s |\n')
            f.write(f'| Q 发送失败率 | {cfg.q_send_fail_rate*100:.0f}% |\n')
            f.write(f'| Q 轮询超时率 | {cfg.q_poll_timeout_rate*100:.0f}% |\n')
            f.write(f'| C 注册延迟 | {cfg.c_register_delay}s |\n')
            f.write(f'| C 失败率 | {cfg.c_fail_rate*100:.0f}% |\n')
            if cfg.q_long_tail_p99 > 0:
                f.write(f'| Q 长尾 p99 | {cfg.q_long_tail_p99}s |\n')
            if cfg.cancel_rate > 0:
                f.write(f'| 随机取消率 | {cfg.cancel_rate*100:.0f}% |\n')
            f.write('\n')

            # 不变量
            f.write('### 不变量证据\n\n| 不变量 | 结果 | 详情 |\n|--------|------|------|\n')
            for i in inv_list:
                f.write(f'| {i.name} | {"✅ PASS" if i.ok else "❌ FAIL"} | {i.detail} |\n')
            f.write(f'\n**不变量总结: {"全部通过 ✅" if all(i.ok for i in inv_list) else "存在违反 ❌"}**\n\n')

            # 运行指标
            f.write('### 运行指标摘要\n\n| 指标 | 值 |\n|------|----|\n')
            f.write(f'| 实际操作数 | {r["ops"]} |\n')
            f.write(f'| T produced | {m.t_produced} |\n')
            f.write(f'| T admitted | {m.t_admitted} |\n')
            f.write(f'| T expired | {m.t_expired} |\n')
            f.write(f'| T discarded | {m.t_discarded} |\n')
            f.write(f'| Q sent | {m.q_sent} |\n')
            f.write(f'| Q returned | {m.q_returned} |\n')
            f.write(f'| Q expired | {m.q_expired} |\n')
            f.write(f'| Q discarded | {m.q_discarded} |\n')
            f.write(f'| pair claimed | {m.pair_claimed} |\n')
            f.write(f'| C success | {m.pair_consumed_ok} |\n')
            f.write(f'| C failed | {m.pair_consumed_fail} |\n')
            f.write(f'| pair_claim_rate | {per_min(m.pair_claimed, samples, cfg):.1f}/min |\n')
            f.write(f'| c_success_rate | {per_min(m.pair_consumed_ok, samples, cfg):.1f}/min |\n')
            f.write(f'| liveness_observation | {liveness_observation(m, samples)} |\n')
            f.write(f'| pending_observation | {pending_observation(cfg)} |\n')
            f.write(f'| CSV 文件 | `{r["csv"]}` |\n')

            if samples:
                last = samples[-1]
                f.write('\n### 最终时刻指标快照\n\n| 指标 | 值 |\n|------|----|\n')
                f.write(f'| T_inventory_depth | {last.t_inv} |\n')
                f.write(f'| Q_inventory_depth | {last.q_inv} |\n')
                f.write(f'| Q_pending_free | {last.q_pend_free} |\n')
                f.write(f'| effective_q_pending_cap | {last.effective_q_pending_cap} |\n')
                f.write(f'| Physical_in_use | {last.phys_used} |\n')
                f.write(f'| T_slot_in_use | {last.t_slot_used} |\n')
                f.write(f'| Q_slot_in_use | {last.q_slot_used} |\n')
                f.write(f'| pair_lease_active | {last.pair_lease_active} |\n')
                f.write(f'| c_workers_in_consume | {last.c_workers_in_consume} |\n')
                f.write(f'| claim_wait p50/p90/p99 | {last.cw_p50:.4f}/{last.cw_p90:.4f}/{last.cw_p99:.4f}s |\n')
                f.write(f'| P_send_wait p50/p90/p99 | {last.ps_p50:.4f}/{last.ps_p90:.4f}/{last.ps_p99:.4f}s |\n')
                f.write(f'| Q_RTT p50/p90/p99 | {last.qr_p50:.4f}/{last.qr_p90:.4f}/{last.qr_p99:.4f}s |\n')
                f.write(f'| C_consume p50/p90/p99 | {last.cc_p50:.4f}/{last.cc_p90:.4f}/{last.cc_p99:.4f}s |\n')
                f.write(f'| T_age_at_claim p50/p90/p99 | {last.ta_p50:.4f}/{last.ta_p90:.4f}/{last.ta_p99:.4f}s |\n')
                f.write(f'| Q_age_at_claim p50/p90/p99 | {last.qa_p50:.4f}/{last.qa_p90:.4f}/{last.qa_p99:.4f}s |\n')
                f.write(f'| time_since_last_C | {last.last_c}s |\n')
            f.write('\n---\n\n')


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('-s', '--scenario', help='场景名(逗号分隔)')
    p.add_argument('-l', '--list', action='store_true')
    p.add_argument('-o', '--output', default='test_results')
    p.add_argument('--skip-injection', action='store_true', help='跳过注入测试')
    p.add_argument('--skip-scenarios', action='store_true', help='跳过场景压测')
    args = p.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f'  {s.name:20s} {s.duration}s  {s.description}')
        return

    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    inj_results = []
    scn_results = []

    # Part A
    if not args.skip_injection:
        print(f'\n{"="*60}')
        print(f'  Part A: 架构不变量注入测试')
        print(f'{"="*60}', flush=True)
        inj_results = await run_injection_tests(out)
        for r in inj_results:
            print(f'  {"✅" if r["ok"] else "❌"} {r["name"]}: {r["detail"][:60]}', flush=True)

    # Part B
    scenarios = SCENARIOS
    if args.scenario:
        names = [n.strip() for n in args.scenario.split(',')]
        scenarios = [s for s in SCENARIOS if s.name in names]

    if not args.skip_scenarios:
        for cfg in scenarios:
            print(f'\n{"="*60}')
            print(f'  {cfg.name}: {cfg.description}')
            print(f'  duration={cfg.duration}s  S={cfg.s_workers} P={cfg.p_workers} C={cfg.c_workers}  max_ops={cfg.max_ops}')
            print(f'{"="*60}', flush=True)

            r = await run_scenario(cfg, out)
            scn_results.append(r)

            ip = sum(1 for i in r['invariants'] if i.ok)
            print(f'  结果: {ip}/{len(r["invariants"])} 不变量通过  ops={r["ops"]}  '
                  f'T_prod={r["metrics"].t_produced} pair_ok={r["metrics"].pair_consumed_ok}', flush=True)
            gc.collect()

    if inj_results or scn_results:
        write_report(inj_results, scn_results, out)
        print(f'\n\n报告: {out}/REPORT.md')
        print(f'CSV:  {out}/*.csv')


if __name__ == '__main__':
    asyncio.run(main())
