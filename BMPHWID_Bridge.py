# -*- coding: utf-8 -*-
"""
=====================================================================
#  BMPHWID Auth v2.1.0  (玩家认证器 - 机器码 + 所有命令用法)
---------------------------------------------------------------------
  功能:
    1. 在玩家电脑上采集【永久稳定的 HWID】
         MachineGuid + 主板 UUID + 物理网卡 MAC
         → 混合 FNV-1a → 36 位 UUID (永不变化)

    2. 显示 HWID + 采集来源列表 (给玩家确认稳定性: 至少 1 个 MG 就稳)

    3. 本地 HTTP 服务  http://127.0.0.1:7788
         GET  /hwid  →  {hwid, ver, name, ok, sources, command}
         客户端 BMPHWID.zip 通过这个接口拉机器码, 再发给服务器

    4. 命令参考面板: 服务器所有 /xxx 命令用法 (不含管理员命令)
         按类别分组: 账号、认证、车辆、其他
         每个命令: 语法 + 说明 + 示例 + 常见错误提示

  打包： pip install pyinstaller
         pyinstaller -F -w -n BMPHWID_Bridge BMPHWID_Bridge.py
         → dist\BMPHWID_Bridge.exe   单文件 ~11MB
=====================================================================
"""

import re, os, sys, json, hashlib, threading, socket
from http.server import HTTPServer, BaseHTTPRequestHandler

VERSION = "2.3.0"
HTTP_PORT = 7788
PAYLOAD_VER = "Bridge-v2.3"
HTTP_NAME = f"BMPHWID_Bridge/v{VERSION}"
WIN_TITLE = f"BeamMP 认证器 v{VERSION}  —  HWID + 命令手册 + 直连服务器"

# 本地配置持久化 (服务器 URL / access_token 等, 写到 EXE 所在目录)
def _get_cfg_path():
    try:
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "bmphwid_bridge_config.json")

CFG = {}
try:
    with open(_get_cfg_path(), "r", encoding="utf-8") as _f:
        CFG = json.loads(_f.read() or "{}")
except Exception:
    CFG = {}

def _save_cfg():
    try:
        with open(_get_cfg_path(), "w", encoding="utf-8") as _f:
            json.dump(CFG, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---- urllib 客户端 (POST JSON) ----
import urllib.request, urllib.error

def api_post(path, body_obj=None, timeout=8):
    """POST JSON 到服务器 HTTP API. 返回 (ok, result_dict)"""
    base = (CFG.get("server_url") or "").strip().rstrip("/")
    if not base:
        return False, {"ok": False, "msg": "请先在【直连服务器】选项卡顶部填入服务器 HTTP API 地址"}
    if not (base.startswith("http://") or base.startswith("https://")):
        return False, {"ok": False, "msg": "服务器地址必须以 http:// 或 https:// 开头"}
    url = base + path
    token = CFG.get("access_token") or ""
    data = b"{}" if body_obj is None else json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read() or b"{}"
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                obj = {"ok": False, "msg": raw.decode("utf-8", errors="replace")[:200]}
            return True, obj
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b"{}"
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            obj = {"ok": False, "msg": f"HTTP {e.code}: {str(e.reason)}"}
        return False, obj
    except Exception as e:
        return False, {"ok": False, "msg": "请求失败: " + str(e)}

def api_ping(timeout=6):
    base = (CFG.get("server_url") or "").strip().rstrip("/")
    if not base:
        return False, {"ok": False, "msg": "请先填入服务器 HTTP API 地址"}
    url = base + "/api/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read() or b"{}"
            try:
                return True, json.loads(raw.decode("utf-8"))
            except Exception:
                return True, {"ok": True, "msg": raw.decode("utf-8", errors="replace")[:200]}
    except Exception as e:
        return False, {"ok": False, "msg": "不通: " + str(e)}

# ============================================================
#  命令参考数据 (从 main.lua 提取, 只保留普通玩家命令, 不含管理员)
# ============================================================
CMD_GROUPS = [
    {
        "name": "账号管理",
        "emoji": "\U0001F464",
        "desc": "注册 / 登录 / 登出 / 查看当前身份",
        "cmds": [
            {
                "name": "/register",
                "syntax": "/register <账号> <密码>",
                "args": [
                    ("账号", "4-20 字符, 字母/数字/下划线, 不区分大小写"),
                    ("密码", "6-64 字符, 区分大小写; 建议包含数字+字母"),
                ],
                "desc": "注册一个新账号, 成功后会提示你 /login 登录。\n注意: 同机器码 或 同 IP 注册超过 3 个账号会被限制。",
                "examples": [
                    "/register <你的账号> <你的密码>",
                    "/register my_name 1234Ab@cd",
                ],
                "errors": [
                    ("账号已存在", "换个名字, 或用 /login 登回你已有的账号"),
                    ("密码太短", "至少 6 个字符, 不能是纯空格"),
                ],
            },
            {
                "name": "/login",
                "syntax": "/login <账号> <密码>",
                "args": [
                    ("账号", "注册时的名字"),
                    ("密码", "注册时的密码"),
                ],
                "desc": "登录到已注册的账号。登录成功后：\n"
                        "• 当前机器码会自动绑定到该账号（下次开机会自动登录，guest 数字变了/IP变了都能命中）\n"
                        "• 身份从未认证→认证用户，车辆上限 1→5 辆\n"
                        "• 所有命令（/register /login 等）不会公屏广播，放心输入。",
                "examples": [
                    "/login <你的账号> <你的密码>",
                ],
                "errors": [
                    ("账号不存在", "先用 /register 注册，或确认拼写对不对"),
                    ("密码不正确", "密码区分大小写；或联系管理重置密码"),
                ],
            },
            {
                "name": "/logout",
                "syntax": "/logout",
                "args": [],
                "desc": "退出当前登录的账号。退出后身份回退为“游客/未认证”，车辆上限变回 1 辆。\n下次进来如果机器码已绑定，会自动登录回去。",
                "examples": ["/logout"],
                "errors": [],
            },
            {
                "name": "/whoami",
                "syntax": "/whoami",
                "args": [],
                "desc": "查看当前登录状态：显示账号名、登录方式（稳定ID/IP兜底/NAME兜底）、\n绑定的 HWID、首次/上次登录时间、当前车辆计数。",
                "examples": ["/whoami"],
                "errors": [],
            },
        ],
    },
    {
        "name": "认证 & HWID",
        "emoji": "\U0001F510",
        "desc": "绑定机器码 → 升级为认证用户 (车辆上限 1→5 辆)",
        "cmds": [
            {
                "name": "/bmpid",
                "syntax": "/bmpid <UUID 或 完整 payload>",
                "args": [
                    ("UUID", "最简形式：36 位 HWID UUID（直接点“复制命令”按钮）"),
                    ("F|payload", "客户端 zip 自动用的 base64 单分块格式（通常不用手动打）"),
                    ("k=v&k=v", "老格式，比如 /bmpid uuid=xxx-xxx"),
                ],
                "desc": "上报当前机器码，升级为认证用户。\n"
                        "⚠️ 认证条件：HWID 绑定 + 账号登录同时满足，才算认证用户。\n"
                        "未认证只能刷 1 辆车；认证后能刷 5 辆；管理员不受限。\n"
                        "执行后服务器私聊会回“HWID已接收，绑定稳定UUID: xxx-xxx”，代表成功。",
                "examples": [
                    "/bmpid 4ca2682c-bd0c-44e4-8413-0a55302f64b1",
                    "（或者直接点上面的“复制命令 (/bmpid UUID)”按钮，粘贴回车）",
                ],
                "errors": [
                    ("用法: /bmpid <payload>", "你只打了 /bmpid 后面没加 UUID → 把 HWID 粘在后面"),
                    ("没有稳定硬件码", "极少见，先关掉所有虚拟机/云桌面，重启本程序再试"),
                ],
            },
        ],
    },
    {
        "name": "车辆限制",
        "emoji": "\U0001F697",
        "desc": "查看当前身份/车辆上限 (1/5/无限制)",
        "cmds": [
            {
                "name": "/vehiclelimit",
                "aliases": ["/vehicles", "/carlimit"],
                "syntax": "/vehiclelimit    (别名: /vehicles /carlimit)",
                "args": [],
                "desc": "显示你的身份、车辆生成上限、当前已经在世界里生成了多少辆：\n"
                        "  • 游客/未认证用户 (没打 /bmpid 且没 /login)  →  上限 1 辆\n"
                        "  • 认证用户 (打了 /bmpid + /login)              →  上限 5 辆\n"
                        "  • 管理员 (服务器账号在 admin 列表里)             →  不限 (999 辆)\n"
                        "如果刷车时被取消 + 私聊提示“已达上限”，直接打这个命令看当前身份。",
                "examples": [
                    "/vehiclelimit    → 您是[未认证用户], 车辆上限 1 辆, 当前已生成 0 辆",
                    "/vehicles        → 别名, 同上（比 /vehiclelimit 好打）",
                    "/carlimit        → 别名, 同上",
                ],
                "errors": [],
            },
        ],
    },
    {
        "name": "帮助 & 其他",
        "emoji": "\U0001F4D6",
        "desc": "查看所有命令 + 服务器规则",
        "cmds": [
            {
                "name": "/help",
                "syntax": "/help",
                "args": [],
                "desc": "在游戏里显示简化版命令列表（只有一行一行的语法）。\n"
                        "想看到完整说明/示例/错误提示 → 看本程序这个面板。",
                "examples": ["/help"],
                "errors": [],
            },
        ],
    },
]


# ============================================================
#  Part 1: 稳定 HWID 采集  (标准库 + winreg + wmic)
# ============================================================
def _read_reg_machineGuid():
    r"""读 HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid"""
    vals = []
    try:
        import winreg
        for arch_flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Cryptography",
                                    0, winreg.KEY_READ | arch_flag) as k:
                    v, _ = winreg.QueryValueEx(k, "MachineGuid")
                    if isinstance(v, str) and len(v) >= 16:
                        vals.append(f"MG:{v.lower().strip()}")
            except Exception:
                pass
    except Exception:
        pass
    return vals

