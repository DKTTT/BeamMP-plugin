# -*- coding: utf-8 -*-
"""
=====================================================================
#  BMPLogin 公网 HTTP API v1.0   (路线 C: 认证器 ↔ 服务器直连)
---------------------------------------------------------------------
  作用: 玩家 Bridge.exe 直接通过公网 HTTP 调用, 不用进游戏聊天.
        直接读写 bmp_login/accounts.json 和 bmp_login/guestmap.json
        (与 BeamMP 服务器主插件共享数据文件).

  API 列表 (全部 POST, JSON body; 返回 JSON {ok, msg, data?}):
    POST /api/auth/register   {username, password, hwid?}
    POST /api/auth/login      {username, password, hwid, ip?, player_name?}
    POST /api/auth/logout     {username, hwid?}
    POST /api/auth/whoami     {hwid, ip?, player_name?}
    POST /api/hwid/bind       {hwid, username?, ip?, player_name?}
    POST /api/vehicle/limit   {hwid, username?, ip?, player_name?}
    GET  /api/ping            → ok=True, ver="..."

  部署 (Windows BeamMP 服务器机器):
      1) 已有 Python 3.10+
      2) pip 不需要额外依赖 (只用标准库: http.server + json)
      3) python BMP_HTTP_API.py --host 0.0.0.0 --port 12124
      4) Windows 防火墙放行 12124 端口
      5) Bridge.exe 顶部填入: http://<服务器公网IP>:12124

  安全:
    • 所有密码传输用 sha256(password) 发送, 与服务器存储的 hashPassword 兼容.
    • hwid 与 player_name 均参与 session: /login 后生成 access_token,
      后续 /whoami /vehiclelimit /logout 携带 Authorization: Bearer <token>.
    • 支持白名单模式 (--allow-hwid 只允许特定 HWID 调用, 或 --allow-ips a.b.c.d/24).
=====================================================================
"""

import re, os, sys, json, hashlib, hmac, time, threading, secrets, argparse, ipaddress, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 共享路径 — 和 main.lua 一致
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BMP_DATA_DIR") or os.path.join(_HERE, "bmp_login")
ACCOUNTS_FILE = GUESTMAP_FILE = BANLIST_FILE = CHAT_QUEUE_FILE = None
ONLINE_FILE = KICK_QUEUE_FILE = VOTE_QUEUE_FILE = ADMIN_AUDIT_FILE = None
BINDINGS_FILE = None


class DataFileError(RuntimeError):
    """数据文件存在但无法解析 (损坏 / 写到一半) —— 必须拒绝写入, 绝不当成空库"""


