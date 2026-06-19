import asyncio
from pathlib import Path

import pytest

from core.observer import Metrics
from run_tests import Inv, Sample, ScenarioConfig, run_scenario, write_report


@pytest.mark.asyncio
async def test_c_timeout_is_recorded_as_failed_consumption(tmp_path: Path):
    cfg = ScenarioConfig(
        name='c_timeout_regression',
        description='C timeout regression',
        duration=1.2,
        max_ops=100,
        s_workers=1,
        p_workers=1,
        c_workers=1,
        physical_cap=1,
        q_pending_cap=1,
        t_slot_cap=2,
        q_slot_cap=2,
        t_gen_delay=0.001,
        q_create_delay=0.001,
        q_poll_delay=0.001,
        t_fail_rate=0.0,
        q_send_fail_rate=0.0,
        q_poll_timeout_rate=0.0,
        c_total_timeout=True,
        c_consume_timeout=0.05,
    )

    result = await run_scenario(cfg, tmp_path)

    assert result['metrics'].pair_claimed > 0
    assert result['metrics'].pair_consumed_fail > 0
    assert result['samples'][-1].c_workers_in_consume == 0
    assert result['samples'][-1].pair_lease_active == 0


def test_report_uses_explicit_rates_from_sample_duration(tmp_path: Path):
    cfg = ScenarioConfig(
        name='report_rate_regression',
        description='Report rate regression',
        duration=10,
    )
    metrics = Metrics()
    metrics.t_produced = 10
    metrics.q_sent = 10
    metrics.q_returned = 10
    metrics.pair_claimed = 10
    metrics.pair_consumed_ok = 8
    metrics.pair_consumed_fail = 2

    sample = Sample(
        ts=10.0,
        t_inv=0,
        q_inv=0,
        q_pend_free=cfg.q_pending_cap,
        effective_q_pending_cap=min(cfg.p_workers, cfg.q_pending_cap),
        phys_used=0,
        t_slot_used=0,
        q_slot_used=0,
        pair_lease_active=0,
        c_workers_in_consume=0,
        t_produced=10,
        q_sent=10,
        q_ret=10,
        pair_claimed=10,
        c_ok=8,
        c_fail=2,
        t_expired=0,
        q_expired=0,
        t_disc=0,
        q_disc=0,
        cw_p50=0.0,
        cw_p90=0.0,
        cw_p99=0.0,
        ps_p50=0.0,
        ps_p90=0.0,
        ps_p99=0.0,
        qr_p50=0.0,
        qr_p90=0.0,
        qr_p99=0.0,
        cc_p50=0.0,
        cc_p90=0.0,
        cc_p99=0.0,
        ta_p50=0.0,
        ta_p90=0.0,
        ta_p99=0.0,
        qa_p50=0.0,
        qa_p90=0.0,
        qa_p99=0.0,
        last_c=0.0,
    )

    write_report(
        inj_results=[],
        scn_results=[{
            'cfg': cfg,
            'metrics': metrics,
            'invariants': [Inv('dummy', True, '')],
            'samples': [sample],
            'csv': str(tmp_path / 'report_rate_regression.csv'),
            'ops': 10,
        }],
        out_dir=tmp_path,
    )

    report = (tmp_path / 'REPORT.md').read_text()
    assert '| pair_claim_rate | 60.0/min |' in report
    assert '| c_success_rate | 48.0/min |' in report
    assert '| effective_q_pending_cap | 4 |' in report
