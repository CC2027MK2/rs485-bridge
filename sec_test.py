# -*- coding: utf-8 -*-
"""
安全回归测试：CORS 策略、Token 鉴权、请求体上限、内置面板、异常信息外泄。

与 selftest.py 一样不需要真实 485 硬件，用 socket:// 冒充串口。
运行： python sec_test.py
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, ".")
from selftest import FakeSlave, PY, GW

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))


def start(extra, port_slave):
    slave = FakeSlave(port_slave, "modbus")
    slave.start()
    p = subprocess.Popen([PY, GW, "--port", f"socket://127.0.0.1:{port_slave}",
                          "--mode", "http", "--http-port", "1601"] + extra,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    time.sleep(2)
    return slave, p


def raw_head(path, headers=None, method="GET", body=None):
    """返回 (status, 头字典文本, 响应体)。"""
    s = socket.create_connection(("127.0.0.1", 1601), timeout=5)
    req = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:1601\r\n"
    for k, v in (headers or {}).items():
        req += f"{k}: {v}\r\n"
    if body is not None:
        req += f"Content-Length: {len(body)}\r\n"
    req += "Connection: close\r\n\r\n"
    s.sendall(req.encode() + (body or b""))
    data = b""
    while True:
        c = s.recv(4096)
        if not c:
            break
        data += c
    s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("utf-8", "replace"), rest


print("=" * 64)
print(" 安全修复验证")
print("=" * 64)

# ---------- A. 默认不发 CORS 头 ----------
print("\n[A] 默认跨域策略（未指定 --cors）")
slave, p = start([], 16040)
try:
    head, _ = raw_head("/api/status")
    check("默认响应不含 Access-Control-Allow-Origin",
          "access-control-allow-origin" not in head.lower(), head.split("\r\n")[0])
    head, _ = raw_head("/api/status", {"Origin": "http://evil.example"}, method="OPTIONS")
    check("OPTIONS 预检也不放行任意来源",
          "access-control-allow-origin" not in head.lower())
    # 内置面板可访问
    head, body = raw_head("/panel")
    check("GET /panel 返回内置控制台", b"RS485" in body and "200" in head.split("\r\n")[0],
          f"{len(body)} 字节")
finally:
    p.terminate(); p.wait(timeout=5); slave.stop = True

# ---------- B. 白名单精确匹配 ----------
print("\n[B] --cors 白名单")
slave, p = start(["--cors", "http://localhost:3000,http://admin.lan"], 16041)
try:
    head, _ = raw_head("/api/status", {"Origin": "http://localhost:3000"})
    check("白名单内来源被回填",
          "access-control-allow-origin: http://localhost:3000" in head.lower())
    head, _ = raw_head("/api/status", {"Origin": "http://evil.example"})
    check("白名单外来源不回填",
          "access-control-allow-origin" not in head.lower())
finally:
    p.terminate(); p.wait(timeout=5); slave.stop = True

# ---------- C. Token 鉴权 ----------
print("\n[C] Token 鉴权与掩码")
slave, p = start(["--token", "secret-token-abc123"], 16042)
try:
    _, body = raw_head("/api/status")
    check("无 Token 返回 401", b"401" in body or b"token" in body)
    head, body = raw_head("/api/status", {"X-Token": "wrong"})
    check("错误 Token 被拒", "401" in head.split("\r\n")[0])
    head, body = raw_head("/api/status", {"X-Token": "secret-token-abc123"})
    check("正确 Token 通过", "200" in head.split("\r\n")[0])
finally:
    p.terminate()
    try:
        out, _ = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    slave.stop = True
    banner_text = out or ""
    check("启动横幅不明文打印 Token",
          "secret-token-abc123" not in banner_text,
          "横幅已掩码" if "secret-token-abc123" not in banner_text else "仍明文泄露")

# ---------- D. 请求体上限 ----------
print("\n[D] 请求体大小上限")
slave, p = start([], 16043)
try:
    big = b'{"hex":"' + b"41" * 40000 + b'"}'
    head, body = raw_head("/api/raw", {"Content-Type": "application/json"},
                          method="POST", body=big)
    check("超限请求体被拒", "500" in head.split("\r\n")[0] or "超过" in body.decode("utf-8", "replace"),
          head.split("\r\n")[0])
    check("拒绝时不回传内部异常类型", b"ValueError" not in body and b"Traceback" not in body)
finally:
    p.terminate(); p.wait(timeout=5); slave.stop = True

# ---------- E. 内部错误不外泄 ----------
print("\n[E] 异常细节不外泄")
slave, p = start([], 16044)
try:
    # 触发内部错误：非法功能码组合
    head, body = raw_head("/api/modbus", {"Content-Type": "application/json"},
                          method="POST", body=b'{"unit":"abc","fc":3}')
    check("异常响应不含 Python 类型名/Traceback",
          b"ValueError" not in body and b"Traceback" not in body and b"invalid literal" not in body,
          body[:80].decode("utf-8", "replace"))
finally:
    p.terminate(); p.wait(timeout=5); slave.stop = True

print("\n" + "=" * 64)
print(f" 通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print(f"   - 失败：{f}")
print("=" * 64)
sys.exit(0 if not FAIL else 1)
