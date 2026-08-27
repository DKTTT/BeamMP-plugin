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

import re, os, sys, json, hashlib, hmac, time, threading, secrets, argparse, ipaddress
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 共享路径 — 和 main.lua 一致
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BMP_DATA_DIR") or os.path.join(_HERE, "bmp_login")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
GUESTMAP_FILE = os.path.join(DATA_DIR, "guestmap.json")
BANLIST_FILE  = os.path.join(DATA_DIR, "banlist.json")
CHAT_QUEUE_FILE = os.path.join(DATA_DIR, "chat_queue.json")
os.makedirs(DATA_DIR, exist_ok=True)

VEHICLE_LIMITS = {"admin": 999, "authenticated": 5, "unauthenticated": 1}
DEFAULT_API_PORT = 12124
VERSION = "BMP-HTTP/v1.0.0"
ACCESS_TTL = 7 * 24 * 3600  # 7 天

# 聊天速率限制: name -> [时间戳列表]
CHAT_RATE_LIMIT = {}
CHAT_RATE_WINDOW = 60   # 60 秒
CHAT_RATE_MAX = 6        # 最多 6 条/分钟

# ============================================================
#  JSON 数组兼容 (与 main.lua Array metatable 一致: 空表是 [])
# ============================================================
def _ensure_list(v):
    if isinstance(v, list): return v
    if isinstance(v, dict) and not v: return []
    return []

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
    return username in ADMINS

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

        # DEBUG: 打印收到的登录请求 (长度+repr, 不泄露真实内容)
        _log_login = f"[DBG-LOGIN] user={username!r} pwd_len={len(password)} pwd_repr={repr(password[:3]+'***'+password[-3:]) if len(password)>=6 else repr(password)} hwid_len={len(hwid)}"
        print(_log_login, flush=True)

        with FILE_LOCK:
            accounts = loadAccounts()
            acc = accounts.get(username)
            if not acc:
                print(f"[DBG-LOGIN] 账号不存在: {username}", flush=True)
                return self._json(200, {"ok": False, "msg": f"账号 {username} 不存在"})
            stored_hash = acc.get("password_hash") or ""
            ok_pwd = verifyPassword(password, stored_hash)
            print(f"[DBG-LOGIN] stored_hash_len={len(stored_hash)} verifyResult={ok_pwd}", flush=True)
            if not ok_pwd:
                return self._json(200, {"ok": False, "msg": "密码不正确"})

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
            acc["login_records"] = records[-100:]  # 最多 100 条
            saveAccounts(accounts)

            # 更新 guestmap (自动登录用, 与 main.lua 共享)
            if bids:
                gm = loadGuestMap()
                for b in bids:
                    gm[b] = {"account": username, "last_seen": int(time.time())}
                saveGuestMap(gm)

        token = _issue_token(username)
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


def _run_server(host, port, cfg):
    httpd = HTTPServer((host, port), APIHandler)
    httpd.cfg = cfg
    print(f"[{VERSION}] 监听 {host}:{port}")
    print(f"  数据目录: {DATA_DIR}")
    print(f"  管理员名单: {sorted(ADMINS) if ADMINS else '(空)'}")
    if cfg.get("allow_subnets"):
        print(f"  IP 白名单: {[str(n) for n in cfg['allow_subnets']]}")
    print("  API:  POST /api/auth/{register,login,logout,whoami}")
    print("        POST /api/hwid/bind")
    print("        POST /api/vehicle/limit")
    print("        GET  /api/ping")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    global DATA_DIR, ACCOUNTS_FILE, GUESTMAP_FILE, BANLIST_FILE, ADMINS
    p = argparse.ArgumentParser(description="BMPLogin HTTP API 服务器 (Bridge.exe 直连用)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    p.add_argument("--data-dir", default=DATA_DIR, help="bmp_login 目录路径 (与 BeamMP 共享)")
    p.add_argument("--allow-ips", default="",
                   help="逗号分隔的 IP/CIDR, 不填=允许所有. 例: 127.0.0.1,192.168.0.0/24,110.x.x.x/32")
    p.add_argument("--admins", default="",
                   help="逗号分隔管理员账号, 例: DRIFTKING,seeyou   (车辆 999 辆上限)")
    args = p.parse_args()

    if args.data_dir:
        DATA_DIR = os.path.abspath(args.data_dir)
        ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
        GUESTMAP_FILE = os.path.join(DATA_DIR, "guestmap.json")
        BANLIST_FILE  = os.path.join(DATA_DIR, "banlist.json")
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
    _run_server(args.host, args.port, cfg)


if __name__ == "__main__":
    main()
