-- ============================================================
--  BMPHWID v2.3.0  BeamNG 沙箱兼容版
--
--  v2.3.0 核心改动:
--    1. 全部用 print() 替代 log() —— 沙箱环境 log() 被隔离, print() 可在 ~ 控制台看到
--    2. HTTP 优先级: curl (沙箱可用) → Engine.HTTP.request (沙箱可用) → socket (可能被沙箱禁用)
--    3. 先直接用 _G.curl / _G.Engine.HTTP 拉 Bridge, 不依赖 require()
-- ============================================================

local MOD = "bmpHwidProbe"
local VER = "2.6.0"
local SERVER_EVENT = "BMPHWID:HWIDReply"
local REQUEST_EVENT = "BMPHWID:RequestHWID"
local BRIDGE_URL = "http://127.0.0.1:7788"

-- 用 print() 输出到 BeamNG 控制台 (沙箱里 log() 不可见)
local function p(msg)
    print("[BMPHWID/"..VER.."] "..tostring(msg))
end

p("===== bmpHwidProbe v"..VER.." 沙箱兼容版 启动 =====")
p("  Bridge URL: "..BRIDGE_URL.."/hwid")
p("  服务器事件: "..SERVER_EVENT)

-- ============================================================
-- 1. UUID 生成
-- ============================================================
local function uuid4()
    local function rnd(a, b) return math.floor((os.clock() * 1000000 + os.time()) % (b - a + 1)) + a end
    local function hx(n) return string.format("%02x", n) end
    local t = {}
    for i = 1, 16 do t[i] = rnd(0, 255) end
    t[7] = (t[7] % 0x10) + 0x40
    t[9] = (t[9] % 0x40) + 0x80
    return hx(t[1])..hx(t[2])..hx(t[3])..hx(t[4]).."-"..
           hx(t[5])..hx(t[6]).."-"..hx(t[7])..hx(t[8]).."-"..
           hx(t[9])..hx(t[10]).."-"..hx(t[11])..hx(t[12])..hx(t[13])..hx(t[14])..hx(t[15])..hx(t[16])
end

