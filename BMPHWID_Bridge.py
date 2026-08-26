# -*- coding: utf-8 -*-
"""
=====================================================================
#  BMPHWID Bridge v1.3.1  (BeamMP 外置 HWID 桥接程序 - 自动版)
---------------------------------------------------------------------
  功能：
    1. 在玩家电脑上采集【永久稳定的 HWID】
       - Windows 注册表 MachineGuid  (HKLM\\Cryptography\\MachineGuid)
       - 第一块物理网卡 MAC 地址
       - WMI Win32_ComputerSystemProduct.UUID  (主板序列号)
       → 三者混合 FNV-1a + 截断 → 生成 36 位 UUID 形式的稳定 ID
         (重装系统不变、换guest数字不变、IP 换了也不变)

    2. 【自动模式 - 新增】使用 Windows IP Helper API (和 BNG 验证服务同款)
       自动检测玩家是否已连接 BeamMP 服务器端口 (12125)，
       检测到后自动模拟键盘发送 /bmpid <UUID> —— 玩家完全无需操作!

    3. GUI 三种方式给 BeamMP 服务器:
        A. [复制命令]       复制 /bmpid F|xxx 到剪贴板 → 玩家游戏里粘贴
        B. [一键粘贴发送]   模拟键盘：切到 BeamNG → 按 T → 粘贴 → 回车  ✨ 推荐
        C. [HTTP Bridge]    启动本地 http://127.0.0.1:7788
             GET  /hwid          → 返回 JSON {hwid, payload, command}
             POST /sendchat      → text=xxx，同样模拟键盘发聊天（给未来 Lua 客户端联网扩展用）

  打包： pip install pyinstaller
         pyinstaller -F -w -n BMPHWID_Bridge BMPHWID_Bridge.py
         → dist\BMPHWID_Bridge.exe  单文件可分发 (~9MB)
=====================================================================
"""

import re, os, sys, json, hashlib, uuid, time, threading, socket, struct
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ============================================================
#  Part 1: 稳定 HWID 采集  (只依赖 Python 标准库，不装 WMI 也能跑)
# ============================================================
def _read_reg_machineGuid():
    r"""读 HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid（64位+32位都找）"""
    vals = []
    try:
        import winreg
        for arch_flag, arch_name in [(winreg.KEY_WOW64_64KEY, "64"), (winreg.KEY_WOW64_32KEY, "32")]:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Cryptography",
                                    0, winreg.KEY_READ | arch_flag) as k:
                    v, _ = winreg.QueryValueEx(k, "MachineGuid")
                    if isinstance(v, str) and len(v) >= 16:
                        vals.append(f"MG{arch_name}:{v.lower().strip()}")
            except Exception:
                pass
    except Exception:
        pass
    return vals

def _read_wmi_UUID():
    """Win32_ComputerSystemProduct.UUID (主板 SMBIOS UUID) —— 优先 wmic.exe，失败再试 wmi 包"""
    out = None
    try:
        import subprocess
        r = subprocess.run(["wmic.exe", "csproduct", "get", "uuid", "/value"],
                           capture_output=True, timeout=10, creationflags=0x08000000)
        def _decode(b):
            if not b: return ""
            for enc in ("utf-8", "utf-16", "gbk", "latin-1"):
                try: return b.decode(enc)
                except Exception: continue
            try: return b.decode("latin-1", errors="ignore")
            except Exception: return ""
        txt = _decode(r.stdout or b"") + _decode(r.stderr or b"")
        m = re.search(r"UUID\s*=\s*([A-Fa-f0-9\-]{30,})", txt)
        if m:
            out = "CSUID:" + m.group(1).lower().strip()
    except Exception:
        pass
    if out: return [out]
    try:
        import wmi  # 可选
        c = wmi.WMI()
        for p in c.Win32_ComputerSystemProduct():
            if getattr(p, "UUID", None):
                return ["CSUID:" + str(p.UUID).lower().strip()]
    except Exception:
        pass
    return []

def _read_first_physical_MAC():
    macs = []
    try:
        import uuid as _uuid_mod
        addr = _uuid_mod.getnode()  # Python 标准库：取"第一个可用网卡"的 48bit MAC
        if addr and addr != 0:
            s = ":".join(f"{(addr >> (8*(5-i))) & 0xff:02x}" for i in range(6))
            # 过滤掉 00-00-00-00-00-00 和 02:00:00... 这种随机/虚拟
            if not s.startswith("00:00:00:00:00") and not s.startswith("02:00:00"):
                macs.append("MAC1:" + s)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["getmac", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=8, creationflags=0x08000000)
        def _decode2(b):
            if not b: return ""
            for enc in ("utf-8","gbk","latin-1"):
                try: return b.decode(enc)
                except Exception: continue
            try: return b.decode("latin-1", errors="ignore")
            except: return ""
        out_text = _decode2(r.stdout or b"")
        for line in out_text.splitlines():
            m = re.search(r"([A-F0-9]{2}-[A-F0-9]{2}-[A-F0-9]{2}-[A-F0-9]{2}-[A-F0-9]{2}-[A-F0-9]{2})", line, re.I)
            if m:
                s = m.group(1).replace("-", ":").lower()
                if not s.startswith("00:00:00:00:00") and not s.startswith("02:00:00"):
                    key = "MAC:" + s
                    if key not in macs: macs.append(key)
    except Exception:
        pass
    return macs

def _fnv1a_64(s: str) -> int:
    data = s.encode("utf-8")
    h = 0xcbf29ce484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h

def _makeUuidFromSources(sources):
    """把 sources 列表字符串混合成 UUID v5 风格的稳定 ID"""
    if not sources:
        # 极端情况：所有采集失败，fallback 到本地生成一次 UUID 存 AppData，下次读
        return _fallbackAppDataUUID()
    joined = "||".join(s.strip().lower() for s in sources if s)
    hi64 = _fnv1a_64("BMPHWID||" + joined)
    lo64 = _fnv1a_64("Seed||v1||"  + joined)
    # 拼成 xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
    hex16 = f"{hi64:016x}{lo64:016x}"
    uuid_str = (hex16[0:8] + "-" + hex16[8:12] + "-4" +
                hex16[13:16] + "-" + f"{(int(hex16[16],16) & 0x3 | 0x8):x}" +
                hex16[17:20] + "-" + hex16[20:32])
    return uuid_str

def _fallbackAppDataUUID():
    """AppData 本地永久保存 UUID（重装系统/换电脑才会变）"""
    try:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "BMPHWID_Bridge")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "stable_uuid.txt")
        if os.path.exists(f):
            with open(f, "r", encoding="utf-8") as fh:
                t = fh.read().strip()
                if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", t or ""):
                    return t
        new = str(uuid.uuid4())
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(new + "\n")
        return new
    except Exception:
        return str(uuid.uuid4())

