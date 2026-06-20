# 技术架构说明

本文档说明 `register.py` 的并发运行模型。README 只保留用户侧使用说明，架构约束、资源生命周期和测试不变量放在这里维护。

## 设计目标

运行时采用 CSP 风格的异步流水线。核心目标不是构建通用调度器，而是让资源所有权闭合、背压边界明确、取消语义可测试。

系统包含三类长期运行 worker：

- `S_Worker` 生产资源 `T`。
- `P_Worker` 发起请求并等待资源 `Q` 返回。
- `C_Worker` 消费一组完整的 `T + Q`。

`T` 和 `Q` 只由 `Inventory` 配对。worker 不直接访问底层队列。

## 运行组件

`Physical_Sem` 限制本地浏览器重操作并发。

`T_Slot_Sem` 限制已入库 `T` 的容量。

`Q_Slot_Sem` 限制已返回并入库 `Q` 的容量。

`Q_Pending_Sem` 限制已发出但尚未终态的外部 `Q` 请求数量。

`Inventory` 拥有 `T` / `Q` 队列，只暴露三个入口：

- `put_t(env)`
- `put_q(env)`
- `claim_pair()`

`ResourceEnvelope` 把资源实体和库存 slot 绑定在一起。无论正常消费、超时、丢弃还是取消，slot 都只能释放一次。

`PairLease` 是 `Inventory.claim_pair()` 返回的异步上下文管理器。pair 一旦 claim 成功，两个 envelope 的所有权就转移给 lease，直到 consumer 退出上下文。

`AdmissionGate` 是局部生产准入门控。它根据静态水位和库存深度决定是否启动更多生产，但不选择 worker 角色、不搬运资源、不在运行时调整并发。

## Worker 流程

### S_Worker

1. 等待 `AdmissionGate` 允许生产 `T`。
2. 获取 `Physical_Sem`。
3. 生产 `T`。
4. 释放 `Physical_Sem`。
5. 通过 `ResourceEnvelope.create_with_slot(...)` 创建 envelope，并获取 `T_Slot_Sem`。
6. 调用 `Inventory.put_t(...)`，把所有权转移给 `Inventory`。
7. 如果所有权转移前失败，丢弃 envelope 或释放已获取的许可。

slot 获取和 envelope 创建必须绑定在一个工厂方法里，避免取消落在“已获取 slot、尚未创建 envelope”的窗口时泄漏许可。

### P_Worker

1. 等待 `AdmissionGate` 允许生产 `Q`。
2. 获取 `Q_Pending_Sem`。
3. 获取 `Physical_Sem`。
4. 创建邮箱并发起外部请求。
5. 释放 `Physical_Sem`。
6. 在不持有 `Physical_Sem` 的情况下等待 `Q` 返回。
7. 通过 `ResourceEnvelope.create_with_slot(...)` 创建 envelope，并获取 `Q_Slot_Sem`。
8. 调用 `Inventory.put_q(...)`，把所有权转移给 `Inventory`。
9. 请求进入终态后释放 `Q_Pending_Sem`。

`Q_Slot_Sem` 只能在 `Q` 真正返回后获取。外部在途请求由 `Q_Pending_Sem` 约束，不能提前占用已返回库存 slot。

### C_Worker

1. 进入 `async with inventory.claim_pair() as pair`。
2. 获取 `Physical_Sem`。
3. 消费 pair。
4. 释放 `Physical_Sem`。
5. 退出 `PairLease`，两个库存 slot 各释放一次。

`C_Worker` 不允许先取单边资源再等待另一边。对 consumer 来说，pair claim 是原子的。

## Inventory 语义

第一版 `Inventory` 使用一把 lock 和一个 condition。lock 保护等待、复查、过期清理和弹出操作。这样 pairing 规则保持简单：

- 等待中的 consumer 不移除资源。
- claim 成功时同时移除一个有效 `T` 和一个有效 `Q`。
- 等待 pair 时被取消，不影响库存。
- claim 成功后被取消，由 `PairLease` 负责核销。

只要锁内逻辑保持很小，除 lazy expiry cleanup 外基本是 O(1)，单锁在第一版可以接受。如果后续 profile 证明锁竞争成为真实瓶颈，再基于数据优化，而不是提前破坏所有权模型。

## 过期模型

`T` 和 `Q` envelope 可以携带 `created_at` 和 `expires_at`。`Inventory` 在配对前可以丢弃已过期资源。

第一版采用 lazy cleanup：

- `put_t`、`put_q`、`claim_pair` 被触发时顺手清理。
- 清理会释放它看到的过期 envelope 对应 slot。
- 系统完全静默时不会主动扫库。
- 单边长期故障和静默停摆由监控暴露，不靠后台 sweeper 修复。

这个取舍可以避免引入常驻清理模块，同时保证正常活跃运行中不会配对明显过期的资源。

## 容量策略

容量边界由 Semaphore 表达。启动时容量优先级是：

```text
显式 PHYSICAL_CAP > CAPACITY_PROFILE > CPU/内存自动派生
```

