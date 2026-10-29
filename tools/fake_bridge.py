# -*- coding: utf-8 -*-
"""
本地演示用: 冒充 BMPHWID_Bridge 在本机 7788 端口返回一个固定机器码。

真机上这个端口的服务由 BMPHWID_Bridge.exe 提供（网页注册页的"读取本机"按钮
就是去 fetch http://127.0.0.1:7788/hwid）。

用法:
    python tools/fake_bridge.py 000003e9-1111-4222-8333-00000078f487
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_HWID = "000003e9-1111-4222-8333-00000078f487"


class Handler(BaseHTTPRequestHandler):
    hwid = DEFAULT_HWID

    def log_message(self, *args):
        return

    def _write(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 网页面板是跨端口的, 必须放行 CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._write({
            "ok": True,
            "name": "DEMO-Bridge",
            "ver": "demo",
            "hwid": self.hwid,
            "sources": ["MG:demo"],
            "command": "/bmpid " + self.hwid,
        })

    def do_POST(self):
        self._write({"ok": True})


def main():
    if len(sys.argv) > 1:
        Handler.hwid = sys.argv[1]
    print(f"[demo] 假 Bridge 监听 127.0.0.1:7788  hwid={Handler.hwid}")
    HTTPServer(("127.0.0.1", 7788), Handler).serve_forever()


if __name__ == "__main__":
    main()