def collect_hwid():
    """返回: (hwid_uuid_str, sources_used_list)"""
    sources = []
    sources.extend(_read_reg_machineGuid())
    sources.extend(_read_wmi_UUID())
    sources.extend(_read_first_physical_MAC())
    return _makeUuidFromSources(sources), sources

# ============================================================
#  Part 2: 和服务器侧 base64/tsv 编码保持一致 (match main.lua 解析)
# ============================================================
def _b64e(s: str) -> str:
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

def buildPayloadDict(hwid_uuid, sources_list):
    """和 lua/ge/extensions/bmpHwidProbe.lua 里 collectHwidData 保持 keys 一致，服务端无需改解析"""
    return {
        "_v": "Bridge-v1.3.1",
        "ts": int(time.time()),
        "settings.BMPLoginHWID(uuid)": hwid_uuid,     # P0 key（服务端优先级最高）
        "bridge_sources": "||".join(sources_list),
        "bridge_os_version": str(sys.platform) + "-" + str(sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else ""),
        "core_env_fingerprint": f"fnv1a32:{_fnv1a_64('|'.join(sources_list or ['fallback'])) & 0xffffffff:08x}",
    }

def buildBmpidChatCommand(data_dict):
    """生成聊天命令 /bmpid F|xxx (单帧格式，注意 BeamMP 聊天框 256 字符上限)"""
    lines = []
    for k in sorted(data_dict.keys()):
        v = data_dict[k]
        if isinstance(v, (str, int, float, bool)):
            sk = str(k).replace("\r", " ").replace("\n", " ").replace("\t", " ")
            sv = str(v).replace("\r", " ").replace("\n", " ").replace("\t", " ")
            lines.append(sk + "\t" + sv)
    tsv = "\n".join(lines)
    return "/bmpid F|" + _b64e(tsv)

# BeamNG/BeamMP 聊天框字符上限（实测 ~256 但保险取 200）
CHAT_MAX = 200

def buildBmpidShortCommand(hwid_uuid):
    """生成超短裸 UUID 命令：/bmpid xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    只有 7+36 = 43 字符，绝对不被聊天框截断 ✅
    服务端 main.lua handleBmpidCommand 已支持裸 UUID 兜底解析
    """
    return "/bmpid " + hwid_uuid

def buildBmpidChunkedCommands(data_dict):
    """如果 /bmpid F|payload 太长(>CHAT_MAX), 拆成多块 001/003|chunk, 002/003|chunk, ...
    返回: (cmds_list, total_count) 或 ([], 0) 如果不需要分块
    """
    lines = []
    for k in sorted(data_dict.keys()):
        v = data_dict[k]
        if isinstance(v, (str, int, float, bool)):
            sk = str(k).replace("\r", " ").replace("\n", " ").replace("\t", " ")
            sv = str(v).replace("\r", " ").replace("\n", " ").replace("\t", " ")
            lines.append(sk + "\t" + sv)
    tsv = "\n".join(lines)
    payload64 = _b64e(tsv)
    full_cmd = "/bmpid F|" + payload64
    if len(full_cmd) <= CHAT_MAX:
        return [full_cmd], 1
    # 需要分块: 每块 "/bmpid NNN/TTT|chunk" 其中 NNN=当前块号 TTT=总块数
    prefix_len = len("/bmpid 999/999|")
    chunk_size = CHAT_MAX - prefix_len
    if chunk_size < 50:
        return [full_cmd], 1   # 退化情况
    total = (len(payload64) + chunk_size - 1) // chunk_size
    total = max(1, min(999, total))
    cmds = []
    for i in range(total):
        start = i * chunk_size
        chunk = payload64[start:start + chunk_size]
        cmds.append(f"/bmpid {i+1:03d}/{total:03d}|{chunk}")
    return cmds, total

# ============================================================
#  Part 3: 模拟键盘 —— 一键发送 /bmpid F|xxx 到 BeamNG 聊天框
# ============================================================
def _findWindowByKeywords(keywords):
    """返回最匹配的窗口句柄（win32gui），找不到返回 0
    新版 v1.2 修复: 优先精确匹配 BeamNG.drive 游戏窗口, 不切 Launcher
    新版 v1.3 修复: AutoWatchdog 检测不到进服务器 —— 修复端口列表, 改按 BeamNG PID 枚举+非标准端口兜底, 加详细调试日志 + 手动检测按钮
    跳过 BeamMP Launcher (两个进程名字都含 Beam, 易混淆)
    """
    try:
        import win32gui, win32process, ctypes
    except Exception:
        return 0
    # BeamNG.drive 游戏窗口标题一般是 "BeamNG.drive" 或 "BeamNG.drive - <场景名>"
    # BeamMP Launcher 窗口标题一般是 "BeamMP Launcher" 或 "BeamMP"
    # 优先级: BeamNG.drive 游戏窗口 > 兜底匹配
    candidates_game = []     # BeamNG.drive 游戏窗口 (最优先)
    candidates_launcher = [] # BeamMP Launcher (用得最少)
    candidates_other = []    # 兜底匹配
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd): return
        try:
            title = win32gui.GetWindowText(hwnd)
            cls   = win32gui.GetClassName(hwnd)
        except Exception:
            return
        if not title: return
        tl = title.lower()
        # 跳过明显不是游戏主窗口的 (比如系统托盘, 工具提示等)
        if tl in ("", "msctls_statusbar32", "tooltips_class32"): return
        # 优先级 1: 标题精确包含 "beamng.drive" 或 "beamng.drive -" (游戏主窗口)
        if "beamng.drive" in tl or "beamng drive" in tl:
            candidates_game.append((hwnd, title, cls))
            return
        # 优先级 2: 标题包含 "beammp launcher" 或 "beammp" 但不是 beamng (Launcher 窗口)
        if "beammp" in tl and "beamng" not in tl:
            candidates_launcher.append((hwnd, title, cls))
            return
        # 优先级 3: 兜底 (老逻辑, 包含 BeamNG/BeamMP/drive)
        for kw in keywords:
            if kw in title or kw.lower() in cls.lower():
                candidates_other.append((hwnd, title, cls))
                return
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass

    # 用 PID + 进程名双重验证, 排除 BeamMP Launcher 进程的假窗口
    def _isRealBeamNGGame(hwnd):
        """检查 hwnd 对应的进程是不是 BeamNG.drive.exe (而不是 BeamMP-Launcher.exe)"""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            # tasklist 验证
            import subprocess
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, timeout=3, creationflags=0x08000000)
            for enc in ("utf-8","gbk","latin-1"):
                try: out = r.stdout.decode(enc); break
                except: continue
            if not out: out = (r.stdout or b"").decode("latin-1", errors="ignore")
            # 排除 BeamMP-Launcher 进程 (用户问题: 模拟键盘发到了 Launcher 而不是游戏)
            if "launcher" in out.lower(): return False
            # 必须是 BeamNG.drive 进程
            if "BeamNG.drive" in out or "beamng.drive" in out.lower(): return True
            if "BeamNG" in out: return True
            return False
        except Exception:
            return False

    # 选择策略:
    # 1. 先从 candidates_game 里找进程名是 BeamNG.drive.exe 的窗口
    for hwnd, title, cls in candidates_game:
        if _isRealBeamNGGame(hwnd):
            return hwnd
    # 2. 如果 candidates_game 里有窗口但进程验证失败, 也先用它 (可能 tasklist 不准)
    if candidates_game:
        return candidates_game[0][0]
    # 3. 兜底: candidates_other (老的 BeamNG / drive 关键词匹配, 但跳过 Launcher)
    for hwnd, title, cls in candidates_other:
        if _isRealBeamNGGame(hwnd):
            return hwnd
    # 4. 最后才考虑 Launcher (基本不会用到, 因为 Launcher 不能发聊天)
    if candidates_launcher:
        return 0  # 故意不返回 Launcher 窗口, 因为发到那里没用
    return 0

