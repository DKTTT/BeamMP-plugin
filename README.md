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
| 玩家认证器 | `BMPHWID_Bridge.py` / `dist/BMPHWID_Bridge.exe` | HWID 采集 + 公屏聊天（账号登录区已隐藏） |
| HTTP API | `BMP_HTTP_API.py` | 服务器端 HTTP 接口，读写 `accounts.json` |
| 管理员后台 | `BMP_Admin.py` / `dist/BMP_Admin.exe` | 账号管理/封禁/聊天广播/统计 |
| 服务器插件 | `Resources/Server/BMPLogin/main.lua` | BeamMP 游戏内认证 + 聊天队列轮询 |
| 启动脚本 | `start_BMP_API.bat` / `start_Bridge.bat` | 一键启动 |

---

## 🔑 认证方式（当前生效）

BeamMP 官方账号服务器恢复后，**本插件自带的"聊天框注册/登录"已关闭**，改为
**BeamMP 官方账号 + 机器码(HWID)** 双条件认证：

| 条件 | 怎么满足 | 在 `/whoami` 里看 |
|---|---|---|
| 官方账号已登录 | 在 BeamMP 启动器里登录自己的账号（游客身份不算） | `官方账号: 已登录 ✓` |
| 机器码已上报 | 开着 `BMPHWID_Bridge.exe`（客户端 mod 会主动拉取并上报） | `机器码: 已上报 ✓` |

两个都满足 = **授信用户**（5 辆车）；缺任意一个 = **未授信**（1 辆，能开但刷不了）。
管理员不看这两个条件，只认 `main.lua` 里的 `ADMIN_BEAMMP_IDS` 白名单（999 辆）。
这些状态在网页面板（`http://<IP>:12124/`）上能看到。

**怎么把自己设成管理员：**

1. 先进一次服务器（不用认证也行），在游戏里输 `/whoami`
2. 会显示 `官方账号ID: xxxxxxxxxxxxxxxx`，复制它
3. 打开 `Resources/Server/BMPLogin/main.lua`，填进：
   ```lua
   local ADMIN_BEAMMP_IDS = {
       ["你的官方账号ID"] = true,
   }
   ```
4. 重启 BeamMP 服务器（插件在启动时会打印白名单数量；为空会打警告）

**开关**（`main.lua` 顶部，改完重启服务器）：

```lua
local ENABLE_CHAT_LOGIN = false   -- 改回 true 就恢复旧的 /register /login 账号体系
local REQUIRE_OFFICIAL_ACCOUNT = true   -- 是否要求官方账号已登录
local REQUIRE_HWID = true               -- 是否要求机器码已上报
```

> 老的 `accounts.json` 和账号不会被删除，`ENABLE_CHAT_LOGIN = true` 之后照旧能用。

---

## 🚀 快速开始

### 服主（首次部署）

```powershell
# 1. 安装 Python 3.10+ (无需任何第三方依赖)
#    https://www.python.org/downloads/

# 2. 编辑 start_BMP_API.bat 顶部的 CONFIG 区, 设置:
#    PORT=12124
#    ALLOW_IPS=               # 可选: 限制玩家 IP 段
#
#    注意: 切到官方账号认证后, ADMINS= 里的账号名不再用于游戏内权限,
#          游戏内管理员请在 main.lua 的 ADMIN_BEAMMP_IDS 里配置。

# 3. 防火墙放行 12124 端口 (管理员 PowerShell)
New-NetFirewallRule -DisplayName "BMP HTTP API" -Direction Inbound -Protocol TCP -LocalPort 12124 -Action Allow

# 4. 双击 start_BMP_API.bat 启动后端
#    看到 "监听 0.0.0.0:12124" 表示成功

# 5. 双击 dist/BMP_Admin.exe 打开管理员后台
#    顶部填 http://127.0.0.1:12124 → 测试连通 → 管理员登录
```

### 玩家（使用）

