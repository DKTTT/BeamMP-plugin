# -*- coding: utf-8 -*-
"""
=====================================================================
#  BMP_Admin  v1.0  —  BeamMP 服务器管理员控制台
---------------------------------------------------------------------
  独立 GUI 工具, 通过 HTTP API 管理服务器:
    • 账号列表 / 删除 / 重置密码 / 切换管理员 / 解绑 HWID
    • 封禁 / 解封 (账号 / HWID / IP)
    • 聊天队列查看 / 管理员广播 / 清空
    • 服务器统计 (账号数 / 活跃数 / 封禁数 / 队列)

  依赖: 只用 Python 标准库 (tkinter + urllib)
  用法: py BMP_Admin.py 或双击 dist/BMP_Admin.exe
=====================================================================
"""

import json, os, sys, time, hashlib, threading, tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import urllib.request, urllib.error
from datetime import datetime

VERSION = "BMP-Admin/v1.2.0"
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False) else __file__)),
    "bmp_admin_config.json"
)

# ================================================================
#  让阻塞 HTTP 不冻结界面
#  Tk 只能在主线程碰, 所以「把请求丢到后台线程 + 主线程泵事件」是这里
#  唯一不需要把 15 个处理函数全部改成回调式的做法:
#    - 窗口保持响应 (拖动/重绘/关闭)
#    - 超时不再等于「整个程序假死」
#  嵌套用一个深度计数挡住, 避免事件回调里再发请求导致无限递归。
# ================================================================
_PUMP_ROOT = None
_PUMP_DEPTH = 0


def _run_http(fn, timeout=8):
    """在后台线程执行 fn()，期间泵 Tk 事件；返回 fn() 的结果或原样抛出异常。"""
    global _PUMP_DEPTH
    root = _PUMP_ROOT
    if root is None or _PUMP_DEPTH > 0:
        return fn()
    box = {}

    def worker():
        try:
            box["v"] = (True, fn())
        except BaseException as e:          # noqa: BLE001 - 原样传回主线程
            box["v"] = (False, e)

    t = threading.Thread(target=worker, daemon=True)
    _PUMP_DEPTH += 1
    try:
        t.start()
        deadline = time.time() + (timeout or 8) + 5
        while t.is_alive() and time.time() < deadline:
            try:
                root.update()
            except Exception:
                break
            t.join(0.01)
    finally:
        _PUMP_DEPTH -= 1
    if t.is_alive():
        raise TimeoutError("请求超时")
    ok, val = box.get("v", (False, TimeoutError("请求超时")))
    if not ok:
        raise val
    return val

# ---- 配置 ----
CFG = {
    "server_url": "127.0.0.1:12124",
    "access_token": "",
    "username": "",
}

def _load_cfg():
    global CFG
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                CFG.update({k: v for k, v in d.items() if k in CFG})
    except Exception:
        pass

def _hash_token(tok):
    """本地存储时只存 token hash"""
    if not tok: return ""
    return hashlib.sha256(tok.encode()).hexdigest()

def _save_cfg():
    try:
        # 直接存 token 明文: 旧实现把 access_token 换成 _token_hash 而 _load_cfg 从不读它,
        # 等于每次启动都是未登录, 自动登录永远不可能生效
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(CFG), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _norm_url(base):
    base = (base or "").strip().rstrip("/")
    if not base:
        return "", "请先填入服务器 API 地址"
    if not (base.startswith("http://") or base.startswith("https://")):
        base = "http://" + base
    return base, ""

def api_post(path, body=None, timeout=8):
    base, err = _norm_url(CFG.get("server_url") or "")
    if err:
        return False, {"ok": False, "msg": err}
    url = base + path
    token = CFG.get("access_token") or ""
    data = b"{}" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        # 后台线程发请求 + 主线程泵事件 -> 服务器不可达时窗口也不会假死
        parsed = _run_http(lambda: json.loads(urllib.request.urlopen(req, timeout=timeout).read()),
                           timeout)
        return True, _fill_msg(parsed)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            if not isinstance(body, dict):
                body = {"ok": False, "msg": f"HTTP {e.code}"}
        except Exception:
            body = {"ok": False, "msg": f"HTTP {e.code}"}
        # 401 必须让界面回到未登录状态, 否则 token 失效后所有操作都在报错
        if e.code == 401:
            body["auth_expired"] = True
        return False, _fill_msg(body)
    except Exception as e:
        return False, {"ok": False, "msg": str(e)}

def _fill_msg(result, fallback="请求失败"):
    """API 的失败响应是 {ok:false,error:...}, 旧界面只读 msg, 于是弹窗显示原始 dict"""
    if isinstance(result, dict):
        if not result.get("msg"):
            result["msg"] = result.get("error") or fallback
    return result

def api_get(path, timeout=6):
    base, err = _norm_url(CFG.get("server_url") or "")
    if err:
        return False, {"ok": False, "msg": err}
    url = base + path
    try:
        parsed = _run_http(lambda: json.loads(urllib.request.urlopen(url, timeout=timeout).read()),
                           timeout)
        return True, _fill_msg(parsed)
    except Exception as e:
        return False, {"ok": False, "msg": str(e)}

def fmt_time(ts):
    if not ts: return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