def sendChatToForeground(command_text: str):
    """
    模拟键盘流程:
        1. 找 BeamNG/BeamMP 窗口，激活它（SetForegroundWindow）
        2. 按 Esc 两下清 UI 模态
        3. 按 'T' 打开 BeamNG 聊天输入框
        4. Ctrl+A 全删 + Ctrl+V 粘贴 command
        5. 按 Enter 发送
    返回 (ok: bool, detail_msg: str)
    """
    try:
        import win32gui, win32con, win32api, win32clipboard
    except ImportError as e:
        return False, ("需要先安装 pywin32 才能用一键发送： pip install pywin32 \n"
                       + "或者直接用[复制命令]按钮手动粘贴。\n\n"
                       + str(e))

    # 剪贴板写入命令
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(command_text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except Exception as e:
        return False, f"写入剪贴板失败：{e}"

    # 用精确 keywords 查找 BeamNG.drive 游戏窗口 (内部已加 PID 验证跳过 BeamMP Launcher)
    hwnd = _findWindowByKeywords(["BeamNG.drive", "BeamNG drive", "BeamNG"])
    # 调试: 输出找到的窗口标题, 帮用户判断是否切对了
    found_title = ""
    if hwnd:
        try:
            found_title = win32gui.GetWindowText(hwnd)
        except Exception:
            pass
    brought_to_front = False
    if hwnd:
        try:
            # v1.3.1 修复: 用 AttachThreadInput 绕过 Windows 前台锁定
            # Windows 安全机制: 只有前台进程才能 SetForegroundWindow 成功
            # 解决: 把当前线程和 BeamNG 窗口线程 attach 到同一个输入队列
            import ctypes
            from ctypes import wintypes

            # 如果窗口最小化, 先恢复
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)

            # 方法 1: AttachThreadInput (最可靠)
            fg_hwnd = win32gui.GetForegroundWindow()
            fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
            bng_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
            current_tid = ctypes.windll.kernel32.GetCurrentThreadId()

            if fg_tid and bng_tid and fg_tid != bng_tid:
                # 附加前台窗口线程到 BeamNG 窗口线程
                ctypes.windll.user32.AttachThreadInput(fg_tid, bng_tid, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                    # 恢复前台线程 (解除 attach)
                    ctypes.windll.user32.AttachThreadInput(fg_tid, bng_tid, False)
                    brought_to_front = True
                finally:
                    # 确保一定解除 attach
                    ctypes.windll.user32.AttachThreadInput(fg_tid, bng_tid, False)
            else:
                # 同一线程或无前台, 直接切
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                brought_to_front = True

            time.sleep(0.5)  # 给游戏更多时间响应窗口切换
        except Exception:
            # 方法 2 兜底: 用 keybd_event 发送 Alt 键 (有时能解锁前台)
            try:
                import win32api
                VK_MENU = 0x12  # Alt key
                KEYEVENTF_KEYUP = 0x0002
                win32api.keybd_event(VK_MENU, 0, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.5)
                # 验证是否真的切过去了
                fg = win32gui.GetForegroundWindow()
                if fg == hwnd:
                    brought_to_front = True
            except Exception:
                brought_to_front = False

    VK_ESCAPE = 0x1B ; VK_T = ord('T') ; VK_CONTROL = 0x11 ; VK_A = ord('A')
    VK_V = ord('V')  ; VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002
    def pvk(vk, down=True):
        win32api.keybd_event(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0)
    def press(vk, hold=0.02):
        pvk(vk, True); time.sleep(hold); pvk(vk, False); time.sleep(0.02)
    def ctrl_combo(vk):
        pvk(VK_CONTROL, True); time.sleep(0.02)
        pvk(vk, True);       time.sleep(0.03)
        pvk(vk, False);      time.sleep(0.02)
        pvk(VK_CONTROL, False); time.sleep(0.03)

    try:
        # Esc x2 清可能弹出来的菜单
        press(VK_ESCAPE); time.sleep(0.05); press(VK_ESCAPE); time.sleep(0.1)
        press(VK_T)           # 打开聊天框
        time.sleep(0.25)
        ctrl_combo(VK_A)      # 全选 (聊天框里可能有残留)
        time.sleep(0.05)
        ctrl_combo(VK_V)      # 粘贴
        time.sleep(0.1)
        press(VK_RETURN)      # 回车发送
        time.sleep(0.05)
        ok_msg = "✅ 已发送：" + command_text[:90] + ("..." if len(command_text) > 90 else "")
        if not brought_to_front:
            ok_msg += "\n\n⚠️ 没能自动切到 BeamNG 窗口，请手动点一下游戏窗口（保持聊天框关闭）再重新点【一键发送】"
        return True, ok_msg
    except Exception as e:
        return False, f"模拟键盘出错：{e}"

# ============================================================
#  Part 3.5: BeamMP 服务器连接自动检测 (和 BNG 验证服务同款 API)
#  使用 Windows IP Helper API (iphlpapi.dll) + ctypes 直接调用
#  无需 psutil 等第三方库，纯标准库实现
# ============================================================
# BeamMP 官方端口列表 (服务器端监听端口)
# - 12123: 车辆数据网络 (Vehicle data network, 默认, 你日志里是这个)
# - 12124-12130: 备用/多实例
# - 2694-2696: BeamNG.drive 协议
# - 27015-27018: Steam 查询端口 (也常用)
# - 30814: BeamNG 官方联机
BEAMMP_DEFAULT_PORTS = {12123,12124,12125,12126,12127,12128,12129,12130,12131,12132,
                        2694,2695,2696, 27015,27016,27017,27018, 30814}

# 进程名缓存 (key: pid, value: (is_beamng, expires_at_monotonic))
_PROCESS_CACHE = {}

def _getEstablishedTcpConnections():
    """用 iphlpapi GetExtendedTcpTable 返回本机 *所有* ESTABLISHED TCP (IPv4)
    返回: [(local_port, remote_ip_str, remote_port, pid)]
    """
    try:
        import ctypes
        from ctypes import wintypes
        iphlpapi = ctypes.windll.iphlpapi
        TCP_TABLE_OWNER_PID_ALL = 5
        AF_INET = 2
        size = wintypes.ULONG(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                     AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if size.value == 0: return []
        buf = (ctypes.c_ubyte * size.value)()
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                           AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if ret != 0: return []
        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [("dwState",   wintypes.DWORD),
                        ("dwLocalAddr",  wintypes.DWORD),
                        ("dwLocalPort",  wintypes.DWORD),
                        ("dwRemoteAddr", wintypes.DWORD),
                        ("dwRemotePort", wintypes.DWORD),
                        ("dwOwningPid",  wintypes.DWORD)]
        class MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
            _fields_ = [("dwNumEntries", wintypes.DWORD),
                        ("table", MIB_TCPROW_OWNER_PID * 1)]
        table = ctypes.cast(ctypes.addressof(buf), ctypes.POINTER(MIB_TCPTABLE_OWNER_PID)).contents
        n = table.dwNumEntries
        rows_ptr = ctypes.cast(ctypes.addressof(table.table), ctypes.POINTER(MIB_TCPROW_OWNER_PID))
        out = []
        for i in range(n):
            row = rows_ptr[i]
            if row.dwState != 5: continue   # 5 = ESTABLISHED
            lp = ((row.dwLocalPort >> 8) & 0xFF) | ((row.dwLocalPort & 0xFF) << 8)
            rp = ((row.dwRemotePort >> 8) & 0xFF) | ((row.dwRemotePort & 0xFF) << 8)
            la = socket.inet_ntoa(struct.pack("<I", row.dwLocalAddr))
            ra = socket.inet_ntoa(struct.pack("<I", row.dwRemoteAddr))
            out.append((lp, la, ra, rp, row.dwOwningPid))
        return out
    except Exception as e:
        return []

def _isBeamNGProcess(pid):
    """检查 pid 是否是 BeamNG.drive 进程 (带缓存, 30s 过期, 避免 3s 一次 tasklist 太慢)"""
    try:
        import subprocess, time as _t
        if pid <= 0: return False
        now = _t.monotonic()
        if pid in _PROCESS_CACHE:
            ok, expires = _PROCESS_CACHE[pid]
            if expires > now:
                return ok
        # tasklist 验证
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=5, creationflags=0x08000000)
        out = ""
        for enc in ("utf-8","gbk","latin-1"):
            try: out = r.stdout.decode(enc); break
            except: continue
        if not out: out = (r.stdout or b"").decode("latin-1", errors="ignore")
        ok = (("BeamNG.drive" in out) or ("beamng.drive" in out.lower()) or
              (("BeamNG" in out or "beamng" in out.lower()) and "launcher" not in out.lower()))
        _PROCESS_CACHE[pid] = (ok, now + 30.0)
        return ok
    except Exception:
        return False

def _isBeamMPLauncherProcess(pid):
    """区分是不是 BeamMP Launcher 进程 (不想匹配它的 TCP, 只想匹配 BeamNG)"""
    try:
        import subprocess, time as _t
        if pid <= 0: return False
        # 共享进程名缓存 key 加"L"区分
        key = ("L", pid)
        now = _t.monotonic()
        if key in _PROCESS_CACHE:
            ok, expires = _PROCESS_CACHE[key]
            if expires > now: return ok
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=5, creationflags=0x08000000)
        out = ""
        for enc in ("utf-8","gbk","latin-1"):
            try: out = r.stdout.decode(enc); break
            except: continue
        if not out: out = (r.stdout or b"").decode("latin-1", errors="ignore")
        ok = ("launcher" in out.lower()) and ("beammp" in out.lower())
        _PROCESS_CACHE[key] = (ok, now + 30.0)
        return ok
    except Exception:
        return False