```
1. 在 BeamMP 启动器里登录你的官方账号 (游客身份无法认证)
2. 双击 dist/BMPHWID_Bridge.exe  (保持开着, 它负责上报机器码)
3. 进服后在游戏里输 /whoami, 确认两项都是 ✓
4. 机器码由客户端的 BMPHWID mod 自动拉取并上报, 一般几秒内完成
5. 想用公屏聊天: 切 Tab3「直连服务器」填 API 地址 → Tab「公屏聊天」
   (未登录本服账号时会显示为 guest, 登录后显示账号名)
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
| `/api/chat/send` | 公屏聊天广播 (未登录显示为 `guest`, 不能自报名字) | 可选 |
| `/api/vote/kick` | 发起投票踢出 (发起人取登录账号) | ✅ |
| `/api/vote/cast` | 参与投票 (投票人取登录账号) | ✅ |
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
| 管理员 | 999 | 官方账号 ID 在 `main.lua` 的 `ADMIN_BEAMMP_IDS` 白名单里 |
| 授信用户 | 5 | BeamMP 官方账号已登录 **且** 机器码已上报 |
| 未授信 | 1 | 缺任意一个条件 (能开一辆, 想多刷就得认证) |

> 旧账号模式 (`ENABLE_CHAT_LOGIN = true`) 下: 管理员 999 / 已登录账号 5 / 游客 1。

---

## 📋 网页面板 (在线人数 / 在线玩家)

浏览器直接打开 **`http://<服务器IP>:12124/`**，不需要另外起 web 服务。

页面内容：

- **顶部大数字 = 在线人数**，下面三个小计：`授信` / `未授信` / `管理员`
- **表格**：玩家名 + 徽章、官方账号ID、**机器码（点一下复制）**、车辆数、在线时长
- 搜索框（名字 / 机器码 / 账号ID）、按状态筛选、自动刷新 2 秒、手动刷新
- 数据超过 30 秒没更新会在顶部弹黄色警告（说明 Lua 插件没在跑 / 服务器没开）

### 在线人数是怎么算的

只有一个口径：**`online_players.json` 里的玩家条数**。

```
main.lua  玩家进服/离服/每 3 秒  -->  online_players.json
                                          ↓
              BMP_HTTP_API  GET /api/players/live  -->  panel.html (每 2 秒)
```

游戏里广播的"当前在线 X 人"和面板上的数字来自同一个函数 `countOnlinePlayers()`，
所以两边永远一致。以前的写法是"进服时先数一遍 +1、离服时先数一遍 −1"，
遇到重复事件或"还没进列表就掉线"会永久偏移一个，这个坑已经修掉了。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页面板 (panel.html) |
| GET | `/api/players/live` | 面板数据（别名 `/api/players/online`） |
| GET | `/api/ping` | 健康检查 (JSON) |

`/api/players/live` 返回：

```json
{
  "ok": true,
  "data": {
    "count": 4,
    "counts": { "total": 4, "trusted": 2, "untrusted": 2, "admin": 1 },
    "players": [ { "playerID": 1001, "name": "DKSTD", "trusted": true, "is_admin": true,
                   "beammp_id": "acc_...", "hwid": "000003e9-...", "vehicle_count": 3,
                   "online_seconds": 493 } ],
    "updated_at": 1793276281,
    "stale": false
  }
}
```

> `panel.html` 必须和 `BMP_HTTP_API.py` **放在同一目录**，否则 `/` 会返回 404 并提示。

> 地图功能已经下线。服务端仍保留 `BMPHWID:Pos` 位置上报（只在内存里，不写盘），
> 想恢复实时地图时在 `_saveOnlinePlayers()` 里把 `data.pos` 加回来、
> 并在 `_players_live()` 里把 `pos` 字段放出去即可。

---

## 🔒 安全措施

