# grok-free-register

自动化注册 Grok (x.ai) 账号的工具。当前版本采用 CSP 风格的异步并发架构:固定 S/P/C Worker、Semaphore 背压、Inventory 原子配对,不再使用中央调度器或运行时动态并发伸缩。

> 仅供学习与研究使用。请遵守目标站点的服务条款与当地法律。

## 当前架构

系统拆成三类长期运行 Worker:

- `S_Worker`: 生成 Turnstile token,记为 `T`。
- `P_Worker`: 创建邮箱、发起验证码请求、等待验证码返回,记为 `Q`。
- `C_Worker`: 从 `Inventory` 原子 claim `1 个 T + 1 个 Q`,完成注册消费。

资源边界全部由 Semaphore 表达:

- `Physical_Sem`: 本地浏览器页面/重操作并发。
- `T_Slot_Sem`: `T` 库存容量。
- `Q_Slot_Sem`: 已返回 `Q` 库存容量。
- `Q_Pending_Sem`: 外部验证码请求在途数量。

关键约束:

- 没有 `Scheduler`、`pick_role()`、动态打分或中心化角色分配。
- `P_Worker` 等待验证码期间不持有 `Physical_Sem`。
- `Q_Slot_Sem` 只在验证码真正返回后获取。
- `C_Worker` 不先拿单边资源等待另一边,只通过 `Inventory.claim_pair()` 获取完整 pair。
- `ResourceEnvelope` 绑定资源和 slot,保证 slot 只释放一次。
- `PairLease` 用 async context manager 保护 claim 后的取消/异常核销。

## 快速开始

```bash
git clone <this-repo>
cd grok-free-register
bash start.sh
```

首次运行会创建 `.venv` 并安装依赖。建议在海外网络环境运行;如需代理,在 `.env` 里设置 `HTTP_PROXY` / `HTTPS_PROXY`。

常用命令:

```bash
bash start.sh --reconfig      # 重新选择邮箱模式
bash run.sh --target 100      # 攒够 100 个成功账号后停止
bash run.sh --max-mem 6G      # 自动容量派生时最多使用 6G 内存预算
```

高级用户可手动配置:

```bash
cp .env.example .env
vim .env
bash run.sh
```

## 邮箱模式

### tempmail

默认模式,零配置。程序会尝试多个免费临时邮箱 provider 作为 fallback。适合快速试跑,稳定性取决于公共 provider。

```env
EMAIL_MODE=tempmail
```

### custom

自建域名邮箱模式,更适合长时间运行。链路如下:

```text
oc<random>@example.com
  -> Cloudflare Email Routing
  -> Cloudflare Email Worker
  -> email_server.py /webhook
  -> register.py 轮询 /check/<email>
```

步骤:

1. 域名接入 Cloudflare,开启 Email Routing。
2. 部署 `cloudflare/email-worker.js`,把 `WEBHOOK_URL` 配成你的收信服务地址。
3. 在 Email Routing 里配置 catch-all,动作选择 Send to a Worker。
4. 服务器上启动收信服务:

```bash
.venv/bin/python email_server.py
```

5. `.env` 配置:

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

注意:Cloudflare Worker 的 `WEBHOOK_URL` 必须使用域名,不能用裸 IP。Cloudflare Workers 对裸 IP fetch 会报 `1003 Direct IP access not allowed`。

## 配置说明

完整示例见 `.env.example`。

### 基础

| 键 | 默认 | 说明 |
|---|---:|---|
| `EMAIL_MODE` | `tempmail` | `tempmail` 或 `custom` |
| `EMAIL_DOMAIN` | 空 | custom 模式使用的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | 本地收信服务地址 |
| `TARGET` | `0` | 成功数量目标,0 表示不限 |

### CSP 容量