def _set_data_dir(path):
    """统一推导全部数据文件路径.
    以前 --data-dir 只改了 6 个路径, 漏了 chat_queue.json / admin_audit.jsonl,
    导致 Bridge 聊天写进旧目录, Lua 插件永远读不到 (聊天完全不广播)。"""
    global DATA_DIR, ACCOUNTS_FILE, GUESTMAP_FILE, BANLIST_FILE, CHAT_QUEUE_FILE
    global ONLINE_FILE, KICK_QUEUE_FILE, VOTE_QUEUE_FILE, ADMIN_AUDIT_FILE, BINDINGS_FILE
    DATA_DIR = os.path.abspath(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
    GUESTMAP_FILE = os.path.join(DATA_DIR, "guestmap.json")
    BANLIST_FILE = os.path.join(DATA_DIR, "banlist.json")
    CHAT_QUEUE_FILE = os.path.join(DATA_DIR, "chat_queue.json")
    ONLINE_FILE = os.path.join(DATA_DIR, "online_players.json")
    KICK_QUEUE_FILE = os.path.join(DATA_DIR, "kick_queue.json")
    VOTE_QUEUE_FILE = os.path.join(DATA_DIR, "vote_queue.json")
    ADMIN_AUDIT_FILE = os.path.join(DATA_DIR, "admin_audit.jsonl")
    BINDINGS_FILE = os.path.join(DATA_DIR, "bindings.json")


_set_data_dir(DATA_DIR)

VEHICLE_LIMITS = {"admin": 999, "authenticated": 5, "unauthenticated": 1}
DEFAULT_API_PORT = 12124
VERSION = "BMP-HTTP/v1.5.0"
ACCESS_TTL = 7 * 24 * 3600  # 7 天

# 聊天速率限制: name -> [时间戳列表]
CHAT_RATE_LIMIT = {}
CHAT_RATE_WINDOW = 60   # 60 秒
CHAT_RATE_MAX = 6        # 最多 6 条/分钟

# ---- 登录速率限制 ----
LOGIN_RATE = {}          # key: "ip:username" -> [timestamps]
LOGIN_RATE_WINDOW = 300  # 5 分钟窗口
LOGIN_RATE_MAX = 5       # 每 5 分钟最多 5 次登录尝试
LOGIN_LOCKOUT_SECONDS = 600  # 超限后锁定 10 分钟

# ---- 请求体限制 ----
MAX_BODY_SIZE = 65536    # 64 KB (JSON body)

# ---- 投票请求限速 (按客户端 IP) ----
VOTE_RATE = {}
VOTE_RATE_WINDOW = 60
VOTE_RATE_MAX = 6


def _vote_allowed(ip):
    now = int(time.time())
    hits = [t for t in VOTE_RATE.get(ip, []) if now - t < VOTE_RATE_WINDOW]
    if len(hits) >= VOTE_RATE_MAX:
        VOTE_RATE[ip] = hits
        return False
    hits.append(now)
    VOTE_RATE[ip] = hits
    if len(VOTE_RATE) > 5000:
        for k in list(VOTE_RATE.keys())[:2500]:
            VOTE_RATE.pop(k, None)
    return True

# ---- 管理员审计日志 ----
# ADMIN_AUDIT_FILE 由 _set_data_dir() 统一赋值

def _audit_log(action, admin, target="", detail=""):
    """管理员操作审计 — 追加写入 JSONL (每行一条, 不截断)"""
    try:
        entry = {
            "ts": int(time.time()),
            "action": action,
            "admin": admin,
            "target": target,
            "detail": detail,
            "ip": "",
        }
        with FILE_LOCK:
            with open(ADMIN_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _check_login_rate(ip, username):
    """检查登录速率限制, 返回 (允许, 剩余秒数)"""
    key = f"{ip}:{username}"
    now = int(time.time())
    # 清理过期记录
    if key in LOGIN_RATE:
        LOGIN_RATE[key] = [t for t in LOGIN_RATE[key] if now - t < LOGIN_RATE_WINDOW]
    else:
        LOGIN_RATE[key] = []
    recent = LOGIN_RATE[key]
    if len(recent) >= LOGIN_RATE_MAX:
        oldest = recent[0]
        wait = LOGIN_LOCKOUT_SECONDS - (now - oldest)
        return False, max(0, wait)
    return True, 0

def _record_login_attempt(ip, username):
    key = f"{ip}:{username}"
    LOGIN_RATE.setdefault(key, [])
    LOGIN_RATE[key].append(int(time.time()))
    # 防止字典无限增长
    if len(LOGIN_RATE) > 10000:
        for k in list(LOGIN_RATE.keys())[:5000]:
            del LOGIN_RATE[k]

# ============================================================
#  JSON 数组兼容 (与 main.lua Array metatable 一致: 空表是 [])
# ============================================================
def _ensure_list(v):
    if isinstance(v, list): return v
    if isinstance(v, dict) and not v: return []
    return []

def _deduplicate_bids(bids):
    """精简 bind_beam_ids: GUEST 只保留最近一条 (减少 UI 噪音)"""
    bids = _ensure_list(bids)
    if len(bids) <= 3:
        return bids
    grouped = {}
    order = []
    for b in bids:
        tag = b.split(":", 1)[0] if ":" in b else "OTHER"
        if tag not in grouped:
            grouped[tag] = []
            order.append(tag)
        grouped[tag].append(b)
    result = []
    for tag in order:
        items = grouped[tag]
        if tag == "GUEST" and len(items) > 1:
            result.append(items[-1])  # 只保留最近一条
        else:
            result.extend(items)
    return result

def readJsonFile(path, retries=4, delay=0.04):
    """读 JSON. 文件不存在 -> None; 文件损坏 -> 抛 DataFileError.

    旧实现把所有异常都吞成 None, 于是"文件损坏"和"文件不存在"无法区分,
    随后的注册/封禁会把整份 accounts.json / banlist.json 覆盖成只剩一条记录。

    这里额外做几次重试: Lua 插件与 Python API 是两个进程, Lua 在 Windows 上
    改名失败时会退回"截断后重写", 正好读到半个文件是可能的。重试能消掉这种
    瞬时误判, 真正损坏的文件仍然会抛异常 (调用方拒绝写入)。"""
    if not os.path.exists(path):
        return None
    last = None
    for attempt in range(max(1, retries)):
        for enc in ("utf-8-sig", "utf-8"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return json.loads(f.read() or "null")
            except (OSError, ValueError) as e:
                last = e
        if attempt + 1 < retries:
            time.sleep(delay)
    raise DataFileError(f"{os.path.basename(path)}: {last}")

def writeJsonFile(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True, None
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return False, str(e)

# ============================================================
#  密码 hash (与 main.lua 的 hashPassword / verifyPassword 完全一致)
#    main.lua 格式:  hashPassword -> salt_hex16 : 64 printable chars
#      hash64[i] = (password[i%pwdLen] + saltBytes[i%8] + i) % 95 + 32
#      (ASCII 32..126 可打印字符，含符号空格, 不是 hex sha256)
# ============================================================
def _lua_hash64(password, salt_hex):
    # 必须按 UTF-8 字节计算, 与 main.lua 的 password:byte(i) / #password 一致;
    # 旧实现按 Unicode 码点算, 中文密码在游戏内永远登录失败
    if not isinstance(password, str) or password == "":
        raise ValueError("empty password")
    pwd = password.encode("utf-8")
    salt_bytes = [int(salt_hex[i:i+1], 16) for i in range(16)]
    pwd_len = max(1, len(pwd))
    out = []
    for i in range(1, 65):
        ci = pwd[(i - 1) % pwd_len]
        si = salt_bytes[(i - 1) % 8]
        out.append(chr(((ci + si + i) % 95) + 32))
    return "".join(out)

def hashPassword(password):
    import secrets as _s
    salt_hex = _s.token_hex(8)   # 16 位 hex
    return f"{salt_hex}:{_lua_hash64(password, salt_hex)}"

def verifyPassword(password, stored):
    if not stored: return False
    if not isinstance(password, str) or password == "": return False
    # 格式 1: "salt_hex16:hash64" (main.lua / API 注册的新账号)
    if ":" in stored:
        # salt 在最左, hash 在最后一个冒号之后 (main.lua verify: 匹配^([^:]+): 和 :(.+)$)
        idx = stored.find(":")
        salt_hex = stored[:idx]
        hash_part = stored[idx+1:]
        if len(hash_part) > 64:
            hash_part = hash_part[-64:]
        if len(salt_hex) == 16:
            computed = _lua_hash64(password, salt_hex)
            if hmac.compare_digest(computed, hash_part):
                return True
        # 格式 2: "salt_hex:<64位小写hex sha256(salt::pwd)>"
        _, got = _salted_hash_sha256(password, salt_hex)
        if hmac.compare_digest(got, hash_part):
            return True
        return False
    # 格式 3: 只有 64 位 sha256 (没有 salt, 纯 sha256(password))
    got = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(got, stored)

def _salted_hash_sha256(password, salt_hex=None):
    """兼容老的 sha256(salt::password) 格式 (未来可能再用, 保留)"""
    if salt_hex is None:
        salt_hex = secrets.token_hex(8)
    combined = f"{salt_hex}::{password}".encode("utf-8")
    return salt_hex, hashlib.sha256(combined).hexdigest()

# ============================================================
#  共享数据: accounts / guestmap / banlist
# ============================================================
def loadAccounts():
    data = readJsonFile(ACCOUNTS_FILE)
    if data is None or (isinstance(data, list) and not data):
        data = {}
    if not isinstance(data, dict):
        raise DataFileError("accounts.json: 顶层不是对象")
    for k, v in data.items():
        if isinstance(v, dict):
            v["bind_beam_ids"] = _ensure_list(v.get("bind_beam_ids"))
            v["login_records"] = _ensure_list(v.get("login_records"))
    return data

def saveAccounts(data):
    return writeJsonFile(ACCOUNTS_FILE, data)

def loadGuestMap():
    data = readJsonFile(GUESTMAP_FILE)
    return data if isinstance(data, dict) else {}

def saveGuestMap(gm):
    return writeJsonFile(GUESTMAP_FILE, gm)

def loadBanlist():
    data = readJsonFile(BANLIST_FILE)
    if data is None or (isinstance(data, list) and not data):
        data = {}
    if not isinstance(data, dict):
        raise DataFileError("banlist.json: 顶层不是对象")
    data["devices"] = _ensure_list(data.get("devices"))
    data["accounts"] = _ensure_list(data.get("accounts"))
    return data

# ============================================================
#  业务逻辑
# ============================================================
FILE_LOCK = threading.RLock()
ADMINS = set(a.strip() for a in os.environ.get("BMP_ADMINS","").split(",") if a.strip())

def _is_admin(username):
    if not username: return False
    # 1) 检查 ADMINS 集合 (命令行 --admins)
    if username in ADMINS:
        return True
    # 2) 检查 accounts.json 中的 is_admin 字段
    try:
        accounts = loadAccounts()
        acc = accounts.get(username)
        if acc and isinstance(acc, dict) and acc.get("is_admin"):
            return True
    except Exception:
        pass
    return False

def _normalize_beam_ids(hwid, ip, player_name, username_bound=None):
    """按 main.lua 的 3 个优先级返回候选 beam_id 列表"""
    bids = []
    if hwid and isinstance(hwid, str) and len(hwid) >= 8:
        bids.append("HWID:settings.BMPLoginHWID(uuid):" + str(hwid).lower().strip())
    if ip and isinstance(ip, str) and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        bids.append("IP:" + ip)
    if player_name and isinstance(player_name, str) and 2 <= len(player_name) <= 40:
        gm = player_name
        if gm.lower().startswith("guest"):
            bids.append("GUEST:" + gm)
        else:
            bids.append("NAME:" + gm)
    # 账号强制绑定 (如果存在 username, 把账号自己的绑定加进去, 方便查找)
    if username_bound and isinstance(username_bound, str):
        pass
    return bids

def _find_account(accounts, bids):
    """在 accounts 的 bind_beam_ids 里找命中 bids 的账号"""
    for username, acc in accounts.items():
        bind_ids = _ensure_list(acc.get("bind_beam_ids"))
        for b in bind_ids:
            if b in bids:
                return username
    return None

def _account_is_admin(accounts, username):
    """ADMINS 集合 或 accounts.json 里持久化的 is_admin 都算管理员"""
    if not username:
        return False
    if username in ADMINS:
        return True
    acc = accounts.get(username) if isinstance(accounts, dict) else None
    return bool(isinstance(acc, dict) and acc.get("is_admin"))

def _revoke_user_tokens(username):
    """改密/删号/撤管理员时立刻作废该账号所有 token.
    旧实现只校验过期时间, 密码被重置后攻击者手里的 token 还能用满 7 天。"""
    if not username:
        return 0
    with TOKENS_LOCK:
        to_pop = [t for t, info in TOKENS.items() if info.get("username") == username]
        for t in to_pop:
            TOKENS.pop(t, None)
    return len(to_pop)

# Access tokens: token -> {username, issued_at, expires_at}
TOKENS = {}
TOKENS_LOCK = threading.Lock()

def _issue_token(username):
    tok = secrets.token_urlsafe(32)
    with TOKENS_LOCK:
        TOKENS[tok] = {"username": username,
                       "issued_at": int(time.time()),
                       "expires_at": int(time.time()) + ACCESS_TTL}
    return tok

def _validate_token(auth_header):
    if not auth_header: return None
    m = re.match(r"Bearer\s+(\S+)", str(auth_header), flags=re.I)
    if not m: return None
    tok = m.group(1)
    with TOKENS_LOCK:
        info = TOKENS.get(tok)
        if not info: return None
        if info["expires_at"] < int(time.time()):
            TOKENS.pop(tok, None)
            return None
        username = info["username"]
    # 账号已被删除时 token 立即失效 (读到损坏文件时不要阻断已登录用户)
    try:
        accounts = loadAccounts()
        if username not in accounts:
            with TOKENS_LOCK:
                TOKENS.pop(tok, None)
            return None
    except DataFileError:
        pass
    return username

def _get_str(body, key, default=""):
    """安全取字符串字段: 任何非字符串类型 (数字 / None / list) 一律返回默认值"""
    v = body.get(key) if isinstance(body, dict) else None
    return v.strip() if isinstance(v, str) else default


class BodyTooLarge(Exception):
    pass


# ============================================================
#  HTTP Handler
# ============================================================
class APIHandler(BaseHTTPRequestHandler):
    server_version = VERSION
    protocol_version = "HTTP/1.1"
    timeout = 15          # 慢连接不能无限占着 handler 线程

    def log_message(self, fmt, *args):
        return

    def _client_allowed(self):
        """--allow-ips 白名单. 旧实现只在 do_POST 里判断, 所有 GET 全部绕过"""
        subnets = getattr(self.server, "cfg", {}).get("allow_subnets")
        if not subnets:
            return True
        try:
            ip_obj = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(ip_obj in n for n in subnets)

    def _vote_allowed(self):
        """投票接口按客户端 IP 限速 (防刷队列)"""
        return _vote_allowed(self.client_address[0])

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        # 安全头
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        self.send_header("Cache-Control", "no-store")
        # CORS — 管理员 API 内部调, 通配只限玩家 API
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-HWID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        if length > MAX_BODY_SIZE:
            # 抛异常交给 do_POST 统一发 413, 否则会先发 413 再发 400 (双响应)
            raise BodyTooLarge()
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except (OSError, TimeoutError):
            self.close_connection = True
            return None
        if not raw: return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_OPTIONS(self):
        self._json(204, {})

    def do_GET(self):
        if not self._client_allowed():
            self._json(403, {"ok": False, "error": "IP not allowed"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/ping":
            self._json(200, {"ok": True, "ver": VERSION,
                             "now": int(time.time())})
            return
        # 网页面板: 浏览器直接打开 http://<服务器IP>:12124/
        if path in ("/", "/index.html", "/panel"):
            return self._serve_panel()
        if path in ("/account", "/register"):
            return self._serve_static("account.html")
        if path == "/api/bng/verify":
            return self._bng_verify()
        if path == "/api/chat/log":
            return self._chat_log()
        if path in ("/api/players/live", "/api/players/online"):
            return self._players_live(getattr(self, "_auth_cache", None))
        if path == "/api/chat/queue":
            return self._chat_queue(getattr(self, "_auth_cache", None))
        if path == "/api/players/names":
            return self._players_names()
        if path == "/api/vote/status":
            return self._vote_status()
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        try:
            return self._do_post_impl()
        except BodyTooLarge:
            return self._json(413, {"ok": False, "error": f"请求体过大 (最大 {MAX_BODY_SIZE} 字节)"})
        except DataFileError as e:
            return self._json(503, {"ok": False, "error": f"数据文件损坏, 已拒绝写入: {e}"})
        except Exception as e:
            try:
                self._json(500, {"ok": False, "error": f"服务器内部错误: {e}"})
            except Exception:
                pass

    def _do_post_impl(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        # 先检查 IP 白名单
        if not self._client_allowed():
            self._json(403, {"ok": False, "error": "IP not allowed"})
            return

        body = self._read_body()
        if body is None:
            self._json(400, {"ok": False, "error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "JSON body must be an object"})
            return

        auth = self.headers.get("Authorization", "")

        # ---- /api/auth/register ----
        if path == "/api/auth/register":
            return self._register(body)
        # ---- /api/auth/login ----
        if path == "/api/auth/login":
            return self._login(body)
        # ---- /api/auth/logout ----
        if path == "/api/auth/logout":
            return self._logout(auth)
        # ---- /api/auth/whoami ----
        if path == "/api/auth/whoami":
            return self._whoami(auth, body)
        # ---- /api/hwid/bind ----
        if path == "/api/hwid/bind":
            return self._bind_hwid(auth, body)
        # ---- /api/vehicle/limit ----
        if path == "/api/vehicle/limit":
            return self._vehicle_limit(auth, body)
        # ---- /api/chat/send  (Bridge 聊天框 → 公屏广播) ----
        if path == "/api/chat/send":
            return self._chat_send(auth, body)
        # ---- /api/chat/queue  (查看当前队列, 调试用) ----
        if path == "/api/chat/queue":
            return self._chat_queue(auth)
        # ---- /api/vote/kick (Bridge 发起投票) ----
        if path == "/api/vote/kick":
            return self._vote_kick(auth, body)
        # ---- /api/vote/cast (Bridge 投票) ----
        if path == "/api/vote/cast":
            return self._vote_cast(auth, body)

        # ---- 网站账号: BNG 官方账号 + 机器码 ----
        if path == "/api/account/register":
            return self._account_register(body)
        if path == "/api/account/login":
            return self._account_login(body)
        if path == "/api/account/login-hwid":
            return self._account_login_hwid(body)

        # ---- 管理员 API ----
        if path.startswith("/api/admin/"):
            return self._admin_route(auth, path, body)

        self._json(404, {"ok": False, "error": "not found: " + path})

    # ============= 业务实现 =============
    def _register(self, body):
        username = _get_str(body, "username")
        password = body.get("password")
        hwid = _get_str(body, "hwid")
        # IP 一律取真实 socket 对端, 不能听客户端自报 (否则可抢占别人的 IP 绑定)
        ip = self.client_address[0]
        player_name = _get_str(body, "player_name")

        if not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
            return self._json(200, {"ok": False, "msg": "账号名不合法 (3-20 字符, 字母/数字/下划线)"})
        if not isinstance(password, str):
            return self._json(200, {"ok": False, "msg": "密码不合法 (4-64 字符)"})
        if len(password) < 4 or len(password) > 64:
            return self._json(200, {"ok": False, "msg": "密码不合法 (4-64 字符)"})
        # 管理员账号名是保留名: 否则任何人都能抢注 --admins 里的名字拿到管理员权限
        if username in ADMINS:
            return self._json(403, {"ok": False,
                                    "msg": "该账号名为管理员保留名, 请先在服务器本机创建账号"})

        with FILE_LOCK:
            accounts = loadAccounts()
            if accounts.get(username):
                return self._json(200, {"ok": False, "msg": f"账号 {username} 已存在"})
            bids = _normalize_beam_ids(hwid, ip, player_name)
            account = {
                "username": username,
                "password_hash": hashPassword(password),
                "register_time": int(time.time()),
                "bind_beam_ids": bids,
                "login_records": [],
            }
            accounts[username] = account
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "msg": "保存失败: " + str(err)})

        token = _issue_token(username)
        return self._json(200, {
            "ok": True,
            "msg": f"注册成功！账号 {username} 已创建, 已自动登录",
            "data": {"username": username, "access_token": token,
                     "bound_ids": bids, "ttl": ACCESS_TTL},
        })

    def _login(self, body):
        username = _get_str(body, "username")
        password = body.get("password")
        hwid = _get_str(body, "hwid")
        # 同上: 用真实对端 IP
        ip = self.client_address[0]
        player_name = _get_str(body, "player_name")
        client_ip = self.client_address[0]

        # 空密码 / 非字符串密码: 直接当密码错误处理 (不能抛异常, 否则不记录限速)
        if not isinstance(password, str) or password == "":
            if username:
                _record_login_attempt(client_ip, username)
            return self._json(200, {"ok": False, "msg": "账号或密码不正确"})

        # ---- 登录速率限制 (防暴力破解) ----
        if username:
            allowed, wait = _check_login_rate(client_ip, username)
            if not allowed:
                return self._json(429, {
                    "ok": False,
                    "msg": f"登录尝试过多, 请 {wait} 秒后再试",
                    "retry_after": wait,
                })

        with FILE_LOCK:
            accounts = loadAccounts()
            acc = accounts.get(username)
            if not acc:
                # 账号不存在也记录 (防止枚举, 但记录登录失败次数)
                if username:
                    _record_login_attempt(client_ip, username)
                return self._json(200, {"ok": False, "msg": "账号或密码不正确"})
            stored_hash = acc.get("password_hash") or ""
            ok_pwd = verifyPassword(password, stored_hash)
            if not ok_pwd:
                # 密码错误 — 记录尝试
                _record_login_attempt(client_ip, username)
                return self._json(200, {"ok": False, "msg": "账号或密码不正确"})

            bids = _normalize_beam_ids(hwid, ip, player_name)
            bound = acc.get("bind_beam_ids") or []
            changed = False
            for b in bids:
                if b and b not in bound:
                    bound.append(b)
                    changed = True
            if changed:
                acc["bind_beam_ids"] = bound
                saveAccounts(accounts)

            # 写登录记录
            records = acc.get("login_records") or []
            records.append({
                "time": int(time.time()),
                "hwid": hwid,
                "ip": ip,
                "name": player_name,
                "via": "HTTP-API",
            })
            acc["login_records"] = records[-100:]
            saveAccounts(accounts)

            # 更新 guestmap
            if bids:
                gm = loadGuestMap()
                for b in bids:
                    gm[b] = {"account": username, "last_seen": int(time.time())}
                saveGuestMap(gm)

        # 登录成功 — 清空速率限制记录
        # (旧代码 keys 是 "ip:username", 却用 `username in LOGIN_RATE` 判断,
        #  条件永远为假 -> 失败次数跨成功登录持续累计, 正常用户也会被锁)
        if username:
            LOGIN_RATE.pop(f"{client_ip}:{username}", None)

        token = _issue_token(username)

        # 管理员登录审计
        if _is_admin(username):
            _audit_log("admin_login", username, detail=f"ip={client_ip} hwid={hwid}")

        return self._json(200, {
            "ok": True,
            "msg": f"登录成功, 欢迎回来 {username}",
            "data": {"username": username,
                     "is_admin": _is_admin(username),
                     "bound_count": len(acc.get("bind_beam_ids") or []),
                     "access_token": token,
                     "ttl": ACCESS_TTL},
        })

    def _logout(self, auth):
        username = _validate_token(auth)
        if not username:
            return self._json(200, {"ok": False, "msg": "未登录"})
        # 撤销当前 token
        with TOKENS_LOCK:
            # 线性搜索该用户的 token
            to_pop = [t for t, info in TOKENS.items() if info["username"] == username]
            for t in to_pop:
                TOKENS.pop(t, None)
        return self._json(200, {"ok": True, "msg": f"{username} 已退出登录"})

    def _whoami(self, auth, body):
        username = _validate_token(auth)
        hwid = (body.get("hwid") or "").strip()
        ip = (body.get("ip") or "").strip()
        player_name = (body.get("player_name") or "").strip()

        if not username:
            # 未登录, 但如果有 hwid 可以帮他查当前绑定情况
            with FILE_LOCK:
                accounts = loadAccounts()
                bids = _normalize_beam_ids(hwid, ip, player_name)
                who = _find_account(accounts, bids)
            return self._json(200, {
                "ok": True,
                "msg": "未登录",
                "data": {"logged": False,
                         "hwid_bound_account": who,
                         "guest_ids": bids,
                         "vehicle_limit": VEHICLE_LIMITS["unauthenticated"],
                         "label": "未认证用户"},
            })

        with FILE_LOCK:
            accounts = loadAccounts()
            acc = accounts.get(username)
            if not acc:
                return self._json(200, {"ok": False, "msg": "账号不存在"})
            bids = acc.get("bind_beam_ids") or []
            records = acc.get("login_records") or []
            last_login = records[-1] if records else None
            reg_time = acc.get("register_time")

        if _is_admin(username):
            label, limit = "管理员", VEHICLE_LIMITS["admin"]
        else:
            if bids:
                label, limit = "认证用户", VEHICLE_LIMITS["authenticated"]
            else:
                label, limit = "未认证用户", VEHICLE_LIMITS["unauthenticated"]

        return self._json(200, {
            "ok": True,
            "msg": f"当前账号: {username} ({label})",
            "data": {"logged": True,
                     "username": username,
                     "is_admin": _is_admin(username),
                     "label": label,
                     "vehicle_limit": limit,
                     "bound_ids": bids,
                     "register_time": reg_time,
                     "last_login": last_login},
        })

    def _bind_hwid(self, auth, body):
        username = _validate_token(auth)
        if not username:
            # 未登录允许先绑定 (和 /bmpid 聊天命令一致), 等玩家登录时自动关联账号
            username = None
        hwid = (body.get("hwid") or "").strip()
        if len(hwid) < 8:
            return self._json(200, {"ok": False, "msg": "hwid 不合法"})
        ip = (body.get("ip") or "").strip()
        player_name = (body.get("player_name") or "").strip()
        bids = _normalize_beam_ids(hwid, ip, player_name)

        with FILE_LOCK:
            accounts = loadAccounts()
            if username:
                acc = accounts.get(username)
                if not acc:
                    return self._json(200, {"ok": False, "msg": "账号不存在"})
                bound = acc.get("bind_beam_ids") or []
                for b in bids:
                    if b and b not in bound:
                        bound.append(b)
                acc["bind_beam_ids"] = bound
                saveAccounts(accounts)
                # guestmap 更新
                gm = loadGuestMap()
                for b in bids:
                    gm[b] = {"account": username, "last_seen": int(time.time())}
                saveGuestMap(gm)
                label, limit = "认证用户", VEHICLE_LIMITS["authenticated"]
            else:
                # 暂存 guestmap 空账号? 存 settings.BMPLoginHWID 标识进 guestmap 不存账号, 但 main.lua 下次 _tryBind 时会用到
                gm = loadGuestMap()
                for b in bids:
                    if b not in gm:
                        gm[b] = {"last_seen": int(time.time()), "via": "HTTP-hwid-only"}
                saveGuestMap(gm)
                label, limit = "未认证用户", VEHICLE_LIMITS["unauthenticated"]

        return self._json(200, {
            "ok": True,
            "msg": f"HWID 已接收, 绑定稳定UUID: {hwid}",
            "data": {"bound_ids": bids, "label": label, "vehicle_limit": limit},
        })

    def _vehicle_limit(self, auth, body):
        username = _validate_token(auth)
        hwid = (body.get("hwid") or "").strip()
        ip = (body.get("ip") or "").strip()
        player_name = (body.get("player_name") or "").strip()
        bids = _normalize_beam_ids(hwid, ip, player_name)

        with FILE_LOCK:
            accounts = loadAccounts()
            if not username:
                username = _find_account(accounts, bids)

            if username and _is_admin(username):
                label, limit = "管理员", VEHICLE_LIMITS["admin"]
            elif username and bids:
                label, limit = "认证用户", VEHICLE_LIMITS["authenticated"]
            elif bids and _find_account(accounts, bids):
                label, limit = "认证用户", VEHICLE_LIMITS["authenticated"]
            else:
                label, limit = "未认证用户", VEHICLE_LIMITS["unauthenticated"]

        return self._json(200, {
            "ok": True,
            "msg": f"您是[{label}], 车辆上限 {limit} 辆",
            "data": {"label": label, "vehicle_limit": limit,
                     "logged": bool(username), "username": username},
        })

    # ============= 聊天 → 公屏广播 =============
    def _chat_send(self, auth, body):
        """
        接收 {player_name, message, hwid?} → 写入 chat_queue.json
        Lua 主插件 onTick 会每秒 poll 这个文件, 读到未发送消息就
        MP.SendChatMessage(-1, "[Bridge] name: text") 广播给所有在线玩家.
        """
        # 1) 取身份: 已登录一律用账号名 (不允许自报名字冒充别人);
        #    未登录只能是 "guest", 不能借用任意 player_name 在公屏上冒充某玩家。
        username = _validate_token(auth)
        claimed = (body.get("player_name") or body.get("name") or "").strip()
        if username:
            name = username
            if claimed and claimed != username:
                _audit_log("chat_name_ignored", username, detail=f"claimed={claimed}")
        else:
            name = "guest"
        if len(name) > 16:
            name = name[:16]
        name = re.sub(r"[\x00-\x1f<>{}\[\]|\\^]+", "_", name).strip() or "guest"

        # 2) 文本检查
        text = (body.get("message") or body.get("text") or "").strip()
        if not text:
            return self._json(200, {"ok": False, "msg": "消息不能为空"})
        if len(text) > 200:
            return self._json(200, {"ok": False, "msg": f"消息太长 ({len(text)}/200)"})
        # 过滤控制字符 + 严重 HTML/JS 注入 (Lua 端再过一次)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

        # 3) 速率限制 (按 name + 客户端 IP 双维度)
        client_ip = self.client_address[0]
        rate_key = f"{name}|{client_ip}"
        now = int(time.time())
        recent = [t for t in CHAT_RATE_LIMIT.get(rate_key, [])
                  if now - t < CHAT_RATE_WINDOW]
        if len(recent) >= CHAT_RATE_MAX:
            wait = CHAT_RATE_WINDOW - (now - min(recent))
            return self._json(200, {
                "ok": False,
                "msg": f"发言太快, 请等 {wait} 秒再试 (限 {CHAT_RATE_MAX}/{CHAT_RATE_WINDOW}s)",
            })
        recent.append(now)
        CHAT_RATE_LIMIT[rate_key] = recent

        # 4) 写入队列 (file lock 防止和 Lua 抢写)
        item = {
            "id": secrets.token_hex(8),
            "ts": now,
            "name": name,
            "text": text,
            "ip": client_ip,
            "username": username,
            "sent": False,
        }
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE)
                if not isinstance(queue, list):
                    queue = []
                queue.append(item)
                # 队列上限 100 (旧的丢掉)
                if len(queue) > 100:
                    queue = queue[-100:]
                ok_w, err_w = writeJsonFile(CHAT_QUEUE_FILE, queue)
                if not ok_w:
                    return self._json(500, {"ok": False, "msg": f"队列写入失败: {err_w}"})
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "队列写入异常: " + str(e)})

        return self._json(200, {
            "ok": True,
            "msg": f"已加入发送队列, 服务器将在 ~1 秒内广播给所有在线玩家",
            "data": {"id": item["id"], "ahead": len([x for x in queue if not x.get("sent")]) - 1},
        })

    def _chat_queue(self, auth):
        """调试用: 查看当前队列"""
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE) or []
        except Exception:
            queue = []
        unsent = [x for x in queue if not x.get("sent")] if isinstance(queue, list) else []
        return self._json(200, {
            "ok": True,
            "data": {
                "total": len(queue) if isinstance(queue, list) else 0,
                "unsent": len(unsent),
                "items": queue[-20:] if isinstance(queue, list) else [],
            },
        })

    # ============= 管理员 API =============
    def _admin_route(self, auth, path, body):
        """管理员路由分发 — 所有 /api/admin/* 端点"""
        username = _validate_token(auth)
        if not username:
            return self._json(401, {"ok": False, "error": "未登录或 token 已过期"})
        if not _is_admin(username):
            return self._json(403, {"ok": False, "error": "非管理员, 无权访问"})
        client_ip = self.client_address[0]

        # 路由表
        if path == "/api/admin/accounts":
            return self._admin_list_accounts()
        if path == "/api/admin/accounts/delete":
            _audit_log("admin_delete_account", username,
                       target=(body or {}).get("username", ""),
                       detail=f"ip={client_ip}")
            return self._admin_delete_account(body)
        if path == "/api/admin/accounts/reset-password":
            _audit_log("admin_reset_password", username,
                       target=(body or {}).get("username", ""),
                       detail=f"ip={client_ip}")
            return self._admin_reset_password(body)
        if path == "/api/admin/accounts/toggle-admin":
            is_admin = (body or {}).get("is_admin", False)
            _audit_log("admin_toggle_admin", username,
                       target=(body or {}).get("username", ""),
                       detail=f"set={is_admin} ip={client_ip}")
            return self._admin_toggle_admin(body)
        if path == "/api/admin/accounts/unbind":
            _audit_log("admin_unbind", username,
                       target=(body or {}).get("username", ""),
                       detail=f"beam_id={(body or {}).get('beam_id','')} ip={client_ip}")
            return self._admin_unbind(body)
        if path == "/api/admin/ban":
            _audit_log("admin_ban", username,
                       target=f"{(body or {}).get('type','')}:{(body or {}).get('value','')}",
                       detail=f"reason={(body or {}).get('reason','')} ip={client_ip}")
            return self._admin_ban(body)
        if path == "/api/admin/unban":
            _audit_log("admin_unban", username,
                       target=f"{(body or {}).get('type','')}:{(body or {}).get('value','')}",
                       detail=f"ip={client_ip}")
            return self._admin_unban(body)
        if path == "/api/admin/banlist":
            return self._admin_banlist()
        if path == "/api/admin/players":
            return self._admin_players()
        if path == "/api/admin/kick":
            _audit_log("admin_kick", username,
                       target=(body or {}).get("playerID", ""),
                       detail=f"reason={(body or {}).get('reason', '')[:100]} ip={client_ip}")
            return self._admin_kick(body)
        if path == "/api/admin/chat-queue":
            return self._admin_chat_queue()
        if path == "/api/admin/chat-queue/send":
            _audit_log("admin_chat_send", username,
                       target=(body or {}).get("player_name", ""),
                       detail=f"msg_len={len((body or {}).get('message',''))} ip={client_ip}")
            return self._admin_chat_send(body)
        if path == "/api/admin/chat-queue/clear":
            only_sent = (body or {}).get("only_sent", False)
            _audit_log("admin_chat_clear", username,
                       target="",
                       detail=f"only_sent={only_sent} ip={client_ip}")
            return self._admin_chat_clear(body)
        if path == "/api/admin/stats":
            return self._admin_stats()

        return self._json(404, {"ok": False, "error": "admin endpoint not found: " + path})

    # ---- 账号列表 (GUEST 精简) ----
    def _admin_list_accounts(self):
        """列出所有账号 (脱敏密码 hash, GUEST 条目精简)"""
        with FILE_LOCK:
            accounts = loadAccounts()
        result = []
        for uname, acc in sorted(accounts.items()):
            ph = acc.get("password_hash", "") or ""
            ph_short = ph[:16] + "..." if len(ph) > 16 else ph
            all_bids = _ensure_list(acc.get("bind_beam_ids"))
            clean_bids = _deduplicate_bids(all_bids)
            result.append({
                "username": uname,
                "is_admin": _is_admin(uname),
                "bind_beam_ids": clean_bids,
                "bind_beam_ids_full": all_bids,
                "login_records_count": len(_ensure_list(acc.get("login_records"))),
                "last_login": (_ensure_list(acc.get("login_records"))[-1].get("time")
                              if _ensure_list(acc.get("login_records")) else None),
                "password_hash_short": ph_short,
                "register_time": acc.get("register_time"),
            })
        return self._json(200, {"ok": True, "data": result})

    def _admin_delete_account(self, body):
        target = _get_str(body, "username")
        if not target:
            return self._json(400, {"ok": False, "error": "缺少 username"})
        with FILE_LOCK:
            accounts = loadAccounts()
            # 用 _account_is_admin 而不是 `target in ADMINS`:
            # is_admin 是持久化字段, 重启后 ADMINS 里看不到, 旧写法会允许删除管理员
            if _account_is_admin(accounts, target):
                return self._json(400, {"ok": False, "error": f"不能删除管理员 {target}"})
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            del accounts[target]
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "error": f"accounts 写入失败: {err}"})
            # 同步清理 guestmap 里指向该账号的条目
            gm = loadGuestMap()
            cleaned = {k: v for k, v in gm.items()
                      if not (isinstance(v, dict) and v.get("account") == target)}
            saveGuestMap(cleaned)
        _revoke_user_tokens(target)
        return self._json(200, {"ok": True, "msg": f"账号 {target} 已删除"})

    def _admin_reset_password(self, body):
        target = _get_str(body, "username")
        new_pw = body.get("new_password")
        if not target or not isinstance(new_pw, str) or not new_pw:
            return self._json(400, {"ok": False, "error": "需要 username 和 new_password"})
        if len(new_pw) < 4:
            return self._json(400, {"ok": False, "error": "密码至少 4 位"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            accounts[target]["password_hash"] = hashPassword(new_pw)
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "error": f"accounts 写入失败: {err}"})
        _revoke_user_tokens(target)
        return self._json(200, {"ok": True, "msg": f"账号 {target} 密码已重置"})

    def _admin_toggle_admin(self, body):
        target = _get_str(body, "username")
        set_admin = bool(body.get("is_admin", False))
        if not target:
            return self._json(400, {"ok": False, "error": "缺少 username"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            # 持久化 is_admin 到 accounts.json (供 Lua 插件读取)
            accounts[target]["is_admin"] = set_admin
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "error": f"accounts 写入失败: {err}"})
            if set_admin:
                ADMINS.add(target)
            else:
                ADMINS.discard(target)
        if not set_admin:
            _revoke_user_tokens(target)
        return self._json(200, {"ok": True, "msg": f"{target} 管理员权限: {'已开启' if set_admin else '已关闭'}",
                               "data": {"is_admin": set_admin, "admins": sorted(ADMINS)}})

    def _admin_unbind(self, body):
        target = _get_str(body, "username")
        beam_id = _get_str(body, "beam_id")
        if not target or not beam_id:
            return self._json(400, {"ok": False, "error": "需要 username 和 beam_id"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            bids = _ensure_list(accounts[target].get("bind_beam_ids"))
            if beam_id not in bids:
                return self._json(404, {"ok": False, "error": f"绑定 {beam_id} 不存在"})
            bids.remove(beam_id)
            accounts[target]["bind_beam_ids"] = bids
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "error": f"accounts 写入失败: {err}"})
            # 同步清理 guestmap
            gm = loadGuestMap()
            if beam_id in gm:
                del gm[beam_id]
                saveGuestMap(gm)
        return self._json(200, {"ok": True, "msg": f"已解除 {target} 的绑定 {beam_id}"})

    def _admin_ban(self, body):
        target_type = _get_str(body, "type")  # account / hwid / ip / name
        target_val = _get_str(body, "value")
        reason = _get_str(body, "reason")
        try:
            duration_days = int(body.get("duration_days") or 0)  # 0 = 永久
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "duration_days 必须是整数"})
        if not target_type or not target_val:
            return self._json(400, {"ok": False, "error": "需要 type(account/hwid/ip/name) 和 value"})
        if target_type not in ("account", "hwid", "ip", "name"):
            return self._json(400, {"ok": False, "error": "type 必须是 account/hwid/ip/name"})

        now = int(time.time())
        expires_at = now + duration_days * 86400 if duration_days > 0 else 0

        with FILE_LOCK:
            banlist = loadBanlist()
            # 按类型存到对应 key; 同时写 value + Lua 兼容字段名
            if target_type in ("account", "name"):
                key = "accounts"
                entry = {"account": target_val, "value": target_val,
                         "reason": reason, "time": now,
                         "duration_days": duration_days, "expires_at": expires_at}
            else:
                key = "devices"
                if target_type == "hwid":
                    entry = {"device_id": target_val, "value": target_val,
                             "reason": reason, "time": now,
                             "duration_days": duration_days, "expires_at": expires_at}
                else:  # ip
                    entry = {"ip": target_val, "value": target_val,
                             "reason": reason, "time": now,
                             "duration_days": duration_days, "expires_at": expires_at}
            banlist[key] = _ensure_list(banlist.get(key))
            # 避免重复 (删旧的)
            existing_values = [e.get("value") if isinstance(e, dict) else e
                               for e in banlist[key]]
            banlist[key] = [e for e in banlist[key]
                           if (e.get("value") if isinstance(e, dict) else e) != target_val]
            banlist[key].append(entry)
            writeJsonFile(BANLIST_FILE, banlist)

        dur_text = f"{duration_days} 天" if duration_days > 0 else "永久"
        return self._json(200, {"ok": True,
                                "msg": f"已封禁 {target_type}:{target_val} ({dur_text})"})

    def _admin_unban(self, body):
        target_type = (body.get("type") or "").strip()
        target_val = (body.get("value") or "").strip()
        if not target_type or not target_val:
            return self._json(400, {"ok": False, "error": "需要 type 和 value"})
        with FILE_LOCK:
            banlist = loadBanlist()
            key = "accounts" if target_type in ("account", "name") else "devices"
            items = _ensure_list(banlist.get(key))
            new_items = [e for e in items
                        if (e.get("value") if isinstance(e, dict) else e) != target_val]
            banlist[key] = new_items
            writeJsonFile(BANLIST_FILE, banlist)
        return self._json(200, {"ok": True, "msg": f"已解封 {target_type}:{target_val}"})

    def _admin_banlist(self):
        now = int(time.time())
        with FILE_LOCK:
            banlist = loadBanlist()

        def _enrich(items, kind):
            result = []
            for e in _ensure_list(items):
                if not isinstance(e, dict):
                    continue
                exp = e.get("expires_at", 0) or 0
                expired = exp > 0 and exp <= now
                remaining = 0
                if exp > 0 and not expired:
                    remaining = exp - now
                val = e.get("value") or e.get("account") or e.get("device_id") or e.get("ip", "")
                result.append({
                    "type": kind,
                    "value": val,
                    "reason": e.get("reason", ""),
                    "time": e.get("time", 0),
                    "duration_days": e.get("duration_days", 0),
                    "expires_at": exp,
                    "expired": expired,
                    "remaining_seconds": remaining,
                })
            return result

        all_bans = (_enrich(banlist.get("accounts"), "account") +
                    _enrich(banlist.get("devices"), "hwid/ip"))
        return self._json(200, {"ok": True, "data": all_bans})

    def _admin_players(self):
        """Return online players list (read from online_players.json)"""
        try:
            with FILE_LOCK:
                data = readJsonFile(ONLINE_FILE) or []
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        return self._json(200, {"ok": True, "data": data})

    # ============= 网页面板 =============
    def _serve_panel(self):
        """把 panel.html 原样返回 (与 BMP_HTTP_API.py 放在同一目录)."""
        return self._serve_static("panel.html")

    def _serve_static(self, name):
        here = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(here, name)
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            self._json(404, {"ok": False,
                             "error": f"找不到 {name}, 请把它和 BMP_HTTP_API.py 放在同一目录"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ============= BNG 账号: 绑定证据 / 注册 / 登录 =============
    def _load_bindings(self):
        """读 Lua 写的 bindings.json: { hwid: {beammp_id, player_name, first_seen, last_seen, seen} }"""
        try:
            with FILE_LOCK:
                data = readJsonFile(BINDINGS_FILE)
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    def _online_rows(self):
        try:
            with FILE_LOCK:
                data = readJsonFile(ONLINE_FILE)
        except Exception:
            data = None
        return data if isinstance(data, list) else []

    def _online_hwid_map(self):
        """当前在线玩家的 hwid -> 在线条目 (用于"本人在线"验证)"""
        out = {}
        for p in self._online_rows():
            if isinstance(p, dict):
                h = (p.get("hwid") or "").strip().lower()
                if h:
                    out[h] = p
        return out

    def _bng_verify(self):
        """GET /api/bng/verify?hwid=xxx

        返回这个机器码在服务器上被观察到与哪个 BeamMP 官方账号同时在线过。
        网页注册页用它来显示"已检测到你的账号 ✓"。
        """
        qs = parse_qs(urlparse(self.path).query)
        hwid = (qs.get("hwid", [""])[0] or "").strip().lower()
        if not hwid:
            return self._json(400, {"ok": False, "error": "缺少 hwid 参数"})

        bindings = self._load_bindings()
        rec = bindings.get(hwid)
        online = self._online_hwid_map().get(hwid)

        if not isinstance(rec, dict) and not online:
            return self._json(200, {"ok": True, "data": {
                "bound": False, "online": False,
                "msg": "服务器还没见过这个机器码。请先进一次服务器（保持 BMPHWID_Bridge 运行），再回来注册。",
            }})

        rec = rec if isinstance(rec, dict) else {}
        data = {
            "bound": bool(rec.get("beammp_id")),
            "online": online is not None,
            "beammp_id": rec.get("beammp_id", ""),
            "player_name": rec.get("player_name", "") or (online or {}).get("name", ""),
            "first_seen": rec.get("first_seen"),
            "last_seen": rec.get("last_seen"),
            "seen": rec.get("seen", 0),
        }
        if not data["beammp_id"]:
            data["msg"] = "看到过这个机器码，但当时是游客（没登录官方账号）。请在 BeamMP 里登录官方账号后再进一次服务器。"
        return self._json(200, {"ok": True, "data": data})

    def _find_account_by_hwid(self, accounts, hwid):
        hwid = (hwid or "").lower()
        for uname, acc in accounts.items():
            if isinstance(acc, dict) and (acc.get("hwid") or "").lower() == hwid:
                return uname
        return None

    def _account_register(self, body):
        """POST /api/account/register {username, password, hwid}

        和老的 /api/auth/register 的区别: 这里的账号必须绑定一个**服务器亲眼见过**
        的 (机器码 ↔ 官方账号) 组合, 否则拒绝 —— 防止随便编个账号来蹭授信。
        """
        username = _get_str(body, "username")
        password = body.get("password")
        hwid = _get_str(body, "hwid").lower()

        if not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
            return self._json(200, {"ok": False, "msg": "账号名不合法 (3-20 字符, 字母/数字/下划线)"})
        if not isinstance(password, str) or not (4 <= len(password) <= 64):
            return self._json(200, {"ok": False, "msg": "密码不合法 (4-64 字符)"})
        if not hwid:
            return self._json(400, {"ok": False, "msg": "缺少机器码 (请先启动 BMPHWID_Bridge)"})

        bindings = self._load_bindings()
        rec = bindings.get(hwid)
        if not isinstance(rec, dict) or not rec.get("beammp_id"):
            return self._json(403, {"ok": False,
                                    "msg": "服务器还没见过这个机器码与官方账号一起在线。"
                                           "请先在游戏里登录 BeamMP 官方账号并进一次服务器。",
                                    "data": {"hwid": hwid, "need_ingame": True}})
        beammp_id = rec["beammp_id"]

        with FILE_LOCK:
            accounts = loadAccounts()
            if accounts.get(username):
                return self._json(200, {"ok": False, "msg": f"账号 {username} 已存在"})
            owner = self._find_account_by_hwid(accounts, hwid)
            if owner and owner != username:
                return self._json(409, {"ok": False,
                                        "msg": f"这个机器码已经绑定了账号 {owner}"})
            acc = {
                "username": username,
                "password_hash": hashPassword(password),
                "register_time": int(time.time()),
                "bind_beam_ids": [],
                "login_records": [],
                "hwid": hwid,
                "beammp_id": beammp_id,
                "beammp_name": rec.get("player_name", ""),
                "verified": True,
                "verified_at": int(time.time()),
                "via": "web-bng",
            }
            accounts[username] = acc
            ok, err = saveAccounts(accounts)
            if not ok:
                return self._json(500, {"ok": False, "msg": f"保存失败: {err}"})

        token = _issue_token(username)
        _audit_log("account_register", username, target=username,
                   detail=f"beammp_id={beammp_id} hwid={hwid[:8]}…")
        return self._json(200, {
            "ok": True,
            "msg": f"注册成功！已绑定 BeamMP 官方账号 {beammp_id[:12]}…",
            "data": {"username": username, "access_token": token, "ttl": ACCESS_TTL,
                     "beammp_id": beammp_id, "hwid": hwid, "verified": True},
        })

    def _account_login(self, body):
        """POST /api/account/login {username, password} —— 普通密码登录"""
        username = _get_str(body, "username")
        password = body.get("password")
        if not username or not isinstance(password, str) or not password:
            return self._json(200, {"ok": False, "msg": "账号或密码不正确"})

        client_ip = self.client_address[0]
        allowed, wait = _check_login_rate(client_ip, username)
        if not allowed:
            return self._json(429, {"ok": False, "msg": f"登录尝试过多, 请 {wait} 秒后再试"})

        with FILE_LOCK:
            accounts = loadAccounts()
            acc = accounts.get(username)
            if not isinstance(acc, dict) or not verifyPassword(password, acc.get("password_hash") or ""):
                _record_login_attempt(client_ip, username)
                return self._json(200, {"ok": False, "msg": "账号或密码不正确"})
        LOGIN_RATE.pop(f"{client_ip}:{username}", None)

        token = _issue_token(username)
        return self._json(200, {
            "ok": True,
            "msg": f"登录成功, 欢迎回来 {username}",
            "data": {"username": username, "access_token": token, "ttl": ACCESS_TTL,
                     "verified": bool(acc.get("verified")),
                     "beammp_id": acc.get("beammp_id", ""),
                     "hwid": acc.get("hwid", ""),
                     "is_admin": _is_admin(username)},
        })

    def _account_login_hwid(self, body):
        """POST /api/account/login-hwid {hwid} —— 一键登录

        双重条件, 两个都必须满足:
          1. 这个机器码绑定过某个账号 (注册时验证过)
          2. 这个机器码**此刻就在服务器里在线**
        所以就算别人抄到了机器码 (它在面板上是公开的), 只要他人的机器不在线,
        也登不进来 —— "本人在线" 才是凭证。
        """
        hwid = _get_str(body, "hwid").lower()
        if not hwid:
            return self._json(400, {"ok": False, "msg": "缺少机器码"})

        online = self._online_hwid_map()
        row = online.get(hwid)
        if not row:
            return self._json(403, {"ok": False,
                                    "msg": "这个机器码当前不在线。请先进服务器（保持 BMPHWID_Bridge 运行）再试。",
                                    "data": {"need_ingame": True}})

        with FILE_LOCK:
            accounts = loadAccounts()
            username = self._find_account_by_hwid(accounts, hwid)
            if not username:
                return self._json(404, {"ok": False, "msg": "这个机器码还没有注册账号, 请先注册"})
            acc = accounts[username]

        token = _issue_token(username)
        return self._json(200, {
            "ok": True,
            "msg": f"已验证本人在线, 登录为 {username}",
            "data": {"username": username, "access_token": token, "ttl": ACCESS_TTL,
                     "verified": bool(acc.get("verified")),
                     "beammp_id": acc.get("beammp_id", ""),
                     "player_name": row.get("name", ""),
                     "is_admin": _is_admin(username)},
        })

    def _chat_log(self):
        """GET /api/chat/log?limit=50 —— 面板聊天记录 (最近 N 条, 含已发送的)"""
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = max(1, min(200, int(qs.get("limit", ["50"])[0])))
        except Exception:
            limit = 50
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE)
        except Exception:
            queue = None
        if not isinstance(queue, list):
            queue = []
        items = [x for x in queue if isinstance(x, dict)][-limit:]
        return self._json(200, {"ok": True, "data": {
            "items": [{"name": i.get("name", ""), "text": i.get("text", ""),
                       "ts": i.get("ts"), "sent": bool(i.get("sent")),
                       "username": i.get("username", "")} for i in items],
            "server_time": int(time.time()),
        }})

    def _players_live(self, auth):
        """网页面板的数据源: 直接读 Lua 写的 online_players.json.

        Lua 在「玩家进服 / 离服 / 每 3 秒」时刷新这个文件。
        人数一律以这个列表的长度为准, 不做任何增量计算 ——
        这样面板上的数字和游戏里广播的"当前在线 X 人"永远是同一个。
        """
        updated_at = None
        try:
            with FILE_LOCK:
                raw = readJsonFile(ONLINE_FILE)
                if os.path.exists(ONLINE_FILE):
                    updated_at = int(os.path.getmtime(ONLINE_FILE))
        except Exception:
            raw = None
        if not isinstance(raw, list):
            raw = []

        players = []
        now = int(time.time())
        for p in raw:
            if not isinstance(p, dict):
                continue
            join_ts = int(p.get("join_time") or now)
            players.append({
                "playerID": p.get("playerID"),
                "name": p.get("name", ""),
                "role": p.get("role", ""),
                "is_authenticated": bool(p.get("is_authenticated")),
                "trusted": bool(p.get("trusted", p.get("is_authenticated"))),
                "is_admin": bool(p.get("is_admin")),
                "beammp_id": p.get("beammp_id", ""),
                "bind_account": p.get("bind_account", ""),
                "hwid": p.get("hwid", ""),
                "vehicle_count": p.get("vehicle_count", 0),
                "join_time": join_ts,
                "online_seconds": max(0, now - join_ts),
            })

        players.sort(key=lambda x: x.get("join_time") or 0)   # 先进服的排前面

        counts = {
            "total": len(players),
            "trusted": sum(1 for p in players if p["trusted"]),
            "untrusted": sum(1 for p in players if not p["trusted"]),
            "admin": sum(1 for p in players if p["is_admin"]),
        }
        # 文件 30 秒没更新 -> Lua 插件没在跑 / 服务器没启动
        stale = (updated_at is None) or (now - updated_at > 30)

        return self._json(200, {
            "ok": True,
            "data": {
                "count": counts["total"],
                "counts": counts,
                "players": players,
                "updated_at": updated_at,
                "stale": stale,
                "server_time": now,
            },
        })

    def _players_names(self):
        try:
            with FILE_LOCK:
                data = readJsonFile(ONLINE_FILE) or []
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        names = [p.get("name", "") for p in data if isinstance(p, dict) and p.get("name")]
        return self._json(200, {"ok": True, "data": names})

    def _vote_kick(self, auth, body):
        """Bridge 发起投票踢出 → 写入 vote_queue.json, Lua 轮询处理.
        必须登录: Lua 端是按玩家名匹配 onlinePlayers 的, 匿名调用等于可以
        随便冒充任意在线玩家发起投票。"""
        username = _validate_token(auth)
        if not username:
            return self._json(401, {"ok": False, "error": "请先登录再发起投票"})
        if not self._vote_allowed():
            return self._json(429, {"ok": False, "error": "投票请求过于频繁, 请稍后再试"})
        target_name = _get_str(body, "target_name")
        reason = _get_str(body, "reason")[:200]
        if not target_name:
            return self._json(400, {"ok": False, "error": "需要 target_name (目标玩家名)"})

        now = int(time.time())
        entry = {
            "action": "kick",
            "target_name": target_name,
            "initiator_name": username,      # 永远用登录账号, 不采信 body
            "reason": reason,
            "timestamp": now,
            "processed": False,
        }
        with FILE_LOCK:
            queue = readJsonFile(VOTE_QUEUE_FILE)
            if not isinstance(queue, list):
                queue = []
            # 队列上限: 防止有人刷爆文件
            pending = [e for e in queue if isinstance(e, dict) and not e.get("processed")]
            if len(pending) >= 20:
                return self._json(429, {"ok": False, "error": "投票队列已满, 请稍后再试"})
            queue.append(entry)
            ok, err = writeJsonFile(VOTE_QUEUE_FILE, queue)
            if not ok:
                return self._json(500, {"ok": False, "error": f"写入失败: {err}"})

        _audit_log("vote_kick", username, target=target_name, detail=reason)
        return self._json(200, {"ok": True, "msg": f"已发起对 {target_name} 的投票"})

    def _vote_cast(self, auth, body):
        """Bridge 参与投票 → 写入 vote_queue.json, Lua 轮询处理 (同样必须登录)"""
        username = _validate_token(auth)
        if not username:
            return self._json(401, {"ok": False, "error": "请先登录再投票"})
        if not self._vote_allowed():
            return self._json(429, {"ok": False, "error": "投票请求过于频繁, 请稍后再试"})
        vote = _get_str(body, "vote").lower()
        if vote not in ("yes", "y", "赞成", "no", "n", "反对"):
            return self._json(400, {"ok": False, "error": "vote 必须是 yes/no"})

        now = int(time.time())
        entry = {
            "action": "vote",
            "voter_name": username,          # 永远用登录账号
            "vote": vote,
            "timestamp": now,
            "processed": False,
        }
        with FILE_LOCK:
            queue = readJsonFile(VOTE_QUEUE_FILE)
            if not isinstance(queue, list):
                queue = []
            queue.append(entry)
            ok, err = writeJsonFile(VOTE_QUEUE_FILE, queue)
            if not ok:
                return self._json(500, {"ok": False, "error": f"写入失败: {err}"})

        return self._json(200, {"ok": True, "msg": f"已记录 {username} 的投票"})

    def _vote_status(self):
        """Bridge 查询当前投票状态"""
        try:
            with FILE_LOCK:
                data = readJsonFile(VOTE_QUEUE_FILE) or []
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        # 只返回最新的 kick 条目 (如果还未处理)
        pending_kick = None
        for e in reversed(data):
            if isinstance(e, dict) and e.get("action") == "kick" and not e.get("processed"):
                pending_kick = e
                break
        return self._json(200, {"ok": True, "pending_kick": pending_kick})

    def _admin_kick(self, body):
        """Add a player to the kick queue (Lua polls every 3s and drops them)"""
        player_id = body.get("playerID", body.get("player_id", body.get("pid")))
        reason = (body.get("reason") or "管理员踢出").strip()
        if player_id is None:
            return self._json(400, {"ok": False, "error": "需要 playerID"})
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "playerID 必须是整数"})

        now = int(time.time())
        kick_entry = {
            "id": str(uuid.uuid4()),
            "playerID": player_id,
            "reason": reason,
            "time": now,
            "done": False,
        }
        with FILE_LOCK:
            queue = readJsonFile(KICK_QUEUE_FILE) or []
            if not isinstance(queue, list):
                queue = []
            queue.append(kick_entry)
            writeJsonFile(KICK_QUEUE_FILE, queue)

        return self._json(200, {"ok": True,
                                "msg": f"已添加踢人队列 #{player_id} (原因: {reason}), 3 秒内生效"})

    def _admin_chat_queue(self):
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE) or []
        except Exception:
            queue = []
        items = queue if isinstance(queue, list) else []
        return self._json(200, {
            "ok": True,
            "data": {
                "total": len(items),
                "unsent": len([x for x in items if not x.get("sent")]),
                "items": items,
            },
        })

    def _admin_chat_send(self, body):
        """管理员以 [Admin] 前缀广播消息"""
        msg = (body.get("message") or "").strip()
        name = (body.get("player_name") or "管理员").strip()
        if not msg:
            return self._json(400, {"ok": False, "error": "message 不能为空"})
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE) or []
                if not isinstance(queue, list):
                    queue = []
                item = {
                    "id": secrets.token_hex(8),
                    "ts": int(time.time()),
                    "name": f"[Admin] {name}",
                    "text": msg,
                    "ip": "",
                    "username": "admin",
                    "sent": False,
                }
                queue.append(item)
                if len(queue) > 200:
                    queue = queue[-200:]
                writeJsonFile(CHAT_QUEUE_FILE, queue)
        except Exception as e:
            return self._json(500, {"ok": False, "error": "队列写入异常: " + str(e)})
        return self._json(200, {"ok": True, "msg": "管理员消息已加入广播队列",
                               "data": {"id": item["id"]}})

    def _admin_chat_clear(self, body):
        """清空聊天队列 (可选择只清已发送)"""
        only_sent = bool(body.get("only_sent", False))
        try:
            with FILE_LOCK:
                queue = readJsonFile(CHAT_QUEUE_FILE) or []
                if only_sent:
                    queue = [x for x in queue if not x.get("sent")]
                else:
                    queue = []
                writeJsonFile(CHAT_QUEUE_FILE, queue)
        except Exception as e:
            return self._json(500, {"ok": False, "error": "清空异常: " + str(e)})
        return self._json(200, {"ok": True, "msg": f"已清空{'已发送消息' if only_sent else '全部队列'}"})

    def _admin_stats(self):
        """服务器统计信息"""
        with FILE_LOCK:
            accounts = loadAccounts()
            gm = loadGuestMap()
            try:
                banlist = loadBanlist()
            except Exception:
                banlist = {}
        total_accounts = len(accounts)
        admin_count = sum(1 for u in accounts if _is_admin(u))
        auth_count = sum(1 for u, acc in accounts.items()
                        if any(b.startswith(("HWID:", "IP:", "NAME:"))
                               for b in _ensure_list(acc.get("bind_beam_ids"))))
        # 最近 24h 活跃账号
        now = int(time.time())
        active_24h = 0
        for u, acc in accounts.items():
            recs = _ensure_list(acc.get("login_records"))
            if recs and recs[-1].get("time", 0) > now - 86400:
                active_24h += 1
        # 聊天队列
        try:
            queue = readJsonFile(CHAT_QUEUE_FILE) or []
            queue_total = len(queue) if isinstance(queue, list) else 0
            queue_unsent = len([x for x in (queue if isinstance(queue, list) else []) if not x.get("sent")])
        except Exception:
            queue_total = 0
            queue_unsent = 0
        return self._json(200, {
            "ok": True,
            "data": {
                "total_accounts": total_accounts,
                "admin_count": admin_count,
                "authenticated_count": auth_count,
                "active_24h": active_24h,
                "guestmap_entries": len(gm),
                "ban_accounts": len(_ensure_list(banlist.get("accounts"))),
                "ban_devices": len(_ensure_list(banlist.get("devices"))),
                "chat_queue_total": queue_total,
                "chat_queue_unsent": queue_unsent,
                "admins": sorted(ADMINS),
                "uptime_since": getattr(self.server, "start_time", None),
            },
        })