| 措施 | 说明 |
|---|---|
| 密码存储 | 盐值哈希 (非明文, 与 Lua 插件双向兼容; 按 UTF-8 字节计算, 支持中文密码) |
| Token | `secrets.token_urlsafe` CSPRNG + 7 天 TTL |
| 登录速率限制 | 5 次/5 分钟, 锁定 10 分钟 (防暴力破解); 登录成功后清零 |
| 请求体限制 | 64 KB (防内存耗尽) |
| 账号枚举防护 | 统一错误消息, 不暴露账号是否存在 |
| 安全响应头 | `X-Content-Type-Options` / `X-Frame-Options` / `Cache-Control` |
| 审计日志 | 管理员所有写操作记录到 `admin_audit.jsonl` |
| HTTPS/TLS | `--tls-cert` + `--tls-key` 启用 TLS 1.2+ (证书出错直接拒绝启动, 不会静默降级明文) |
| Token 本地存储 | Bridge/Admin 存在用户目录的配置文件里 (必须可回读才能自动登录, 请勿分享该文件) |
| IP 白名单 | `--allow-ips 114.0.0.0/8,192.168.0.0/16` (POST 与 GET 全部生效) |
| 聊天速率 | 6 条/分钟 |
| 管理员账号名 | `--admins` 里的账号名是保留名, 不能被匿名注册抢注 |
| 发言身份 | `/api/chat/send` 不接受自报名字: 已登录用账号名, 未登录只能是 `guest` |
| 投票身份 | `/api/vote/*` 必须登录, 发起人/投票人一律取账号名; 按 IP 限速 6 次/分钟 |
| 数据文件保护 | `accounts.json` / `banlist.json` 解析失败时拒绝写入 (503), 不会用空库覆盖真实数据; 读到半个文件会自动重试, 不会误判 |
| Token 吊销 | 改密码 / 删账号 / 撤销管理员时立刻作废该账号所有 token |

---

## 📂 目录结构

```
BeamMP-plugin/
├── BMP_HTTP_API.py              # 服务器 HTTP API 后端
├── panel.html                   # 网页面板 (在线人数) —— 必须和 API 同目录
├── BMPHWID_Bridge.py            # 玩家认证器源码
├── BMP_Admin.py                 # 管理员后台源码
├── Resources/Server/BMPLogin/main.lua  # BeamMP 服务端 Lua 插件
├── Resources/Client/BMPHWID.zip # 客户端 mod (坐标上报 + 机器码上报)
├── bmp_login/
│   ├── accounts.json            # 账号数据 (密码 hash)
│   ├── guestmap.json            # HWID → 账号映射
│   ├── banlist.json             # 封禁列表
│   ├── chat_queue.json          # 待广播聊天队列
│   ├── online_players.json      # 在线玩家 (网页面板读它)
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

### v3.5.0 (2026-10-29) — 实时地图下线, 在线人数做对

- 🗑 **移除实时地图**：`panel.html` 重写成纯粹的「在线人数 / 在线玩家」页面
  （去掉了 canvas、底图、校准、坐标网格、箭头绘制）。服务端仍保留
  `BMPHWID:Pos` 位置上报，但只在内存里、不再写盘，随时可以恢复。
- 🔢 **在线人数口径统一**：新增 `countOnlinePlayers()`，游戏内广播与网页面板
  走同一个数。修掉两个会让数字永久偏移的老问题：
  - `onPlayerConnected` 用「先数一遍 + 1」，重复/重放事件会把同一个人算两次
  - `onPlayerDisconnect` 用「先数一遍 − 1」，玩家其实没进过列表（进服过程中
    掉线）时会少算一个，而且还会广播一条"离开了服务器"
  现在统一改成「改完表再数一次」，并且没在列表里的断开不再广播。
- 📊 面板升级：顶部大数字 + `授信/未授信/管理员` 三个小计、搜索框、
  按状态筛选、自动刷新 2 秒可暂停、机器码/账号ID 点击复制、在线时长。
- 🩺 新增数据健康度：`/api/players/live` 返回 `updated_at` 与 `stale`，
  面板 30 秒未更新会顶黄条提示（服务器没开 / 插件没加载一眼可见）。
- 🔌 `/api/players/live` 增加别名 `/api/players/online`；返回值新增
  `counts{total,trusted,untrusted,admin}`，移除 `pos` 和 `map` 字段；
  玩家列表按进服时间排序（老玩家在前）。

### v3.4.0 (2026-10-29) — 实时地图网页面板 + 未授信 1 辆车

- 🗺 **新增 `panel.html` 网页面板**：浏览器打开 `http://<IP>:12124/` 就能用
  - 实时地图：玩家箭头（朝向 = 车头方向）、管理员/授信/未授信 三色、
    滚轮缩放 / 拖拽平移 / 自动适配
  - 玩家列表：名字、徽章、官方账号ID、**机器码（点击复制）**、车辆数、在线时长
  - 可选底图：载入地图俯视图 + 填世界坐标范围做校准，存 localStorage
