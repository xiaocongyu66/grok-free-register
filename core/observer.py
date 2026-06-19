"""
Observer — 只读观测层

只记录指标、生成日志。不修改 Semaphore,不调度 Worker,不释放资源。
"""
import time


class Metrics:
    """全局指标收集器。所有字段只在 asyncio 单线程中写入,无需加锁。"""

    __slots__ = (
        'start_time',
        't_produced', 't_admitted', 't_claimed', 't_expired', 't_discarded',
        't_solve_count', 't_solve_seconds', 't_solve_failed',
        'q_sent', 'q_returned', 'q_admitted', 'q_claimed', 'q_expired', 'q_discarded',
        'q_send_batches', 'q_send_batch_items',
        'pair_claimed', 'pair_consumed_ok', 'pair_consumed_fail',
        'success_count',
    )

    def __init__(self):
        self.start_time = time.time()
        # T 生命周期
        self.t_produced = 0
        self.t_admitted = 0
        self.t_claimed = 0
        self.t_expired = 0
        self.t_discarded = 0
        self.t_solve_count = 0
        self.t_solve_seconds = 0.0
        self.t_solve_failed = 0
        # Q 生命周期
        self.q_sent = 0
        self.q_returned = 0
        self.q_admitted = 0
        self.q_claimed = 0
        self.q_expired = 0
        self.q_discarded = 0
        self.q_send_batches = 0
        self.q_send_batch_items = 0
        # Pair
        self.pair_claimed = 0
        self.pair_consumed_ok = 0
        self.pair_consumed_fail = 0
        # 成功数
        self.success_count = 0

    def snapshot(self, inventory, sems):
        """生成一行监控日志。"""
        elapsed = time.time() - self.start_time
        rate = self.success_count / (elapsed / 60) if elapsed > 60 else 0
        p_batch_avg = (
            self.q_send_batch_items / self.q_send_batches
            if self.q_send_batches else 0
        )
        t_solve_avg = (
            self.t_solve_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        p_send = sems.get("p_send")
        admission = sems.get("admission")
        p_send_part = f' p_send:{p_send._value}' if p_send is not None else ''
        admission_part = (
            f' t_prog:{admission.t_in_progress} q_inflight:{admission.q_inflight}'
            if admission is not None else ''
        )
        return (
            f'[*] T:{inventory.t_depth} Q:{inventory.q_depth} '
            f'phys:{sems["physical"]._value}{p_send_part} t_slot:{sems["t_slot"]._value} '
            f'q_slot:{sems["q_slot"]._value} q_pend:{sems["q_pending"]._value} '
            f'p_batch:{p_batch_avg:.1f}{admission_part} '
            f't_solve_avg:{t_solve_avg:.1f} t_solve_fail:{self.t_solve_failed} '
            f't_prod:{self.t_produced} t_adm:{self.t_admitted} t_exp:{self.t_expired} '
            f'q_sent:{self.q_sent} q_ret:{self.q_returned} q_adm:{self.q_admitted} q_exp:{self.q_expired} '
            f'pair:{self.pair_claimed} ok:{self.pair_consumed_ok} fail:{self.pair_consumed_fail} '
            f'rate:{rate:.1f}/min #{self.success_count}'
        )
