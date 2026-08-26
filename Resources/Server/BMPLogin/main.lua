-- ============================================================
-- BMP Login Plugin v2.1.0 (Clean Rebuild)
-- BeamMP Server Player Account Management
-- ============================================================

local PLUGIN_NAME = "BMP Login"
local PLUGIN_VERSION = "2.1.0"
local DATA_DIR = "bmp_login"
local ACCOUNTS_FILE = DATA_DIR .. "/accounts.json"
local ADMINS_FILE = DATA_DIR .. "/admins.json"
local BANLIST_FILE = DATA_DIR .. "/banlist.json"
local GUEST_MAP_FILE = DATA_DIR .. "/guest_map.json"

local accounts = {}
local admins = {}
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
    if not storedHash or storedHash == "" then return false end
    
    local salt = storedHash:match("^([^:]+):")
    local hash = storedHash:match(":(.+)$")
    if not salt or not hash then return false end
    
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

local function loadAdmins()
    local data = readJsonFile(ADMINS_FILE)
    if data then admins = data else admins = {} end
    print("[BMP Login] 管理员数量: " .. #admins)
end

local function saveAdmins()
    writeJsonFile(ADMINS_FILE, admins)
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
local function getPlayerRole(beamId)
    if not beamId then return "游客" end
    for _, admin in ipairs(admins) do
        if admin.beam_id == beamId then return "管理员" end
    end
    if accounts[beamId] then return "玩家" end
    return "游客"
end

local function isAdmin(beamId)
    if not beamId then return false end
    for _, admin in ipairs(admins) do
        if admin.beam_id == beamId then return true end
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
--   管理员: 无限制
-- ============================================================
local VEHICLE_LIMITS = {
    admin = 999,        -- 管理员无限制
    authenticated = 5,  -- 认证用户
    unauthenticated = 1, -- 未认证用户
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
    if beam_id_ok and beam_id and isAdmin(beam_id) then
        return VEHICLE_LIMITS.admin
    end
    if isPlayerAuthenticated(playerID) then
        return VEHICLE_LIMITS.authenticated
    end
    return VEHICLE_LIMITS.unauthenticated
end

local function getPlayerAuthLabel(playerID)
    if not playerID then return "未认证" end
    local beam_id_ok, beam_id = pcall(function() return getPlayerStableInfo(playerID) end)
    if beam_id_ok and beam_id and isAdmin(beam_id) then return "管理员" end
    if isPlayerAuthenticated(playerID) then return "认证" end
    return "未认证"
end

-- per-player 当前车辆数 (playerID -> count)
local playerVehicleCount = {}

local function getAdminLevel(beamId)
    if not beamId then return 0 end
    for _, admin in ipairs(admins) do
        if admin.beam_id == beamId then return admin.level or 1 end
    end
    return 0
end

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
    if not verifyPassword(password, account.password_hash) then
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
    MP.SendChatMessage(playerID, " 登录成功！欢迎回来，" .. username)
    logMsg("玩家 " .. tostring(playerName) .. " 登录了账号: " .. username)
    return true
end

local function logoutAccount(playerID)
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    local role = getPlayerRole(beamId)
    
    if role == "游客" then
        MP.SendChatMessage(playerID, " 你当前未登录任何账号")
        return
    end
    
    MP.SendChatMessage(playerID, " 已退出登录")
    logMsg("玩家 " .. tostring(playerName) .. " 退出了登录")
end

-- ============================================================
-- Admin Commands
-- ============================================================
local function addAdmin(playerID, targetName, level)
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    level = level or 1
    for _, admin in ipairs(admins) do
        if admin.username == targetName then
            admin.level = level
            saveAdmins()
            MP.SendChatMessage(playerID, " 已更新管理员 " .. targetName .. " 的权限等级为 " .. level)
            return
        end
    end
    
    local targetBeamId = nil
    if accounts[targetName] then
        local bindings = accounts[targetName].bind_beam_ids or {}
        targetBeamId = bindings[1]
    end
    
    admins[#admins + 1] = {
        username = targetName,
        beam_id = targetBeamId or "PENDING:" .. targetName,
        level = level
    }
    saveAdmins()
    MP.SendChatMessage(playerID, " 已添加管理员: " .. targetName .. " (等级 " .. level .. ")")
    logMsg("管理员 " .. tostring(playerName) .. " 提升 " .. targetName .. " 为管理员(等级" .. level .. ")")
end

local function removeAdmin(playerID, targetName)
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    for i, admin in ipairs(admins) do
        if admin.username == targetName then
            table.remove(admins, i)
            saveAdmins()
            MP.SendChatMessage(playerID, " 已移除管理员: " .. targetName)
            logMsg("管理员 " .. tostring(playerName) .. " 移除了管理员: " .. targetName)
            return
        end
    end
    
    MP.SendChatMessage(playerID, " 错误: 未找到管理员 " .. targetName)
end

local function banUser(playerID, targetName, reason)
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    for _, entry in ipairs(banlist.accounts) do
        if entry.username == targetName then
            MP.SendChatMessage(playerID, " 账号 " .. targetName .. " 已在封禁列表中")
            return
        end
    end
    
    banlist.accounts[#banlist.accounts + 1] = {
        username = targetName,
        reason = reason or "未指定原因",
        time = os.time(),
        by = playerName
    }
    saveBanlist()
    MP.SendChatMessage(playerID, " 已封禁账号: " .. targetName)
    logMsg("管理员 " .. tostring(playerName) .. " 封禁了账号: " .. targetName)
end

local function unbanUser(playerID, targetName)
    local beamId = getPlayerStableInfo(playerID)
    local playerName = MP.GetPlayerName(playerID)
    
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    for i, entry in ipairs(banlist.accounts) do
        if entry.username == targetName then
            table.remove(banlist.accounts, i)
            saveBanlist()
            MP.SendChatMessage(playerID, " 已解封账号: " .. targetName)
            logMsg("管理员 " .. tostring(playerName) .. " 解封了账号: " .. targetName)
            return
        end
    end
    
    MP.SendChatMessage(playerID, " 错误: 未找到封禁记录 " .. targetName)
end

local function listAdmins(playerID)
    local beamId = getPlayerStableInfo(playerID)
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    MP.SendChatMessage(playerID, " =========== 管理员列表 ===========")
    for _, admin in ipairs(admins) do
        MP.SendChatMessage(playerID, " " .. admin.username .. " - 等级 " .. tostring(admin.level) .. " (ID: " .. tostring(admin.beam_id) .. ")")
    end
    MP.SendChatMessage(playerID, " ====================================")
end

local function listOnline(playerID)
    local beamId = getPlayerStableInfo(playerID)
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    MP.SendChatMessage(playerID, " =========== 在线玩家 ===========")
    local count = 0
    for id, name in pairs(onlinePlayers) do
        local stableId, ip, _ = getPlayerStableInfo(id)
        local role = getPlayerRole(stableId)
        MP.SendChatMessage(playerID, " " .. tostring(name) .. " - " .. role .. " (IP: " .. tostring(ip) .. ")")
        count = count + 1
    end
    MP.SendChatMessage(playerID, " 共 " .. count .. " 人在线")
    MP.SendChatMessage(playerID, " ====================================")
end

local function queryUser(playerID, targetName)
    local beamId = getPlayerStableInfo(playerID)
    if not isAdmin(beamId) then
        MP.SendChatMessage(playerID, " 错误: 你没有管理员权限")
        return
    end
    
    local account = accounts[targetName]
    if not account then
        MP.SendChatMessage(playerID, " 错误: 未找到账号 " .. targetName)
        return
    end
    
    MP.SendChatMessage(playerID, " =========== 账号信息: " .. targetName .. " ===========")
    MP.SendChatMessage(playerID, " 注册时间: " .. os.date("%Y-%m-%d %H:%M:%S", account.register_time))
    MP.SendChatMessage(playerID, " 绑定ID数: " .. tostring(#(account.bind_beam_ids or {})))
    MP.SendChatMessage(playerID, " 登录次数: " .. tostring(#(account.login_records or {})))
    
    local lastLogin = (account.login_records or {})[#(account.login_records or {})]
    if lastLogin then
        MP.SendChatMessage(playerID, " 最后登录: " .. os.date("%Y-%m-%d %H:%M:%S", lastLogin.time) .. " (ID: " .. tostring(lastLogin.beam_id) .. ")")
    end
    MP.SendChatMessage(playerID, " ================================================")
end

-- ============================================================
-- Chat Message Handler
-- ============================================================
function onChatMessage(playerID, playerName, message)
    if not message or message == "" then return end
    
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
    -- 普通聊天返回 0 = 允许正常广播
    return 0
end

function _handleChat(playerID, playerName, message)
    
    local beamId = getPlayerStableInfo(playerID)
    local role = getPlayerRole(beamId)
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
        
        if cmd == "/help" then
            MP.SendChatMessage(playerID, " =========== 可用命令 ===========")
            MP.SendChatMessage(playerID, " /register <账号> <密码> - 注册新账号")
            MP.SendChatMessage(playerID, " /login <账号> <密码> - 登录账号")
            MP.SendChatMessage(playerID, " /logout - 退出登录")
            MP.SendChatMessage(playerID, " /whoami - 查看登录状态")
            MP.SendChatMessage(playerID, " /vehiclelimit - 查看车辆上限 (1/5)")
            MP.SendChatMessage(playerID, " /bmpid <payload> - 客户端HWID回传")
            if role == "管理员" then
                MP.SendChatMessage(playerID, " --- 管理员命令 ---")
                MP.SendChatMessage(playerID, " /addadmin <账号> [等级] - 提升为管理员")
                MP.SendChatMessage(playerID, " /removeadmin <账号> - 移除管理员")
                MP.SendChatMessage(playerID, " /banuser <账号> [原因] - 封禁账号")
                MP.SendChatMessage(playerID, " /unbanuser <账号> - 解封账号")
                MP.SendChatMessage(playerID, " /listadmins - 查看管理员列表")
                MP.SendChatMessage(playerID, " /listonline - 查看在线玩家")
                MP.SendChatMessage(playerID, " /queryuser <账号> - 查询账号信息")
            end
            MP.SendChatMessage(playerID, " ================================")
            MP.SendChatMessage(playerID, " [车辆限制] 未认证用户 1 辆, 认证用户 5 辆")
            
        elseif cmd == "/register" then
            local username, password = args:match("^(%S+)%s+(.+)$")
            registerAccount(playerID, username, password)
            
        elseif cmd == "/login" then
            local username, password = args:match("^(%S+)%s+(.+)$")
            loginAccount(playerID, username, password)
            
        elseif cmd == "/logout" then
            logoutAccount(playerID)
            
        elseif cmd == "/whoami" then
            local beamId2 = getPlayerStableInfo(playerID)
            local role2 = getPlayerRole(beamId2)
            MP.SendChatMessage(playerID, " 你当前身份: " .. role2)
            MP.SendChatMessage(playerID, " 你的ID: " .. tostring(beamId2))
            if accounts[playerName] then
                MP.SendChatMessage(playerID, " 已注册账号: " .. playerName)
            end
            
        elseif cmd == "/bmpid" then
            handleBmpidCommand(playerID, playerName, args)

        elseif cmd == "/vehiclelimit" or cmd == "/vehicles" or cmd == "/carlimit" then
            local limit = getPlayerVehicleLimit(playerID)
            local current = playerVehicleCount[playerID] or 0
            local label = getPlayerAuthLabel(playerID)
            MP.SendChatMessage(playerID, " ============ 车辆上限 ===========")
            MP.SendChatMessage(playerID, " 你的身份: " .. label .. "用户")
            MP.SendChatMessage(playerID, " 当前已生成: " .. tostring(current) .. " / " .. tostring(limit) .. " 辆")
            if label == "未认证" then
                MP.SendChatMessage(playerID, " 未认证用户上限 " .. VEHICLE_LIMITS.unauthenticated .. " 辆")
                MP.SendChatMessage(playerID, " 认证方式: /bmpid <UUID> + /login <账号> <密码>")
                MP.SendChatMessage(playerID, " 认证后上限 " .. VEHICLE_LIMITS.authenticated .. " 辆")
            elseif label == "认证" then
                MP.SendChatMessage(playerID, " 认证用户上限 " .. VEHICLE_LIMITS.authenticated .. " 辆")
            else
                MP.SendChatMessage(playerID, " 管理员无车辆限制")
            end
            MP.SendChatMessage(playerID, " ================================")

        elseif cmd == "/gethwid" then
            local beamId3, ip3, _ = getPlayerStableInfo(playerID)
            MP.SendChatMessage(playerID, " ============ 获取稳定HWID 指南 ===========")
            MP.SendChatMessage(playerID, " 方法A (推荐·无需客户端mod):")
            MP.SendChatMessage(playerID, "  1. 在服务器下载目录里找到 BMPHWID_Bridge.exe 并运行")
            MP.SendChatMessage(playerID, "  2. 保持 BeamNG 在前台，点 Bridge 里的 [一键粘贴发送]")
            MP.SendChatMessage(playerID, "  3. 服务器收到后会立即私聊提示你 HWID 已绑定 ✅")
            MP.SendChatMessage(playerID, " 方法B (自己手动复制):")
            MP.SendChatMessage(playerID, "  1. 运行 BMPHWID_Bridge.exe → 点 [复制命令]")
            MP.SendChatMessage(playerID, "  2. 在游戏里按 T → Ctrl+V → 回车粘贴发送 /bmpid ... 那一行")
            MP.SendChatMessage(playerID, " 方法C (客户端mod·优先·最自动化):")
            MP.SendChatMessage(playerID, "  服务器 BMPHWID.zip 客户端已自动随服务器下发")
            MP.SendChatMessage(playerID, "  若加载成功，进入服务器后 30 秒内会自动回传 HWID")
            MP.SendChatMessage(playerID, " 方法D (仅测试):")
            MP.SendChatMessage(playerID, "  在 BeamNG 控制台执行: extensions.bmpHwidProbe.trySendFull()")
            MP.SendChatMessage(playerID, " ===========================================")
            if beamId3 then
                MP.SendChatMessage(playerID, " 当前你被识别为 ID: " .. tostring(beamId3))
                MP.SendChatMessage(playerID, " 建议：先登录账号 (/login 账号 密码)，然后按上面任意方法获得稳定HWID，下次自动登录就生效了 ✨")
            end
            if ip3 then MP.SendChatMessage(playerID, " 当前IP兜底（无需操作，登录后自动绑定）：IP:" .. ip3) end
            
        elseif cmd == "/addadmin" then
            local target, level = args:match("^(%S+)%s*(%d*)")
            level = tonumber(level) or 1
            addAdmin(playerID, target, level)
            
        elseif cmd == "/removeadmin" then
            local target = args:match("^(%S+)")
            removeAdmin(playerID, target)
            
        elseif cmd == "/banuser" then
            local target, reason = args:match("^(%S+)%s*(.*)")
            banUser(playerID, target, reason)
            
        elseif cmd == "/unbanuser" then
            local target = args:match("^(%S+)")
            unbanUser(playerID, target)
            
        elseif cmd == "/listadmins" then
            listAdmins(playerID)
            
        elseif cmd == "/listonline" then
            listOnline(playerID)
            
        elseif cmd == "/queryuser" then
            local target = args:match("^(%S+)")
            queryUser(playerID, target)
            
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
        loadAdmins()
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
            print("[BMP Login] All events registered successfully (incl. BMPHWID client)")
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
    
    -- Check device ban
    if beamId then
        for _, entry in ipairs(banlist.devices or {}) do
            if entry.device_id and beamId == entry.device_id then
                return "该设备已被封禁"
            end
        end
    end
    
    -- Check account ban by IP
    if ip then
        for _, entry in ipairs(banlist.accounts or {}) do
            if entry.ip and ip == entry.ip then
                return "该IP已被封禁"
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
    
    local role = getPlayerRole(beamId)
    print("[BMP Login] 玩家连接: " .. tostring(name) .. " (身份: " .. role .. ")")
end

function onPlayerDisconnect(playerID)
    local name = onlinePlayers[playerID] or ("Player" .. tostring(playerID))
    local count = 0
    for _ in pairs(onlinePlayers) do count = count + 1 end
    if count > 0 then count = count - 1 end

    onlinePlayers[playerID] = nil
    playerAuthCache[playerID] = nil
    playerVehicleCount[playerID] = nil  -- 清空车辆计数
    pendingHwidRequests[playerID] = nil  -- 清理 HWID 请求

    MP.SendChatMessage(-1, " " .. tostring(name) .. " 离开了服务器。当前在线: " .. tostring(count) .. " 人")
    print("[BMP Login] 玩家离开: " .. tostring(name))
end

-- ============================================================
-- Vehicle Events (minimal)
-- ============================================================
function onVehicleSpawn(playerID, vehicleID, vehicleData)
    -- 车辆生成事件 (3 参数: playerID, vehicleID, vehicleData)
    -- 按官方 BeamMP 文档, return 1 可取消事件 (车辆不会被生成)

    -- 当前已生成车辆数 +1 (即将生成)
    playerVehicleCount[playerID] = (playerVehicleCount[playerID] or 0) + 1
    local count = playerVehicleCount[playerID]
    local limit = getPlayerVehicleLimit(playerID)
    local authLabel = getPlayerAuthLabel(playerID)

    -- 超过限制: 取消生成
    if count > limit then
        playerVehicleCount[playerID] = count - 1  -- 回退计数 (车辆被取消)
        local msg
        if limit == VEHICLE_LIMITS.unauthenticated then
            msg = " 您是[" .. authLabel .. "用户], 车辆上限 " .. limit .. " 辆, 已达上限, 生成被取消. 请先 /bmpid 发送 HWID + /login 登录账号后即可生成 " .. VEHICLE_LIMITS.authenticated .. " 辆"
        else
            msg = " 您是[" .. authLabel .. "用户], 车辆上限 " .. limit .. " 辆, 已达上限, 生成被取消"
        end
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
