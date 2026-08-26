-- ============================================================
--  BMPHWID v2.6.0  modScript 入口 (服务器请求架构版)
--
--  核心: 服务器 onPlayerConnected → MP.TriggerClientEvent("BMPHWID:RequestHWID")
--        客户端 AddEventHandler 监听 → 收到后 probeMod.onHwidRequest()
--        → 从 Bridge.exe HTTP 拉 HWID → TriggerServerEvent 发回服务器
--
--  兜底: 如果 AddEventHandler 注册失败, modScript 加载时也主动发一次
-- ============================================================

print("[BMPHWID.modScript] modScript v2.6.0 入口启动 (服务器请求架构版)")

-- 加载 extension
local probeMod = nil
pcall(function()
    if type(extensions) == "table" and type(extensions.loadModule) == "function" then
        local ok, e = pcall(extensions.loadModule, "bmpHwidProbe")
        if ok then
            probeMod = rawget(_G, "extensions") and rawget(_G.extensions, "bmpHwidProbe")
            print("[BMPHWID.modScript] ✅ extensions.loadModule 成功, probeMod="..tostring(type(probeMod)))
        end
    end
end)

if not probeMod then
    pcall(function()
        local ok, mod = pcall(require, "ge.extensions.bmpHwidProbe")
        if ok and type(mod) == "table" then
            probeMod = mod
            print("[BMPHWID.modScript] ✅ require 成功")
        end
    end)
end

print("[BMPHWID.modScript] probeMod="..tostring(type(probeMod)))

-- 暴露手动调试入口
_G.bmpHwidProbeManualRun = function()
    if probeMod and type(probeMod.runOnce) == "function" then
        return probeMod.runOnce()
    end
    return false
end

-- ============================================================
-- 注册 AddEventHandler 监听服务器的 HWID 请求
-- ============================================================
local REQUEST_EVENT = "BMPHWID:RequestHWID"
local handlerRegistered = false

-- 检查 AddEventHandler 是否可用 (BeamMP MPGameNetwork 提供)
local addEventHandler = rawget(_G, "AddEventHandler")
if type(addEventHandler) == "function" then
    local ok, err = pcall(addEventHandler, REQUEST_EVENT, function(arg1, arg2, arg3)
        print("[BMPHWID.modScript] 📡 收到服务器事件: "..REQUEST_EVENT.." (arg1="..tostring(arg1)..")")
        if probeMod and type(probeMod.onHwidRequest) == "function" then
            pcall(probeMod.onHwidRequest)
        elseif probeMod and type(probeMod.runOnce) == "function" then
            pcall(probeMod.runOnce)
        end
    end)
    if ok then
        handlerRegistered = true
        print("[BMPHWID.modScript] ✅ 已注册 AddEventHandler 监听 "..REQUEST_EVENT)
    else
        print("[BMPHWID.modScript] ❌ AddEventHandler 注册失败: "..tostring(err))
    end
else
    print("[BMPHWID.modScript] ⚠️ AddEventHandler 不可用 (type="..tostring(type(addEventHandler))..")")
    print("[BMPHWID.modScript]   TriggerServerEvent 也不可用, 可能 BeamMP mod 没加载")
end

-- ============================================================
-- 兜底: 如果事件监听没注册成功, 主动发一次 HWID
-- (如果服务器已经连上, 这次就能收到; 如果没连上, 等下次)
-- ============================================================
if not handlerRegistered and probeMod and type(probeMod.runOnce) == "function" then
    print("[BMPHWID.modScript] ⚠️ 事件监听未注册, 主动发送一次 HWID 作为兜底")
    pcall(probeMod.runOnce)
end

print("[BMPHWID.modScript] modScript v2.6.0 入口结束 (监听已注册="..tostring(handlerRegistered)..")")
