"""
Grok Free Register — 自适应状态机版
=============================================
单进程 asyncio + 单共享 CloakBrowser + 中央状态机调度器:
  - Scheduler 按 token/验证码缓冲缺口动态分配 SOLVE/PRODUCE/CONSUME
  - load_governor 采样实时 CPU 利用率,自适应伸缩并发(对核数自动放大)

两种邮箱模式(EMAIL_MODE):
  - tempmail (默认,零配置): 免费临时邮箱,多 provider 自动 fallback
  - custom: 自建域名邮箱,Cloudflare Email Routing → Worker → 本地 webhook
            (见 email_server.py / cloudflare/email-worker.js)

配置全部走环境变量 / .env(见 .env.example);CLI: --max-mem 6G --target 100
用法:
  bash start.sh          # 一键引导
  python register.py
"""
import os, json, random, string, time, re, secrets, base64, struct, asyncio, glob, sys, multiprocessing
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import requests as req
from urllib.parse import quote
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor

os.makedirs("keys", exist_ok=True)
SITE_URL = "https://accounts.x.ai"

# ── 配置（环境变量 / .env，见 .env.example）──
def _env_int(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except ValueError:
        return default

EMAIL_MODE      = (os.environ.get("EMAIL_MODE") or "tempmail").strip().lower()   # tempmail | custom
if EMAIL_MODE == "mailtm":      # 兼容旧名
    EMAIL_MODE = "tempmail"
LOCAL_EMAIL_API = (os.environ.get("EMAIL_API") or "http://127.0.0.1:8080").strip()
EMAIL_DOMAIN    = (os.environ.get("EMAIL_DOMAIN") or "").strip()
CPU_TARGET      = _env_int("CPU_TARGET", 85)         # 调速器目标 CPU 利用率 %
MIN_FREE_MEM_MB = _env_int("MIN_FREE_MEM_MB", 500)   # 可用内存下限(MB),低于则收缩并发
T_TARGET        = _env_int("T_TARGET", 4)            # token 池缓冲目标
Q_TARGET        = _env_int("Q_TARGET", 4)            # 就绪验证码缓冲目标
TARGET          = _env_int("TARGET", 0)              # 攒够 N 个号自动停(0=不限;--target N 可覆盖)
_MAX_SLOTS_ENV  = (os.environ.get("MAX_SLOTS") or "").strip()  # 空=自动 cpu*4

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

def log(msg): print(msg, flush=True)
def rand_str(n=15): return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))
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
    except: return None
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
    except:
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


# ──────────────────────────────────────────────
#  配置获取
# ──────────────────────────────────────────────
async def fetch_config():
    global SITE_KEY, ACTION_ID, STATE_TREE
    log('[*] Fetching config...')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=find_chrome(), headless=True)
        try:
            page = await browser.new_page()
            await page.goto(f'{SITE_URL}/sign-up?redirect=grok-com', timeout=30000)
            await page.wait_for_timeout(5000)
            html = await page.content()
            m = re.search(r'0x4AAAAAAA[a-zA-Z0-9_-]+', html)
            if m: SITE_KEY = m.group(0); log(f'[+] SITE_KEY: {SITE_KEY}')
            for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
                if 'sign-up' not in chunk: continue
                decoded = chunk.replace('\\"', '"')
                f_match = re.search(r'"f":\[\[\[', decoded)
                if not f_match: continue
                f_start = f_match.start() + 5
                end_idx = decoded.find('"$undefined"', f_start)
                if end_idx < 0: continue
                STATE_TREE = quote(decoded[f_start:end_idx].replace('\\\\"', '"').replace('\\', ''), safe='')
                log(f'[+] STATE_TREE: {STATE_TREE[:50]}...')
                break
            js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
            for js_url in js_urls[:50]:
                try:
                    js = await page.evaluate(f"(async()=>{{return await fetch('{js_url}').then(r=>r.text()).catch(()=>\"\" )}})()")
                    if not js: continue
                    if not any(kw in js for kw in ['createUser','registerUser','emailValidation']): continue
                    hexes = re.findall(r'[a-fA-F0-9]{40,50}', js)
                    if hexes: ACTION_ID = hexes[0]; break
                except: continue
            if ACTION_ID: log(f'[+] ACTION_ID: {ACTION_ID}')
        finally:
            await browser.close()
    if not all([SITE_KEY, ACTION_ID, STATE_TREE]):
        raise RuntimeError("Config fetch failed")