def isBeamNGConnectedToServer(server_port=None):
    """检测玩家是否已进入 BeamMP 服务器.

    v1.3.1 关键修复:
      BeamMP 架构是 BeamNG mod 只跟本地 Launcher (127.0.0.1:4444/4445) 通信,
      由 Launcher 自己去连远端 BeamMP 服务器 (12123 等).
      所以在 BeamNG 进程的 TCP 里**永远找不到**远端服务器连接, 必须看 Launcher 的 TCP!

    新策略:
      1. 找出所有 BeamMP-Launcher.exe 进程的 PID
      2. 枚举本机 ESTABLISHED TCP, 筛出 PID 属于 Launcher 的连接
      3. 排除 127.x / 0.0.0.0 / ::1 的本地连接, 剩下的就是 Launcher 连远端服务器
      4. 命中条件 A: 远端端口 in BEAMMP_DEFAULT_PORTS (12123/12125/2694 等)
      5. 命中条件 B: 远端端口 >= 1024 且 远端IP 不是 127/192.168/10/172.16-31/169.254
      6. 兜底: 如果没找到 Launcher, 退而求其次看 BeamNG 是否已建立 127.0.0.1:4444/4445
         (说明 mod 已连 Launcher, 但 Launcher 还没连服务器, 报告"已开游戏但未进服务器")
    """
    conns = _getEstablishedTcpConnections()
    total = len(conns)
    launcher_conns = []
    beamng_conns = []
    launcher_pids_seen = set()
    beamng_pids_seen = set()
    match = None

    # 第一遍: 找 BeamMP-Launcher 进程的 TCP (这才是连服务器的进程)
    for lp, la, ra, rp, pid in conns:
        if pid <= 4: continue
        if _isBeamMPLauncherProcess(pid):
            launcher_pids_seen.add(pid)
            match_reason = None
            # 条件 A: 端口匹配
            if server_port and rp == server_port:
                match_reason = f"匹配指定端口 {server_port}"
            elif rp in BEAMMP_DEFAULT_PORTS:
                match_reason = f"匹配 BeamMP 默认端口 {rp}"
            # 条件 B: 非本机/非内网的 ESTABLISHED (允许 192.168.x.x 给 Launcher, 因为 Launcher 连任何非 loopback 都是服务器连接)
            if not match_reason:
                is_local = (ra.startswith("127.") or ra in ("0.0.0.0","::1") or
                            ra.startswith("10.") or ra.startswith("169.254.") or
                            (ra.startswith("172.") and 16 <= int(ra.split(".")[1]) <= 31))
                # v1.3.1: 对 Launcher 进程, 允许 192.168.x.x (局域网对战也是真实服务器连接)
                if pid in launcher_pids_seen:
                    is_local = is_local and not ra.startswith("192.168.")
                if not is_local and rp >= 1024:
                    match_reason = f"非标准端口匹配 (远端 {ra}:{rp} 为外部 ESTABLISHED)"
            conn_type = match_reason if match_reason else "launcher-local"
            launcher_conns.append((lp, la, ra, rp, pid, conn_type))
            if match_reason and not match:
                match = (ra, rp, pid, match_reason)
        elif _isBeamNGProcess(pid):
            beamng_pids_seen.add(pid)
            beamng_conns.append((lp, la, ra, rp, pid, "beamng-local"))

    # 兜底: 如果没找到 Launcher 的远端连接, 但找到 BeamNG 已连 4444/4445
    # 报告状态让用户知道 "已开游戏但还没点 Connect"
    fallback_local = None
    if not match and beamng_conns:
        for lp, la, ra, rp, pid, _ in beamng_conns:
            if ra.startswith("127.") and rp in (4444, 4445):
                fallback_local = (ra, rp, pid)
                break

    ok = (match is not None)
    debug = {
        "total_est": total,
        "launcher_conns": launcher_conns,
        "beamng_conns": beamng_conns,
        "matched": match,
        "launcher_pids": sorted(launcher_pids_seen),
        "beamng_pids": sorted(beamng_pids_seen),
        "fallback_local_handshake": fallback_local,  # (ip, port, pid) 或 None
    }
    return ok, debug

