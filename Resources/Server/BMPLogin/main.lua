-- ============================================================
-- BMP Login Plugin v3.7.1 (官方账号 + 机器码 + 网站验证)
-- BeamMP Server Player Account Management
-- 兼容 BeamMP-Server v3.9.0 ~ v3.9.4+
-- ============================================================

local PLUGIN_NAME = "BMP Login"
local PLUGIN_VERSION = "3.7.1"
local DATA_DIR = "bmp_login"
local ACCOUNTS_FILE = DATA_DIR .. "/accounts.json"
local BANLIST_FILE = DATA_DIR .. "/banlist.json"
local GUEST_MAP_FILE = DATA_DIR .. "/guest_map.json"
local ONLINE_FILE = DATA_DIR .. "/online_players.json"
local KICK_QUEUE_FILE = DATA_DIR .. "/kick_queue.json"
local VOTE_QUEUE_FILE = DATA_DIR .. "/vote_queue.json"
local BINDINGS_FILE = DATA_DIR .. "/bindings.json"    -- 机器码 ↔ 官方账号 的绑定证据

-- ============================================================
-- 认证模式开关  ★ 部署前先看这里 ★
--
--   授信判定只看一件事: **这个机器码在服务器网站上注册过, 并且验证通过**
--   (accounts.json 里 verified=true 且 hwid 对得上)。
--
--     授信用户 (5 辆) = 机器码已在本站注册且 verified
--     其它人     (1 辆) = 没注册的, 不管是登录了官方账号还是游客
--
--   官方账号和机器码不再是玩的时候的前置条件, 而是**注册时的门槛**:
--   网站只允许「服务器见过它和官方账号同时在线」的机器码注册,
--   所以"注册过"本身就隐含了"曾经有官方账号 + 有机器码"。
--   注册之后就算以游客身份进服, 只要机器码对得上, 照样是授信。
--
--   把 ENABLE_CHAT_LOGIN 改回 true 即可恢复旧的 /register /login 账号体系
--   (accounts.json 里的老账号仍然保留, 不会被删)。
-- ============================================================
local ENABLE_CHAT_LOGIN = false   -- 内置聊天框注册/登录 (官方账号可用后关闭)

-- 认证判定: 三个开关可以单独关掉, 方便排查问题
local REQUIRE_WEB_VERIFY = true         -- ★ 必须在本站注册过且机器码验证通过 (主开关)
local REQUIRE_HWID = false              -- 额外要求: Bridge 上报过机器码 (默认已隐含, 关)
local REQUIRE_OFFICIAL_ACCOUNT = false  -- 额外要求: 官方账号已登录 (默认关, 游客也算授信)

-- 网站账号 → 授信白名单的刷新间隔 (秒)
--   accounts.json 可能被 BMP_HTTP_API 在运行时改写 (玩家刚注册), Lua 这边要能跟上
local WEB_VERIFY_REFRESH = 10

-- 管理员白名单: BeamMP **官方账号 ID** (onPlayerAuth 里的 identifiers.beammp)
--   玩家在游戏里输 /whoami 就能看到自己的"官方账号ID", 复制过来填进去。
--   注意这里不是显示名 —— 显示名可以随便改, ID 改不了, 不会被冒充。
--   (管理员走的是官方账号, 不依赖网站注册; 这样万一网站挂了你还进得去后台)
local ADMIN_BEAMMP_IDS = {
    -- ["0123456789abcdef0123456789abcdef"] = true,
}

-- 聊天登录关闭后, 玩家输 /register /login 时给的提示 (所有命令复用同一句)
local CHAT_LOGIN_OFF_MSG =
    "内置账号登录已关闭。授信只要一步: 打开服务器网站注册 (用机器码验证)。"
    .. "注册前请在 BeamMP 登录官方账号并开着 BMPHWID_Bridge 进一次服。用 /whoami 查看状态"

local accounts = {}
local banlist = {}
local guestAccountMap = {}
local onlinePlayers = {}
local playerAuthCache = {}
-- onPlayerAuth 拿到身份时还没有 playerID, 先按玩家名暂存, onPlayerJoining 再取回
-- (以前用 playerAuthCache[0] 单槽, 同时多人连接会互相覆盖, 造成身份/封禁判断错乱)
local pendingAuthByName = {}

-- 投票踢出状态
local activeVoteKick = nil   -- { target_id, initiator_id, reason, start_time, duration, votes_yes, votes_no, voted_players, last_broadcast }
local voteKickCooldown = {}  -- initiator_id -> last vote start time
local VOTE_KICK_DURATION = 30  -- 投票持续时间 (秒)
local VOTE_KICK_MIN_PLAYERS = 2  -- 至少在线人数才能发起
local VOTE_KICK_BROADCAST_INTERVAL = 5  -- 倒计时广播间隔 (秒)

-- ============================================================
-- Logging
-- ============================================================
-- ============================================================
-- Logging (console only — NEVER send internal logs to public chat)
-- ============================================================
local function logMsg(msg)
    if not msg then return end
    print("[BMP Login] " .. tostring(msg))
end

local DEBUG_VERBOSE = false  -- 设为 true 可启用详细调试日志

local function logDebug(msg)
    if not msg or not DEBUG_VERBOSE then return end
    print("[BMP Login] [DEBUG] " .. tostring(msg))
end

local function logWarn(msg)
    if not msg then return end
    print("[BMP Login] [WARN] " .. tostring(msg))
end

local function logError(msg)
    if not msg then return end
    print("[BMP Login] [ERROR] " .. tostring(msg))
end

-- ============================================================
-- Utility: string.trim (must be defined BEFORE first use)
-- ============================================================
function string.trim(s)
    if type(s) ~= "string" then return "" end
    return s:match("^%s*(.-)%s*$") or ""
end

-- ============================================================
-- Array Helper (force empty tables to be JSON arrays)
-- ============================================================
function Array(t)
    local mt = { __tag_json_array = true }
    setmetatable(t, mt)
    return t
end

local function isArray(t)
    if type(t) ~= "table" then return false end
    local mt = getmetatable(t)
    if mt and mt.__tag_json_array then return true end
    return false
end

-- 判断 t 是否为"顺序数组" (键恰好是 1..n 的整数)
-- 必须显式判断: 否则普通 Lua 表 {a,b,c} 在 jsonEncode 里会被当成空对象写成 {},
-- 导致 banlist.json / kick_queue.json / vote_queue.json 等数据被整体清空
local function isSequentialArray(t)
    if type(t) ~= "table" then return false, 0 end
    local n = 0
    for k in pairs(t) do
        if type(k) ~= "number" or k < 1 or k % 1 ~= 0 then return false, 0 end
        n = n + 1
    end
    for i = 1, n do
        if t[i] == nil then return false, 0 end
    end
    return true, n
end