| 键 | 默认 | 说明 |
|---|---:|---|
| `PHYSICAL_CAP` | `0` | 本地浏览器重操作并发,0 表示启动期自动派生 |
| `PHYSICAL_PER_CPU` | `2` | 自动派生时 CPU 侧上限,`cpu * PHYSICAL_PER_CPU` |
| `PHYSICAL_MEM_MB` | `512` | 自动派生时每个物理许可的内存预算 |
| `MIN_FREE_MEM_MB` | `500` | 自动派生时预留的内存 |
| `CAPACITY_PROFILE` | 空 | 可选离线压测 profile JSON,只在启动期读取 |
| `T_SLOT_CAP` | `8` | `T` 库存容量 |
| `Q_SLOT_CAP` | `8` | 已返回 `Q` 库存容量 |
| `Q_PENDING_CAP` | `12` | 外部验证码请求在途上限 |

容量优先级:

```text
显式 PHYSICAL_CAP > CAPACITY_PROFILE > CPU/内存自动派生
```

`CAPACITY_PROFILE` 是静态启动配置,不是运行时调度器。它不会在程序运行中动态调整并发。

### Worker 数量

| 键 | 默认 | 说明 |
|---|---:|---|
| `S_WORKERS` | `0` | 0 表示 `Physical_Sem + 2` |
| `P_WORKERS` | `0` | 0 表示 `Q_Pending_Sem + 2` |
| `C_WORKERS` | `0` | 0 表示 `Physical_Sem + 2` |

通常不需要手动设置 Worker 数量。优先调整资源容量,不要把 Worker 数当吞吐旋钮。

### Admission 水位

| 键 | 默认 | 说明 |
|---|---:|---|
| `T_HIGH_WATER` | 自动 | 默认 `min(T_SLOT_CAP, max(T_TARGET, Physical_Sem))` |
| `T_LOW_WATER` | 自动 | 默认 `T_HIGH_WATER // 2` |
| `Q_HIGH_WATER` | 自动 | 默认 `Q_PENDING_CAP` |
| `Q_LOW_WATER` | 自动 | 默认 `min(Q_TARGET, Q_HIGH_WATER // 2)` |
| `P_BATCH_MAX` | `4` | 单个 P 发送页面最多批量发码数 |
| `P_SEND_CAP` | `0` | 0 表示不额外限制 P 发送页面;>0 为显式静态限制 |

`AdmissionGate` 只是局部生产准入门控,不选择角色,不移动资源,不替代 CSP 背压模型。

### TTL 和超时

| 键 | 默认 | 说明 |
|---|---:|---|
| `T_MAX_AGE` | `300` | token 最大年龄,秒 |
| `Q_MAX_AGE` | `120` | 验证码最大年龄,秒 |
| `P_REQUEST_TIMEOUT` | `95` | P 等待验证码返回超时,秒 |
| `C_CONSUME_TIMEOUT` | `60` | C 消费完整 pair 超时,秒 |

过期清理采用 lazy cleanup:只在 `put` / `claim` 被触发时清理过期资源。系统完全静默时不会主动扫库;单边故障和长期静默应由监控暴露。

### Solver

| 键 | 默认 | 说明 |
|---|---:|---|
| `SOLVER_REUSE` | `1` | 复用停在 sign-up 的 solver 页面 |
| `MAX_SOLVER_REUSE` | `25` | 单个 solver 页面最大复用次数 |
| `SOLVER_INITIAL_WAIT_MS` | `1500` | 注入 Turnstile 后首次等待 |
| `SOLVER_POLL_INTERVAL_MS` | `500` | token 轮询间隔 |
| `SOLVER_POLL_ATTEMPTS` | `100` | 最大轮询次数 |

当前 solver 是浏览器页面内注入 Turnstile widget 并等待 token callback。页面复用可以减少重复导航成本,但主瓶颈通常仍是 token 等待时间和 Chrome renderer CPU。

## 监控日志

监控行示例:

```text
[*] T:0 Q:6 phys:0 t_slot:7 q_slot:1 q_pend:12 p_batch:4.0 t_prog:5 q_inflight:0 t_solve_avg:23.7 t_solve_fail:0 t_prod:38 t_adm:38 t_exp:0 q_sent:44 q_ret:44 q_adm:44 q_exp:0 pair:38 ok:37 fail:0 rate:9.9/min #37
```