# ──────────────────────────────────────────────
#  异步操作
# ──────────────────────────────────────────────
async def grpc_create_code(page, email):
    inner = pb_str(1, email)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    return s == '0'

async def grpc_verify_code(page, email, code):
    inner = pb_str(1, email) + pb_str(2, code)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    return s == '0'

async def server_action_register(page, email, password, code, turnstile_token):
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
    result_text = await page.evaluate(f"""(async()=>{{var r=await fetch('{SITE_URL}/sign-up',{{method:'POST',headers:{{'accept':'text/x-component','content-type':'text/plain;charset=UTF-8','next-router-state-tree':'{STATE_TREE}','next-action':'{ACTION_ID}'}},body:atob('{pb64}')}});return await r.text();}})()""")
    # 注册响应里带一个 set-cookie 重定向 URL,必须访问它,x.ai 才会下发真正的 sso cookie(152 字符 JWT)。
    # 注意:直接解 q= 里的 JWT 取 config.token 是错的——那是 120 字符内部 blob,不是 sso 凭证。
    text = result_text.replace('\\/', '/')  # RSC 里 / 被转义成 \/
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[^:" \s\\]+)1:', text)
    if not m:
        m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[A-Za-z0-9_.\-]+)', text)
    if not m:
        return None
    url = m.group(1)
    # 首方导航访问该 URL,浏览器正常落 sso cookie(跨域 fetch 会被 CORS/三方cookie 拦)
    try:
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
    except Exception:
        pass
    cookies = await page.context.cookies()
    return next((c['value'] for c in cookies if c['name'] == 'sso'), None)

# solver 预热页面池:复用已停在 sign-up 的页面,省掉每次 page.goto 的重型 SPA 加载
# SOLVER_REUSE=0 可关闭(用于 A/B 对比 goto 优化的增益)
SOLVER_REUSE = (os.environ.get("SOLVER_REUSE", "1").strip().lower() not in ("0", "false", "no"))
_solver_pool = []
_solver_lock = asyncio.Lock()
MAX_SOLVER_REUSE = 25

async def _get_solver_page(browser):
    if SOLVER_REUSE:
        async with _solver_lock:
            if _solver_pool:
                return _solver_pool.pop()
    p = await browser.new_page()
    await p.set_viewport_size({"width": 800, "height": 600})
    await p.goto(f'{SITE_URL}/sign-up', timeout=20000)
    await p.wait_for_timeout(1000)
    return {"page": p, "n": 0}

async def _put_solver_page(item, ok):
    p = item["page"]
    item["n"] += 1
    if SOLVER_REUSE and ok and item["n"] < MAX_SOLVER_REUSE:
        try:  # 清理本次注入痕迹,留待复用
            await p.evaluate("document.querySelectorAll('.cf-turnstile').forEach(e=>e.remove());var i=document.querySelector('input[name=\"cf-turnstile-response\"]');if(i)i.remove();")
            async with _solver_lock:
                _solver_pool.append(item)
            return
        except Exception:
            pass
    try: await p.close()
    except Exception: pass

async def solve_one_turnstile(browser):
    item = await _get_solver_page(browser)
    p = item["page"]
    ok = False
    try:
        # 注入 widget;turnstile 脚本已加载(复用页面)则直接 render,否则先加载脚本
        await p.evaluate(f"""var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{window.turnstile&&window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;}}}})}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{setTimeout(__r,1000)}};document.head.appendChild(s);}}""")
        await p.wait_for_timeout(2000)
        for sel in ["iframe[src*='challenges.cloudflare.com']","iframe[src*='turnstile']",".cf-turnstile iframe"]:
            try:
                fr = p.frame_locator(sel).first
                await fr.locator("#checkbox, .checkbox, input[type=checkbox], body").first.click(timeout=3000)
                break
            except: continue
        for i in range(50):
            await asyncio.sleep(1)
            try:
                t = await p.evaluate('document.querySelector("input[name=\\"cf-turnstile-response\\"]")?.value||""')
                if t and len(t) > 10:
                    ok = True
                    return t
            except: pass
            if i > 0 and i % 10 == 0:
                try: await p.locator(".cf-turnstile").first.click(timeout=1000)
                except: pass
        return None
    except:
        return None
    finally:
        await _put_solver_page(item, ok)


