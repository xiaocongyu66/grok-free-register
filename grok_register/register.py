"""
Grok Free Register — CSP 异步并发架构
=============================================
单进程 asyncio + 单共享 CloakBrowser + Semaphore 背压:
  - S_Worker: 生成 Turnstile token (T)
  - P_Worker: 创建邮箱 + 发送验证码 + 轮询验证码 (Q)
  - C_Worker: claim pair 并执行注册
  - Semaphore 背压控制容量,无需中心调度器

邮箱模式(EMAIL_MODE):
  - tempmail (默认,零配置): 免费临时邮箱,多 provider 自动 fallback
  - moemail: MoeMail OpenAPI,需要 MOEMAIL_API_KEY
  - custom: 自建域名邮箱,Cloudflare Email Routing → Worker → 本地 webhook
            (见 grok_register/email_server.py / cloudflare/email-worker.js)

配置全部走环境变量 / .env(见 .env.example);CLI: --max-mem 6G --target 100
用法:
  bash start.sh          # 一键引导
"""
import os, json, random, string, time, re, secrets, base64, struct, asyncio, glob, sys, multiprocessing, atexit, threading, tempfile
from datetime import datetime, timezone
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import requests as req
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse, urlunparse
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor

# CSP 架构组件
from grok_register.core.admission import AdmissionGate
from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from grok_register.proxy_auto import ProxyAutoConfig, ProxyAutoManager, load_active_proxies
from grok_register.proxy_relay import BuiltinProxyRelay, BuiltinProxyRelayConfig
from xai_enroller.fingerprints import (
    BROWSER_FINGERPRINT_FILENAME,
    browser_context_options,
    get_or_create_browser_fingerprint,
)

SITE_URL = "https://accounts.x.ai"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── 配置（环境变量 / .env，见 .env.example）──
def _env_int(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except ValueError:
        return default

def _env_int_or_none(key):
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def _env_list(key, default=""):
    raw = os.environ.get(key)
    if raw is None:
        raw = default
    items = []
    for part in re.split(r"[\n,]+", str(raw)):
        item = part.strip().lower()
        if item:
            items.append(item)
    return tuple(items)

def _env_bool(key, default=False):
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

def _normalize_key_export_formats(items):
    normalized = []
    for item in items:
        value = (item or "").strip().lower()
        if value == "sub":
            value = "sub2api"
        if value not in {"legacy", "sub2api", "cpa"}:
            continue
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)

EMAIL_MODE      = (os.environ.get("EMAIL_MODE") or "tempmail").strip().lower()   # tempmail | moemail | custom
if EMAIL_MODE == "mailtm":      # 兼容旧名
    EMAIL_MODE = "tempmail"
LOCAL_EMAIL_API = (os.environ.get("EMAIL_API") or "http://127.0.0.1:8080").strip()
EMAIL_DOMAIN    = (os.environ.get("EMAIL_DOMAIN") or "").strip()
MOEMAIL_API     = (
    os.environ.get("MOEMAIL_API")
    or os.environ.get("MOEMAIL_API_URL")
    or "https://moemail.app"
).strip()
MOEMAIL_API_KEY = (
    os.environ.get("MOEMAIL_API_KEY")
    or os.environ.get("MOEMAIL_TOKEN")
    or ""
).strip()
MOEMAIL_DOMAIN  = (os.environ.get("MOEMAIL_DOMAIN") or "").strip()
MOEMAIL_EXPIRY_MS = _env_int("MOEMAIL_EXPIRY_MS", 3600000)
KEY_EXPORT_DIR = str(Path((os.environ.get("KEY_EXPORT_DIR") or "keys").strip() or "keys").expanduser())
KEY_EXPORT_FORMATS = _normalize_key_export_formats(
    _env_list("KEY_EXPORT_FORMATS", "legacy,sub2api")
) or ("legacy",)
KEY_EXPORT_ENROLLER = _env_bool("KEY_EXPORT_ENROLLER", True)
KEY_EXPORT_ENROLLER_TIMEOUT = max(30, _env_int("KEY_EXPORT_ENROLLER_TIMEOUT", 1800))
KEY_EXPORT_ENROLLER_POLL_SEC = max(1, _env_int("KEY_EXPORT_ENROLLER_POLL_SEC", 5))
KEY_EXPORT_ENROLLER_RETRY_ATTEMPTS = min(
    3, max(0, _env_int("KEY_EXPORT_ENROLLER_RETRY_ATTEMPTS", 0))
)
KEY_EXPORT_ENROLLER_DRAIN_TIMEOUT = max(
    0, _env_int("KEY_EXPORT_ENROLLER_DRAIN_TIMEOUT", 10)
)
os.makedirs(KEY_EXPORT_DIR, exist_ok=True)
_PROXY_POOL_FILE_ENV = os.environ.get("PROXY_POOL_FILE")
PROXY_POOL_FILE = (_PROXY_POOL_FILE_ENV if _PROXY_POOL_FILE_ENV is not None else "代理.txt").strip()
PROXY_POOL_STRATEGY = (os.environ.get("PROXY_POOL_STRATEGY") or "round_robin").strip().lower()


def _env_proxy_pool_text() -> str:
    """Live-read multi-proxy env (HF Secrets / panel). Mix formats freely."""
    return (
        os.environ.get("PROXY_POOL")
        or os.environ.get("PROXY_POOL_LIST")
        or os.environ.get("PROXIES")
        or os.environ.get("PROXY_LIST")
        or ""
    ).strip()
PROXY_RELAY_ENABLED = (
    os.environ.get("PROXY_RELAY_ENABLED", "1").strip().lower()
    in ("1", "true", "yes", "on")
)
PROXY_RELAY_URL = (os.environ.get("PROXY_RELAY_URL") or "http://127.0.0.1:18080").strip().rstrip("/")
PROXY_RELAY_KERNEL = (os.environ.get("PROXY_RELAY_KERNEL") or "auto").strip().lower()
PROXY_RELAY_HOST = (os.environ.get("PROXY_RELAY_HOST") or "127.0.0.1").strip()
PROXY_RELAY_PROXY_SCHEME = (os.environ.get("PROXY_RELAY_PROXY_SCHEME") or "auto").strip().lower()
PROXY_RELAY_TIMEOUT = _env_int("PROXY_RELAY_TIMEOUT", 8)
PROXY_RELAY_RETRY_SEC = _env_int("PROXY_RELAY_RETRY_SEC", 30)
PROXY_RELAY_BUILTIN_ENABLED = _env_bool("PROXY_RELAY_BUILTIN_ENABLED", True)
PROXY_RELAY_AUTO_INSTALL = _env_bool("PROXY_RELAY_AUTO_INSTALL", True)
PROXY_RELAY_SING_BOX_BIN = (os.environ.get("PROXY_RELAY_SING_BOX_BIN") or "").strip()
PROXY_RELAY_WORK_DIR = (os.environ.get("PROXY_RELAY_WORK_DIR") or "logs/proxy-relay").strip()
PROXY_RELAY_START_PORT = _env_int("PROXY_RELAY_START_PORT", 19080)
PROXY_RELAY_MAX_NODES = _env_int("PROXY_RELAY_MAX_NODES", 48)
PROXY_RELAY_START_TIMEOUT = _env_int("PROXY_RELAY_START_TIMEOUT", 8)
PROXY_AUTO_CONFIG = ProxyAutoConfig.from_env(os.environ)
PROXY_AUTO_REQUIRE_ACTIVE = _env_bool("PROXY_AUTO_REQUIRE_ACTIVE", PROXY_AUTO_CONFIG.enabled)
PROXY_POOL_USE_TESTED_ONLY = _env_bool(
    "PROXY_POOL_USE_TESTED_ONLY",
    PROXY_AUTO_CONFIG.enabled and PROXY_AUTO_REQUIRE_ACTIVE,
)
CF_ARES_EMAIL_MODE = (os.environ.get("CF_ARES_EMAIL") or "0").strip().lower()
CF_ARES_BROWSER_ENGINE = (os.environ.get("CF_ARES_BROWSER_ENGINE") or "auto").strip()
CF_ARES_HEADLESS = (
    os.environ.get("CF_ARES_HEADLESS", "1").strip().lower()
    in ("1", "true", "yes", "on")
)
CF_ARES_PROXY = (
    os.environ.get("CF_ARES_PROXY")
    or os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or ""
).strip()
CF_ARES_XAI_MODE = (
    os.environ.get("CF_ARES_XAI")
    or os.environ.get("CF_ARES_GROK")
    or (CF_ARES_EMAIL_MODE if CF_ARES_EMAIL_MODE in ("1", "true", "yes", "on", "fallback", "always") else "0")
).strip().lower()
CF_ARES_IMPERSONATE = (os.environ.get("CF_ARES_IMPERSONATE") or "chrome120").strip()
CF_ARES_CHROME_PATH = (os.environ.get("CF_ARES_CHROME_PATH") or "").strip()
CF_ARES_PATH = (os.environ.get("CF_ARES_PATH") or "").strip()
CF_ARES_BUNDLED_PATH = PROJECT_ROOT / "vendor" / "CF-Ares"
CF_ARES_TIMEOUT = _env_int("CF_ARES_TIMEOUT", 30)
MIN_FREE_MEM_MB = _env_int("MIN_FREE_MEM_MB", 500)   # 自动容量派生时保留的内存(MB)
T_TARGET        = _env_int("T_TARGET", 4)            # token 池缓冲目标
Q_TARGET        = _env_int("Q_TARGET", 4)            # 就绪验证码缓冲目标
TARGET          = _env_int("TARGET", 0)              # 攒够 N 个号自动停(0=不限;--target N 可覆盖)

# CSP 容量参数
PHYSICAL_CAP    = _env_int("PHYSICAL_CAP", 0)        # 本地物理资源许可,0=自动派生
PHYSICAL_PER_CPU = _env_int("PHYSICAL_PER_CPU", 2)   # 自动派生 CPU 侧保守上限;压测可临时覆盖
PHYSICAL_MEM_MB = _env_int("PHYSICAL_MEM_MB", 512)   # 每个物理许可的保守内存预算(MB)
CAPACITY_PROFILE = (os.environ.get("CAPACITY_PROFILE") or "").strip()
T_SLOT_CAP      = _env_int("T_SLOT_CAP", 8)          # token 库存缓冲
Q_SLOT_CAP      = _env_int("Q_SLOT_CAP", 8)          # 验证码库存缓冲
Q_PENDING_CAP   = _env_int("Q_PENDING_CAP", 12)       # 外部在途 Q 请求上限
T_MAX_AGE       = _env_int("T_MAX_AGE", 300)          # token 最大年龄(秒)
Q_MAX_AGE       = _env_int("Q_MAX_AGE", 120)          # 验证码最大年龄(秒)
P_REQUEST_TIMEOUT = _env_int("P_REQUEST_TIMEOUT", 95) # P 等待 Q 返回超时(秒)
EMAIL_CODE_RESEND_ATTEMPTS = max(0, _env_int("EMAIL_CODE_RESEND_ATTEMPTS", 2))
EMAIL_CODE_RESEND_AFTER_SEC = max(1, _env_int("EMAIL_CODE_RESEND_AFTER_SEC", 35))
C_CONSUME_TIMEOUT = _env_int("C_CONSUME_TIMEOUT", 60) # C 消费完整 pair 超时(秒)
S_WORKERS       = _env_int("S_WORKERS", 0)            # 0=自动
P_WORKERS       = _env_int("P_WORKERS", 0)
C_WORKERS       = _env_int("C_WORKERS", 0)
C_HOT_PAGE_POOL = (os.environ.get("C_HOT_PAGE_POOL", "0").strip().lower() in ("1", "true", "yes"))
C_HOT_PAGE_POOL_SIZE = _env_int("C_HOT_PAGE_POOL_SIZE", 0)
C_SET_COOKIE_VIA_REQUEST = (
    os.environ.get("C_SET_COOKIE_VIA_REQUEST", "1")
    .strip()
    .lower()
    in ("1", "true", "yes")
)

# CSP v2 局部门控/批量发送参数。水位默认在启动期结合 Physical_Sem 派生。
_T_HIGH_WATER_OVERRIDE = _env_int_or_none("T_HIGH_WATER")
_T_LOW_WATER_OVERRIDE  = _env_int_or_none("T_LOW_WATER")
_Q_HIGH_WATER_OVERRIDE = _env_int_or_none("Q_HIGH_WATER")
_Q_LOW_WATER_OVERRIDE  = _env_int_or_none("Q_LOW_WATER")
P_BATCH_MAX     = max(1, _env_int("P_BATCH_MAX", 4))
P_SEND_CAP      = _env_int("P_SEND_CAP", 0)           # >0=显式限制并发 P 发送页面;0=不额外建模
PAGE_GOTO_WAIT_UNTIL = os.environ.get("PAGE_GOTO_WAIT_UNTIL", "domcontentloaded").strip() or "domcontentloaded"
PAGE_POST_WAIT_MS = _env_int("PAGE_POST_WAIT_MS", 500)
PAGE_BLOCK_STATIC_ASSETS = (
    os.environ.get("PAGE_BLOCK_STATIC_ASSETS", "0").strip().lower()
    in ("1", "true", "yes")
)
REGISTRATION_DIAGNOSTICS = (
    os.environ.get("REGISTRATION_DIAGNOSTICS", "0").strip().lower()
    in ("1", "true", "yes")
)
REGISTER_HEARTBEAT_INTERVAL = max(0, _env_int("REGISTER_HEARTBEAT_INTERVAL", 60))
REGISTRATION_RATE_LIMIT_COOLDOWN = max(
    30, _env_int("REGISTRATION_RATE_LIMIT_COOLDOWN", 60)
)
REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS", 60)
)
REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL", 3)
)
# proxy = cool only the rate-limited exit IP (recommended with multi-proxy pools)
# global = pause all C workers after any rate limit (legacy)
REGISTRATION_RATE_LIMIT_SCOPE = (
    os.environ.get("REGISTRATION_RATE_LIMIT_SCOPE") or "proxy"
).strip().lower()
if REGISTRATION_RATE_LIMIT_SCOPE not in {"proxy", "global"}:
    REGISTRATION_RATE_LIMIT_SCOPE = "proxy"
REGISTER_LOG_MODE = "user"

SITE_KEY = None
ACTION_ID = None
STATE_TREE = None

start_time = time.time()
success_count = 0
file_lock = asyncio.Lock()
STOP = asyncio.Event()

# 角色标识 + 轮询/HTTP 专用线程池（与 CPU 密集的浏览器操作解耦）
SOLVE, PRODUCE, CONSUME, IDLE = 'SOLVE', 'PRODUCE', 'CONSUME', 'IDLE'
POLL_EXECUTOR = ThreadPoolExecutor(max_workers=32)

def resolve_register_log_mode(argv=None, env=None):
    argv = list([] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    mode = (env.get("REGISTER_LOG_MODE") or "user").strip().lower()
    if "--debug" in argv:
        mode = "debug"
    if mode not in {"user", "debug"}:
        raise ValueError("REGISTER_LOG_MODE must be user or debug")
    return mode


def _terminal_output(msg):
    print(msg, flush=True)


def log(msg):
    try:
        _terminal_output(msg)
    except Exception:
        return


def debug_log(msg):
    if REGISTER_LOG_MODE == "debug":
        log(msg)
def rand_str(n=15): return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def sanitize_terminal_error(error):
    return type(error).__name__


def format_user_registration_event(
    kind,
    *,
    task_id=None,
    count=None,
    rate_per_minute=None,
    wait_seconds=None,
    remaining=None,
    proxy=None,
    proxy_key=None,
):
    if kind == "service_started":
        progress = f"剩余 {remaining}" if remaining is not None else "持续运行"
        return f"[✓] 注册服务已启动 | {progress}"
    if kind == "started":
        suffix = f" | 剩余 {remaining}" if remaining is not None else ""
        return f"[→] 开始注册 #{task_id}{suffix}"
    if kind == "success":
        rate = "—" if rate_per_minute is None else f"{rate_per_minute:.1f}/分"
        return f"[✓] 注册成功 #{task_id} | 运行平均 {rate} | 累计 {count}"
    if kind == "failed":
        return f"[✗] 注册失败 #{task_id} | 已跳过，继续下一任务"
    if kind == "rate_limited":
        label = proxy_key or proxy
        if label:
            return f"[⏸] 出口限流 {label} | {wait_seconds}秒后换 IP 重试"
        return f"[⏸] 触发限流 | {wait_seconds}秒后恢复探测"
    if kind == "recovered":
        return f"[▶] 限流解除 | 实际等待 {wait_seconds}秒"
    if kind == "stopped":
        return f"[■] 注册服务已停止 | 累计 {count or 0}"
    raise ValueError(f"unknown user registration event: {kind}")


class RegistrationRateLimited(RuntimeError):
    """注册提交被目标站点的限流页替代。"""


def _proxy_rate_limit_key(proxy):
    """Stable key for per-exit cooldowns (host:port, or 'direct')."""
    if not proxy:
        return "direct"
    try:
        parsed = urlparse(proxy)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
        if host and port:
            return f"{host}:{port}"
        if host:
            return host
    except Exception:
        pass
    return str(proxy).strip() or "direct"


class RegistrationRateLimitCircuit:
    """在检测到注册限流后暂停新的 C 阶段提交。

    scope=global: 任一出口限流后全局冷却（旧行为）。
    scope=proxy:  只冷却触发限流的代理 IP，其它出口继续跑。
    """

    def __init__(
        self,
        cooldown_seconds,
        recovery_seconds=60,
        recovery_interval=3,
        clock=time.monotonic,
        scope="global",
    ):
        self.cooldown_seconds = cooldown_seconds
        self.recovery_seconds = recovery_seconds
        self.recovery_interval = recovery_interval
        self._clock = clock
        self.scope = "proxy" if str(scope).lower() == "proxy" else "global"
        self._blocked_until = 0.0
        self._tripped_at = None
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0
        self._proxy_blocked_until = {}
        self._proxy_lock = threading.Lock()

    def remaining_seconds(self, proxy=None):
        if self.scope == "proxy":
            now = self._clock()
            with self._proxy_lock:
                self._purge_proxy_blocks_locked(now)
                if proxy is not None:
                    key = _proxy_rate_limit_key(proxy)
                    until = self._proxy_blocked_until.get(key, 0.0)
                    return max(0, int(until - now + 0.999))
                blocked = dict(self._proxy_blocked_until)
            if not blocked:
                return 0
            # Snapshot pool outside proxy_lock to avoid pool_lock/proxy_lock deadlocks.
            if self._pool_has_unblocked(blocked):
                return 0
            soonest = min(blocked.values())
            return max(0, int(soonest - now + 0.999))
        return max(0, int(self._blocked_until - self._clock() + 0.999))

    def is_open(self, proxy=None):
        return self.remaining_seconds(proxy) > 0

    def is_proxy_blocked(self, proxy):
        if self.scope != "proxy":
            return self.is_open()
        return self.remaining_seconds(proxy) > 0

    def _purge_proxy_blocks_locked(self, now=None):
        now = self._clock() if now is None else now
        expired = [k for k, until in self._proxy_blocked_until.items() if until <= now]
        for key in expired:
            self._proxy_blocked_until.pop(key, None)

    def _snapshot_pool_keys(self):
        try:
            with _proxy_pool_lock:
                items = list(_load_proxy_pool_locked())
        except Exception:
            items = []
        if not items:
            return [_proxy_rate_limit_key(None)]
        return [_proxy_rate_limit_key(proxy) for proxy in items]

    def _pool_has_unblocked(self, blocked):
        """True if at least one pool exit is not in the blocked map."""
        for key in self._snapshot_pool_keys():
            if key not in blocked:
                return True
        return False

    def available_proxy_count(self):
        if self.scope != "proxy":
            return 0 if self.is_open() else 1
        now = self._clock()
        with self._proxy_lock:
            self._purge_proxy_blocks_locked(now)
            blocked = dict(self._proxy_blocked_until)
        keys = self._snapshot_pool_keys()
        return sum(1 for key in keys if key not in blocked)

    def trip(self, proxy=None):
        if self.scope == "proxy" and proxy is not None:
            key = _proxy_rate_limit_key(proxy)
            now = self._clock()
            with self._proxy_lock:
                prev = self._proxy_blocked_until.get(key, 0.0)
                starts_new_window = prev <= now
                self._proxy_blocked_until[key] = max(
                    prev,
                    now + self.cooldown_seconds,
                )
            return starts_new_window

        starts_new_window = not self.is_open()
        if self._tripped_at is None:
            self._tripped_at = self._clock()
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0
        self._probe_active = False
        self._probe_token = None
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + self.cooldown_seconds,
        )
        # Also cool the specific proxy so subsequent picks avoid it during global cool-down.
        if proxy is not None:
            key = _proxy_rate_limit_key(proxy)
            with self._proxy_lock:
                self._proxy_blocked_until[key] = max(
                    self._proxy_blocked_until.get(key, 0.0),
                    self._clock() + self.cooldown_seconds,
                )
        return starts_new_window

    async def wait(self):
        if self.scope == "proxy":
            while True:
                if not self.is_open():
                    return False
                await asyncio.sleep(min(max(self.remaining_seconds(), 1), 5))
        while True:
            if self.is_open():
                await asyncio.sleep(min(self.remaining_seconds(), 5))
                continue
            if self._tripped_at is None:
                if not self._recovering_until:
                    return False
                if self._probe_active:
                    await asyncio.sleep(0.2)
                    continue
                if self._clock() >= self._recovering_until:
                    self._recovering_until = 0.0
                    self._next_recovery_submit = 0.0
                    return False
                recovery_wait = self._next_recovery_submit - self._clock()
                if recovery_wait > 0:
                    await asyncio.sleep(min(recovery_wait, 0.5))
                    continue
            if not self._probe_active:
                self._probe_active = True
                self._probe_token = object()
                return self._probe_token
            await asyncio.sleep(0.2)

    def can_submit(self, probe_token=False):
        """仅允许正常态任务或当前恢复探针发起注册提交。"""
        if self.scope == "proxy":
            return not self.is_open()
        if self._tripped_at is None and not self._recovering_until:
            return True
        return (
            probe_token is not False
            and probe_token is self._probe_token
            and not self.is_open()
        )

    def consume_recovery_seconds(self, probe_token=None):
        if self.scope == "proxy":
            return None
        if self._tripped_at is None:
            self.complete_recovery_submission(probe_token)
            return None
        if probe_token is not None and probe_token is not self._probe_token:
            return None
        elapsed = self._clock() - self._tripped_at
        self._tripped_at = None
        self._blocked_until = 0.0
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = self._clock() + self.recovery_seconds
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return elapsed

    def complete_recovery_submission(self, probe_token):
        """完成恢复期内的一次成功提交，并按固定节奏放行下一项。"""
        if self.scope == "proxy":
            return False
        if probe_token is not self._probe_token or not self._recovering_until:
            return False
        self._probe_active = False
        self._probe_token = None
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return True

    def release_probe(self, probe_token):
        """提交前资源已失效时让出探针，不额外增加冷却窗口。"""
        if self.scope == "proxy":
            return
        if probe_token is self._probe_token:
            self._probe_active = False
            self._probe_token = None

    def defer_probe(self, probe_token):
        """真正的探针失败后重新进入完整冷却。"""
        if self.scope == "proxy":
            return
        if probe_token is not self._probe_token:
            return
        self._probe_active = False
        self._probe_token = None
        if self._tripped_at is not None:
            self._blocked_until = max(
                self._blocked_until,
                self._clock() + self.cooldown_seconds,
            )
        elif self._recovering_until:
            self._next_recovery_submit = self._clock() + self.recovery_interval


