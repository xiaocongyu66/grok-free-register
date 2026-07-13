# 在 Hugging Face Space（Docker · 2vCPU / 16GB）部署 grok-free-register

你的 Sharkey 多阶段 Dockerfile 已经证明：**HF Docker Space + 足够内存可以跑浏览器相关依赖**。  
本仓库的 hybrid Turnstile 同样依赖 Chromium；**2 核 16G** 比免费小规格合适得多。

## 能跑什么

| 组件 | 2c/16G Docker Space |
|------|---------------------|
| Dashboard 控制面（`:PORT`，HF 默认 7860） | ✅ |
| 协议注册 `REGISTER_ENGINE=protocol` | ✅ |
| Hybrid Turnstile（Playwright/Chromium） | ✅ 建议 2～4 browser worker |
| Go register-worker / inventory | ✅ 镜像内已编译 |
| 持久账号 `keys/` | 建议开 Space **Persistent storage** → `/data` |

## 创建 Space

1. 新建 Space → **SDK: Docker**
2. Hardware：**2 vCPU · 16 GB RAM**（或更高）
3. **构建源码**  
   - Dockerfile 会在构建时 **`git clone` 完整仓库**  
     `https://github.com/xiaocongyu66/grok-free-register`（`main`）  
   - 因此 Space 里即使只有 `Dockerfile` 也能编过（不要用缺 `grok_register/` 的半截树）  
   - 日志里若仍是旧 Commit SHA（如 `b084584`）且路径报错：到 Space **Factory rebuild**，并确认推送了含 clone 逻辑的最新 Dockerfile  
   - 可选 build-arg：`REPO_URL` / `REPO_REF` 换分支
4. **Secrets**（Settings → Repository secrets）建议：

```text
# 面板密码（公网 Space 务必设置）
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_strong_password
# 可选 API Token：Authorization: Bearer ...
# CONTROL_PLANE_TOKEN=...

MOEMAIL_API_KEY=...
MOEMAIL_API=https://...
MOEMAIL_DOMAIN=...
EMAIL_MODE=moemail
CAPSOLVER_API_KEY=...          # 强烈推荐：减轻本机浏览器压力
REGISTER_ENGINE=protocol
TURNSTILE_SOLVER=hybrid
TURNSTILE_SOLVER_THREADS=2
GO_REGISTER_WORKERS=4
CONTROL_PLANE_ALLOW_ACTIONS=1
```

面板 HTML（`GET /`）与 `/api/health` **始终 200**，避免 HF「启动中」卡死。  
API / 下载需登录：打开页面后用登录框输入 `DASHBOARD_USER` / `DASHBOARD_PASSWORD`。

```bash
# 健康检查（无需密码）
curl -sS https://xxx.hf.space/api/health
# 状态 API
curl -u admin:your_strong_password https://xxx.hf.space/api/status
```

5. 可选 Variables：`TURNSTILE_SOLVER_ON_DEMAND=1`

## 本地构建自测

```bash
docker build -t grok-free-register:hf .
docker run --rm -p 7860:7860 \
  -e MOEMAIL_API_KEY=xxx \
  -e CAPSOLVER_API_KEY=xxx \
  -v grok-data:/data \
  grok-free-register:hf
```

打开：http://127.0.0.1:7860/

## 与 Sharkey Dockerfile 的对应关系

| Sharkey 脚本里 | 本镜像 |
|----------------|--------|
| 多阶段 build | Python deps + Go/Rust/C++ + runtime |
| `tini` + `entrypoint.sh` | 相同 |
| `PORT` / `SPACE_ID` | Dashboard 绑 `0.0.0.0:$PORT` |
| 浏览器依赖 | Playwright Chromium + 系统 lib |
| Postgres | **不需要**（注册机不依赖 PG） |
| Redis | **不需要** |

## 性能建议（16G）

- `TURNSTILE_SOLVER_THREADS=2`～`4`（再高容易把内存打满）
- `GO_REGISTER_WORKERS` 不要远大于 Turnstile 槽位（例如 4～8）
- 有 **CapSolver** 时协议路径可优先打码 API，吞吐更稳
- 主进程退出会清理 hybrid 进程组（本仓库已加 orphan 清理）

## 注意

- 批量注册可能违反 xAI / HF 服务条款；仅用于你有权操作的环境。
- Space 休眠会打断长任务；生产级长跑仍可用自有 VPS 作 worker，Space 只作面板。
- 首次构建较久（Playwright 浏览器 + 多语言编译）。