# ──────────────────────────────────────────────
#  邮箱服务:custom(自建 webhook) / tempmail(免费临时邮箱,多 provider fallback)
# ──────────────────────────────────────────────
# 免 key 的公共临时邮箱 provider(实测可用,互为 fallback 消灭单点):
#  - mail.tm 同协议:mail.tm / mail.gw / duckmail.sbs
#  - 独立 API:tempmail.lol
# handle 编码 provider,供 poll_code 分派;新增 provider 只要在这两处加一段即可。
TEMPMAIL_BASES = ["https://api.mail.tm", "https://api.mail.gw", "https://api.duckmail.sbs"]

def _extract_code(text):
    """多层兜底提取验证码,抗邮件模板变化。"""
    for pat in (r'>([A-Z0-9]{3}-[A-Z0-9]{3})<', r'>([A-Z0-9]{6})<', r'\b([A-Z0-9]{3}-?[A-Z0-9]{3})\b'):
        m = re.search(pat, text)
        if m:
            return m.group(1).replace('-', '')
    return None

def _mailtm_create(base, password):
    """mail.tm 同协议建箱;返回 (handle, email)。"""
    d = req.get(f'{base}/domains', timeout=12).json()
    d = d.get('hydra:member', d) if isinstance(d, dict) else d
    doms = [x['domain'] for x in d if x.get('isActive', True) and not x.get('isPrivate', False)]
    if not doms:
        raise RuntimeError('no domain')
    email = f'oc{secrets.token_hex(5)}@{doms[0]}'
    req.post(f'{base}/accounts', json={'address': email, 'password': password}, timeout=12)
    tok = req.post(f'{base}/token', json={'address': email, 'password': password}, timeout=12).json().get('token', '')
    if not tok:
        raise RuntimeError('no token')
    return f'mt|{base}|{tok}', email

def _lol_create():
    """tempmail.lol 建箱;返回 (handle, email)。"""
    r = req.post('https://api.tempmail.lol/v2/inbox/create', timeout=12).json()
    addr, tok = r.get('address', ''), r.get('token', '')
    if not addr or not tok:
        raise RuntimeError('lol create failed')
    return f'lol|{tok}', addr

def create_email():
    """custom 用自建域名(本地 webhook);tempmail 随机打散多个 provider,逐个 fallback。"""
    if EMAIL_MODE == 'custom':
        email = f'oc{secrets.token_hex(5)}@{EMAIL_DOMAIN}'
        password = rand_str()
        return email, email, password  # 地址即用,验证码经 CF Worker POST 到本地 webhook

    password = rand_str()
    # 优先用已跑通的 mail.tm,其余按序仅作 fallback
    makers = [(lambda b=b: _mailtm_create(b, password)) for b in TEMPMAIL_BASES] + [_lol_create]
    for make in makers:
        try:
            handle, email = make()
            return handle, email, password
        except Exception:
            continue
    raise RuntimeError('所有临时邮箱 provider 均不可用')

