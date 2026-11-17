#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 机示例三：REST API 客户端
============================================================
A 机网关以 --mode http 运行时，用 HTTP 调用最方便：
无需串口驱动、无需特殊网络库，浏览器 / 脚本 / 低代码平台都能直接调。

接口：
  GET  /api/status
  POST /api/modbus   {"unit":1,"fc":3,"addr":0,"count":3,"scale":0.1}
  POST /api/raw      {"hex":"01 03 00 00 00 03"}

用法：
  python http_client.py --host 192.168.1.100 --fc 3 --addr 0 --count 3
  python http_client.py --host 192.168.1.100 --fc 6 --addr 10 --value 1234
  python http_client.py --host 192.168.1.100 --raw "01 03 00 00 00 03 85 CB"
  python http_client.py --host 192.168.1.100 --status
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def api(host, port, path, obj=None, token="", timeout=10):
    url = f"http://{host}:{port}{path}"
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"无法连接 {url}：{e.reason}"}


def main():
    ap = argparse.ArgumentParser(description="REST API 客户端示例（B 机侧）")
    ap.add_argument("--host", required=True, help="A 机的 IP")
    ap.add_argument("--port", type=int, default=8080, help="A 机 HTTP 端口，默认 8080")
    ap.add_argument("--token", default="", help="API Token")
    ap.add_argument("--status", action="store_true", help="查询网关状态")
    ap.add_argument("--fc", type=int, default=3, help="功能码 1/2/3/4/5/6，默认 3")
    ap.add_argument("--unit", type=int, default=1, help="从站地址，默认 1")
    ap.add_argument("--addr", type=int, default=0, help="起始地址，默认 0")
    ap.add_argument("--count", type=int, default=1, help="读取数量，默认 1")
    ap.add_argument("--value", type=int, default=None, help="写入值（FC05/06）")
    ap.add_argument("--values", type=int, nargs="*", help="批量写入（FC16）")
    ap.add_argument("--scale", type=float, default=None, help="倍率换算")
    ap.add_argument("--signed", action="store_true", help="按有符号数解释寄存器")
    ap.add_argument("--raw", help="直接发十六进制帧到总线")
    a = ap.parse_args()

    if a.status:
        print(json.dumps(api(a.host, a.port, "/api/status", token=a.token),
                         ensure_ascii=False, indent=2))
        return

    if a.raw:
        r = api(a.host, a.port, "/api/raw", {"hex": a.raw}, a.token)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if a.fc in (5, 6):
        if a.value is None:
            ap.error("FC05/FC06 需要 --value")
        body = {"unit": a.unit, "fc": a.fc, "addr": a.addr, "value": a.value}
    elif a.fc == 16:
        if not a.values:
            ap.error("FC16 需要 --values")
        body = {"unit": a.unit, "fc": 16, "addr": a.addr, "values": a.values}
    else:
        body = {"unit": a.unit, "fc": a.fc, "addr": a.addr, "count": a.count}
        if a.scale is not None:
            body["scale"] = a.scale
        if a.signed:
            body["signed"] = True

    r = api(a.host, a.port, "/api/modbus", body, a.token)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if not r.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
