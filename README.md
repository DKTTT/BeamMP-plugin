# BeamMP 认证 & 管理插件

BeamMP 服务器账号认证系统 + 公屏聊天 + 管理员后台，支持 **不进游戏直接通过 GUI 客户端操作**。

## 📦 组件总览

```
┌─────────────────┐     HTTP API      ┌──────────────────┐     Lua 事件      ┌─────────────────┐
│  BMPHWID_Bridge  │ ──────────────────► │  BMP_HTTP_API     │ ─────────────────► │  BeamMP Server   │
│  (玩家客户端)    │  /api/auth/*      │  (服务器后端)     │  main.lua        │  (游戏内聊天)    │
└─────────────────┘  /api/chat/send    └──────────────────┘  MP.SendChatMsg  └─────────────────┘
        │                                       ▲
        │ 本地 7788 HWID 采集                    │ 管理员 API
        ▼                                       │
┌─────────────────┐                    ┌──────────────────┐
│  HWID 采集服务   │                    │   BMP_Admin      │
│  (机器码+网卡)   │                    │  (管理员后台)    │
└─────────────────┘                    └──────────────────┘
```

| 组件 | 文件 | 作用 |
|---|---|---|
| 玩家认证器 | `BMPHWID_Bridge.py` / `dist/BMPHWID_Bridge.exe` | HWID 采集 + 账号登录/注册/公屏聊天 |
| HTTP API | `BMP_HTTP_API.py` | 服务器端 HTTP 接口，读写 `accounts.json` |
| 管理员后台 | `BMP_Admin.py` / `dist/BMP_Admin.exe` | 账号管理/封禁/聊天广播/统计 |
| 服务器插件 | `Resources/Server/BMPLogin/main.lua` | BeamMP 游戏内聊天认证 + 聊天队列轮询 |
| 启动脚本 | `start_BMP_API.bat` / `start_Bridge.bat` | 一键启动 |

---

## 🚀 快速开始

### 服主（首次部署）

```powershell
# 1. 安装 Python 3.10+ (无需任何第三方依赖)
#    https://www.python.org/downloads/

# 2. 编辑 start_BMP_API.bat 顶部的 CONFIG 区, 设置:
#    ADMINS=DRIFTKING,你的管理员账号
#    PORT=12124
#    ALLOW_IPS=               # 可选: 限制玩家 IP 段

# 3. 防火墙放行 12124 端口 (管理员 PowerShell)
New-NetFirewallRule -DisplayName "BMP HTTP API" -Direction Inbound -Protocol TCP -LocalPort 12124 -Action Allow

# 4. 双击 start_BMP_API.bat 启动后端
#    看到 "监听 0.0.0.0:12124" 表示成功

# 5. 双击 dist/BMP_Admin.exe 打开管理员后台
#    顶部填 http://127.0.0.1:12124 → 测试连通 → 管理员登录
```

### 玩家（使用）

```
1. 双击 dist/BMPHWID_Bridge.exe
2. 切 Tab3「直连服务器」
3. 顶部 API 地址填服主给的地址 (如 http://118.xxx.xxx.xxx:12124)
4. 点「测试连通」→ 灯变绿
5. 注册/登录 → 自动获取认证身份 (5 辆车)
6. Tab3「公屏聊天」→ 发消息直接到游戏公屏, 不用开游戏
```

---

## 🔌 API 列表

### 玩家 API（HTTP POST JSON）

| 端点 | 说明 | 需登录 |
|---|---|---|
| `/api/auth/register` | `{username, password, hwid}` | ❌ |
| `/api/auth/login` | `{username, password, hwid}` → 返回 token | ❌ |
| `/api/auth/logout` | Header: `Bearer <token>` | ✅ |
| `/api/auth/whoami` | 查看当前身份/绑定/车辆上限 | ✅ |
| `/api/hwid/bind` | 绑定 HWID 到账号 | 可选 |
| `/api/vehicle/limit` | 查询车辆上限 | ✅ |
| `/api/chat/send` | 公屏聊天广播 | 可选 |
| `GET /api/ping` | 健康检查 | ❌ |

### 管理员 API（需管理员 token）

| 端点 | 说明 |
|---|---|
| `/api/admin/accounts` | 列出所有账号 |
| `/api/admin/accounts/delete` | 删除账号 |
| `/api/admin/accounts/reset-password` | 重置密码 |
| `/api/admin/accounts/toggle-admin` | 设/取消管理员 |
| `/api/admin/accounts/unbind` | 解绑 HWID |
| `/api/admin/ban` | 封禁 (账号/HWID/IP) |
| `/api/admin/unban` | 解封 |
| `/api/admin/banlist` | 封禁列表 |
| `/api/admin/chat-queue` | 聊天队列 |
| `/api/admin/chat-queue/send` | 管理员广播 `[Admin]` 前缀 |
| `/api/admin/chat-queue/clear` | 清空队列 |
| `/api/admin/stats` | 服务器统计 |