def _tempmail_fetch(handle):
    """按 handle 前缀分派,取该邮箱当前邮件全文(subject+text+html);无则 None。"""
    kind = handle.split('|', 1)[0]
    if kind == 'lol':
        tok = handle.split('|', 1)[1]
        data = req.get(f'https://api.tempmail.lol/v2/inbox?token={tok}', timeout=10).json()
        items = data.get('emails') or data.get('messages') or []
        if not items:
            return None
        return '\n'.join(f"{i.get('subject','')}\n{i.get('body','')}\n{i.get('html','')}"
                         for i in items if isinstance(i, dict))
    # mail.tm 同协议:handle = "mt|base|token"
    _, base, tok = handle.split('|', 2)
    hdr = {'Accept': 'application/json', 'Authorization': f'Bearer {tok}'}
    data = req.get(f'{base}/messages', headers=hdr, timeout=10).json()
    msgs = data if isinstance(data, list) else data.get('hydra:member', [])
    if not msgs:
        return None
    mid = str(msgs[0].get('id') or '')
    detail = req.get(f'{base}/messages/{mid}', headers=hdr, timeout=10).json()
    parts = [str(detail.get(k, '')) for k in ['subject', 'intro', 'text', 'html']]
    if isinstance(detail.get('html'), list):
        parts.append('\n'.join(str(x) for x in detail['html']))
    return '\n'.join(parts)

def poll_code(handle, max_wait=90):
    """轮询验证码:custom 查本地 webhook /check;tempmail 按 provider 取信。"""
    if EMAIL_MODE == 'custom':
        for _ in range(max_wait):
            time.sleep(1)
            try:
                resp = req.get(f'{LOCAL_EMAIL_API}/check/{handle}', timeout=5)
                if resp.status_code == 200 and resp.json().get('code'):
                    return resp.json()['code']
            except Exception:
                pass
        return None

    for _ in range(max_wait):
        time.sleep(1)
        try:
            text = _tempmail_fetch(handle)
            if text:
                code = _extract_code(text)
                if code:
                    return code
        except Exception:
            pass
    return None


# ──────────────────────────────────────────────
#  自适应状态机调度器
# ──────────────────────────────────────────────
class Scheduler:
    """持有流水线实时状态,决定每个工作单元此刻该解 token / 发码 / 注册,
    并按 CPU 负载动态伸缩并发(target_slots)。"""
    def __init__(self, token_pool, ready_queue, cpu):
        self.tokens = token_pool
        self.ready = ready_queue
        self.cpu = cpu
        # MAX_SLOTS 留空=自动 cpu*4;否则用用户配置(下限 2)
        self.max_slots = max(2, int(_MAX_SLOTS_ENV)) if _MAX_SLOTS_ENV.isdigit() else max(4, cpu * 4)
        self.target_slots = min(self.max_slots, max(2, cpu))
        self.active = 0
        self.codes_sent = 0
        self.codes_got = 0
        self.bg = set()

    def pick_role(self):
        T, Q = self.tokens.qsize(), self.ready.qsize()
        if T == 0 and Q > 0:   return SOLVE      # consumer 缺 token,最高优先
        if Q > 0 and T > 0:    return CONSUME    # 两路就绪 → 出号
        if T < T_TARGET:       return SOLVE      # 补 token 缓冲
        if Q < Q_TARGET:       return PRODUCE    # 补验证码
        return IDLE                              # 缓冲都满,歇着

    async def gate(self):
        # active < target_slots 之间无 await,asyncio 单线程下不会交错,放行原子
        while self.active >= self.target_slots:
            await asyncio.sleep(0.15)
        self.active += 1

    def release(self):
        if self.active > 0:
            self.active -= 1

    def spawn_bg(self, coro):
        t = asyncio.create_task(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)


async def poll_and_enqueue(sched, sent):
    """异步轮询验证码并入队(不占并发名额,阻塞轮询跑在线程池里)。"""
    loop = asyncio.get_event_loop()
    futs = [(it, loop.run_in_executor(POLL_EXECUTOR, poll_code, it['jwt'])) for it in sent]
    for it, fut in futs:
        try:
            code = await asyncio.wait_for(fut, timeout=95)
        except asyncio.TimeoutError:
            log(f'[P] {it["email"]} poll timeout'); continue
        if code:
            sched.codes_got += 1
            await sched.ready.put({'email': it['email'], 'password': it['password'], 'code': code})
            log(f'[P] {it["email"]} code={code} q:{sched.ready.qsize()}')
        else:
            log(f'[P] {it["email"]} no code')