字段含义:

| 字段 | 含义 |
|---|---|
| `T` / `Q` | 当前库存深度 |
| `phys` | 空闲 `Physical_Sem` 数 |
| `t_slot` / `q_slot` | 空闲库存 slot |
| `q_pend` | 空闲外部请求 pending 许可 |
| `p_batch` | P 发码批量平均值 |
| `t_prog` | AdmissionGate 中正在生产的 T |
| `q_inflight` | AdmissionGate 中已准入但未终态的 Q |
| `t_solve_avg` | 平均 token solve 时间 |
| `t_solve_fail` | token solve 失败次数 |
| `t_prod` / `t_adm` / `t_exp` | T 产生 / 入库 / 过期 |
| `q_sent` / `q_ret` / `q_adm` / `q_exp` | Q 发出 / 返回 / 入库 / 过期 |
| `pair` | claim pair 次数 |
| `ok` / `fail` | C 消费成功 / 失败 |
| `rate` / `#` | 累计成功速率 / 累计成功数 |

常见判断:

- `T:0` 长期存在且 `Q` 有库存:瓶颈在 S/token 生成。
- `Q:0` 长期存在且 `T` 有库存:瓶颈在 P/验证码链路。
- `phys:0` 长期存在:本地浏览器重资源被打满。
- `t_solve_avg` 升高且失败增加:solver 并发可能过高或页面/网络质量变差。

## 测试

轻量单元测试:

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

pytest 测试:

```bash
python3 -m pytest tests -q
python3 -m pytest tests -q -m "not slow"
```

场景压测和报告:

```bash
python3 run_tests.py
python3 run_tests.py -s steady_state
python3 run_tests.py --list
```

`run_tests.py` 默认输出到 `test_results/`,该目录是生成物,不建议提交。

日志分析:

```bash
python3 - <<'PY'
from pathlib import Path
from runtime_log_analyzer import analyze_text
print(analyze_text(Path('/tmp/csp_run.log').read_text()))
PY
```

## 输出

成功账号写入:

```text
keys/accounts.txt
keys/grok.txt
```

`accounts.txt` 每行格式:

```text
email:password:sso_token
```

`keys/` 包含真实账号凭证,已在 `.gitignore` 中排除,不要提交。

## 项目结构

```text
register.py                 主运行入口,CSP worker 和真实注册流程
core/
  admission.py              局部生产准入门控
  envelope.py               ResourceEnvelope,资源与 slot 绑定
  inventory.py              Inventory + PairLease
  observer.py               只读指标和监控行
runtime_log_analyzer.py     运行日志解析与阶段速率统计
run_tests.py                架构不变量和场景压测 runner
tests/                      单元、取消、性质、压力测试
email_server.py             custom 邮箱模式的本地收信服务
cloudflare/email-worker.js  Cloudflare Email Routing Worker 示例
start.sh                    首次配置 + 运行
run.sh                      运行入口
setup.sh                    安装依赖
.env.example                配置示例
```

## 参数探索建议

不要逐设备手工乱调所有参数。建议分层处理:

- 自动派生:默认使用 CPU/内存预算派生 `PHYSICAL_CAP`。
- 显式覆盖:只在压测证明默认值不合适时设置 `PHYSICAL_CAP`。
- 离线 profile:可把压测得到的稳定值写入 `CAPACITY_PROFILE`,启动期读取。
- 不建议作为第一优先级手调:`S_WORKERS`、`P_WORKERS`、`C_WORKERS`。

真实压测时至少观察:

- 最终 `rate` 和最近窗口成功率。
- `fail` 是否增加。
- `t_solve_avg` 和 `t_solve_fail`。
- `T/Q` 哪一侧长期为空。
- CPU 和内存是否被 Chrome renderer 打满。

## License

MIT
