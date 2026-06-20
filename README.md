# grok-free-register

`grok-free-register` 是一个命令行注册工具。它会在本机启动浏览器，完成注册页面、邮箱验证码和结果保存流程。

支持两种邮箱模式：

- `tempmail`：默认模式，不需要额外配置，适合快速试跑。
- `custom`：自建域名邮箱模式，适合长时间运行。

运行结果会写入 `keys/` 目录。

## 快速开始

```bash
git clone https://github.com/hechuyi/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会创建 `.venv`、安装依赖，并按提示生成 `.env` 配置。

常用命令：

```bash
bash run.sh                 # 按当前配置运行
bash run.sh --target 100    # 成功 100 个账号后停止
bash run.sh --max-mem 6G    # 自动估算并发时最多使用 6G 内存预算
bash start.sh --reconfig    # 重新选择邮箱模式
```

如果需要代理，在 `.env` 中配置：

```env
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

## 邮箱模式

### tempmail

默认模式，零配置。程序会尝试多个免费临时邮箱服务。

```env
EMAIL_MODE=tempmail
```

这个模式适合测试能否正常跑通，但公共邮箱服务的稳定性不可控。

### custom

自建域名邮箱模式。你需要一个接入 Cloudflare Email Routing 的域名，并在服务器上运行本项目自带的收信服务。

基本流程：

```text
注册邮箱地址
  -> Cloudflare Email Routing
  -> Cloudflare Email Worker
  -> email_server.py
  -> register.py
```

配置步骤：

1. 在 Cloudflare 中为域名开启 Email Routing。
2. 部署 `cloudflare/email-worker.js`。
3. 在 Email Routing 中配置 catch-all，并把动作设为发送到该 Worker。
4. 在服务器上启动收信服务：

```bash
.venv/bin/python email_server.py
```

5. 在 `.env` 中配置：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

`WEBHOOK_URL` 需要使用域名地址，不要直接使用裸 IP。

## 常用配置

完整配置示例见 `.env.example`。多数情况下只需要改邮箱模式、目标数量和代理。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `EMAIL_MODE` | `tempmail` | 邮箱模式，支持 `tempmail` 和 `custom` |
| `EMAIL_DOMAIN` | 空 | `custom` 模式使用的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | 本地收信服务地址 |
| `TARGET` | `0` | 成功数量目标，`0` 表示不限 |
| `PHYSICAL_CAP` | `0` | 浏览器并发上限，`0` 表示启动时自动估算 |
| `PHYSICAL_PER_CPU` | `2` | 自动估算并发时的 CPU 侧参考值 |
| `PHYSICAL_MEM_MB` | `512` | 自动估算并发时每个浏览器任务的内存预算 |
| `T_SLOT_CAP` | `8` | token 缓冲容量 |
| `Q_SLOT_CAP` | `8` | 验证码缓冲容量 |
| `Q_PENDING_CAP` | `12` | 等待验证码返回的请求上限 |
| `SOLVER_INITIAL_WAIT_MS` | `500` | token 页面注入后的首次等待时间 |
| `SOLVER_FAST_CLICK` | `1` | 没有可见验证框时跳过慢点击 |
| `PAGE_GOTO_WAIT_UNTIL` | `domcontentloaded` | 注册页面导航等待条件 |
| `PAGE_POST_WAIT_MS` | `500` | 页面导航后的短等待时间 |
| `C_HOT_PAGE_POOL` | `0` | 可选性能模式，复用消费阶段页面以提升速度 |
| `C_HOT_PAGE_POOL_SIZE` | `0` | 热页池容量，`0` 表示按启动期并发自动派生 |

不确定怎么调时，先保持默认值。需要压测时，优先观察 `PHYSICAL_CAP`，不要先改 Worker 数量。

## 运行日志

运行时会定期输出一行状态，例如：

```text
[*] T:0 Q:6 phys:0 t_solve_avg:23.7 q_sent:44 q_ret:44 pair:38 ok:37 fail:0 rate:9.9/min #37
```

常用字段：

| 字段 | 含义 |
|---|---|
| `T` | 当前可用 token 数量 |
| `Q` | 当前可用验证码数量 |
| `phys` | 空闲浏览器并发许可 |
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

## 输出文件

成功账号写入：

```text
keys/accounts.txt
keys/grok.txt
```

`accounts.txt` 每行格式：

```text
email:password:sso_token
```

`keys/` 目录包含账号凭证，默认不会提交到 Git。

## 项目文件

```text
register.py                 主运行入口
email_server.py             custom 邮箱模式的本地收信服务
cloudflare/email-worker.js  Cloudflare Email Routing Worker 示例
start.sh                    首次配置和运行
run.sh                      运行入口
setup.sh                    安装依赖
.env.example                配置示例
runtime_log_analyzer.py     运行日志分析工具
tests/                      测试
docs/architecture.md        技术架构说明
```

## 测试

轻量测试：

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

`run_tests.py` 默认输出到 `test_results/`，该目录是生成物，不需要提交。

## 技术文档

并发模型、资源生命周期、参数策略和必须保持的不变量见 [docs/architecture.md](docs/architecture.md)。

## License

MIT
