# grok-free-register

`grok-free-register` 是一个命令行注册工具。程序会启动本机浏览器，完成页面操作、邮箱验证码处理和结果保存。

运行结果写入 `keys/` 目录。

## 快速开始

```bash
git clone https://github.com/hechuyi/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会自动创建 `.venv`、安装依赖，并引导生成 `.env`。

常用命令：

```bash
bash run.sh                 # 按当前 .env 运行
bash run.sh --target 100    # 成功 100 个后停止
bash run.sh --max-mem 6G    # 自动估算并发时最多使用 6G 内存
bash start.sh --reconfig    # 重新选择邮箱模式
```

需要代理时，在 `.env` 中加入：

```env
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

## 邮箱模式

`tempmail` 是默认模式，不需要额外配置，适合快速试跑：

```env
EMAIL_MODE=tempmail
```

`custom` 是自建域名邮箱模式，适合长时间运行。需要一个已接入 Cloudflare Email Routing 的域名，并在运行机器上启动本项目的收信服务。

配置步骤：

1. 在 Cloudflare 为域名开启 Email Routing。
2. 部署 `cloudflare/email-worker.js`。
3. 在 Email Routing 中配置 catch-all，动作选择发送到该 Worker。
4. 在运行机器上启动收信服务：

```bash
.venv/bin/python email_server.py
```

5. 在 `.env` 中配置：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

如果 Worker 需要回调本机服务，`WEBHOOK_URL` 应使用可访问的域名地址。

## 配置

完整模板见 `.env.example`。日常使用通常只需要配置邮箱模式、代理、目标数量和内存预算。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `EMAIL_MODE` | `tempmail` | 邮箱模式，支持 `tempmail` 和 `custom` |
| `EMAIL_DOMAIN` | 空 | `custom` 模式使用的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | 本地收信服务地址 |
| `TARGET` | `0` | 成功数量目标，`0` 表示不限 |
| `PHYSICAL_CAP` | `0` | 浏览器并发上限，`0` 表示启动时自动估算 |
| `PHYSICAL_PER_CPU` | `2` | 自动估算时每个 CPU 核心对应的并发参考值 |
| `PHYSICAL_MEM_MB` | `512` | 自动估算时每个浏览器任务的内存预算 |
| `MIN_FREE_MEM_MB` | `500` | 自动估算时保留的内存 |
| `T_SLOT_CAP` | `8` | token 缓冲容量 |
| `Q_SLOT_CAP` | `8` | 验证码缓冲容量 |
| `Q_PENDING_CAP` | `12` | 等待验证码返回的请求上限 |
| `SOLVER_MOUSE_CLICK_RETRIES` | `3` | token 验证框中心点击次数，`0` 表示关闭 |
| `PAGE_BLOCK_STATIC_ASSETS` | `0` | 可选：阻断部分静态资源，降低页面准备成本 |
| `C_HOT_PAGE_POOL` | `0` | 可选：复用消费阶段页面，减少页面重建开销 |

不确定怎么设置时，先保持默认值。性能压测时优先观察 `PHYSICAL_CAP` 和内存，不建议先改 Worker 数量。

## 运行日志

运行时会定期输出状态行，例如：

```text
[*] T:0 Q:6 phys:0 s_phys:0.10/11.30 p_stage:0.02/1.10/1.70 c_stage:0.02/0.30/0.80 t_solve_avg:11.8 q_sent:44 q_ret:44 pair:38 ok:37 fail:0 rate:9.9/min #37
```

常用字段：

| 字段 | 含义 |
|---|---|
| `T` | 当前可用 token 数量 |
| `Q` | 当前可用验证码数量 |
| `phys` | 空闲浏览器并发许可 |
| `s_phys` / `p_phys` / `c_phys` | S/P/C 获取浏览器许可的平均等待秒数 / 平均持有秒数 |
| `p_stage` | P 阶段平均耗时：建邮箱 / 准备页面 / 发送请求 |
| `c_stage` | C 阶段平均耗时：拿页面 / 验证码校验 / 注册提交 |
| `c_hot` | C 热页池命中 / 未命中次数 |
| `t_solve_avg` | 平均 token 获取时间 |
| `q_sent` / `q_ret` | 已发送 / 已收到的验证码数量 |
| `pair` | 已配对消费次数 |
| `ok` / `fail` | 成功 / 失败数量 |
| `rate` | 当前累计成功速率 |

简单判断：

- `T` 长期为 `0` 且 `Q` 有库存，通常是 token 获取较慢。
- `Q` 长期为 `0` 且 `T` 有库存，通常是邮箱或验证码链路较慢。
- `phys` 长期为 `0`，说明浏览器并发已经用满。
- `t_solve_avg` 明显升高，通常表示浏览器压力、网络质量或 token 服务响应变慢。

可以用日志分析工具解析已有日志：

```bash
python3 - <<'PY'
from pathlib import Path
from runtime_log_analyzer import analyze_text
print(analyze_text(Path("run.log").read_text()))
PY
```

## 输出文件

成功结果写入：

```text
keys/accounts.txt
keys/grok.txt
```

`accounts.txt` 每行格式：

```text
email:password:sso_token
```

`keys/` 目录包含运行结果，默认不会提交到 Git。

## 项目结构

```text
register.py                 主运行入口
email_server.py             custom 模式的本地收信服务
cloudflare/email-worker.js  Cloudflare Email Routing Worker 示例
start.sh                    首次配置和运行
run.sh                      按当前配置运行
setup.sh                    安装依赖
.env.example                配置模板
runtime_log_analyzer.py     运行日志分析工具
tests/                      自动化测试
docs/architecture.md        并发架构说明
```

## 测试

快速检查：

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

完整测试：

```bash
python3 -m pytest tests -q
```

场景压测：

```bash
python3 run_tests.py
python3 run_tests.py --list
```

`run_tests.py` 默认输出到 `test_results/`，该目录是生成物。

## 开发文档

[docs/architecture.md](docs/architecture.md) 记录并发模型、资源生命周期和必须保持的不变量。

## License

MIT
