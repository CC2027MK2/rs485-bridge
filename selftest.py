#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端自测：不用真实 485 硬件，验证网关的协议转换是否正确。

做法：
  1. 起一个"假从站"，用 TCP 扮演串口另一端的设备
     - modbus 角色：实现 FC01/02/03/04/05/06/16，从站地址 1 有效、2 静默（模拟挂死设备）
     - echo 角色：收到什么原样返回（用于 raw 模式）
  2. 网关以 socket://127.0.0.1:PORT 作为串口连接过去
  3. 测试客户端从另一侧按 Modbus TCP / 原始字节发请求，逐项断言

运行：
  python selftest.py
"""

import socket
import struct
import subprocess
import sys
import threading
import time

PY = sys.executable
GW = "rs485_bridge.py"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))


# ---------------------------------------------------------------- CRC
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def add_crc(f: bytes) -> bytes:
    c = crc16(f)
    return f + bytes([c & 0xFF, c >> 8])


def crc_ok(f: bytes) -> bool:
    return len(f) >= 4 and crc16(f[:-2]) == (f[-2] | f[-1] << 8)


# ---------------------------------------------------------------- 假从站
class FakeSlave(threading.Thread):
    def __init__(self, port, role="modbus"):
        super().__init__(daemon=True)
        self.port = port
        self.role = role
        self.regs = {0: 0x1234, 1: 0x5678, 2: 0x9ABC, 10: 111, 11: 222, 12: 333}
        self.coils = {0: 1, 1: 0, 2: 1, 3: 1}
        self.requests = 0
        self.stop = False
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(4)
        self.srv.settimeout(0.5)

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        conn.settimeout(0.05)
        buf = bytearray()
        try:
            while not self.stop:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                if self.role == "echo":
                    conn.sendall(bytes(buf))
                    buf.clear()
                    time.sleep(0.005)
                    continue
                # modbus 角色：按 RTU 规则切帧
                while len(buf) >= 4:
                    unit, fc = buf[0], buf[1]
                    need = self.frame_len(bytes(buf), fc)
                    if need is None or len(buf) < need:
                        break
                    frame = bytes(buf[:need])
                    del buf[:need]
                    self.requests += 1
                    time.sleep(0.003)          # 模拟设备处理耗时
                    if unit == 2:
                        continue               # 模拟挂死设备：不响应
                    resp = self.build(frame)
                    if resp:
                        conn.sendall(resp)
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def frame_len(buf: bytes, fc: int):
        if fc in (1, 2, 3, 4, 5, 6):
            return 8
        if fc in (15, 16):
            if len(buf) < 7:
                return None
            return 9 + buf[6]
        return 8

    def build(self, req: bytes) -> bytes:
        if not crc_ok(req):
            return b""
        unit, fc = req[0], req[1]
        addr = struct.unpack(">H", req[2:4])[0]

        if fc in (1, 2):
            cnt = struct.unpack(">H", req[4:6])[0]
            nbytes = (cnt + 7) // 8
            data = bytearray(nbytes)
            for i in range(cnt):
                if self.coils.get(addr + i, 0):
                    data[i // 8] |= 1 << (i % 8)
            return add_crc(bytes([unit, fc, nbytes]) + bytes(data))

        if fc in (3, 4):
            cnt = struct.unpack(">H", req[4:6])[0]
            data = b"".join(struct.pack(">H", self.regs.get(addr + i, 0)) for i in range(cnt))
            return add_crc(bytes([unit, fc, len(data)]) + data)

        if fc in (5, 6):
            val = struct.unpack(">H", req[4:6])[0]
            if fc == 6:
                self.regs[addr] = val
            else:
                self.coils[addr] = 1 if val == 0xFF00 else 0
            return add_crc(req[:6])

        if fc == 16:
            cnt = struct.unpack(">H", req[4:6])[0]
            for i in range(cnt):
                self.regs[addr + i] = struct.unpack(">H", req[7 + i * 2:9 + i * 2])[0]
            return add_crc(bytes([unit, fc]) + req[2:6])

        return add_crc(bytes([unit, fc | 0x80, 1]))


# ---------------------------------------------------------------- Modbus TCP 客户端
def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            return None
        b += c
    return b


def mb_request(sock, tid, uid, pdu):
    sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu)
    mbap = recv_exact(sock, 7)
    if not mbap:
        return None, None, None
    t, p, length, u = struct.unpack(">HHHB", mbap)
    return t, u, recv_exact(sock, length - 1)


def start_gateway(args, wait=2.0):
    p = subprocess.Popen([PY, GW] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    time.sleep(wait)
    return p


def kill(p):
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 主流程
def main():
    print("=" * 62)
    print(" RS485 网关 端到端自测")
    print("=" * 62)

    # ---------- 测试 1：Modbus 模式 ----------
    print("\n[1] Modbus RTU <-> Modbus TCP 协议转换")
    slave = FakeSlave(16000, "modbus")
    slave.start()
    gw = start_gateway(["--port", "socket://127.0.0.1:16000",
                        "--mode", "modbus", "--modbus-port", "1502",
                        "--baud", "9600", "--resp-timeout", "0.6"])
    try:
        s = socket.create_connection(("127.0.0.1", 1502), timeout=5)

        # FC03 读 3 个保持寄存器，起始 0
        # 响应 PDU = fc(1) + byte_count(1) + data(6) = 8 字节
        pdu = struct.pack(">BHH", 3, 0, 3)
        tid, uid, rpdu = mb_request(s, 0x1234, 1, pdu)
        ok = rpdu is not None and len(rpdu) == 8 and rpdu[0] == 3
        regs = struct.unpack(">HHH", rpdu[2:8]) if ok else ()
        check("FC03 读保持寄存器", ok and regs == (0x1234, 0x5678, 0x9ABC),
              f"tid=0x{tid:04X} regs={[hex(x) for x in regs]}")

        check("事务 ID 原样回填", tid == 0x1234, f"回填 0x{tid:04X}")

        # FC06 写单个寄存器，再读回
        pdu = struct.pack(">BHH", 6, 10, 4321)
        tid, uid, rpdu = mb_request(s, 0x2222, 1, pdu)
        check("FC06 写单寄存器回显", rpdu is not None and rpdu[0] == 6,
              f"pdu={rpdu.hex(' ').upper() if rpdu else None}")
        pdu = struct.pack(">BHH", 3, 10, 1)
        tid, uid, rpdu = mb_request(s, 0x2223, 1, pdu)
        val = struct.unpack(">H", rpdu[2:4])[0] if rpdu and len(rpdu) >= 4 else -1
        check("写入值可读回", val == 4321, f"读到 {val}")

        # FC01 读线圈
        pdu = struct.pack(">BHH", 1, 0, 4)
        tid, uid, rpdu = mb_request(s, 0x3333, 1, pdu)
        bits = [(rpdu[2] >> i) & 1 for i in range(4)] if rpdu and len(rpdu) >= 3 else []
        check("FC01 读线圈", bits == [1, 0, 1, 1], f"bits={bits}")

        # 从站 2 静默 -> 应返回异常码 0x0B（网关目标设备无响应）
        pdu = struct.pack(">BHH", 3, 0, 1)
        tid, uid, rpdu = mb_request(s, 0x4444, 2, pdu)
        is_exc = rpdu is not None and rpdu[0] == 0x83 and rpdu[1] == 0x0B
        check("从站无响应时返回异常码 0x0B", is_exc, f"pdu={rpdu.hex() if rpdu else None}")

        # 连续 20 次事务，确认事务 ID 不串、总线锁有效
        ok_all = True
        for i in range(20):
            pdu = struct.pack(">BHH", 3, 10, 1)
            tid, uid, rpdu = mb_request(s, 0x5000 + i, 1, pdu)
            if not rpdu or len(rpdu) < 4 or struct.unpack(">H", rpdu[2:4])[0] != 4321:
                ok_all = False
                break
        check("连续 20 次事务无错帧", ok_all)

        s.close()
    finally:
        kill(gw)
        slave.stop = True

    # ---------- 测试 2：raw 模式 ----------
    print("\n[2] raw 原始字节透传")
    slave2 = FakeSlave(16001, "echo")
    slave2.start()
    gw2 = start_gateway(["--port", "socket://127.0.0.1:16001",
                         "--mode", "raw", "--tcp-port", "1501",
                         "--idle-ms", "20", "--resp-timeout", "0.5"])
    try:
        s = socket.create_connection(("127.0.0.1", 1501), timeout=5)
        s.settimeout(3)
        payload = bytes.fromhex("01030000000A")
        s.sendall(payload)
        got = recv_exact(s, len(payload))
        check("raw 模式原样往返", got == payload,
              f"发 {payload.hex(' ').upper()} 收 {got.hex(' ').upper() if got else None}")
        s.close()
    finally:
        kill(gw2)
        slave2.stop = True

    # ---------- 测试 3：HTTP 模式 ----------
    print("\n[3] REST API")
    import urllib.request

    slave3 = FakeSlave(16002, "modbus")
    slave3.start()
    gw3 = start_gateway(["--port", "socket://127.0.0.1:16002",
                         "--mode", "http", "--http-port", "1503",
                         "--token", "t0k", "--resp-timeout", "0.6"])
    try:
        def post(path, obj):
            req = urllib.request.Request(
                "http://127.0.0.1:1503" + path,
                data=__import__("json").dumps(obj).encode(),
                headers={"Content-Type": "application/json", "X-Token": "t0k"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return __import__("json").loads(r.read().decode())

        r = post("/api/modbus", {"unit": 1, "fc": 3, "addr": 0, "count": 3})
        check("POST /api/modbus 读寄存器",
              r.get("ok") and r.get("regs") == [0x1234, 0x5678, 0x9ABC],
              f"regs={[hex(x) for x in r.get('regs', [])]}")

        r = post("/api/modbus", {"unit": 1, "fc": 3, "addr": 10, "count": 3,
                                 "scale": 0.1})
        check("scale 换算", r.get("values") == [11.1, 22.2, 33.3], f"{r.get('values')}")

        r = post("/api/raw", {"hex": add_crc(bytes([1, 3, 0, 0, 0, 3])).hex(" ").upper()})
        check("POST /api/raw", r.get("ok") and r.get("rx_len", 0) > 0,
              f"rx={r.get('rx')}")

        # token 错误应被拒
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:1503/api/status",
                headers={"X-Token": "wrong"})
            urllib.request.urlopen(req, timeout=5)
            rejected = False
        except urllib.error.HTTPError as e:
            rejected = e.code == 401
        check("错误 token 返回 401", rejected)
    finally:
        kill(gw3)
        slave3.stop = True

    # ---------- 汇总 ----------
    print("\n" + "=" * 62)
    print(f" 通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"   - 失败：{f}")
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