- 📡 **客户端 mod v2.8.0 增加坐标上报**：BeamMP 服务端 Lua 拿不到玩家坐标，
  所以由客户端每秒 `TriggerServerEvent("BMPHWID:Pos", "x,y,z,heading,map")`，
  服务端 `onHwidPos()` 收下后写进 `online_players.json`（超过 10 秒没更新就当作失效）。
- 🔌 新增 `GET /api/players/live`（面板数据源）和 `GET /`（返回 panel.html）；
  `GET /api/ping` 保持原样。
- 🐞 **机器码显示为空**：原来是从 `beam_id` 里截 HWID，但官方账号登录时
  `getPlayerStableInfo` 的 P0 会把 `beam_id` 固定成 `BEAMMP:xxx`，机器码根本不在里面。
  改为直接读 `playerAuthCache.hwid_data`（带优先级 key 和兜底）。
- ⚙️ **未授信刷车数量 0 → 1**：`VEHICLE_LIMITS.unauthenticated = 1`。
  同时修掉一个隐藏问题：`onVehicleSpawn` 里对游客是**无条件 `return 1`** 的，
  导致下面的数量限制对未认证玩家从来没生效过。现在统一走数量判定，
  未授信第一次生成会提示"你是未授信用户，只能生成 1 辆，认证后 5 辆 (/whoami)"，
  第二次才拦。

### v3.3.0 (2026-10-29) — 切换为 "BeamMP 官方账号 + 机器码" 认证

官方账号服务器恢复，内置的聊天框账号体系先关掉。

- ⚙️ `main.lua` 新增开关 `ENABLE_CHAT_LOGIN = false`：`/register` `/login` `/logout`
  会给出一句说明并拒绝执行；`/help` 不再列出这几条命令；欢迎指引改成认证流程。
  改回 `true` 即可完整恢复旧账号体系（`accounts.json` 原样保留，不删数据）。
- ⚙️ 认证判定改为 **官方账号已登录 (`!isGuest`) 且 机器码已上报**：
  - 新增 `hasOfficialAccount()` / `hasStableHwid()` 两个独立判定
  - 用 `REQUIRE_OFFICIAL_ACCOUNT` / `REQUIRE_HWID` 可以单独关掉某一项排查问题
  - 旧模式下的 accounts.json 自动登录、`bound_account` 判定在官方模式下不再参与
- ⚙️ 管理员改成 **官方账号 ID 白名单** `ADMIN_BEAMMP_IDS`：
  - ID 来自 `onPlayerAuth` 的 `identifiers.beammp`，改不了名、冒充不了
  - 玩家在游戏里输 `/whoami` 就能看到自己的 ID，复制进 main.lua 即可
  - 空白名单会在启动日志里打醒目警告（防止把自己锁在门外）
  - `/listadmins` 在官方模式下直接列出白名单 ID；`/addadmin` `/removeadmin` 提示改用配置