-- ============================================================
-- JSON Encode
-- ============================================================
local function jsonEncode(val)
    local v = val
    local t = type(v)
    
    if v == nil then return "null" end
    if t == "boolean" then return v and "true" or "false" end
    if t == "number" then return tostring(v) end
    if t == "string" then
        v = v:gsub("\\", "\\\\")
        v = v:gsub("\"", "\\\"")
        v = v:gsub("\n", "\\n")
        v = v:gsub("\r", "\\r")
        v = v:gsub("\t", "\\t")
        return "\"" .. v .. "\""
    end
    
    if t == "table" then
        local mt = getmetatable(v)
        if isArray(v) then
            local parts = {}
            for i = 1, #v do
                parts[i] = jsonEncode(v[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        
        -- 未打 Array() 标记的顺序数组也必须编码成 JSON 数组
        local seq, n = isSequentialArray(v)
        if seq then
            local parts = {}
            for i = 1, n do parts[i] = jsonEncode(v[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end

        local parts = {}
        for k, val in pairs(v) do
            if type(k) == "string" or type(k) == "number" then
                parts[#parts + 1] = jsonEncode(tostring(k)) .. ":" .. jsonEncode(val)
            end
        end
        table.sort(parts)
        return "{" .. table.concat(parts, ",") .. "}"
    end
    
    return "null"
end

-- ============================================================
-- JSON Decode
-- ============================================================
local function jsonDecode(str)
    local pos = 1
    local function skipWs()
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == " " or c == "\t" or c == "\n" or c == "\r" then
                pos = pos + 1
            else
                break
            end
        end
    end
    
    local function parseString()
        pos = pos + 1
        local result = ""
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == '"' then pos = pos + 1; return result end
            if c == "\\" then
                pos = pos + 1
                local nc = str:sub(pos, pos)
                if nc == "n" then result = result .. "\n"
                elseif nc == "r" then result = result .. "\r"
                elseif nc == "t" then result = result .. "\t"
                elseif nc == '"' then result = result .. '"'
                elseif nc == "\\" then result = result .. "\\"
                else result = result .. nc end
            else
                result = result .. c
            end
            pos = pos + 1
        end
        return result
    end
    
    local function parseNumber()
        local start = pos
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == "," or c == "}" or c == "]" or c == " " or c == "\n" or c == "\r" or c == "\t" then
                break
            end
            pos = pos + 1
        end
        return tonumber(str:sub(start, pos - 1))
    end
    
    local function parseBool()
        if str:sub(pos, pos + 3) == "true" then
            pos = pos + 4; return true
        elseif str:sub(pos, pos + 4) == "false" then
            pos = pos + 5; return false
        end
        return nil
    end
    
    local function parseNull()
        if str:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil end
        return nil
    end
    
    -- Forward declarations for mutual recursion
    local parseObject
    local parseArray
    
    local function parseValue()
        skipWs()
        if pos > #str then return nil end
        local c = str:sub(pos, pos)
        if c == "{" then return parseObject()
        elseif c == "[" then return parseArray()
        elseif c == '"' then return parseString()
        elseif c == "t" or c == "f" then return parseBool()
        elseif c == "n" then return parseNull()
        else return parseNumber() end
    end
    
    parseArray = function()
        pos = pos + 1
        local arr = {}
        skipWs()
        if str:sub(pos, pos) == "]" then pos = pos + 1; return Array(arr) end
        while pos <= #str do
            skipWs()
            if str:sub(pos, pos) == "]" then pos = pos + 1; break end
            arr[#arr + 1] = parseValue()
            skipWs()
            if str:sub(pos, pos) == "," then pos = pos + 1 end
        end
        return arr
    end
    
    parseObject = function()
        pos = pos + 1
        local obj = {}
        skipWs()
        if str:sub(pos, pos) == "}" then pos = pos + 1; return obj end
        while pos <= #str do
            skipWs()
            if str:sub(pos, pos) == "}" then pos = pos + 1; break end
            local key = parseString()
            skipWs()
            if str:sub(pos, pos) == ":" then pos = pos + 1 end
            skipWs()
            obj[key] = parseValue()
            skipWs()
            if str:sub(pos, pos) == "," then pos = pos + 1 end
        end
        return obj
    end
    
    local result = parseValue()
    return result
end

-- ============================================================
-- Password Hashing
-- ============================================================
local function hashPassword(password)
    -- 空密码会让下面的 (i-1) % pwdLen 变成 % 0 (Lua 5.3 直接报错), 必须挡掉
    if type(password) ~= "string" or password == "" then return nil end

    local salt = ""
    for i = 1, 16 do
        salt = salt .. string.format("%x", math.random(0, 15))
    end
    local saltBytes = {}
    for i = 1, 16 do
        saltBytes[i] = tonumber(salt:sub(i, i), 16)
    end
    
    local hash = ""
    local pwdLen = #password
    if pwdLen == 0 then return nil end
    for i = 1, 64 do
        local idx = ((i - 1) % pwdLen) + 1
        local c = password:byte(idx)
        local s = saltBytes[((i - 1) % 8) + 1]
        hash = hash .. string.char((c + s + i) % 95 + 32)
    end
    
    return salt .. ":" .. hash
end

local function verifyPassword(password, storedHash)
    if type(password) ~= "string" or password == "" then
        return false
    end
    if not storedHash or storedHash == "" then
        logWarn("verifyPassword: storedHash 为空, 拒绝登录")
        return false
    end
    
    local salt = storedHash:match("^([^:]+):")
    local hash = storedHash:match(":(.+)$")
    if not salt or not hash then
        logWarn("verifyPassword: 无法从存储值解析出 salt:hash")
        return false
    end
    
    if #hash > 64 then hash = hash:sub(-64) end
    
    local saltBytes = {}
    for i = 1, 16 do
        saltBytes[i] = tonumber(salt:sub(i, i), 16) or 0
    end
    
    local pwdLen = #password
    local computed = ""
    for i = 1, 64 do
        local idx = ((i - 1) % pwdLen) + 1
        local c = password:byte(idx)
        local s = saltBytes[((i - 1) % 8) + 1]
        computed = computed .. string.char((c + s + i) % 95 + 32)
    end
    
    return computed == hash
end

-- ============================================================
-- File I/O
-- ============================================================
local function readJsonFile(filePath)
    local file = io.open(filePath, "r")
    if not file then return nil end
    local content = file:read("*all")
    file:close()
    if not content or content == "" then return nil end
    return jsonDecode(content)
end

local function ensureDir(dir)
    if not dir or dir == "" then return true end
    local sep = (package.config or "/"):sub(1, 1)
    if sep == "\\" then
        -- Windows: cmd 内建 mkdir, 不能直接透传到控制台
        os.execute('if not exist "' .. dir .. '" mkdir "' .. dir .. '" >NUL 2>NUL')
    else
        -- Linux / macOS (BeamMP 服务端最常见的部署环境)
        os.execute('mkdir -p "' .. dir .. '" 2>/dev/null')
    end
    return true
end

local function writeJsonFile(filePath, data)
    local dir = filePath:match("^(.+)[/\\][^/\\]+$")
    if dir and dir ~= "" then
        ensureDir(dir)
    end

    local encoded = jsonEncode(data)

    -- 先写 .tmp 再改名, 避免 Python API 与本插件同时读写时读到半个文件
    local tmp = filePath .. ".tmp"
    local file = io.open(tmp, "w")
    if file then
        file:write(encoded)
        file:flush()
        file:close()
        if os.rename(tmp, filePath) then
            return true
        end
        os.remove(tmp)
    end

    -- 改名失败 (Windows 上目标已存在或仍被占用) 时退回直接覆盖写
    local f2 = io.open(filePath, "w")
    if not f2 then
        logWarn("writeJsonFile 失败: " .. tostring(filePath))
        return false
    end
    f2:write(encoded)
    f2:flush()
    f2:close()
    return true
end

-- ============================================================
-- 服务器单调时间 (BeamMP v3.9.4+ 新 API 适配)
--   v3.9.4 新增 MP.GetServerTime()   -> 秒 (double, 单调递增)
--             MP.GetServerTimeMS() -> 毫秒 (uint64)
--   这两个是 steady_clock 基准的单调时钟, 不受系统时间被手动修改 /
--   NTP 校时跳变影响, 适合投票倒计时、冷却、轮询节流这类"时长"计算。
--   ⚠️ 它们不是 epoch 时间戳: 写进 JSON 的时间戳、封禁到期时间仍必须用 os.time()
--   老版本服务端 (<= v3.9.3) 没有这两个函数, 自动回退到 os.time()
-- ============================================================
local HAS_SERVER_TIME = false
do
    local ok = pcall(function()
        HAS_SERVER_TIME = (MP ~= nil) and (type(MP.GetServerTime) == "function")
    end)
    if not ok then HAS_SERVER_TIME = false end
end

local function monotonicNow()
    if HAS_SERVER_TIME then
        local ok, v = pcall(function() return MP.GetServerTime() end)
        if ok and type(v) == "number" then return v end
    end
    return os.time()
end

-- ============================================================
-- Bridge 聊天 → 公屏广播 (1Hz 轮询)
--   流程: Bridge GUI Tab3 聊天框 → POST /api/chat/send
--         → BMP_HTTP_API.py 写 bmp_login/chat_queue.json
--         → MP.CreateEventTimer(BMP_CHAT_QUEUE_POLL, 1000) 1Hz 轮询
--         → MP.SendChatMessage(-1, "[Bridge:账号] 显示名: 消息")
--   ⚠️ 此模块必须放在 jsonEncode/jsonDecode/writeJsonFile 之后定义
--   ⚠️ 并且 processChatQueueGlobal 必须是全局函数 (MP.RegisterEvent 要求)
-- ============================================================
local CHAT_QUEUE_FILE = DATA_DIR .. "/chat_queue.json"
local lastChatQueuePoll = 0
local CHAT_QUEUE_INTERVAL = 1   -- 秒 (文件轮询节流; CreateEventTimer 本身就是 1Hz)

local function sanitizeChatText(s)
    s = tostring(s or "")
    s = s:gsub("[%z\1-\31]", "")
    if #s > 200 then s = s:sub(1, 200) .. "..." end
    return s
end

local function processChatQueueInternal()
    local now = monotonicNow()
    if now - lastChatQueuePoll < CHAT_QUEUE_INTERVAL then return end
    lastChatQueuePoll = now

    local f = io.open(CHAT_QUEUE_FILE, "r")
    if not f then return end
    local content = f:read("*a")
    f:close()
    if not content or content == "" then return end

    local ok_parse, queue = pcall(jsonDecode, content)
    if not ok_parse or type(queue) ~= "table" then return end

    local pending = {}
    local remaining = {}
    for _, item in ipairs(queue) do
        if type(item) == "table" then
            if item.sent then
                if #remaining < 50 then
                    table.insert(remaining, item)
                end
            else
                table.insert(pending, item)
            end
        end
    end

    if #pending == 0 then return end

    -- 广播 pending 消息给所有在线玩家
    for _, item in ipairs(pending) do
        local name = sanitizeChatText(item.name or "guest")
        if #name > 16 then name = name:sub(1, 16) end
        local text = sanitizeChatText(item.text or "")
        local prefix = "[Bridge]"
        if item.username and item.username ~= "" then
            prefix = "[Bridge:" .. tostring(item.username) .. "]"
        end
        local msg = " " .. prefix .. " " .. name .. ": " .. text
        pcall(function() MP.SendChatMessage(-1, msg) end)
        item.sent = true
        item.sent_ts = os.time()
        print("[BMP Login] [ChatQueue] broadcast: " .. msg)
    end

    -- 写回 (合并 + 限制 100 条)
    local new_queue = {}
    for _, item in ipairs(remaining) do
        if #new_queue < 100 then table.insert(new_queue, item) end
    end
    for _, item in ipairs(pending) do
        if #new_queue < 100 then table.insert(new_queue, item) end
    end

    local f2 = io.open(CHAT_QUEUE_FILE, "w")
    if f2 then
        f2:write(jsonEncode(new_queue))
        f2:close()
    end
end

-- ⚠️ BeamMP MP.RegisterEvent 要求: 函数名必须 **全局 (_G 可找到)**
-- 同时 CreateEventTimer 的回调签名是 (event_name:string)
function processChatQueueGlobal(eventName)
    pcall(processChatQueueInternal)
    return 0
end

-- 兜底: 玩家发消息 / 进服 时也顺便 poll 一次 (哪怕 timer 坏了也能出)
function _tryPollChatQueue()
    pcall(processChatQueueInternal)
end

-- ============================================================
-- Data Loading / Saving
-- ============================================================
local function loadAccounts()
    local data = readJsonFile(ACCOUNTS_FILE)
    if data then
        accounts = data
    else
        accounts = {}
    end
    -- 修复 bind_beam_ids / login_records 结构：确保它们始终是数组（带 Array metatable）
    -- （之前保存成对象 {} 会导致自动登录匹配、登录次数统计失效）
    local repaired = 0
    for uname, acc in pairs(accounts) do
        if type(acc) == "table" then
            -- 判断 Array() 标记: metatable.__tag_json_array == true
            local bi = acc.bind_beam_ids
            local bi_ok = false
            if type(bi) == "table" then
                local mt = getmetatable(bi)
                bi_ok = (mt and mt.__tag_json_array == true)
            end
            if not bi_ok then
                local new = Array({})
                if type(bi) == "table" then
                    -- 对象形态 {}: 用 pairs 拿所有字符串/数字 key 对应的值都塞数组
                    for _, v in ipairs(bi) do new[#new+1] = v end
                    for k, v in pairs(bi) do
                        if type(k) ~= "number" or (k < 1 or k > #bi or math.floor(k) ~= k) then
                            new[#new+1] = v
                        end
                    end
                end
                acc.bind_beam_ids = new
                repaired = repaired + 1
            end
            local lr = acc.login_records
            local lr_ok = false
            if type(lr) == "table" then
                local mt = getmetatable(lr)
                lr_ok = (mt and mt.__tag_json_array == true)
            end
            if not lr_ok then
                local new = Array({})
                if type(lr) == "table" then
                    for _, v in ipairs(lr) do new[#new+1] = v end
                    for k, v in pairs(lr) do
                        if type(k) ~= "number" or (k < 1 or k > #lr or math.floor(k) ~= k) then
                            new[#new+1] = v
                        end
                    end
                end
                acc.login_records = new
                repaired = repaired + 1
            end
        end
    end
    if repaired > 0 then
        logMsg("已修复 " .. repaired .. " 个账号字段结构 (对象 -> 数组)，立即写回文件")
        -- 直接调 writeJsonFile (此时 saveAccounts 可能还没定义, 避免 nil 错误)
        local ok, err = pcall(writeJsonFile, ACCOUNTS_FILE, accounts)
        logMsg("writeJsonFile 结果 ok=" .. tostring(ok) .. " err=" .. tostring(err or "nil"))
    end
    local count = 0
    for _ in pairs(accounts) do count = count + 1 end
    print("[BMP Login] 已注册账号: " .. count)
end

local function saveAccounts()
    writeJsonFile(ACCOUNTS_FILE, accounts)
end


local function loadBanlist()
    local data = readJsonFile(BANLIST_FILE)
    if data then
        banlist = data
        if not banlist.accounts then banlist.accounts = Array({}) end
        if not banlist.devices then banlist.devices = Array({}) end
    else
        banlist = { accounts = Array({}), devices = Array({}) }
    end
    print("[BMP Login] 封禁账号: " .. #banlist.accounts)
    print("[BMP Login] 封禁设备: " .. #banlist.devices)
end

local function saveBanlist()
    writeJsonFile(BANLIST_FILE, banlist)
end

local function loadGuestMap()
    local data = readJsonFile(GUEST_MAP_FILE)
    if data then guestAccountMap = data else guestAccountMap = {} end
    local count = 0
    for _ in pairs(guestAccountMap) do count = count + 1 end
    print("[BMP Login] Guest映射记录: " .. count)
end

local function saveGuestMap()
    writeJsonFile(GUEST_MAP_FILE, guestAccountMap)
end

-- ============================================================
-- 机器码 ↔ BeamMP 官方账号 绑定证据 (bindings.json)
--
--   为什么需要这张表:
--     网页注册时要用机器码证明"确实是这个游戏账号的本人"。
--     但机器码本身不是秘密 —— 它在网页面板/在线列表里是公开的,
--     谁抄走了都能拿去注册。真正靠得住的证据只有一种:
--     **服务器亲眼看到这个机器码和这个官方账号同时在线过**。
--
--   结构: { "<hwid>": { beammp_id, player_name, first_seen, last_seen, seen } }
--   写入频率做了节流, 不会每次上报都刷盘。
-- ============================================================
local bindings = {}
local bindingsDirty = false
local lastBindingsSave = 0
local BINDINGS_SAVE_INTERVAL = 15   -- 秒

local function loadBindings()
    local data = readJsonFile(BINDINGS_FILE)
    if type(data) == "table" then
        bindings = data
    else
        bindings = {}
    end
    local n = 0
    for _ in pairs(bindings) do n = n + 1 end
    print("[BMP Login] 机器码↔账号 绑定记录: " .. n)
end

local function flushBindings(force)
    if not bindingsDirty then return end
    local now = os.time()
    if not force and (now - lastBindingsSave) < BINDINGS_SAVE_INTERVAL then return end
    lastBindingsSave = now
    bindingsDirty = false
    writeJsonFile(BINDINGS_FILE, bindings)
end

-- 记录一次「我亲眼看到 hwid 与 beammpId 同时在线」
function recordBinding(playerID, hwid, playerName)
    if not hwid or hwid == "" then return end
    -- 注意: 不能调用后面才定义的 getPlayerBeammpId (会变成全局查找 -> nil),
    -- 这里直接读缓存, 等价且没有顺序问题
    local ac = playerAuthCache and playerAuthCache[playerID]
    -- 只记「登录了官方账号」的人: 游客也有 identifiers.beammp (是 guest id),
    -- 记下来就等于给游客发了一张"已验证"的门票。
    if not (ac and ac.official_account == true) then return end
    local beammpId = ac.beammp_id
    if not beammpId or beammpId == "" then
        local bid = ac.auth_beam_id
        if type(bid) == "string" then beammpId = bid:match("^BEAMMP:(.+)$") end
    end
    if not beammpId or beammpId == "" then return end

    hwid = tostring(hwid):lower()
    local now = os.time()
    local e = bindings[hwid]
    if type(e) ~= "table" then
        e = { beammp_id = beammpId, player_name = playerName or "", first_seen = now,
              last_seen = now, seen = 0 }
        bindings[hwid] = e
    else
        -- 换号: 以最近的账号为准, 但保留变更次数, 便于人工识别一人多号
        if e.beammp_id ~= beammpId then
            e.prev_beammp_id = e.beammp_id
            e.beammp_id = beammpId
        end
        e.player_name = playerName or e.player_name or ""
        e.last_seen = now
    end
    e.seen = (e.seen or 0) + 1
    bindingsDirty = true
    logDebug("binding: hwid=" .. hwid .. " <-> beammp=" .. beammpId .. " seen=" .. e.seen)
    flushBindings(false)
end

-- ============================================================
-- Player Role
-- ============================================================
-- 前置声明: getPlayerStableInfo 的真实实现放在文件后面 (需要 MP.GetPlayerIdentifiers)
-- 如果不在这里声明成 local, 上面 isPlayerAdmin 里的调用会被编译成全局查找
-- (_ENV.getPlayerStableInfo == nil), 运行时直接 "attempt to call a nil value",
-- 导致所有管理员命令和投票踢出全部失效。
local getPlayerStableInfo
-- 同理: 认证判定也要前置声明, getPlayerRole 在它前面
local isPlayerAuthenticated

-- 取玩家的 BeamMP 官方账号 ID (onPlayerAuth 的 identifiers.beammp)
local function getPlayerBeammpId(playerID)
    if not playerID then return nil end
    local ac = playerAuthCache and playerAuthCache[playerID]
    if ac and ac.beammp_id and ac.beammp_id ~= "" then return ac.beammp_id end
    -- 回退: beamId 形如 "BEAMMP:xxxx"
    local ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    if ok and type(beam_id) == "string" then
        local id = beam_id:match("^BEAMMP:(.+)$")
        if id and id ~= "" then return id end
    end
    return nil
end

local function getPlayerRole(beamId, playerID)
    if not beamId then return "游客" end
    -- 官方账号 + 机器码模式: 角色完全由认证判定决定
    if not ENABLE_CHAT_LOGIN then
        if playerID and isPlayerAuthenticated(playerID) then return "玩家" end
        return "游客"
    end
    -- 方法 1: accounts 直接以 beamId 为 key (老格式)
    if accounts[beamId] then return "玩家" end
    -- 方法 2: 通过 playerAuthCache.bound_account 检查 (登录后绑定)
    if playerID and playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account then
        local uname = playerAuthCache[playerID].bound_account
        if uname and accounts[uname] then return "玩家" end
    end
    return "游客"
end

-- ============================================================
-- Admin Check (管理员权限判定)
--   官方账号模式: 只看 ADMIN_BEAMMP_IDS (BeamMP 官方账号 ID 白名单)
--   旧账号模式  : accounts.json 的 is_admin + ADMIN_WHITELIST
-- ============================================================
local ADMIN_WHITELIST = {
    -- 仅 ENABLE_CHAT_LOGIN=true 时生效: 在此添加管理员账号 (小写), 例如: "driftking" = true
}

local function isPlayerAdmin(playerID)
    if not playerID then return false end

    -- ---- 官方账号模式 ----
    if not ENABLE_CHAT_LOGIN then
        local bid = getPlayerBeammpId(playerID)
        if bid and ADMIN_BEAMMP_IDS[bid] then return true end
        return false
    end

    local beamId = getPlayerStableInfo(playerID)
    if not beamId then return false end
    local role = getPlayerRole(beamId, playerID)
    if role == "游客" then return false end
    
    -- 方法 1: 通过 playerAuthCache.bound_account 检查 is_admin
    local uname = nil
    if playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account then
        uname = playerAuthCache[playerID].bound_account
    end
    
    if uname and accounts[uname] and type(accounts[uname]) == "table" then
        if accounts[uname].is_admin then return true end
    end
    
    -- 方法 2: 检查账号名在白名单
    if uname and ADMIN_WHITELIST[string.lower(uname)] then
        return true
    end
    
    -- 方法 3: 遍历所有账号找 is_admin 且匹配 beamId
    for aname, acc in pairs(accounts) do
        if type(acc) == "table" and acc.is_admin then
            -- 检查是否绑定了当前玩家的 beamId
            local bids = acc.bind_beam_ids or {}
            if type(bids) == "table" then
                for _, bid in ipairs(bids) do
                    if bid == beamId then return true end
                end
            end
        end
    end
    
    return false
end


-- ============================================================
-- 认证用户判定 (基于稳定 HWID)
--   认证用户: 有稳定 HWID 绑定 (Bridge.exe 提供 + 通过 BMPHWID.zip 回传 + 已写入 playerAuthCache)
--   未认证用户: 无稳定 HWID (用 GUEST:guestXXX 临时 ID, 不可靠)
--
-- 车辆数量限制:
--   认证用户: 5 辆 (机器码已绑定, 可信任)
--   未认证用户: 1 辆 (无稳定身份, 防滥用)
-- ============================================================
local VEHICLE_LIMITS = {
    admin = 999,          -- 管理员 (与 README / HTTP API 的 999 保持一致)
    authenticated = 5,    -- 授信用户 (官方账号 + 机器码)
    unauthenticated = 1,  -- 未授信用户: 给 1 辆, 至少能开一辆, 不至于完全玩不了
}

-- 地图位置上报: 客户端 BMPHWID mod 每秒 TriggerServerEvent("BMPHWID:Pos")
-- 服务端 Lua **拿不到**玩家坐标, 只能由客户端推上来
local playerPositions = {}   -- playerID -> { x, y, z, h, map, ts }
local POS_STALE_SECONDS = 10 -- 超过这么久没更新就当位置失效

-- 取玩家的机器码原文 (给网页面板显示/复制用)
--   注意: 不能从 beam_id 里截 —— 官方账号登录时 P0 会把 beam_id 固定成
--   "BEAMMP:xxx", 机器码根本不在里面。必须直接读 hwid_data。
local HWID_PRIORITY_KEYS = {
    "settings.BMPLoginHWID(uuid)",
    "uuid",
    "beammp_launcher_hwid",
    "bmpid_raw",
    "FS:getUserPath()",
    "core_env_fingerprint",
}

local function getPlayerHwid(playerID)
    if not playerID then return "" end
    local ac = playerAuthCache and playerAuthCache[playerID]
    if ac and type(ac.hwid_data) == "table" then
        for _, k in ipairs(HWID_PRIORITY_KEYS) do
            local v = ac.hwid_data[k]
            if type(v) == "string" and v ~= "" then return v end
        end
        for k, v in pairs(ac.hwid_data) do
            if type(k) == "string" and k:sub(1, 1) ~= "_"
                    and type(v) == "string" and v ~= "" then
                return v
            end
        end
    end
    -- 兜底: beam_id 本身就是 HWID:xxx 的情况
    local mk, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    if mk and type(beam_id) == "string" then
        return beam_id:match("^HWID:[^:]+:(.+)$") or beam_id:match("^HWID:(.+)$") or ""
    end
    return ""
end

-- Bridge 是否上报过机器码
local function hasStableHwid(playerID)
    if not playerID then return false end
    local ac = playerAuthCache and playerAuthCache[playerID]
    if ac and type(ac.hwid_data) == "table" then
        for k, v in pairs(ac.hwid_data) do
            -- "_version"/"_count" 只是元信息, 不算机器码
            if type(k) == "string" and k:sub(1, 1) ~= "_"
                    and v and tostring(v):match("%S") then
                return true
            end
        end
    end
    -- beam_id 以 "HWID:" 开头 = 客户端上报的稳定机器码
    local ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    if ok and beam_id and tostring(beam_id):match("^HWID:") then
        return true
    end
    return false
end

-- ============================================================
-- 网站账号白名单 (授信的必要条件)
--   只有「在本站注册过 且 机器码验证通过 (verified=true)」的机器码才算授信。
--   accounts.json 由 BMP_HTTP_API 在运行时改写 (玩家刚注册就生效),
--   所以这里定时重建一张 hwid -> username 的表, 而不是每个玩家都去翻文件。
-- ============================================================
local verifiedHwids = {}          -- "<hwid>" -> username
local verifiedCount = 0
local lastVerifiedRefresh = 0

local function rebuildVerifiedHwids()
    local data = readJsonFile(ACCOUNTS_FILE)
    local map = {}
    local n = 0
    if type(data) == "table" then
        for uname, acc in pairs(data) do
            if type(acc) == "table" and acc.verified == true then
                local h = acc.hwid
                if type(h) == "string" and h ~= "" then
                    map[h:lower()] = tostring(uname)
                    n = n + 1
                end
            end
        end
    end
    verifiedHwids = map
    verifiedCount = n
    lastVerifiedRefresh = os.time()
    logDebug("verified hwid 白名单已刷新: " .. n .. " 条")
end

local function ensureVerifiedHwids()
    local now = os.time()
    if (now - lastVerifiedRefresh) >= WEB_VERIFY_REFRESH then
        local ok, err = pcall(rebuildVerifiedHwids)
        if not ok then
            logWarn("刷新 verified 白名单失败: " .. tostring(err))
            lastVerifiedRefresh = now      -- 别在坏文件上死循环
        end
    end
end

-- 这个玩家是否对应一个已验证的网站账号; 返回 (true, username) / (false, nil)
local function getWebAccount(playerID)
    if not playerID then return false, nil end
    local hwid = getPlayerHwid(playerID)
    if type(hwid) ~= "string" or hwid == "" then return false, nil end
    ensureVerifiedHwids()
    local uname = verifiedHwids[hwid:lower()]
    if uname then return true, uname end
    return false, nil
end

-- 是否登录了 BeamMP 官方账号 (onPlayerAuth 的 isGuest == false)
local function hasOfficialAccount(playerID)
    if not playerID then return false end
    local ac = playerAuthCache and playerAuthCache[playerID]
    if ac and ac.official_account ~= nil then
        return ac.official_account == true
    end
    -- 没有 onPlayerAuth 记录时, 退一步看 beamId 是不是 BEAMMP:xxx
    local ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    return ok and type(beam_id) == "string" and beam_id:match("^BEAMMP:") ~= nil
end

isPlayerAuthenticated = function(playerID)
    if not playerID then return false end

    -- ---- 官方账号 + 机器码 + 网站验证 ----
    if not ENABLE_CHAT_LOGIN then
        if REQUIRE_OFFICIAL_ACCOUNT and not hasOfficialAccount(playerID) then
            return false
        end
        if REQUIRE_HWID and not hasStableHwid(playerID) then
            return false
        end
        if REQUIRE_WEB_VERIFY then
            local ok = getWebAccount(playerID)
            if not ok then return false end
        end
        return true
    end

    -- ---- 旧模式 (插件自带账号) ----
    -- 方法 1: playerAuthCache 有 hwid_data
    if playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].hwid_data then
        local hwid_data = playerAuthCache[playerID].hwid_data
        if type(hwid_data) == "table" then
            for _, v in pairs(hwid_data) do
                if v and tostring(v):match("%S") then return true end
            end
        end
    end
    -- 方法 2: beam_id 以 "HWID:" 开头 (来自 getPlayerStableInfo P0 命中)
    local beam_id_ok, beam_id = pcall(function()
        return getPlayerStableInfo(playerID)
    end)
    if beam_id_ok and beam_id and tostring(beam_id):match("^HWID:") then
        return true
    end
    -- 方法 3: 已登录账号 (登录后会绑定 beam_id, 包括 IP 兜底)
    if playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account then
        return true
    end
    return false
end

local function getPlayerVehicleLimit(playerID)
    if not playerID then return VEHICLE_LIMITS.unauthenticated end
    if isPlayerAdmin(playerID) then
        return VEHICLE_LIMITS.admin
    end
    if isPlayerAuthenticated(playerID) then
        return VEHICLE_LIMITS.authenticated
    end
    return VEHICLE_LIMITS.unauthenticated
end

local function getPlayerAuthLabel(playerID)
    if not playerID then return "未认证" end
    if isPlayerAuthenticated(playerID) then return "认证" end
    return "未认证"
end

-- per-player 当前车辆数 (playerID -> count)
local playerVehicleCount = {}


-- ============================================================
-- Player Stable Info (HWID)
--   Priority order:
--   P0 = identifiers.beammp from onPlayerAuth (official BeamMP unique ID → best!)
--   P1 = client BMPHWID plugin payload (settings.BMPLoginHWID / uuid / etc.)
--   P2 = MP.GetPlayerIdentifiers (beammp / discord / hwid / ip KV)
--   P3 = MP.GetPlayerHWID
--   P4 = fallback: GUEST:xxx / NAME:xxx
-- ============================================================
getPlayerStableInfo = function(playerID)
    local beamId = nil
    local ip = nil
    local name = nil
    
    -- [Priority 0: Official BeamMP from onPlayerAuth (auth cache)]
    --    onPlayerAuth runs BEFORE onPlayerJoining and delivers the most reliable ID
    if playerID and playerAuthCache[playerID] and playerAuthCache[playerID].auth_beam_id then
        beamId = playerAuthCache[playerID].auth_beam_id
        if playerAuthCache[playerID].auth_ip then ip = playerAuthCache[playerID].auth_ip end
        local ok, pn = pcall(function() return MP.GetPlayerName(playerID) end)
        if ok and pn then name = pn end
        logDebug("getPlayerStableInfo(#" .. playerID .. ") P0 (onPlayerAuth) beam_id=" .. tostring(beamId) .. ", name=" .. tostring(name))
        return beamId, ip, name
    end
    
    -- [Priority 1: Client BMPHWID plugin payload]
    if playerID and playerAuthCache[playerID] and playerAuthCache[playerID].hwid_data then
        local d = playerAuthCache[playerID].hwid_data
        local priority_keys = {
            "settings.BMPLoginHWID(uuid)",
            "uuid",
            "beammp_launcher_hwid",
            "FS:getUserPath()",
            "core_env_fingerprint"
        }
        for _, k in ipairs(priority_keys) do
            if d[k] and d[k] ~= "" then
                beamId = "HWID:" .. k .. ":" .. d[k]
                break
            end
        end
        if beamId then
            local ok, pn = pcall(function() return MP.GetPlayerName(playerID) end)
            if ok and pn then name = pn end
            logDebug("getPlayerStableInfo(#" .. playerID .. ") P1 (client-hwid) beam_id=" .. tostring(beamId) .. ", name=" .. tostring(name))
            return beamId, ip, name
        end
    end
    
    -- Get player name
    local ok, pname = pcall(function() return MP.GetPlayerName(playerID) end)
    if ok and pname then name = pname end
    
    -- Get identifiers (BeamMP v3+ official API: key-value table)
    --   Docs: identifiers = { ip="x.x.x.x", beammp="unique_id", discord="xxx" }
    --   MP.GetPlayerIdentifiers(player_id) returns the same KV table
    --   Also MP.GetPlayerHWID(player_id) available in some builds
    local ids = nil
    local ok2, result = pcall(function() return MP.GetPlayerIdentifiers(playerID) end)
    if ok2 and result then ids = result end
    
    local function parseIds(tbl)
        if not tbl or type(tbl) ~= "table" then return end
        -- [Method A - Official KV] Direct key access per BeamMP docs (Aug 2026)
        if tbl.beammp and type(tbl.beammp) == "string" and tbl.beammp ~= "" then
            beamId = beamId or "BEAMMP:" .. tbl.beammp
        end
        if tbl.ip and type(tbl.ip) == "string" and tbl.ip ~= "" then
            ip = tbl.ip
        end
        if tbl.discord and type(tbl.discord) == "string" and tbl.discord ~= "" then
            beamId = beamId or "DISCORD:" .. tbl.discord
        end
        if tbl.hwid and type(tbl.hwid) == "string" and tbl.hwid ~= "" then
            beamId = beamId or "HWID:srv:" .. tbl.hwid
        end
        -- [Method B - Array fallback] For older versions: prefix string array
        for k, v in pairs(tbl) do
            if type(k) == "number" and type(v) == "string" then
                local prefix, val = v:match("^([^:]+):(.+)$")
                if prefix == "beammp" then
                    beamId = beamId or "BEAMMP:" .. val
                elseif prefix == "hwid" then
                    beamId = beamId or "HWID:srv:" .. val
                elseif prefix == "discord" then
                    beamId = beamId or "DISCORD:" .. val
                elseif prefix == "ip" then
                    ip = ip or val
                end
            end
        end
    end
    
    parseIds(ids)
    
    -- Try MP.GetPlayerHWID if exists (只接受 playerID; 个别构建里没有这个函数)
    if not beamId then
        local ok3, phwid = pcall(function() return MP.GetPlayerHWID(playerID) end)
        if ok3 and phwid and type(phwid) == "string" and phwid ~= "" then
            beamId = "HWID:srv:" .. phwid
        end
    end
    
    logDebug("getPlayerStableInfo(#" .. tostring(playerID) .. ") identifiers parsed: beam_id=" .. tostring(beamId) .. ", ip=" .. tostring(ip) .. ", name=" .. tostring(name))
    
    -- Fallback: use guest name as stable ID
    if not beamId and name then
        local guestId = name:match("uest(%d+)$")
        if guestId then
            beamId = "GUEST:" .. name
        else
            beamId = "NAME:" .. name
        end
    end
    
    -- Last fallback
    if not beamId then
        beamId = "FALLBACK:player" .. tostring(playerID)
    end
    
    -- Debug log
    logDebug("getPlayerStableInfo(" .. tostring(playerID) .. ") -> beam_id=" .. beamId .. ", ip=" .. tostring(ip) .. ", name=" .. tostring(name))
    
    return beamId, ip, name
end

-- ============================================================
-- Welcome Guide
-- ============================================================
local function sendWelcomeGuide(playerID, playerName)
    local msg = {}
    if not ENABLE_CHAT_LOGIN then
        msg[1] = " =========== 授信指引 ==========="
        msg[2] = " 欢迎来到服务器！本服靠「网站注册 + 机器码验证」给授信"
        msg[3] = " 1) 启动 BMPHWID_Bridge.exe (它会自动上报你的机器码)"
        msg[4] = " 2) 浏览器打开 http://<服务器IP>:12124/account ，点【读取本机】注册"
        msg[5] = " 注册立刻生效, 不用重进; 用 /whoami 查看状态"
        msg[6] = " /vehiclelimit - 查看车辆上限"
        msg[7] = " /help - 查看所有命令"
        msg[8] = " ================================"
        msg[9] = " [车辆限制] 未授信 1 辆, 授信 5 辆, 管理员 999 辆"
        msg[10] = " [授信条件] 在服务器网站注册过且机器码验证通过"
    else
        msg[1] = " =========== 登录指引 ==========="
        msg[2] = " 欢迎来到服务器！请使用以下命令注册或登录："
        msg[3] = " /register <账号> <密码> - 注册新账号"
        msg[4] = " /login <账号> <密码> - 登录已有账号"
        msg[5] = " /whoami - 查看登录状态"
        msg[6] = " /vehiclelimit - 查看车辆上限"
        msg[7] = " /bmpid <UUID> - 绑定稳定机器码 (认证后 5 辆)"
        msg[8] = " /help - 查看所有命令"
        msg[9] = " ================================"
        msg[10] = " [车辆限制] 未认证用户 1 辆, 认证用户 5 辆"
        msg[11] = " [认证方式] /bmpid UUID + /login 账号 密码"
    end

    for _, m in ipairs(msg) do
        MP.SendChatMessage(playerID, m)
    end
end

-- ============================================================
-- Register / Login / Logout
-- ============================================================
local function registerAccount(playerID, username, password)
    if not username or username == "" then
        MP.SendChatMessage(playerID, " 错误: 账号名不能为空")
        return false
    end
    if not password or password == "" then
        MP.SendChatMessage(playerID, " 错误: 密码不能为空")
        return false
    end
    if accounts[username] then
        MP.SendChatMessage(playerID, " 错误: 账号 " .. username .. " 已存在")
        return false
    end
    
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    local pwdHash = hashPassword(password)
    if not pwdHash then
        MP.SendChatMessage(playerID, " 错误: 密码无效")
        return false
    end
    
    accounts[username] = {
        username = username,
        password_hash = pwdHash,
        register_time = os.time(),
        bind_beam_ids = Array({ beamId }),
        login_records = Array({})
    }
    
    saveAccounts()
    MP.SendChatMessage(playerID, " 注册成功！账号 " .. username .. " 已创建")
    MP.SendChatMessage(playerID, " 请使用 /login " .. username .. " <密码> 登录")
    
    logMsg("玩家 " .. tostring(playerName) .. " 注册了账号: " .. username)
    return true
end

local function loginAccount(playerID, username, password)
    if not username or not accounts[username] then
        MP.SendChatMessage(playerID, " 错误: 账号 " .. tostring(username) .. " 不存在")
        return false
    end
    
    local account = accounts[username]
    
    local storedHash = account.password_hash or ""
    if not verifyPassword(password, storedHash) then
        MP.SendChatMessage(playerID, " 错误: 密码不正确")
        return false
    end
    
    local beamId, playerIp, playerName = getPlayerStableInfo(playerID)
    if not playerName then playerName = MP.GetPlayerName(playerID) end
    
    -- Update bind: 如果 beamId 不稳定(GUEST:开头)，还同时绑定公网 IPv4 作为兜底
    --   (只要玩家 IP 不变，guest 数字变了照样自动登录)
    local function _tryBind(id)
        if not id or id == "" then return end
        local bound = false
        for _, b in ipairs(account.bind_beam_ids or {}) do
            if b == id then bound = true; break end
        end
        if not bound then
            account.bind_beam_ids = account.bind_beam_ids or Array({})
            account.bind_beam_ids[#account.bind_beam_ids + 1] = id
        end
    end
    _tryBind(beamId)
    if (not beamId or beamId:sub(1,6) == "GUEST:" or beamId:sub(1,7) == "NAME:g") then
        -- 当前拿到的是不稳定 guest 名，绑定 IP 兜底（允许 IPv4 公网；局域网/内网 10./192.168./172.16-31. 也允许）
        if playerIp and playerIp:match("^%d+%.%d+%.%d+%.%d+$") then
            _tryBind("IP:" .. playerIp)
        end
    end
    
    -- Add login record
    account.login_records = account.login_records or Array({})
    account.login_records[#account.login_records + 1] = {
        time = os.time(),
        beam_id = beamId
    }
    
    saveAccounts()
    -- 标记为已认证 (bound_account, isPlayerAuthenticated 方法 3)
    if not playerAuthCache then playerAuthCache = {} end
    playerAuthCache[playerID] = playerAuthCache[playerID] or {}
    playerAuthCache[playerID].bound_account = username
    
    MP.SendChatMessage(playerID, " 登录成功！欢迎回来，" .. username)
    logMsg("玩家 " .. tostring(playerName) .. " 登录了账号: " .. username)
    return true
end

local function logoutAccount(playerID)
    if not isPlayerAuthenticated(playerID) then
        MP.SendChatMessage(playerID, " 你当前未登录任何账号")
        return
    end
    
    local playerName = MP.GetPlayerName(playerID)
    MP.SendChatMessage(playerID, " 已退出登录")
    logMsg("玩家 " .. tostring(playerName) .. " 退出了登录")
end

-- [已移除: 管理员命令 - 无管理员版]

-- ============================================================
-- Chat Message Handler
-- ============================================================
function onChatMessage(playerID, playerName, message)
    if not message or message == "" then return end

    -- 兜底: 玩家发任何消息都顺便 poll 一次 Bridge 聊天队列 (即使 timer 出问题也能出消息)
    _tryPollChatQueue()

    -- Quick classify: is it a command (/xxx)?
    local isCommand = (type(message) == "string" and message:sub(1, 1) == "/")
    
    -- Wrap entire handler in pcall to catch any errors silently
    local ok, err = pcall(function()
        _handleChat(playerID, playerName, message)
    end)
    if not ok then
        print("[BMP Login] ERROR in onChatMessage: " .. tostring(err))
        pcall(function()
            MP.SendChatMessage(playerID, " 处理聊天消息时出错: " .. tostring(err))
        end)
    end
    
    -- 关键: 所有 /开头命令 取消广播（防止 /login 密码、/bmpid 硬件码泄漏）
    -- Per BeamMP docs: return 1 cancels the event so it is NOT shown to anyone
    if isCommand then
        return 1
    end
    
    -- 认证用户发言: 加 [授信用户] 前缀重新广播 (拦截原消息, 手动转发)
    if not isCommand then
        local beamId = getPlayerStableInfo(playerID)
        if beamId and isPlayerAuthenticated(playerID) then
            -- 用账号名 (如果已登录) 代替 guest 名
            local displayName = playerName
            if playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account then
                displayName = playerAuthCache[playerID].bound_account
            end
            MP.SendChatMessage(-1, " [授信用户] " .. displayName .. ": " .. message)
            return 1  -- 拦截原消息, 用带前缀的替代
        end
    end
    
    -- 普通聊天返回 0 = 允许正常广播
    return 0
end

-- ============================================================
-- 在线人数: 唯一口径
--   以前是「进服时先数一遍 + 1」「离服时先数一遍 - 1」这种增量算法,
--   一旦事件重复/漏发 (重连、进服过程中掉线、插件热重载) 就会永久偏移,
--   而且 online_players.json 与聊天里的数字会对不上。
--   现在统一「改完表再数一次」, 谁问都是同一个数。
-- ============================================================
function countOnlinePlayers()
    local n = 0
    for _ in pairs(onlinePlayers) do n = n + 1 end
    return n
end

-- 兼容旧调用 (投票踢出用)
local function _countOnlinePlayers()
    return countOnlinePlayers()
end

local function _getOnlinePlayerName(pid)
    local data = onlinePlayers[pid]
    if data and data.name then return data.name end
    local ok, pn = pcall(function() return MP.GetPlayerName(pid) end)
    if ok and pn then return pn end
    return "Player" .. tostring(pid)
end

local function _broadcastVoteKick(msg)
    if msg then
        MP.SendChatMessage(-1, " [投票] " .. msg)
    end
end

local function _startVoteKick(initiatorID, targetID, reason)
    -- 先统一转换为数字
    initiatorID = tonumber(initiatorID) or initiatorID
    targetID = tonumber(targetID) or targetID
    
    -- 基本检查
    if activeVoteKick then
        MP.SendChatMessage(initiatorID, " 已有进行中的投票, 请等待当前投票结束")
        return false
    end
    if initiatorID == targetID then
        MP.SendChatMessage(initiatorID, " 不能对自己发起投票")
        return false
    end
    if not onlinePlayers[targetID] then
        MP.SendChatMessage(initiatorID, " 目标玩家不在线或不存在")
        return false
    end
    if isPlayerAdmin(targetID) then
        MP.SendChatMessage(initiatorID, " 不能对管理员发起投票")
        return false
    end
    
    local onlineCount = _countOnlinePlayers()
    if onlineCount < VOTE_KICK_MIN_PLAYERS then
        MP.SendChatMessage(initiatorID, " 在线人数不足 (需要至少 " .. tostring(VOTE_KICK_MIN_PLAYERS) .. " 人)")
        return false
    end
    
    -- 冷却检查 (用单调时钟, 系统时间被改动也不会算错)
    local now = monotonicNow()
    if voteKickCooldown[initiatorID] and (now - voteKickCooldown[initiatorID]) < 120 then
        local remain = math.ceil(120 - (now - voteKickCooldown[initiatorID]))
        MP.SendChatMessage(initiatorID, " 发起人冷却中, 还需等待 " .. tostring(remain) .. " 秒")
        return false
    end
    
    local targetName = _getOnlinePlayerName(targetID)
    
    activeVoteKick = {
        target_id = targetID,
        initiator_id = initiatorID,
        reason = reason or "未说明原因",
        start_time = now,
        duration = VOTE_KICK_DURATION,
        votes_yes = 0,
        votes_no = 0,
        voted_players = {},
        last_broadcast = now,
    }
    voteKickCooldown[initiatorID] = now
    
    _broadcastVoteKick("投票踢出已发起! 目标: " .. tostring(targetName) .. " (ID:" .. tostring(targetID) .. ")")
    _broadcastVoteKick("原因: " .. tostring(reason or "未说明"))
    _broadcastVoteKick("在 " .. tostring(VOTE_KICK_DURATION) .. " 秒内输入 /vote yes 或 /vote no")
    
    return true
end

local function _handleVote(playerID, vote)
    if not activeVoteKick then
        MP.SendChatMessage(playerID, " 当前没有进行中的投票")
        return
    end
    
    playerID = tonumber(playerID) or playerID
    
    if playerID == activeVoteKick.initiator_id or playerID == activeVoteKick.target_id then
        -- 发起者和目标玩家不能投票
        if playerID == activeVoteKick.target_id then
            MP.SendChatMessage(playerID, " 你是被投票目标, 不能投票")
        else
            MP.SendChatMessage(playerID, " 你是发起者, 不能投票")
        end
        return
    end
    
    if activeVoteKick.voted_players[playerID] then
        MP.SendChatMessage(playerID, " 你已经投过票了")
        return
    end
    
    local v = vote:lower()
    if v == "yes" or v == "y" or v == "赞成" or v == "支持" then
        activeVoteKick.votes_yes = activeVoteKick.votes_yes + 1
        activeVoteKick.voted_players[playerID] = true
        MP.SendChatMessage(playerID, " 你投了赞成票 (同意:" .. tostring(activeVoteKick.votes_yes) .. " 反对:" .. tostring(activeVoteKick.votes_no) .. ")")
    elseif v == "no" or v == "n" or v == "反对" or v == "不赞成" then
        activeVoteKick.votes_no = activeVoteKick.votes_no + 1
        activeVoteKick.voted_players[playerID] = true
        MP.SendChatMessage(playerID, " 你投了反对票 (同意:" .. tostring(activeVoteKick.votes_yes) .. " 反对:" .. tostring(activeVoteKick.votes_no) .. ")")
    else
        MP.SendChatMessage(playerID, " 投票无效, 请输入 yes 或 no")
    end
end

local function _cancelVoteKick(reason)
    if not activeVoteKick then return end
    local msg = "投票已取消"
    if reason then msg = msg .. ": " .. reason end
    _broadcastVoteKick(msg)
    activeVoteKick = nil
end

local function _finalizeVoteKick()
    if not activeVoteKick then return end
    
    local total_voters = activeVoteKick.votes_yes + activeVoteKick.votes_no
    local yes_pct = 0
    if total_voters > 0 then
        yes_pct = math.floor(activeVoteKick.votes_yes * 100 / total_voters)
    end
    
    local targetID = activeVoteKick.target_id
    local targetName = _getOnlinePlayerName(targetID)
    local initiatorName = _getOnlinePlayerName(activeVoteKick.initiator_id)
    local reason = activeVoteKick.reason
    
    _broadcastVoteKick("投票结束! 同意:" .. tostring(activeVoteKick.votes_yes) .. " 反对:" .. tostring(activeVoteKick.votes_no) .. " (赞成率:" .. tostring(yes_pct) .. "%)")
    
    -- 过半数赞成票则踢出
    if activeVoteKick.votes_yes > activeVoteKick.votes_no and yes_pct > 50 then
        _broadcastVoteKick("投票通过! " .. tostring(targetName) .. " (ID:" .. tostring(targetID) .. ") 将被踢出!")
        _broadcastVoteKick("发起者: " .. tostring(initiatorName) .. " | 原因: " .. tostring(reason))
        -- 执行踢出: 写入踢人队列并立即踢出
        local queue = {}
        local f = io.open(KICK_QUEUE_FILE, "r")
        if f then
            local content = f:read("*a")
            f:close()
            local ok, parsed = pcall(jsonDecode, content or "[]")
            if ok and type(parsed) == "table" then
                queue = parsed
            end
        end
        local kick_entry = {
            playerID = targetID,
            reason = "投票踢出: " .. tostring(reason) .. " (同意:" .. tostring(activeVoteKick.votes_yes) .. " 反对:" .. tostring(activeVoteKick.votes_no) .. ")",
            initiator = "vote",
            timestamp = os.time(),
            done = false,
        }
        table.insert(queue, kick_entry)
        -- writeJsonFile: 自带建目录 + 原子写 (直接 io.open 在全新部署上会静默失败)
        writeJsonFile(KICK_QUEUE_FILE, queue)
        -- 立即踢出
        pcall(function() MP.DropPlayer(targetID, "投票踢出: " .. tostring(reason)) end)
    else
        _broadcastVoteKick("投票未通过, " .. tostring(targetName) .. " 不会被踢出")
    end
    
    activeVoteKick = nil
end

local function _checkVoteKickTimeout()
    if not activeVoteKick then return end
    
    local now = monotonicNow()
    local elapsed = now - activeVoteKick.start_time
    local remaining = activeVoteKick.duration - elapsed
    
    -- 目标玩家已下线
    if not onlinePlayers[activeVoteKick.target_id] then
        _cancelVoteKick("目标玩家已离线")
        return
    end
    
    -- 倒计时广播 (每 VOTE_KICK_BROADCAST_INTERVAL 秒)
    if now - activeVoteKick.last_broadcast >= VOTE_KICK_BROADCAST_INTERVAL and remaining > 0 then
        activeVoteKick.last_broadcast = now
        local targetName = _getOnlinePlayerName(activeVoteKick.target_id)
        local targetID = tonumber(activeVoteKick.target_id) or activeVoteKick.target_id
        local msg = string.format("投票进行中: %s (ID:%s) | 赞成:%d 反对:%d | 剩余:%d秒 | 输入 /vote yes 或 /vote no",
            tostring(targetName), tostring(targetID),
            activeVoteKick.votes_yes, activeVoteKick.votes_no, math.ceil(remaining))
        _broadcastVoteKick(msg)
    end
    
    -- 时间到
    if remaining <= 0 then
        _finalizeVoteKick()
    end
end

-- 从 vote_queue.json 读取 Bridge 发起的投票请求, 处理后标记 processed
local function _processVoteQueue()
    local f = io.open(VOTE_QUEUE_FILE, "r")
    if not f then return end
    local content = f:read("*all") or "[]"
    f:close()
    
    local ok_parse, queue = pcall(jsonDecode, content)
    if not ok_parse or type(queue) ~= "table" or #queue == 0 then return end
    
    local changed = false
    for _, entry in ipairs(queue) do
        if entry and type(entry) == "table" and not entry.processed then
            local action = entry.action
            if action == "kick" then
                -- Bridge 发起投票踢出
                local targetName = entry.target_name or ""
                local initiatorName = entry.initiator_name or ""
                local reason = entry.reason or "Bridge 发起"
                
                -- 通过名字找到 playerID
                local targetID = nil
                local initiatorID = nil
                for pid, info in pairs(onlinePlayers) do
                    if info and info.name == targetName then targetID = pid end
                    if info and info.name == initiatorName then initiatorID = pid end
                end
                
                if targetID and initiatorID then
                    local ok = _startVoteKick(initiatorID, targetID, reason)
                    if ok then
                        _broadcastVoteKick("投票由 Bridge 发起: " .. tostring(initiatorName) .. " → " .. tostring(targetName))
                    end
                else
                    _broadcastVoteKick("投票请求被忽略: 玩家 " .. tostring(targetName) .. " 不在线")
                end
                entry.processed = true
                changed = true
                
            elseif action == "vote" then
                -- Bridge 投票
                local voterName = entry.voter_name or ""
                local voteValue = entry.vote or ""
                
                local voterID = nil
                for pid, info in pairs(onlinePlayers) do
                    if info and info.name == voterName then voterID = pid end
                end
                
                if voterID and activeVoteKick then
                    if voteValue == "yes" or voteValue == "y" or voteValue == "赞成" then
                        _handleVote(voterID, "yes")
                    elseif voteValue == "no" or voteValue == "n" or voteValue == "反对" then
                        _handleVote(voterID, "no")
                    end
                end
                entry.processed = true
                changed = true
            end
        end
    end
    
    if changed then
        local fw = io.open(VOTE_QUEUE_FILE, "w")
        if fw then
            local ok, encoded = pcall(jsonEncode, queue)
            if ok and encoded then
                fw:write(encoded)
            end
            fw:close()
        end
    end
end

function _handleChat(playerID, playerName, message)
    
    local beamId = getPlayerStableInfo(playerID)
    local role = getPlayerRole(beamId, playerID)
    local prefix = "" .. playerName .. ": "
    
    -- Guest reminder
    if role == "游客" then
        if message:sub(1, 1) ~= "/" then
            if ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " 提示: 你尚未登录。使用 /help 查看可用命令")
            else
                MP.SendChatMessage(playerID, " 提示: 你还没通过认证 (需官方账号登录 + 机器码), 输入 /whoami 查看")
            end
        end
    end
    
    -- Commands
    if message:sub(1, 1) == "/" then
        local cmd, args = message:match("^(%S+)%s*(.*)$")
        if not cmd then return end
        cmd = cmd:lower()
        
        -- 管理员命令: 重载最新数据 (API 可能在服务器运行时修改了 JSON)
        local admin_cmds = {
            ["/listonline"] = true, ["/kick"] = true, ["/ban"] = true,
            ["/unban"] = true, ["/listadmins"] = true, ["/addadmin"] = true,
            ["/removeadmin"] = true,
        }
        if admin_cmds[cmd] then
            pcall(function() loadAccounts() end)
            pcall(function() loadBanlist() end)
        end
        
        if cmd == "/help" then
            MP.SendChatMessage(playerID, " =========== 玩家命令 ===========")
            if ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " /register <账号> <密码> - 注册新账号")
                MP.SendChatMessage(playerID, " /login <账号> <密码> - 登录账号")
                MP.SendChatMessage(playerID, " /logout - 退出登录")
            end
            MP.SendChatMessage(playerID, " /whoami - 查看认证状态")
            MP.SendChatMessage(playerID, " /vehiclelimit - 查看车辆上限")
            MP.SendChatMessage(playerID, " /bmpid <payload> - 客户端HWID回传")
            MP.SendChatMessage(playerID, " =========== 投票踢出 ===========")
            MP.SendChatMessage(playerID, " /votekick <ID> [原因] - 发起投票踢出")
            MP.SendChatMessage(playerID, " /vote <yes|no> - 参与投票")
            MP.SendChatMessage(playerID, " /cancelvote - 取消当前投票 (管理员)")
            if isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " =========== 管理员命令 ===========")
                MP.SendChatMessage(playerID, " /listonline - 查看在线玩家列表")
                MP.SendChatMessage(playerID, " /kick <ID> [原因] - 踢出玩家")
                MP.SendChatMessage(playerID, " /ban <ID> <类型> <值> [天数] [原因] - 封禁玩家")
                MP.SendChatMessage(playerID, " /unban <ID> <类型> <值> - 解封玩家")
                MP.SendChatMessage(playerID, " /listadmins - 查看管理员列表")
                MP.SendChatMessage(playerID, " /addadmin <账号> - 设为管理员")
                MP.SendChatMessage(playerID, " /removeadmin <账号> - 移除管理员")
            end

        elseif cmd == "/register" then
            if not ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " " .. CHAT_LOGIN_OFF_MSG)
                return
            end
            local uname, pwd = args:match("^(%S+)%s+(%S+)$")
            if uname and pwd then
                registerAccount(playerID, uname, pwd)
            else
                MP.SendChatMessage(playerID, " 用法: /register <账号> <密码>")
            end

        elseif cmd == "/login" then
            if not ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " " .. CHAT_LOGIN_OFF_MSG)
                return
            end
            local uname, pwd = args:match("^(%S+)%s+(%S+)$")
            if uname and pwd then
                loginAccount(playerID, uname, pwd)
            else
                MP.SendChatMessage(playerID, " 用法: /login <账号> <密码>")
            end

        elseif cmd == "/logout" then
            if not ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " " .. CHAT_LOGIN_OFF_MSG)
                return
            end
            logoutAccount(playerID)

        elseif cmd == "/whoami" then
            local beamId = getPlayerStableInfo(playerID)
            local role = getPlayerRole(beamId, playerID)
            local limit = getPlayerVehicleLimit(playerID)
            local authLabel = isPlayerAuthenticated(playerID) and "已认证" or "未认证"
            MP.SendChatMessage(playerID, " 身份: " .. role .. "  认证状态: " .. authLabel .. "  车辆上限: " .. limit .. " 辆")

            if not ENABLE_CHAT_LOGIN then
                -- 授信只看「机器码 + 网站注册」; 官方账号只作为附加信息显示
                local webOk, webUser = getWebAccount(playerID)
                local hwid = hasStableHwid(playerID)
                local official = hasOfficialAccount(playerID)
                MP.SendChatMessage(playerID,
                    " 授信条件: 机器码 " .. (hwid and "✓ 已上报" or "✗ 未上报")
                    .. "   网站注册 " .. (webOk and ("✓ " .. tostring(webUser)) or "✗ 未注册")
                    .. "   (当前身份: " .. (official and "官方账号" or "游客") .. ")")
                local bid = getPlayerBeammpId(playerID)
                if bid then
                    MP.SendChatMessage(playerID, " 官方账号ID: " .. tostring(bid))
                end
                -- 只提示第一个没满足的, 免得一次刷屏
                if not hwid then
                    MP.SendChatMessage(playerID, " 请启动 BMPHWID_Bridge.exe (机器码上报后才能注册)")
                elseif not webOk then
                    MP.SendChatMessage(playerID, " 还需要注册: 浏览器打开 http://<服务器IP>:12124/account")
                    MP.SendChatMessage(playerID, " 点【读取本机】自动填机器码, 注册后立刻变成授信 (5 辆)")
                end
            end

            if beamId then
                MP.SendChatMessage(playerID, " 绑定标识: " .. tostring(beamId))
            end

        elseif cmd == "/vehiclelimit" or cmd == "/vehicles" or cmd == "/carlimit" then
            local limit = getPlayerVehicleLimit(playerID)
            local authLabel = isPlayerAuthenticated(playerID) and "已认证用户" or "未认证用户"
            MP.SendChatMessage(playerID, " 您是[" .. authLabel .. "], 车辆上限 " .. limit .. " 辆")

        elseif cmd == "/bmpid" then
            local payload = args and args:match("^%s*(.-)%s*$") or ""
            handleBmpidCommand(playerID, playerName, payload)

        -- ============ 投票踢出命令 (所有玩家可用) ============
        elseif cmd == "/votekick" then
            local target_id, reason = args:match("^(%d+)%s*(.*)$")
            if not target_id then
                MP.SendChatMessage(playerID, " 用法: /votekick <玩家ID> [原因]")
                return
            end
            _startVoteKick(playerID, tonumber(target_id), reason)

        elseif cmd == "/vote" then
            local vote = args:match("^(%S+)")
            if not vote then
                MP.SendChatMessage(playerID, " 用法: /vote <yes|no>")
                return
            end
            _handleVote(playerID, vote)

        elseif cmd == "/cancelvote" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            _cancelVoteKick("管理员终止")

        -- ============ 管理员命令 ============
        elseif cmd == "/listonline" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            MP.SendChatMessage(playerID, " ======== 在线玩家列表 ========")
            local count = 0
            for pid, pdata in pairs(onlinePlayers) do
                count = count + 1
                local pname = pdata.name or "Player" .. tostring(pid)
                local brole = pdata.role or "未知"
                local acct = pdata.bind_account or ""
                local beam_id = (pdata.beam_id or ""):sub(1, 50)
                local veh = pdata.vehicle_count or 0
                local auth = pdata.is_authenticated and "✓" or "✗"
                local info = string.format(" [%d] %s | %s | 认证:%s | 账号:%s | 车辆:%d",
                    pid, pname, brole, auth, acct, veh)
                MP.SendChatMessage(playerID, info)
                if beam_id ~= "" then
                    MP.SendChatMessage(playerID, "   BeamID: " .. beam_id)
                end
            end
            MP.SendChatMessage(playerID, " 共 " .. tostring(count) .. " 人在线")

        elseif cmd == "/kick" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            local target_id, reason = args:match("^(%d+)%s*(.*)$")
            target_id = tonumber(target_id)
            if not target_id then
                MP.SendChatMessage(playerID, " 用法: /kick <玩家ID> [原因]")
                return
            end
            if onlinePlayers[target_id] then
                local tname = onlinePlayers[target_id].name or "Player" .. tostring(target_id)
                reason = (reason and reason:match("^%s*(.-)%s*$")) or "管理员踢出"
                local now = os.time()
                local kick_entry = {
                    id = tostring(os.time()) .. "_" .. tostring(math.random(1000,9999)),
                    playerID = target_id,
                    reason = reason,
                    time = now,
                    done = false,
                }
                local queue = readJsonFile(KICK_QUEUE_FILE) or {}
                if type(queue) ~= "table" then queue = {} end
                table.insert(queue, kick_entry)
                writeJsonFile(KICK_QUEUE_FILE, queue)
                MP.SendChatMessage(playerID, " 已踢出 #" .. target_id .. " " .. tname .. " (原因: " .. reason .. ")")
                MP.SendChatMessage(-1, " [踢出] " .. tostring(tname) .. " 已被管理员踢出 (原因: " .. reason .. ")")
                print("[BMP Login] 管理员 " .. tostring(playerName) .. " 踢出玩家 #" .. target_id .. " 原因: " .. tostring(reason))
            else
                MP.SendChatMessage(playerID, " 找不到玩家 ID #" .. tostring(target_id))
            end

        elseif cmd == "/ban" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            -- /ban <玩家ID> <类型> <值> [天数] [原因]
            local target_id, btype, bval, days_rest, reason_rest = args:match("^(%d+)%s+(%S+)%s+(%S+)%s*(.*)$")
            target_id = tonumber(target_id)
            if not target_id or not btype or not bval then
                MP.SendChatMessage(playerID, " 用法: /ban <玩家ID> <account|hwid|ip|name> <值> [天数] [原因]")
                MP.SendChatMessage(playerID, " 示例: /ban 3 hwid a543b2c8-edc1 7 使用外挂")
                MP.SendChatMessage(playerID, "       /ban 2 account DRIFTKING 0 永久封禁")
                return
            end
            local duration_days = 0
            local reason = ""
            if days_rest then
                local d, r = days_rest:match("^(%d+)%s*(.*)$")
                if d then
                    duration_days = tonumber(d)
                    reason = r or ""
                else
                    reason = days_rest
                end
            end
            btype = btype:lower()
            if btype == "id" then btype = "account" end  -- 兼容旧用法
            if btype ~= "account" and btype ~= "hwid" and btype ~= "ip" and btype ~= "name" then
                MP.SendChatMessage(playerID, " 类型必须是 account/hwid/ip/name")
                return
            end
            if not reason or reason == "" then reason = "管理员封禁" end
            local now = os.time()
            local expires_at = duration_days > 0 and (now + duration_days * 86400) or 0
            
            loadBanlist()  -- reload global banlist
            local key = (btype == "account" or btype == "name") and "accounts" or "devices"
            banlist[key] = banlist[key] or {}
            
            local entry
            if btype == "account" or btype == "name" then
                entry = {account = bval, value = bval, reason = reason,
                         time = now, duration_days = duration_days, expires_at = expires_at}
            elseif btype == "hwid" then
                entry = {device_id = bval, value = bval, reason = reason,
                         time = now, duration_days = duration_days, expires_at = expires_at}
            else
                entry = {ip = bval, value = bval, reason = reason,
                         time = now, duration_days = duration_days, expires_at = expires_at}
            end
            
            -- 去除旧的相同条目
            local new_list = {}
            for _, e in ipairs(banlist[key]) do
                if type(e) == "table" and (e.value or "") ~= bval then
                    table.insert(new_list, e)
                end
            end
            table.insert(new_list, entry)
            banlist[key] = new_list
            writeJsonFile(BANLIST_FILE, banlist)
            
            local dur_text = duration_days > 0 and (duration_days .. "天") or "永久"
            local target_name = onlinePlayers[target_id] and onlinePlayers[target_id].name or "Player" .. tostring(target_id)
            MP.SendChatMessage(playerID, " 已封禁 " .. btype .. ":" .. bval .. " (" .. dur_text .. ")")
            MP.SendChatMessage(playerID, " 目标玩家 #" .. target_id .. " " .. tostring(target_name))
            print("[BMP Login] 管理员 " .. tostring(playerName) .. " 封禁 " .. btype .. ":" .. bval .. " " .. dur_text)
            
            -- 立即踢出已在线的被封禁玩家
            if onlinePlayers[target_id] then
                pcall(function() MP.DropPlayer(target_id, "您已被封禁: " .. reason) end)
            end

        elseif cmd == "/unban" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            local btype, bval = args:match("^(%S+)%s+(%S+)$")
            if not btype or not bval then
                MP.SendChatMessage(playerID, " 用法: /unban <account|hwid|ip|name> <值>")
                return
            end
            btype = btype:lower()
            loadBanlist()  -- reload global banlist
            local key = (btype == "account" or btype == "name") and "accounts" or "devices"
            banlist[key] = banlist[key] or {}
            local new_list = {}
            for _, e in ipairs(banlist[key]) do
                if type(e) == "table" and (e.value or "") ~= bval then
                    table.insert(new_list, e)
                end
            end
            banlist[key] = new_list
            writeJsonFile(BANLIST_FILE, banlist)
            MP.SendChatMessage(playerID, " 已解封 " .. btype .. ":" .. bval)
            print("[BMP Login] 管理员 " .. tostring(playerName) .. " 解封 " .. btype .. ":" .. bval)

        elseif cmd == "/listadmins" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            MP.SendChatMessage(playerID, " ======== 管理员列表 ========")
            local count = 0
            if not ENABLE_CHAT_LOGIN then
                -- 官方账号模式: 管理员写死在 ADMIN_BEAMMP_IDS
                for bid, _ in pairs(ADMIN_BEAMMP_IDS) do
                    count = count + 1
                    MP.SendChatMessage(playerID, "  - 官方账号ID " .. tostring(bid))
                end
                if count == 0 then
                    MP.SendChatMessage(playerID, " (白名单为空! 请在 main.lua 的 ADMIN_BEAMMP_IDS 里填自己的官方账号ID)")
                else
                    MP.SendChatMessage(playerID, " 共 " .. tostring(count) .. " 个管理员")
                end
                return
            end
            for uname, acc in pairs(accounts) do
                if type(acc) == "table" and acc.is_admin then
                    count = count + 1
                    MP.SendChatMessage(playerID, "  - " .. tostring(uname))
                end
            end
            for uname, _ in pairs(ADMIN_WHITELIST) do
                count = count + 1
                MP.SendChatMessage(playerID, "  - " .. tostring(uname) .. " (白名单)")
            end
            if count == 0 then
                MP.SendChatMessage(playerID, " (暂无管理员)")
            else
                MP.SendChatMessage(playerID, " 共 " .. tostring(count) .. " 个管理员")
            end

        elseif cmd == "/addadmin" or cmd == "/removeadmin" then
            if not ENABLE_CHAT_LOGIN then
                MP.SendChatMessage(playerID, " 内置账号已关闭, 管理员请在 main.lua 的 ADMIN_BEAMMP_IDS 里配置")
                return
            end
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            local target = args:match("^(%S+)")
            if not target then
                MP.SendChatMessage(playerID, " 用法: " .. cmd .. " <账号>")
                return
            end
            -- 查找账号 (大小写不敏感)
            local found_key = nil
            for k, _ in pairs(accounts) do
                if k:lower() == target:lower() then
                    found_key = k
                    break
                end
            end
            if cmd == "/addadmin" then
                if found_key then
                    accounts[found_key].is_admin = true
                    saveAccounts()
                    MP.SendChatMessage(playerID, " 账号 " .. found_key .. " 已设为管理员")
                    print("[BMP Login] 管理员 " .. tostring(playerName) .. " 提升 " .. found_key .. " 为管理员")
                else
                    MP.SendChatMessage(playerID, " 账号 " .. target .. " 不存在")
                end
            else
                local found_whitelist = nil
                for wk, _ in pairs(ADMIN_WHITELIST) do
                    if wk:lower() == target:lower() then
                        found_whitelist = wk
                        break
                    end
                end
                if found_key then
                    accounts[found_key].is_admin = false
                    saveAccounts()
                    MP.SendChatMessage(playerID, " 账号 " .. found_key .. " 已移除管理员权限")
                    print("[BMP Login] 管理员 " .. tostring(playerName) .. " 移除 " .. found_key .. " 的管理员权限")
                elseif found_whitelist then
                    ADMIN_WHITELIST[found_whitelist] = nil
                    MP.SendChatMessage(playerID, " 账号 " .. found_whitelist .. " 已从白名单移除")
                else
                    MP.SendChatMessage(playerID, " 账号 " .. target .. " 不是管理员")
                end
            end

        else
            MP.SendChatMessage(playerID, " 未知命令: " .. cmd .. "，使用 /help 查看可用命令")
        end
        
        return  -- Don't echo commands to public chat
    end
    
    -- Public chat message
    logDebug("Chat: [" .. tostring(playerName) .. "] " .. tostring(message))
end

-- ============================================================
-- Event Handlers
-- ============================================================
function onInit()
    -- Use print for init messages (MP.SendChatMessage may not be ready)
    print("[BMP Login] Plugin v" .. PLUGIN_VERSION .. " 正在加载...")
    
    -- Load all data
    pcall(function()
        loadAccounts()
    end)
    pcall(function()
        loadBanlist()
    end)
    pcall(function()
        loadGuestMap()
    end)
    pcall(function()
        loadBindings()
    end)
    pcall(function()
        rebuildVerifiedHwids()      -- 授信白名单 (accounts.json 里 verified=true 的机器码)
        print("[BMP Login] 已注册且验证的机器码: " .. verifiedCount)
    end)
    
    -- Register all events (BeamMP v3.9.3 requires MP.RegisterEvent with 2 params)
    pcall(function()
        if MP.RegisterEvent then
            MP.RegisterEvent("onPlayerAuth", "onPlayerAuth")
            MP.RegisterEvent("onPlayerJoining", "onPlayerJoining")
            MP.RegisterEvent("onPlayerJoin", "onPlayerConnected")
            MP.RegisterEvent("onPlayerDisconnect", "onPlayerDisconnect")
            MP.RegisterEvent("onChatMessage", "onChatMessage")
            MP.RegisterEvent("onVehicleSpawn", "onVehicleSpawn")
            MP.RegisterEvent("onVehicleEdited", "onVehicleEdited")
            MP.RegisterEvent("onVehicleReset", "onVehicleReset")
            MP.RegisterEvent("onVehicleDeleted", "onVehicleDeleted")
            -- Client HWID plugin events
            MP.RegisterEvent("BMPHWID:HWIDReply", "onHwidReply")
            MP.RegisterEvent("BMPHWID:Version", "onHwidVersion")
            -- 实时地图: 客户端每秒推一次坐标
            MP.RegisterEvent("BMPHWID:Pos", "onHwidPos")

            -- ============================================================
            -- Chat queue timer (Bridge GUI → HTTP API → 文件 → 1Hz 轮询 → 公屏广播)
            -- 官方 API (v3.0+):  MP.CreateEventTimer(name, interval_ms)
            -- 替代不存在的 onTick (server 端无 onTick, 只有 client mod 才有)
            -- ============================================================
            local CHAT_POLL_EVENT = "BMP_CHAT_QUEUE_POLL"
            MP.RegisterEvent(CHAT_POLL_EVENT, "processChatQueueGlobal")
            local ok_timer = pcall(function() MP.CreateEventTimer(CHAT_POLL_EVENT, 1000) end)
            if ok_timer then
                print("[BMP Login] ChatQueue timer started (1Hz, 1000ms) using MP.CreateEventTimer")
            else
                print("[BMP Login] WARN: MP.CreateEventTimer failed, chat queue will be polled on chat/join events only")
            end
            print("[BMP Login] All events registered successfully (incl. BMPHWID client + chat poll timer)")
        else
            print("[BMP Login] ERROR: MP.RegisterEvent not available")
        end
    end)
    
    print("[BMP Login] ========================================")
    print("[BMP Login] Plugin v" .. PLUGIN_VERSION .. " 已加载")
    print("[BMP Login] ========================================")
    print("[BMP Login] 事件: onPlayerAuth, onPlayerJoining, onPlayerConnected, onPlayerDisconnect, onChatMessage")
    print("[BMP Login] 数据目录: " .. DATA_DIR)
    -- 认证模式 (部署后第一眼要能看出来)
    if ENABLE_CHAT_LOGIN then
        print("[BMP Login] 认证模式: 内置账号 (/register /login)")
    else
        print("[BMP Login] 认证模式: 网站注册 + 机器码验证"
            .. "  (要求网站验证=" .. tostring(REQUIRE_WEB_VERIFY)
            .. ", 额外要求机器码=" .. tostring(REQUIRE_HWID)
            .. ", 额外要求官方账号=" .. tostring(REQUIRE_OFFICIAL_ACCOUNT) .. ")")
        print("[BMP Login] 车辆上限: 授信 " .. VEHICLE_LIMITS.authenticated
            .. " 辆 / 未授信 " .. VEHICLE_LIMITS.unauthenticated
            .. " 辆 / 管理员 " .. VEHICLE_LIMITS.admin .. " 辆")
        local n = 0
        for _ in pairs(ADMIN_BEAMMP_IDS) do n = n + 1 end
        if n == 0 then
            print("[BMP Login] *** 警告: ADMIN_BEAMMP_IDS 是空的, 游戏内没有任何管理员! ***")
            print("[BMP Login] *** 进服后输 /whoami 看到自己的官方账号ID, 填进 main.lua 的 ADMIN_BEAMMP_IDS ***")
        else
            print("[BMP Login] 管理员白名单: " .. n .. " 个官方账号ID")
        end
    end
    -- BeamMP v3.9.4+ 适配状态
    if HAS_SERVER_TIME then
        print("[BMP Login] 时间源: MP.GetServerTime() (BeamMP v3.9.4+ 单调时钟)")
    else
        print("[BMP Login] 时间源: os.time() 回退 (服务端 < v3.9.4, 无 MP.GetServerTime)")
    end
    print("[BMP Login] ========================================")
end

function onPlayerAuth(player_name, role, isGuest, identifiers)
    local ip = nil
    local beamId = nil
    
    -- Parse identifiers (BeamMP v3+ official API: key-value table)
    --  Docs example:
    --    function onPlayerAuth(player_name, role, isGuest, identifiers)
    --        local ip = identifiers.ip
    --        local beammp = identifiers.beammp or "N/A"
    if identifiers and type(identifiers) == "table" then
        -- [Method A] Direct KV access (preferred, per BeamMP Aug 2026 docs)
        if identifiers.beammp and type(identifiers.beammp) == "string" and identifiers.beammp ~= "" then
            beamId = "BEAMMP:" .. identifiers.beammp
        end
        if identifiers.ip and type(identifiers.ip) == "string" and identifiers.ip ~= "" then
            local s = tostring(identifiers.ip)
            if not s:match("^%d+%.%d+%.%d+%.%d+$") then
                beamId = beamId or ("BEAMMP-or-HWID:" .. s)
            else
                ip = s
            end
        end
        if identifiers.discord and type(identifiers.discord) == "string" and identifiers.discord ~= "" then
            beamId = beamId or "DISCORD:" .. identifiers.discord
        end
        if identifiers.hwid and type(identifiers.hwid) == "string" and identifiers.hwid ~= "" then
            beamId = beamId or "HWID:srv:" .. identifiers.hwid
        end
        -- [Method B] Fallback: prefix string array format (older builds)
        if not beamId or not ip then
            for k, v in pairs(identifiers) do
                if type(k) == "number" and type(v) == "string" then
                    local prefix, val = v:match("^([^:]+):(.+)$")
                    if prefix == "beammp" then
                        beamId = beamId or "BEAMMP:" .. val
                    elseif prefix == "hwid" then
                        beamId = beamId or "HWID:srv:" .. val
                    elseif prefix == "discord" then
                        beamId = beamId or "DISCORD:" .. val
                    elseif prefix == "ip" then
                        ip = ip or val
                    end
                end
            end
        end
    end
    
    -- Dump raw identifiers to console for debug (only prints, NOT chat)
    local raw = ""
    if identifiers and type(identifiers) == "table" then
        for k, v in pairs(identifiers) do
            raw = raw .. " " .. tostring(k) .. "=" .. tostring(v)
        end
    end
    print("[BMP Login] onPlayerAuth: " .. tostring(player_name) .. " | role=" .. tostring(role) .. " | isGuest=" .. tostring(isGuest) .. " | RAW[" .. (raw ~= "" and raw:sub(2) or "") .. "]")
    print("[BMP Login] onPlayerAuth: parsed beam_id=" .. tostring(beamId) .. " | ip=" .. tostring(ip))
    
    -- Check device ban (hwid / ip)
    if beamId then
        for _, entry in ipairs(banlist.devices or {}) do
            if type(entry) == "table" then
                local exp = entry.expires_at or 0
                if exp == 0 or exp > os.time() then
                    local val = entry.value or entry.device_id or entry.ip or ""
                    if val ~= "" and (beamId == val or beamId:sub(1, #val) == val) then
                        local remain = exp > 0 and math.ceil((exp - os.time()) / 86400) or 0
                        local dur_text = exp > 0 and ("剩余 " .. remain .. " 天") or "永久"
                        return "该设备已被封禁 (" .. dur_text .. "), 原因: " .. tostring(entry.reason or "")
                    end
                end
            end
        end
    end
    
    -- Check account ban (by beam_id or player name matching)
    for _, entry in ipairs(banlist.accounts or {}) do
        if type(entry) == "table" then
            local exp = entry.expires_at or 0
            if exp == 0 or exp > os.time() then
                local val = entry.value or entry.account or ""
                if val ~= "" and beamId and beamId:find(val, 1, true) then
                    local remain = exp > 0 and math.ceil((exp - os.time()) / 86400) or 0
                    local dur_text = exp > 0 and ("剩余 " .. remain .. " 天") or "永久"
                    return "该账号已被封禁 (" .. dur_text .. "), 原因: " .. tostring(entry.reason or "")
                end
            end
        end
    end
    
    -- Pre-populate auth cache with beam_id from identifiers (before player fully joins)
    if beamId or player_name then
        local nowTs = os.time()
        -- 顺手清理残留 (玩家连到一半掉线就不会有人来取回)
        for nm, v in pairs(pendingAuthByName) do
            if type(v) ~= "table" or not v.ts or (nowTs - v.ts) > 60 then
                pendingAuthByName[nm] = nil
            end
        end
        if player_name then
            pendingAuthByName[player_name] = {
                beam_id = beamId,
                ip = ip,
                ts = nowTs,
                name = player_name,
                -- 官方账号模式要用的两个字段
                beammp_id = (identifiers and type(identifiers) == "table" and type(identifiers.beammp) == "string")
                            and identifiers.beammp or nil,
                official = (isGuest == false) or (isGuest == nil and identifiers ~= nil
                            and type(identifiers) == "table" and type(identifiers.beammp) == "string"),
            }
        end
    end
    
    return true
end

function onPlayerJoining(playerID)
    -- [Step 0] 取回 onPlayerAuth 暂存的身份 (onPlayerAuth 阶段还没有 playerID)
    local ok_pn, curName = pcall(function() return MP.GetPlayerName(playerID) end)
    if not ok_pn then curName = nil end

    local pending = curName and pendingAuthByName[curName] or nil
    if not pending then
        -- 名字对不上 (改名 / 大小写差异): 只有唯一一条暂存时才敢用
        local only, cnt = nil, 0
        for _, v in pairs(pendingAuthByName) do cnt = cnt + 1; only = v end
        if cnt == 1 then pending = only end
    end

    if pending then
        playerAuthCache[playerID] = playerAuthCache[playerID] or {}
        if pending.beam_id then
            playerAuthCache[playerID].auth_beam_id = pending.beam_id
        end
        if pending.ip then
            playerAuthCache[playerID].auth_ip = pending.ip
        end
        -- 官方账号信息 (认证判定 / 管理员白名单都依赖它)
        playerAuthCache[playerID].beammp_id = pending.beammp_id
        playerAuthCache[playerID].official_account = pending.official == true
        if pending.name then pendingAuthByName[pending.name] = nil end
        print("[BMP Login] onPlayerJoining(#" .. tostring(playerID) .. "): 从 onPlayerAuth 携带 pending beam_id="
            .. tostring(pending.beam_id) .. " official=" .. tostring(pending.official)
            .. " beammp_id=" .. tostring(pending.beammp_id) .. " name=" .. tostring(curName))
    end
    
    local beamId, ip, name = getPlayerStableInfo(playerID)
    
    -- Check auto-login (仅旧账号模式; 官方账号模式下 accounts.json 不再参与认证)
    -- 支持 3 种匹配方式（优先级：beamId -> IP兜底 -> NAME兜底）：
    --   1. boundId == 当前 beamId            （命中 HWID 或 BEAMMP: 官方 ID 时）
    --   2. boundId == "IP:" .. playerIp      （登录过一次后，IP 没变 + guest 数字变了也能命中）
    --   3. boundId == "NAME:" .. playerName  （玩家强制改了 BeamMP launcher 登录名回退绑定）
    local autoUser, autoVia, autoBound = nil, nil, nil
    if ENABLE_CHAT_LOGIN then
        for username, account in pairs(accounts) do
            for _, boundId in ipairs(account.bind_beam_ids or {}) do
                if boundId == beamId then
                    autoUser, autoVia, autoBound = username, "稳定ID", boundId
                elseif ip and boundId == "IP:" .. ip then
                    autoUser, autoVia, autoBound = username, "IP兜底", boundId
                elseif name and boundId == "NAME:" .. name then
                    autoUser, autoVia, autoBound = username, "NAME兜底", boundId
                end
                if autoUser then break end
            end
            if autoUser then break end
        end
    end
    if autoUser then
        playerAuthCache[playerID] = playerAuthCache[playerID] or {}
        playerAuthCache[playerID].username = autoUser
        playerAuthCache[playerID].beam_id  = beamId
        playerAuthCache[playerID].login_time = os.time()
        print("[BMP Login] 玩家 " .. tostring(name) .. " 自动登录为: " .. autoUser
              .. "  (via=" .. tostring(autoVia) .. ", bound=" .. tostring(autoBound) .. ")")
        MP.SendChatMessage(playerID, " ✨ 欢迎回来，已通过【" .. tostring(autoVia) .. "】自动登录为账号: " .. autoUser)
    end
    
    -- 注意: onlinePlayers 只在 onPlayerConnected 中写入 (完整 entry 表)
    -- 这里只打日志, 避免把字符串写进 onlinePlayers 导致定时器报错
    print("[BMP Login] 玩家加入: " .. tostring(name) .. " (ID: " .. tostring(beamId) .. ")")
end

-- 全局: 等待 HWID 请求的玩家 (playerID → 请求时间戳)
local pendingHwidRequests = {}

function onPlayerConnected(playerID)
    local beamId = getPlayerStableInfo(playerID)
    local name = nil
    local ok, pname = pcall(function() return MP.GetPlayerName(playerID) end)
    if ok and pname then name = pname end
    if not name then name = "Player" .. tostring(playerID) end
    
    -- 写入在线玩家列表 (供网页面板 / Admin GUI 读取)
    local role = getPlayerRole(beamId, playerID)
    local isAuth = isPlayerAuthenticated(playerID)
    local entry = {
        playerID = playerID,
        name = name,
        beam_id = beamId or "",
        role = role,
        is_authenticated = isAuth and true or false,
        bind_account = (playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account) or "",
        join_time = os.time(),
        vehicle_count = playerVehicleCount[playerID] or 0
    }
    onlinePlayers[playerID] = entry
    _saveOnlinePlayers()

    -- 人数以写完之后重新数为准 (处理重复的 onPlayerConnected / 重连)
    local count = countOnlinePlayers()
    
    -- Welcome message to public
    MP.SendChatMessage(-1, " " .. tostring(name) .. " 加入了服务器！当前在线: " .. tostring(count) .. " 人")
    
    -- Send guide privately
    sendWelcomeGuide(playerID, name)
    
    -- ============================================================
    -- 服务器主动请求架构: 向该玩家的客户端 zip 请求 HWID
    -- 客户端 zip (BMPHWID) 收到后从 Bridge.exe 拉机器码回传
    -- 如果 15 秒内没收到回复 → 默认标记为未认证玩家
    -- ============================================================
    pendingHwidRequests[playerID] = os.time()
    local ok2, err2 = pcall(function()
        MP.TriggerClientEvent(playerID, "BMPHWID:RequestHWID", "")
    end)
    if ok2 then
        print("[BMP Login] 已向玩家 #"..playerID.." 请求 HWID (TriggerClientEvent BMPHWID:RequestHWID)")
    else
        print("[BMP Login] TriggerClientEvent 请求 HWID 失败: "..tostring(err2))
    end
    
    print("[BMP Login] 玩家连接: " .. tostring(name) .. " (身份: " .. role .. ")")

    -- 连接时立即检查封禁 (onPlayerAuth 可能漏过)
    local kicked = _checkBanAndKick(playerID)
    if kicked then
        return  -- 已被踢出, 不执行后续逻辑
    end

    -- 兜底: 玩家进服也顺便 poll 一次 Bridge 聊天队列 (之前累积的消息立即出)
    _tryPollChatQueue()
end

function onPlayerDisconnect(playerID)
    local wasOnline = onlinePlayers[playerID] ~= nil
    local name = (onlinePlayers[playerID] and onlinePlayers[playerID].name) or ("Player" .. tostring(playerID))

    -- 投票踢出清理: 如果断开的玩家是投票目标或发起者, 取消投票
    if activeVoteKick then
        if activeVoteKick.target_id == playerID then
            _cancelVoteKick("目标玩家已离线")
        elseif activeVoteKick.initiator_id == playerID then
            _cancelVoteKick("发起者已离线")
        end
    end

    onlinePlayers[playerID] = nil
    playerAuthCache[playerID] = nil
    playerVehicleCount[playerID] = nil
    pendingHwidRequests[playerID] = nil
    playerPositions[playerID] = nil

    _saveOnlinePlayers()
    pcall(function() flushBindings(true) end)   -- 玩家走时把绑定证据落盘

    -- 人数以删完之后重新数为准。
    -- 以前是"先数一遍再 -1": 如果这个玩家根本没进过 onlinePlayers
    -- (进服过程中掉线 / 重复事件), 人数就会被永久少算一个。
    local count = countOnlinePlayers()

    if wasOnline then
        MP.SendChatMessage(-1, " " .. tostring(name) .. " 离开了服务器。当前在线: " .. tostring(count) .. " 人")
    end
    print("[BMP Login] 玩家离开: " .. tostring(name) .. " (在列表中=" .. tostring(wasOnline)
        .. ", 当前在线 " .. tostring(count) .. ")")
end

-- ============================================================
-- Online Players JSON (供 Admin GUI 读取)
-- ============================================================
function _saveOnlinePlayers()
    local list = Array({})
    for pid, data in pairs(onlinePlayers) do
        if type(data) == "table" then
            -- 网页面板需要的附加字段 (onPlayerConnected 里不一定有)
            data.trusted = isPlayerAuthenticated(pid) and true or false
            data.is_admin = isPlayerAdmin(pid) and true or false
            data.beammp_id = getPlayerBeammpId(pid) or ""

            -- 机器码: 直接读 hwid_data (官方账号登录时 beam_id 被 BEAMMP: 前缀占住了)
            data.hwid = getPlayerHwid(pid)

            -- 网站账号 (授信的必要条件), 网页面板会显示
            local webOk, webUser = getWebAccount(pid)
            data.web_verified = webOk and true or false
            data.web_account = webUser or ""

            -- 位置信息: 实时地图已下线, 不再写进 JSON (playerPositions 仍在内存里,
            -- 以后要恢复地图, 把下面几行取消注释即可)
            data.pos = nil

            table.insert(list, data)
        end
    end
    local ok, encoded = pcall(jsonEncode, list)
    if ok and encoded then
        -- 必须走 writeJsonFile: 它会先建目录 + 原子写。
        -- 以前这里直接 io.open(ONLINE_FILE,"w"), 全新服务器上 bmp_login/ 还不存在时
        -- io.open 返回 nil, 代码静默跳过 —— 网页面板就一直是空的, 而且没有任何报错。
        local wrote, err = writeJsonFile(ONLINE_FILE, list)
        if not wrote then
            logWarn("_saveOnlinePlayers 写文件失败: " .. tostring(err or ONLINE_FILE))
        end
    else
        logWarn("_saveOnlinePlayers: jsonEncode failed: " .. tostring(encoded))
    end
end

-- ============================================================
-- 客户端位置上报 (BMPHWID:Pos)  —— 实时地图下线后仅保留在内存
--   payload: "x,y,z,heading,mapname"
--   要恢复地图: 在 _saveOnlinePlayers 里把 data.pos 的赋值加回来,
--   并在 BMP_HTTP_API._players_live 里把 pos 字段放出去。
-- ============================================================
function onHwidPos(playerID, data)
    if not playerID or type(data) ~= "string" or data == "" then return end
    local parts = {}
    for p in data:gmatch("([^,]+)") do parts[#parts + 1] = p end
    if #parts < 3 then return end
    local x = tonumber(parts[1])
    local y = tonumber(parts[2])
    local z = tonumber(parts[3])
    if not x or not y or not z then return end
    playerPositions[playerID] = {
        x = x, y = y, z = z,
        h = tonumber(parts[4]) or 0,
        map = (parts[5] or ""):gsub("%s+$", ""),
        ts = os.time(),
    }
    -- 只留在内存里, 不再写进 online_players.json (地图下线了, 也不该每秒刷盘)
end

-- ============================================================
-- Kick Queue Poller (API 写入 → Lua 读 → 踢人)
-- ============================================================
function _processKickQueue()
    local q = readJsonFile(KICK_QUEUE_FILE)
    if not q or type(q) ~= "table" or #q == 0 then return end
    
    local now = os.time()
    local remaining = {}
    for _, item in ipairs(q) do
        if type(item) ~= "table" then
            -- 无效条目, 跳过
        elseif item.done then
            -- 已处理的记录: 保留 1 小时内的供审计
            if item.done_at and (now - item.done_at) <= 3600 then
                table.insert(remaining, item)
            end
        else
            -- 待处理的踢出请求
            local pid = item.playerID
            local reason = item.reason or "管理员踢出"
            
            -- 踢人
            if pid and onlinePlayers[pid] then
                local pname = (onlinePlayers[pid] and onlinePlayers[pid].name) or "Player" .. tostring(pid)
                MP.SendChatMessage(-1, " [踢出] " .. tostring(pname) .. " 已被管理员踢出 (原因: " .. tostring(reason) .. ")")
                pcall(function() MP.DropPlayer(pid, "您已被踢出: " .. tostring(reason)) end)
                print("[BMP Login] 踢出玩家 #" .. tostring(pid) .. " 原因: " .. tostring(reason))
            end
            
            -- 标记已处理
            item.done = true
            item.done_at = now
            table.insert(remaining, item)
        end
    end
    
    writeJsonFile(KICK_QUEUE_FILE, remaining)
end

-- ============================================================
-- 获取玩家所有可能的 beamId (用于封禁匹配)
-- ============================================================
local function getAllBeamIds(playerID)
    local ids = {}
    local seen = {}
    local function add(bid)
        if bid and type(bid) == "string" and bid ~= "" and not seen[bid] then
            seen[bid] = true
            table.insert(ids, bid)
        end
    end
    
    -- 1) Primary beamId from getPlayerStableInfo
    local beamId = getPlayerStableInfo(playerID)
    add(beamId)
    
    -- 2) From auth cache (onPlayerAuth)
    if playerAuthCache and playerAuthCache[playerID] then
        local ac = playerAuthCache[playerID]
        add(ac.auth_beam_id)
        -- HWID data from client plugin
        if ac.hwid_data and type(ac.hwid_data) == "table" then
            for k, v in pairs(ac.hwid_data) do
                if v and type(v) == "string" and v ~= "" then
                    add("HWID:" .. tostring(k) .. ":" .. v)
                end
            end
        end
    end
    
    -- 3) From MP.GetPlayerIdentifiers
    local ok, result = pcall(function() return MP.GetPlayerIdentifiers(playerID) end)
    if ok and result and type(result) == "table" then
        if result.beammp and type(result.beammp) == "string" then
            add("BEAMMP:" .. result.beammp)
        end
        if result.ip and type(result.ip) == "string" then
            add("IP:" .. result.ip)
        end
        if result.hwid and type(result.hwid) == "string" then
            add("HWID:srv:" .. result.hwid)
        end
        if result.discord and type(result.discord) == "string" then
            add("DISCORD:" .. result.discord)
        end
        -- Array fallback
        for k, v in pairs(result) do
            if type(k) == "number" and type(v) == "string" then
                local prefix, val = v:match("^([^:]+):(.+)$")
                if prefix == "beammp" then add("BEAMMP:" .. val)
                elseif prefix == "hwid" then add("HWID:srv:" .. val)
                elseif prefix == "ip" then add("IP:" .. val)
                end
            end
        end
    end
    
    -- 4) Try MP.GetPlayerHWID
    local ok2, phwid = pcall(function() return MP.GetPlayerHWID(playerID) end)
    if ok2 and phwid and type(phwid) == "string" and phwid ~= "" then
        add("HWID:srv:" .. phwid)
    end
    
    -- 5) Player name for NAME: matching
    local ok3, pname = pcall(function() return MP.GetPlayerName(playerID) end)
    if ok3 and pname and type(pname) == "string" and pname ~= "" then
        add("NAME:" .. pname)
    end
    
    return ids
end

-- ============================================================
-- 检查某个 beamId 是否匹配封禁值
-- ============================================================
local function beamIdMatches(beamId, ban_val, ban_type)
    if not beamId or not ban_val or ban_val == "" then return false end
    ban_val = tostring(ban_val)
    
    -- Exact match
    if beamId == ban_val then return true end
    
    -- If ban_val already has a prefix (HWID:, IP:, etc.), check exact or prefix
    if ban_val:match("^HWID:") or ban_val:match("^IP:") or ban_val:match("^BEAMMP:") or ban_val:match("^NAME:") then
        if beamId == ban_val then return true end
        if beamId:sub(1, #ban_val) == ban_val then return true end
        return false
    end
    
    -- No prefix in ban_val: try various prefixes
    -- HWID match: beamId contains the value
    if beamId:match("^HWID:") then
        -- Extract the actual HWID from beamId (after the last colon)
        local hwid_part = beamId:match("^HWID:[^:]+:(.+)$") or beamId:match("^HWID:(.+)$")
        if hwid_part and (hwid_part == ban_val or hwid_part:lower() == ban_val:lower()) then
            return true
        end
        -- Also check if beamId just contains the value
        if beamId:find(ban_val, 1, true) then return true end
    end
    
    -- IP match: beamId is IP:<value>
    if beamId:match("^IP:") then
        local ip_part = beamId:sub(4)
        if ip_part == ban_val then return true end
    end
    
    -- NAME match: beamId is NAME:<value>
    if beamId:match("^NAME:") then
        local name_part = beamId:sub(6)
        if name_part == ban_val or name_part:lower() == ban_val:lower() then
            return true
        end
    end
    
    -- Generic: beamId contains ban_val
    if beamId:find(ban_val, 1, true) then return true end
    
    return false
end

-- ============================================================
-- Ban Check & Kick (实时踢人 + 连接时检查)
-- ============================================================
function _checkBanAndKick(playerID)
    if not playerID then return false end
    
    local beamIds = getAllBeamIds(playerID)
    local playerName = "Player" .. tostring(playerID)
    local ok, pn = pcall(function() return MP.GetPlayerName(playerID) end)
    if ok and pn and type(pn) == "string" and pn ~= "" then playerName = pn end
    
    local now = os.time()
    local bound_account = nil
    if playerAuthCache and playerAuthCache[playerID] and playerAuthCache[playerID].bound_account then
        bound_account = playerAuthCache[playerID].bound_account
    end
    
    -- 检查设备封禁 (HWID / IP / BEAMMP)
    for _, entry in ipairs(banlist.devices or {}) do
        if type(entry) == "table" then
            local exp = entry.expires_at or 0
            if exp == 0 or exp > now then
                local val = entry.value or entry.device_id or entry.ip or entry.hwid or entry.beammp or ""
                if val ~= "" then
                    for _, bid in ipairs(beamIds) do
                        if beamIdMatches(bid, val, "device") then
                            local reason = entry.reason or "违反服务器规则"
                            MP.SendChatMessage(-1, " [封禁] " .. playerName .. " 已被踢出 (设备封禁, 原因: " .. reason .. ")")
                            pcall(function() MP.DropPlayer(playerID, "您已被封禁: " .. reason) end)
                            print("[BMP Login] 踢出玩家 " .. playerName .. " (设备封禁, 匹配: " .. tostring(bid) .. " vs " .. tostring(val) .. ")")
                            return true
                        end
                    end
                end
            end
        end
    end
    
    -- 检查账号封禁 (匹配 bound_account 或 NAME: beamId)
    for _, entry in ipairs(banlist.accounts or {}) do
        if type(entry) == "table" then
            local exp = entry.expires_at or 0
            if exp == 0 or exp > now then
                local val = entry.value or entry.account or ""
                if val ~= "" then
                    -- 1) Check bound_account
                    if bound_account and bound_account == val then
                        local reason = entry.reason or "违反服务器规则"
                        MP.SendChatMessage(-1, " [封禁] " .. playerName .. " 已被踢出 (账号封禁: " .. val .. ", 原因: " .. reason .. ")")
                        pcall(function() MP.DropPlayer(playerID, "您已被封禁 (账号: " .. val .. "), 原因: " .. reason) end)
                        print("[BMP Login] 踢出玩家 " .. playerName .. " (账号封禁: " .. val .. ")")
                        return true
                    end
                    -- 2) Check NAME: beamId
                    for _, bid in ipairs(beamIds) do
                        if bid:match("^NAME:") and beamIdMatches(bid, val, "account") then
                            local reason = entry.reason or "违反服务器规则"
                            MP.SendChatMessage(-1, " [封禁] " .. playerName .. " 已被踢出 (账号封禁: " .. val .. ", 原因: " .. reason .. ")")
                            pcall(function() MP.DropPlayer(playerID, "您已被封禁 (账号: " .. val .. "), 原因: " .. reason) end)
                            print("[BMP Login] 踢出玩家 " .. playerName .. " (账号封禁: " .. val .. ")")
                            return true
                        end
                    end
                end
            end
        end
    end
    
    return false
end

-- 定时轮询 banlist.json 变化 (每 3 秒) + 检查所有在线玩家
local BAN_POLL_EVENT = "BMP_BAN_POLL"

function processBanPollGlobal(eventName)
    local now = os.time()
    
    -- 1) Reload banlist
    local file = io.open(BANLIST_FILE, "r")
    if file then
        file:close()
        local newBanlist = readJsonFile(BANLIST_FILE)
        if newBanlist then
            banlist = newBanlist
            if not banlist.accounts then banlist.accounts = Array({}) end
            if not banlist.devices then banlist.devices = Array({}) end
        end
    end
    
    -- 2) Check all online players for bans
    local changed = false
    for pid, _ in pairs(onlinePlayers or {}) do
        if _checkBanAndKick(pid) then changed = true end
    end
    
    -- 3) Process kick queue (API → Lua → 踢人)
    _processKickQueue()
    
    -- 3.5) Vote kick timeout check
    _checkVoteKickTimeout()
    
    -- 3.6) Process Bridge vote queue (vote_queue.json)
    _processVoteQueue()
    
    -- 4) Refresh online players JSON (update roles/vehicle counts)
    for pid, data in pairs(onlinePlayers) do
        local beamId = getPlayerStableInfo(pid)
        data.beam_id = beamId or data.beam_id
        data.role = getPlayerRole(beamId, pid)
        data.is_authenticated = isPlayerAuthenticated(pid) and true or false
        data.vehicle_count = playerVehicleCount[pid] or 0
        if playerAuthCache and playerAuthCache[pid] and playerAuthCache[pid].bound_account then
            data.bind_account = playerAuthCache[pid].bound_account
        end
    end
    _saveOnlinePlayers()
    return 0