async def produce_codes(browser, sched):
    """补足就绪队列:按缺口创建邮箱,单个长驻 page 连发 gRPC,轮询另起后台任务。"""
    loop = asyncio.get_event_loop()
    need = min(5, Q_TARGET - sched.ready.qsize())
    if need <= 0:
        return
    batch = []
    for _ in range(need):
        try:
            jwt, email, password = await loop.run_in_executor(POLL_EXECUTOR, create_email)
            batch.append({'jwt': jwt, 'email': email, 'password': password})
        except Exception:
            pass
    if not batch:
        return
    sent = []
    page = await browser.new_page()
    try:
        await page.set_viewport_size({"width": 800, "height": 600})
        await page.goto(f'{SITE_URL}/sign-up?redirect=grok-com', timeout=30000)
        await page.wait_for_timeout(1500)
        for item in batch:
            try:
                if await grpc_create_code(page, item['email']):
                    sched.codes_sent += 1
                    sent.append(item)
                    log(f'[P] {item["email"]} code sent')
            except Exception:
                pass
            await asyncio.sleep(1)
    finally:
        try: await page.close()
        except Exception: pass
    if sent:
        sched.spawn_bg(poll_and_enqueue(sched, sent))


async def consume_one(browser, sched):
    """取 1 token + 1 就绪项完成注册;取不全则放回,不丢单。"""
    global success_count
    try:
        tok_item = sched.tokens.get_nowait()
    except asyncio.QueueEmpty:
        return
    try:
        item = sched.ready.get_nowait()
    except asyncio.QueueEmpty:
        try: sched.tokens.put_nowait(tok_item)
        except asyncio.QueueFull: pass
        return
    email, password, code = item['email'], item['password'], item['code']
    token = tok_item['token']
    t0 = time.time()
    try:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 800, "height": 600})
        await page.goto(f'{SITE_URL}/sign-up?redirect=grok-com', timeout=30000)
        await page.wait_for_timeout(1500)
        sso = None
        if await grpc_verify_code(page, email, code):
            sso = await server_action_register(page, email, password, code, token)
        try: await page.close()
        except Exception: pass
        if sso:
            elapsed = time.time() - t0
            async with file_lock:
                with open("keys/grok.txt", "a") as f: f.write(sso + "\n")
                with open("keys/accounts.txt", "a") as f: f.write(f"{email}:{password}:{sso}\n")
                success_count += 1
                count = success_count
            avg = (time.time() - start_time) / count
            log(f'[✓] {email} {elapsed:.1f}s avg:{avg:.1f}s #{count}')
    except Exception as e:
        log(f'[C] Error: {str(e)[:60]}')


async def worker(wid, browser, sched):
    """通用工作单元:循环向调度器要角色并执行,并发由 gate 控制。"""
    while not STOP.is_set():
        role = sched.pick_role()
        if role == IDLE:
            await asyncio.sleep(0.5)
            continue
        await sched.gate()
        try:
            if role == SOLVE:
                token = await solve_one_turnstile(browser)
                if token:
                    try: sched.tokens.put_nowait({'token': token, 'time': time.time()})
                    except asyncio.QueueFull: pass
            elif role == PRODUCE:
                await produce_codes(browser, sched)
            elif role == CONSUME:
                await consume_one(browser, sched)
        except Exception as e:
            log(f'[W{wid}] {role} err: {str(e)[:60]}')
        finally:
            sched.release()
        await asyncio.sleep(0.3)


# ──────────────────────────────────────────────
#  负载控制器（按实时 CPU 利用率自适应伸缩 + 监控）
# ──────────────────────────────────────────────
def _cpu_times():
    with open('/proc/stat') as f:
        nums = list(map(int, f.readline().split()[1:]))
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle

def _free_mem_mb():
    """可用内存(MB);读不到返回很大值表示不限。"""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 1 << 30

