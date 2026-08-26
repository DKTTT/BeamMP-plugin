# -*- coding: utf-8 -*-
"""
=====================================================================
#  BMPHWID Bridge v2.0.0  (极简版 - 只显示机器码 + 使用说明)
---------------------------------------------------------------------
  功能 (只保留 4 件事):
    1. 在玩家电脑上采集【永久稳定的 HWID】
         MachineGuid + 主板 UUID + 物理网卡 MAC
         → 混合 FNV-1a → 36 位 UUID (永不变化)

    2. 显示 HWID + 采集来源列表 (给玩家确认稳定性: 至少 1 个 MG 就稳)

    3. 本地 HTTP 服务  http://127.0.0.1:7788
         GET  /hwid  →  {hwid, ver, name, ok, sources, command}
         客户端 BMPHWID.zip 通过这个接口拉机器码, 再发给服务器
         (服务器主动请求架构, 完全不需要模拟键盘)

    4. 使用说明面板 + 复制 HWID 按钮 (兜底, 玩家可手动 /bmpid <uuid>)

  打包： pip install pyinstaller
         pyinstaller -F -w -n BMPHWID_Bridge BMPHWID_Bridge.py
         → dist\BMPHWID_Bridge.exe   单文件 ~11MB
=====================================================================
"""

import re, os, sys, json, hashlib, threading, socket
from http.server import HTTPServer, BaseHTTPRequestHandler

VERSION = "2.0.0"
HTTP_PORT = 7788
PAYLOAD_VER = "Bridge-v2.0"
HTTP_NAME = f"BMPHWID_Bridge/v{VERSION}"
WIN_TITLE = f"BMPHWID Bridge v{VERSION} — 外置稳定 HWID"

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

    # GUI 构造
    root = tk.Tk()
    root.title(WIN_TITLE)
    root.geometry("780x560")
    root.minsize(640, 520)
    root.configure(bg="#f7f7f9")

    pad = {"padx": 16, "pady": 8}

    # 标题
    top = tk.Frame(root, bg="#f7f7f9")
    top.pack(fill="x", **pad)
    tk.Label(top,
             text="\U0001F527 BMPHWID Bridge — 稳定 HWID 一键发送",
             font=("Microsoft YaHei UI", 16, "bold"),
             bg="#f7f7f9", fg="#1d3557").pack(side="left")

    # 说明第一行
    intro = tk.Label(root,
                     text="下面这个 HWID 在你这台电脑上 【永久不变】, guest 数字变了 / IP 换了 / 游戏重开了都不会变:",
                     font=("Microsoft YaHei UI", 10),
                     bg="#f7f7f9", fg="#333", justify="left", wraplength=740)
    intro.pack(fill="x", padx=18, pady=(0, 4))

    # HWID 文本框
    f_id = tk.Frame(root, bg="#ffffff", bd=1, relief="solid")
    f_id.pack(fill="x", padx=18, pady=(0, 4))
    e_id = tk.Entry(f_id, font=("Consolas", 14), bd=0, justify="center",
                    bg="#ffffff", fg="#1d3557", readonlybackground="#ffffff",
                    relief="flat")
    e_id.insert(0, hwid)
    e_id.config(state="readonly")
    e_id.pack(fill="x", padx=10, pady=10)

    # 复制按钮
    btn_row = tk.Frame(root, bg="#f7f7f9")
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
               width=20).pack(side="left")
    ttk.Button(btn_row, text="\U0001F4CB 复制命令 (/bmpid UUID)",
               command=_copy_cmd, width=28).pack(side="left", padx=10)
    tk.Label(btn_row,
             text=f"当前显示: 短UUID命令 ({len(command)} 字)",
             font=("Microsoft YaHei UI", 9),
             bg="#f7f7f9", fg="#666").pack(side="right")

    # 采集来源
    f_src = tk.LabelFrame(root,
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
            txt = "\u2705 MachineGuid (HKLM\\Cryptography):  " + s[3:][:20] + "..."
            fg = "#2b9348"
        elif s.startswith("CSUID:"):
            txt = "\u2705 主板 SMBIOS UUID (wmic csproduct):  " + s[6:][:20] + "..."
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
    f_guide = tk.LabelFrame(root,
                            text=" 使用说明: ",
                            font=("Microsoft YaHei UI", 10, "bold"),
                            bg="#f7f7f9", fg="#1d3557", bd=1, relief="solid",
                            labelanchor="nw")
    f_guide.pack(fill="both", expand=True, padx=18, pady=(4, 10))

    guide_lines = [
        "\u2460  本程序要先启动, 再用 BeamMP Launcher 启动游戏!",
        "\u2461  游戏里, 服务器会自动问你的客户端 zip 要 HWID.",
        "\u2462  客户端 BMPHWID.zip 会从本程序 (http://127.0.0.1:7788/hwid) 拉 UUID 回传服务器.",
        "\u2463  一切自动! 你进游戏后直接 /login 你的账号密码, HWID 会永远绑定到你账号.",
        "",
        "兜底: 如果服务器提示你未认证 → 点击上面的【复制命令 (/bmpid UUID)】",
        "        然后在游戏聊天框里粘贴 + 回车 → 再 /login 账号密码",
    ]
    for line in guide_lines:
        if not line:
            tk.Frame(f_guide, height=6, bg="#ffffff").pack(fill="x")
            continue
        tk.Label(f_guide, text=line,
                 font=("Microsoft YaHei UI", 10),
                 bg="#ffffff", fg="#222", anchor="w",
                 justify="left", wraplength=700).pack(fill="x", padx=12, pady=2, ipady=1)

    # 底部状态栏
    status_f = tk.Frame(root, bg="#ffffff", bd=1, relief="solid")
    status_f.pack(fill="x", padx=18, pady=(0, 14))
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
