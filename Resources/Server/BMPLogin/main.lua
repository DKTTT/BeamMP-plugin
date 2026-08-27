-- ============================================================
-- BMP Login Plugin v2.4.0 (完整管理员版)
-- BeamMP Server Player Account Management
-- ============================================================

local PLUGIN_NAME = "BMP Login"
local PLUGIN_VERSION = "2.4.0"
local DATA_DIR = "bmp_login"
local ACCOUNTS_FILE = DATA_DIR .. "/accounts.json"
local BANLIST_FILE = DATA_DIR .. "/banlist.json"
local GUEST_MAP_FILE = DATA_DIR .. "/guest_map.json"
local ONLINE_FILE = DATA_DIR .. "/online_players.json"
local KICK_QUEUE_FILE = DATA_DIR .. "/kick_queue.json"

local accounts = {}
local banlist = {}
local guestAccountMap = {}
local onlinePlayers = {}
local playerAuthCache = {}

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

local function logDebug(msg)
    if not msg then return end
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
        
        local arr = {}
        for i = 1, #v do arr[i] = true end
        local parts = {}
        for k, val in pairs(v) do
            if not arr[k] then
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
    for i = 1, 64 do
        local idx = ((i - 1) % pwdLen) + 1
        local c = password:byte(idx)
        local s = saltBytes[((i - 1) % 8) + 1]
        hash = hash .. string.char((c + s + i) % 95 + 32)
    end
    
    return salt .. ":" .. hash
end