def _read_wmi_UUID():
    """主板 SMBIOS UUID (via wmic.exe)"""
    try:
        import subprocess
        r = subprocess.run(
            ["wmic.exe", "csproduct", "get", "uuid", "/value"],
            capture_output=True, timeout=10, creationflags=0x08000000)
        def _dec(b):
            if not b: return ""
            for enc in ("utf-8", "utf-16", "gbk", "latin-1"):
                try: return b.decode(enc)
                except Exception: pass
            try: return b.decode("latin-1", errors="ignore")
            except Exception: return ""
        txt = _dec(r.stdout or b"") + _dec(r.stderr or b"")
        m = re.search(r"UUID\s*=\s*([A-Fa-f0-9\-]{30,})", txt)
        if m: return ["CSUID:" + m.group(1).lower().strip()]
    except Exception:
        pass
    return []

def _read_physical_MACs():
    """优先 uuid.getnode() 首网卡 + ipconfig 全部 MAC"""
    macs = []
    # 首网卡
    try:
        import uuid as _u
        addr = _u.getnode()
        if addr and addr != 0:
            s = ":".join(f"{(addr >> (8*(5-i))) & 0xff:02x}" for i in range(6))
            if not s.startswith("00:00:00:00:00") and not s.startswith("02:00:00"):
                macs.append("MAC:" + s)
    except Exception:
        pass
    # ipconfig 兜底所有物理网卡
    try:
        import subprocess
        r = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=6,
                           creationflags=0x08000000)
        def _d(b):
            try: return (b or b"").decode("gbk", errors="ignore")
            except Exception: return (b or b"").decode("latin-1", errors="ignore")
        txt = _d(r.stdout or b"")
        for m in re.finditer(r"物理地址[^\n\r:：]*[：:]\s*([A-Fa-f0-9\-]{17})", txt):
            s = m.group(1).replace("-", ":").lower()
            if s.startswith("00:00:00:00:00") or s.startswith("02:00:00"):
                continue
            if not any(s in x for x in macs):
                macs.append("MAC:" + s)
    except Exception:
        pass
    return macs

def collect_sources():
    """返回稳定采集源列表"""
    return _read_reg_machineGuid() + _read_wmi_UUID() + _read_physical_MACs()

def fnv1a_hex(text):
    h = 0xcbf29ce484222325
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"

def build_stable_hwid(sources):
    """sources → UUIDv4 形式的永久 ID"""
    if not sources:
        import uuid as _u
        return str(_u.uuid4()).lower()
    concat = "||".join(sorted(set(sources)))
    h = fnv1a_hex(concat)
    # 拼成 UUIDv4 形式: xxxxxxxx-xxxx-4xxx-8/9/A/Bxxx-xxxxxxxxxxxx
    while len(h) < 32:
        h = h + fnv1a_hex(concat + "|round2")
    a = h[0:8]
    b = h[8:12]
    c = "4" + h[13:16]
    d8 = "89ab"[int(h[16], 16) % 4] + h[17:20]
    e = h[20:32]
    return f"{a}-{b}-{c}-{d8}-{e}"


# ============================================================
#  Part 2: 本地 HTTP 服务 (给 BMPHWID.zip 拉 HWID 用)
# ============================================================
HTTP_STATE = {
    "hwid": None,
    "sources": [],
    "command": None,
}

class HWIDHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # 静默, 不打印任何到控制台/tk窗口

    def _write(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/hwid") or self.path.startswith("/") and len(self.path) <= 1:
            self._write(200, {
                "ok": True,
                "name": HTTP_NAME,
                "ver": PAYLOAD_VER,
                "hwid": HTTP_STATE["hwid"],
                "sources": HTTP_STATE["sources"],
                "command": HTTP_STATE["command"],
            })
            return
        self._write(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.endswith("/ping"):
            self._write(200, {"ok": True})
            return
        self._write(404, {"ok": False, "error": "not found"})

def _serve_forever(httpd):
    try:
        httpd.serve_forever()
    except Exception:
        pass


# ============================================================
#  Part 3: Tk GUI (极简: HWID + 来源列表 + 使用说明 + 复制按钮)
# ============================================================
def _build_cmd_detail_frame(parent, cmd, hwid):
    """构建单个命令的详细面板"""
    import tkinter as tk
    frame = tk.Frame(parent, bg="#ffffff")

    # 标题行: 命令名 + 语法
    hdr = tk.Frame(frame, bg="#ffffff")
    hdr.pack(fill="x", padx=12, pady=(10, 2))
    tk.Label(hdr, text=cmd["name"],
             font=("Consolas", 14, "bold"),
             bg="#ffffff", fg="#1d3557").pack(side="left")
    if cmd.get("aliases"):
        tk.Label(hdr, text="   别名: " + " / ".join(cmd["aliases"]),
                 font=("Microsoft YaHei UI", 9),
                 bg="#ffffff", fg="#555").pack(side="left", padx=(8, 0))

    # 语法
    syn = tk.Frame(frame, bg="#f1f5f9", bd=1, relief="solid")
    syn.pack(fill="x", padx=12, pady=2)
    tk.Label(syn, text="语法:  " + cmd["syntax"],
             font=("Consolas", 11),
             bg="#f1f5f9", fg="#0f172a").pack(anchor="w", padx=10, pady=6)

    # 参数
    if cmd.get("args"):
        args_f = tk.LabelFrame(frame,
                               text=" 参数: ",
                               font=("Microsoft YaHei UI", 9, "bold"),
                               bg="#ffffff", fg="#1d3557", bd=0,
                               labelanchor="nw")
        args_f.pack(fill="x", padx=14, pady=(4, 2))
        for (an, ad) in cmd["args"]:
            row = tk.Frame(args_f, bg="#ffffff")
            row.pack(fill="x", pady=1)
            tk.Label(row, text="  <" + an + ">",
                     font=("Consolas", 10, "bold"),
                     bg="#ffffff", fg="#6d28d9", width=16, anchor="w").pack(side="left")
            tk.Label(row, text="  " + ad,
                     font=("Microsoft YaHei UI", 9),
                     bg="#ffffff", fg="#334155", anchor="w",
                     wraplength=520, justify="left").pack(side="left", fill="x", expand=True)

    # 说明
    desc_f = tk.LabelFrame(frame,
                           text=" 说明: ",
                           font=("Microsoft YaHei UI", 9, "bold"),
                           bg="#ffffff", fg="#1d3557", bd=0,
                           labelanchor="nw")
    desc_f.pack(fill="x", padx=14, pady=(4, 2))
    for line in cmd["desc"].split("\n"):
        line = line.rstrip()
        if not line:
            tk.Frame(desc_f, height=4, bg="#ffffff").pack(fill="x")
            continue
        tk.Label(desc_f, text="  " + line,
                 font=("Microsoft YaHei UI", 10),
                 bg="#ffffff", fg="#1e293b",
                 anchor="w", justify="left",
                 wraplength=640).pack(fill="x", padx=4, pady=1, ipady=1)

    # 示例
    if cmd.get("examples"):
        ex_f = tk.LabelFrame(frame,
                             text=" 示例: ",
                             font=("Microsoft YaHei UI", 9, "bold"),
                             bg="#ffffff", fg="#1d3557", bd=0,
                             labelanchor="nw")
        ex_f.pack(fill="x", padx=14, pady=(4, 2))

        def _extract_cmd_only(text):
            """从示例行里抽出纯命令: 去掉注释 → 别名说明 / 中文结果提示, 并把参数里的 <占位符> 保留"""
            if not isinstance(text, str): return ""
            t = text
            # 去掉以 "    →" 或 "  →" 开头的注释块（服务器返回的中文说明）
            for sep in ["    →", "  →", " →"]:
                if sep in t:
                    idx = t.find(sep)
                    # 只当 → 左边不是参数符 (比如 password 的 < >) 才截断
                    left = t[:idx]
                    right = t[idx:]
                    if ("别名" in right or "同上" in right or "比 " in right or
                        "您是" in right or "（" in right or "," in right or "车辆上限" in right or "当前已生成" in right):
                        t = left.rstrip()
            # 去掉 "    (别名: ...)" 这样的尾注释 (比如 syntax 字段)
            if "    (别名:" in t:
                t = t[:t.find("    (别名:")].rstrip()
            # 去尾行中文括号说明 ("/bmpid 例子的第二行说明" 这类不会走到这, 因为不是 / 开头)
            return t.strip()

        def _make_copy(btn_parent, text, root2):
            if not text or not isinstance(text, str) or text.startswith("（"):
                return lambda: None
            cmd_only = _extract_cmd_only(text)
            if not cmd_only or not cmd_only.startswith("/"):
                return lambda: None
            def _inner():
                try:
                    root2.clipboard_clear()
                    root2.clipboard_append(cmd_only)
                    root2.update()
                except Exception:
                    pass
            return _inner

        import tkinter as _tk_b
        for ex in cmd["examples"]:
            row = _tk_b.Frame(ex_f, bg="#ffffff")
            row.pack(fill="x", pady=1)
            e = _tk_b.Entry(row, font=("Consolas", 10), bg="#ecfdf5", fg="#065f46",
                            readonlybackground="#ecfdf5", relief="flat", bd=0)
            e.insert(0, ex)
            e.config(state="readonly")
            e.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=2)
            if isinstance(ex, str) and ex.startswith("/") and _extract_cmd_only(ex).startswith("/"):
                from tkinter import ttk as _ttk
                copy_btn = _ttk.Button(row, text="复制", width=6,
                                       command=_make_copy(row, ex, parent.winfo_toplevel()))
                copy_btn.pack(side="right", padx=4)

    # 常见错误
    if cmd.get("errors"):
        err_f = tk.LabelFrame(frame,
                              text=" 常见错误 & 解决: ",
                              font=("Microsoft YaHei UI", 9, "bold"),
                              bg="#ffffff", fg="#991b1b", bd=0,
                              labelanchor="nw")
        err_f.pack(fill="x", padx=14, pady=(4, 8))
        for (ep, es) in cmd["errors"]:
            row = tk.Frame(err_f, bg="#fff5f5")
            row.pack(fill="x", padx=4, pady=1)
            tk.Label(row, text="  ✖ " + ep,
                     font=("Microsoft YaHei UI", 9, "bold"),
                     bg="#fff5f5", fg="#991b1b", width=22, anchor="w").pack(side="left", pady=2)
            tk.Label(row, text=" → " + es,
                     font=("Microsoft YaHei UI", 9),
                     bg="#fff5f5", fg="#7f1d1d", anchor="w",
                     wraplength=500, justify="left").pack(side="left", fill="x", expand=True, pady=2)

    return frame


def _run_gui(hwid, sources_list):
    import tkinter as tk
    from tkinter import ttk

    # 构建 /bmpid 命令 (给玩家手动粘贴兜底)
    command = f"/bmpid {hwid}"

    # 更新全局 HTTP 状态
    HTTP_STATE["hwid"] = hwid
    HTTP_STATE["sources"] = sources_list
    HTTP_STATE["command"] = command

    # 启动本地 HTTP
    httpd = None
    http_thread = None
    http_msg = ""
    try:
        httpd = HTTPServer(("127.0.0.1", HTTP_PORT), HWIDHandler)
        http_thread = threading.Thread(target=_serve_forever, args=(httpd,),
                                         daemon=True)
        http_thread.start()
        http_msg = f"✅ 启动成功  http://127.0.0.1:{HTTP_PORT}"
    except OSError as e:
        http_msg = f"⚠️ 端口 {HTTP_PORT} 被占用: {e}  (BMPHWID.zip 可能拉不到机器码)"

    # ============================================================
    # GUI 构造 (更大尺寸 + Notebook 两个选项卡)
    # ============================================================
    root = tk.Tk()
    root.title(WIN_TITLE)
    root.geometry("880x720")
    root.minsize(760, 600)
    root.configure(bg="#f7f7f9")

    # ====== 选项卡 (Notebook) ======
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=(12, 10))

    # ============================================================
    # Tab 1: HWID 认证 + 采集来源 + 使用说明
    # ============================================================
    tab1 = tk.Frame(nb, bg="#f7f7f9")
    nb.add(tab1, text="  \U0001F511 HWID 认证  ")

    # 标题
    top = tk.Frame(tab1, bg="#f7f7f9")
    top.pack(fill="x", padx=10, pady=(12, 4))
    tk.Label(top,
             text="\U0001F527 BeamMP 认证器 — 采集机器码 + 账号登录",
             font=("Microsoft YaHei UI", 16, "bold"),
             bg="#f7f7f9", fg="#1d3557").pack(side="left")

    # 说明第一行
    intro = tk.Label(tab1,
                     text="下面这个 HWID 在你这台电脑上 【永久不变】, guest 数字变了 / IP 换了 / 游戏重开了都不会变:",
                     font=("Microsoft YaHei UI", 10),
                     bg="#f7f7f9", fg="#333", justify="left", wraplength=820)
    intro.pack(fill="x", padx=18, pady=(0, 4))

    # HWID 文本框
    f_id = tk.Frame(tab1, bg="#ffffff", bd=1, relief="solid")
    f_id.pack(fill="x", padx=18, pady=(0, 4))
    e_id = tk.Entry(f_id, font=("Consolas", 14), bd=0, justify="center",
                    bg="#ffffff", fg="#1d3557", readonlybackground="#ffffff",
                    relief="flat")
    e_id.insert(0, hwid)
    e_id.config(state="readonly")
    e_id.pack(fill="x", padx=10, pady=10)

    # 复制按钮
    btn_row = tk.Frame(tab1, bg="#f7f7f9")
    btn_row.pack(fill="x", padx=18, pady=(2, 10))

    def _copy_cmd():
        root.clipboard_clear()
        root.clipboard_append(command)
        try: root.update()
        except Exception: pass

    def _copy_hwid():
        root.clipboard_clear()
        root.clipboard_append(hwid)
        try: root.update()
        except Exception: pass

    ttk.Button(btn_row, text="\U0001F4CB 复制 HWID", command=_copy_hwid,
               width=22).pack(side="left")
    ttk.Button(btn_row, text="\U0001F4CB 复制命令 (/bmpid UUID)",
               command=_copy_cmd, width=30).pack(side="left", padx=10)
    tk.Label(btn_row,
             text=f"当前显示: 短UUID命令 ({len(command)} 字)",
             font=("Microsoft YaHei UI", 9),
             bg="#f7f7f9", fg="#666").pack(side="right")

    # 采集来源
    f_src = tk.LabelFrame(tab1,
                          text=" 采集来源 (用于确认 HWID 稳定性, 至少有 1 项 MachineGuid 就非常稳定了) : ",
                          font=("Microsoft YaHei UI", 10, "bold"),
                          bg="#f7f7f9", fg="#1d3557", bd=1, relief="solid",
                          labelanchor="nw")
    f_src.pack(fill="both", expand=False, padx=18, pady=(4, 10))

    has_mg = any(x.startswith("MG:") for x in sources_list)
    has_csu = any(x.startswith("CSUID:") for x in sources_list)
    has_mac = any(x.startswith("MAC:") for x in sources_list)

    for s in sources_list:
        if s.startswith("MG:"):
            txt = "\u2705 MachineGuid (HKLM\\Cryptography):  " + s[3:][:24] + "..."
            fg = "#2b9348"
        elif s.startswith("CSUID:"):
            txt = "\u2705 主板 SMBIOS UUID (wmic csproduct):  " + s[6:][:24] + "..."
            fg = "#2b9348"
        elif s.startswith("MAC:"):
            txt = "\u2705 物理网卡 MAC 地址:  " + s[4:]
            fg = "#2b9348"
        else:
            txt = s
            fg = "#555"
        tk.Label(f_src, text=txt, font=("Consolas", 10),
                 bg="#ffffff", fg=fg, anchor="w").pack(fill="x", padx=8, pady=2)

    if not (has_mg or has_csu or has_mac):
        tk.Label(f_src,
                 text="\u274C 没有采集到任何稳定来源 (HWID 不稳定, 每次启动会变)",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 bg="#fff3cd", fg="#b7791f").pack(fill="x", padx=8, pady=4)

    # 使用说明
    f_guide = tk.LabelFrame(tab1,
                            text=" 使用说明 (自动模式 + 兜底) : ",
                            font=("Microsoft YaHei UI", 10, "bold"),
                            bg="#f7f7f9", fg="#1d3557", bd=1, relief="solid",
                            labelanchor="nw")
    f_guide.pack(fill="both", expand=True, padx=18, pady=(4, 10))

    guide_lines = [
        "\u2460  本程序要先启动, 再用 BeamMP Launcher 启动游戏!  (顺序不能反)",
        "\u2461  进入服务器后: 服务器会自动向你的 BMPHWID.zip 请求 HWID（不需要你做任何操作）",
        "\u2462  BMPHWID.zip 从本程序 http://127.0.0.1:7788/hwid 拉到 UUID → 直接回传服务器",
        "\u2463  进游戏直接 /login 你的账号密码, HWID 就会永远绑定到你的账号上",
        "\u2464  下次开机会自动登录（guest 数字变了 / IP 换了 都能命中稳定 ID）",
        "",
        "兜底 (服务器提示未认证时才用):",
        "   → 点击上面的 【复制命令 (/bmpid UUID)】 按钮",
        "   → 在游戏聊天框里粘贴 + 回车",
        "   → 服务器会私聊回复 HWID已接收，然后输入 /login 账号 密码",
    ]
    for line in guide_lines:
        if not line:
            tk.Frame(f_guide, height=6, bg="#ffffff").pack(fill="x")
            continue
        tk.Label(f_guide, text=line,
                 font=("Microsoft YaHei UI", 10),
                 bg="#ffffff", fg="#222", anchor="w",
                 justify="left", wraplength=780).pack(fill="x", padx=12, pady=2, ipady=1)

    # ============================================================
    # Tab 2: 命令参考 (按组 按钮 + 详细面板)
    # ============================================================
    tab2 = tk.Frame(nb, bg="#f7f7f9")
    nb.add(tab2, text="  \U0001F4D6 命令参考 (普通玩家)  ")

    # 顶部说明
    hdr2 = tk.Frame(tab2, bg="#f7f7f9")
    hdr2.pack(fill="x", padx=12, pady=(10, 4))
    tk.Label(hdr2,
             text="\U0001F4D6 服务器命令参考（普通玩家可用，不含管理员命令）",
             font=("Microsoft YaHei UI", 14, "bold"),
             bg="#f7f7f9", fg="#1d3557").pack(side="left")
    tk.Label(tab2,
             text="左侧选分类 → 点命令按钮 → 右侧显示详细语法 / 参数 / 说明 / 示例(可复制) / 常见错误",
             font=("Microsoft YaHei UI", 9),
             bg="#f7f7f9", fg="#64748b").pack(fill="x", padx=14)

    # 主体: PanedWindow 左(分类) 中(命令按钮) 右(详情)
    main_pan = tk.Frame(tab2, bg="#f7f7f9")
    main_pan.pack(fill="both", expand=True, padx=10, pady=(4, 8))

    # 左侧: 分类按钮列表
    grp_frame = tk.Frame(main_pan, bg="#ffffff", bd=1, relief="solid")
    grp_frame.pack(side="left", fill="y", padx=(4, 2))
    grp_frame.configure(width=160)

    # 中间: 命令按钮列表
    cmdlist_frame = tk.Frame(main_pan, bg="#ffffff", bd=1, relief="solid")
    cmdlist_frame.pack(side="left", fill="y", padx=(2, 2))
    cmdlist_frame.configure(width=200)

    # 右侧: 详情 (带 Scrollbar)
    right_wrap = tk.Frame(main_pan, bg="#f7f7f9")
    right_wrap.pack(side="left", fill="both", expand=True, padx=(2, 4))
    right_wrap.configure(width=480)

    right_canvas = tk.Canvas(right_wrap, bg="#ffffff", bd=1, relief="solid",
                             highlightthickness=0)
    right_sb = ttk.Scrollbar(right_wrap, orient="vertical", command=right_canvas.yview)
    right_content = tk.Frame(right_canvas, bg="#ffffff")
    right_content.bind("<Configure>",
                       lambda e: right_canvas.configure(scrollregion=right_canvas.bbox("all")))
    right_canvas.create_window((0, 0), window=right_content, anchor="nw")
    right_canvas.configure(yscrollcommand=right_sb.set)
    right_canvas.pack(side="left", fill="both", expand=True)
    right_sb.pack(side="right", fill="y")

    # 鼠标滚轮
    def _bind_wheel(evt):
        right_canvas.bind_all("<MouseWheel>",
                              lambda e2: right_canvas.yview_scroll(int(-1 * (e2.delta / 120)), "units"))
    def _unbind_wheel(evt):
        right_canvas.unbind_all("<MouseWheel>")
    right_canvas.bind("<Enter>", _bind_wheel)
    right_canvas.bind("<Leave>", _unbind_wheel)

    current_group_idx = [0]
    current_cmd_idx = [0]

    def _render_detail(gi, ci):
        # Clear right_content
        for c in right_content.winfo_children():
            c.destroy()
        grp = CMD_GROUPS[gi]
        cmd = grp["cmds"][ci]
        df = _build_cmd_detail_frame(right_content, cmd, hwid)
        df.pack(fill="both", expand=True)
        right_canvas.update_idletasks()
        right_canvas.yview_moveto(0.0)

    def _render_cmd_buttons(gi):
        """根据分类渲染中间的命令按钮列表, 自动选中第 0 个"""
        for w in cmdlist_frame.winfo_children():
            w.destroy()
        grp = CMD_GROUPS[gi]
        tk.Label(cmdlist_frame,
                 text="  " + grp.get("emoji", "") + " " + grp["name"],
                 font=("Microsoft YaHei UI", 10, "bold"),
                 bg="#dbeafe", fg="#1e3a8a", anchor="w").pack(fill="x", pady=(2, 2))
        if grp.get("desc"):
            tk.Label(cmdlist_frame, text=grp["desc"],
                     font=("Microsoft YaHei UI", 8),
                     bg="#ffffff", fg="#475569",
                     wraplength=180, justify="left").pack(fill="x", padx=8, pady=(0, 6))

        btns = []
        for ci, cmd in enumerate(grp["cmds"]):
            txt = cmd["name"]
            if cmd.get("aliases"):
                txt += "\n(" + " ".join(cmd["aliases"][:2]) + ")"
            b = tk.Button(cmdlist_frame, text=txt, bd=0, cursor="hand2",
                          font=("Consolas", 10, "bold"),
                          fg="#1e293b", bg="#ffffff",
                          activebackground="#1d3557", activeforeground="#ffffff",
                          anchor="w", justify="left", padx=8, pady=6,
                          relief="flat")
            def _on_enter(evt, b2=b):
                b2.configure(bg="#e2e8f0", fg="#0f172a")
            def _on_leave(evt, b2=b, sel_idx=None):
                if sel_idx is None or sel_idx != current_cmd_idx[0]:
                    b2.configure(bg="#ffffff", fg="#1e293b")
            def _make_click(i_g, i_c, b2):
                def _inner():
                    for bi, (bb, _, _) in enumerate(btns):
                        if bi == i_c:
                            bb.configure(bg="#1d3557", fg="#ffffff")
                        else:
                            bb.configure(bg="#ffffff", fg="#1e293b")
                    current_cmd_idx[0] = i_c
                    _render_detail(i_g, i_c)
                return _inner
            b.bind("<Enter>", _on_enter)
            b.bind("<Leave>", _on_leave)
            b.configure(command=_make_click(gi, ci, b))
            b.pack(fill="x", padx=4, pady=1)
            btns.append((b, gi, ci))

        # 选中第 0 个
        if btns:
            btns[0][0].configure(bg="#1d3557", fg="#ffffff")
            current_cmd_idx[0] = 0
            _render_detail(gi, 0)

    def _render_group_buttons():
        for w in grp_frame.winfo_children():
            w.destroy()
        tk.Label(grp_frame, text="  分类",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 bg="#dbeafe", fg="#1e3a8a", anchor="w").pack(fill="x", pady=(2, 2))
        for gi, grp in enumerate(CMD_GROUPS):
            txt = " " + grp.get("emoji", "") + " " + grp["name"]
            b = tk.Button(grp_frame, text=txt, bd=0, cursor="hand2",
                          font=("Microsoft YaHei UI", 10, "bold"),
                          fg="#1e293b", bg="#ffffff",
                          activebackground="#1d3557", activeforeground="#ffffff",
                          anchor="w", justify="left", padx=6, pady=6,
                          relief="flat")
            group_btns = []
            def _on_enter(evt, b2=b):
                b2.configure(bg="#e2e8f0", fg="#0f172a")
            def _on_leave(evt, b2=b):
                if b2 not in group_btns_sel_ref:
                    b2.configure(bg="#ffffff", fg="#1e293b")
            def _make_click(i_g, b2):
                def _inner():
                    for bb in grp_frame.winfo_children():
                        if isinstance(bb, tk.Button) and bb is not b2:
                            bb.configure(bg="#ffffff", fg="#1e293b")
                    b2.configure(bg="#1d3557", fg="#ffffff")
                    group_btns_sel_ref.clear()
                    group_btns_sel_ref.append(b2)
                    current_group_idx[0] = i_g
                    _render_cmd_buttons(i_g)
                return _inner
            group_btns_sel_ref = []
            b.bind("<Enter>", _on_enter)
            b.bind("<Leave>", _on_leave)
            b.configure(command=_make_click(gi, b))
            b.pack(fill="x", padx=3, pady=1)
            group_btns.append(b)
        # 默认选中第 0 组
        first = grp_frame.winfo_children()[1] if len(grp_frame.winfo_children()) > 1 else None
        if isinstance(first, tk.Button):
            first.configure(bg="#1d3557", fg="#ffffff")
            group_btns_sel_ref.append(first)

    _render_group_buttons()
    _render_cmd_buttons(0)

    # ============================================================
    # Tab 3: 直连服务器 (HTTP API) — 账号/认证/车辆按键式表单
    # ============================================================
    tab3 = tk.Frame(nb, bg="#f7f7f9")
    nb.add(tab3, text="  \U0001F310 直连服务器 (不用进游戏聊天)  ")


    # ---- 顶部 URL 栏 ----
    cfg_bar = tk.Frame(tab3, bg="#f7f7f9")
    cfg_bar.pack(fill="x", padx=12, pady=(12, 4))
    tk.Label(cfg_bar, text="服务器 HTTP API 地址:",
             font=("Microsoft YaHei UI", 10, "bold"),
             bg="#f7f7f9", fg="#1d3557").pack(side="left")
    url_entry = tk.Entry(cfg_bar, font=("Consolas", 11), bg="#ffffff", fg="#0f172a",
                         relief="solid", bd=1)
    url_entry.insert(0, CFG.get("server_url") or "")
    url_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=3)

    def _save_url(*_):
        v = url_entry.get().strip()
        CFG["server_url"] = v
        _save_cfg()

    url_entry.bind("<FocusOut>", _save_url)
    url_entry.bind("<Return>", _save_url)

    status_dot = tk.Label(cfg_bar, text="⬤", font=("Arial", 14),
                          bg="#f7f7f9", fg="#9ca3af")
    status_dot.pack(side="left", padx=(8, 0))

    def _update_status_dot(text_ok, extra_msg=None):
        if text_ok and extra_msg and extra_msg.get("ok"):
            status_dot.configure(fg="#22c55e")   # 绿
        elif text_ok:
            status_dot.configure(fg="#f59e0b")   # 黄 (http ok 但业务 ok=false)
        else:
            status_dot.configure(fg="#ef4444")   # 红 (不通)

    def _do_ping():
        _save_url()
        net_ok, obj = api_ping()
        _update_status_dot(net_ok, obj)
        _log(obj.get("msg") or ("ping ok ver=" + str(obj.get("ver")) if obj.get("ok") else str(obj)))

    ttk.Button(cfg_bar, text="🗼 测试连通", width=10, command=_do_ping).pack(side="left", padx=6)
    ttk.Button(cfg_bar, text="💾 保存地址", width=10, command=_save_url).pack(side="left")

    # ---- 登录状态条 ----
    sess_bar = tk.Frame(tab3, bg="#eef2ff", bd=1, relief="solid")
    sess_bar.pack(fill="x", padx=12, pady=(2, 6))
    sess_label = tk.Label(sess_bar, text=" 状态: 未登录",
                          font=("Microsoft YaHei UI", 10, "bold"),
                          bg="#eef2ff", fg="#3730a3", anchor="w")
    sess_label.pack(fill="x", padx=8, pady=4)

    def _refresh_sess_label():
        tok = CFG.get("access_token") or ""
        u = CFG.get("username")
        if tok and u:
            lbl = f" 状态: 已登录 账号 {u}"
            if CFG.get("label"): lbl += f"  (身份: {CFG['label']}, 车辆上限: {CFG.get('limit','?')})"
            sess_label.configure(text=lbl, bg="#dcfce7", fg="#166534")
            sess_bar.configure(bg="#dcfce7")
        else:
            sess_label.configure(text=" 状态: 未登录 (请先注册或登录)",
                                 bg="#fef3c7", fg="#92400e")
            sess_bar.configure(bg="#fef3c7")

    # ---- 右下日志框 (所有操作的结果) ----
    log_wrap = tk.Frame(tab3, bg="#f7f7f9")
    log_wrap.pack(side="bottom", fill="x", padx=12, pady=(4, 8))
    log_lf = tk.LabelFrame(log_wrap, text=" 操作日志 (最新在最上): ",
                           font=("Microsoft YaHei UI", 9, "bold"),
                           bg="#f7f7f9", fg="#1d3557", bd=0, labelanchor="nw")
    log_lf.pack(fill="x")
    log_text = tk.Text(log_lf, height=7, font=("Consolas", 9),
                       bg="#0f172a", fg="#e2e8f0", relief="flat", bd=0,
                       wrap="word", state="disabled")
    log_text.pack(fill="x", padx=4, pady=4)

    def _log(msg):
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {str(msg)}\n"
        try:
            log_text.configure(state="normal")
            log_text.insert("1.0", line)
            log_text.delete("60.0", "end")
            log_text.configure(state="disabled")
        except Exception:
            pass

    def _log_response(obj, net_ok):
        if not net_ok or not obj:
            _log("❌ 网络错误: " + str(obj.get("msg") if isinstance(obj, dict) else obj))
            return
        ok_flag = obj.get("ok")
        msg = obj.get("msg") or "(无消息)"
        if ok_flag:
            _log("✅ " + msg)
        else:
            _log("❌ " + msg)
        data = obj.get("data")
        if isinstance(data, dict):
            # 保存 token / username / label / limit
            if data.get("access_token"):
                CFG["access_token"] = data["access_token"]
            if data.get("username"):
                CFG["username"] = data["username"]
            if data.get("label"):
                CFG["label"] = data["label"]
            if data.get("vehicle_limit") is not None:
                CFG["limit"] = data["vehicle_limit"]
            _save_cfg()
            _refresh_sess_label()
            # 展开有用字段 (不展开 access_token)
            shown = {k: (("***" if k == "access_token" else v)) for k, v in data.items()}
            _log("   ↳ data: " + json.dumps(shown, ensure_ascii=False))

    _refresh_sess_label()

    # ---- 表单主区域: Notebook 子 tab 按命令分 ----
    inner = ttk.Notebook(tab3)
    inner.pack(fill="both", expand=True, padx=10, pady=(4, 2))

    def _mk_tab(parent, title):
        f = tk.Frame(parent, bg="#ffffff")
        inner.add(f, text=title, padding=6)
        return f

    # --- 子Tab: 注册 ---
    tr = _mk_tab(inner, "  👤 注册账号  ")
    _row = 0
    def _grid_entry(master, label, show=None, default="", width=32):
        nonlocal _row
        tk.Label(master, text=label, font=("Microsoft YaHei UI", 10, "bold"),
                 bg="#ffffff", fg="#0f172a", anchor="e", width=14).grid(row=_row, column=0, padx=8, pady=4, sticky="e")
        ent = tk.Entry(master, font=("Consolas", 11), show=show,
                       bg="#fff", relief="solid", bd=1, width=width)
        ent.insert(0, default)
        ent.grid(row=_row, column=1, columnspan=2, padx=8, pady=4, sticky="we")
        _row += 1
        return ent

    e_r_user = _grid_entry(tr, "账号名: ")
    e_r_pass = _grid_entry(tr, "密码: ", show="●")
    e_r_name = _grid_entry(tr, "游戏名(可选): ",
                           default=CFG.get("last_player_name") or "")
    tr.columnconfigure(1, weight=1)

    def _do_register():
        u = e_r_user.get().strip(); pw = e_r_pass.get()
        if not u or not pw:
            _log("❌ 请填写账号名和密码"); return
        _save_url()
        CFG["last_player_name"] = e_r_name.get().strip()
        _save_cfg()
        body = {"username": u, "password": pw, "hwid": hwid,
                "player_name": CFG["last_player_name"]}
        net_ok, obj = api_post("/api/auth/register", body)
        _log_response(obj, net_ok)
        # 注册成功, 也顺手登一下 whoami 更新 label
        if net_ok and obj.get("ok"):
            net_ok2, obj2 = api_post("/api/auth/whoami", {})
            _log_response(obj2, net_ok2)

    tk.Button(tr, text="🚀 注册 (+ 自动登录)", font=("Microsoft YaHei UI", 11, "bold"),
              bg="#16a34a", fg="white", activebackground="#15803d", activeforeground="white",
              padx=16, pady=6, bd=0, command=_do_register
              ).grid(row=_row, column=1, pady=10, sticky="w", padx=8)

    # --- 子Tab: 登录 ---
    tl = _mk_tab(inner, "  🔐 登录  ")
    _row = 0
    e_l_user = _grid_entry(tl, "账号名: ", default=CFG.get("username") or "")
    e_l_pass = _grid_entry(tl, "密码: ", show="●")
    e_l_name = _grid_entry(tl, "游戏名(可选): ", default=CFG.get("last_player_name") or "")
    tl.columnconfigure(1, weight=1)

    def _do_login():
        u = e_l_user.get().strip(); pw = e_l_pass.get()
        if not u or not pw:
            _log("❌ 请填写账号名和密码"); return
        _save_url()
        CFG["last_player_name"] = e_l_name.get().strip()
        _save_cfg()
        body = {"username": u, "password": pw, "hwid": hwid,
                "player_name": CFG["last_player_name"]}
        # DEBUG: 打印实际发送的 body (密码脱敏)
        _safe_pw = pw[:3] + "***" + pw[-3:] if len(pw) >= 6 else "*" * len(pw)
        _log(f"[DEBUG] 发送登录请求: user={u!r} pwd={_safe_pw!r} pwd_len={len(pw)} body_keys={list(body.keys())}")
        net_ok, obj = api_post("/api/auth/login", body)
        _log(f"[DEBUG] API 响应: net_ok={net_ok} obj={json.dumps(obj, ensure_ascii=False)[:200] if isinstance(obj, dict) else obj}")
        if net_ok and obj.get("ok"):
            # 关键修复: 登录成功后立即保存 token 到 CFG (确保后续 whoami 能带 token)
            d = obj.get("data") or {}
            if d.get("access_token"):
                CFG["access_token"] = d["access_token"]
                CFG["username"] = d.get("username") or u
                _save_cfg()
                _log(f"[DEBUG] token 已保存: {d['access_token'][:10]}...")
            # 现在 CFG 已有新 token, whoami 能正确带 Authorization 头
            net_ok2, obj2 = api_post("/api/auth/whoami", {})
            _log(f"[DEBUG] whoami 响应: {json.dumps(obj2, ensure_ascii=False)[:200] if isinstance(obj2, dict) else obj2}")
            _log_response(obj, True)
            _log_response(obj2, net_ok2)
        else:
            # 登录失败: 清掉旧 token 防止后续请求混乱
            CFG.pop("access_token", None)
            _log_response(obj, net_ok)

    tk.Button(tl, text="🚀 登录", font=("Microsoft YaHei UI", 11, "bold"),
              bg="#1d4ed8", fg="white", activebackground="#1e40af", activeforeground="white",
              padx=24, pady=6, bd=0, command=_do_login
              ).grid(row=_row, column=1, pady=10, sticky="w", padx=8)

    def _do_logout():
        net_ok, obj = api_post("/api/auth/logout", {})
        if net_ok and obj.get("ok"):
            CFG.pop("access_token", None)
            CFG.pop("username", None)
            CFG.pop("label", None)
            CFG.pop("limit", None)
            _save_cfg()
            _refresh_sess_label()
        _log_response(obj, net_ok)

    tk.Button(tl, text="退出登录", font=("Microsoft YaHei UI", 10),
              bg="#64748b", fg="white", padx=10, pady=4, bd=0, command=_do_logout
              ).grid(row=_row, column=2, pady=10, sticky="w", padx=10)

    # --- 子Tab: /whoami 状态 ---
    tw = _mk_tab(inner, "  👀 我是谁  ")
    tw.columnconfigure(0, weight=1)
    who_frame = tk.Frame(tw, bg="#ffffff")
    who_frame.pack(fill="both", expand=True, padx=8, pady=8)
    who_text = tk.Label(who_frame,
                        text="点击【查询】按钮, 显示当前登录账号 / 身份 / 绑定 HWID / 车辆上限",
                        font=("Microsoft YaHei UI", 10),
                        bg="#ffffff", fg="#334155", justify="left", anchor="nw",
                        wraplength=560)
    who_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _do_whoami():
        body = {"hwid": hwid, "player_name": CFG.get("last_player_name") or ""}
        net_ok, obj = api_post("/api/auth/whoami", body)
        _log_response(obj, net_ok)
        if net_ok and obj.get("ok") and obj.get("data"):
            d = obj["data"]
            lines = ["✅ " + obj.get("msg", "") + "\n"]
            lines.append(f"已登录: {'是' if d.get('logged') else '否'}")
            if d.get("username"): lines.append(f"当前账号: {d['username']}")
            lines.append(f"当前身份: {d.get('label','?')}")
            lines.append(f"车辆上限: {d.get('vehicle_limit','?')} 辆")
            if d.get("is_admin"): lines.append("权限: 管理员 👑")
            if d.get("bound_ids"):
                lines.append("\n绑定的稳定ID:")
                for bid in d["bound_ids"]:
                    lines.append("  • " + bid)
            if d.get("last_login"):
                t = d["last_login"].get("time")
                try:
                    import datetime as _dtm
                    lines.append("\n上次登录: " + _dtm.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d %H:%M:%S"))
                except Exception:
                    lines.append("\n上次登录: t=" + str(t))
            who_text.configure(text="\n".join(lines), justify="left", anchor="nw")

    ttk.Button(who_frame, text="🔍 查询我是谁", command=_do_whoami).pack(pady=6)

    # --- 子Tab: /bmpid 绑定 HWID ---
    tb = _mk_tab(inner, "  🔗 绑定 HWID  ")
    _row = 0
    tk.Label(tb, text="本机 HWID:", font=("Microsoft YaHei UI", 10, "bold"),
             bg="#ffffff", fg="#0f172a", anchor="e", width=14).grid(row=0, column=0, padx=8, pady=4, sticky="e")
    e_b_id = tk.Entry(tb, font=("Consolas", 11), bg="#ecfdf5", fg="#065f46",
                      relief="solid", bd=1, width=40)
    e_b_id.insert(0, hwid)
    e_b_id.config(state="readonly")
    e_b_id.grid(row=0, column=1, columnspan=2, padx=8, pady=4, sticky="w")
    e_b_name = _grid_entry(tb, "游戏名(可选): ", default=CFG.get("last_player_name") or "")
    tb.columnconfigure(1, weight=1)

    def _do_bind():
        _save_url()
        CFG["last_player_name"] = e_b_name.get().strip()
        _save_cfg()
        body = {"hwid": hwid, "player_name": CFG["last_player_name"]}
        net_ok, obj = api_post("/api/hwid/bind", body)
        _log_response(obj, net_ok)

    tk.Button(tb, text="🔗 把本机 HWID 上报给服务器",
              font=("Microsoft YaHei UI", 11, "bold"),
              bg="#7c3aed", fg="white", activebackground="#6d28d9", activeforeground="white",
              padx=16, pady=6, bd=0, command=_do_bind
              ).grid(row=_row, column=1, pady=10, sticky="w", padx=8)

    # --- 子Tab: 车辆上限 ---
    tv = _mk_tab(inner, "  🚗 车辆上限  ")
    tv.columnconfigure(0, weight=1)
    v_frame = tk.Frame(tv, bg="#ffffff")
    v_frame.pack(fill="both", expand=True, padx=8, pady=8)
    v_text = tk.Label(v_frame,
                      text="点击【查询】按钮: 按当前 HWID + 登录状态 判断车辆上限 (1/5/999)",
                      font=("Microsoft YaHei UI", 10),
                      bg="#ffffff", fg="#334155", justify="left", anchor="nw",
                      wraplength=560)
    v_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _do_limit():
        body = {"hwid": hwid, "player_name": CFG.get("last_player_name") or ""}
        net_ok, obj = api_post("/api/vehicle/limit", body)
        _log_response(obj, net_ok)
        if net_ok and obj.get("ok") and obj.get("data"):
            d = obj["data"]
            lines = ["✅ " + obj.get("msg", "") + "\n"]
            lines.append(f"当前身份: {d.get('label','?')}")
            lines.append(f"车辆上限: {d.get('vehicle_limit','?')} 辆")
            if d.get("username"): lines.append(f"关联账号: {d['username']}")
            lines.append(f"已登录: {'是' if d.get('logged') else '否'}")
            lines.append("\n规则:")
            lines.append("  · 未认证 = 1 辆 (没绑定 HWID + 没登录账号)")
            lines.append("  · 认证用户 = 5 辆 (绑定 HWID + 登录账号)")
            lines.append("  · 管理员 = 999 辆 (不受限)")
            v_text.configure(text="\n".join(lines), justify="left", anchor="nw")

    ttk.Button(v_frame, text="🚗 查询车辆上限", command=_do_limit).pack(pady=6)

    # --- 子Tab: 聊天 → 公屏广播 ---
    tc = _mk_tab(inner, "  💬 公屏聊天  ")
    tc.columnconfigure(1, weight=1)
    chat_top = tk.Frame(tc, bg="#ffffff")
    chat_top.pack(fill="x", padx=8, pady=(8, 4))

    tk.Label(chat_top,
             text="在此处输入消息 → 服务器 1 秒内广播到游戏公屏\n[玩家名]: 你的消息 (无需进游戏)",
             font=("Microsoft YaHei UI", 9),
             bg="#ffffff", fg="#475569", justify="left", anchor="w").pack(fill="x")

    # 名字 + 消息 输入行
    input_row = tk.Frame(tc, bg="#ffffff")
    input_row.pack(fill="x", padx=8, pady=(0, 4))
    tk.Label(input_row, text="显示名:", font=("Microsoft YaHei UI", 10, "bold"),
             bg="#ffffff", fg="#0f172a", width=8).grid(row=0, column=0, padx=4, pady=4, sticky="e")
    e_chat_name = tk.Entry(input_row, font=("Microsoft YaHei UI", 11),
                            bg="#fff", relief="solid", bd=1, width=14)
    e_chat_name.insert(0, CFG.get("last_player_name") or CFG.get("username") or "")
    e_chat_name.grid(row=0, column=1, padx=4, pady=4, sticky="w")

    tk.Label(input_row, text="消息:", font=("Microsoft YaHei UI", 10, "bold"),
             bg="#ffffff", fg="#0f172a", width=6).grid(row=1, column=0, padx=4, pady=4, sticky="e")
    e_chat_msg = tk.Entry(input_row, font=("Microsoft YaHei UI", 11),
                          bg="#fffbeb", relief="solid", bd=1)
    e_chat_msg.grid(row=1, column=1, columnspan=3, padx=4, pady=4, sticky="we")
    input_row.columnconfigure(1, weight=1)

    # 发送按钮 (点击 + Enter 触发)
    def _do_send_chat():
        name = e_chat_name.get().strip() or "guest"
        text = e_chat_msg.get().strip()
        if not text:
            _log("❌ 消息不能为空"); return
        _save_url()
        CFG["last_player_name"] = name
        _save_cfg()
        body = {"player_name": name, "message": text, "hwid": hwid}
        net_ok, obj = api_post("/api/chat/send", body)
        _log_response(obj, net_ok)
        if net_ok and obj.get("ok"):
            e_chat_msg.delete(0, "end")   # 发送成功清空输入框

    tk.Button(input_row, text="📢 广播", font=("Microsoft YaHei UI", 11, "bold"),
              bg="#dc2626", fg="white", activebackground="#b91c1c", activeforeground="white",
              padx=20, pady=4, bd=0, command=_do_send_chat
              ).grid(row=1, column=4, padx=4, pady=4, sticky="e")
    input_row.columnconfigure(4, weight=0)

    # 回车也触发发送
    e_chat_msg.bind("<Return>", lambda e: _do_send_chat())

    # 提示
    tk.Label(tc,
             text="限制: 每分钟最多 6 条 / 单条 200 字符; 服务器会过滤控制字符; \n"
                  "未登录时显示名随便填, 已登录会带 [Bridge:账号名] 前缀",
             font=("Microsoft YaHei UI", 8),
             bg="#ffffff", fg="#94a3b8", justify="left", anchor="w"
             ).pack(fill="x", padx=8, pady=(4, 0))

    # 队列状态 (调试用)
    chat_debug = tk.Label(tc, text="", font=("Consolas", 8),
                          bg="#ffffff", fg="#64748b", justify="left", anchor="w")
    chat_debug.pack(fill="x", padx=8, pady=(4, 4))

    def _refresh_chat_queue():
        net_ok, obj = api_ping()  # 先检查服务器活着
        if not (net_ok and obj.get("ok")):
            chat_debug.configure(text="⏳ 服务器未连接")
            return
        # GET /api/chat/queue
        import urllib.request as _ur, urllib.error as _ue
        base = (CFG.get("server_url") or "").strip().rstrip("/")
        if not base: return
        url = base + "/api/chat/queue"
        try:
            with _ur.urlopen(url, timeout=3) as resp:
                raw = resp.read() or b"{}"
                o = json.loads(raw.decode("utf-8"))
                if o.get("ok") and o.get("data"):
                    d = o["data"]
                    chat_debug.configure(text=f"📊 队列: 共 {d.get('total',0)} 条, 待发送 {d.get('unsent',0)} 条")
        except Exception:
            pass

    ttk.Button(input_row, text="🔄 刷新队列", command=_refresh_chat_queue
               ).grid(row=0, column=4, padx=4, pady=4, sticky="e")

    # 首次启动: 自动 ping 更新状态点
    try:
        root.after(500, _do_ping)
    except Exception:
        pass

    # 底部状态栏 (Notebook 外面, 三个 Tab 共享)
    status_f = tk.Frame(root, bg="#ffffff", bd=1, relief="solid")
    status_f.pack(fill="x", padx=14, pady=(0, 14))
    tk.Label(status_f,
             text=f"\U0001F310 本地 HTTP 服务: http://127.0.0.1:{HTTP_PORT}     → {http_msg}",
             font=("Microsoft YaHei UI", 9),
             bg="#ffffff", fg="#1d3557", anchor="w").pack(fill="x", padx=12, pady=6)
    tk.Label(status_f,
             text=f"GET  http://127.0.0.1:{HTTP_PORT}/hwid    → BMPHWID.zip 自动拉取 (不需要任何操作)",
             font=("Consolas", 8),
             bg="#ffffff", fg="#555", anchor="w").pack(fill="x", padx=12, pady=(0, 6))

    try:
        root.mainloop()
    finally:
        try:
            if httpd: httpd.shutdown()
        except Exception:
            pass


def _run_cli_print(hwid, sources_list):
    """无 tkinter 时的 CLI 模式"""
    payload = {
        "ok": True,
        "name": HTTP_NAME,
        "ver": PAYLOAD_VER,
        "hwid": hwid,
        "sources": sources_list,
        "command": f"/bmpid {hwid}",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ============================================================
#  Entry
# ============================================================
def main():
    # 1) 采集
    sources_list = collect_sources()
    hwid = build_stable_hwid(sources_list)

    # 2) --cli 模式 (脚本用)
    if len(sys.argv) >= 2 and (sys.argv[1] == "--cli" or sys.argv[1] == "-c"):
        _run_cli_print(hwid, sources_list)
        return

    # 3) GUI 模式
    gui_ok = False
    if os.environ.get("BMPHWID_FORCE_CLI") != "1":
        try:
            import tkinter  # noqa: F401
            gui_ok = True
        except Exception:
            gui_ok = False
    if gui_ok:
        _run_gui(hwid, sources_list)
    else:
        _run_cli_print(hwid, sources_list)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 最后兜底 CLI
        try:
            ss = collect_sources()
            hw = build_stable_hwid(ss)
            sys.stdout.write(json.dumps({
                "ok": True, "ver": PAYLOAD_VER, "hwid": hw, "sources": ss,
                "command": f"/bmpid {hw}",
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        sys.exit(0)