end

-- 顶层注册必须 pcall 包住: 老版本服务端若没有 MP.CreateEventTimer,
-- 未保护的调用会让整个插件 chunk 加载失败 (插件完全不起作用)
local ok_ban_timer = pcall(function()
    MP.RegisterEvent(BAN_POLL_EVENT, "processBanPollGlobal")
    MP.CreateEventTimer(BAN_POLL_EVENT, 3000)  -- 每 3 秒
end)
if not ok_ban_timer then
    logWarn("BAN_POLL 定时器注册失败, 封禁轮询将只在其它事件触发时执行")
end

-- ============================================================
-- Vehicle Events (minimal)
-- ============================================================
function onVehicleSpawn(playerID, vehicleID, vehicleData)
    -- 车辆生成事件 (3 参数: playerID, vehicleID, vehicleData)
    -- 按官方 BeamMP 文档, return 1 可取消事件 (车辆不会被生成)

    local beamId = getPlayerStableInfo(playerID)
    local role = getPlayerRole(beamId, playerID)
    local isAuth = isPlayerAuthenticated(playerID)
    local authLabel = getPlayerAuthLabel(playerID)
    local limit = getPlayerVehicleLimit(playerID)

    -- 未授信玩家不再"一律禁止", 而是按 VEHICLE_LIMITS.unauthenticated (=1) 限制:
    -- 让没认证的人也能开一辆车, 想多刷就得去认证。
    -- (以前这里直接 return 1, 导致下面的数量限制对游客根本没生效)

    -- 当前已生成车辆数 +1 (即将生成)
    playerVehicleCount[playerID] = (playerVehicleCount[playerID] or 0) + 1
    local count = playerVehicleCount[playerID]

    -- 超过限制: 取消生成
    if count > limit then
        playerVehicleCount[playerID] = count - 1  -- 回退计数 (车辆被取消)
        local msg
        if authLabel ~= "认证" then
            -- 未授信: 说清楚"上限是 1, 认证后 5", 而不是只说已达上限
            msg = " 未授信用户只能生成 " .. limit .. " 辆车. "
            if ENABLE_CHAT_LOGIN then
                msg = msg .. "登录账号后上限 " .. VEHICLE_LIMITS.authenticated .. " 辆"
            else
                local missing = {}
                if not hasStableHwid(playerID) then missing[#missing + 1] = "启动 BMPHWID_Bridge.exe 上报机器码" end
                if not getWebAccount(playerID) then
                    missing[#missing + 1] = "在服务器网站注册 (http://<服务器IP>:12124/account)"
                end
                if #missing == 0 then
                    msg = msg .. "授信后上限 " .. VEHICLE_LIMITS.authenticated .. " 辆"
                else
                    msg = msg .. "授信后上限 " .. VEHICLE_LIMITS.authenticated
                        .. " 辆. 还需要: " .. table.concat(missing, " + ")
                end
            end
        else
            msg = " 您是[" .. authLabel .. "用户], 车辆上限 " .. limit .. " 辆, 已达上限, 生成被取消"
        end
        MP.SendChatMessage(playerID, msg)
        logDebug("onVehicleSpawn 取消 #" .. tostring(playerID) .. " 现有 " .. (count - 1) .. "/" .. limit)
        return 1  -- 取消事件 (车辆不会生成)
    end

    -- 未认证玩家第一次生成: 提示一次, 让他知道有更高级别
    if authLabel ~= "认证" and count == 1 then
        MP.SendChatMessage(playerID, " 你是未授信用户, 只能生成 " .. limit
            .. " 辆车. 完成认证后可生成 " .. VEHICLE_LIMITS.authenticated .. " 辆 (输入 /whoami 查看)")
    end

    -- 在限制内: 通过, 记日志
    logDebug("onVehicleSpawn OK #" .. tostring(playerID) .. " (" .. authLabel .. "用户) " .. count .. "/" .. limit .. " vid=" .. tostring(vehicleID))
end

-- 车辆被编辑 (玩家修改配置)
function onVehicleEdited(playerID, vehicleID, vehicleData)
    -- 不影响车辆数量, 仅记日志
    logDebug("onVehicleEdited #" .. tostring(playerID) .. " vid=" .. tostring(vehicleID))
end

-- 车辆被重置 (玩家按 R 重置车辆位置)
function onVehicleReset(playerID, vehicleID, vehicleData)
    -- 不影响车辆数量, 仅记日志
    logDebug("onVehicleReset #" .. tostring(playerID) .. " vid=" .. tostring(vehicleID))
end

-- 车辆被删除 (玩家按 Ctrl+Delete 或离开)
function onVehicleDeleted(playerID, vehicleID)
    -- 车辆数 -1 (不超过 0)
    if playerVehicleCount[playerID] and playerVehicleCount[playerID] > 0 then
        playerVehicleCount[playerID] = playerVehicleCount[playerID] - 1
        logDebug("onVehicleDeleted #" .. tostring(playerID) .. " vid=" .. tostring(vehicleID) .. " 剩余 " .. playerVehicleCount[playerID])
    end
end

-- ============================================================
-- BMPHWID Client Plugin: Base64 Decoder
-- ============================================================
local B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local b64_lookup = {}
for i = 1, #B64_CHARS do
    b64_lookup[B64_CHARS:sub(i, i)] = i - 1
end

local function b64Decode(s)
    if type(s) ~= "string" then return "" end
    -- HEX fallback: bmpHwidProbe uses "HEX:xxxxxxxx" if base64 unavailable
    if s:sub(1, 4) == "HEX:" then
        local hex = s:sub(5)
        local out = {}
        for i = 1, #hex, 2 do
            local byte = tonumber(hex:sub(i, i + 1), 16)
            if byte then out[#out + 1] = string.char(byte) end
        end
        return table.concat(out)
    end
    s = s:gsub("%s", ""):gsub("=", "")
    local out = {}
    local acc = 0
    local bits = 0
    for i = 1, #s do
        local c = s:sub(i, i)
        local val = b64_lookup[c]
        if val then
            -- 纯算术实现: BeamMP 的 Lua 5.3 不一定带 bit32 兼容库,
            -- 直接调 bit32.bor 会 "attempt to index a nil value (global 'bit32')"
            acc = acc * 64 + val
            bits = bits + 6
            if bits >= 8 then
                bits = bits - 8
                local divisor = 2 ^ bits
                local byte = math.floor(acc / divisor) % 256
                acc = acc % divisor
                out[#out + 1] = string.char(byte)
            end
        end
    end
    return table.concat(out)
end

-- ============================================================
-- BMPHWID Client Plugin: Chunked Payload Assembly + Parser
-- ============================================================
-- Per-player chunk accumulator (for multi-chunk replies)
local hwidChunkAccum = {}  -- playerID -> { chunks = {...}, expected = n, playerName = x }

local function findPlayerIdByName(playerName)
    for id, info in pairs(onlinePlayers) do
        local n = (type(info) == "table" and info.name) or info
        if n == playerName then return id end
    end
    -- Fallback: scan by MP.GetPlayerName
    local maxId = 64
    for id = 0, maxId do
        local ok, n = pcall(function() return MP.GetPlayerName(id) end)
        if ok and n == playerName then return id end
    end
    return nil
end

local function parseHwidPayload(payload)
    -- payload: "v1|cnt=3|me=<b64>|uuid=<b64>|settings.BMPLoginHWID(uuid)=<b64>|..."

    -- 兼容旧版客户端 zip (<= v2.6.0): "v1|cnt=1|cur=0|<一整段 b64>",
    -- 解开后是 "key<TAB>value" 换行分隔的文本。
    -- 旧服务器逻辑会把 "cur=0" 当字段、把整段 b64 直接丢掉, 于是这些玩家的
    -- HWID 从来没上报成功过。这里做个向后兼容。
    do
        local legacy = payload:match("^v%d|cnt=[^|]*|cur=[^|]*|(.+)$")
        if legacy and #legacy > 16 then
            local decoded = b64Decode(legacy)
            local legacyResult = {}
            local n = 0
            for line in decoded:gmatch("[^\r\n]+") do
                local k, v = line:match("^([^\t]+)\t(.*)$")
                if k and v and v ~= "" then
                    legacyResult[string.trim(k)] = v
                    n = n + 1
                end
            end
            if n > 0 then
                legacyResult._version = "legacy-2.6"
                return legacyResult
            end
        end
    end

    local result = {}
    local field_num = 0
    for part in string.gmatch(payload, "([^|]+)") do
        field_num = field_num + 1
        if field_num == 1 then
            result._version = part
        elseif field_num == 2 then
            result._count = part:match("=(.+)$") or part
        else
            local k, b64v = part:match("^([^=]+)=(.*)$")
            if k and b64v then
                local v = b64Decode(b64v)
                result[k] = v
            end
        end
    end
    return result
end

local function processHwidPayload(playerID, playerName, payload)
    if not payload or payload == "" then return end
    
    print("[BMP Login] [HWID] 收到来自 " .. tostring(playerName) .. "(#" .. playerID .. ") 的原始 payload: " .. tostring(payload))
    
    local hwidData = parseHwidPayload(payload)
    if not next(hwidData) then
        print("[BMP Login] [HWID] payload 解析后为空，跳过")
        return
    end
    
    print("[BMP Login] [HWID] 解析完成, 字段数: " .. tostring(#hwidData or 0) .. " / keys: " .. jsonEncode(hwidData))
    
    -- Store
    local stableId, ip, _ = getPlayerStableInfo(playerID)
    local existing = playerAuthCache[playerID] or {}
    existing.hwid_data = hwidData
    existing.last_hwid_update = os.time()
    playerAuthCache[playerID] = existing
    
    -- Pick the most stable HWID key for auto-login binding
    local bestHwid = nil
    local bestHwidValue = nil      -- 纯净的机器码值 (不带 "key:" 前缀), 网页端比对用
    local priority_keys = {
        "settings.BMPLoginHWID(uuid)",   -- 1) Our persistent UUID (best)
        "uuid",                           -- 2) Any other UUID
        "beammp_launcher_hwid",           -- 3) BeamMP launcher HWID
        "FS:getUserPath()",              -- 4) User home path
        "core_env_fingerprint",           -- 5) CPU/GFX fingerprint
        "beammp_env_playerid"             -- 6) BeamMP env player id
    }
    for _, k in ipairs(priority_keys) do
        if hwidData[k] and hwidData[k] ~= "" then
            bestHwid = k .. ":" .. hwidData[k]
            bestHwidValue = hwidData[k]
            break
        end
    end
    
    -- Also check "me" field for player name verification
    local me = hwidData["me"]
    if me and me ~= "" and me ~= playerName then
        print("[BMP Login] [HWID] 客户端上报玩家名 (" .. tostring(me) .. ") 与服务器记录 (" .. tostring(playerName) .. ") 不一致")
    end
    
    if bestHwid then
        local newBeamId = "HWID:" .. bestHwid
        print("[BMP Login] [HWID] 玩家 " .. tostring(playerName) .. " 获得稳定 HWID: " .. newBeamId)
        -- Update guestAccountMap for auto-login
        if existing and existing.username then
            guestAccountMap[newBeamId] = {
                account = existing.username,
                last_seen = os.time()
            }
            saveGuestMap()
            print("[BMP Login] [HWID] 已绑定账号 " .. existing.username .. " ↔ " .. newBeamId)
        end
        -- Send confirmation to player
        pcall(function()
            MP.SendChatMessage(playerID, " HWID已接收: 绑定 " .. bestHwid:sub(1, 32) .. "...")
        end)

        -- 记录「机器码 ↔ BeamMP 官方账号」的绑定证据 (网站注册时用它验证本人)
        -- 传纯净的 UUID, 不是 "key:value" 串 —— 网页端拿到的 /hwid 就是纯净值
        recordBinding(playerID, bestHwidValue, playerName)
    else
        print("[BMP Login] [HWID] 解析后的字段中没有可用 HWID")
        pcall(function()
            MP.SendChatMessage(playerID, " HWID已接收，但没有稳定硬件码")
        end)
    end