local function verifyPassword(password, storedHash)
    if not storedHash or storedHash == "" then
        print("[BMP Login] verifyPassword: storedHash is empty!")
        return false
    end
    
    local salt = storedHash:match("^([^:]+):")
    local hash = storedHash:match(":(.+)$")
    if not salt or not hash then
        print("[BMP Login] verifyPassword: failed to parse salt:hash from stored")
        return false
    end
    
    print("[BMP Login] verifyPassword: salt=" .. salt .. " (#" .. #salt .. ") hash_len=" .. #hash)
    print("[BMP Login] verifyPassword: hash=" .. hash)
    
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
    
    print("[BMP Login] verifyPassword: computed=" .. computed)
    print("[BMP Login] verifyPassword: match=" .. tostring(computed == hash))
    
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

local function writeJsonFile(filePath, data)
    -- Ensure directory exists
    local dir = filePath:match("^(.+)/[^/]+$")
    if dir and dir ~= "" then
        pcall(function()
            os.execute('if not exist "' .. dir .. '" mkdir "' .. dir .. '"')
        end)
    end
    local file = io.open(filePath, "w")
    if not file then return false end
    file:write(jsonEncode(data))
    file:close()
    return true
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
    local now = os.time()
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
        print("[BMP Login] 已修复 " .. repaired .. " 个账号字段结构 (对象 -> 数组)，立即写回文件")
        -- 直接调 writeJsonFile (此时 saveAccounts 可能还没定义, 避免 nil 错误)
        local ok, err = pcall(writeJsonFile, ACCOUNTS_FILE, accounts)
        print("[BMP Login] writeJsonFile 结果 ok=" .. tostring(ok) .. " err=" .. tostring(err or "nil"))
        -- 再 DEBUG：验证写回后 DRIFTKING 两个字段在内存里确实是数组
        if accounts["DRIFTKING"] then
            local dk = accounts["DRIFTKING"]
            local mt_bi = getmetatable(dk.bind_beam_ids)
            local mt_lr = getmetatable(dk.login_records)
            print("[BMP Login] DRIFTKING 内存验证: bind_beam_ids(#="..#dk.bind_beam_ids..") mt.tag="..tostring(mt_bi and mt_bi.__tag_json_array).." / login_records(#="..#dk.login_records..") mt.tag="..tostring(mt_lr and mt_lr.__tag_json_array))
            -- 单独 jsonEncode DRIFTKING 看输出
            print("[BMP Login] DRIFTKING encode: " .. jsonEncode(dk))
        end
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
-- Player Role
-- ============================================================
local function getPlayerRole(beamId, playerID)
    if not beamId then return "游客" end
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
--   优先使用 accounts.json 中的 is_admin 字段
--   回退使用硬编码的 ADMIN_WHITELIST
-- ============================================================
local ADMIN_WHITELIST = {
    -- 在此处添加管理员账号 (小写), 例如: "driftking" = true
}

local function isPlayerAdmin(playerID)
    if not playerID then return false end
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
    authenticated = 5,  -- 认证用户
    unauthenticated = 0, -- 未认证用户 (禁止刷车)
}

local function isPlayerAuthenticated(playerID)
    if not playerID then return false end
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
    local beam_id_ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    if isPlayerAuthenticated(playerID) then
        return VEHICLE_LIMITS.authenticated
    end
    return VEHICLE_LIMITS.unauthenticated
end

local function getPlayerAuthLabel(playerID)
    if not playerID then return "未认证" end
    local beam_id_ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
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
local function getPlayerStableInfo(playerID)
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
    
    -- Try MP.GetPlayerHWID if exists
    if not beamId then
        local ok3, phwid = pcall(function() return MP.GetPlayerHWID(playerID) end)
        if ok3 and phwid and type(phwid) == "string" and phwid ~= "" then
            beamId = "HWID:srv:" .. phwid
        end
        local ok4, pname2 = pcall(function() return MP.GetPlayerHWID(name or pname) end)
        if ok4 and pname2 and type(pname2) == "string" and pname2 ~= "" and not beamId then
            beamId = "HWID:srv:" .. pname2
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
    
    accounts[username] = {
        username = username,
        password_hash = hashPassword(password),
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
    -- DEBUG: 打印 hash 前 20 字符 + 长度
    print("[BMP Login] DEBUG verify: user=" .. username .. " hash_len=" .. #storedHash .. " hash_prefix=" .. storedHash:sub(1, 20))
    
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

function _handleChat(playerID, playerName, message)
    
    local beamId = getPlayerStableInfo(playerID)
    local role = getPlayerRole(beamId, playerID)
    local prefix = "" .. playerName .. ": "
    
    -- Guest reminder
    if role == "游客" then
        if message:sub(1, 1) ~= "/" then
            MP.SendChatMessage(playerID, " 提示: 你尚未登录。使用 /help 查看可用命令")
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
            MP.SendChatMessage(playerID, " /register <账号> <密码> - 注册新账号")
            MP.SendChatMessage(playerID, " /login <账号> <密码> - 登录账号")
            MP.SendChatMessage(playerID, " /logout - 退出登录")
            MP.SendChatMessage(playerID, " /whoami - 查看登录状态")
            MP.SendChatMessage(playerID, " /vehiclelimit - 查看车辆上限")
            MP.SendChatMessage(playerID, " /bmpid <payload> - 客户端HWID回传")
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
            local uname, pwd = args:match("^(%S+)%s+(%S+)$")
            if uname and pwd then
                registerAccount(playerID, uname, pwd)
            else
                MP.SendChatMessage(playerID, " 用法: /register <账号> <密码>")
            end

        elseif cmd == "/login" then
            local uname, pwd = args:match("^(%S+)%s+(%S+)$")
            if uname and pwd then
                loginAccount(playerID, uname, pwd)
            else
                MP.SendChatMessage(playerID, " 用法: /login <账号> <密码>")
            end

        elseif cmd == "/logout" then
            logoutAccount(playerID)

        elseif cmd == "/whoami" then
            local beamId = getPlayerStableInfo(playerID)
            local role = getPlayerRole(beamId, playerID)
            local limit = getPlayerVehicleLimit(playerID)
            local authLabel = isPlayerAuthenticated(playerID) and "已认证" or "未认证"
            MP.SendChatMessage(playerID, " 身份: " .. role .. "  认证状态: " .. authLabel .. "  车辆上限: " .. limit .. " 辆")
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

        elseif cmd == "/addadmin" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            local target = args:match("^(%S+)")
            if not target then
                MP.SendChatMessage(playerID, " 用法: /addadmin <账号>")
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
            if found_key then
                accounts[found_key].is_admin = true
                saveAccounts()
                MP.SendChatMessage(playerID, " 账号 " .. found_key .. " 已设为管理员")
                print("[BMP Login] 管理员 " .. tostring(playerName) .. " 提升 " .. found_key .. " 为管理员")
            else
                MP.SendChatMessage(playerID, " 账号 " .. target .. " 不存在")
            end

        elseif cmd == "/removeadmin" then
            if not isPlayerAdmin(playerID) then
                MP.SendChatMessage(playerID, " 你没有权限使用此命令")
                return
            end
            local target = args:match("^(%S+)")
            if not target then
                MP.SendChatMessage(playerID, " 用法: /removeadmin <账号>")
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
    if beamId then
        playerAuthCache[0] = playerAuthCache[0] or {}
        playerAuthCache[0].pending_beam_id = beamId
        playerAuthCache[0].pending_ip = ip
        playerAuthCache[0].pending_name = player_name
    end
    
    return true
end

function onPlayerJoining(playerID)
    -- [Step 0] Carry over pending data from onPlayerAuth (which runs with no playerID yet)
    if playerAuthCache[0] and (playerAuthCache[0].pending_beam_id or playerAuthCache[0].pending_name) then
        local curName = nil
        local ok, pn = pcall(function() return MP.GetPlayerName(playerID) end)
        if ok and pn then curName = pn end
        -- Match via name (onPlayerAuth uses player_name which equals GetPlayerName here)
        local pending = playerAuthCache[0]
        if not pending.pending_name or not curName or pending.pending_name == curName then
            playerAuthCache[playerID] = playerAuthCache[playerID] or {}
            if pending.pending_beam_id then
                playerAuthCache[playerID].auth_beam_id = pending.pending_beam_id
            end
            if pending.pending_ip then
                playerAuthCache[playerID].auth_ip = pending.pending_ip
            end
            print("[BMP Login] onPlayerJoining(#" .. playerID .. "): 从 onPlayerAuth 携带 pending beam_id=" .. tostring(pending.pending_beam_id) .. " name=" .. tostring(curName))
            -- Clear global pending so next player doesn't collide
            playerAuthCache[0] = nil
        end
    end
    
    local beamId, ip, name = getPlayerStableInfo(playerID)
    
    -- Check auto-login
    -- 支持 3 种匹配方式（优先级：beamId -> IP兜底 -> NAME兜底）：
    --   1. boundId == 当前 beamId            （命中 HWID 或 BEAMMP: 官方 ID 时）
    --   2. boundId == "IP:" .. playerIp      （登录过一次后，IP 没变 + guest 数字变了也能命中）
    --   3. boundId == "NAME:" .. playerName  （玩家强制改了 BeamMP launcher 登录名回退绑定）
    local autoUser, autoVia, autoBound = nil, nil, nil
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
    if autoUser then
        playerAuthCache[playerID] = playerAuthCache[playerID] or {}
        playerAuthCache[playerID].username = autoUser
        playerAuthCache[playerID].beam_id  = beamId
        playerAuthCache[playerID].login_time = os.time()
        print("[BMP Login] 玩家 " .. tostring(name) .. " 自动登录为: " .. autoUser
              .. "  (via=" .. tostring(autoVia) .. ", bound=" .. tostring(autoBound) .. ")")
        MP.SendChatMessage(playerID, " ✨ 欢迎回来，已通过【" .. tostring(autoVia) .. "】自动登录为账号: " .. autoUser)
    end
    
    onlinePlayers[playerID] = name
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
    
    local count = 0
    for _ in pairs(onlinePlayers) do count = count + 1 end
    
    -- 写入在线玩家列表 (供 Admin GUI 读取)
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
    
    -- Welcome message to public
    MP.SendChatMessage(-1, " " .. tostring(name) .. " 加入了服务器！当前在线: " .. tostring(count + 1) .. " 人")
    
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
    local name = (onlinePlayers[playerID] and onlinePlayers[playerID].name) or ("Player" .. tostring(playerID))
    local count = 0
    for _ in pairs(onlinePlayers) do count = count + 1 end
    if count > 0 then count = count - 1 end

    onlinePlayers[playerID] = nil
    playerAuthCache[playerID] = nil
    playerVehicleCount[playerID] = nil
    pendingHwidRequests[playerID] = nil

    _saveOnlinePlayers()
    
    MP.SendChatMessage(-1, " " .. tostring(name) .. " 离开了服务器。当前在线: " .. tostring(count) .. " 人")
    print("[BMP Login] 玩家离开: " .. tostring(name))
end

-- ============================================================
-- Online Players JSON (供 Admin GUI 读取)
-- ============================================================
function _saveOnlinePlayers()
    local list = {}
    for pid, data in pairs(onlinePlayers) do
        table.insert(list, data)
    end
    writeJsonFile(ONLINE_FILE, list)
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
local lastBanMtime = 0
MP.RegisterEvent(BAN_POLL_EVENT, function()
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
end)
MP.CreateEventTimer(BAN_POLL_EVENT, 3000)  -- 每 3 秒

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

    -- 未认证玩家: 禁止刷车
    if role == "游客" or not isAuth then
        local msg = " 您尚未登录, 无法生成车辆. 请先 /login <账号> <密码> 登录账号后即可生成 " .. VEHICLE_LIMITS.authenticated .. " 辆"
        MP.SendChatMessage(playerID, msg)
        logDebug("onVehicleSpawn 拦截游客 #" .. tostring(playerID))
        return 1
    end

    -- 当前已生成车辆数 +1 (即将生成)
    playerVehicleCount[playerID] = (playerVehicleCount[playerID] or 0) + 1
    local count = playerVehicleCount[playerID]

    -- 超过限制: 取消生成
    if count > limit then
        playerVehicleCount[playerID] = count - 1  -- 回退计数 (车辆被取消)
        local msg = " 您是[" .. authLabel .. "用户], 车辆上限 " .. limit .. " 辆, 已达上限, 生成被取消"
        MP.SendChatMessage(playerID, msg)
        logDebug("onVehicleSpawn 取消 #" .. tostring(playerID) .. " 现有 " .. (count - 1) .. "/" .. limit)
        return 1  -- 取消事件 (车辆不会生成)
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
            acc = bit32.bor(bit32.lshift(acc, 6), val)
            bits = bits + 6
            if bits >= 8 then
                bits = bits - 8
                local byte = bit32.rshift(acc, bits) % 256
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
    for id, n in pairs(onlinePlayers) do
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
