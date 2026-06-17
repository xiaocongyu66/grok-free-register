# grok-free-register

自动化注册 Grok (x.ai) 账号的工具,基于 [CloakBrowser](https://pypi.org/project/cloakbrowser/) 绕过 Cloudflare 防护。自适应调度,免费临时邮箱开箱即用,产出可直接用于 API 的 `sso` 凭证。

> 仅供学习与研究使用。请遵守目标站点的服务条款与当地法律。

## 特性

- **零配置即用**:默认走免费临时邮箱(multi-provider fallback),`bash start.sh` 一条命令跑起来。
- **自适应状态机调度**:单进程协程池 + 中央调度器,按 token/验证码缺口动态分配「解码 / 发码 / 注册」,按实时 CPU 利用率自动伸缩并发——换更多核的机器无需改代码自动加速。
- **两种邮箱模式**:免费临时邮箱 / 自建域名邮箱(Cloudflare Email Routing)。
- **资源上限可配**:CPU 目标利用率、并发上限、内存下限都可在 `.env` 配置。
- **正确的凭证提取**:注册后跟随 set-cookie 重定向,取真正的 `sso` JWT(非内部 blob)。

## 快速开始

```bash
git clone <this-repo> && cd grok-free-register
bash start.sh            # 自动装依赖 → 引导选模式 → 开跑(回车=默认免费邮箱)
```

首次运行会自动下载 Chromium(~200MB)。建议在**海外网络环境**运行;如需代理见下方配置。

重新选择邮箱模式:`bash start.sh --reconfig`。
高级用户也可手动 `cp .env.example .env` 编辑后 `bash run.sh`。

## 工作原理

```
                                 ┌────────────────────┐
Turnstile widget  ──token──►    │                    │
(注入+点击 checkbox)             │   CloakBrowser     │
                                 │   (Chromium)       │
临时邮箱 / webhook  ◄─验证码──  │                    │ ──注册(Server Action)──►  x.ai 服务端
(poll 取码)                       │                    │ ◄──set-cookie 重定向───
                                 └────────────────────┘   跟随后取 sso (JWT)
                                     ▲    │
                                     │    │ gRPC 发码+验码
                                     │    ▼
                                 x.ai 发验证码到邮箱
```

1. **CloakBrowser** — TLS 指纹与真实浏览器一致的 Chromium,绕过 Cloudflare 对普通 HTTP 客户端的 403。
2. **gRPC-web** — 在浏览器页面内 `fetch()` 发送 protobuf 编码的 `CreateEmailValidationCode` / `VerifyEmailValidationCode`(必须在 x.ai 源内执行,cf_clearance 绑 TLS 指纹)。
3. **Turnstile** — 注入 Cloudflare Turnstile widget 并自动点击,本地解出 token。
4. **Server Action** — 发送 Next.js RSC Server Action 完成注册;响应里返回一个 `set-cookie?q=...` 重定向 URL。
5. **sso 提取** — 访问该重定向 URL,浏览器落下真正的 `sso` cookie(152 字符 JWT),即为账号凭证。

## 邮箱模式

### 模式一:免费临时邮箱(默认,`EMAIL_MODE=tempmail`)

零配置,直接用免费临时邮箱。按顺序自动 fallback:mail.tm → mail.gw → duckmail.sbs → tempmail.lol。mail.tm 优先(已跑通),其余仅作兜底——单个挂了不影响运行。适合快速试跑。

### 模式二:自建域名邮箱(`EMAIL_MODE=custom`)

用你自己的域名,稳定、可大量随机地址。链路:

```
oc<随机>@你的域名  →  Cloudflare Email Routing  →  Email Worker  →  POST 到本地 webhook(email_server.py)
```

步骤:

1. 域名接入 Cloudflare,开启 **Email Routing**。
2. 部署本仓库 `cloudflare/email-worker.js`(见文件内注释,wrangler 部署),把 webhook 地址配成你服务器的收信服务。
3. 在 Email Routing 的 **Catch-all** 规则里,动作选 **Send to a Worker** → 选该 Worker。
4. 服务器上跑收信服务:`.venv/bin/python email_server.py`(默认监听 `0.0.0.0:8080`,提供 `/webhook` 收信、`/check/<addr>` 供注册机查码)。
5. `.env` 设 `EMAIL_MODE=custom`、`EMAIL_DOMAIN=你的域名`、`EMAIL_API=http://127.0.0.1:8080`。

> ⚠️ **关键坑(务必看)**:Email Worker 里 `fetch` 你的 webhook **必须用域名,不能用裸 IP**。Cloudflare Workers 不允许请求裸 IP,会返回 `error 1003 (Direct IP access not allowed)` 并被静默吞掉——Worker 显示成功、你服务器却收不到任何请求。
> 做法:给服务器 IP 加一条 DNS A 记录(如 `hook.example.com`,DNS-only/灰云),Worker 的 `WEBHOOK_URL` 填 `http://hook.example.com:8080/webhook`。
>
> 子域名收信:Email Routing 的 catch-all 只对根域生效;要收 `*@mail.example.com` 这类子域,需在 Email Routing 单独启用该子域并为它配置自己的 catch-all。

## 配置(`.env`,见 `.env.example`)

| 键 | 默认 | 说明 |
|---|---|---|
| `EMAIL_MODE` | `tempmail` | `tempmail`(免费临时邮箱,多 provider fallback)/ `custom`(自建域名) |
| `EMAIL_DOMAIN` | 空 | custom 模式:你的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | custom 模式:本地收信服务地址 |
| `MAX_SLOTS` | 自动 `cpu*4` | 最大并发浏览器操作数 |
| `CPU_TARGET` | `85` | 调速器目标 CPU 利用率(%) |
| `MIN_FREE_MEM_MB` | `500` | 可用内存低于此值(MB)则收缩并发 |
| `T_TARGET` / `Q_TARGET` | `4` / `4` | token / 验证码缓冲目标 |
| `SOLVER_REUSE` | `1` | solver 页面复用(`0` 关闭,用于 A/B 测优化增益) |
| `TARGET` | `0`(不限) | 攒够 N 个号自动停止(CLI: `--target 100`) |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 可选代理 |

## 自适应调度

程序起 `MAX_SLOTS` 个通用工作单元,中央调度器每轮按当前状态决定各单元做什么:

- token 池空而有待注册项 → 优先**解 Turnstile**;
- token 和验证码都就绪 → **注册出号**;
- 缓冲不足 → 补**发码**(只补到目标值,避免过量发码被限流)。

`load_governor` 每 4 秒采样真实 CPU 利用率,向 `CPU_TARGET` 收敛地增减并发;可用内存低于 `MIN_FREE_MEM_MB` 时强制收缩。换到核数更多的机器会自动放大并发。

监控行示例:

```
[*] slots:5/8 act:5 cpu:84%/85 mem:5300M T:4 Q:6 sent:30 got:30(100%) rate:7.4/min #27
```

`slots` 当前并发上限/硬顶,`act` 在岗数,`cpu` 实测/目标,`mem` 可用内存,`T`/`Q` 两个缓冲池,`sent/got` 发码/收到(命中率),`rate` 出号速率,`#` 累计成功数。

## 输出

成功账号写入 `keys/accounts.txt`,每行:

```
邮箱:密码:sso_token
```

`sso_token` 是 152 字符的 JWT(`sso=` cookie),可直接用于 API/反代调用。`keys/grok.txt` 单独存 sso。

## 项目结构

```
register.py            # 主程序:配置解析 / 自适应调度器 / 注册流程
email_server.py        # custom 模式的本地收信服务(/webhook 收信, /check 查码)
cloudflare/
  email-worker.js      # Cloudflare Email Worker 示例(含 1003 坑的说明)
start.sh               # 一键:装依赖 + 引导配置 + 运行
setup.sh / run.sh      # 底层:安装 / 运行
.env.example           # 配置示例
```

## 优化与后续

- **已实现:solver 页面复用** —— Turnstile 解码占 CPU 最多且最频繁,故复用预热页面(停在 sign-up,省掉每次 `page.goto` 的重型 SPA 加载),小幅提升单机吞吐(2 核实测 ~7→~8/min)。
- **后续可选**:发码/注册端的页面或上下文复用。注册需逐账号干净会话(cookie 隔离),复用风险更高,故未默认开启。
- 瓶颈本质是本地解 Turnstile 的 CPU 开销——核越多越快,调度器会自动放大并发。

## License

MIT