class AutoWatchdogThread(threading.Thread):
    """后台自动监测线程:
    每 3s 用 iphlpapi 检查 BeamNG 是否已连接 BeamMP 服务器,
    一旦检测到 -> 等待 8s 让加载完成 -> 自动模拟键盘发送 /bmpid <uuid>
    完成后 stop 监听"""
    def __init__(self, hwid_uuid, on_log=None, on_sent=None, server_port=None, delay_secs=8.0):
        super().__init__(daemon=True)
        self.hwid_uuid   = hwid_uuid
        self.short_cmd   = buildBmpidShortCommand(hwid_uuid)
        self.on_log      = on_log or (lambda msg: None)
        self.on_sent     = on_sent or (lambda ok, msg: None)
        self.server_port = server_port
        self.delay_secs  = delay_secs
        self.stop_flag   = threading.Event()
        self._sent       = False  # 是否已发送过
        self._sent_lock  = threading.Lock()

    def run(self):
        self.on_log("[AutoWatch] 启动 - 每 3s 检查 BeamNG 是否已连接 BeamMP 服务器")
        attempts = 0
        last_summary_attempt = -99
        while not self.stop_flag.is_set():
            attempts += 1
            connected, debug = isBeamNGConnectedToServer(self.server_port)
            if connected:
                ra, rp, pid, reason = debug["matched"]
                self.on_log(f"[AutoWatch] ✅ 检测到 BeamNG 已连接服务器: {reason}")
                self.on_log(f"[AutoWatch]   远端: {ra}:{rp}, PID={pid}, "
                            f"本机ESTABLISHED共 {debug['total_est']} 条, "
                            f"BeamNG TCP {len(debug['beamng_conns'])} 条")
                self.on_log(f"[AutoWatch] 等待 {self.delay_secs}s 让游戏加载完成...")
                # 分段 sleep 以便能及时响应 stop
                slept = 0.0
                while slept < self.delay_secs and not self.stop_flag.is_set():
                    time.sleep(0.5); slept += 0.5
                if self.stop_flag.is_set(): return
                with self._sent_lock:
                    if self._sent:
                        return
                    self._sent = True
                self.on_log(f"[AutoWatch] 🚀 自动发送 {self.short_cmd[:60]}... 到 BeamNG 聊天框")
                ok, msg = sendChatToForeground(self.short_cmd)
                self.on_log(f"[AutoWatch] 发送结果 ok={ok}: {msg}")
                self.on_sent(ok, msg)
                return
            # 未连接: 记录更详细的调试概要（每 5 次打一次）
            if attempts - last_summary_attempt >= 5:
                last_summary_attempt = attempts
                total = debug["total_est"]
                bn = len(debug["beamng_conns"])
                ln = len(debug["launcher_conns"])
                bpids = debug["beamng_pids"]
                lpids = debug["launcher_pids"]
                fallback = debug.get("fallback_local_handshake")
                if bn == 0 and not bpids and ln == 0 and not lpids:
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: 暂未发现 BeamNG/Launcher 进程 "
                                f"(本机 ESTABLISHED {total} 条). 请先打开 BeamMP Launcher 并 Connect 到服务器")
                elif ln == 0 and not lpids:
                    # 有 BeamNG 但没 Launcher
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: 发现 BeamNG PID={bpids} "
                                f"但未发现 BeamMP Launcher (本机 ESTABLISHED {total}). "
                                f"请用 BeamMP Launcher 启动游戏, 不要直接双击 BeamNG.drive.exe")
                elif ln > 0 and not fallback and not lpids:
                    # 有 Launcher 但 BeamNG 还没连上 Launcher
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: Launcher PID={lpids} "
                                f"已运行, 但 BeamNG 还没建立 4444/4445 本地握手 "
                                f"(Launcher TCP {ln} 条). BeamNG 仍在启动中...")
                elif fallback and not lpids:
                    # 有 BeamNG 连 4444/4445 但 Launcher 还没远端连服务器
                    ra, rp, pid = fallback
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: BeamNG 已连 Launcher "
                                f"(127.0.0.1:{rp} PID={pid}), 但 Launcher 还没连远端服务器. "
                                f"请在 Launcher 里点 Connect 进入服务器")
                elif ln > 0:
                    # 有 Launcher 但所有 TCP 都是本地的 (没远端连服务器)
                    llines = []
                    for lp, la, ra, rp, pid, tp in debug["launcher_conns"][:6]:
                        llines.append(f"{lp}->{ra}:{rp} (PID{pid}, {tp})")
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: Launcher TCP {ln} 条 "
                                f"[{'; '.join(llines)}] — 未命中远端服务器. "
                                f"请在 Launcher 里点 Connect 进入服务器")
                else:
                    self.on_log(f"[AutoWatch] ⏳ 等待 {attempts} 次: 未命中 "
                                f"(BeamNG {bn}条/Launcher {ln}条). 请在 Launcher 里点 Connect")
            self.stop_flag.wait(3.0)
        self.on_log("[AutoWatch] 已停止")

    def stop(self):
        self.stop_flag.set()

    @property
    def already_sent(self):
        return self._sent

