#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 机示例二：raw 原始字节透传客户端
============================================================
A 机网关以 --mode raw 运行时，TCP 连接等同于一根 RS485 线：
发什么字节就上总线，总线回什么字节就原样返回。

适合：私有协议、DL/T645 电表、自定义物联网模块、调试抓包。

用法：
  # 发一帧十六进制
  python raw_socket_client.py --host 192.168.1.100 --port 5000 --hex "01 03 00 00 00 0A C5 CD"

  # 交互模式：一行一帧，直接敲 hex
  python raw_socket_client.py --host 192.168.1.100 --port 5000 --interactive

  # 把响应存成文件
  python raw_socket_client.py --host 192.168.1.100 --port 5000 --hex "..." --out resp.bin
"""

import argparse
import socket
import sys
import time


def recv_all(sock, idle_ms=200, max_wait=2.0):
    """读到总线静默为止（485 一问一答场景下等价于读完一整帧响应）。"""
    sock.settimeout(idle_ms / 1000.0)
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf.extend(chunk)
        t0 = time.time() - 0.0001   # 收到数据就重置静默计时
        if len(buf) > 8192:
            break
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser(description="raw 透传客户端示例（B 机侧）")
    ap.add_argument("--host", required=True, help="A 机的 IP")
    ap.add_argument("--port", type=int, default=5000, help="A 机网关端口，默认 5000")
    ap.add_argument("--hex", help="要发送的十六进制帧，如 '01 03 00 00 00 0A'")
    ap.add_argument("--interactive", "-i", action="store_true", help="交互模式")
    ap.add_argument("--out", help="把响应写入文件")
    a = ap.parse_args()

    if not a.hex and not a.interactive:
        ap.error("需要 --hex 或 --interactive")

    sock = socket.create_connection((a.host, a.port), timeout=5)
    print(f"已连接 {a.host}:{a.port}")

    def send_one(h):
        h = "".join(h.split()).replace("0x", "")
        try:
            payload = bytes.fromhex(h)
        except ValueError:
            print("  hex 格式错误")
            return b""
        t0 = time.time()
        sock.sendall(payload)
        resp = recv_all(sock)
        ms = (time.time() - t0) * 1000
        print(f"  TX {payload.hex(' ').upper()}")
        print(f"  RX {resp.hex(' ').upper() if resp else '(无响应)'}   {len(resp)} 字节  {ms:.1f}ms")
        return resp

    try:
        if a.interactive:
            print("输入十六进制帧，回车发送，空行或不输入 q 退出\n")
            while True:
                try:
                    line = input("hex> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line or line.lower() == "q":
                    break
                send_one(line)
        else:
            resp = send_one(a.hex)
            if a.out and resp:
                with open(a.out, "wb") as f:
                    f.write(resp)
                print(f"  已保存到 {a.out}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
