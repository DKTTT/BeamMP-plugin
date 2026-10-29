# -*- coding: utf-8 -*-
"""
本地演示用: 生成假的 online_players.json, 不用开游戏也能验证网页面板 / 在线人数。

用法 (在仓库根目录):
    python tools/demo_players.py --data-dir bmp_login

然后浏览器打开 http://127.0.0.1:12124/
"""
import argparse
import json
import os
import time

PLAYERS = [
    # playerID, 名字, 管理员, 授信, join_time 偏移(秒)
    (1001, "DKSTD", True, True, 720),
    (1002, "Alice", False, True, 480),
    (1003, "Bob", False, False, 180),
    (1004, "Charlie", False, False, 30),
]


def hwid_of(pid):
    return "%08x-1111-4222-8333-%012x" % (pid, pid * 7919)


def build():
    now = int(time.time())
    out = []
    for pid, name, is_admin, trusted, age in PLAYERS:
        out.append({
            "playerID": pid,
            "name": name,
            "role": "玩家" if trusted else "游客",
            "is_authenticated": trusted,
            "trusted": trusted,
            "is_admin": is_admin,
            "beammp_id": ("acc_%s_%04x" % (name.lower(), pid)) if trusted else "",
            "bind_account": "",
            "beam_id": ("BEAMMP:acc_%s_%04x" % (name.lower(), pid)) if trusted else "GUEST:" + name.lower(),
            "hwid": hwid_of(pid) if trusted else "",
            "vehicle_count": 1 + (pid % 3),
            "join_time": now - age,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="与 BMP_HTTP_API.py 共用同一个数据目录")
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    target = os.path.join(args.data_dir, "online_players.json")
    print(f"[demo] 写入 {target}  每 {args.interval}s 一次, Ctrl+C 停止")
    try:
        while True:
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(build(), f, ensure_ascii=False)
            os.replace(tmp, target)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[demo] 停止")


if __name__ == "__main__":
    main()