async def load_governor(sched):
    try:
        prev = _cpu_times()
    except Exception:
        prev = None
    while not STOP.is_set():
        await asyncio.sleep(4)
        cpu_pct = float(CPU_TARGET)
        try:
            cur = _cpu_times()
            if prev:
                dt, di = cur[0] - prev[0], cur[1] - prev[1]
                if dt > 0: cpu_pct = 100.0 * (1 - di / dt)
            prev = cur
        except Exception:
            pass

        free_mb = _free_mem_mb()
        starving = sched.tokens.qsize() < T_TARGET or sched.ready.qsize() < Q_TARGET
        # 内存护栏优先:可用内存低于下限就收缩;否则按 CPU 目标(±5%)自适应伸缩。
        if free_mb < MIN_FREE_MEM_MB and sched.target_slots > 2:
            sched.target_slots -= 1
        elif cpu_pct > CPU_TARGET + 5 and sched.target_slots > 2:
            sched.target_slots -= 1
        elif (cpu_pct < CPU_TARGET - 5 and starving
              and free_mb > MIN_FREE_MEM_MB and sched.target_slots < sched.max_slots):
            sched.target_slots += 1

        elapsed = time.time() - start_time
        rate = success_count / (elapsed / 60) if elapsed > 60 else 0
        hit = (sched.codes_got / sched.codes_sent * 100) if sched.codes_sent else 0
        log(f'[*] slots:{sched.target_slots}/{sched.max_slots} act:{sched.active} '
            f'cpu:{cpu_pct:.0f}%/{CPU_TARGET} mem:{free_mb}M T:{sched.tokens.qsize()} Q:{sched.ready.qsize()} '
            f'sent:{sched.codes_sent} got:{sched.codes_got}({hit:.0f}%) '
            f'rate:{rate:.1f}/min #{success_count}')
        if TARGET and success_count >= TARGET:
            log(f'[*] 已达目标 {TARGET} 个,停止。'); STOP.set()


# ──────────────────────────────────────────────
#  主入口
# ──────────────────────────────────────────────
async def main():
    global TARGET
    max_mem_arg = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--max-mem' and i + 1 < len(sys.argv):
            max_mem_arg = sys.argv[i + 1]
        elif arg == '--target' and i + 1 < len(sys.argv):
            TARGET = int(sys.argv[i + 1])

    resources = get_system_resources(max_mem_arg)
    cpu = resources['cpu']

    # 校验邮箱模式配置
    if EMAIL_MODE not in ('tempmail', 'custom'):
        log(f"[!] EMAIL_MODE 非法: {EMAIL_MODE}(应为 tempmail 或 custom)"); sys.exit(1)
    if EMAIL_MODE == 'custom' and not EMAIL_DOMAIN:
        log("[!] custom 模式需在 .env 设置 EMAIL_DOMAIN(并运行 email_server.py)"); sys.exit(1)

    log("=" * 50)
    log(f"  Grok Free Register (Adaptive Scheduler)")
    log(f"  CPU: {cpu} cores  Memory: {resources['available_mem']}/{resources['total_mem']}MB")
    log(f"  EmailMode: {EMAIL_MODE}" + (f" ({EMAIL_DOMAIN})" if EMAIL_MODE == 'custom' else ""))
    log(f"  Limits: MAX_SLOTS={_MAX_SLOTS_ENV or f'auto(cpu*4)'} CPU_TARGET={CPU_TARGET}% MIN_FREE_MEM={MIN_FREE_MEM_MB}M" +
        (f" TARGET={TARGET}" if TARGET else ""))
    log("=" * 50)

    await fetch_config()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=find_chrome(), headless=True)
        log('[*] Browser launched')

        token_pool = asyncio.Queue(maxsize=20)
        ready_queue = asyncio.Queue(maxsize=50)
        sched = Scheduler(token_pool, ready_queue, cpu)

        tasks = [asyncio.create_task(load_governor(sched))]
        for i in range(sched.max_slots):
            tasks.append(asyncio.create_task(worker(i, browser, sched)))
        log(f'[*] Scheduler up: {sched.max_slots} workers, target {sched.target_slots} slots (cpu={cpu})')

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log('[*] Shutting down...')
        finally:
            for t in tasks:
                t.cancel()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