# ============================================================
#  Part 4: 本地 HTTP 服务 (127.0.0.1:7788)  —— 给将来 Lua / 外部工具扩展用
# ============================================================
HTTP_PORT = 7788
# 全局发送锁 (zip 触发 + AutoWatchdog 触发可能并发, 互斥防止重复发送)
_BRIDGE_SEND_LOCK = threading.Lock()
_BRIDGE_ALREADY_SENT = False

class _Handler(BaseHTTPRequestHandler):
    bridge_ctx = None  # 由 _HttpServerThread 注入 {hwid, sources, data, command}
    def log_message(self, fmt, *args): pass  # 静默
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/status", "/hwid"):
            c = self.bridge_ctx
            self._send_json({
                "ok": True,
                "name": "BMPHWID_Bridge/v1.3.1",
                "hwid": c["hwid"],
                "sources_used": c["sources"],
                "payload_dict": c["data"],
                "command": c["command"],
                "command_len": len(c["command"]),
            })
        else:
            self._send_json({"ok":False, "error":"not found"}, 404)
    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/sendchat":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            text = None
            try:
                if raw:
                    j = json.loads(raw.decode("utf-8"))
                    text = j.get("text") or j.get("command")
                if not text:
                    q = parse_qs(u.query)
                    if "text" in q: text = q["text"][0]
                    if "command" in q and not text: text = q["command"][0]
            except Exception:
                pass
            if not text:
                return self._send_json({"ok":False, "error":"missing 'text' in body/query"}, 400)
            # 全局锁防止 zip 多次触发 + AutoWatchdog 同时触发重复发送
            global _BRIDGE_SEND_LOCK, _BRIDGE_ALREADY_SENT
            with _BRIDGE_SEND_LOCK:
                if _BRIDGE_ALREADY_SENT:
                    return self._send_json({"ok":True, "detail":"already sent earlier, skip", "text":text[:200], "skipped":True})
                _BRIDGE_ALREADY_SENT = True
            # 异步执行模拟键盘 (避免 HTTP 阻塞 zip curl)
            threading.Thread(target=lambda: sendChatToForeground(text), daemon=True).start()
            self._send_json({"ok": True, "detail":"queued for keyboard send", "text": text[:200]})
        else:
            self._send_json({"ok":False, "error":"not found"}, 404)

class _HttpServerThread(threading.Thread):
    def __init__(self, ctx):
        super().__init__(daemon=True)
        self.ctx = ctx
    def run(self):
        class _H(_Handler): bridge_ctx = self.ctx
        for attempt in range(3):
            try:
                srv = HTTPServer(("127.0.0.1", HTTP_PORT), _H)
                srv.serve_forever()
            except OSError:
                break  # 端口被占用 = 已有老版本 bridge 占着
            except Exception:
                time.sleep(0.5)