`CAPACITY_PROFILE` 只在启动期读取。它是静态 profile，不是运行时负载调度器。

worker 数量默认由容量派生：

- `S_WORKERS = Physical_Sem + 2`
- `P_WORKERS = Q_Pending_Sem + 2`
- `C_WORKERS = Physical_Sem + 2`

worker 数量不是优先调参项。主要并发边界是容量许可，而不是 coroutine 循环数量。

## 第一版明确不做

第一版不包含：

- 中心调度器；
- 运行时角色选择；
- 动态打分；
- 动态并发控制；
- worker 级回队；
- 高价值资源抢救策略；
- 后台过期清扫；
- 自动切换高风险浏览器模式。

这些能力不是永久禁止，但必须在单独实验中证明有效，不能混进基础所有权模型。

## 必须保持的不变量

实现和测试必须维持以下不变量：

- 每个已获取的库存 slot 最终释放一次且只释放一次。
- 每个已准入的 pending 请求最终释放一次 `Q_Pending_Sem`。
- `P_Worker` 等待 `Q` 返回时不持有 `Physical_Sem`。
- `Q_Slot_Sem` 只在 `Q` 返回后获取。
- `C_Worker` 只能通过 `Inventory.claim_pair()` 获取 `T` 和 `Q`。
- `claim_pair()` 要么返回一个受 `PairLease` 保护的完整 pair，要么不返回资源。
- 等待 pair 时取消，不移除库存资源。
- claim pair 后取消，两个 envelope 由 `PairLease` 释放。
- 触发清理时，过期资源不能被配对。
- 监控只读，不修改 Semaphore、队列或 worker 状态。

## 测试范围

相关测试组：

- `tests.test_inventory_unittest`：库存、过期和 lease 行为。
- `tests.test_register_runtime_unittest`：worker 运行语义和监控行。
- `tests.test_admission_gate`：局部门控水位。
- `tests.test_runtime_log_analyzer`：日志解析兼容性。
- `tests/test_cancel.py`：取消边界。
- `tests/test_property.py`：随机化不变量检查。
- `tests/test_stress.py`：更高并发 fake-service 压测。

推荐快速检查：

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

完整 pytest：

```bash
python3 -m pytest tests -q
```

场景 runner：

```bash
python3 run_tests.py
```

## 性能判断

吞吐判断应基于真实日志，而不是只看理论值。至少需要比较：

- 最终累计 `rate`；
- 最近窗口成功率；
- `fail`；
- `t_solve_avg`；
- `T:0` 或 `Q:0` 是否长期存在；
- `Physical_Sem` 是否长期打满；
- 浏览器进程造成的 CPU 和内存压力。

当前证据显示，`T` 生产经常是主瓶颈。提高浏览器并发在某个点之前可能提升吞吐，但超过机器和浏览器渲染能力后，token 延迟会上升，整体吞吐反而会下降。

## 已进入默认路径的性能优化

默认运行路径包含三类低风险优化：

- `SOLVER_FAST_CLICK=1`：注入 `T` widget 后，如果没有发现可见验证 frame，就不再等待慢点击超时，直接进入 token 轮询。
- `SOLVER_INITIAL_WAIT_MS=500`：缩短注入后的首次固定等待。
- `PAGE_GOTO_WAIT_UNTIL=domcontentloaded` 与 `PAGE_POST_WAIT_MS=500`：`P_Worker` 和 `C_Worker` 的注册页面准备只等待 DOM 可用，再保留一个短固定等待窗口。

这些优化只改变单个 worker 内部的固定步骤耗时，不引入中心调度器、不改变 `Inventory` 所有权语义，也不在运行时动态调整容量。

可选性能路径：

- `C_HOT_PAGE_POOL=1`：`C_Worker` 可以复用停在注册页的隔离页面 context，并在每次消费后清理 cookies、localStorage 和 sessionStorage。
- `C_SET_COOKIE_VIA_REQUEST=1`：注册成功后用浏览器 context 的 request 访问 set-cookie URL，避免把热页导航离开注册页；如果没有拿到 `sso`，仍回退到原导航路径。

这个优化仍然发生在单个 `C_Worker` 的 pair lease 内，不改变 `Inventory` 配对语义，也不引入中心调度。真实 10 分钟 A/B 中，关闭热页池时吞吐约 `15.5/min`、C 消费平均耗时约 `7.5s`、浏览器 RSS 峰值约 `4.0GB`；开启热页池后，样本吞吐约 `22/min`、C 消费平均耗时约 `2.2s`。本测试服务器的运行 profile 采用 `C_HOT_PAGE_POOL=1`、`C_HOT_PAGE_POOL_SIZE=3`、`C_SET_COOKIE_VIA_REQUEST=1`；这只是该服务器的静态 profile，不是通用最优参数。通用仓库默认仍保持关闭，池大小需要按机器内存做压测或使用启动期派生值。

以下实验方向不属于默认路径：请求后端替代页面后端、等待 `T` 时保留更多活页面、更高默认物理并发。它们只能作为单独压测项处理。