def _run_server(host, port, cfg):
    # ThreadingHTTPServer: 旧版 HTTPServer 单线程, 一个慢连接就能卡死整个 API
    httpd = ThreadingHTTPServer((host, port), APIHandler)
    httpd.daemon_threads = True
    httpd.cfg = cfg
    httpd.start_time = int(time.time())

    # ---- TLS/HTTPS 支持 ----
    tls_cert = cfg.get("tls_cert")
    tls_key = cfg.get("tls_key")
    if tls_cert and tls_key:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            print(f"[{VERSION}] 监听 {host}:{port}  (HTTPS/TLS)")
        except Exception as e:
            # 必须 fail-closed: 用户显式要求 HTTPS 却静默降级成明文,
            # 会让所有玩家密码/token 在公网明文传输
            print(f"[FATAL] TLS 启动失败, 已中止: {e}")
            try:
                httpd.server_close()
            except Exception:
                pass
            raise SystemExit(1)
    else:
        print(f"[{VERSION}] 监听 {host}:{port}")
        print("  [警告] 未启用 TLS — 公网部署建议配置 --tls-cert / --tls-key")

    print(f"  数据目录: {DATA_DIR}")
    print(f"  管理员名单: {sorted(ADMINS) if ADMINS else '(空)'}")
    if cfg.get("allow_subnets"):
        print(f"  IP 白名单: {[str(n) for n in cfg['allow_subnets']]}")
    if tls_cert and tls_key:
        print(f"  TLS 证书: {tls_cert}")
    print("  玩家 API:  POST /api/auth/{register,login,logout,whoami}")
    print("             POST /api/hwid/bind  /api/vehicle/limit")
    print("             POST /api/chat/send   GET  /api/ping")
    print("  管理员 API: POST /api/admin/accounts (列表/删除/重置密码/设管理员/解绑)")
    print("             POST /api/admin/ban /unban /banlist")
    print("             POST /api/admin/chat-queue/send /clear  GET /api/admin/stats")
    print("  审计日志:  bmp_login/admin_audit.jsonl")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    # stdout 被重定向 (Windows 服务 / 计划任务 / > api.log) 时, Python 会用本地
    # 编码 (GBK/cp1252) 打印中文和 emoji 直接 UnicodeEncodeError, 进程起不来
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    global DATA_DIR, ACCOUNTS_FILE, GUESTMAP_FILE, BANLIST_FILE, ADMINS
    global CHAT_QUEUE_FILE, ONLINE_FILE, KICK_QUEUE_FILE, VOTE_QUEUE_FILE, ADMIN_AUDIT_FILE
    p = argparse.ArgumentParser(description="BMPLogin HTTP API 服务器 (Bridge.exe 直连用)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    p.add_argument("--data-dir", default=DATA_DIR, help="bmp_login 目录路径 (与 BeamMP 共享)")
    p.add_argument("--allow-ips", default="",
                   help="逗号分隔的 IP/CIDR, 不填=允许所有. 例: 127.0.0.1,192.168.0.0/24,110.x.x.x/32")
    p.add_argument("--admins", default="",
                   help="逗号分隔管理员账号, 例: DRIFTKING,seeyou   (车辆 999 辆上限)")
    p.add_argument("--tls-cert", default="",
                   help="TLS 证书文件 (PEM), 启用 HTTPS")
    p.add_argument("--tls-key", default="",
                   help="TLS 私钥文件 (PEM), 启用 HTTPS")
    args = p.parse_args()

    if args.data_dir:
        # 统一改全部路径 (含 chat_queue.json / admin_audit.jsonl)
        _set_data_dir(args.data_dir)

    if args.admins:
        ADMINS = set(a.strip() for a in args.admins.split(",") if a.strip())
        # 把 --admins 同步进 accounts.json 的 is_admin 字段,
        # 否则 Lua 插件 (只认 accounts.json) 不认这些管理员, HTTP 与游戏内权限不一致
        try:
            _accounts = loadAccounts()
            _missing = []
            _changed = False
            for _a in sorted(ADMINS):
                if isinstance(_accounts.get(_a), dict):
                    if not _accounts[_a].get("is_admin"):
                        _accounts[_a]["is_admin"] = True
                        _changed = True
                else:
                    _missing.append(_a)
            if _changed:
                _ok, _err = saveAccounts(_accounts)
                if not _ok:
                    print(f"[WARN] 同步 --admins 到 accounts.json 失败: {_err}")
            if _missing:
                print(f"[WARN] 以下管理员账号尚未注册, 请先在服务器本机创建: {_missing}")
        except DataFileError as e:
            print(f"[FATAL] accounts.json 无法解析, 已中止: {e}")
            raise SystemExit(1)

    # 解析 --allow-ips
    allow_subnets = None
    if args.allow_ips and args.allow_ips.strip():
        allow_subnets = []
        for tok in args.allow_ips.split(","):
            tok = tok.strip()
            if not tok: continue
            try:
                _net = ipaddress.ip_network(tok, strict=False) if "/" in tok else None
                if _net is None:
                    _addr = ipaddress.ip_address(tok)
                    # IPv4 补 /32, IPv6 补 /128 (旧代码一律 /32, 把 ::1 变成 ::1/32)
                    _net = ipaddress.ip_network(("%s/%d" % (tok, 32 if _addr.version == 4 else 128)),
                                                strict=False)
                allow_subnets.append(_net)
            except Exception as e:
                print(f"[WARN] 跳过无效的白名单条目 {tok}: {e}")

    cfg = {"allow_subnets": allow_subnets}
    if args.tls_cert and args.tls_key:
        cfg["tls_cert"] = os.path.abspath(args.tls_cert)
        cfg["tls_key"] = os.path.abspath(args.tls_key)
    _run_server(args.host, args.port, cfg)


if __name__ == "__main__":
    main()