- 🐞 `/vehiclelimit` `/onVehicleSpawn` 的提示按模式区分：官方模式下会直接告诉玩家
  **还缺哪一步**（"还需要: 在 BeamMP 登录官方账号 + 启动 BMPHWID_Bridge.exe 上报机器码"），
  而不是让人去 `-login`。
- 🐞 `/whoami` 在官方模式下逐项显示 `官方账号: 已登录 ✓ / 机器码: 已上报 ✓` 和自己的官方账号 ID。
- ⚙️ Bridge v2.6.0：`ENABLE_ACCOUNT_LOGIN = False` 时隐藏「注册账号」「登录」两个子页，
  换成一行说明；后端接口与代码保持完整，改回 `True` 即恢复。

### v3.2.0 (2026-10-29) — 第二轮: 客户端 mod + 界面卡死 + 冒名漏洞

这一轮处理的是 v3.1.0 里明确标注"没做"的遗留项。

**客户端 mod (`BMPHWID.zip` → v2.7.0) — 之前从没审过**
- 🐞 **HWID 从来没能上报成功**: 客户端发的是 `F@hwid|v1|cnt=1|cur=0|<整段 b64>`,
  而服务器 `parseHwidPayload` 把第 2 段之后都当 `key=b64` —— `cur=0` 被当成一个字段,
  整段 b64 因为不含 `=` 被直接丢弃。等于玩家机器码永远为空, 自动认证全靠 GUEST/IP 兜底。
  已改为服务器期望的 `F@<me>|v1|cnt=N|key1=<b64>|...`, 并且服务端**向后兼容**旧 zip。
- 🐞 **主线程卡 3 秒**: `curl` / `Engine.HTTP.request` 的回调由游戏主循环派发,
  旧代码却用 `while not done do end` 忙等 3 秒 —— 回调永远没机会执行,
  结果是每次都白卡 3 秒且 100% 拿不到 HWID。改为异步回调, 结果到了再补发一次。
- 🐞 UUID 兜底熵极低: 16 个字节全部来自 `os.clock()*1e6+os.time()` 取模,
  而 `os.clock()` 在同一循环里几乎不变 → 生成的 "随机" ID 高度重复。
  改用 `math.random` + 显式播种 (注意: BeamNG 是 LuaJIT/5.1 语义, **不能用 5.3 的位运算符**)。
- 🐞 HTTP 探测结果被永久缓存: 插件加载早于 curl/BeamMP 就绪时, "全部不可用" 会被记一辈子,
  之后再也不重试 → 改为只在探测成功时缓存。

**界面假死 (BMP_Admin / BMPHWID_Bridge)**
- 🐞 两个 GUI 的所有 HTTP 请求原来都跑在 Tk 主线程上, 服务器不可达时窗口彻底卡死
  (Admin 每 5 秒一次、Bridge 每 3 秒一次)。现在请求放到后台线程,
  主线程用 `root.update()` 泵事件 —— 窗口可以拖动/重绘/关闭, 超时也不再等于假死。
  加了嵌套深度计数防止事件回调里再发请求导致递归。
- 🐞 Bridge 启动时在 `mainloop()` 之前就同步拉两次数据, 启动白屏数秒 → 改成 `after()` 排队。

**冒名 / 滥用 (BMP_HTTP_API.py)**
- 🔒 `/api/chat/send` 原来完全采信 body 里的 `player_name`, 任何人都能在公屏上
  以任意玩家名义发言 → 已登录一律用账号名, 未登录只能是 `guest`。
- 🔒 `/api/vote/kick` `/api/vote/cast` 无需登录且发起人/投票人名字来自 body,
  Lua 端又是按名字匹配在线玩家 → 匿名就能冒充任意玩家发起投票踢人。
  现在必须登录, 名字一律取账号; 并按 IP 限速 6 次/分钟、待处理队列上限 20 条。
- 🐞 `/api/chat/send` 的 `if not writeJsonFile(...)` 判断永远为假 (函数返回的是元组),
  写盘失败仍返回成功。