# ================================================================
#  主应用
# ================================================================
class AdminApp:
    def __init__(self, root):
        self.root = root
        root.title(f"BeamMP 管理员控制台 {VERSION}")
        root.geometry("1100x720")
        root.configure(bg="#f0f2f5")
        root.minsize(900, 600)

        _load_cfg()

        # 让 api_post/api_get 能泵 Tk 事件 (见 _run_http)
        global _PUMP_ROOT
        _PUMP_ROOT = root

        # iid -> 完整在线玩家记录 (表格里的 BeamID 是截断显示值, 不能反查)
        self._online_rows = {}
        self._accounts = {}
        self._online_job = None
        self._refreshing_online = False

        self._build_ui()
        self._refresh_status()
        try:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            pass

    def _on_close(self):
        try:
            if getattr(self, "_online_job", None):
                self.root.after_cancel(self._online_job)
        except Exception:
            pass
        self.root.destroy()

    # ---- UI 构建 ----
    def _build_ui(self):
        # 顶部工具栏
        top = tk.Frame(self.root, bg="#1e293b", height=50)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        tk.Label(top, text="⚙️  BeamMP 服务器管理员控制台",
                 font=("Microsoft YaHei UI", 14, "bold"),
                 bg="#1e293b", fg="#f1f5f9").pack(side="left", padx=15)

        # URL
        tk.Label(top, text="API地址:", bg="#1e293b", fg="#94a3b8",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(20, 2))
        self.url_var = tk.StringVar(value=CFG.get("server_url", ""))
        # 旧实现只有 [_do_ping] 才把输入框写回 CFG, 于是改了地址后登录/刷新/封禁
        # 还在打旧服务器. 这里用 trace 保证任何时刻输入框都同步到 CFG.
        try:
            self.url_var.trace_add("write",
                                   lambda *_: CFG.__setitem__("server_url", self.url_var.get().strip()))
        except Exception:
            pass
        url_entry = tk.Entry(top, textvariable=self.url_var, width=30,
                             font=("Consolas", 10), bg="#334155", fg="#e2e8f0",
                             insertbackground="white", relief="flat")
        url_entry.pack(side="left", ipady=3)

        tk.Button(top, text="🔌 测试", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=8,
                  command=self._do_ping).pack(side="left", padx=5)

        # 登录/登出
        self.btn_login = tk.Button(top, text="🔑 管理员登录", bg="#10b981", fg="white",
                                    font=("Microsoft YaHei UI", 9, "bold"), relief="flat", padx=10,
                                    command=self._do_login)
        self.btn_login.pack(side="right", padx=10)

        self.lbl_user = tk.Label(top, text="", bg="#1e293b", fg="#fbbf24",
                                  font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_user.pack(side="right", padx=5)

        # 状态条
        self.status_bar = tk.Frame(self.root, bg="#0f172a", height=30)
        self.status_bar.pack(fill="x", side="bottom")
        self.status_label = tk.Label(self.status_bar, text="就绪",
                                      bg="#0f172a", fg="#94a3b8",
                                      font=("Microsoft YaHei UI", 9), anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=4)

        # Notebook
        style = ttk.Style()
        style.configure("TNotebook", background="#f0f2f5")
        style.configure("TNotebook.Tab", padding=[15, 8],
                        font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", font=("Consolas", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_accounts_tab()
        self._build_online_tab()
        self._build_ban_tab()
        self._build_chat_tab()
        self._build_stats_tab()

        # 自动登录 (如果有 token)
        if CFG.get("access_token") and CFG.get("username"):
            self.lbl_user.config(text=f"👤 {CFG['username']} (管理员)")
            self.btn_login.config(text="🔒 登出", bg="#ef4444", command=self._do_logout)
            self.url_var.set(CFG.get("server_url", ""))

    # ---- Tab 1: 账号管理 ----
    def _build_accounts_tab(self):
        tab = tk.Frame(self.nb, bg="#f0f2f5")
        self.nb.add(tab, text="👥 账号管理")

        # 工具栏
        tb = tk.Frame(tab, bg="#f0f2f5")
        tb.pack(fill="x", padx=8, pady=(8, 4))

        tk.Button(tb, text="🔄 刷新", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._refresh_accounts).pack(side="left", padx=2)
        tk.Button(tb, text="🗑️ 删除账号", bg="#ef4444", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._delete_account).pack(side="left", padx=2)
        tk.Button(tb, text="🔑 重置密码", bg="#f59e0b", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._reset_password).pack(side="left", padx=2)
        self.btn_admin = tk.Button(tb, text="👑 设为管理员", bg="#8b5cf6", fg="white",
                                    font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                                    command=self._toggle_admin)
        self.btn_admin.pack(side="left", padx=2)
        tk.Button(tb, text="🔗 解绑HWID", bg="#06b6d4", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._unbind_hwid).pack(side="left", padx=2)

        # Treeview
        cols = ("username", "is_admin", "bind_count", "last_login", "register_time")
        self.tv_accounts = ttk.Treeview(tab, columns=cols, show="headings", height=15)
        self.tv_accounts.heading("username", text="账号")
        self.tv_accounts.heading("is_admin", text="管理员")
        self.tv_accounts.heading("bind_count", text="绑定数")
        self.tv_accounts.heading("last_login", text="最后登录")
        self.tv_accounts.heading("register_time", text="注册时间")
        self.tv_accounts.column("username", width=140)
        self.tv_accounts.column("is_admin", width=60, anchor="center")
        self.tv_accounts.column("bind_count", width=70, anchor="center")
        self.tv_accounts.column("last_login", width=150)
        self.tv_accounts.column("register_time", width=150)

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.tv_accounts.yview)
        self.tv_accounts.configure(yscrollcommand=vsb.set)
        self.tv_accounts.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)

        # 详情面板 (可滚动 Text, 防截断)
        detail_frame = tk.LabelFrame(tab, text="账号详情 / 绑定 HWID",
                                      font=("Microsoft YaHei UI", 9, "bold"),
                                      bg="#f0f2f5", fg="#334155")
        detail_frame.pack(fill="x", padx=8, pady=4)

        self.txt_detail = tk.Text(detail_frame, height=8, wrap="word",
                                    font=("Consolas", 9),
                                    bg="white", fg="#334155",
                                    relief="solid", bd=1,
                                    state="disabled", cursor="arrow")
        self.txt_detail.pack(fill="x", padx=8, pady=8)
        self.txt_detail.insert("1.0", "选中账号查看详情")
        self.txt_detail.config(state="disabled")

        self.tv_accounts.bind("<<TreeviewSelect>>", self._on_account_select)

    # ---- Tab 2: 封禁管理 ----
    def _build_ban_tab(self):
        tab = tk.Frame(self.nb, bg="#f0f2f5")
        self.nb.add(tab, text="🚫 封禁管理")

        # 封禁操作
        frm1 = tk.LabelFrame(tab, text="添加封禁",
                              font=("Microsoft YaHei UI", 9, "bold"),
                              bg="#f0f2f5")
        frm1.pack(fill="x", padx=8, pady=8)

        tk.Label(frm1, text="类型:", bg="#f0f2f5").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        self.ban_type = ttk.Combobox(frm1, values=["account", "hwid", "ip"], width=10, state="readonly")
        self.ban_type.set("account")
        self.ban_type.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        tk.Label(frm1, text="值:", bg="#f0f2f5").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        self.ban_val = tk.Entry(frm1, width=20)
        self.ban_val.grid(row=0, column=3, padx=4, pady=4)

        tk.Label(frm1, text="天数:", bg="#f0f2f5").grid(row=0, column=4, padx=4, pady=4, sticky="e")
        self.ban_days = tk.Entry(frm1, width=6)
        self.ban_days.insert(0, "0")
        self.ban_days.grid(row=0, column=5, padx=4, pady=4, sticky="w")
        tk.Label(frm1, text="(0=永久)", bg="#f0f2f5", fg="#94a3b8").grid(row=0, column=6, padx=2, pady=4, sticky="w")

        tk.Label(frm1, text="原因:", bg="#f0f2f5").grid(row=1, column=0, padx=4, pady=4, sticky="e")
        self.ban_reason = tk.Entry(frm1, width=30)
        self.ban_reason.grid(row=1, column=1, padx=4, pady=4, columnspan=3, sticky="w")

        tk.Button(frm1, text="🚫 封禁", bg="#ef4444", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._do_ban).grid(row=1, column=4, padx=8, pady=4, columnspan=2)

        # 封禁列表
        frm2 = tk.LabelFrame(tab, text="当前封禁列表",
                              font=("Microsoft YaHei UI", 9, "bold"),
                              bg="#f0f2f5")
        frm2.pack(fill="both", expand=True, padx=8, pady=4)

        cols2 = ("type", "value", "reason", "expires", "time")
        self.tv_ban = ttk.Treeview(frm2, columns=cols2, show="headings", height=12)
        for c, t, w in [("type", "类型", 70), ("value", "值", 180),
                         ("reason", "原因", 200), ("expires", "到期", 100),
                         ("time", "时间", 130)]:
            self.tv_ban.heading(c, text=t)
            self.tv_ban.column(c, width=w)
        vsb2 = ttk.Scrollbar(frm2, orient="vertical", command=self.tv_ban.yview)
        self.tv_ban.configure(yscrollcommand=vsb2.set)
        self.tv_ban.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb2.pack(side="left", fill="y", pady=4)

        btn_bar = tk.Frame(frm2, bg="#f0f2f5")
        btn_bar.pack(fill="x", pady=4)
        tk.Button(btn_bar, text="🔄 刷新", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._refresh_ban).pack(side="left", padx=4)
        tk.Button(btn_bar, text="✅ 解封选中", bg="#10b981", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._do_unban).pack(side="left", padx=4)

    # ---- Tab 3: 在线玩家 ----
    def _build_online_tab(self):
        tab = tk.Frame(self.nb, bg="#f0f2f5")
        self.nb.add(tab, text="🎮 在线玩家")

        # 控制栏
        ctrl = tk.Frame(tab, bg="#f0f2f5")
        ctrl.pack(fill="x", padx=8, pady=4)

        tk.Label(ctrl, text="踢人原因:", bg="#f0f2f5").pack(side="left", padx=4)
        self.kick_reason = tk.Entry(ctrl, width=25)
        self.kick_reason.insert(0, "违规行为")
        self.kick_reason.pack(side="left", padx=4)

        tk.Button(ctrl, text="🔄 刷新列表", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._refresh_online).pack(side="right", padx=4)

        # 在线列表
        frm = tk.LabelFrame(tab, text="当前在线玩家 (双击行踢出, 右键拉黑)",
                            font=("Microsoft YaHei UI", 9, "bold"),
                            bg="#f0f2f5")
        frm.pack(fill="both", expand=True, padx=8, pady=4)

        cols = ("pid", "name", "role", "auth", "account", "beam_id", "vehicles", "online")
        self.tv_online = ttk.Treeview(frm, columns=cols, show="headings", height=15)
        for c, t, w in [("pid", "ID", 50), ("name", "玩家名", 120),
                         ("role", "身份", 60), ("auth", "认证", 50),
                         ("account", "绑定账号", 100), ("beam_id", "BeamID", 150),
                         ("vehicles", "车辆", 50), ("online", "在线", 70)]:
            self.tv_online.heading(c, text=t)
            self.tv_online.column(c, width=w)
        vsb = ttk.Scrollbar(frm, orient="vertical", command=self.tv_online.yview)
        self.tv_online.configure(yscrollcommand=vsb.set)
        self.tv_online.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)

        # 绑定双击踢出 (只在数据单元格上生效, 双击表头/空白区不应踢人)
        self.tv_online.bind("<Double-1>", self._on_online_double_click)

        # 操作按钮 (先 pack 底部按钮栏, 否则列表 expand 后会把它挤掉)
        btn_bar = tk.Frame(frm, bg="#f0f2f5")
        btn_bar.pack(side="bottom", fill="x", pady=4)
        tk.Button(btn_bar, text="👢 踢出选中", bg="#ef4444", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._kick_selected).pack(side="left", padx=4)
        tk.Button(btn_bar, text="🚫 拉黑选中", bg="#f97316", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._ban_selected).pack(side="left", padx=4)

        # 自动刷新
        self._auto_refresh_online()

    def _on_online_double_click(self, event):
        if self.tv_online.identify_region(event.x, event.y) != "cell":
            return
        self._kick_selected()

    def _auto_refresh_online(self):
        """每 5 秒自动刷新在线列表.
        定时器必须放在 finally 里: 旧实现一旦某次刷新抛异常, 就再也不排下一次,
        列表永远停在旧数据 (且旧玩家 ID 仍然可用, 可能踢错人)。"""
        try:
            self._refresh_online(silent=True)
        except Exception:
            pass
        finally:
            try:
                self._online_job = self.root.after(5000, self._auto_refresh_online)
            except Exception:
                self._online_job = None

    def _refresh_online(self, silent=False):
        if not self._require_admin(silent=silent): return
        # HTTP 现在会在泵事件时让其它回调插进来, 用标记挡住重入
        if self._refreshing_online:
            return
        self._refreshing_online = True
        try:
            self._refresh_online_impl(silent)
        finally:
            self._refreshing_online = False

    def _refresh_online_impl(self, silent=False):
        ok, r = api_post("/api/admin/players")
        if not ok or not r.get("ok"):
            if not silent:
                self._show_error(r)
            else:
                # 刷新失败时清空旧数据: BeamMP 会复用 playerID, 留着旧行很容易踢错人
                for item in self.tv_online.get_children():
                    self.tv_online.delete(item)
                self._online_rows = {}
            return
        # 刷新会重建所有行, 先记住当前选中的 playerID, 重建后恢复
        keep_pids = set()
        for iid in self.tv_online.selection():
            rec = getattr(self, "_online_rows", {}).get(iid)
            if rec:
                keep_pids.add(str(rec.get("playerID", "")))
        for item in self.tv_online.get_children():
            self.tv_online.delete(item)
        self._online_rows = {}
        now = int(time.time())
        for p in r.get("data", []):
            if not isinstance(p, dict):
                continue
            pid = p.get("playerID", "")
            name = p.get("name", "")
            role = p.get("role", "")
            auth = "✅" if p.get("is_authenticated") else "❌"
            acct = p.get("bind_account", "")
            beam_id_full = p.get("beam_id", "") or ""
            beam_id = beam_id_full[:40]      # 仅用于显示
            veh = p.get("vehicle_count", 0)
            jt = p.get("join_time", 0)
            online_min = (now - jt) // 60 if jt else 0
            online_text = f"{online_min}分" if online_min < 60 else f"{online_min // 60}时{online_min % 60}分"
            iid = self.tv_online.insert("", "end", values=(
                pid, name, role, auth, acct, beam_id, veh, online_text))
            # 保存完整记录: 表格里的是截断后的 BeamID, 不能反查
            self._online_rows[iid] = p
            if str(pid) in keep_pids:
                self.tv_online.selection_add(iid)
        count = len(r.get("data", []))
        if not silent:
            if count == 0:
                self._set_status("⚠️ 在线 0 人 — 请确认 BeamMP 服务器已启动并加载 BMP Login 插件", "#fbbf24")
            else:
                self._set_status(f"✅ 在线 {count} 人", "#10b981")

    def _kick_selected(self):
        if not self._require_admin():
            return
        sel = self.tv_online.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择玩家")
            return
        reason = self.kick_reason.get().strip() or "违规行为"
        # 先快照, 后面弹窗会跑嵌套事件循环, 期间自动刷新可能删掉树里的行
        rows = [(s, getattr(self, "_online_rows", {}).get(s)) for s in sel]
        for _s, rec in rows:
            if not rec:
                continue
            pid = rec.get("playerID")
            pname = rec.get("name", "")
            if not messagebox.askyesno("确认", f"确定要踢出玩家 {pname} (ID={pid})?\n原因: {reason}"):
                return
            ok, r = api_post("/api/admin/kick", {"playerID": pid, "reason": reason})
            if ok and r.get("ok"):
                self._set_status(f"✅ {r.get('msg')}", "#10b981")
            else:
                self._show_error(r)
        self._refresh_online(silent=True)

    def _ban_selected(self):
        if not self._require_admin():
            return
        sel = self.tv_online.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择玩家")
            return
        rows = [(s, getattr(self, "_online_rows", {}).get(s)) for s in sel]
        for _s, rec in rows:
            if not rec:
                continue
            pid = rec.get("playerID")
            pname = rec.get("name", "")
            bind_account = rec.get("bind_account") or ""
            beam_id = rec.get("beam_id") or ""
            is_auth = bool(rec.get("is_authenticated"))

            if is_auth and bind_account:
                # 认证玩家: 按账号封禁
                ban_type = "account"
                ban_value = bind_account
                ban_desc = f"账号 {bind_account}"
            elif beam_id.startswith("HWID:"):
                # 有 HWID: 按设备封禁 (用完整 BeamID 最后一段, 绝不使用表格里的截断值)
                ban_type = "hwid"
                ban_value = beam_id.split(":")[-1] if ":" in beam_id else beam_id
                ban_desc = f"HWID {ban_value[:20]}..."
            elif beam_id.startswith("IP:"):
                # 有 IP: 按 IP 封禁
                ban_type = "ip"
                ban_value = beam_id[3:]
                ban_desc = f"IP {ban_value}"
            else:
                # 兜底: 按玩家名封禁 (游客)
                ban_type = "name"
                ban_value = pname
                ban_desc = f"玩家名 {pname}"
            
            duration = simpledialog.askinteger("封禁时长",
                f"封禁玩家 {pname} (ID={pid})\n类型: {ban_desc}\n\n封禁天数 (0=永久, 1-365):",
                initialvalue=0, minvalue=0, maxvalue=365)
            if duration is None:
                return
            
            dur_text = "永久" if duration == 0 else f"{duration}天"
            if not messagebox.askyesno("确认",
                f"确定要拉黑 {pname}?\n类型: {ban_desc}\n时长: {dur_text}\n(3 秒内踢出)"):
                return
            
            ok, r = api_post("/api/admin/ban", {
                "type": ban_type, "value": ban_value,
                "reason": f"管理员从在线列表拉黑 ({dur_text})",
                "duration_days": duration
            })
            if ok and r.get("ok"):
                self._set_status(f"✅ {r.get('msg')}", "#10b981")
            else:
                self._show_error(r)
        self._refresh_online(silent=True)

    # ---- Tab 4: 聊天广播 ----
    def _build_chat_tab(self):
        tab = tk.Frame(self.nb, bg="#f0f2f5")
        self.nb.add(tab, text="📢 聊天广播")

        # 管理员广播
        frm1 = tk.LabelFrame(tab, text="管理员公屏广播",
                              font=("Microsoft YaHei UI", 9, "bold"),
                              bg="#f0f2f5")
        frm1.pack(fill="x", padx=8, pady=8)

        tk.Label(frm1, text="显示名:", bg="#f0f2f5").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        self.chat_name = tk.Entry(frm1, width=20)
        self.chat_name.insert(0, "管理员")
        self.chat_name.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        tk.Label(frm1, text="消息:", bg="#f0f2f5").grid(row=1, column=0, padx=4, pady=4, sticky="ne")
        self.chat_msg = tk.Text(frm1, height=3, width=60, font=("Consolas", 10))
        self.chat_msg.grid(row=1, column=1, padx=4, pady=4, sticky="w")

        tk.Button(frm1, text="📢 广播到公屏", bg="#8b5cf6", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padx=15,
                  command=self._admin_chat_send).grid(row=1, column=2, padx=10, pady=4)

        # 聊天队列
        frm2 = tk.LabelFrame(tab, text="聊天队列 (所有待发/已发消息)",
                              font=("Microsoft YaHei UI", 9, "bold"),
                              bg="#f0f2f5")
        frm2.pack(fill="both", expand=True, padx=8, pady=4)

        cols3 = ("id", "time", "name", "text", "sent")
        self.tv_chat = ttk.Treeview(frm2, columns=cols3, show="headings", height=10)
        for c, t, w in [("id", "ID", 100), ("time", "时间", 140),
                         ("name", "发送者", 150), ("text", "消息内容", 350),
                         ("sent", "状态", 60)]:
            self.tv_chat.heading(c, text=t)
            self.tv_chat.column(c, width=w)
        vsb3 = ttk.Scrollbar(frm2, orient="vertical", command=self.tv_chat.yview)
        self.tv_chat.configure(yscrollcommand=vsb3.set)
        self.tv_chat.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb3.pack(side="left", fill="y", pady=4)

        btn_bar = tk.Frame(frm2, bg="#f0f2f5")
        btn_bar.pack(fill="x", pady=4)
        tk.Button(btn_bar, text="🔄 刷新", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=self._refresh_chat).pack(side="left", padx=4)
        tk.Button(btn_bar, text="🗑️ 清空已发送", bg="#f59e0b", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=lambda: self._clear_chat(True)).pack(side="left", padx=4)
        tk.Button(btn_bar, text="🗑️ 清空全部", bg="#ef4444", fg="white",
                  font=("Microsoft YaHei UI", 9), relief="flat", padx=10,
                  command=lambda: self._clear_chat(False)).pack(side="left", padx=4)

    # ---- Tab 4: 服务器统计 ----
    def _build_stats_tab(self):
        tab = tk.Frame(self.nb, bg="#f0f2f5")
        self.nb.add(tab, text="📊 服务器统计")

        self.stats_frame = tk.Frame(tab, bg="#f0f2f5")
        self.stats_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.stats_labels = {}
        self._render_stats_placeholder()

        tk.Button(tab, text="🔄 刷新统计", bg="#3b82f6", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padx=20,
                  command=self._refresh_stats).pack(pady=10)

    def _render_stats_placeholder(self):
        for w in self.stats_frame.winfo_children():
            w.destroy()
        items = [
            ("total_accounts", "总账号数", "👥"),
            ("admin_count", "管理员数", "👑"),
            ("authenticated_count", "已认证数", "✅"),
            ("active_24h", "24h活跃", "⏰"),
            ("guestmap_entries", "GuestMap条目", "🗺️"),
            ("ban_accounts", "封禁账号", "🚫"),
            ("ban_devices", "封禁设备", "🔒"),
            ("chat_queue_total", "聊天队列", "💬"),
            ("chat_queue_unsent", "待发送", "📨"),
        ]
        self.stats_labels = {}
        for i, (key, label, icon) in enumerate(items):
            row, col = divmod(i, 3)
            card = tk.Frame(self.stats_frame, bg="white", bd=1, relief="solid")
            card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            self.stats_frame.grid_columnconfigure(col, weight=1)

            tk.Label(card, text=icon, font=("Arial", 24),
                     bg="white").pack(pady=(15, 0))
            tk.Label(card, text=label, font=("Microsoft YaHei UI", 10),
                     bg="white", fg="#64748b").pack()
            lbl = tk.Label(card, text="--", font=("Consolas", 22, "bold"),
                           bg="white", fg="#1e293b")
            lbl.pack(pady=(5, 15))
            self.stats_labels[key] = lbl

        info = tk.Label(self.stats_frame, text="管理员列表: --",
                         font=("Microsoft YaHei UI", 9),
                         bg="#f0f2f5", fg="#64748b", anchor="w")
        info.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.stats_labels["_admins"] = info

    def _update_stats(self, data):
        if not data: return
        for key, lbl in self.stats_labels.items():
            if key.startswith("_"):
                if key == "_admins":
                    admins = data.get("admins", [])
                    if admins:
                        lbl.config(text="管理员列表: " + ", ".join(admins))
            elif key in data:
                lbl.config(text=str(data[key]))

    # ---- 状态/登录 ----
    def _set_status(self, text, color="#94a3b8"):
        self.status_label.config(text=text, fg=color)
        self.root.update_idletasks()

    def _show_error(self, r):
        """统一错误弹窗: API 的失败响应是 {ok:false,error:...}, 旧代码直接用 msg 会显示原始 dict;
        token 过期 (401) 时还要把界面切回未登录状态, 否则后面每个操作都会继续 401"""
        r = r if isinstance(r, dict) else {"msg": str(r)}
        msg = r.get("msg") or r.get("error") or "请求失败"
        if r.get("auth_expired") or "token" in str(msg) or "未登录" in str(msg):
            CFG.pop("access_token", None)
            CFG.pop("username", None)
            try:
                _save_cfg()
            except Exception:
                pass
            self.lbl_user.config(text="")
            self.btn_login.config(text="🔑 管理员登录", bg="#10b981", command=self._do_login)
            self._set_status("⚠️ 登录已过期, 请重新登录", "#fbbf24")
            messagebox.showwarning("登录已过期", "管理员登录已失效, 请重新登录")
            return
        messagebox.showerror("错误", msg)

    def _refresh_status(self):
        url = CFG.get("server_url", "")
        admin = CFG.get("username", "")
        token_ok = bool(CFG.get("access_token"))
        parts = []
        if url: parts.append(f"API: {url}")
        if admin:
            parts.append(f"管理员: {admin}")
            if token_ok:
                parts.append("✅已认证")
            else:
                parts.append("⚠️ token过期")
        if parts:
            self._set_status(" | ".join(parts))

    def _do_ping(self):
        CFG["server_url"] = self.url_var.get().strip()
        _save_cfg()
        ok, r = api_get("/api/ping")
        if ok and r.get("ok"):
            self._set_status(f"✅ 连通: {r.get('ver')}", "#10b981")
            messagebox.showinfo("连通测试", f"✅ API 正常\n版本: {r.get('ver')}")
        else:
            self._set_status(f"❌ 不通: {r.get('msg', r)}", "#ef4444")
            messagebox.showerror("连通测试", f"❌ 连接失败:\n{r.get('msg', r)}")

    def _do_login(self):
        if CFG.get("access_token"):
            self._do_logout()
            return
        # 登录
        win = tk.Toplevel(self.root)
        win.title("管理员登录")
        win.geometry("350x180")
        win.configure(bg="#f0f2f5")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="账号:", bg="#f0f2f5").pack(pady=(20, 2))
        u_entry = tk.Entry(win, font=("Consolas", 11), width=25)
        u_entry.pack(pady=2)
        tk.Label(win, text="密码:", bg="#f0f2f5").pack(pady=(10, 2))
        p_entry = tk.Entry(win, font=("Consolas", 11), width=25, show="*")
        p_entry.pack(pady=2)

        def on_login():
            u = u_entry.get().strip()
            p = p_entry.get()
            if not u or not p:
                messagebox.showwarning("提示", "请填写账号和密码")
                return
            self._set_status("登录中...", "#fbbf24")
            ok, r = api_post("/api/auth/login", {"username": u, "password": p})
            if ok and r.get("ok"):
                d = r.get("data", {})
                if not d.get("is_admin"):
                    messagebox.showerror("登录失败", f"账号 {u} 不是管理员")
                    self._set_status("❌ 非管理员账号", "#ef4444")
                    return
                CFG["access_token"] = d["access_token"]
                CFG["username"] = u
                _save_cfg()
                self.lbl_user.config(text=f"👤 {u} (管理员)")
                self.btn_login.config(text="🔒 登出", bg="#ef4444", command=self._do_logout)
                self._set_status(f"✅ 已登录: {u}", "#10b981")
                win.destroy()
                self._refresh_all()
            else:
                messagebox.showerror("登录失败", r.get("msg", str(r)))
                self._set_status("❌ 登录失败", "#ef4444")

        tk.Button(win, text="🔑 登录", bg="#10b981", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padx=20,
                  command=on_login).pack(pady=15)
        u_entry.focus_set()
        win.bind("<Return>", lambda e: on_login())

    def _do_logout(self):
        if not messagebox.askyesno("确认", "确定要登出吗?"):
            return
        # 先通知服务器作废 token, 否则本地登出了但 token 还能用满 7 天
        if CFG.get("access_token"):
            try:
                api_post("/api/auth/logout", {})
            except Exception:
                pass
        CFG.pop("access_token", None)
        CFG.pop("username", None)
        _save_cfg()
        self.lbl_user.config(text="")
        self.btn_login.config(text="🔑 管理员登录", bg="#10b981", command=self._do_login)
        self._set_status("已登出", "#64748b")
        for tv in [self.tv_accounts, self.tv_ban, self.tv_chat, self.tv_online]:
            for item in tv.get_children():
                tv.delete(item)
        self._online_rows = {}
        self._accounts = {}
        try:
            self.txt_detail.delete("1.0", "end")
        except Exception:
            pass
        self._render_stats_placeholder()

    # ---- 登录检查 ----
    def _require_admin(self, silent=False):
        if not CFG.get("access_token"):
            if not silent:
                messagebox.showwarning("需要登录", "请先管理员登录")
            return False
        return True

    # ---- 账号管理 ----
    def _refresh_accounts(self):
        if not self._require_admin(): return
        self._set_status("加载账号列表...", "#fbbf24")
        ok, r = api_post("/api/admin/accounts")
        if not ok or not r.get("ok"):
            self._show_error(r)
            self._set_status("❌ 加载失败", "#ef4444")
            return
        # 缓存一份, 供详情面板和"设为管理员"判断当前状态 (避免每次点选都重新下载)
        self._accounts = {a["username"]: a for a in r.get("data", [])
                          if isinstance(a, dict) and "username" in a}
        for item in self.tv_accounts.get_children():
            self.tv_accounts.delete(item)
        for a in r.get("data", []):
            self.tv_accounts.insert("", "end", iid=a["username"], values=(
                a["username"],
                "👑 管理员" if a["is_admin"] else "玩家",
                len(a.get("bind_beam_ids", [])),
                fmt_time(a.get("last_login")),
                fmt_time(a.get("register_time")),
            ))
        self._set_status(f"✅ 已加载 {len(r.get('data', []))} 个账号", "#10b981")

    def _on_account_select(self, event=None):
        sel = self.tv_accounts.selection()
        if not sel: return
        username = sel[0]
        a = getattr(self, "_accounts", {}).get(username)
        if a is None:
            # 缓存里没有 -> 清空详情, 不要留着上一个账号的绑定信息
            self.txt_detail.config(state="normal")
            self.txt_detail.delete("1.0", "end")
            self.txt_detail.config(state="disabled")
            return
        if a is not None:
            if True:
                    bids = a.get("bind_beam_ids", [])
                    # ---- 按前缀分组 + 去重: GUEST 只留最近一条 ----
                    grouped = {"HWID": [], "IP": [], "GUEST": [], "NAME": []}
                    for b in bids:
                        if not isinstance(b, str):
                            continue
                        tag = b.split(":", 1)[0] if ":" in b else "OTHER"
                        if tag in grouped:
                            grouped[tag].append(b)
                        else:
                            grouped.setdefault("OTHER", []).append(b)

                    lines = [f"账号: {username}  |  管理员: {'是' if a['is_admin'] else '否'}  |  总绑定: {len(bids)}"]
                    lines.append("")

                    # 逐类显示
                    def show_group(label, items, limit=None):
                        if not items: return
                        if limit and len(items) > limit:
                            lines.append(f"  [{label}] {items[0]}  (共 {len(items)} 条, 仅显示最近)")
                        else:
                            for it in items:
                                lines.append(f"  [{label}] {it}")
                        lines.append("")

                    show_group("HWID", grouped.get("HWID", []))
                    show_group("IP", grouped.get("IP", []))
                    show_group("NAME", grouped.get("NAME", []))
                    show_group("GUEST", grouped.get("GUEST", []), limit=1)

                    # 其他类型
                    for tag, items in grouped.items():
                        if tag not in ("HWID", "IP", "NAME", "GUEST"):
                            show_group(tag, items)

                    if not bids:
                        lines.append("  (无绑定)")
                        lines.append("")

                    lines.append(f"注册: {fmt_time(a.get('register_time'))}  |  最后登录: {fmt_time(a.get('last_login'))}")

                    # 写入 Text 控件
                    self.txt_detail.config(state="normal")
                    self.txt_detail.delete("1.0", "end")
                    self.txt_detail.insert("1.0", "\n".join(lines))
                    self.txt_detail.config(state="disabled")

    def _delete_account(self):
        if not self._require_admin(): return
        sel = self.tv_accounts.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个账号")
            return
        username = sel[0]
        if not messagebox.askyesno("确认删除", f"确定要删除账号 {username} 吗?\n此操作不可恢复!"):
            return
        ok, r = api_post("/api/admin/accounts/delete", {"username": username})
        if ok and r.get("ok"):
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            self._refresh_accounts()
        else:
            self._show_error(r)

    def _reset_password(self):
        if not self._require_admin(): return
        sel = self.tv_accounts.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个账号")
            return
        username = sel[0]
        new_pw = simpledialog.askstring("重置密码",
                                         f"为 {username} 设置新密码:\n(至少4位)",
                                         parent=self.root, show="*")
        if not new_pw: return
        if len(new_pw) < 4:
            messagebox.showwarning("提示", "密码至少 4 位")
            return
        ok, r = api_post("/api/admin/accounts/reset-password",
                          {"username": username, "new_password": new_pw})
        if ok and r.get("ok"):
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            messagebox.showinfo("成功", r.get("msg"))
        else:
            self._show_error(r)

    def _toggle_admin(self):
        if not self._require_admin(): return
        sel = self.tv_accounts.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个账号")
            return
        username = sel[0]
        # 旧实现把 askyesno 的返回值当成"新的管理员状态"发出去,
        # 于是点"否"/直接关窗都会把管理员降级. 这里必须先读出当前状态.
        current = bool((getattr(self, "_accounts", {}).get(username) or {}).get("is_admin"))
        action = "取消" if current else "设为"
        if not messagebox.askyesno("管理员权限",
                                   f"确定要将 {username} {action}管理员吗?\n(管理员可管理服务器账号)"):
            return
        ok, r = api_post("/api/admin/accounts/toggle-admin",
                          {"username": username, "is_admin": not current})
        if ok and r.get("ok"):
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            self._refresh_accounts()
        else:
            self._show_error(r)

    def _unbind_hwid(self):
        if not self._require_admin(): return
        sel = self.tv_accounts.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个账号")
            return
        username = sel[0]
        ok, r = api_post("/api/admin/accounts")
        if not ok or not r.get("ok"): return
        acc = next((a for a in r.get("data", []) if a["username"] == username), None)
        if not acc or not acc.get("bind_beam_ids"):
            messagebox.showinfo("提示", f"{username} 没有绑定")
            return
        # 让用户选要解绑的
        win = tk.Toplevel(self.root)
        win.title(f"解绑 HWID — {username}")
        win.geometry("500x350")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="选择要解绑的绑定:",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(pady=10)

        lb_frame = tk.Frame(win)
        lb_frame.pack(fill="both", expand=True, padx=20)
        sb = ttk.Scrollbar(lb_frame)
        sb.pack(side="right", fill="y")
        listbox = tk.Listbox(lb_frame, yscrollcommand=sb.set,
                              font=("Consolas", 10), selectmode=tk.SINGLE)
        listbox.pack(side="left", fill="both", expand=True)
        sb.config(command=listbox.yview)

        for bid in acc["bind_beam_ids"]:
            listbox.insert("end", bid)

        def do_unbind():
            sel_idx = listbox.curselection()
            if not sel_idx:
                messagebox.showwarning("提示", "请选择要解绑的绑定")
                return
            bid = listbox.get(sel_idx[0])
            ok2, r2 = api_post("/api/admin/accounts/unbind",
                                {"username": username, "beam_id": bid})
            if ok2 and r2.get("ok"):
                self._set_status(f"✅ {r2.get('msg')}", "#10b981")
                win.destroy()
                self._refresh_accounts()
            else:
                self._show_error(r2)

        tk.Button(win, text="🔗 解绑选中", bg="#06b6d4", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padx=15,
                  command=do_unbind).pack(pady=10)

    # ---- 封禁管理 ----
    def _refresh_ban(self):
        if not self._require_admin(): return
        self._set_status("加载封禁列表...", "#fbbf24")
        ok, r = api_post("/api/admin/banlist")
        if not ok or not r.get("ok"):
            self._show_error(r)
            return
        for item in self.tv_ban.get_children():
            self.tv_ban.delete(item)
        data = r.get("data", [])
        if isinstance(data, list):
            for entry in data:
                typ = entry.get("type", "")
                val = entry.get("value", "")
                reason = entry.get("reason", "")
                dur = entry.get("duration_days", 0)
                exp = entry.get("expires_at", 0)
                expired = entry.get("expired", False)
                if expired:
                    exp_text = "已过期"
                elif exp > 0:
                    remain = entry.get("remaining_seconds", 0)
                    if remain > 86400:
                        exp_text = f"{remain // 86400}天"
                    elif remain > 3600:
                        exp_text = f"{remain // 3600}小时"
                    else:
                        exp_text = f"{remain // 60}分钟"
                else:
                    exp_text = "永久"
                t = fmt_time(entry.get("time"))
                self.tv_ban.insert("", "end", values=(typ, val, reason, exp_text, t))
        self._set_status(f"✅ 封禁列表已加载", "#10b981")

    def _do_ban(self):
        if not self._require_admin(): return
        t = self.ban_type.get()
        v = self.ban_val.get().strip()
        reason = self.ban_reason.get().strip()
        raw_days = self.ban_days.get().strip() or "0"
        try:
            days = int(raw_days)
        except ValueError:
            # 旧实现 except 里 days=0, 而 API 把 0 当"永久封禁" —— 打错字就变永久封号
            messagebox.showwarning("提示", "封禁天数必须是 0-365 的整数 (0 = 永久)")
            return
        if days < 0 or days > 365:
            messagebox.showwarning("提示", "封禁天数必须是 0-365 的整数 (0 = 永久)")
            return
        if not v:
            messagebox.showwarning("提示", "请填写封禁值")
            return
        body = {"type": t, "value": v, "reason": reason, "duration_days": days}
        ok, r = api_post("/api/admin/ban", body)
        if ok and r.get("ok"):
            self.ban_val.delete(0, "end")
            self.ban_reason.delete(0, "end")
            self.ban_days.delete(0, "end")
            self.ban_days.insert(0, "0")
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            self._refresh_ban()
        else:
            self._show_error(r)

    def _do_unban(self):
        if not self._require_admin(): return
        sel = self.tv_ban.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择要解封的条目")
            return
        for s in sel:
            vals = self.tv_ban.item(s, "values")
            ok, r = api_post("/api/admin/unban",
                              {"type": vals[0], "value": vals[1]})
            if not ok or not r.get("ok"):
                self._show_error(r)
                return
        self._set_status("✅ 解封成功", "#10b981")
        self._refresh_ban()

    # ---- 聊天广播 ----
    def _refresh_chat(self):
        if not self._require_admin(): return
        ok, r = api_post("/api/admin/chat-queue")
        if not ok or not r.get("ok"):
            self._show_error(r)
            return
        for item in self.tv_chat.get_children():
            self.tv_chat.delete(item)
        items = r.get("data", {}).get("items", [])
        for it in items:
            self.tv_chat.insert("", "end", values=(
                it.get("id", "")[:12],
                fmt_time(it.get("ts")),
                it.get("name", ""),
                it.get("text", ""),
                "✅" if it.get("sent") else "⏳",
            ))
        unsent = r.get("data", {}).get("unsent", 0)
        total = r.get("data", {}).get("total", 0)
        if unsent > 0:
            self._set_status(f"⚠️ 队列: {total} 条, 待发 {unsent} 条 — BeamMP 服务器启动后自动广播", "#fbbf24")
        else:
            self._set_status(f"✅ 队列: {total} 条, 全部已发送", "#10b981")

    def _admin_chat_send(self):
        if not self._require_admin(): return
        name = self.chat_name.get().strip() or "管理员"
        msg = self.chat_msg.get("1.0", "end-1c").strip()
        if not msg:
            messagebox.showwarning("提示", "请输入消息内容")
            return
        ok, r = api_post("/api/admin/chat-queue/send",
                          {"message": msg, "player_name": name})
        if ok and r.get("ok"):
            self.chat_msg.delete("1.0", "end")
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            self._refresh_chat()
        else:
            self._show_error(r)

    def _clear_chat(self, only_sent):
        if not self._require_admin(): return
        label = "已发送消息" if only_sent else "全部队列"
        if not messagebox.askyesno("确认", f"确定要清空{label}吗?"):
            return
        ok, r = api_post("/api/admin/chat-queue/clear", {"only_sent": only_sent})
        if ok and r.get("ok"):
            self._set_status(f"✅ {r.get('msg')}", "#10b981")
            self._refresh_chat()
        else:
            self._show_error(r)

    # ---- 统计 ----
    def _refresh_stats(self):
        if not self._require_admin(): return
        self._set_status("加载统计...", "#fbbf24")
        ok, r = api_post("/api/admin/stats")
        if not ok or not r.get("ok"):
            self._show_error(r)
            return
        self._update_stats(r.get("data", {}))
        self._set_status("✅ 统计已更新", "#10b981")

    # ---- 全部刷新 ----
    def _refresh_all(self):
        self._refresh_accounts()
        self._refresh_ban()
        self._refresh_chat()
        self._refresh_stats()
        self._refresh_status()


def main():
    root = tk.Tk()
    app = AdminApp(root)

    # 如果已有 token, 自动刷新
    if CFG.get("access_token"):
        root.after(500, app._refresh_all)

    root.mainloop()


if __name__ == "__main__":
    main()