end

-- Handle BMPHWID:HWIDReply (client TriggerServerEvent)
-- BeamMP custom event signatures vary; try common forms
local function handleHwidReplyFromClient(playerID_or_name, secondArg, thirdArg)
    local playerID = nil
    local frame = nil
    local playerName = nil
    
    -- Try to figure out which arg is what
    if type(playerID_or_name) == "number" then
        playerID = playerID_or_name
        frame = secondArg
    elseif type(secondArg) == "number" then
        playerID = secondArg
        frame = playerID_or_name
    end
    if type(playerID_or_name) == "string" then playerName = playerID_or_name end
    if type(thirdArg) == "string" then playerName = thirdArg end
    if type(secondArg) == "string" and not frame then frame = secondArg end
    
    -- Resolve playerID from name if needed
    if not playerID and playerName then
        playerID = findPlayerIdByName(playerName)
    end
    -- Resolve name from ID if needed
    if playerID and not playerName then
        local ok, n = pcall(function() return MP.GetPlayerName(playerID) end)
        if ok and n then playerName = n end
    end
    
    if not playerID then
        print("[BMP Login] [HWID] 收到事件但无法定位玩家 (args: " .. tostring(playerID_or_name) .. ", " .. tostring(secondArg) .. ", " .. tostring(thirdArg) .. ")")
        return
    end
    playerName = playerName or ("Player" .. playerID)
    
    if not frame or type(frame) ~= "string" then
        print("[BMP Login] [HWID] frame 不是字符串, 跳过")
        return
    end
    
    -- Frame format (from bmpHwidProbe.lua):
    --   Single-chunk (F): "F@meB64|payload"
    --   Multi-chunk start (S): "S@meB64|001/003|chunk"
    --   Multi-chunk mid (M):   "M@meB64|002/003|chunk"
    --   Multi-chunk end (E):   "E@meB64|003/003|chunk"
    
    print("[BMP Login] [HWID] 收到 client-event frame: " .. frame:sub(1, 80) .. (#frame > 80 and "..." or ""))
    
    local header, rest = frame:match("^([^|]+)|(.*)$")
    if not header then header = frame; rest = "" end
    
    -- Parse header: "F@meB64" or "S@meB64" etc.
    local tag, meB64 = header:match("^([FSMCE])@(.*)$")
    if not tag then
        -- Fallback: no @ (old format)
        tag = "F"
        meB64 = ""
    end
    
    if tag == "F" then
        -- Single chunk: rest is the payload
        processHwidPayload(playerID, playerName, rest)
        hwidChunkAccum[playerID] = nil
    else
        -- Multi-chunk: rest = "001/003|chunk..."
        local numStr, chunk = rest:match("^(%d+/%d+)|(.*)$")
        if not numStr then chunk = rest end
        local cur, total = nil, nil
        if numStr then
            local cStr, tStr = numStr:match("^(%d+)/(%d+)$")
            cur = tonumber(cStr)
            total = tonumber(tStr)
        end
        local acc = hwidChunkAccum[playerID] or { chunks = {}, expected = total, playerName = playerName }
        acc.playerName = playerName
        acc.expected = acc.expected or total
        acc.chunks[cur or (#acc.chunks + 1)] = chunk or ""
        
        if tag == "E" or (cur and total and cur >= total) then
            -- Assemble
            local assembled = {}
            for i = 1, (total or #acc.chunks) do
                if acc.chunks[i] then assembled[#assembled + 1] = acc.chunks[i] end
            end
            local fullPayload = table.concat(assembled)
            processHwidPayload(playerID, playerName, fullPayload)
            hwidChunkAccum[playerID] = nil
        else
            hwidChunkAccum[playerID] = acc
        end
    end
end

-- Public event handlers
function onHwidReply(arg1, arg2, arg3, arg4)
    print("[BMP Login] [HWID] BMPHWID:HWIDReply fired (args: " .. tostring(arg1) .. ", " .. tostring(arg2) .. ", " .. tostring(arg3) .. ", " .. tostring(arg4) .. ")")
    -- 清除 pending 请求标记 (不管哪个 arg 是 playerID)
    for _, candidate in ipairs({arg1, arg2, arg3, arg4}) do
        if type(candidate) == "number" and pendingHwidRequests[candidate] then
            pendingHwidRequests[candidate] = nil
            print("[BMP Login] [HWID] 玩家 #"..candidate.." 的 HWID 请求已响应")
            break
        end
    end
    local ok, err = pcall(function()
        handleHwidReplyFromClient(arg1, arg2, arg3)
    end)
    if not ok then
        print("[BMP Login] [HWID] ERROR processing HWIDReply: " .. tostring(err))
    end
end

function onHwidVersion(arg1, arg2, arg3, arg4)
    print("[BMP Login] [HWID] 客户端版本: " .. tostring(arg1) .. " | " .. tostring(arg2) .. " | " .. tostring(arg3))
end

-- ============================================================
-- /bmpid chat command: Process payload (chat fallback path)
--  Frame format:
--    Single: "F|payload"
--    Multi:  "001/003|chunk"
--  Plus old k=v format still accepted: /bmpid uuid=x&client_name=BMPHWID
-- ============================================================
function handleBmpidCommand(playerID, playerName, rawPayload)
    if not rawPayload or rawPayload == "" then
        pcall(function() MP.SendChatMessage(playerID, " 用法: /bmpid <payload>") end)
        return
    end
    
    -- First: try new format (single F| or multi 001/003|)
    local header2, rest2 = rawPayload:match("^([^|]+)|(.*)$")
    if header2 then
        -- Multi-chunk chat format: "001/003|chunk"
        local cStr, tStr = header2:match("^(%d+)/(%d+)$")
        if cStr and tStr then
            -- Multi chunk
            local cur = tonumber(cStr)
            local total = tonumber(tStr)
            local acc = hwidChunkAccum[playerID] or { chunks = {}, expected = total, playerName = playerName, via = "chat" }
            acc.expected = acc.expected or total
            acc.chunks[cur] = rest2 or ""
            if cur and total and cur >= total then
                local assembled = {}
                for i = 1, total do
                    if acc.chunks[i] then assembled[#assembled + 1] = acc.chunks[i] end
                end
                processHwidPayload(playerID, playerName, table.concat(assembled))
                hwidChunkAccum[playerID] = nil
            else
                hwidChunkAccum[playerID] = acc
            end
            return
        end
        -- Single chat format: "F|payload"
        if header2 == "F" or header2 == "f" then
            processHwidPayload(playerID, playerName, rest2 or "")
            return
        end
    end
    
    -- Fallback: old k=v&k=v format (manual /bmpid uuid=...)
    local hwidData = {}
    for pair in string.gmatch(rawPayload, "([^&]+)") do
        local k, v = pair:match("^([^=]+)=(.*)$")
        if k and v then
            hwidData[string.trim(k)] = string.trim(v)
        end
    end
    
    -- Fallback 1: k=v&k=v 格式 (老格式 /bmpid uuid=xxx&foo=bar)
    if next(hwidData) then
        local stableId, ip, name = getPlayerStableInfo(playerID)
        local existing = playerAuthCache[playerID] or {}
        existing.hwid_data = hwidData
        existing.last_hwid_update = os.time()
        playerAuthCache[playerID] = existing
        if hwidData.uuid then
            local newBeamId = "HWID:settings.BMPLoginHWID(uuid):" .. hwidData.uuid
            print("[BMP Login] [HWID] /bmpid 手动上报: " .. tostring(playerName) .. " → " .. newBeamId)
            pcall(function() MP.SendChatMessage(playerID, " HWID已更新: " .. hwidData.uuid) end)
            if existing and existing.username then
                guestAccountMap[newBeamId] = { account = existing.username, last_seen = os.time() }
                saveGuestMap()
            end
        end
        print("[BMP Login] [HWID] /bmpid k=v 数据: " .. jsonEncode(hwidData))
        return
    end

    -- Fallback 2: 裸 UUID 直接接受 (Bridge 用户经常只发 UUID 因为它最稳定且命令短)
    --   接受格式: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 字符)
    --             或纯 hex 32 字符
    --             或 "裸HWID:任意字符串" (作为兜底稳定 ID 直接绑定)
    local trimmed = (rawPayload or ""):gsub("^%s+", ""):gsub("%s+$", "")
    -- 36-char UUID
    local uuid_m = trimmed:match("^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
    if uuid_m then
        local hwidData2 = { ["settings.BMPLoginHWID(uuid)"] = string.lower(uuid_m) }
        local stableId, ip, name = getPlayerStableInfo(playerID)
        local existing = playerAuthCache[playerID] or {}
        existing.hwid_data = hwidData2
        existing.last_hwid_update = os.time()
        playerAuthCache[playerID] = existing
        local newBeamId = "HWID:settings.BMPLoginHWID(uuid):" .. string.lower(uuid_m)
        print("[BMP Login] [HWID] /bmpid 裸UUID上报: " .. tostring(playerName) .. " → " .. newBeamId)
        pcall(function() MP.SendChatMessage(playerID, " HWID已接收，绑定稳定UUID: " .. string.lower(uuid_m)) end)
        if existing and existing.username then
            guestAccountMap[newBeamId] = { account = existing.username, last_seen = os.time() }
            saveGuestMap()
        end
        return
    end
    -- 任意非空字符串(>=8 字符 无空格) 作为兜底稳定 ID
    if #trimmed >= 8 and not trimmed:match("%s") then
        local hwidData2 = { ["bmpid_raw"] = trimmed }
        local existing = playerAuthCache[playerID] or {}
        existing.hwid_data = hwidData2
        existing.last_hwid_update = os.time()
        playerAuthCache[playerID] = existing
        local newBeamId = "HWID:bmpid_raw:" .. trimmed
        print("[BMP Login] [HWID] /bmpid 原始串上报: " .. tostring(playerName) .. " → " .. newBeamId)
        pcall(function() MP.SendChatMessage(playerID, " HWID已接收，绑定原始稳定ID: " .. trimmed) end)
        return
    end
    pcall(function() MP.SendChatMessage(playerID, " HWID数据格式错误，支持: /bmpid F|payload | /bmpid 001/003|chunk | /bmpid <UUID> | /bmpid k=v&k=v") end)
end

-- ============================================================
-- End of Plugin
-- ============================================================
