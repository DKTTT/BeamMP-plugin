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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 共享路径 — 和 main.lua 一致
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BMP_DATA_DIR") or os.path.join(_HERE, "bmp_login")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
GUESTMAP_FILE = os.path.join(DATA_DIR, "guestmap.json")
BANLIST_FILE  = os.path.join(DATA_DIR, "banlist.json")
CHAT_QUEUE_FILE = os.path.join(DATA_DIR, "chat_queue.json")
ONLINE_FILE = os.path.join(DATA_DIR, "online_players.json")
KICK_QUEUE_FILE = os.path.join(DATA_DIR, "kick_queue.json")
os.makedirs(DATA_DIR, exist_ok=True)

VEHICLE_LIMITS = {"admin": 999, "authenticated": 5, "unauthenticated": 1}
DEFAULT_API_PORT = 12124
VERSION = "BMP-HTTP/v1.0.0"
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

# ---- 管理员审计日志 ----
ADMIN_AUDIT_FILE = os.path.join(DATA_DIR, "admin_audit.jsonl")

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

def readJsonFile(path):
    if not os.path.exists(path): return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.loads(f.read() or "null")
    except Exception:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.loads(f.read() or "null")
        except Exception:
            return None

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
    salt_bytes = [int(salt_hex[i:i+1], 16) for i in range(16)]
    pwd_len = max(1, len(password))
    out = []
    for i in range(1, 65):
        ci = ord(password[(i - 1) % pwd_len])
        si = salt_bytes[(i - 1) % 8]
        out.append(chr(((ci + si + i) % 95) + 32))
    return "".join(out)

def hashPassword(password):
    import secrets as _s
    salt_hex = _s.token_hex(8)   # 16 位 hex
    return f"{salt_hex}:{_lua_hash64(password, salt_hex)}"