REGISTRATION_RATE_LIMIT_CIRCUIT = RegistrationRateLimitCircuit(
    REGISTRATION_RATE_LIMIT_COOLDOWN,
    recovery_seconds=REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS,
    recovery_interval=REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL,
    scope=REGISTRATION_RATE_LIMIT_SCOPE,
)

def _signup_response_markers(text):
    """将失败响应归类为固定标签，诊断时不输出任何服务端正文。"""
    lowered = text.lower()
    groups = {
        "challenge": ("captcha", "cf-chl", "challenge-platform"),
        "rate_limited": ("rate limit", "too many requests", "try again later"),
        "signin_page": ("sign in", "log in"),
        "signup_page": ("sign up", "create your account"),
        "next_page": ("__next", "/_next/"),
        "action_error": ("server action", "next-action", "digest"),
    }
    return ",".join(name for name, needles in groups.items() if any(x in lowered for x in needles)) or "unclassified"

def pb_varint(n):
    parts = []
    while n > 0x7f: parts.append((n & 0x7f) | 0x80); n >>= 7
    parts.append(n); return bytes(parts)
def pb_str(fid, val):
    vb = val.encode()
    return struct.pack('B', (fid << 3) | 2) + pb_varint(len(vb)) + vb
def decode_jwt_payload(token):
    parts = token.split('.')
    if len(parts) < 2: return None
    payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
    try: return json.loads(base64.urlsafe_b64decode(payload))
    except Exception: return None
def find_chrome():
    paths = glob.glob(os.path.expanduser("~/.cloakbrowser/chromium-*/chrome"))
    if not paths: raise RuntimeError("CloakBrowser not found")
    return sorted(paths)[-1]


# ──────────────────────────────────────────────
#  资源检测
# ──────────────────────────────────────────────
def get_system_resources(max_mem_arg=None):
    import subprocess
    cpu_count = multiprocessing.cpu_count()
    try:
        out = subprocess.check_output(["free", "-m"]).decode()
        for line in out.split("\n"):
            if "Mem" in line:
                parts = line.split()
                total, available = int(parts[1]), int(parts[6])
                break
        else:
            total, available = 4096, 2048
    except Exception:
        total, available = 4096, 2048

    if max_mem_arg:
        if max_mem_arg.endswith('%'):
            max_mem = int(total * float(max_mem_arg[:-1]) / 100)
        elif max_mem_arg.upper().endswith('G'):
            max_mem = int(float(max_mem_arg[:-1]) * 1024)
        elif max_mem_arg.upper().endswith('M'):
            max_mem = int(max_mem_arg[:-1])
        else:
            max_mem = int(max_mem_arg)
    else:
        max_mem = available

    return {'cpu': cpu_count, 'total_mem': total, 'available_mem': available, 'max_mem': max_mem}


def load_capacity_profile(path=CAPACITY_PROFILE):
    """读取离线校准生成的设备 profile。不存在或无效时返回空配置。"""
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    profile = {}
    try:
        physical_cap = int(data.get("physical_cap", 0))
    except (TypeError, ValueError):
        physical_cap = 0
    if physical_cap > 0:
        profile["physical_cap"] = physical_cap
    return profile