-- ============================================================
-- 2. Base64 编码
-- ============================================================
local function b64encode(s)
    local b = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    local out = {}
    local i = 1
    while i <= #s do
        local a, b2, c = s:byte(i), s:byte(i+1), s:byte(i+2)
        local n = a * 65536 + (b2 or 0) * 256 + (c or 0)
        local c1 = math.floor(n / 262144) % 64
        local c2 = math.floor(n / 4096) % 64
        local c3 = math.floor(n / 64) % 64
        local c4 = n % 64
        out[#out+1] = b:sub(c1+1, c1+1)
        out[#out+1] = b:sub(c2+1, c2+1)
        if b2 then out[#out+1] = b:sub(c3+1, c3+1) else out[#out+1] = "=" end
        if c  then out[#out+1] = b:sub(c4+1, c4+1) else out[#out+1] = "=" end
        i = i + 3
    end
    return table.concat(out)
end

-- ============================================================
-- 3. HTTP 层 (沙箱兼容版)
--    优先级: curl → Engine.HTTP.request → socket.http → socket.tcp
-- ============================================================
local httpApi = nil
local function probeHttp()
    if httpApi then return httpApi end
    local res = {}

    -- 优先级 1: curl (BeamNG 沙箱标配, _G.curl)
    local c = rawget(_G, "curl")
    p("  [HTTP] 检查 _G.curl: "..tostring(type(c)))
    if type(c) == "table" and type(c.get) == "function" then
        res.get = function(url)
            local result = {body=nil, done=false}
            local ok, err = pcall(c.get, url, function(r)
                if r and r.body then result.body = r.body end
                result.done = true
            end)
            local t0 = os.clock()
            while not result.done and (os.clock() - t0) < 3.0 do
                -- busy wait (sandbox has no sleep)
            end
            return result.body
        end
        res.getName = "curl"
        p("  [HTTP] ✅ curl.get 可用 (沙箱内置)")
    end

    -- 优先级 2: Engine.HTTP.request (BeamNG 标准 API)
    if not res.get then
        local E = rawget(_G, "Engine")
        p("  [HTTP] 检查 _G.Engine: "..tostring(type(E)))
        if type(E) == "table" and type(E.HTTP) == "table" and type(E.HTTP.request) == "function" then
            res.get = function(url)
                local result = {body=nil, done=false}
                E.HTTP.request("GET", url, {}, "", function(r)
                    if r and (r.body or r.data) then result.body = r.body or r.data end
                    result.done = true
                end)
                local t0 = os.clock()
                while not result.done and (os.clock() - t0) < 3.0 do end
                return result.body
            end
            res.getName = "Engine.HTTP.request"
            p("  [HTTP] ✅ Engine.HTTP.request 可用")
        end
    end

    -- 优先级 3: LuaSocket (可能被沙箱禁用, 但 BeamMP mod 用的就是这个)
    local ok_socket, sk = pcall(require, "socket")
    p("  [HTTP] 检查 require('socket'): "..tostring(ok_socket and "成功" or "失败"))
    if ok_socket and type(sk) == "table" then
        -- 3a: socket.http 高级封装
        local ok_http, sk_http = pcall(require, "socket.http")
        if ok_http and type(sk_http) == "table" then
            res.get = function(url)
                local body, code = sk_http.request(url)
                return body, code
            end
            res.getName = "socket.http"
            p("  [HTTP] ✅ socket.http 高级封装可用")
        end
        -- 3b: socket.tcp 原始 TCP (兜底)
        if type(sk.tcp) == "function" then
            res.rawTcp = sk.tcp
            p("  [HTTP] ✅ socket.tcp 原始 TCP 可用")
        end
    end

    res.ready = (res.get ~= nil) or (res.rawTcp ~= nil)
    httpApi = res
    if res.ready then
        p("  [HTTP] ✅ HTTP 就绪: get="..tostring(res.getName or "nil").." rawTcp="..tostring(type(res.rawTcp)))
    else
        p("  [HTTP] ❌ 所有 HTTP API 都不可用!")
    end
    return res
end

-- 原始 TCP HTTP GET 兜底
local function rawTcpGet(url)
    local api = probeHttp()
    if not api.rawTcp then return nil, "no rawTcp" end
    local host, port, path = url:match("^http://([^/:]+):(%d+)(/.*)$")
    if not host then host, port, path = url:match("^http://([^/:]+)(/.*)$"); port = "80" end
    if not host then return nil, "url parse fail" end
    port = tonumber(port) or 80
    path = path or "/"
    local sock = api.rawTcp()
    if not sock then return nil, "socket.tcp() 返回 nil" end
    pcall(function() sock:settimeout(3) end)
    local ok, err = pcall(function() sock:connect(host, port) end)
    if not ok then
        p("  [rawTcp] 连接 "..host..":"..port.." 失败: "..tostring(err))
        pcall(function() sock:close() end)
        return nil, "connect fail"
    end
    local req = "GET "..path.." HTTP/1.1\r\nHost: "..host..":"..port.."\r\nConnection: close\r\n\r\n"
    pcall(function() sock:send(req) end)
    local chunks = {}
    local deadline = os.clock() + 3
    while os.clock() < deadline do
        local data, st, partial = sock:receive(4096)
        if data then
            chunks[#chunks+1] = data
        elseif st == "timeout" then
            if partial then chunks[#chunks+1] = partial end
            break
        else
            break
        end
    end
    pcall(function() sock:close() end)
    local fullBody = table.concat(chunks)
    if not fullBody or #fullBody == 0 then return nil, "empty" end
    local body = fullBody:match("\r\n\r\n(.*)$") or fullBody:match("\n\n(.*)$") or ""
    return body, nil
end

-- 从 Bridge 拉 HWID
local function fetchHwidFromBridge()
    local api = probeHttp()
    if not api.ready then
        p("  [fetch] ❌ 无 HTTP API")
        return nil
    end

    -- 路径 1: 封装好的 get (curl / Engine.HTTP / socket.http)
    if api.get then
        p("  [fetch] 路径1: GET "..BRIDGE_URL.."/hwid 用 "..tostring(api.getName))
        local body, code = api.get(BRIDGE_URL .. "/hwid")
        if body and type(body) == "string" and #body > 0 then
            local hwid_m = body:match('"hwid"%s*:%s*"([^"]+)"')
            if hwid_m then
                p("  [fetch] ✅ 从 Bridge 拉到 HWID: "..hwid_m)
                return hwid_m
            end
            p("  [fetch] JSON 解析失败, body前150字节: "..tostring(body):sub(1, 150))
            return nil
        end
        p("  [fetch] GET 返回空 body (code="..tostring(code)..")")
    end

    -- 路径 2: raw TCP
    if api.rawTcp then
        p("  [fetch] 路径2: socket.tcp 原始 TCP")
        local body, err = rawTcpGet(BRIDGE_URL .. "/hwid")
        if body then
            local hwid_m = body:match('"hwid"%s*:%s*"([^"]+)"')
            if hwid_m then
                p("  [fetch] ✅ [raw TCP] 拉到 HWID: "..hwid_m)
                return hwid_m
            end
            p("  [fetch] [raw TCP] body解析失败, 前150字节: "..tostring(body):sub(1, 150))
        else
            p("  [fetch] [raw TCP] 失败: "..tostring(err))
        end
    end

    p("  [fetch] ❌ Bridge 未启动或 HTTP 失败")
    return nil
end

-- ============================================================
-- 4. settings 永久保存
-- ============================================================
local function saveHwidToSettings(hwid)
    local sett = rawget(_G, "settings")
    if type(sett) ~= "table" then return false end
    local ok = pcall(function()
        if type(sett.setValue) == "function" then sett.setValue("BMPLoginHWID", hwid) end
        if type(sett.save) == "function" then sett.save() end
        if type(sett.saveSettings) == "function" then sett.saveSettings() end
    end)
    return ok and true or false
end

local function loadHwidFromSettings()
    local sett = rawget(_G, "settings")
    if type(sett) ~= "table" then return nil end
    local v = nil
    pcall(function() if type(sett.getValue) == "function" then v = sett.getValue("BMPLoginHWID") end end)
    if type(v) == "string" and v:match("^%x+-%x+-%x+-%x+-%x+$") then return v end
    return nil
end

-- ============================================================
-- 5. BeamMP API 探针
-- ============================================================
local function probeBeamMP()
    local result = {}
    local tse = rawget(_G, "TriggerServerEvent")
    result.triggerType = type(tse)
    if type(tse) == "function" then
        result.triggerFn = tse
        p("  [BeamMP] ✅ _G.TriggerServerEvent = function")
    else
        -- 尝试通过其他名字找到
        p("  [BeamMP] _G.TriggerServerEvent = "..tostring(tse)..", 搜索替代...")
        for _, name in ipairs({"mp", "MP", "beammp", "BeamMP"}) do
            local t = rawget(_G, name)
            if type(t) == "table" and type(t.TriggerServerEvent) == "function" then
                result.triggerFn = function(ev, data) return t.TriggerServerEvent(ev, data) end
                p("  [BeamMP] ✅ 找到 "..name..".TriggerServerEvent = function")
                break
            end
        end
    end
    return result
end

-- ============================================================
-- 6. 主流程
-- ============================================================
local state = {
    tElapsed = 0,
    attempts = 0,
    cachedHwid = nil,
    sendSucceeded = false,
    triggerSeenNil = 0,
    triggerSeenFn = 0,
    httpSeenFail = 0,
}

local function runOnce()
    state.attempts = state.attempts + 1
    p("▶ 尝试 #"..state.attempts.." (t="..string.format("%.1f", state.tElapsed).."s)")

    -- 1) HWID: settings → Bridge HTTP → 兜底生成
    if not state.cachedHwid then
        local v = loadHwidFromSettings()
        if v then
            state.cachedHwid = v
            p("  从 settings 读历史 HWID: "..v)
        end
    end
    local fresh = fetchHwidFromBridge()
    if fresh then
        state.cachedHwid = fresh
        saveHwidToSettings(fresh)
        p("  ✅ 已永久保存到 settings")
    end
    if not state.cachedHwid then
        local new = uuid4()
        state.cachedHwid = new
        saveHwidToSettings(new)
        p("  ⚠️ 生成新 UUID (Bridge 不可用): "..new)
    end

    -- 2) 探针 BeamMP API
    local api = probeBeamMP()
    local tse_type = api.triggerType

    if tse_type == "function" then
        state.triggerSeenFn = state.triggerSeenFn + 1
    else
        state.triggerSeenNil = state.triggerSeenNil + 1
    end

    -- 3) 发送
    if tse_type == "function" and api.triggerFn then
        local lines = {
            "_version\tBMPHWID-v2.3",
            "bridge_os_version\twin32",
            "bridge_sources\tbridge.exe+lua-socket",
            "core_env_fingerprint\tfnv1a",
            "settings.BMPLoginHWID(uuid)\t"..state.cachedHwid,
            "uuid\t"..state.cachedHwid,
        }
        local payload = b64encode(table.concat(lines, "\n"))
        local frame = "F@"..state.cachedHwid.."|v1|cnt=1|cur=0|"..payload
        p("  🚀 TriggerServerEvent('"..SERVER_EVENT.."', "..#frame.." bytes)")
        local ok, err = pcall(api.triggerFn, SERVER_EVENT, frame)
        if ok then
            state.sendSucceeded = true
            p("  ✅✅✅ 成功! HWID: "..state.cachedHwid)
            p("  服务器应该收到 BMPHWID:HWIDReply, 绑定该 HWID 到你的账号")
            return true
        else
            p("  ❌ 发送失败: "..tostring(err))
        end
    else
        if state.tElapsed < 60.0 then
            p("  ⏳ "..string.format("%.0f", 60.0 - state.tElapsed).."s 内继续等 BeamMP 加载 (TriggerServerEvent="..tostring(tse_type)..")")
        else
            p("  ⚠️ 60s 已过, TriggerServerEvent 仍是 "..tostring(tse_type))
        end
    end
    return false
end

local function onUpdate(dt, dtSim, dtRaw)
    if state.sendSucceeded then return end
    -- BeamNG engine passes (dt) or (dtReal, dtSim, dtRaw); use first numeric arg
    local delta = tonumber(dt) or tonumber(dtSim) or 0
    state.tElapsed = state.tElapsed + delta
    local sched = {2, 3, 5, 8, 12, 18, 25, 35, 45, 55, 62, 70, 80, 95}
    if state.attempts < #sched and state.tElapsed >= sched[state.attempts + 1] then
        runOnce()
    end
end

-- 暴露扩展接口
local M = {}
M.onUpdate = onUpdate
M.onVehicleSpawned          = function() onUpdate(0.05) end
M.onVehicleResetted         = function() onUpdate(0.05) end
M.onFreeroamLoaded          = function() onUpdate(0.5) end
M.onPlayerCameraModeChanged = function() onUpdate(0.05) end
M.state = state
M.VER = VER
M.runOnce = runOnce
M.getHwid = function() return state.cachedHwid end

-- ============================================================
-- 服务器主动请求架构: onHwidRequest
-- 服务器 MP.TriggerClientEvent("BMPHWID:RequestHWID") 会触发此函数
-- ============================================================
function M.onHwidRequest()
    p("📡 收到服务器 HWID 请求 ("..REQUEST_EVENT..")")
    -- 直接跑 runOnce() 拉 Bridge + 发送回服务器
    local ok, result = pcall(runOnce)
    if ok and result then
        p("✅ HWID 请求响应成功")
    else
        p("❌ HWID 请求响应失败: "..tostring(result))
        -- 即使失败也生成一个 HWID 发送 (不阻塞玩家)
        if not state.cachedHwid then
            state.cachedHwid = uuid4()
            saveHwidToSettings(state.cachedHwid)
            p("  ⚠️ Bridge 不可用, 生成临时 UUID: "..state.cachedHwid)
        end
        -- 用缓存/临时 UUID 发送一次 (让玩家至少能进服务器)
        local api = probeBeamMP()
        if api.triggerFn then
            local lines = {
                "_version\tBMPHWID-v2.6",
                "bridge_os_version\twin32",
                "bridge_sources\tbridge.exe+lua-socket",
                "core_env_fingerprint\tfnv1a",
                "settings.BMPLoginHWID(uuid)\t"..state.cachedHwid,
                "uuid\t"..state.cachedHwid,
            }
            local payload = b64encode(table.concat(lines, "\n"))
            local frame = "F@"..state.cachedHwid.."|v1|cnt=1|cur=0|"..payload
            p("  🚀 兜底发送 (无 Bridge): "..#frame.." bytes")
            pcall(api.triggerFn, SERVER_EVENT, frame)
        end
    end
end

p("✅ bmpHwidProbe v"..VER.." 注册完成")
p("   调度: 2s/3s/5s/8s/12s/18s/25s/35s/45s/55s/62s/70s/80s/95s")
p("   手动触发: 控制台输入 extensions.bmpHwidProbe.runOnce()")
p("   或输入 bmpHwidProbeManualRun()")
p("   服务器请求事件: "..REQUEST_EVENT.." → onHwidRequest()")

return M