def verifyPassword(password, stored):
    if not stored: return False
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
    data = readJsonFile(ACCOUNTS_FILE) or {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                v["bind_beam_ids"] = _ensure_list(v.get("bind_beam_ids"))
                v["login_records"] = _ensure_list(v.get("login_records"))
    return data

def saveAccounts(data):
    return writeJsonFile(ACCOUNTS_FILE, data)

def loadGuestMap():
    return readJsonFile(GUESTMAP_FILE) or {}

def saveGuestMap(gm):
    return writeJsonFile(GUESTMAP_FILE, gm)

def loadBanlist():
    data = readJsonFile(BANLIST_FILE) or {}
    if isinstance(data, dict):
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
        return info["username"]

# ============================================================
#  HTTP Handler
# ============================================================
class APIHandler(BaseHTTPRequestHandler):
    server_version = VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

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
            # 拒绝超大请求
            self._json(413, {"ok": False, "error": f"请求体过大 (最大 {MAX_BODY_SIZE} 字节)"})
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw: return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_OPTIONS(self):
        self._json(204, {})

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/api/ping"):
            self._json(200, {"ok": True, "ver": VERSION,
                             "now": int(time.time()),
                             "data_dir": DATA_DIR})
            return
        if path == "/api/chat/queue":
            return self._chat_queue(getattr(self, "_auth_cache", None))
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        # 先检查 IP 白名单
        cfg = getattr(self.server, "cfg", {})
        allow_subnets = cfg.get("allow_subnets")
        if allow_subnets:
            client_ip = self.client_address[0]
            try:
                ip_obj = ipaddress.ip_address(client_ip)
                if not any(ip_obj in n for n in allow_subnets):
                    self._json(403, {"ok": False, "error": "IP not allowed"})
                    return
            except Exception:
                self._json(403, {"ok": False, "error": "IP parse error"})
                return

        body = self._read_body()
        if body is None:
            self._json(400, {"ok": False, "error": "invalid JSON body"})
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

        # ---- 管理员 API ----
        if path.startswith("/api/admin/"):
            return self._admin_route(auth, path, body)

        self._json(404, {"ok": False, "error": "not found: " + path})

    # ============= 业务实现 =============
    def _register(self, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        hwid = (body.get("hwid") or "").strip()
        ip = (body.get("ip") or "").strip()
        player_name = (body.get("player_name") or "").strip()

        if not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
            return self._json(200, {"ok": False, "msg": "账号名不合法 (3-20 字符, 字母/数字/下划线)"})
        if len(password) < 4 or len(password) > 64:
            return self._json(200, {"ok": False, "msg": "密码不合法 (4-64 字符)"})

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
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        hwid = (body.get("hwid") or "").strip()
        ip = (body.get("ip") or "").strip()
        player_name = (body.get("player_name") or "").strip()
        client_ip = self.client_address[0]

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
        if username and username in LOGIN_RATE:
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
        # 1) 取身份 (可选登录态: 已登录用 username, 否则用 player_name)
        username = _validate_token(auth)
        name = (body.get("player_name") or body.get("name") or "").strip()
        if not name:
            name = username or "guest"
        # 截断名字长度
        if len(name) > 16:
            name = name[:16]
        # 简单过滤: 不允许纯空格 / 控制字符
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
                if not writeJsonFile(CHAT_QUEUE_FILE, queue):
                    return self._json(500, {"ok": False, "msg": "队列写入失败"})
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
        target = (body.get("username") or "").strip()
        if not target:
            return self._json(400, {"ok": False, "error": "缺少 username"})
        if target in ADMINS:
            return self._json(400, {"ok": False, "error": f"不能删除管理员 {target}"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            del accounts[target]
            saveAccounts(accounts)
            # 同步清理 guestmap 里指向该账号的条目
            gm = loadGuestMap()
            cleaned = {k: v for k, v in gm.items()
                      if not (isinstance(v, dict) and v.get("account") == target)}
            saveGuestMap(cleaned)
        return self._json(200, {"ok": True, "msg": f"账号 {target} 已删除"})

    def _admin_reset_password(self, body):
        target = (body.get("username") or "").strip()
        new_pw = body.get("new_password") or ""
        if not target or not new_pw:
            return self._json(400, {"ok": False, "error": "需要 username 和 new_password"})
        if len(new_pw) < 4:
            return self._json(400, {"ok": False, "error": "密码至少 4 位"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            accounts[target]["password_hash"] = hashPassword(new_pw)
            saveAccounts(accounts)
        return self._json(200, {"ok": True, "msg": f"账号 {target} 密码已重置"})

    def _admin_toggle_admin(self, body):
        target = (body.get("username") or "").strip()
        set_admin = bool(body.get("is_admin", False))
        if not target:
            return self._json(400, {"ok": False, "error": "缺少 username"})
        with FILE_LOCK:
            accounts = loadAccounts()
            if target not in accounts:
                return self._json(404, {"ok": False, "error": f"账号 {target} 不存在"})
            # 持久化 is_admin 到 accounts.json (供 Lua 插件读取)
            accounts[target]["is_admin"] = set_admin
            saveAccounts(accounts)
            if set_admin:
                ADMINS.add(target)
            else:
                ADMINS.discard(target)
        return self._json(200, {"ok": True, "msg": f"{target} 管理员权限: {'已开启' if set_admin else '已关闭'}",
                               "data": {"is_admin": set_admin, "admins": sorted(ADMINS)}})

    def _admin_unbind(self, body):
        target = (body.get("username") or "").strip()
        beam_id = (body.get("beam_id") or "").strip()
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
            saveAccounts(accounts)
            # 同步清理 guestmap
            gm = loadGuestMap()
            if beam_id in gm:
                del gm[beam_id]
                saveGuestMap(gm)
        return self._json(200, {"ok": True, "msg": f"已解除 {target} 的绑定 {beam_id}"})

    def _admin_ban(self, body):
        target_type = (body.get("type") or "").strip()  # account / hwid / ip / name
        target_val = (body.get("value") or "").strip()
        reason = (body.get("reason") or "").strip()
        duration_days = int(body.get("duration_days") or 0)  # 0 = 永久
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
    httpd = HTTPServer((host, port), APIHandler)
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
            print(f"[{VERSION}] 监听 {host}:{port}  (🔒 HTTPS/TLS)")
        except Exception as e:
            print(f"[!] TLS 启动失败, 回退到 HTTP: {e}")
            print(f"[{VERSION}] 监听 {host}:{port}")
    else:
        print(f"[{VERSION}] 监听 {host}:{port}")
        print("  ⚠️  未启用 TLS — 公网部署建议配置 --tls-cert / --tls-key")

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
    global DATA_DIR, ACCOUNTS_FILE, GUESTMAP_FILE, BANLIST_FILE, ADMINS, ONLINE_FILE, KICK_QUEUE_FILE
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
        DATA_DIR = os.path.abspath(args.data_dir)
        ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
        GUESTMAP_FILE = os.path.join(DATA_DIR, "guestmap.json")
        BANLIST_FILE  = os.path.join(DATA_DIR, "banlist.json")
        ONLINE_FILE = os.path.join(DATA_DIR, "online_players.json")
        KICK_QUEUE_FILE = os.path.join(DATA_DIR, "kick_queue.json")
        os.makedirs(DATA_DIR, exist_ok=True)

    if args.admins:
        ADMINS = set(a.strip() for a in args.admins.split(",") if a.strip())

    # 解析 --allow-ips
    allow_subnets = None
    if args.allow_ips and args.allow_ips.strip():
        allow_subnets = []
        for tok in args.allow_ips.split(","):
            tok = tok.strip()
            if not tok: continue
            try:
                if "/" in tok:
                    allow_subnets.append(ipaddress.ip_network(tok, strict=False))
                else:
                    allow_subnets.append(ipaddress.ip_network(tok + "/32", strict=False))
            except Exception as e:
                print(f"[WARN] 跳过无效的白名单条目 {tok}: {e}")

    cfg = {"allow_subnets": allow_subnets}
    if args.tls_cert and args.tls_key:
        cfg["tls_cert"] = os.path.abspath(args.tls_cert)
        cfg["tls_key"] = os.path.abspath(args.tls_key)
    _run_server(args.host, args.port, cfg)


if __name__ == "__main__":
    main()