def derive_capacity(
    cpu_count,
    max_mem_mb,
    *,
    physical_cap=None,
    profile_physical_cap=None,
    physical_per_cpu=None,
    physical_mem_mb=None,
    min_free_mem_mb=None,
):
    """启动期静态容量派生:显式配置 > 设备 profile > CPU/内存保守自动值。"""
    configured_physical = PHYSICAL_CAP if physical_cap is None else physical_cap
    profiled_physical = profile_physical_cap or 0
    per_cpu = PHYSICAL_PER_CPU if physical_per_cpu is None else physical_per_cpu
    mem_per_physical = PHYSICAL_MEM_MB if physical_mem_mb is None else physical_mem_mb
    reserve_mem = MIN_FREE_MEM_MB if min_free_mem_mb is None else min_free_mem_mb

    cpu_cap = max(1, cpu_count * max(1, per_cpu))
    usable_mem = max(0, max_mem_mb - reserve_mem)
    mem_cap = max(1, usable_mem // max(1, mem_per_physical))
    auto_cap = max(1, min(cpu_cap, mem_cap))

    if configured_physical > 0:
        physical = configured_physical
    elif profiled_physical > 0:
        physical = max(1, min(profiled_physical, mem_cap))
    else:
        physical = auto_cap

    s_workers = S_WORKERS if S_WORKERS > 0 else physical + 2
    p_workers = P_WORKERS if P_WORKERS > 0 else min(Q_PENDING_CAP + 2, max(2, physical * 2))
    c_workers = C_WORKERS if C_WORKERS > 0 else physical + 2
    return physical, s_workers, p_workers, c_workers


def derive_p_batch_max(physical_cap, configured=None):
    configured = P_BATCH_MAX if configured is None else configured
    return max(1, min(max(1, configured), max(1, physical_cap)))


def derive_admission_watermarks(
    physical_cap,
    *,
    t_slot_cap=None,
    q_pending_cap=None,
    t_target=None,
    q_target=None,
    t_high_override=None,
    t_low_override=None,
    q_high_override=None,
    q_low_override=None,
):
    """派生 CSP v2 局部门控水位。

    T 的默认高水位跟随 Physical_Sem,避免物理并发提高后仍只允许少量 T
    in-progress。显式环境变量覆盖仍然优先。
    """
    t_slot = T_SLOT_CAP if t_slot_cap is None else t_slot_cap
    q_pending = Q_PENDING_CAP if q_pending_cap is None else q_pending_cap
    t_goal = T_TARGET if t_target is None else t_target
    q_goal = Q_TARGET if q_target is None else q_target

    t_high_cfg = _T_HIGH_WATER_OVERRIDE if t_high_override is None else t_high_override
    t_low_cfg = _T_LOW_WATER_OVERRIDE if t_low_override is None else t_low_override
    q_high_cfg = _Q_HIGH_WATER_OVERRIDE if q_high_override is None else q_high_override
    q_low_cfg = _Q_LOW_WATER_OVERRIDE if q_low_override is None else q_low_override

    if t_high_cfg is None:
        t_high = min(max(1, t_slot), max(1, physical_cap))
    else:
        t_high = max(1, min(max(1, t_slot), t_high_cfg))

    if t_low_cfg is None:
        t_low = max(0, min(t_high, t_high // 2))
    else:
        t_low = max(0, min(t_high, t_low_cfg))

    if q_high_cfg is None:
        q_high = max(1, q_pending)
    else:
        q_high = max(1, min(max(1, q_pending), q_high_cfg))

    if q_low_cfg is None:
        q_low = max(0, min(q_high, q_goal, q_high // 2))
    else:
        q_low = max(0, min(q_high, q_low_cfg))

    return {
        "t_low": t_low,
        "t_high": t_high,
        "q_low": q_low,
        "q_high": q_high,
    }


def derive_c_hot_page_pool_size(physical_cap, c_workers, configured_size=None):
    """启动期静态派生 C 热页池容量；显式配置优先。"""
    configured = C_HOT_PAGE_POOL_SIZE if configured_size is None else configured_size
    if configured and configured > 0:
        return configured
    return max(1, min(max(1, physical_cap), max(1, c_workers)))


# ──────────────────────────────────────────────
#  配置获取
# ──────────────────────────────────────────────
async def fetch_config():
    global SITE_KEY, ACTION_ID, STATE_TREE
    debug_log('[*] Fetching config...')
    # Prefer each live proxy once, then a few round-robin picks.
    # Empty-string proxy was previously treated as a real value and skipped the pool.
    pool = []
    try:
        with _proxy_pool_lock:
            pool = list(_load_proxy_pool_locked())
    except Exception:
        pool = []
    attempts = list(pool[:8])
    if not attempts:
        attempts = [None, None, None]
    else:
        # a couple extra random/round-robin picks for flaky exits
        attempts = attempts + [None, None]

    async with async_playwright() as pw:
        for attempt, proxy in enumerate(attempts, 1):
            browser = await pw.chromium.launch(executable_path=find_chrome(), headless=True)
            context = None
            page = None
            try:
                SITE_KEY = ACTION_ID = STATE_TREE = None
                # None → _new_grok_page picks from pool; explicit proxy uses that exit.
                context, page = await _new_grok_page(browser, proxy=proxy)
                used = _page_proxy(page) or proxy or "direct"
                debug_log(f'[*] config fetch attempt {attempt} via {_proxy_rate_limit_key(used) if used != "direct" else "direct"}')
                await page.goto(
                    f'{SITE_URL}/sign-up?redirect=grok-com',
                    timeout=45000,
                    wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(3000)
                html = await page.content()
                m = re.search(r'0x4AAAAAAA[a-zA-Z0-9_-]+', html)
                if m: SITE_KEY = m.group(0); debug_log(f'[+] SITE_KEY: {SITE_KEY}')
                for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
                    if 'sign-up' not in chunk: continue
                    decoded = chunk.replace('\\"', '"')
                    f_match = re.search(r'"f":\[\[\[', decoded)
                    if not f_match: continue
                    f_start = f_match.start() + 5
                    end_idx = decoded.find('"$undefined"', f_start)
                    if end_idx < 0: continue
                    STATE_TREE = quote(decoded[f_start:end_idx].replace('\\\\"', '"').replace('\\', ''), safe='')
                    debug_log(f'[+] STATE_TREE: {STATE_TREE[:50]}...')
                    break
                js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
                for js_url in js_urls[:50]:
                    try:
                        js = await page.evaluate(f"(async()=>{{return await fetch('{js_url}').then(r=>r.text()).catch(()=>\"\" )}})()")
                        if not js: continue
                        if not any(kw in js for kw in ['createUser','registerUser','emailValidation']): continue
                        hexes = re.findall(r'[a-fA-F0-9]{40,50}', js)
                        if hexes: ACTION_ID = hexes[0]; break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue
                if ACTION_ID: debug_log(f'[+] ACTION_ID: {ACTION_ID}')
                if all([SITE_KEY, ACTION_ID, STATE_TREE]):
                    return
                debug_log(f'[*] config fetch attempt {attempt} incomplete')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_log(f'[*] config fetch attempt {attempt} failed: {sanitize_terminal_error(exc)}')
            finally:
                await _close_grok_page(context, page)
                await _close_browser_safely(browser)
    raise RuntimeError("Config fetch failed")


# ──────────────────────────────────────────────
#  异步操作
# ──────────────────────────────────────────────
def _remember_page_proxy(page, proxy):
    try:
        setattr(page, "_grok_proxy", proxy)
    except Exception:
        pass

def _page_proxy(page):
    try:
        return getattr(page, "_grok_proxy", None)
    except Exception:
        return None

def _xai_http_request(method, url, *, page=None, proxy=None, **kwargs):
    proxy = proxy if proxy is not None else _page_proxy(page)
    request_kwargs = dict(kwargs)
    if proxy and "proxies" not in request_kwargs:
        request_kwargs.update(_requests_proxy_kwargs(proxy))
    cf_kwargs = dict(kwargs)
    if proxy:
        cf_kwargs["proxy"] = proxy

    if _cf_ares_xai_mode_always():
        return _cf_ares_request(method, url, **cf_kwargs)

    try:
        response = getattr(req, method.lower())(url, **request_kwargs)
    except Exception:
        debug_log("[xAI] HTTP retry via CF-Ares after request error")
        return _cf_ares_request(method, url, **cf_kwargs)
    if _looks_like_cloudflare_block(response):
        debug_log("[xAI] HTTP retry via CF-Ares after Cloudflare block")
        return _cf_ares_request(method, url, **cf_kwargs)
    return response

async def _xai_http_request_async(method, url, *, page=None, proxy=None, **kwargs):
    return await asyncio.to_thread(
        _xai_http_request,
        method,
        url,
        page=page,
        proxy=proxy,
        **kwargs,
    )

def _xai_grpc_status_from_response(response):
    if response is None or not hasattr(response, "headers"):
        return None
    status_code = getattr(response, "status_code", 200) or 200
    if status_code >= 400:
        return str(status_code)
    return _response_header(response, "grpc-status", "0") or "0"

async def _grpc_create_code_http(page, email):
    inner = pb_str(1, email)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    response = await _xai_http_request_async(
        "POST",
        f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode",
        page=page,
        headers={
            "accept": "*/*",
            "content-type": "application/grpc-web+proto",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
        },
        data=frame,
        timeout=20,
    )
    return _xai_grpc_status_from_response(response)

async def _grpc_verify_code_http(page, email, code):
    inner = pb_str(1, email) + pb_str(2, code)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    response = await _xai_http_request_async(
        "POST",
        f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode",
        page=page,
        headers={
            "accept": "*/*",
            "content-type": "application/grpc-web+proto",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
        },
        data=frame,
        timeout=20,
    )
    return _xai_grpc_status_from_response(response)

async def grpc_create_code(page, email):
    inner = pb_str(1, email)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    if _cf_ares_xai_mode_enabled():
        try:
            status = await _grpc_create_code_http(page, email)
            if status is not None:
                return status == '0'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log(f"[xAI] create code HTTP fallback to page fetch: {sanitize_terminal_error(exc)}")
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    return s == '0'


async def _prepare_signup_page(page, *, redirect=True, timeout=30000):
    if PAGE_BLOCK_STATIC_ASSETS:
        await page.route("**/*", _route_static_asset_filter)
    url = f'{SITE_URL}/sign-up?redirect=grok-com' if redirect else f'{SITE_URL}/sign-up'
    await page.goto(url, timeout=timeout, wait_until=PAGE_GOTO_WAIT_UNTIL)
    if PAGE_POST_WAIT_MS > 0:
        await page.wait_for_timeout(PAGE_POST_WAIT_MS)


async def _route_static_asset_filter(route):
    req = route.request
    if (
        req.resource_type in ("image", "font", "media", "stylesheet")
        or "/_next/static/" in req.url
        or "analytics" in req.url
    ):
        await route.abort()
        return
    await route.continue_()


async def grpc_verify_code(page, email, code):
    inner = pb_str(1, email) + pb_str(2, code)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    if _cf_ares_xai_mode_enabled():
        try:
            status = await _grpc_verify_code_http(page, email, code)
            if status is not None:
                if REGISTRATION_DIAGNOSTICS and status != '0':
                    debug_log('[C] verify rejected')
                return status == '0'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log(f"[xAI] verify code HTTP fallback to page fetch: {sanitize_terminal_error(exc)}")
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    if REGISTRATION_DIAGNOSTICS and s != '0':
        debug_log('[C] verify rejected')
    return s == '0'


def auth_cookie_snapshot(cookies):
    """保留认证所需 Cookie 的原始作用域；不写入邮箱密码。"""
    fields = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    return [
        {field: cookie[field] for field in fields if field in cookie}
        for cookie in cookies
        if cookie.get("name") in {"sso", "sso-rw"}
    ]

def _cookie_items_from_response(response):
    cookies = []
    jar = getattr(response, "cookies", None)
    try:
        iterator = jar.items()
    except Exception:
        iterator = ()
    for name, value in iterator:
        cookies.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": "accounts.x.ai",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    set_cookie = _response_header(response, "set-cookie", "")
    if set_cookie:
        for part in str(set_cookie).split(","):
            match = re.match(r"\s*([^=;,]+)=([^;]*)", part)
            if not match:
                continue
            name, value = match.group(1), match.group(2)
            if name in {"sso", "sso-rw"}:
                cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": "accounts.x.ai",
                        "path": "/",
                        "httpOnly": "httponly" in part.lower(),
                        "secure": "secure" in part.lower(),
                        "sameSite": "Lax",
                    }
                )
    return cookies

def _sso_from_cookie_items(cookies):
    return next((c.get("value") for c in cookies if c.get("name") == "sso"), None)

async def _server_action_register_http(page, payload):
    response = await _xai_http_request_async(
        "POST",
        f"{SITE_URL}/sign-up",
        page=page,
        headers={
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-router-state-tree": STATE_TREE,
            "next-action": ACTION_ID,
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
        },
        data=payload,
        timeout=30,
    )
    if response is None or not hasattr(response, "text"):
        return None
    return {
        "status": getattr(response, "status_code", 200) or 200,
        "retryAfter": _response_header(response, "retry-after", ""),
        "text": _response_text(response),
    }

async def _get_sso_via_ares_set_cookie(page, url, *, include_session=False):
    if not _cf_ares_xai_mode_enabled():
        return None
    try:
        response = await _xai_http_request_async(
            "GET",
            url,
            page=page,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
            },
            timeout=15,
        )
        cookies = _cookie_items_from_response(response)
        sso = _sso_from_cookie_items(cookies)
        if not sso:
            return None
        if include_session:
            return sso, auth_cookie_snapshot(cookies)
        return sso
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f"[xAI] set-cookie via CF-Ares failed: {sanitize_terminal_error(exc)}")
        return None


async def server_action_register(
    page,
    email,
    password,
    code,
    turnstile_token,
    *,
    include_session=False,
):
    payload = json.dumps([{
        "emailValidationCode": code,
        "createUserAndSessionRequest": {
            "email": email,
            "givenName": random.choice(["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles"]),
            "familyName": random.choice(["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]),
            "clearTextPassword": password, "tosAcceptedVersion": "$undefined"
        },
        "turnstileToken": turnstile_token, "promptOnDuplicateEmail": True
    }])
    pb64 = base64.b64encode(payload.encode()).decode()
    diagnostic = None
    result_text = None
    if _cf_ares_xai_mode_enabled():
        try:
            diagnostic = await _server_action_register_http(page, payload)
            if diagnostic is not None:
                result_text = diagnostic["text"]
                debug_log("[xAI] signup submitted via CF-Ares HTTP")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log(f"[xAI] signup HTTP fallback to page fetch: {sanitize_terminal_error(exc)}")

    if result_text is None and REGISTRATION_DIAGNOSTICS:
        diagnostic_json = await page.evaluate(f"""(async()=>{{var r=await fetch('{SITE_URL}/sign-up',{{method:'POST',headers:{{'accept':'text/x-component','content-type':'text/plain;charset=UTF-8','next-router-state-tree':'{STATE_TREE}','next-action':'{ACTION_ID}'}},body:atob('{pb64}')}});return JSON.stringify({{status:r.status,retryAfter:r.headers.get('retry-after')||'',text:await r.text()}});}})()""")
        diagnostic = json.loads(diagnostic_json)
        result_text = diagnostic['text']
    elif result_text is None:
        diagnostic = None
        result_text = await page.evaluate(f"""(async()=>{{var r=await fetch('{SITE_URL}/sign-up',{{method:'POST',headers:{{'accept':'text/x-component','content-type':'text/plain;charset=UTF-8','next-router-state-tree':'{STATE_TREE}','next-action':'{ACTION_ID}'}},body:atob('{pb64}')}});return await r.text();}})()""")
    # 注册响应里带一个 set-cookie 重定向 URL,必须访问它,x.ai 才会下发真正的 sso cookie(152 字符 JWT)。
    # 注意:直接解 q= 里的 JWT 取 config.token 是错的——那是 120 字符内部 blob,不是 sso 凭证。
    text = result_text.replace('\\/', '/')  # RSC 里 / 被转义成 \/
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[^:" \s\\]+)1:', text)
    if not m:
        m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[A-Za-z0-9_.\-]+)', text)
    if not m:
        markers = _signup_response_markers(result_text)
        if diagnostic is not None:
            debug_log(
                f"[C] signup no session http_status={diagnostic['status']} "
                f"retry_after={diagnostic['retryAfter'] or '-'} response_bytes={len(result_text)} "
                f"markers={markers}"
            )
        if "rate_limited" in markers:
            raise RegistrationRateLimited("signup_rate_limited")
        return None
    url = m.group(1)
    ares_result = await _get_sso_via_ares_set_cookie(
        page,
        url,
        include_session=include_session,
    )
    if ares_result:
        return ares_result
    if C_SET_COOKIE_VIA_REQUEST:
        try:
            await page.context.request.get(url, timeout=15000)
            cookies = await page.context.cookies()
            sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
            if sso:
                if include_session:
                    return sso, auth_cookie_snapshot(cookies)
                return sso
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # 首方导航访问该 URL,浏览器正常落 sso cookie(跨域 fetch 会被 CORS/三方cookie 拦)
    try:
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    cookies = await page.context.cookies()
    sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
    if REGISTRATION_DIAGNOSTICS and not sso:
        debug_log('[C] signup set-cookie completed without sso cookie')
    if not sso:
        return None
    if include_session:
        return sso, auth_cookie_snapshot(cookies)
    return sso

# solver 预热页面池:复用已停在 sign-up 的页面,省掉每次 page.goto 的重型 SPA 加载
# SOLVER_REUSE=0 可关闭(用于 A/B 对比 goto 优化的增益)
SOLVER_REUSE = (os.environ.get("SOLVER_REUSE", "1").strip().lower() not in ("0", "false", "no"))
_solver_pool = []
_solver_lock = asyncio.Lock()
MAX_SOLVER_REUSE = _env_int("MAX_SOLVER_REUSE", 25)
SOLVER_INITIAL_WAIT_MS = _env_int("SOLVER_INITIAL_WAIT_MS", 500)
SOLVER_POLL_INTERVAL_MS = _env_int("SOLVER_POLL_INTERVAL_MS", 500)
SOLVER_POLL_ATTEMPTS = _env_int("SOLVER_POLL_ATTEMPTS", 100)
SOLVER_HARD_TIMEOUT = max(10, _env_int("SOLVER_HARD_TIMEOUT", 90))
SOLVER_CLEANUP_TIMEOUT = max(1, _env_int("SOLVER_CLEANUP_TIMEOUT", 5))
SOLVER_FAST_CLICK = (os.environ.get("SOLVER_FAST_CLICK", "1").strip().lower() not in ("0", "false", "no"))
SOLVER_MOUSE_CLICK_RETRIES = _env_int("SOLVER_MOUSE_CLICK_RETRIES", 3)
SOLVER_MOUSE_CLICK_INTERVAL_MS = _env_int("SOLVER_MOUSE_CLICK_INTERVAL_MS", 600)
SOLVER_TIMELINE_TRACE = (os.environ.get("SOLVER_TIMELINE_TRACE", "0").strip().lower() in ("1", "true", "yes"))
SOLVER_TIMELINE_SAMPLE = _env_int("SOLVER_TIMELINE_SAMPLE", 8)
# Turnstile 后端(默认 hybrid=Go 网关多核异步):
#   hybrid — native/solver-gateway + Rust 看门狗 + C++ util + Python browser worker
#   d3vin  — 旧 vendor d3vin(兼容，后续删除)
#   theyka — 旧 vendor theyka(兼容，后续删除)
#   local  — 本机 Playwright 注入
#   api    — 外部 REST API
from grok_register.turnstile_solver import (
    ensure_solver_for_register,
    ensure_solver_if_needed,
    health_check as turnstile_health_check,
    is_api_backend,
    resolve_api_url,
    resolve_engine,
    resolve_solver_mode,
    stop_managed_solver as _stop_managed_turnstile_solver,
)

TURNSTILE_SOLVER = resolve_solver_mode()
TURNSTILE_API_URL = resolve_api_url(TURNSTILE_SOLVER)
TURNSTILE_API_POLL_INTERVAL_MS = max(50, _env_int("TURNSTILE_API_POLL_INTERVAL_MS", 400))
# hybrid 默认稍长超时；旧引擎仍可用 SOLVER_HARD_TIMEOUT
_default_api_timeout = 120 if TURNSTILE_SOLVER == "hybrid" else SOLVER_HARD_TIMEOUT
TURNSTILE_API_TIMEOUT = max(10, _env_int("TURNSTILE_API_TIMEOUT", _default_api_timeout))
TURNSTILE_API_ACTION = (os.environ.get("TURNSTILE_API_ACTION") or "").strip()
TURNSTILE_API_CDATA = (os.environ.get("TURNSTILE_API_CDATA") or "").strip()
_solver_timeline_emitted = 0
_solver_timeline_next_id = 0


def _new_solver_timeline(*, enabled=None):
    global _solver_timeline_next_id
    enabled = SOLVER_TIMELINE_TRACE if enabled is None else enabled
    if not enabled:
        return None
    _solver_timeline_next_id += 1
    return {"start": time.time(), "solve_id": _solver_timeline_next_id, "events": []}


def _trace_solver_event(timeline, event, **fields):
    if timeline is None:
        return
    item = {"t": round(time.time() - timeline["start"], 4), "event": event}
    if "solve_id" in timeline:
        item["solve_id"] = timeline["solve_id"]
    item.update(fields)
    timeline["events"].append(item)


def _new_solver_poll_stats():
    return {
        "poll_attempts": 0,
        "first_token_attempt": None,
        "poll_read_ms_total": 0.0,
        "poll_read_ms_max": 0.0,
        "poll_read_count": 0,
        "poll_retry_click_count": 0,
    }


def _finish_solver_poll_stats(stats):
    if not stats:
        return {}
    read_count = stats.pop("poll_read_count", 0)
    total = stats.pop("poll_read_ms_total", 0.0)
    stats["poll_read_ms_avg"] = round(total / read_count, 1) if read_count else 0.0
    stats["poll_read_ms_max"] = round(stats.get("poll_read_ms_max", 0.0), 1)
    return stats


async def _turnstile_frame_count(p):
    try:
        return await p.evaluate(
            "() => document.querySelectorAll('iframe[src*=turnstile], iframe[src*=challenges.cloudflare.com]').length"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return 0


async def _turnstile_dom_snapshot(p):
    try:
        return await p.evaluate(
            """() => {
                const clip = (value, n = 96) => String(value || "").slice(0, n);
                const rectInfo = (el) => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        visible: r.width >= 1 && r.height >= 1
                    };
                };
                const elemInfo = (el) => {
                    if (!el) return null;
                    return {
                        tag: clip(el.tagName),
                        id: clip(el.id, 64),
                        class: clip(el.className, 96),
                        is_iframe: el.tagName === "IFRAME"
                    };
                };
                const urlInfo = (src) => {
                    try {
                        const u = new URL(src || "", location.href);
                        return {host: clip(u.host, 96), path: clip(u.pathname, 96)};
                    } catch (_) {
                        return {host: "", path: ""};
                    }
                };
                const widget = document.querySelector(".cf-turnstile");
                const wr = rectInfo(widget);
                const center = wr ? {
                    x: Math.round(wr.x + wr.w / 2),
                    y: Math.round(wr.y + wr.h / 2)
                } : null;
                const centerEl = center ? document.elementFromPoint(center.x, center.y) : null;
                const iframes = Array.from(document.querySelectorAll("iframe"));
                const iframeSummaries = iframes.slice(0, 8).map((f) => {
                    const info = Object.assign(urlInfo(f.getAttribute("src") || ""), rectInfo(f) || {});
                    info.in_widget = widget ? widget.contains(f) : false;
                    return info;
                });
                const isTurnstileFrame = (f) => {
                    const src = f.getAttribute("src") || "";
                    return src.includes("turnstile") || src.includes("challenges.cloudflare.com");
                };
                const response = document.querySelector('input[name="cf-turnstile-response"]');
                return {
                    __csp_solver_snapshot: true,
                    ready_state: document.readyState,
                    viewport: {w: window.innerWidth, h: window.innerHeight},
                    widget: Object.assign({present: Boolean(widget)}, wr || {}),
                    click_center: center,
                    element_at_center: elemInfo(centerEl),
                    all_iframe_count: iframes.length,
                    turnstile_iframe_count: iframes.filter(isTurnstileFrame).length,
                    iframe_summaries: iframeSummaries,
                    turnstile_loaded: Boolean(window.turnstile),
                    response_input: {
                        present: Boolean(response),
                        token_len: response && response.value ? response.value.length : 0
                    }
                };
            }"""
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}


async def _turnstile_page_trace(p):
    try:
        return await p.evaluate(
            "() => window.__cspTurnstileTrace ? Object.assign({}, window.__cspTurnstileTrace) : null"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _get_solver_page(browser):
    if SOLVER_REUSE:
        async with _solver_lock:
            if _solver_pool:
                item = _solver_pool.pop()
                item["reused"] = True
                item["goto_s"] = 0.0
                return item
    context, p = await _new_grok_page(browser)
    await p.set_viewport_size({"width": 800, "height": 600})
    goto_started = time.time()
    await p.goto(f'{SITE_URL}/sign-up', timeout=20000)
    await p.wait_for_timeout(1000)
    return {
        "context": context,
        "page": p,
        "n": 0,
        "reused": False,
        "goto_s": time.time() - goto_started,
    }

async def _put_solver_page(item, ok):
    p = item["page"]
    item["n"] += 1
    if SOLVER_REUSE and ok and item["n"] < MAX_SOLVER_REUSE:
        try:  # 清理本次注入痕迹,留待复用
            await asyncio.wait_for(
                p.evaluate("document.querySelectorAll('.cf-turnstile').forEach(e=>e.remove());var i=document.querySelector('input[name=\"cf-turnstile-response\"]');if(i)i.remove();"),
                timeout=SOLVER_CLEANUP_TIMEOUT,
            )
            async with _solver_lock:
                _solver_pool.append(item)
            return
        except asyncio.CancelledError:
            await _close_solver_page(item)
            raise
        except Exception:
            pass
    await _close_solver_page(item)


async def _close_solver_page(item):
    """Bound cleanup so a wedged renderer cannot trap the worker in finally."""
    page = item["page"]
    context = item.get("context")

    async def close():
        await _close_grok_page(context, page)

    try:
        await asyncio.wait_for(close(), timeout=SOLVER_CLEANUP_TIMEOUT)
    except asyncio.CancelledError:
        # Solver cancellation is expected at the hard deadline.  Give cleanup
        # its own bounded task so the page is not returned to the reuse pool.
        cleanup = asyncio.create_task(close())
        try:
            await asyncio.wait_for(cleanup, timeout=SOLVER_CLEANUP_TIMEOUT)
        except BaseException:
            cleanup.cancel()
        raise
    except Exception:
        pass

async def _inject_turnstile_widget(p, *, timeline=False):
    if not timeline:
        await p.evaluate(f"""var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{window.turnstile&&window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;}}}})}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{setTimeout(__r,1000)}};document.head.appendChild(s);}}""")
        return
    await p.evaluate(f"""var __trace=window.__cspTurnstileTrace={{created_at:performance.now(),script_inserted_at:null,script_loaded_at:null,render_called_at:null,render_returned_at:null,token_written_at:null,token_len:0,error:null}};var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{try{{if(!window.turnstile)return;__trace.render_called_at=performance.now();var __ret=window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;__trace.token_written_at=performance.now();__trace.token_len=t?t.length:0;}}}});__trace.render_returned_at=performance.now();__trace.render_return_type=typeof __ret;}}catch(e){{__trace.error=e&&e.name?e.name:String(e);}}}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{__trace.script_loaded_at=performance.now();setTimeout(__r,1000)}};__trace.script_inserted_at=performance.now();document.head.appendChild(s);}}""")


async def _has_visible_turnstile_frame(p):
    try:
        return await p.evaluate(
            """() => Array.from(document.querySelectorAll('iframe')).some((f) => {
                const r = f.getBoundingClientRect();
                return r.width >= 20 && r.height >= 20;
            })"""
        )
    except Exception:
        return False


async def _read_turnstile_token(p):
    try:
        return await p.evaluate('document.querySelector("input[name=\\"cf-turnstile-response\\"]")?.value||""')
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""


async def _mouse_click_turnstile_center(p):
    clicked, _trace = await _mouse_click_turnstile_center_trace(p)
    return clicked


async def _mouse_click_turnstile_center_trace(p):
    trace = {}
    started = time.time()
    box = await p.evaluate(
        """() => {
            const e = document.querySelector('.cf-turnstile');
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }"""
    )
    trace["box_eval_ms"] = round((time.time() - started) * 1000, 1)
    if not box:
        return False, trace
    x = float(box["x"])
    y = float(box["y"])
    trace["click_x"] = round(x, 1)
    trace["click_y"] = round(y, 1)
    started = time.time()
    await p.mouse.move(max(0, x - 25), max(0, y - 8))
    trace["mouse_move1_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.move(x, y, steps=8)
    trace["mouse_move2_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.down()
    trace["mouse_down_ms"] = round((time.time() - started) * 1000, 1)
    await asyncio.sleep(0.05)
    started = time.time()
    await p.mouse.up()
    trace["mouse_up_ms"] = round((time.time() - started) * 1000, 1)
    return True, trace


async def _repeat_mouse_click_turnstile(p, *, timeline=None):
    retries = max(0, SOLVER_MOUSE_CLICK_RETRIES)
    if retries <= 0:
        return False
    clicked = False
    interval = max(50, SOLVER_MOUSE_CLICK_INTERVAL_MS) / 1000.0
    for i in range(retries):
        token = await _read_turnstile_token(p)
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_before", attempt=i + 1,
            token_len=len(token or ""), iframe_count=iframe_count, dom=dom
        )
        if token and len(token) > 10:
            return clicked
        click_started = time.time()
        click_error = None
        try:
            if timeline is not None:
                attempt_clicked, click_trace = await _mouse_click_turnstile_center_trace(p)
            else:
                attempt_clicked = await _mouse_click_turnstile_center(p)
                click_trace = {}
            clicked = attempt_clicked or clicked
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            click_error = type(exc).__name__
            attempt_clicked = False
            click_trace = {}
        click_call_ms = round((time.time() - click_started) * 1000, 1)
        token = await _read_turnstile_token(p) if timeline is not None else ""
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_after", attempt=i + 1, clicked=clicked,
            attempt_clicked=attempt_clicked,
            token_len=len(token or ""), iframe_count=iframe_count,
            click_call_ms=click_call_ms, click_error=click_error,
            click_trace=click_trace, dom=dom
        )
        if i != retries - 1:
            await asyncio.sleep(interval)
    return clicked


async def _click_turnstile_if_possible(p, *, fast=False, timeline=None):
    if SOLVER_MOUSE_CLICK_RETRIES > 0:
        return await _repeat_mouse_click_turnstile(p, timeline=timeline)
    visible = await _has_visible_turnstile_frame(p)
    _trace_solver_event(timeline, "visible_check", visible=visible)
    if fast and not visible:
        return visible
    click_timeout = 500 if fast else 3000
    for sel in ["iframe[src*='challenges.cloudflare.com']","iframe[src*='turnstile']",".cf-turnstile iframe"]:
        try:
            fr = p.frame_locator(sel).first
            await fr.locator("#checkbox, .checkbox, input[type=checkbox], body").first.click(timeout=click_timeout)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return visible


async def _poll_turnstile_token(p, *, stats=None):
    for i in range(SOLVER_POLL_ATTEMPTS):
        await asyncio.sleep(max(50, SOLVER_POLL_INTERVAL_MS) / 1000)
        if stats is not None:
            stats["poll_attempts"] = i + 1
            read_started = time.time()
        try:
            t = await _read_turnstile_token(p)
            if stats is not None:
                read_ms = (time.time() - read_started) * 1000
                stats["poll_read_count"] += 1
                stats["poll_read_ms_total"] += read_ms
                stats["poll_read_ms_max"] = max(stats["poll_read_ms_max"], read_ms)
            if t and len(t) > 10:
                if stats is not None and stats["first_token_attempt"] is None:
                    stats["first_token_attempt"] = i + 1
                return t
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        retry_every = max(1, int(10000 / max(50, SOLVER_POLL_INTERVAL_MS)))
        if i > 0 and i % retry_every == 0:
            try:
                await p.locator(".cf-turnstile").first.click(timeout=1000)
                if stats is not None:
                    stats["poll_retry_click_count"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    return None


async def _start_turnstile_challenge(browser, *, fast_click=False):
    global _solver_timeline_emitted
    item = await _get_solver_page(browser)
    p = item["page"]
    trace_timeline = SOLVER_TIMELINE_TRACE and _solver_timeline_emitted < SOLVER_TIMELINE_SAMPLE
    if trace_timeline:
        _solver_timeline_emitted += 1
    timeline = _new_solver_timeline(enabled=trace_timeline)
    trace = {
        "goto_s": item.get("goto_s", 0.0),
        "reused": bool(item.get("reused", False)),
        "reuse_count": item.get("n", 0),
        "inject_s": 0.0,
        "initial_s": 0.0,
        "click_s": 0.0,
        "wait_s": 0.0,
        "visible_frame": False,
    }
    item["trace"] = trace
    item["timeline"] = timeline
    try:
        stage_started = time.time()
        _trace_solver_event(timeline, "inject_start")
        await _inject_turnstile_widget(p, timeline=timeline is not None)
        trace["inject_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "inject_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_inject",
                page_trace=await _turnstile_page_trace(p)
            )
        stage_started = time.time()
        await p.wait_for_timeout(SOLVER_INITIAL_WAIT_MS)
        trace["initial_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "initial_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_initial",
                page_trace=await _turnstile_page_trace(p)
            )
        stage_started = time.time()
        trace["visible_frame"] = await _click_turnstile_if_possible(
            p, fast=fast_click, timeline=timeline
        )
        trace["click_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "click_stage_done", clicked=bool(trace["visible_frame"]))
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_click",
                page_trace=await _turnstile_page_trace(p)
            )
        return item
    except BaseException:
        await _put_solver_page(item, False)
        raise


async def _wait_turnstile_challenge(item):
    ok = False
    try:
        wait_started = time.time()
        timeline = item.get("timeline")
        poll_stats = _new_solver_poll_stats() if timeline is not None else None
        _trace_solver_event(timeline, "poll_start")
        token = await _poll_turnstile_token(item["page"], stats=poll_stats)
        item.get("trace", {})["wait_s"] = time.time() - wait_started
        ok = token is not None
        page_trace = await _turnstile_page_trace(item["page"]) if timeline is not None else None
        _trace_solver_event(
            timeline, "poll_done", ok=ok, token_len=len(token or ""),
            page_trace=page_trace, **_finish_solver_poll_stats(poll_stats)
        )
        return token
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    finally:
        timeline = item.get("timeline")
        if timeline is not None:
            debug_log("[solver_timeline] " + json.dumps(timeline["events"], separators=(",", ":")))
        await _put_solver_page(item, ok)


def _turnstile_api_task_id(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("task_id", "taskId", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _turnstile_api_result_state(payload):
    """归一化 Theyka / D3-vin 的 /result 响应。

    返回 (state, token):
      pending  — 仍在求解
      ready    — 拿到 token
      failed   — 明确失败
      error    — 响应不可解析
    """
    if payload is None:
        return "error", None
    if isinstance(payload, str):
        text = payload.strip()
        if text in ("CAPTCHA_NOT_READY", "processing", "PENDING", "pending"):
            return "pending", None
        if text in ("CAPTCHA_FAIL", "error", "failed", "ERROR"):
            return "failed", None
        if len(text) > 10:
            return "ready", text
        return "error", None
    if not isinstance(payload, dict):
        return "error", None

    status = str(payload.get("status") or "").strip().lower()
    if status in ("processing", "pending", "captcha_not_ready", "in_progress"):
        return "pending", None
    if status in ("error", "failed", "fail"):
        return "failed", None

    value = payload.get("value")
    if value is None and isinstance(payload.get("solution"), dict):
        value = payload["solution"].get("token") or payload["solution"].get("value")
    if value is None:
        value = payload.get("token")

    if isinstance(value, str):
        text = value.strip()
        if text in ("CAPTCHA_NOT_READY", "processing", "PENDING", "pending"):
            return "pending", None
        if text in ("CAPTCHA_FAIL", "error", "failed", "ERROR"):
            return "failed", None
        if len(text) > 10:
            return "ready", text

    if status == "ready":
        return "error", None
    if payload.get("error") or payload.get("errorCode"):
        return "failed", None
    return "error", None


def _turnstile_api_http_get(url, *, timeout):
    """同步 GET;不走代理(外部 solver 通常在本机/内网)。

    timeout 可为 float 秒,或 (connect, read) 元组。
    """
    return req.get(url, timeout=timeout, proxies={"http": None, "https": None})


# 限制同时打到内置 solver 的任务数,避免 5 个 S_Worker 把 2 线程 solver 打满后互相拖死
_turnstile_api_inflight = None


def _get_turnstile_api_inflight():
    global _turnstile_api_inflight
    if _turnstile_api_inflight is None:
        # hybrid: align with effective slots (workers × concurrency), else threads
        if TURNSTILE_SOLVER == "hybrid":
            workers_raw = (os.environ.get("SOLVER_GATEWAY_WORKERS") or "auto").strip().lower()
            try:
                import os as _os

                cores = max(1, (_os.cpu_count() or 2) - 1)
            except Exception:
                cores = 2
            if workers_raw in ("", "auto", "0"):
                workers = max(1, min(cores, _env_int("SOLVER_GATEWAY_WORKERS_MAX", 6)))
            else:
                try:
                    workers = max(1, int(workers_raw))
                except ValueError:
                    workers = max(1, _env_int("TURNSTILE_SOLVER_THREADS", 2))
            conc = _env_int("SOLVER_WORKER_CONCURRENCY", 0)
            if conc <= 0:
                conc = 2 if workers <= 2 and cores >= 3 else 1
            default_inflight = max(1, workers * conc)
        else:
            default_inflight = max(1, _env_int("TURNSTILE_SOLVER_THREADS", 2))
        inflight = max(1, _env_int("TURNSTILE_API_INFLIGHT", default_inflight))
        _turnstile_api_inflight = asyncio.Semaphore(inflight)
        debug_log(
            f"[solver-api] inflight={inflight} mode={TURNSTILE_SOLVER} engine={resolve_engine()}"
        )
    return _turnstile_api_inflight


async def solve_one_turnstile_via_api():
    """通过 Theyka / D3-vin 兼容 REST API 获取 Turnstile token。

    On-demand: if API is down, try to start managed hybrid/d3vin once, then retry.
    """
    if not SITE_KEY:
        debug_log("[solver-api] SITE_KEY missing")
        return None, {"backend": "api", "error": "no_sitekey"}

    global TURNSTILE_API_URL
    # Lazy start when on-demand and solver not yet listening
    if is_api_backend(TURNSTILE_SOLVER) and not turnstile_health_check(TURNSTILE_API_URL, timeout=1.0):
        try:
            meta = await asyncio.to_thread(
                ensure_solver_if_needed, log=lambda m: debug_log(m)
            )
            TURNSTILE_API_URL = meta.get("api_url") or TURNSTILE_API_URL
            if meta.get("managed"):
                atexit.register(_stop_managed_turnstile_solver)
                debug_log(f"[solver-api] on-demand started {TURNSTILE_API_URL}")
        except Exception as exc:
            debug_log(f"[solver-api] on-demand start failed: {sanitize_terminal_error(exc)}")

    sem = _get_turnstile_api_inflight()
    await sem.acquire()
    try:
        return await _solve_one_turnstile_via_api_unlocked()
    finally:
        sem.release()


async def _solve_one_turnstile_via_api_unlocked():
    base = TURNSTILE_API_URL
    page_url = f"{SITE_URL}/sign-up"
    params = [f"url={quote(page_url, safe='')}", f"sitekey={quote(SITE_KEY, safe='')}"]
    if TURNSTILE_API_ACTION:
        params.append(f"action={quote(TURNSTILE_API_ACTION, safe='')}")
    if TURNSTILE_API_CDATA:
        params.append(f"cdata={quote(TURNSTILE_API_CDATA, safe='')}")
    create_url = f"{base}/turnstile?{'&'.join(params)}"

    trace = {
        "backend": "api",
        "api_url": base,
        "goto_s": 0.0,
        "inject_s": 0.0,
        "initial_s": 0.0,
        "click_s": 0.0,
        "wait_s": 0.0,
        "reused": False,
        "visible_frame": False,
        "task_id": None,
        "polls": 0,
        "error": None,
    }
    # create 在 D3-vin 上应很快返回 taskId;给足 connect/read 避免偶发阻塞
    create_timeout = (5, min(30, TURNSTILE_API_TIMEOUT))
    try:
        create_started = time.time()
        response = await asyncio.to_thread(
            _turnstile_api_http_get, create_url, timeout=create_timeout
        )
        trace["inject_s"] = time.time() - create_started
    except Exception as exc:
        trace["error"] = type(exc).__name__
        debug_log(f"[solver-api] create failed: {sanitize_terminal_error(exc)}")
        return None, trace

    try:
        payload = response.json()
    except Exception:
        payload = None
    if response.status_code >= 400:
        trace["error"] = f"http_{response.status_code}"
        debug_log(f"[solver-api] create HTTP {response.status_code}: {str(response.text)[:200]}")
        return None, trace

    task_id = _turnstile_api_task_id(payload)
    if not task_id:
        # 少数实现可能同步直接返回 token
        state, token = _turnstile_api_result_state(payload)
        if state == "ready" and token:
            return token, trace
        trace["error"] = "no_task_id"
        debug_log(f"[solver-api] create missing task_id: {str(payload)[:200]}")
        return None, trace
    trace["task_id"] = task_id

    result_url = f"{base}/result?id={quote(task_id, safe='')}"
    poll_interval = TURNSTILE_API_POLL_INTERVAL_MS / 1000.0
    deadline = time.time() + TURNSTILE_API_TIMEOUT
    wait_started = time.time()
    consecutive_timeouts = 0
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        trace["polls"] += 1
        # D3-vin /result 在 event loop 忙时可能慢;用短 connect + 中等 read,超时后重试
        remaining = max(1.0, deadline - time.time())
        poll_timeout = (3, min(20.0, remaining))
        try:
            result_resp = await asyncio.to_thread(
                _turnstile_api_http_get,
                result_url,
                timeout=poll_timeout,
            )
            consecutive_timeouts = 0
        except Exception as exc:
            consecutive_timeouts += 1
            trace["error"] = type(exc).__name__
            if consecutive_timeouts <= 3 or consecutive_timeouts % 10 == 0:
                debug_log(
                    f"[solver-api] poll error ({consecutive_timeouts}x): "
                    f"{sanitize_terminal_error(exc)}"
                )
            continue
        try:
            result_payload = result_resp.json()
        except Exception:
            # Theyka 未就绪时可能直接返回 JSON 字符串 "CAPTCHA_NOT_READY"
            text = (result_resp.text or "").strip()
            if text.startswith('"') and text.endswith('"'):
                try:
                    result_payload = json.loads(text)
                except Exception:
                    result_payload = text.strip('"')
            else:
                result_payload = text or None

        state, token = _turnstile_api_result_state(result_payload)
        if state == "ready" and token:
            trace["wait_s"] = time.time() - wait_started
            trace["error"] = None
            return token, trace
        if state == "failed":
            trace["wait_s"] = time.time() - wait_started
            trace["error"] = "captcha_fail"
            debug_log(f"[solver-api] task {task_id} failed")
            return None, trace
        # pending / error with non-4xx keep polling until deadline
        if result_resp.status_code == 422:
            trace["wait_s"] = time.time() - wait_started
            trace["error"] = "http_422"
            return None, trace

    trace["wait_s"] = time.time() - wait_started
    trace["error"] = "timeout"
    debug_log(f"[solver-api] task {task_id} timeout after {TURNSTILE_API_TIMEOUT}s")
    return None, trace


async def solve_one_turnstile(browser):
    token, _trace = await solve_one_turnstile_with_trace(browser)
    return token


async def solve_one_turnstile_with_trace(browser):
    if is_api_backend(TURNSTILE_SOLVER):
        return await solve_one_turnstile_via_api()
    item = await _start_turnstile_challenge(browser, fast_click=SOLVER_FAST_CLICK)
    token = await _wait_turnstile_challenge(item)
    return token, item.get("trace", {})


def _record_solver_trace(metrics, trace, total_seconds, token):
    metrics.t_solve_count += 1
    metrics.t_solve_seconds += total_seconds
    if token is None:
        metrics.t_solve_failed += 1
    if not trace:
        return
    metrics.solver_goto_seconds += trace.get("goto_s", 0.0)
    metrics.solver_inject_seconds += trace.get("inject_s", 0.0)
    metrics.solver_initial_seconds += trace.get("initial_s", 0.0)
    metrics.solver_click_seconds += trace.get("click_s", 0.0)
    metrics.solver_wait_seconds += trace.get("wait_s", 0.0)
    if trace.get("reused"):
        metrics.solver_reused_count += 1
    if trace.get("visible_frame"):
        metrics.solver_visible_frame_count += 1


# ──────────────────────────────────────────────
#  邮箱服务:custom(自建 webhook) / moemail(OpenAPI) / tempmail(免费临时邮箱)
# ──────────────────────────────────────────────
# 免 key 的公共临时邮箱 provider(实测可用,互为 fallback 消灭单点):
#  - mail.tm 同协议:mail.tm / mail.gw / duckmail.sbs
#  - 独立 API:tempmail.lol
# 可选 provider:
#  - MoeMail OpenAPI:设置 MOEMAIL_API_KEY 后可单独用 EMAIL_MODE=moemail,也会加入 tempmail fallback
# handle 编码 provider,供 poll_code 分派;新增 provider 只要在这两处加一段即可。
TEMPMAIL_BASES = ["https://api.mail.tm", "https://api.mail.gw", "https://api.duckmail.sbs"]
_proxy_pool_cache = {"path": None, "mtime_ns": None, "items": (), "index": 0}
_proxy_pool_lock = threading.Lock()
_proxy_relay_link_cache = {}
_proxy_relay_external_retry_at = 0.0
_builtin_proxy_relay = None
_builtin_proxy_relay_lock = threading.Lock()
_proxy_auto_manager = None
_proxy_auto_manager_lock = threading.Lock()
_proxy_auto_startup_validated = False
_SHARE_LINK_SCHEMES = (
    "vmess", "vless", "trojan", "ss", "hy2", "hysteria2", "tuic", "anytls",
    # socks5 with auth is relayed via sing-box (Chromium cannot do SOCKS5 auth)
    "socks", "socks5", "socks5h",
)
_cf_ares_clients = {}
_cf_ares_import_error = None
_cf_ares_lock = threading.Lock()

def _normalize_proxy_line(line):
    proxy = (line or "").strip()
    if not proxy or proxy.startswith("#"):
        return None
    # telegram socks deep links → socks5h://user:pass@host:port
    if "t.me/socks" in proxy or "telegram.me/socks" in proxy:
        proxy = _telegram_socks_to_url(proxy) or proxy
    if _share_link_scheme(proxy):
        if _proxy_needs_relay(proxy):
            return _proxy_from_share_link(proxy)
        return _normalize_direct_proxy_url(proxy)
    lowered = proxy.lower()
    if "://" not in lowered:
        if ":" not in proxy:
            return None
        proxy = f"http://{proxy}"
        lowered = proxy.lower()
    if not lowered.startswith(("http://", "https://", "socks4://", "socks5://", "socks5h://")):
        return None
    if _proxy_needs_relay(proxy):
        return _proxy_from_share_link(proxy)
    return _normalize_direct_proxy_url(proxy)


def _telegram_socks_to_url(text):
    """Convert https://t.me/socks?server=...&port=...&user=...&pass=... to socks5h URL."""
    try:
        parsed = urlparse((text or "").strip())
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if "t.me" not in host and "telegram.me" not in host:
        return None
    if "socks" not in path:
        return None
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    server = (qs.get("server") or "").strip()
    port = (qs.get("port") or "").strip()
    user = (qs.get("user") or "").strip()
    password = (qs.get("pass") or qs.get("password") or "").strip()
    if not server or not port:
        return None
    if user or password:
        return f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{server}:{port}"
    return f"socks5h://{server}:{port}"


def _proxy_needs_relay(proxy):
    """Whether this proxy URL must be converted to a local HTTP relay.

    Playwright/Chromium cannot use SOCKS5 with username/password
    ("Browser does not support socks5 proxy authentication"), so authenticated
    socks5/socks5h are relayed to 127.0.0.1 HTTP via sing-box.

    Share-link protocols (vmess/vless/trojan/ss/...) also need a relay.
    """
    try:
        parsed = urlparse((proxy or "").strip())
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme in _SHARE_LINK_SCHEMES and scheme not in {
        "socks",
        "socks5",
        "socks5h",
        "http",
        "https",
    }:
        return True
    if scheme in {"socks", "socks5", "socks5h"} and (parsed.username or parsed.password):
        return True
    return False


def _normalize_direct_proxy_url(proxy):
    try:
        parsed = urlparse((proxy or "").strip())
    except Exception:
        return proxy
    scheme = (parsed.scheme or "").lower()
    if scheme == "socks":
        rest = (proxy or "").split("://", 1)[-1]
        return f"socks5://{rest}"
    return proxy

def _share_link_scheme(text):
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/", (text or "").strip())
    if not match:
        return ""
    scheme = match.group(1).lower()
    return scheme if scheme in _SHARE_LINK_SCHEMES else ""

def _relay_kernel_candidates(scheme):
    requested = (PROXY_RELAY_KERNEL or "auto").strip().lower().replace("_", "-")
    if requested in ("singbox", "sing-box"):
        return ("sing-box",)
    if requested == "xray":
        return ("xray",)
    if scheme in {"hy2", "hysteria2", "tuic", "anytls"}:
        return ("sing-box",)
    return ("sing-box", "xray")

def _relay_proxy_url(local_port, kernel="sing-box"):
    scheme = (PROXY_RELAY_PROXY_SCHEME or "auto").strip().lower()
    if scheme == "auto":
        scheme = "socks5" if (kernel or "").strip().lower() == "xray" else "http"
    if scheme not in {"http", "socks4", "socks5", "socks5h"}:
        scheme = "http"
    host = (PROXY_RELAY_HOST or "127.0.0.1").strip()
    return f"{scheme}://{host}:{int(local_port)}"

def _proxy_relay_json(method, path, payload=None):
    if not PROXY_RELAY_URL:
        raise RuntimeError("PROXY_RELAY_URL 未配置")
    url = PROXY_RELAY_URL.rstrip("/") + path
    if hasattr(req, "Session"):
        session = req.Session()
        session.trust_env = False
        response = session.request(
            method,
            url,
            json=payload,
            timeout=PROXY_RELAY_TIMEOUT,
        )
    else:
        response = getattr(req, method.lower())(
            url,
            json=payload,
            timeout=PROXY_RELAY_TIMEOUT,
        )
    data = response.json()
    status = getattr(response, "status_code", 200)
    failed = isinstance(data, dict) and data.get("ok") is False
    if status >= 400 or failed:
        message = (data.get("error") or data.get("message")) if isinstance(data, dict) else None
        raise RuntimeError(message or getattr(response, "text", "proxy relay request failed"))
    return data

def _builtin_proxy_relay_manager():
    global _builtin_proxy_relay
    if not PROXY_RELAY_BUILTIN_ENABLED:
        return None
    with _builtin_proxy_relay_lock:
        if _builtin_proxy_relay is None:
            _builtin_proxy_relay = BuiltinProxyRelay(
                BuiltinProxyRelayConfig(
                    enabled=PROXY_RELAY_BUILTIN_ENABLED,
                    host=PROXY_RELAY_HOST,
                    proxy_scheme=PROXY_RELAY_PROXY_SCHEME,
                    work_dir=PROXY_RELAY_WORK_DIR,
                    sing_box_bin=PROXY_RELAY_SING_BOX_BIN,
                    auto_install=PROXY_RELAY_AUTO_INSTALL,
                    start_port=PROXY_RELAY_START_PORT,
                    max_nodes=PROXY_RELAY_MAX_NODES,
                    start_timeout=PROXY_RELAY_START_TIMEOUT,
                ),
                logger=debug_log,
            )
        return _builtin_proxy_relay

def _builtin_proxy_relay_import(share_link, kernel="sing-box", local_port=""):
    manager = _builtin_proxy_relay_manager()
    if manager is None:
        raise RuntimeError("built-in proxy relay is disabled")
    node = manager.import_link(share_link, kernel=kernel, local_port=local_port)
    proxy = _proxy_from_relay_node(node)
    if not proxy:
        proxy = node.get("proxy") if isinstance(node, dict) else None
    return proxy

def _prune_builtin_proxy_relay(active_proxies):
    manager = _builtin_proxy_relay
    if manager is None:
        return
    try:
        manager.prune(active_proxies)
    except Exception as exc:
        debug_log(f"built-in proxy relay prune failed: {sanitize_terminal_error(exc)}")

def _is_builtin_relay_proxy(proxy):
    try:
        parsed = urlparse(proxy)
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    relay_host = (PROXY_RELAY_HOST or "127.0.0.1").strip().lower()
    if host not in {relay_host, "localhost", "127.0.0.1", "::1"}:
        return False
    try:
        port = int(parsed.port)
    except Exception:
        return False
    start = max(1, int(PROXY_RELAY_START_PORT))
    return start <= port < min(65536, start + 2000)

def _cleanup_tested_proxy(candidate, proxy, ok):
    if ok or not proxy or not _share_link_scheme(candidate):
        return
    _proxy_relay_link_cache.pop((candidate or "").strip(), None)
    manager = _builtin_proxy_relay
    if manager is None:
        return
    try:
        manager.stop_link(candidate)
    except Exception as exc:
        debug_log(f"built-in proxy relay cleanup failed: {sanitize_terminal_error(exc)}")

def _proxy_relay_runtime_hint():
    manager = _builtin_proxy_relay_manager() if PROXY_RELAY_BUILTIN_ENABLED else None
    if manager is None:
        return "built-in proxy relay disabled"
    return manager.runtime_hint()

def _external_proxy_relay_allowed():
    return bool(PROXY_RELAY_URL and time.time() >= float(_proxy_relay_external_retry_at or 0))

def _mark_external_proxy_relay_failed():
    global _proxy_relay_external_retry_at
    _proxy_relay_external_retry_at = time.time() + max(1, PROXY_RELAY_RETRY_SEC)

def _relay_state_nodes(state):
    if not isinstance(state, dict):
        return []
    nodes = state.get("nodes")
    if nodes is None and isinstance(state.get("data"), dict):
        nodes = state["data"].get("nodes")
    return nodes if isinstance(nodes, list) else []

def _relay_node_value(node, *keys):
    for key in keys:
        value = node.get(key) if isinstance(node, dict) else None
        if value not in (None, ""):
            return value
    return None

def _find_relay_node(state, share_link):
    for node in _relay_state_nodes(state):
        link = _relay_node_value(node, "link", "share_link", "shareLink", "url")
        if str(link or "").strip() == share_link:
            return node
    return None

def _proxy_from_relay_node(node):
    if not node:
        return None
    try:
        local_port = _relay_node_value(node, "local_port", "localPort", "listen_port", "listenPort")
        kernel = _relay_node_value(node, "kernel", "core") or "sing-box"
        return _relay_proxy_url(int(local_port), kernel)
    except Exception:
        return None

def _extract_relay_message_port(message):
    match = re.search(
        r"(?:本地端口|起始端口|local\s*port|start(?:ing)?\s*port)\D*(\d+)",
        message or "",
        re.I,
    )
    return int(match.group(1)) if match else None

def _proxy_from_share_link(share_link):
    share_link = share_link.strip()
    if not PROXY_RELAY_ENABLED:
        return None
    cached = _proxy_relay_link_cache.get(share_link)
    if cached:
        return cached

    scheme = _share_link_scheme(share_link)
    last_error = None
    if _external_proxy_relay_allowed():
        try:
            state = _proxy_relay_json("GET", "/api/state")
            proxy = _proxy_from_relay_node(_find_relay_node(state, share_link))
            if proxy:
                _proxy_relay_link_cache[share_link] = proxy
                return proxy

            for kernel in _relay_kernel_candidates(scheme):
                try:
                    result = _proxy_relay_json(
                        "POST",
                        "/api/nodes/import",
                        {"share_link": share_link, "kernel": kernel, "local_port": ""},
                    )
                    state = _proxy_relay_json("GET", "/api/state")
                    proxy = _proxy_from_relay_node(_find_relay_node(state, share_link))
                    if not proxy:
                        port = _extract_relay_message_port(result.get("message", ""))
                        proxy = _relay_proxy_url(port, kernel) if port else None
                    if proxy:
                        _proxy_relay_link_cache[share_link] = proxy
                        return proxy
                except Exception as exc:
                    last_error = exc
        except Exception as exc:
            last_error = exc
            _mark_external_proxy_relay_failed()

    if PROXY_RELAY_BUILTIN_ENABLED:
        for kernel in _relay_kernel_candidates(scheme):
            if kernel != "sing-box":
                continue
            try:
                proxy = _builtin_proxy_relay_import(share_link, kernel=kernel, local_port="")
                if proxy:
                    _proxy_relay_link_cache[share_link] = proxy
                    return proxy
            except Exception as exc:
                last_error = exc

    if last_error:
        debug_log(f"代理池分享链接转换失败({scheme or 'unknown'}): {last_error}")
    return None

def _unique_proxies(items):
    seen = set()
    out = []
    for item in items:
        proxy = (item or "").strip()
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        out.append(proxy)
    return out

def _auto_bootstrap_proxies():
    cached = []
    with _proxy_pool_lock:
        cached.extend(
            proxy
            for proxy in (_proxy_pool_cache.get("items", ()) or ())
            if not _is_builtin_relay_proxy(proxy)
        )
    manual = []
    for candidate in _proxy_pool_paths():
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            proxy = _normalize_proxy_line(raw)
            if proxy:
                manual.append(proxy)
        break
    active = [
        proxy
        for proxy in load_active_proxies(PROXY_AUTO_CONFIG)
        if not _is_builtin_relay_proxy(proxy)
    ]
    return _unique_proxies(cached + manual + active)

def _ensure_proxy_auto_manager_started():
    global _proxy_auto_manager
    if not PROXY_AUTO_CONFIG.enabled:
        return None
    with _proxy_auto_manager_lock:
        if _proxy_auto_manager is None:
            _proxy_auto_manager = ProxyAutoManager(
                PROXY_AUTO_CONFIG,
                _normalize_proxy_line,
                bootstrap_proxies=_auto_bootstrap_proxies,
                cleanup_proxy=_cleanup_tested_proxy,
                logger=debug_log,
            )
        _proxy_auto_manager.start()
        return _proxy_auto_manager

def _refresh_auto_proxy_pool_once():
    """Refresh the auto proxy pool synchronously before starting workers."""
    global _proxy_auto_manager
    if not PROXY_AUTO_CONFIG.enabled:
        return load_active_proxies(PROXY_AUTO_CONFIG)
    with _proxy_auto_manager_lock:
        if _proxy_auto_manager is None:
            _proxy_auto_manager = ProxyAutoManager(
                PROXY_AUTO_CONFIG,
                _normalize_proxy_line,
                bootstrap_proxies=_auto_bootstrap_proxies,
                cleanup_proxy=_cleanup_tested_proxy,
                logger=debug_log,
            )
        manager = _proxy_auto_manager
    proxies = manager.refresh_once()
    proxies = proxies or load_active_proxies(PROXY_AUTO_CONFIG)
    _prune_builtin_proxy_relay(proxies)
    return proxies

def _proxy_auto_no_active_message():
    message = "Proxy auto fetch produced no active proxies"
    hints = []
    try:
        state = json.loads(PROXY_AUTO_CONFIG.state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    if isinstance(state, dict):
        test_count = state.get("test_count")
        failed_count = state.get("failed_count")
        if test_count is not None:
            hints.append(f"tested={test_count}")
        if failed_count is not None:
            hints.append(f"failed={failed_count}")

    error_summary = state.get("error_summary") if isinstance(state, dict) else None
    if isinstance(error_summary, dict) and error_summary:
        failures = ", ".join(
            f"{reason} x{count}"
            for reason, count in list(error_summary.items())[:3]
        )
        hints.append(f"failures: {failures}")

    if (
        PROXY_RELAY_ENABLED
        and isinstance(error_summary, dict)
        and error_summary.get("unsupported proxy")
    ):
        try:
            _proxy_relay_json("GET", "/api/state")
        except Exception as exc:
            hints.append(
                f"proxy-relay is not reachable at {PROXY_RELAY_URL}: {exc}"
            )
        hints.append(
            f"built-in relay: {_proxy_relay_runtime_hint()}"
        )
        hints.append(
            "start proxy-relay, enable built-in relay, or provide http/socks proxies instead of share links"
        )

    hints.append(f"state={PROXY_AUTO_CONFIG.state_path}")
    if not hints:
        return message
    return f"{message} ({'; '.join(hints)})"

async def _prepare_auto_proxy_pool_before_start():
    global _proxy_auto_startup_validated
    if not PROXY_AUTO_CONFIG.enabled:
        return
    test_targets = ", ".join(PROXY_AUTO_CONFIG.test_urls)
    log(f"[proxy-auto] 启动前拉取代理并测试 Grok 连通性: {test_targets}")
    debug_log("[proxy-auto] refreshing before registration start")
    proxies = await asyncio.to_thread(_refresh_auto_proxy_pool_once)
    debug_log(f"[proxy-auto] startup active={len(proxies)}")
    if PROXY_AUTO_REQUIRE_ACTIVE and not proxies:
        message = _proxy_auto_no_active_message()
        log(f"[proxy-auto] 未筛出可用代理: {message}")
        raise RuntimeError(message)
    if proxies:
        _proxy_auto_startup_validated = True
        log(
            f"[proxy-auto] 已优选 {len(proxies)} 个可访问 Grok/xAI 的代理, "
            f"写入 {PROXY_AUTO_CONFIG.active_path}"
        )
    else:
        log("[proxy-auto] 未筛出可用代理,按当前配置继续运行")
    _ensure_proxy_auto_manager_started()

def _auto_proxy_mtime_ns():
    if not PROXY_AUTO_CONFIG.enabled:
        return None
    try:
        return PROXY_AUTO_CONFIG.active_path.stat().st_mtime_ns
    except OSError:
        return None

def _load_auto_proxy_items():
    if not PROXY_AUTO_CONFIG.enabled:
        return []
    items = []
    for raw in load_active_proxies(PROXY_AUTO_CONFIG):
        proxy = _normalize_proxy_line(raw)
        if proxy:
            items.append(proxy)
    return items

def _proxy_pool_paths():
    if not PROXY_POOL_FILE:
        return []
    configured = Path(PROXY_POOL_FILE).expanduser()
    paths = [configured]
    if _PROXY_POOL_FILE_ENV is None and configured.name == "代理.txt":
        paths.append(Path("proxy.txt"))
    return paths


def _split_proxy_pool_text(text: str) -> list[str]:
    """Split env/file proxy text into raw lines.

    Supports mixed separators so HF Secrets can hold many formats:
      - real newlines
      - escaped \\n / \\r\\n (common when pasting into one-line secrets)
      - commas, semicolons, pipes
    """
    if not text:
        return []
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    # literal backslash-n from single-line env paste
    raw = raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    # also split common list separators (keep share-link query strings intact:
    # only split on separators that are outside typical URL usage at line level)
    chunks: list[str] = []
    for part in re.split(r"[\n,;|]+", raw):
        line = (part or "").strip()
        if line:
            chunks.append(line)
    return chunks


def _load_env_proxy_list_items() -> tuple[list[str], bool]:
    """Parse PROXY_POOL / PROXY_POOL_LIST / PROXIES env into normalized proxies."""
    text = _env_proxy_pool_text()
    if not text:
        return [], False
    items: list[str] = []
    relay_pending = False
    for raw in _split_proxy_pool_text(text):
        is_share_link = bool(_share_link_scheme(raw))
        proxy = _normalize_proxy_line(raw)
        if proxy:
            items.append(proxy)
        elif is_share_link and PROXY_RELAY_ENABLED:
            relay_pending = True
    return items, relay_pending


def _load_file_proxy_items(path: Path) -> tuple[list[str], bool]:
    items: list[str] = []
    relay_pending = False
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            is_share_link = bool(_share_link_scheme(raw))
            proxy = _normalize_proxy_line(raw)
            if proxy:
                items.append(proxy)
            elif is_share_link and PROXY_RELAY_ENABLED:
                relay_pending = True
    except OSError:
        return [], False
    return items, relay_pending


def _load_proxy_pool_locked():
    auto_mtime_ns = _auto_proxy_mtime_ns()
    tested_only = _use_tested_proxy_pool_only()
    env_text = _env_proxy_pool_text()
    env_items, env_relay = _load_env_proxy_list_items()
    env_sig = hash(env_text) if env_text else 0

    path = None
    stat = None
    for candidate in _proxy_pool_paths():
        try:
            stat = candidate.stat()
        except OSError:
            continue
        path = candidate
        break

    now = time.time()
    cache_path = f"env:{env_sig}|file:{path}" if path else f"env:{env_sig}"
    if (
        _proxy_pool_cache.get("path") == cache_path
        and _proxy_pool_cache.get("mtime_ns") == (stat.st_mtime_ns if stat else None)
        and _proxy_pool_cache.get("auto_mtime_ns") == auto_mtime_ns
        and _proxy_pool_cache.get("env_sig") == env_sig
    ):
        retry_at = _proxy_pool_cache.get("retry_at")
        if not _proxy_pool_cache.get("relay_pending") or (retry_at and retry_at > now):
            return _proxy_pool_cache.get("items", ())

    file_items: list[str] = []
    file_relay = False
    if path is not None:
        file_items, file_relay = _load_file_proxy_items(path)

    auto_items = _load_auto_proxy_items()
    relay_pending = env_relay or file_relay
    if tested_only:
        items = _unique_proxies(auto_items)
    else:
        # priority: env list → file → auto-tested
        items = _unique_proxies(list(env_items) + list(file_items) + list(auto_items))

    _proxy_pool_cache.update(
        {
            "path": cache_path,
            "mtime_ns": stat.st_mtime_ns if stat else None,
            "auto_mtime_ns": auto_mtime_ns,
            "env_sig": env_sig,
            "items": tuple(items),
            "index": 0,
            "relay_pending": relay_pending,
            "retry_at": (now + max(1, PROXY_RELAY_RETRY_SEC)) if relay_pending else None,
        }
    )
    return _proxy_pool_cache["items"]

def _use_tested_proxy_pool_only():
    return bool(PROXY_AUTO_CONFIG.enabled and PROXY_POOL_USE_TESTED_ONLY and _proxy_auto_startup_validated)

def _pick_grok_proxy(*, prefer_unblocked=True):
    """Pick one proxy from the Grok/XAI proxy pool; missing file means direct.

    When prefer_unblocked is True and rate-limit scope is per-proxy, skip exits
    that are currently cooling down so concurrent C workers fan out across IPs.
    """
    _ensure_proxy_auto_manager_started()
    with _proxy_pool_lock:
        items = list(_load_proxy_pool_locked())
        if not items:
            return None
        candidates = items
        if prefer_unblocked and REGISTRATION_RATE_LIMIT_SCOPE == "proxy":
            free = [
                proxy
                for proxy in items
                if not REGISTRATION_RATE_LIMIT_CIRCUIT.is_proxy_blocked(proxy)
            ]
            if free:
                candidates = free
        if PROXY_POOL_STRATEGY == "random":
            return random.choice(candidates)
        # round-robin over the full pool index, but only return unblocked candidates
        start = int(_proxy_pool_cache.get("index", 0)) % max(len(items), 1)
        _proxy_pool_cache["index"] = (start + 1) % max(len(items), 1)
        ordered = items[start:] + items[:start]
        for proxy in ordered:
            if proxy in candidates:
                return proxy
        return candidates[0]


def _write_turnstile_solver_proxies(pool_items=None):
    """Write Chromium-safe proxies for vendored Turnstile solver.

    Registration may use authenticated SOCKS5 directly (Playwright supports it),
    but the Turnstile solver's raw Chromium needs local HTTP relays. Build those
    from the manual pool (via sing-box) and emit only 127.0.0.1 HTTP endpoints.
    """
    if pool_items is None:
        with _proxy_pool_lock:
            pool_items = list(_load_proxy_pool_locked())
    lines = []
    sources = list(pool_items or [])
    # Also re-read manual file lines so we can still relay SOCKS auth for solver
    # even when the registration pool keeps socks5h URLs direct.
    try:
        for path in _proxy_pool_paths():
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = (raw or "").strip()
                if not raw or raw.startswith("#"):
                    continue
                if raw not in sources:
                    sources.append(raw)
    except Exception:
        pass

    for proxy in sources:
        try:
            parsed = urlparse(proxy if "://" in proxy else f"socks5h://{proxy}")
        except Exception:
            continue
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}:
            lines.append(proxy)
            continue
        if scheme in {"http", "https"} and not (parsed.username or parsed.password):
            lines.append(proxy)
            continue
        # SOCKS with auth (or share links): create/reuse local HTTP relay for solver
        if scheme in {"socks", "socks5", "socks5h"} and (parsed.username or parsed.password):
            if PROXY_RELAY_ENABLED:
                try:
                    # force through share-link relay path regardless of pool direct mode
                    relayed = _proxy_from_share_link(proxy)
                    if relayed:
                        lines.append(relayed)
                except Exception as exc:
                    debug_log(
                        f"turnstile relay for {host}:{parsed.port} failed: "
                        f"{sanitize_terminal_error(exc)}"
                    )
            continue
        if scheme in _SHARE_LINK_SCHEMES and PROXY_RELAY_ENABLED:
            try:
                relayed = _proxy_from_share_link(proxy)
                if relayed:
                    lines.append(relayed)
            except Exception:
                pass
    # de-dupe preserve order
    seen = set()
    unique = []
    for item in lines:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    out = PROJECT_ROOT / "turnstile-proxies.txt"
    text = ("\n".join(unique) + ("\n" if unique else ""))
    try:
        out.write_text(text, encoding="utf-8")
    except Exception:
        pass
    # Write into active solver work dirs (hybrid + legacy engines for transition)
    for eng in ("hybrid", "d3vin", "theyka"):
        try:
            work = PROJECT_ROOT / "logs" / "turnstile-solver" / eng / "proxies.txt"
            work.parent.mkdir(parents=True, exist_ok=True)
            work.write_text(text, encoding="utf-8")
        except Exception:
            pass
    # Auto-enable solver proxy when we have usable local endpoints
    if unique and not os.environ.get("TURNSTILE_SOLVER_PROXY"):
        os.environ["TURNSTILE_SOLVER_PROXY"] = "1"
    return len(unique)

def _pick_email_proxy():
    """Backward-compatible alias; proxy pool is used for Grok/XAI, not email providers."""
    return _pick_grok_proxy()

def _requests_proxy_kwargs(proxy):
    if not proxy:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}

def _playwright_proxy(proxy):
    if not proxy:
        return None
    parsed = urlparse(proxy)
    scheme = parsed.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks4", "socks5"}:
        return None
    if not parsed.hostname or not parsed.port:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    item = {"server": f"{scheme}://{host}:{parsed.port}"}
    if parsed.username:
        item["username"] = parsed.username
    if parsed.password:
        item["password"] = parsed.password
    return item

async def _new_grok_page(browser, proxy=None, *, always_context=False, browser_fingerprint_id=None):
    proxy = _pick_grok_proxy() if proxy is None else proxy
    playwright_proxy = _playwright_proxy(proxy)
    context_kwargs = browser_context_options(
        browser_fingerprint_id,
        proxy=playwright_proxy,
    )
    if context_kwargs or always_context:
        kwargs = dict(context_kwargs)
        context = await browser.new_context(**kwargs)
        page = await context.new_page()
        _remember_page_proxy(page, proxy)
        return context, page
    page = await browser.new_page()
    _remember_page_proxy(page, proxy)
    return None, page

async def _close_grok_page(context, page):
    if context is not None:
        try:
            await context.close()
            return
        except Exception:
            pass
    if page is not None:
        try:
            await page.close()
        except Exception:
            pass

async def _close_browser_safely(browser):
    if browser is None:
        return
    try:
        await browser.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f'[*] browser close ignored: {sanitize_terminal_error(exc)}')

def _cf_ares_mode_enabled(mode=None):
    mode = CF_ARES_EMAIL_MODE if mode is None else (mode or "")
    return mode in ("1", "true", "yes", "on", "fallback", "always")

def _cf_ares_mode_always(mode=None):
    mode = CF_ARES_EMAIL_MODE if mode is None else (mode or "")
    return mode == "always"

def _cf_ares_xai_mode_enabled():
    return _cf_ares_mode_enabled(CF_ARES_XAI_MODE)

def _cf_ares_xai_mode_always():
    return _cf_ares_mode_always(CF_ARES_XAI_MODE)

def _cf_ares_normalize_source_path(path):
    if path.is_file():
        path = path.parent
    if path.name == "cf_ares":
        path = path.parent
    return path if (path / "cf_ares").is_dir() else path

def _cf_ares_add_import_path():
    raw_paths = [CF_ARES_BUNDLED_PATH]
    if CF_ARES_PATH:
        raw_paths.append(Path(CF_ARES_PATH).expanduser())
    for raw_path in raw_paths:
        candidate = _cf_ares_normalize_source_path(raw_path)
        if not candidate.exists():
            continue
        text = str(candidate)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)

def _cf_ares_client_class():
    _cf_ares_add_import_path()
    try:
        from cf_ares import AresClient
        return AresClient
    except Exception:
        pass
    for name in list(sys.modules):
        if name == "cf_ares" or name.startswith("cf_ares."):
            sys.modules.pop(name, None)
    _cf_ares_add_import_path()
    from cf_ares import AresClient
    return AresClient

def _cf_ares_get_client(proxy=None):
    """Lazy-load CF-Ares only when the optional email transport needs it."""
    global _cf_ares_import_error
    key = proxy or CF_ARES_PROXY or "__direct__"
    if key in _cf_ares_clients:
        return _cf_ares_clients[key]
    if _cf_ares_import_error is not None:
        raise RuntimeError("cf-ares unavailable") from _cf_ares_import_error
    try:
        AresClient = _cf_ares_client_class()
    except Exception as exc:
        _cf_ares_import_error = exc
        raise RuntimeError("cf-ares unavailable") from exc

    kwargs = {
        "browser_engine": CF_ARES_BROWSER_ENGINE or "auto",
        "headless": CF_ARES_HEADLESS,
        "timeout": CF_ARES_TIMEOUT,
        "debug": REGISTER_LOG_MODE == "debug",
    }
    effective_proxy = proxy or CF_ARES_PROXY
    if effective_proxy:
        kwargs["proxy"] = effective_proxy
    if CF_ARES_CHROME_PATH:
        kwargs["chrome_path"] = CF_ARES_CHROME_PATH
    _cf_ares_clients[key] = AresClient(**kwargs)
    return _cf_ares_clients[key]

def _close_cf_ares_client():
    clients = list(_cf_ares_clients.values())
    _cf_ares_clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass

atexit.register(_close_cf_ares_client)

def _looks_like_cloudflare_block(response):
    status = getattr(response, "status_code", 200)
    if status not in (403, 503):
        return False
    text = getattr(response, "text", "") or ""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "cloudflare",
            "cf-browser-verification",
            "cf-im-under-attack",
            "challenge platform",
            "just a moment",
            "turnstile",
            "captcha",
            "error code: 1010",
        )
    )

def _cf_ares_request(method, url, **kwargs):
    with _cf_ares_lock:
        proxy = kwargs.pop("proxy", None)
        timeout = kwargs.pop("timeout", None)
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            client = _cf_ares_get_client(proxy=proxy)
            return getattr(client, method.lower())(url, **kwargs)
        except Exception as exc:
            debug_log(f"CF-Ares client unavailable, falling back to curl_cffi: {sanitize_terminal_error(exc)}")
            return _curl_cffi_request(method, url, proxy=proxy, **kwargs)

def _curl_cffi_request(method, url, **kwargs):
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:
        raise RuntimeError("curl_cffi unavailable") from exc

    request_kwargs = dict(kwargs)
    proxy = request_kwargs.pop("proxy", None)
    if proxy and "proxy" not in request_kwargs and "proxies" not in request_kwargs:
        request_kwargs["proxy"] = proxy
    if CF_ARES_IMPERSONATE and "impersonate" not in request_kwargs:
        request_kwargs["impersonate"] = CF_ARES_IMPERSONATE
    return curl_requests.request(method.upper(), url, **request_kwargs)

def _response_header(response, name, default=""):
    headers = getattr(response, "headers", None)
    if not headers:
        return default
    try:
        return headers.get(name, headers.get(name.lower(), default))
    except Exception:
        lowered = name.lower()
        for key, value in dict(headers).items():
            if str(key).lower() == lowered:
                return value
    return default

def _response_text(response):
    try:
        return response.text
    except Exception:
        try:
            return response.content.decode("utf-8", errors="replace")
        except Exception:
            return ""

def _email_http_request(method, url, **kwargs):
    """External email-provider HTTP with optional CF-Ares fallback."""
    proxy = kwargs.pop("proxy", None)
    request_kwargs = dict(kwargs)
    if proxy and "proxies" not in request_kwargs:
        request_kwargs.update(_requests_proxy_kwargs(proxy))
    cf_kwargs = dict(kwargs)
    if proxy:
        cf_kwargs["proxy"] = proxy

    if not _cf_ares_mode_enabled():
        return getattr(req, method.lower())(url, **request_kwargs)

    if _cf_ares_mode_always():
        return _cf_ares_request(method, url, **cf_kwargs)

    try:
        response = getattr(req, method.lower())(url, **request_kwargs)
    except Exception as exc:
        try:
            debug_log("[P] email HTTP retry via CF-Ares after request error")
            return _cf_ares_request(method, url, **cf_kwargs)
        except Exception:
            raise exc
    if _looks_like_cloudflare_block(response):
        try:
            debug_log("[P] email HTTP retry via CF-Ares after Cloudflare block")
            return _cf_ares_request(method, url, **cf_kwargs)
        except Exception:
            return response
    return response

def _email_get(url, **kwargs):
    return _email_http_request("GET", url, **kwargs)

def _email_post(url, **kwargs):
    return _email_http_request("POST", url, **kwargs)

def _extract_code(text):
    """多层兜底提取验证码,抗邮件模板变化。"""
    for pat in (r'>([A-Z0-9]{3}-[A-Z0-9]{3})<', r'>([A-Z0-9]{6})<', r'\b([A-Z0-9]{3}-?[A-Z0-9]{3})\b'):
        m = re.search(pat, text)
        if m:
            return m.group(1).replace('-', '')
    return None

def _mailtm_create(base, password):
    """mail.tm 同协议建箱;返回 (handle, email)。"""
    d = _email_get(f'{base}/domains', timeout=12).json()
    d = d.get('hydra:member', d) if isinstance(d, dict) else d
    doms = [x['domain'] for x in d if x.get('isActive', True) and not x.get('isPrivate', False)]
    if not doms:
        raise RuntimeError('no domain')
    email = f'oc{secrets.token_hex(5)}@{doms[0]}'
    _email_post(f'{base}/accounts', json={'address': email, 'password': password}, timeout=12)
    tok = _email_post(f'{base}/token', json={'address': email, 'password': password}, timeout=12).json().get('token', '')
    if not tok:
        raise RuntimeError('no token')
    return f'mt|{base}|{tok}', email

def _lol_create():
    """tempmail.lol 建箱;返回 (handle, email)。"""
    r = _email_post('https://api.tempmail.lol/v2/inbox/create', timeout=12).json()
    addr, tok = r.get('address', ''), r.get('token', '')
    if not addr or not tok:
        raise RuntimeError('lol create failed')
    return f'lol|{tok}', addr

def _moemail_api_root(api=None):
    """规范化 MoeMail API 根地址;用户粘贴 /moe 等页面路径时回退到站点根。"""
    raw = (api or MOEMAIL_API or "https://moemail.app").strip().rstrip("/")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("invalid moemail api")
    path = parsed.path.rstrip("/")
    if path and path != "/api" and not path.endswith("/api"):
        path = ""
    parsed = parsed._replace(path=path, params="", query="", fragment="")
    return urlunparse(parsed).rstrip("/")

def _moemail_url(path, api=None):
    root = _moemail_api_root(api)
    if root.endswith("/api") and path.startswith("/api/"):
        return root + path[len("/api"):]
    return root + path

def _moemail_headers(json_body=False):
    if not MOEMAIL_API_KEY:
        raise RuntimeError("moemail api key missing")
    headers = {"Accept": "application/json", "X-API-Key": MOEMAIL_API_KEY}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers

def _json_or_error(response, context):
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise RuntimeError(f"{context} http {status}")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"{context} invalid json") from exc

def _moemail_domains():
    if MOEMAIL_DOMAIN:
        return [MOEMAIL_DOMAIN]
    data = _json_or_error(
        _email_get(_moemail_url("/api/config"), headers=_moemail_headers(), timeout=12),
        "moemail config",
    )
    domains = [
        item.strip()
        for item in str(data.get("emailDomains") or "").split(",")
        if item.strip()
    ]
    if not domains:
        raise RuntimeError("moemail no domain")
    return domains

def _moemail_create(password=None):
    """MoeMail OpenAPI 建箱;返回 (handle, email)。"""
    domain = _moemail_domains()[0]
    payload = {
        "name": f"oc{secrets.token_hex(5)}",
        "expiryTime": MOEMAIL_EXPIRY_MS,
        "domain": domain,
    }
    data = _json_or_error(
        _email_post(
            _moemail_url("/api/emails/generate"),
            headers=_moemail_headers(json_body=True),
            json=payload,
            timeout=12,
        ),
        "moemail create",
    )
    email_id = data.get("id")
    address = data.get("email") or data.get("address")
    if not email_id or not address:
        raise RuntimeError("moemail create failed")
    return f"moe|{email_id}", address

def create_email():
    """custom 用自建域名(本地 webhook);tempmail 随机打散多个 provider,逐个 fallback。"""
    if EMAIL_MODE == 'custom':
        email = f'oc{secrets.token_hex(5)}@{EMAIL_DOMAIN}'
        password = rand_str()
        return email, email, password  # 地址即用,验证码经 CF Worker POST 到本地 webhook

    password = rand_str()
    if EMAIL_MODE == 'moemail':
        handle, email = _moemail_create(password)
        return handle, email, password

    # 优先用已跑通的 mail.tm,其余按序仅作 fallback
    makers = []
    if MOEMAIL_API_KEY:
        makers.append(lambda: _moemail_create(password))
    makers.extend((lambda b=b: _mailtm_create(b, password)) for b in TEMPMAIL_BASES)
    makers.append(_lol_create)
    for make in makers:
        try:
            handle, email = make()
            return handle, email, password
        except Exception:
            continue
    raise RuntimeError('所有临时邮箱 provider 均不可用')

def _moemail_fetch(handle):
    """读取 MoeMail 邮箱当前邮件全文(subject+content+html);无则 None。"""
    _, email_id = handle.split("|", 1)
    data = _json_or_error(
        _email_get(
            _moemail_url(f"/api/emails/{email_id}"),
            headers=_moemail_headers(),
            timeout=10,
        ),
        "moemail messages",
    )
    items = data.get("messages") or []
    if not items:
        return None

    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        message = item
        if not any(message.get(k) for k in ("content", "html")) and item.get("id"):
            try:
                detail = _json_or_error(
                    _email_get(
                        _moemail_url(f"/api/emails/{email_id}/{item['id']}"),
                        headers=_moemail_headers(),
                        timeout=10,
                    ),
                    "moemail message",
                )
                message = detail.get("message") or detail
            except Exception:
                message = item
        parts.append(
            "\n".join(
                str(message.get(k, ""))
                for k in ("subject", "content", "html")
            )
        )
    return "\n".join(parts) if parts else None

def _tempmail_fetch(handle):
    """按 handle 前缀分派,取该邮箱当前邮件全文(subject+text+html);无则 None。"""
    kind = handle.split('|', 1)[0]
    if kind == 'lol':
        tok = handle.split('|', 1)[1]
        data = _email_get(f'https://api.tempmail.lol/v2/inbox?token={tok}', timeout=10).json()
        items = data.get('emails') or data.get('messages') or []
        if not items:
            return None
        return '\n'.join(f"{i.get('subject','')}\n{i.get('body','')}\n{i.get('html','')}"
                         for i in items if isinstance(i, dict))
    if kind == 'moe':
        return _moemail_fetch(handle)
    # mail.tm 同协议:handle = "mt|base|token"
    if kind != 'mt':
        raise RuntimeError('unknown tempmail provider')
    _, base, tok = handle.split('|', 2)
    hdr = {'Accept': 'application/json', 'Authorization': f'Bearer {tok}'}
    data = _email_get(f'{base}/messages', headers=hdr, timeout=10).json()
    msgs = data if isinstance(data, list) else data.get('hydra:member', [])
    if not msgs:
        return None
    mid = str(msgs[0].get('id') or '')
    detail = _email_get(f'{base}/messages/{mid}', headers=hdr, timeout=10).json()
    parts = [str(detail.get(k, '')) for k in ['subject', 'intro', 'text', 'html']]
    if isinstance(detail.get('html'), list):
        parts.append('\n'.join(str(x) for x in detail['html']))
    return '\n'.join(parts)

def poll_code_once(handle):
    """检查一次验证码:custom 查本地 webhook /check;tempmail 按 provider 取信。"""
    if EMAIL_MODE == 'custom':
        try:
            resp = req.get(f'{LOCAL_EMAIL_API}/check/{handle}', timeout=5)
            if resp.status_code == 200 and resp.json().get('code'):
                return resp.json()['code']
        except Exception:
            pass
        return None

    try:
        text = _tempmail_fetch(handle)
        if text:
            return _extract_code(text)
    except Exception:
        pass
    return None

def poll_code(handle, max_wait=90):
    """轮询验证码:custom 查本地 webhook /check;tempmail 按 provider 取信。"""
    for _ in range(max_wait):
        time.sleep(1)
        code = poll_code_once(handle)
        if code:
            return code
    return None


async def _create_email_async(loop):
    """在线程池中创建邮箱,避免阻塞 asyncio 事件循环。"""
    return await loop.run_in_executor(POLL_EXECUTOR, create_email)


async def _poll_code_async(loop, handle):
    """在线程池中轮询验证码。"""
    return await loop.run_in_executor(POLL_EXECUTOR, poll_code, handle)


async def _poll_code_once_async(loop, handle):
    """在线程池中执行一次轻量验证码检查。"""
    return await loop.run_in_executor(POLL_EXECUTOR, poll_code_once, handle)


async def _acquire_many(sem, count):
    """一次预留多个许可；取消或异常时回滚已获取许可。"""
    acquired = 0
    try:
        for _ in range(count):
            await sem.acquire()
            acquired += 1
        return acquired
    except BaseException:
        for _ in range(acquired):
            sem.release()
        raise


class _NoopAsyncSemaphore:
    async def acquire(self):
        return True

    def release(self):
        return None


async def _send_q_request_batch(browser, physical_sem, p_send_sem, requests, metrics=None):
    """使用每个账号自己的浏览器指纹页面发送一批 Q 请求。

    返回每个请求的 sent 状态。等待 Q 返回不在此函数内发生,因此这里释放
    Physical_Sem 后不会占用本地重资源。
    """
    p_send_acquired = False
    physical_acquired = False
    physical_wait_started = None
    physical_hold_started = None
    context = None
    page = None
    await p_send_sem.acquire()
    p_send_acquired = True
    try:
        physical_wait_started = time.time()
        await physical_sem.acquire()
        physical_acquired = True
        physical_hold_started = time.time()
        if metrics is not None:
            metrics.p_physical_count += 1
            metrics.p_physical_wait_seconds += physical_hold_started - physical_wait_started
        results = []
        send_started = time.time()
        for item in requests:
            context = None
            page = None
            sent = False
            try:
                browser_fingerprint_id = item.get("browser_fingerprint_id")
                context, page = await _new_grok_page(
                    browser,
                    browser_fingerprint_id=browser_fingerprint_id,
                )
                if not browser_fingerprint_id:
                    await page.set_viewport_size({"width": 800, "height": 600})
                stage_started = time.time()
                await _prepare_signup_page(page, redirect=True, timeout=30000)
                if metrics is not None:
                    metrics.p_page_prepare_count += 1
                    metrics.p_page_prepare_seconds += time.time() - stage_started
                sent = await grpc_create_code(page, item["email"])
            except asyncio.CancelledError:
                raise
            except Exception:
                sent = False
            finally:
                await _close_grok_page(context, page)
            results.append({**item, "sent": sent})
        if metrics is not None:
            metrics.p_send_count += 1
            metrics.p_send_seconds += time.time() - send_started
        return results
    finally:
        if physical_acquired:
            if metrics is not None and physical_hold_started is not None:
                metrics.p_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()
        if p_send_acquired:
            p_send_sem.release()


async def _resend_q_request(browser, physical_sem, p_send_sem, request, metrics=None):
    """重新发送单个邮箱验证码请求；只走 Grok/xAI 页面代理链路。"""
    if browser is None or physical_sem is None:
        return False
    p_send_sem = p_send_sem or _NoopAsyncSemaphore()
    results = await _send_q_request_batch(
        browser,
        physical_sem,
        p_send_sem,
        [request],
        metrics,
    )
    if metrics is not None:
        metrics.q_send_batches += 1
        metrics.q_send_batch_items += len(results)
    return bool(results and results[0].get("sent"))


async def _poll_code_with_resends(
    loop,
    request,
    *,
    browser,
    physical_sem,
    p_send_sem,
    metrics,
):
    """等待验证码；超时窗口内未收到时按配置重新触发 xAI 发码。"""
    deadline = time.monotonic() + P_REQUEST_TIMEOUT
    resend_attempts = 0
    next_resend_at = time.monotonic() + EMAIL_CODE_RESEND_AFTER_SEC

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        try:
            code = await asyncio.wait_for(
                _poll_code_once_async(loop, request["handle"]),
                timeout=max(1.0, min(5.0, remaining)),
            )
        except asyncio.TimeoutError:
            code = None
        if code:
            return code

        now = time.monotonic()
        if (
            resend_attempts < EMAIL_CODE_RESEND_ATTEMPTS
            and now >= next_resend_at
        ):
            resend_attempts += 1
            sent = False
            try:
                sent = await _resend_q_request(
                    browser,
                    physical_sem,
                    p_send_sem,
                    request,
                    metrics,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_log(f"[P] resend email code err: {sanitize_terminal_error(exc)}")
            if sent:
                if metrics is not None:
                    metrics.q_sent += 1
                debug_log(f"[P] resent verification code attempt={resend_attempts}")
            next_resend_at = time.monotonic() + EMAIL_CODE_RESEND_AFTER_SEC
            continue

        if resend_attempts < EMAIL_CODE_RESEND_ATTEMPTS:
            sleep_for = min(1.0, max(0.0, next_resend_at - now), max(0.0, remaining))
        else:
            sleep_for = min(1.0, max(0.0, remaining))
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


async def _poll_and_admit_q(
    request,
    inventory,
    q_pending_sem,
    q_slot_sem,
    metrics,
    *,
    q_batch_lease=None,
    admission_gate=None,
    browser=None,
    physical_sem=None,
    p_send_sem=None,
):
    """等待单个 Q 返回并入库；每个请求独立释放 pending/inflight。"""
    loop = asyncio.get_event_loop()
    release_reservation = True
    try:
        try:
            if browser is not None and EMAIL_CODE_RESEND_ATTEMPTS > 0:
                code = await _poll_code_with_resends(
                    loop,
                    request,
                    browser=browser,
                    physical_sem=physical_sem,
                    p_send_sem=p_send_sem,
                    metrics=metrics,
                )
            else:
                code = await asyncio.wait_for(
                    _poll_code_async(loop, request["handle"]),
                    timeout=P_REQUEST_TIMEOUT,
                )
        except asyncio.CancelledError:
            # poll_code 仍在运行时取消，底层轮询可能继续持有该请求；此处
            # 不能提前归还 pending/inflight，否则新请求会超量进入。
            release_reservation = False
            raise
        except asyncio.TimeoutError:
            code = None

        if code is None:
            metrics.q_discarded += 1
            return False

        metrics.q_returned += 1
        returned_at = time.time()
        q_env = None
        try:
            q_env = await ResourceEnvelope.create_with_slot(
                'Q',
                {
                    'email': request["email"],
                    'password': request["password"],
                    'code': code,
                    'browser_fingerprint_id': request.get("browser_fingerprint_id"),
                },
                q_slot_sem,
                expires_at=returned_at + Q_MAX_AGE,
            )
            await inventory.put_q(q_env)
            debug_log('[P] verification code admitted')
            return True
        except asyncio.CancelledError:
            if q_env is not None and not q_env.released:
                q_env.discard()
            raise
        except Exception:
            if q_env is not None and not q_env.released:
                q_env.discard()
            metrics.q_discarded += 1
            return False
    finally:
        if release_reservation:
            # poll 已终止（含超时/网络/解析异常），或 Q 已返回后任务在
            # 入库阶段取消：请求所有权已经回到本协程，必须归还许可。
            q_pending_sem.release()
            if q_batch_lease is not None:
                await q_batch_lease.release_one()
            if admission_gate is not None:
                await admission_gate.notify_changed()


def _observe_background_task(task):
    """Consume detached task failures so background settlement is not silent."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        debug_log(f'[P] background settle err: {sanitize_terminal_error(e)}')


# ──────────────────────────────────────────────
#  CSP Worker
# ──────────────────────────────────────────────

class _CHotPageLease:
    def __init__(self, browser, metrics=None, browser_fingerprint_id=None):
        self.browser = browser
        self.metrics = metrics
        self.browser_fingerprint_id = browser_fingerprint_id
        self.context = None
        self.page = None

    async def __aenter__(self):
        started = time.time()
        try:
            self.context, self.page = await _acquire_c_page(
                self.browser,
                self.metrics,
                browser_fingerprint_id=self.browser_fingerprint_id,
            )
            return self.page
        finally:
            if self.metrics is not None:
                self.metrics.c_page_acquire_count += 1
                self.metrics.c_page_acquire_seconds += time.time() - started

    async def __aexit__(self, exc_type, exc, tb):
        await _release_c_page(
            self.context,
            self.page,
            healthy=exc_type is None,
            browser_fingerprint_id=self.browser_fingerprint_id,
        )
        self.context = None
        self.page = None
        return False


_c_hot_page_pool = []
_c_hot_page_lock = asyncio.Lock()
_c_hot_page_pool_size = derive_c_hot_page_pool_size(PHYSICAL_CAP or 1, C_WORKERS or 1)


async def _new_c_hot_page(browser, browser_fingerprint_id=None):
    context, page = await _new_grok_page(
        browser,
        always_context=True,
        browser_fingerprint_id=browser_fingerprint_id,
    )
    if not browser_fingerprint_id:
        await page.set_viewport_size({"width": 800, "height": 600})
    await _prepare_signup_page(page, redirect=True, timeout=30000)
    return context, page


async def _acquire_c_page(browser, metrics=None, browser_fingerprint_id=None):
    if browser_fingerprint_id:
        if metrics is not None:
            metrics.c_hot_page_misses += 1
        return await _new_c_hot_page(
            browser,
            browser_fingerprint_id=browser_fingerprint_id,
        )

    if C_HOT_PAGE_POOL:
        async with _c_hot_page_lock:
            if _c_hot_page_pool:
                if metrics is not None:
                    metrics.c_hot_page_hits += 1
                return _c_hot_page_pool.pop()
        if metrics is not None:
            metrics.c_hot_page_misses += 1
        return await _new_c_hot_page(browser)

    context, page = await _new_grok_page(browser)
    await page.set_viewport_size({"width": 800, "height": 600})
    await _prepare_signup_page(page, redirect=True, timeout=30000)
    return context, page


async def _release_c_page(context, page, *, healthy, browser_fingerprint_id=None):
    if browser_fingerprint_id:
        await _close_grok_page(context, page)
        return

    if not C_HOT_PAGE_POOL:
        await _close_grok_page(context, page)
        return

    if context is None:
        await _close_grok_page(context, page)
        return

    if healthy:
        try:
            if "/sign-up" not in (getattr(page, "url", "") or ""):
                healthy = False
            else:
                await context.clear_cookies()
                await page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                )
                async with _c_hot_page_lock:
                    if len(_c_hot_page_pool) < _c_hot_page_pool_size:
                        _c_hot_page_pool.append((context, page))
                        return
        except asyncio.CancelledError:
            try:
                await context.close()
            except Exception:
                pass
            raise
        except Exception:
            pass

    try:
        await context.close()
    except Exception:
        try:
            await page.close()
        except Exception:
            pass


def _c_page_lease(browser, metrics=None, browser_fingerprint_id=None):
    return _CHotPageLease(browser, metrics, browser_fingerprint_id)


async def _close_c_hot_page_pool():
    async with _c_hot_page_lock:
        items = list(_c_hot_page_pool)
        _c_hot_page_pool.clear()
    for context, page in items:
        try:
            await context.close()
        except Exception:
            try:
                await page.close()
            except Exception:
                pass


async def s_worker(wid, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate=None):
    """S_Worker: 生成 T 并入库。"""
    use_api_solver = is_api_backend(TURNSTILE_SOLVER)
    while not STOP.is_set():
        t_lease = None
        try:
            if admission_gate is not None:
                t_lease = await admission_gate.acquire_t_production()

            # 外部 API solver 不占用本机浏览器物理并发槽
            physical_held = False
            physical_wait_started = time.time()
            physical_hold_started = physical_wait_started
            if not use_api_solver:
                await physical_sem.acquire()
                physical_held = True
                physical_hold_started = time.time()
                metrics.s_physical_count += 1
                metrics.s_physical_wait_seconds += physical_hold_started - physical_wait_started
            token = None
            trace = {}
            solve_started = time.time()
            hard_timeout = TURNSTILE_API_TIMEOUT if use_api_solver else SOLVER_HARD_TIMEOUT
            try:
                try:
                    token, trace = await asyncio.wait_for(
                        solve_one_turnstile_with_trace(browser),
                        timeout=hard_timeout,
                    )
                except asyncio.TimeoutError:
                    debug_log(f'[S] {wid} solver timeout after {hard_timeout}s')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    debug_log(f'[S] {wid} solver error: {sanitize_terminal_error(exc)}')
            finally:
                solve_elapsed = time.time() - solve_started
                _record_solver_trace(metrics, trace, solve_elapsed, token)
                if physical_held:
                    metrics.s_physical_hold_seconds += time.time() - physical_hold_started
                    physical_sem.release()

            if token is None:
                metrics.t_discarded += 1
                if t_lease is not None:
                    await t_lease.release()
                    t_lease = None
                await asyncio.sleep(0.5)
                continue

            metrics.t_produced += 1
            now = time.time()
            t_env = None
            try:
                t_env = await ResourceEnvelope.create_with_slot(
                    'T', token, t_slot_sem, expires_at=now + T_MAX_AGE
                )
                await inventory.put_t(t_env)
                if admission_gate is not None:
                    await admission_gate.notify_changed()
            except asyncio.CancelledError:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
                raise
            except Exception:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A single browser/page failure must not terminate the permanent
            # producer.  The next iteration acquires a fresh solver page.
            metrics.t_discarded += 1
            debug_log(f'[S] {wid} worker error: {sanitize_terminal_error(exc)}')
        finally:
            if t_lease is not None:
                await t_lease.release()
        await asyncio.sleep(0.2)


async def p_worker(
    wid,
    browser,
    inventory,
    physical_sem,
    q_pending_sem,
    q_slot_sem,
    metrics,
    admission_gate=None,
    p_send_sem=None,
    max_batch=1,
):
    """P_Worker: 创建邮箱 + 发码 + 轮询 + 入库。"""
    loop = asyncio.get_event_loop()
    p_send_sem = p_send_sem or _NoopAsyncSemaphore()
    while not STOP.is_set():
        q_lease = None
        pending_owned = 0
        settle_tasks = []
        try:
            if admission_gate is not None:
                q_lease = await admission_gate.acquire_q_batch(max_batch=max_batch)
                batch_count = q_lease.count
            else:
                batch_count = 1

            pending_owned = await _acquire_many(q_pending_sem, batch_count)

            requests = []
            for _ in range(batch_count):
                email_started = time.time()
                try:
                    handle, email, password = await _create_email_async(loop)
                    browser_fingerprint_id = await asyncio.to_thread(
                        _remember_account_browser_fingerprint,
                        email,
                    )
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    requests.append(
                        {
                            "handle": handle,
                            "email": email,
                            "password": password,
                            "browser_fingerprint_id": browser_fingerprint_id,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    debug_log(f'[P] {wid} create email err: {sanitize_terminal_error(e)}')
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()

            if not requests:
                continue

            results = await _send_q_request_batch(
                browser, physical_sem, p_send_sem, requests, metrics
            )
            metrics.q_send_batches += 1
            metrics.q_send_batch_items += len(results)

            for item in results:
                if not item["sent"]:
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()
                    continue

                metrics.q_sent += 1
                pending_owned -= 1
                task = asyncio.create_task(
                    _poll_and_admit_q(
                        item,
                        inventory,
                        q_pending_sem,
                        q_slot_sem,
                        metrics,
                        q_batch_lease=q_lease,
                        admission_gate=admission_gate,
                        browser=browser,
                        physical_sem=physical_sem,
                        p_send_sem=p_send_sem,
                    )
                )
                task.add_done_callback(_observe_background_task)
                settle_tasks.append(task)

            if settle_tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in settle_tasks))

        except asyncio.CancelledError:
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
            raise
        except Exception as e:
            debug_log(f'[P] {wid} err: {sanitize_terminal_error(e)}')
            metrics.q_discarded += 1
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
        await asyncio.sleep(0.2)


def _pair_is_expired(pair, now=None):
    """检查已 claim 的 T/Q 是否在等待期间失效。"""
    now = time.time() if now is None else now
    return any(
        bool(check(now))
        for envelope in (pair.t, pair.q)
        if (check := getattr(envelope, "is_expired", None)) is not None
    )


def _append_registration_line(path, line, mode=None, *, durable=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        stream.write(line)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    if mode is not None:
        os.chmod(path, mode)


_key_export_file_lock = threading.Lock()
_key_export_tasks = set()


def _key_export_path(*parts):
    return Path(KEY_EXPORT_DIR).joinpath(*parts)


def _browser_fingerprint_path():
    return _key_export_path(BROWSER_FINGERPRINT_FILENAME)


def _remember_account_browser_fingerprint(email, browser_fingerprint_id=None):
    return get_or_create_browser_fingerprint(
        _browser_fingerprint_path(),
        email,
        browser_fingerprint_id,
    )


def _key_export_oauth_formats():
    if not KEY_EXPORT_ENROLLER:
        return ()
    return tuple(fmt for fmt in KEY_EXPORT_FORMATS if fmt in {"cpa", "sub2api"})


def _atomic_write_private_json(path, document):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _load_or_create_key_export_salt():
    configured = os.environ.get("XAI_ENROLLER_SOURCE_SALT")
    if configured:
        return configured.encode()
    path = _key_export_path(".xai-enroller-salt")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value.encode()
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".xai-enroller-salt.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return value.encode()


def _sub2api_account_from_credential(email, credential):
    from xai_enroller.protocol import XAIProfile

    profile = XAIProfile.default()
    credentials = {
        "access_token": credential.access_token,
        "refresh_token": credential.refresh_token,
        "expires_at": credential.expires_at,
        "client_id": profile.client_id,
        "scope": profile.scope,
        "email": email,
        "base_url": "https://api.x.ai/v1",
    }
    if credential.id_token:
        credentials["id_token"] = credential.id_token
    if credential.token_type:
        credentials["token_type"] = credential.token_type
    return {
        "name": email or credential.subject or "grok-account",
        "platform": "grok",
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": credentials,
        "extra": {
            "email": email,
            "subject": credential.subject,
            "last_refresh": credential.last_refresh,
        },
    }


def _sub2api_document(accounts):
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": list(accounts),
    }


def _store_sub2api_credential_sync(email, credential, name_secret):
    from xai_enroller.sinks import credential_filename

    directory = _key_export_path("sub2api")
    filename = credential_filename(credential, name_secret).removesuffix(".json")
    account = _sub2api_account_from_credential(email, credential)
    with _key_export_file_lock:
        account_path = directory / f"{filename}.sub2api.json"
        _atomic_write_private_json(account_path, _sub2api_document([account]))

        accounts = []
        seen = set()
        for path in sorted(directory.glob("*.sub2api.json")):
            if path.name == "accounts.sub2api.json":
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for item in document.get("accounts", []):
                if not isinstance(item, dict):
                    continue
                credentials = item.get("credentials") or {}
                key = (
                    item.get("platform"),
                    credentials.get("refresh_token"),
                    credentials.get("access_token"),
                    item.get("name"),
                )
                if key in seen:
                    continue
                seen.add(key)
                accounts.append(item)
        _atomic_write_private_json(
            directory / "accounts.sub2api.json",
            _sub2api_document(accounts),
        )
    return filename


class _LocalKeyExportSink:
    def __init__(self, email, formats, name_secret):
        self.email = email
        self.formats = set(formats)
        self.name_secret = name_secret

    async def store(self, credential):
        from xai_enroller.models import SinkReceipt
        from xai_enroller.sinks import LocalAuthFileSink

        fingerprints = []
        if "cpa" in self.formats:
            receipt = await LocalAuthFileSink(
                _key_export_path("cpa"),
                name_secret=self.name_secret,
                email=self.email,
            ).store(credential)
            fingerprints.append(receipt.fingerprint)
        if "sub2api" in self.formats:
            fingerprint = await asyncio.to_thread(
                _store_sub2api_credential_sync,
                self.email,
                credential,
                self.name_secret,
            )
            fingerprints.append(fingerprint)
        return SinkReceipt(",".join(fingerprints) or "no-output")


class _SingleRegistrationSource:
    def __init__(self, record):
        self.record = record

    def records(self):
        yield self.record


async def _run_key_export_enrollment(email, sso, session_cookies, browser_fingerprint_id=None):
    formats = _key_export_oauth_formats()
    if not formats:
        return None

    import httpx
    from xai_enroller.coordinator import EnrollmentCoordinator
    from xai_enroller.executors import PlaywrightExecutor
    from xai_enroller.models import SourceRecord
    from xai_enroller.protocol import XAIProfile, XAIProtocol

    name_secret = _load_or_create_key_export_salt()
    browser_fingerprint_id = _remember_account_browser_fingerprint(
        email,
        browser_fingerprint_id,
    )
    record = SourceRecord(
        email,
        sso,
        tuple(session_cookies or ()),
        browser_fingerprint_id,
    )
    proxy = _pick_grok_proxy()
    client_kwargs = {}
    if proxy and urlparse(proxy).scheme.lower() in {"http", "https"}:
        client_kwargs["proxy"] = proxy
    client = httpx.AsyncClient(**client_kwargs)
    try:
        coordinator = EnrollmentCoordinator(
            source=_SingleRegistrationSource(record),
            protocol=XAIProtocol(
                client,
                XAIProfile.default(),
                default_poll_interval=KEY_EXPORT_ENROLLER_POLL_SEC,
            ),
            executor=PlaywrightExecutor(
                concurrency=1,
                executable_path=find_chrome(),
                proxy=_playwright_proxy(proxy),
            ),
            sink=_LocalKeyExportSink(email, formats, name_secret),
            ledger_path=_key_export_path("xai-enroller-ledger.db"),
            ledger_salt=name_secret,
            concurrency=1,
            timeout=KEY_EXPORT_ENROLLER_TIMEOUT,
            retry_attempts=KEY_EXPORT_ENROLLER_RETRY_ATTEMPTS,
        )
        results = await coordinator.run(target=1)
        result = results[0] if results else None
        if result is not None and result.status.value == "imported":
            debug_log(f"[keys] exported OAuth credential formats={','.join(formats)}")
        elif result is not None:
            debug_log(f"[keys] OAuth export skipped status={result.status.value} reason={result.reason_code}")
        return result
    finally:
        await client.aclose()


def _schedule_key_export_enrollment(email, sso, session_cookies, browser_fingerprint_id=None):
    if not _key_export_oauth_formats():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(
        _run_key_export_enrollment(
            email,
            sso,
            session_cookies,
            browser_fingerprint_id,
        )
    )
    _key_export_tasks.add(task)

    def done(completed):
        _key_export_tasks.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            debug_log(f"[keys] OAuth export failed: {sanitize_terminal_error(exc)}")

    task.add_done_callback(done)
    return task


async def _drain_key_export_tasks(timeout=None):
    tasks = [task for task in list(_key_export_tasks) if not task.done()]
    if not tasks:
        return
    timeout = KEY_EXPORT_ENROLLER_DRAIN_TIMEOUT if timeout is None else timeout
    if timeout <= 0:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _persist_registration(email, password, sso, session_cookies, browser_fingerprint_id=None):
    browser_fingerprint_id = _remember_account_browser_fingerprint(
        email,
        browser_fingerprint_id,
    )
    if session_cookies:
        document = json.dumps(
            {
                "email": email,
                "cookies": session_cookies,
                "browser_fingerprint_id": browser_fingerprint_id,
            },
            separators=(",", ":"),
        )
        _append_registration_line(
            str(_key_export_path("auth-sessions.jsonl")),
            document + "\n",
            mode=0o600,
            durable=True,
        )
    if "legacy" in KEY_EXPORT_FORMATS:
        _append_registration_line(
            str(_key_export_path("accounts.txt")),
            f"{email}:{password}:{sso}\n",
        )
        _append_registration_line(str(_key_export_path("grok.txt")), sso + "\n")
    return browser_fingerprint_id


async def _consume_pair(
    browser,
    physical_sem,
    pair,
    metrics,
    task_id=None,
    recovery_probe=None,
):
    """执行一次 C 消费。返回 True 表示业务成功,False 表示消费失败。"""
    global success_count
    email = pair.q.value['email']
    password = pair.q.value['password']
    code = pair.q.value['code']
    browser_fingerprint_id = pair.q.value.get('browser_fingerprint_id')
    token = pair.t.value

    # c_worker 在单次消费超时开始前完成冷却等待。保留这里的默认路径，
    # 供直接调用者使用，同时避免把 60 秒冷却计入 60 秒消费超时。
    if recovery_probe is None:
        recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()

    # 直接调用路径也可能在 circuit.wait() 中跨过资源有效期。尚未开始
    # 注册时只让出探针，不把本地过期误判成一次恢复探测失败。
    if _pair_is_expired(pair):
        if recovery_probe:
            REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
        return False

    physical_acquired = False
    physical_hold_started = None
    recovery_completed = False
    limited_proxy = None
    try:
        physical_wait_started = time.time()
        await physical_sem.acquire()
        physical_acquired = True
        physical_hold_started = time.time()
        metrics.c_physical_count += 1
        metrics.c_physical_wait_seconds += physical_hold_started - physical_wait_started
        t0 = time.time()
        try:
            async with _c_page_lease(
                browser,
                metrics,
                browser_fingerprint_id=browser_fingerprint_id,
            ) as page:
                limited_proxy = _page_proxy(page)
                sso = None
                session_cookies = []
                verify_started = time.time()
                try:
                    verified = await grpc_verify_code(page, email, code)
                finally:
                    metrics.c_verify_count += 1
                    metrics.c_verify_seconds += time.time() - verify_started
                # 限流可能由另一项并发任务在本任务验证码校验期间触发。
                # 非当前 probe 不得在 circuit 打开后开始新的注册提交；同时
                # 避免使用校验过程中刚过期的 T/Q。
                # per-proxy scope: skip submit only if THIS exit is cooling.
                submit_ok = REGISTRATION_RATE_LIMIT_CIRCUIT.can_submit(recovery_probe)
                if (
                    submit_ok
                    and REGISTRATION_RATE_LIMIT_SCOPE == "proxy"
                    and REGISTRATION_RATE_LIMIT_CIRCUIT.is_proxy_blocked(limited_proxy)
                ):
                    submit_ok = False
                if (
                    verified
                    and not _pair_is_expired(pair)
                    and submit_ok
                ):
                    register_started = time.time()
                    try:
                        registration = await server_action_register(
                            page,
                            email,
                            password,
                            code,
                            token,
                            include_session=True,
                        )
                        if registration:
                            sso, session_cookies = registration
                    finally:
                        metrics.c_register_count += 1
                        metrics.c_register_seconds += time.time() - register_started
        except RegistrationRateLimited:
            if REGISTRATION_RATE_LIMIT_CIRCUIT.trip(limited_proxy):
                wait_sec = REGISTRATION_RATE_LIMIT_CIRCUIT.remaining_seconds(
                    limited_proxy
                )
                log(
                    format_user_registration_event(
                        "rate_limited",
                        wait_seconds=wait_sec,
                        proxy_key=_proxy_rate_limit_key(limited_proxy),
                    )
                )
            return False

        if sso:
            elapsed = time.time() - t0
            async with file_lock:
                browser_fingerprint_id = _persist_registration(
                    email,
                    password,
                    sso,
                    session_cookies,
                    browser_fingerprint_id,
                )
                _schedule_key_export_enrollment(
                    email,
                    sso,
                    session_cookies,
                    browser_fingerprint_id,
                )
                metrics.record_success()
                success_count = metrics.success_count
                count = metrics.success_count
            log(
                format_user_registration_event(
                    "success",
                    task_id=task_id,
                    count=count,
                    rate_per_minute=metrics.runtime_average_success_rate(),
                )
            )
            if recovery_probe:
                recovered_after = REGISTRATION_RATE_LIMIT_CIRCUIT.consume_recovery_seconds(
                    recovery_probe
                )
                recovery_completed = True
                if recovered_after is not None:
                    log(
                        format_user_registration_event(
                            "recovered", wait_seconds=round(recovered_after)
                        )
                    )
            return True

        log(format_user_registration_event("failed", task_id=task_id))
        return False
    finally:
        if recovery_probe and not recovery_completed:
            REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        if physical_acquired:
            metrics.c_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()


async def c_worker(wid, browser, inventory, physical_sem, metrics, admission_gate=None):
    """C_Worker: claim pair 并执行注册。"""
    while not STOP.is_set():
        recovery_probe = False
        task_id = None
        try:
            async with inventory.claim_pair() as pair:
                if admission_gate is not None:
                    await admission_gate.notify_changed()

                # 必须先 claim，再通过冷却闸门。否则多个 worker 会在限流前
                # 通过闸门，随后于冷却期陆续拿到 pair 并漏闸。等待发生在
                # wait_for 之外，因此不计入单次 C 消费超时。
                recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()
                if _pair_is_expired(pair):
                    if recovery_probe:
                        REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
                        recovery_probe = False
                    continue

                task_id = metrics.next_registration_task()
                log(
                    format_user_registration_event(
                        "started",
                        task_id=task_id,
                        remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
                    )
                )
                try:
                    ok = await asyncio.wait_for(
                        _consume_pair(
                            browser,
                            physical_sem,
                            pair,
                            metrics,
                            task_id=task_id,
                            recovery_probe=recovery_probe,
                        ),
                        timeout=C_CONSUME_TIMEOUT,
                    )
                    if ok:
                        metrics.pair_consumed_ok += 1
                    else:
                        metrics.pair_consumed_fail += 1
                except asyncio.TimeoutError:
                    metrics.pair_consumed_fail += 1
                    log(format_user_registration_event("failed", task_id=task_id))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if task_id is not None:
                log(format_user_registration_event("failed", task_id=task_id))
            debug_log(f'[C] {wid} err: {sanitize_terminal_error(e)}')
            metrics.pair_consumed_fail += 1
        finally:
            if recovery_probe:
                # 覆盖等待 pair、获取物理许可或其他边界处的取消/异常。
                # 已成功或已延期的 probe 在这里会是幂等 no-op。
                REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        await asyncio.sleep(0.2)


# ──────────────────────────────────────────────
#  只读监控
# ──────────────────────────────────────────────
async def monitor(inventory, sems, metrics, interval=8, runtime_extra=None):
    """定期输出系统状态,并写入 runtime-status.json 供控制面板读取。"""
    from grok_register.runtime_status import build_register_snapshot, publish as publish_runtime

    last_user_heartbeat = 0.0
    while not STOP.is_set():
        await asyncio.sleep(interval)
        try:
            extra = dict(runtime_extra or {})
            extra.update(
                {
                    "target": TARGET,
                    "email_mode": EMAIL_MODE,
                    "turnstile_solver": TURNSTILE_SOLVER,
                    "turnstile_api_url": TURNSTILE_API_URL if is_api_backend(TURNSTILE_SOLVER) else "",
                    "log_mode": REGISTER_LOG_MODE,
                    "rate_limit_open": bool(REGISTRATION_RATE_LIMIT_CIRCUIT.is_open()),
                    "rate_limit_remaining_sec": float(
                        REGISTRATION_RATE_LIMIT_CIRCUIT.remaining_seconds()
                    ),
                    "rate_limit_scope": REGISTRATION_RATE_LIMIT_SCOPE,
                    "rate_limit_free_proxies": int(
                        REGISTRATION_RATE_LIMIT_CIRCUIT.available_proxy_count()
                    ),
                }
            )
            publish_runtime(
                build_register_snapshot(
                    metrics=metrics,
                    inventory=inventory,
                    sems=sems,
                    extra=extra,
                )
            )
        except Exception:
            pass
        if REGISTER_LOG_MODE == "debug":
            log(metrics.snapshot(inventory, sems))
        elif REGISTER_HEARTBEAT_INTERVAL > 0:
            now = time.monotonic()
            if now - last_user_heartbeat >= REGISTER_HEARTBEAT_INTERVAL:
                last_user_heartbeat = now
                solver_part = (
                    f" token失败:{metrics.t_solve_failed}"
                    if metrics.t_solve_failed
                    else ""
                )
                log(
                    "[*] 运行中 | "
                    f"T:{inventory.t_depth} Q:{inventory.q_depth} "
                    f"发码:{metrics.q_sent} 回码:{metrics.q_returned} "
                    f"开始:{metrics.registration_starts} 成功:{metrics.success_count}"
                    f"{solver_part}"
                )
        if TARGET and metrics.success_count >= TARGET:
            log(f'[*] 已达目标 {TARGET} 个,停止。'); STOP.set()


# ──────────────────────────────────────────────
#  主入口
# ──────────────────────────────────────────────
async def main():
    from grok_register.polyglot import PolyglotError, print_stack_banner, require_polyglot_stack

    try:
        require_polyglot_stack()
    except PolyglotError as exc:
        print(f"[✗] {exc}", file=sys.stderr)
        return 2
    print_stack_banner()
    global TARGET, _c_hot_page_pool_size, TURNSTILE_API_URL, SITE_KEY, ACTION_ID, STATE_TREE
    max_mem_arg = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--max-mem' and i + 1 < len(sys.argv):
            max_mem_arg = sys.argv[i + 1]
        elif arg == '--target' and i + 1 < len(sys.argv):
            TARGET = int(sys.argv[i + 1])

    resources = get_system_resources(max_mem_arg)
    cpu = resources['cpu']
    capacity_profile = load_capacity_profile()

    # 自动派生容量
    physical_cap, s_workers, p_workers, c_workers = derive_capacity(
        cpu,
        resources['max_mem'],
        profile_physical_cap=capacity_profile.get("physical_cap"),
    )
    p_batch_max = derive_p_batch_max(physical_cap)
    p_send_cap = P_SEND_CAP if P_SEND_CAP > 0 else 0
    admission_watermarks = derive_admission_watermarks(physical_cap)
    _c_hot_page_pool_size = derive_c_hot_page_pool_size(physical_cap, c_workers)

    # 校验邮箱模式配置
    if EMAIL_MODE not in ('tempmail', 'moemail', 'custom'):
        log("[!] 配置错误：EMAIL_MODE 应为 tempmail、moemail 或 custom"); return 2
    if EMAIL_MODE == 'custom' and not EMAIL_DOMAIN:
        log("[!] 配置错误：custom 模式需在 .env 设置 EMAIL_DOMAIN"); return 2
    if EMAIL_MODE == 'moemail' and not MOEMAIL_API_KEY:
        log("[!] 配置错误：moemail 模式需在 .env 设置 MOEMAIL_API_KEY"); return 2

    debug_log("=" * 50)
    debug_log(f"  Grok Free Register (CSP Architecture)")
    debug_log(f"  CPU: {cpu} cores  Memory: {resources['available_mem']}/{resources['total_mem']}MB")
    debug_log(f"  MaxMemForAuto: {resources['max_mem']}MB  MemReserve: {MIN_FREE_MEM_MB}MB  PhysicalMemBudget: {PHYSICAL_MEM_MB}MB")
    if capacity_profile:
        debug_log(f"  CapacityProfile: {CAPACITY_PROFILE} physical_cap={capacity_profile.get('physical_cap')}")
    debug_log(f"  EmailMode: {EMAIL_MODE}")
    debug_log(f"  Physical_Sem={physical_cap}  T_Slot={T_SLOT_CAP}  Q_Slot={Q_SLOT_CAP}  Q_Pending={Q_PENDING_CAP}")
    debug_log(
        f"  Admission: T_LOW/HIGH={admission_watermarks['t_low']}/{admission_watermarks['t_high']}  "
        f"Q_LOW/HIGH={admission_watermarks['q_low']}/{admission_watermarks['q_high']}"
    )
    debug_log(f"  P_BatchMax={p_batch_max}  P_Send_Sem={'disabled' if p_send_cap == 0 else p_send_cap}")
    debug_log(
        f"  C_HotPagePool={'on' if C_HOT_PAGE_POOL else 'off'}"
        f" size={_c_hot_page_pool_size if C_HOT_PAGE_POOL else 0}"
        f" setCookieViaRequest={'on' if C_SET_COOKIE_VIA_REQUEST else 'off'}"
    )
    debug_log(f"  Workers: S={s_workers} P={p_workers} C={c_workers}")
    debug_log(
        f"  Timeouts: Solver={SOLVER_HARD_TIMEOUT}s SolverCleanup={SOLVER_CLEANUP_TIMEOUT}s "
        f"P_Request={P_REQUEST_TIMEOUT}s C_Consume={C_CONSUME_TIMEOUT}s"
    )
    debug_log(
        f"  SolverBackend: {TURNSTILE_SOLVER}"
        + (
            f" api={TURNSTILE_API_URL} timeout={TURNSTILE_API_TIMEOUT}s"
            if is_api_backend(TURNSTILE_SOLVER)
            else ""
        )
    )
    debug_log(
        f"  SolverMouseClick: retries={SOLVER_MOUSE_CLICK_RETRIES} "
        f"interval={SOLVER_MOUSE_CLICK_INTERVAL_MS}ms"
    )
    if TARGET:
        debug_log(f"  Target: {TARGET}")
    debug_log("=" * 50)

    # 先加载代理池(会把带认证 SOCKS5 经 sing-box 转成本地 HTTP),
    # 再把可用本地 HTTP 代理写给内置 Turnstile solver(Chromium 不支持 SOCKS5 auth)。
    await _prepare_auto_proxy_pool_before_start()
    try:
        with _proxy_pool_lock:
            pool_items = list(_load_proxy_pool_locked())
        written = _write_turnstile_solver_proxies(pool_items)
        if written:
            debug_log(f"[*] Turnstile solver proxies: {written} local HTTP endpoints")
            log(f"[*] 已为 Turnstile solver 写入 {written} 个本地 HTTP 代理(SOCKS5 鉴权中继)")
        elif pool_items:
            debug_log(f"[*] proxy pool has {len(pool_items)} items but none suitable for Chromium solver")
    except Exception as exc:
        debug_log(f"[*] write turnstile solver proxies failed: {sanitize_terminal_error(exc)}")

    reg_engine = (os.environ.get("REGISTER_ENGINE") or "protocol").strip().lower()

    # Protocol / Go path: pure HTTP signup — does NOT need Next.js ACTION_ID/STATE_TREE.
    # Never call Playwright fetch_config here: HF/proxy failure used to exit instantly.
    if reg_engine in {"go", "protocol", "http"}:
        from grok_register.go_register import maybe_run_go_register_from_python
        from grok_register.protocol_register import TURNSTILE_SITEKEY as _PROTO_SITEKEY

        SITE_KEY = SITE_KEY or _PROTO_SITEKEY
        ACTION_ID = ACTION_ID or ""
        STATE_TREE = STATE_TREE or ""
        log(
            f"[*] 协议注册 engine={reg_engine} sitekey={SITE_KEY} "
            f"(无需抓取 Next.js ACTION_ID/STATE_TREE)"
        )

        if is_api_backend(TURNSTILE_SOLVER):
            if turnstile_health_check(TURNSTILE_API_URL, timeout=1.2):
                log(f"[*] 协议注册：复用已运行的 Turnstile {TURNSTILE_API_URL}")
            else:
                log(
                    f"[*] 协议注册：Turnstile 按需启动；"
                    f"url={TURNSTILE_API_URL}"
                )
        log("[*] 协议注册引擎 (HTTP 协议路径，不写 accounts.cpa.json)")
        if TURNSTILE_API_URL:
            os.environ["TURNSTILE_API_URL"] = TURNSTILE_API_URL
        atexit.register(_stop_managed_turnstile_solver)
        try:
            code = maybe_run_go_register_from_python(
                SITE_KEY,
                ACTION_ID,
                STATE_TREE,
            )
            return 0 if code is None else int(code)
        finally:
            try:
                _stop_managed_turnstile_solver()
                log("[*] 协议注册结束：已停止 Turnstile solver 进程组")
            except Exception as exc:
                debug_log(f"stop solver after protocol: {exc}")

    # Browser path (REGISTER_ENGINE=python): needs page config + Playwright
    try:
        await fetch_config()
    except Exception as exc:
        log(f"[!] 浏览器路径 config 抓取失败: {sanitize_terminal_error(exc)}")
        return 2
    if not all([SITE_KEY, ACTION_ID, STATE_TREE]):
        log("[!] 浏览器路径需要 SITE_KEY/ACTION_ID/STATE_TREE，config 不完整")
        return 2

    # Turnstile warm-up for browser path
    if is_api_backend(TURNSTILE_SOLVER):
        try:
            if TURNSTILE_SOLVER in ("d3vin", "theyka"):
                log(
                    f"[!] 旧版 Turnstile 引擎 {TURNSTILE_SOLVER}；"
                    "推荐 hybrid 或 local"
                )
            solver_meta = await asyncio.to_thread(ensure_solver_for_register, log=log)
            TURNSTILE_API_URL = solver_meta.get("api_url") or resolve_api_url(TURNSTILE_SOLVER)
            if solver_meta.get("managed"):
                atexit.register(_stop_managed_turnstile_solver)
            eng = solver_meta.get("engine") or resolve_engine(TURNSTILE_SOLVER)
            if solver_meta.get("deferred"):
                log(
                    f"[*] Turnstile 按需模式 | mode={TURNSTILE_SOLVER} engine={eng} "
                    f"url={TURNSTILE_API_URL}（遇到问题再自动拉起）"
                )
            elif solver_meta.get("managed") or solver_meta.get("already_running"):
                log(
                    f"[*] Turnstile API 就绪 | mode={TURNSTILE_SOLVER} "
                    f"engine={eng} url={TURNSTILE_API_URL}"
                )
            else:
                log(f"[*] Turnstile API: {TURNSTILE_API_URL}")
        except Exception as exc:
            log(f"[!] Turnstile 配置检查失败: {sanitize_terminal_error(exc)}")
            if TURNSTILE_SOLVER not in ("local",):
                log("[!] 将在首次求解失败时再尝试启动 solver")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=find_chrome(), headless=True)
        debug_log('[*] Browser launched')

        # CSP 组件
        metrics = Metrics()
        inventory = Inventory(metrics=metrics)
        admission_gate = AdmissionGate(
            inventory,
            t_low=admission_watermarks["t_low"],
            t_high=admission_watermarks["t_high"],
            q_low=admission_watermarks["q_low"],
            q_high=admission_watermarks["q_high"],
        )
        physical_sem = asyncio.Semaphore(physical_cap)
        p_send_sem = asyncio.Semaphore(p_send_cap) if p_send_cap > 0 else None
        t_slot_sem = asyncio.Semaphore(T_SLOT_CAP)
        q_slot_sem = asyncio.Semaphore(Q_SLOT_CAP)
        q_pending_sem = asyncio.Semaphore(Q_PENDING_CAP)

        sems = {
            'physical': physical_sem,
            't_slot': t_slot_sem,
            'q_slot': q_slot_sem,
            'q_pending': q_pending_sem,
            'admission': admission_gate,
        }
        if p_send_sem is not None:
            sems['p_send'] = p_send_sem

        tasks = []

        # S_Workers
        for i in range(s_workers):
            tasks.append(asyncio.create_task(
                s_worker(i, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate)
            ))

        # P_Workers
        for i in range(p_workers):
            tasks.append(asyncio.create_task(
                p_worker(
                    i,
                    browser,
                    inventory,
                    physical_sem,
                    q_pending_sem,
                    q_slot_sem,
                    metrics,
                    admission_gate,
                    p_send_sem,
                    p_batch_max,
                )
            ))

        # C_Workers
        for i in range(c_workers):
            tasks.append(asyncio.create_task(
                c_worker(i, browser, inventory, physical_sem, metrics, admission_gate)
            ))

        # Monitor (+ runtime status for web control plane)
        runtime_extra = {
            "workers": {"S": s_workers, "P": p_workers, "C": c_workers},
            "physical_cap": physical_cap,
            "proxy_auto_enabled": bool(PROXY_AUTO_CONFIG.enabled),
        }
        tasks.append(
            asyncio.create_task(monitor(inventory, sems, metrics, runtime_extra=runtime_extra))
        )

        debug_log(f'[*] CSP up: S={s_workers} P={p_workers} C={c_workers} workers')
        log(
            format_user_registration_event(
                "service_started",
                remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
            )
        )

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            await _drain_key_export_tasks()
            await _close_c_hot_page_pool()
            await _close_browser_safely(browser)
            try:
                from grok_register.runtime_status import clear_pid, publish

                publish(
                    {
                        "service": "register",
                        "running": False,
                        "pid": os.getpid(),
                        "success_count": metrics.success_count,
                    }
                )
                clear_pid()
            except Exception:
                pass
            log(format_user_registration_event("stopped", count=metrics.success_count))
    return 0

if __name__ == "__main__":
    try:
        REGISTER_LOG_MODE = resolve_register_log_mode(sys.argv[1:])
    except ValueError:
        log("[!] 配置错误：REGISTER_LOG_MODE 应为 user 或 debug")
        raise SystemExit(2)
    try:
        from grok_register.runtime_status import write_pid

        write_pid(os.getpid())
    except Exception:
        pass
    try:
        exit_code = asyncio.run(main())
    except ValueError as exc:
        log(f"[!] 配置错误：{sanitize_terminal_error(exc)}")
        exit_code = 2
    except KeyboardInterrupt:
        log("[!] 用户中断")
        exit_code = 130
    finally:
        # Main stopped → kill hybrid Turnstile stack (gateway + browsers + watchdog)
        try:
            _stop_managed_turnstile_solver()
        except Exception:
            pass
        try:
            from grok_register.turnstile_solver import kill_orphan_solvers

            kill_orphan_solvers()
        except Exception:
            pass
        try:
            from grok_register.runtime_status import clear_pid

            clear_pid()
        except Exception:
            pass
    raise SystemExit(exit_code)