**并发写文件**
- 🐞 Lua 插件与 Python API 是两个进程, Windows 上 Lua 的 `os.rename` 覆盖失败会退回
  "截断后重写", 正好被 Python 读到半个 JSON。`readJsonFile` 现在会重试 4 次 (40ms 间隔),
  真正损坏的文件仍然抛 `DataFileError` 让调用方拒绝写入 —— 既不会被瞬时竞态误伤,
  也不会用空库覆盖真实数据。

### v3.1.0 (2026-10-29) — 兼容 BeamMP-Server v3.9.4 + 大批 bug 修复

**新版本适配**
- ✅ 适配 BeamMP-Server **v3.9.4** 新增的 `MP.GetServerTime()` / `MP.GetServerTimeMS()`
  - 投票踢出倒计时、发起冷却改用它 (steady_clock 单调时钟, 系统时间被改动/NTP 跳变都算不错)
  - 旧版服务端 (≤ v3.9.3) 没有这两个函数, 自动回退 `os.time()`, 启动日志会打印当前时间源
  - 封禁到期时间等"绝对时间戳"仍用 `os.time()`, 与 Python 的 epoch 语义保持一致
- ✅ 车辆上限与 HTTP API 对齐: 管理员 999 / 认证 5 / 游客 0

**Lua 插件 (main.lua) 关键修复**
- 🐞 **管理员命令全部失效**: `isPlayerAdmin()` 调用的 `getPlayerStableInfo` 是后面才声明的
  `local` 函数, 编译期变成全局查找 → 运行时 `attempt to call a nil value`。
  受影响: `/listonline /kick /ban /unban /listadmins /addadmin /removeadmin /cancelvote`
  以及 `/votekick`(内部要判断目标是不是管理员)。已加前置声明。
- 🐞 **封禁列表 / 各队列被清空**: `jsonEncode` 只认 `Array()` 打过标记的表, 普通顺序数组
  `{a,b,c}` 会被当成空对象写成 `{}`。后果: `/ban`、`/unban`、踢人队列、投票队列、
  聊天队列每次写入都会把已有数据全部抹掉。已改为自动识别 1..n 的顺序数组。
- 🐞 空密码会让 `(i-1) % pwdLen` 变成 `% 0` 直接报错 → 已加校验
- 🐞 目录创建只用了 Windows 的 `if not exist ... mkdir` → Linux 服务端写文件全部失败, 现按平台区分
- 🐞 写文件改为「先写 `.tmp` 再 rename」, 避免 Python API 与本插件互相读到半个文件
- 🐞 `onPlayerAuth` 的待处理身份原来放在 `playerAuthCache[0]` 单槽, 多人同时连接会互相覆盖,
  现按玩家名存放在 `pendingAuthByName`, 60 秒自动过期
- 🐞 顶层 `MP.RegisterEvent/CreateEventTimer` 没有 pcall, 旧服务端缺 API 时整个插件加载失败
- 🐞 `b64Decode` 依赖 `bit32` (Lua 5.1/5.2 才有), 部分 5.3 构建直接报错 → 改为纯算术实现
- 🐞 去掉把 `password_hash` 打到控制台的调试输出
- 🐞 投票倒计时广播的 `%d` 遇到非数字 target_id 会抛错 → 统一 `tostring` + `math.ceil`