# ============================================================
#  Part 5: Tkinter GUI  (Python 标准库自带，打包后 ~9MB)
# ============================================================
def _run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext

    # 一启动就算好 hwid
    hwid, sources = collect_hwid()
    data = buildPayloadDict(hwid, sources)
    command = buildBmpidChatCommand(data)
    ctx  = {"hwid": hwid, "sources": sources, "data": data, "command": command}

    win = tk.Tk()
    win.title("BMPHWID Bridge  v1.3.1   —   外置稳定 HWID 桥接")
    win.geometry("880x820")
    win.minsize(820, 720)

    pad = {"padx": 14, "pady": 6}
    big = ("Segoe UI", 11)
    mono = ("Consolas", 10)
    head = ("Segoe UI", 14, "bold")

    # --- Header ---
    ttk.Label(win, text="🔧 BMPHWID Bridge — 稳定 HWID 一键发送", font=head).pack(anchor="w", **pad)
    ttk.Label(win,
              text="下面这个 HWID 在你这台电脑上【永久不变】，guest 数字变了 / IP 换了 / 游戏重开了都不会变：",
              font=big, wraplength=780, justify="left").pack(anchor="w", padx=14)

    frm = ttk.Frame(win) ; frm.pack(fill="x", **pad)
    ttk.Label(frm, text="稳定 HWID：", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
    hwid_var = tk.StringVar(value=hwid)
    hwid_ent = ttk.Entry(frm, textvariable=hwid_var, font=("Consolas", 12, "bold"), width=52, state="readonly")
    hwid_ent.grid(row=0, column=1, sticky="we", padx=6)
    frm.columnconfigure(1, weight=1)
    def copy_hwid():
        win.clipboard_clear(); win.clipboard_append(hwid)
        messagebox.showinfo("已复制", "HWID 已复制到剪贴板。")
    ttk.Button(frm, text="📋 复制 HWID", command=copy_hwid).grid(row=0, column=2, padx=6)

    # --- Sources ---
    ttk.Label(win, text="采集来源（用于确认 HWID 稳定性，至少有 1 项 MachineGuid 就非常稳定了）：",
              font=big).pack(anchor="w", padx=14)
    txt_src = scrolledtext.ScrolledText(win, height=6, font=mono)
    txt_src.pack(fill="x", padx=14)
    if sources:
        txt_src.insert("1.0", "\n".join(" ✔  " + s for s in sources))
    else:
        txt_src.insert("1.0", " ⚠️  所有系统接口采集失败，使用 AppData 本地保存的 UUID（仍稳定）。")
    txt_src.configure(state="disabled")

    # --- Command preview (默认显示超短的裸 UUID 命令，最稳) ---
    short_cmd = buildBmpidShortCommand(hwid)         # /bmpid xxxxxxxx-...-xxxxxxxxxxxx  (43 字符, 绝不被截断)
    full_cmd  = buildBmpidChatCommand(data)         # /bmpid F|xxx                       (493 字符, 会被聊天框截断)
    chunked_cmds, total_chunks = buildBmpidChunkedCommands(data)
    preview_cmd = short_cmd                        # 默认预览短命令
    txt_cmd = scrolledtext.ScrolledText(win, height=5, font=mono, bg="#1e1e1e", fg="#dcdcaa",
                                        insertbackground="#ffffff")
    txt_cmd.pack(fill="both", expand=False, padx=14, pady=(4, 10))
    txt_cmd.insert("1.0", preview_cmd)
    txt_cmd.configure(state="disabled")
    preview_var = tk.StringVar(value=f"↑ 当前显示：短UUID命令 ({len(short_cmd)} 字符 ✅ 绝不被聊天框截断)")
    ttk.Label(win, textvariable=preview_var,
              font=("Segoe UI", 10), foreground="#2b8a3e").pack(anchor="w", padx=14)

    # --- Action buttons (短UUID是主推,因为服务端已支持裸UUID解析) ---
    btns = ttk.Frame(win); btns.pack(fill="x", **pad)
    def copy_short():
        win.clipboard_clear(); win.clipboard_append(short_cmd)
        messagebox.showinfo("已复制 (短UUID命令)",
            f"已复制：\n{short_cmd}\n\n长度 {len(short_cmd)} 字符，"
            f"切到 BeamNG 游戏里按 T → Ctrl+V → 回车 即可发送。\n"
            f"服务端 main.lua 已支持裸 UUID 直接解析，会自动绑定 P0 级最高优先级 HWID。")
    def send_short():
        ok, msg = sendChatToForeground(short_cmd)
        if ok:
            messagebox.showinfo("一键发送 (短UUID)", msg)
        else:
            messagebox.showwarning("一键发送失败", msg)
    def send_chunked():
        # 分块发送 (用于把完整 6 字段 payload 一并传过去)
        if total_chunks <= 1:
            ok, msg = sendChatToForeground(full_cmd)
            messagebox.showinfo("一键发送 (完整单帧)", msg if ok else f"失败: {msg}")
            return
        # 多块: 0.5s 间隔依次发送每块
        threading.Thread(target=lambda: _send_chunks_thread(chunked_cmds), daemon=True).start()
        messagebox.showinfo("一键发送 (分块)",
            f"将连续发送 {total_chunks} 块聊天消息（每块间隔 0.5s），保持 BeamNG 在前台，请勿切窗口。\n\n"
            f"完成后游戏里会收到服务器的 HWID 接收确认。")
    def _send_chunks_thread(cmds):
        for i, c in enumerate(cmds, 1):
            ok, msg = sendChatToForeground(c)
            time.sleep(0.5)
            win.after(0, lambda i=i, ok=ok, m=msg: _log(f"块 {i}/{len(cmds)} → ok={ok}"))
        win.after(0, lambda: _log(f"✅ 所有 {len(cmds)} 块发送完毕"))

    ttk.Button(btns, text="1️⃣  发送短UUID  ⭐主推 (43字符)", command=send_short, width=34).pack(side="left", padx=8, ipadx=8, ipady=8)
    ttk.Button(btns, text="2️⃣  复制短UUID  (手动粘贴)", command=copy_short, width=28).pack(side="left", padx=8, ipadx=8, ipady=8)
    ttk.Button(btns, text="3️⃣  发送完整payload (分块)", command=send_chunked, width=30).pack(side="left", padx=8, ipadx=8, ipady=8)

    # --- Auto Watchdog (新增 v1.1): 自动检测进服务器后自动发 ---
    auto_frame = ttk.LabelFrame(win, text="🤖 自动模式 (推荐开启): 检测到 BeamNG 连服务器后自动发送")
    auto_frame.pack(fill="x", padx=14, pady=(10, 0))
    auto_var   = tk.BooleanVar(value=True)  # 默认开
    auto_status_var = tk.StringVar(value="状态：等待 BeamNG 连接服务器...")
    watchdog_ref = {"thread": None}

    def _auto_log(msg):
        ts = time.strftime("%H:%M:%S")
        win.after(0, lambda: _log(msg))
        win.after(0, lambda: auto_status_var.set(f"状态：{msg}"))

    def _auto_sent_callback(ok, msg):
        if ok:
            win.after(0, lambda: messagebox.showinfo("自动发送成功",
                f"Bridge 自动检测到你已进服务器并发送了 HWID！\n\n{msg}\n\n"
                "现在去游戏里 /login 你的账号，登录后这个 HWID 会永久绑定。"))
        else:
            win.after(0, lambda: messagebox.showwarning("自动发送失败",
                f"Bridge 自动发送失败，请手动点【1️⃣ 发送短UUID】按钮。\n\n{msg}"))

    def start_auto():
        if watchdog_ref["thread"] and watchdog_ref["thread"].is_alive():
            return
        t = AutoWatchdogThread(
            hwid_uuid=hwid,
            on_log=_auto_log,
            on_sent=_auto_sent_callback,
            server_port=None,        # v1.3 不再硬编码端口, 交给新的智能匹配逻辑 (BeamNG PID枚举 + 默认端口表 + 非标准端口兜底)
            delay_secs=8.0,
        )
        watchdog_ref["thread"] = t
        t.start()
        _auto_log("[Auto] 自动检测已启动 - 进服务器 8 秒后会自动发送 HWID")

    def stop_auto():
        """v1.3 停止 AutoWatchdog 线程 (补回之前误删的函数)"""
        if watchdog_ref["thread"]:
            try:
                watchdog_ref["thread"].stop()
            except Exception:
                pass
            watchdog_ref["thread"] = None
        _auto_log("[Auto] 自动检测已停止")

    def run_manual_detect():
        """v1.3.1 手动立即检测一次 (Debug), 弹窗显示详细结果 + 写日志
        (v1.3.1: 改成检测 BeamMP-Launcher 的 TCP, 因为 BeamNG 只连本地 Launcher)"""
        _log("[Debug] 🔬 立即执行手动检测 isBeamNGConnectedToServer() ...")
        try:
            connected, debug = isBeamNGConnectedToServer(server_port=None)
        except Exception as e:
            _log(f"[Debug] 检测异常: {e!r}")
            messagebox.showerror("检测异常", repr(e))
            return
        total   = debug["total_est"]
        lconns  = debug["launcher_conns"]
        bconns  = debug["beamng_conns"]
        lpids   = debug["launcher_pids"]
        bpids   = debug["beamng_pids"]
        match   = debug["matched"]
        fallback = debug.get("fallback_local_handshake")
        _log(f"[Debug] 结果: connected={connected}, 本机 ESTABLISHED={total}, "
             f"Launcher_PIDs={lpids}, Launcher_TCP={len(lconns)}, "
             f"BeamNG_PIDs={bpids}, BeamNG_TCP={len(bconns)}")
        lines = [
            f"手动检测结果 (v1.3.1 - 看 Launcher TCP):",
            f"  ✅ 检测到连接:     {'YES' if connected else 'NO'}",
            f"  🔗 本机ESTABLISHED:  {total} 条",
            f"  🚀 Launcher 进程PID: {lpids if lpids else '(未发现 BeamMP-Launcher.exe 进程)'}",
            f"  📶 Launcher TCP:     {len(lconns)} 条",
            f"  🚗 BeamNG 进程PID:   {bpids if bpids else '(未发现 BeamNG.drive.exe 进程)'}",
            f"  📶 BeamNG TCP:      {len(bconns)} 条 (只连本地 4444/4445)",
        ]
        if match:
            ra, rp, pid, reason = match
            lines.append(f"  🎯 匹配到的服务器:   {ra}:{rp} (PID {pid}, 原因: {reason})")
        else:
            lines.append(f"  🎯 匹配到的服务器:   (无 - Launcher 没连远端服务器)")
        if fallback:
            ra, rp, pid = fallback
            lines.append(f"  ⚠️  本地握手:        BeamNG 已连 127.0.0.1:{rp} PID={pid}")
            lines.append(f"     说明: mod 已连 Launcher, 但 Launcher 还没连服务器 (没点 Connect?)")
        if lconns:
            lines.append("")
            lines.append("  —— Launcher 所有 ESTABLISHED TCP 明细 ——")
            for i, (lp, la, ra, rp, pid, tp) in enumerate(lconns, 1):
                lines.append(f"     {i}. {la}:{lp}  ->  {ra}:{rp}   PID={pid}   type={tp}")
        if bconns:
            lines.append("")
            lines.append("  —— BeamNG 所有 ESTABLISHED TCP 明细 (应该全是 127.0.0.1) ——")
            for i, (lp, la, ra, rp, pid, tp) in enumerate(bconns, 1):
                lines.append(f"     {i}. {la}:{lp}  ->  {ra}:{rp}   PID={pid}   type={tp}")
        if not connected:
            lines.append("")
            if not lpids and not bpids:
                lines.append("  💡 建议: 没发现 BeamNG/Launcher 进程, 请先打开 BeamMP Launcher")
            elif not lpids and bpids:
                lines.append("  💡 建议: 找到 BeamNG 但没 Launcher, 请用 BeamMP Launcher 启动游戏, 不要直接双击 BeamNG.drive.exe")
            elif lpids and not fallback:
                lines.append("  💡 建议: Launcher 已运行但 BeamNG 还没建立 4444/4445 握手, BeamNG 仍在启动")
            elif fallback and not match:
                lines.append("  💡 建议: BeamNG 已连 Launcher, 请在 Launcher 里点 Connect 进入服务器")
            elif lpids and not match:
                lines.append("  💡 建议: Launcher 已运行但没远端 TCP, 请在 Launcher 里点 Connect")
        msg_text = "\n".join(lines)
        _log("[Debug] " + " | ".join(lines[:6]))
        # 用顶层窗口显示详细信息 (messagebox 有时会截断太长内容, 用 ScrolledText Toplevel)
        top = tk.Toplevel(win); top.title("AutoWatchdog 检测结果 Debug (v1.3.1)")
        top.geometry("780x600")
        st = scrolledtext.ScrolledText(top, font=("Consolas", 10), wrap="none")
        st.pack(fill="both", expand=True, padx=8, pady=8)
        st.insert("1.0", msg_text)
        st.configure(state="disabled")
        ttk.Button(top, text="关闭", command=top.destroy).pack(pady=6)

    auto_ctrl = ttk.Frame(auto_frame); auto_ctrl.pack(fill="x", padx=10, pady=6)
    ttk.Checkbutton(auto_ctrl, text="启动时自动检测 (进服务器自动绑定 HWID)", variable=auto_var).pack(side="left")
    ttk.Button(auto_ctrl, text="立即启动自动检测", command=start_auto).pack(side="left", padx=10)
    ttk.Button(auto_ctrl, text="停止", command=stop_auto).pack(side="left", padx=4)
    ttk.Button(auto_ctrl, text="🔬 立即检测一次 (Debug)",
               command=run_manual_detect).pack(side="left", padx=12)
    ttk.Label(auto_frame, textvariable=auto_status_var, font=("Segoe UI", 10),
              foreground="#8850bf").pack(anchor="w", padx=10, pady=(0, 6))

    # --- HTTP Status ---
    http_status_var = tk.StringVar(value=f"🌐 本地 HTTP 服务：http://127.0.0.1:{HTTP_PORT}   (可选，已自动启动)")
    ttk.Label(win, textvariable=http_status_var, font=("Segoe UI", 10),
              foreground="#2b8a3e").pack(anchor="w", padx=14, pady=(12, 0))
    ttk.Label(win,
              text=f"     GET  http://127.0.0.1:{HTTP_PORT}/hwid          → JSON 返回 HWID、命令"
                   f"\n     POST http://127.0.0.1:{HTTP_PORT}/sendchat      → Body: {{\"text\":\"你要发的聊天内容\"}}，由 Bridge 自动敲键盘发送",
              font=mono).pack(anchor="w", padx=22)

    # --- Log area ---
    ttk.Label(win, text="日志：", font=big).pack(anchor="w", padx=14, pady=(10, 2))
    logbox = scrolledtext.ScrolledText(win, height=14, font=mono, state="disabled")
    logbox.pack(fill="both", expand=True, padx=14, pady=(0, 12))
    def _log(line):
        ts = time.strftime("%H:%M:%S")
        logbox.configure(state="normal")
        logbox.insert("end", f"[{ts}] {line}\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    _log(f"[启动] 采集到稳定 HWID = {hwid}")
    _log(f"[启动] 采集来源数量：{len(sources)}")
    if sources:
        for s in sources: _log("        · " + s)
    _log(f"[启动] 短UUID命令: {short_cmd}  ({len(short_cmd)} 字符 ✅ 绝不被聊天框截断)")
    _log(f"[启动] 完整payload: {len(full_cmd)} 字符，会拆成 {total_chunks} 块发送 (3️⃣ 按钮)")
    # 启 HTTP
    t = _HttpServerThread(ctx) ; t.start()
    _log(f"[HTTP] 监听 127.0.0.1:{HTTP_PORT}  ——  在浏览器打开 http://127.0.0.1:{HTTP_PORT}/hwid 可直接验证")

    # 默认启动自动检测 (auto_var 默认 True)
    if auto_var.get():
        start_auto()

    win.mainloop()


if __name__ == "__main__":
    # 如果有参数 --cli 就打印命令不弹 GUI（给 Launcher 静默采集用）
    if len(sys.argv) > 1 and sys.argv[1] in ("-c", "--cli", "--stdout"):
        hwid, sources = collect_hwid()
        data = buildPayloadDict(hwid, sources)
        cmd_full   = buildBmpidChatCommand(data)
        cmd_short  = buildBmpidShortCommand(hwid)
        chunks, total = buildBmpidChunkedCommands(data)
        print(json.dumps({
            "hwid": hwid,
            "sources": sources,
            "short_command": cmd_short,
            "short_command_len": len(cmd_short),
            "full_command": cmd_full,
            "full_command_len": len(cmd_full),
            "chunked_commands": chunks,
            "chunked_total": total,
        }, ensure_ascii=False, indent=2))
    else:
        try:
            _run_gui()
        except Exception as gui_err:
            # GUI 环境失败（SSH/Headless）降级到 CLI
            hwid, sources = collect_hwid()
            data = buildPayloadDict(hwid, sources)
            cmd_short = buildBmpidShortCommand(hwid)
            print(f"[GUI 打开失败: {gui_err}]  降级输出:\n")
            print(json.dumps({"hwid":hwid,"sources":sources,"short_command":cmd_short}, ensure_ascii=False, indent=2))