### 返回格式

```json
{ "ok": true, "msg": "登录成功", "data": { ... } }
{ "ok": false, "error": "账号或密码不正确" }
```

---

## 📊 车辆权限

| 角色 | 车辆上限 | 获得方式 |
|---|---|---|
| 管理员 | 999 | `--admins` 参数 |
| 已认证玩家 | 5 | 登录账号 + 绑定 HWID |
| 未认证游客 | 1 | 不登录直接进服 |

---

## 🔒 安全措施

| 措施 | 说明 |
|---|---|
| 密码存储 | 盐值哈希 (非明文, 与 Lua 插件双向兼容) |
| Token | `secrets.token_urlsafe` CSPRNG + 7 天 TTL |
| 登录速率限制 | 5 次/5 分钟, 锁定 10 分钟 (防暴力破解) |
| 请求体限制 | 64 KB (防内存耗尽) |
| 账号枚举防护 | 统一错误消息, 不暴露账号是否存在 |
| 安全响应头 | `X-Content-Type-Options` / `X-Frame-Options` / `Cache-Control` |
| 审计日志 | 管理员所有写操作记录到 `admin_audit.jsonl` |
| HTTPS/TLS | `--tls-cert` + `--tls-key` 启用 TLS 1.2+ |
| Token 本地存储 | Bridge/Admin 只存 SHA-256 hash, 不留明文 |
| IP 白名单 | `--allow-ips 114.0.0.0/8,192.168.0.0/16` |
| 聊天速率 | 6 条/分钟 |

---

## 📂 目录结构

```
BeamMP-plugin/
├── BMP_HTTP_API.py              # 服务器 HTTP API 后端
├── BMPHWID_Bridge.py            # 玩家认证器源码
├── BMP_Admin.py                 # 管理员后台源码
├── Resources/Server/BMPLogin/main.lua  # BeamMP 服务端 Lua 插件
├── bmp_login/
│   ├── accounts.json            # 账号数据 (密码 hash)
│   ├── guestmap.json            # HWID → 账号映射
│   ├── banlist.json             # 封禁列表
│   ├── chat_queue.json          # 待广播聊天队列
│   └── admin_audit.jsonl        # 管理员操作审计日志
├── dist/
│   ├── BMPHWID_Bridge.exe       # 玩家认证器打包
│   └── BMP_Admin.exe            # 管理员后台打包
├── start_BMP_API.bat           # 服主: 启动 API
├── start_Bridge.bat            # 玩家: 启动 Bridge
├── start_ALL.bat               # 一键全起
└── stop_ALL.bat                # 一键停止
```

---

## 🔧 高级配置

### 启用 HTTPS (公网部署推荐)

```powershell
# 用 Let's Encrypt 或自签名证书
py -3 BMP_HTTP_API.py --host 0.0.0.0 --port 12124 `
    --admins DRIFTKING `
    --tls-cert cert.pem --tls-key key.pem `
    --data-dir bmp_login
```

### IP 白名单

```powershell
# 只允许特定 IP 段
py -3 BMP_HTTP_API.py --allow-ips "114.0.0.0/8,192.168.0.0/16"
```

### 多管理员

```powershell
py -3 BMP_HTTP_API.py --admins "DRIFTKING,admin2,admin3"
```

---

## 📋 变更日志

### v3.0.0 (2026-08-27)
- ✅ 新增管理员后台 BMP_Admin (账号/封禁/聊天/统计 4 个 Tab)
- ✅ 全面安全加固 (速率限制/审计日志/安全头/TLS/Token hash)
- ✅ 公屏聊天 (Bridge → API → Lua → 游戏公屏)
- ✅ Bridge URL 自动补 `http://` 前缀
- ✅ 管理员操作审计日志

### v2.3.0 (2026-08-27)
- ✅ Bridge Tab3「直连服务器」(注册/登录/whoami/绑定/车辆/聊天)
- ✅ HTTP API 7 个玩家端点

### v2.1.0 (2026-08-26)
- ✅ Bridge 命令手册 Tab (4 分组 × 7 命令)

### 更早版本
- HWID 稳定采集 (MachineGuid + 主板 UUID + 网卡 MAC → FNV-1a → UUID)
- BeamMP mod 客户端 (BMPHWID.zip)