**HTTP API (BMP_HTTP_API.py) 关键修复**
- 🔒 **提权漏洞**: `--admins` 里的名字可以被匿名注册抢注, 注册完直接拥有管理员权限 → 已改为保留名
- 🔒 `--allow-ips` 原来只在 POST 生效, 所有 GET 完全绕过 → 已统一
- 🔒 改密/删号/撤管理员后 **token 不失效** (还能用满 7 天) → 现在立即吊销
- 🔒 `--data-dir` 漏改 `chat_queue.json` / `admin_audit.jsonl` → Bridge 聊天写进旧目录, 游戏里永远不广播
- 🔒 TLS 证书错误时静默降级成明文 HTTP → 改为 fail-closed 拒绝启动
- 🐞 数据文件损坏被当成空库, 下一次注册把 `accounts.json` 覆盖成只剩新账号 → 改为 503 拒绝写入
- 🐞 `HTTPServer` 单线程 + 无超时, 一个慢连接能卡死整个 API → 改 `ThreadingHTTPServer` + 15s 超时
- 🐞 JSON body 不是对象 (`[]`、数字字段) 时抛异常且不回包 → 统一 400/500 JSON
- 🐞 空密码/中文密码: `_lua_hash64` 会 IndexError, 且按码点计算与 Lua 的字节计算不一致 → 中文密码游戏内可登录
- 🐞 登录成功不清零限速 (key 是 `ip:user` 却用 `user in LOGIN_RATE` 判断) → 正常用户也会被锁
- 🐞 stdout 被重定向时 banner 里的 emoji 触发 `UnicodeEncodeError`, 进程起不来 → 强制 UTF-8
- 🐞 超大请求体会连发 413+400 两个响应; 删除管理员时只认 `--admins` 不认持久化 `is_admin`

**管理员后台 (BMP_Admin.py) 关键修复**
- 🐞 **拉黑发的是截断后的 HWID**: 表格里显示 `beam_id[:40]`, 回读后 `split(":")[-1]` 只剩 7 个字符,
  存进 banlist 的是个假设备号 (还可能误伤别人) → 改为按 iid 保存完整记录
- 🐞 「设为管理员」点"否"/关窗 = 降级 → 改为先读当前状态, 取消不发请求
- 🐞 API 地址输入框只有点「测试」才生效, 其它操作还在打旧服务器 → `trace_add` 实时同步
- 🐞 自动刷新循环一旦异常就永久停摆; 刷新会清空选中; 打开弹窗时再刷新会 `TclError` → 已修
- 🐞 错误弹窗显示原始 dict (`{'ok': False, 'error': ...}`) → 统一取 `error`, 401 自动回未登录
- 🐞 封禁天数输错 (`abc`) 会变成 `0` = 永久封禁 → 校验 0-365
- 🐞 token 只存 SHA-256 导致自动登录永不生效 → 改为真实持久化
- 🐞 登出不通知服务端 (token 仍有效) / 双击表头会踢人 / 聊天按钮被挤成 15px

**玩家认证器 (BMPHWID_Bridge.py) 关键修复**
- 🐞 **"永久机器码"会变**: 指纹里含了 *全部* 网卡 MAC, 装 Hyper-V/WSL2/VPN/USB 网卡就换一个
  → 账号绑定失效、HWID 封禁形同虚设。现只用 MachineGuid / 主板 UUID,
  没有稳定源时用"每份安装持久化一次"的兜底 ID; MAC 仅作展示
- 🐞 本地只存 token hash (无法回读) → 每次启动都未登录; 改为可回读的持久化
- 🐞 配置文件非原子写 + `except: pass` → 崩溃/无权限时配置静默丢失
- 🐞 `HTTP://host` 被拼成 `http://HTTP://host`; 地址带路径会变成 `/api/api/...` → 已规范化并拒绝带路径
- 🐞 服务端 `{ok:false,error:...}` 一律显示成「网络错误: None」; 401 不会清登录态
- 🐞 登录失败后状态栏仍显示「已登录」; 登出在服务端 `ok:false` 时本地不清状态
- 🐞 日志面板会打印密码片段和含 `access_token` 的完整响应
- 🐞 「绑定 HWID」页 `_row` 没推进, 游戏名输入框正好盖住本机 HWID 显示
- 🐞 玩家列表里的 `(暂无玩家在线)` 占位行可以被当成玩家发起投票
- 🐞 `api_ping` 任何 200 都亮绿灯 (端口填错也显示连通) → 非 JSON 直接判失败
- 🐞 `--windowed` 打包后 `sys.stdout is None`, `--cli` 静默无输出 → 改为落盘 `bmphwid_last.json`

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
