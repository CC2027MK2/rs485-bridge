#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 机示例一：Modbus TCP 客户端（零依赖，只用标准库）
============================================================
A 机网关以 --mode modbus 运行时，B 机把它当成一台普通 Modbus TCP 设备。

用法：
  # 读 3 个保持寄存器（FC03），起始地址 0
  python modbus_tcp_client.py --host 192.168.1.100 --unit 1 --fc 3 --addr 0 --count 3

  # 读输入寄存器（FC04），带 0.1 倍率换算
  python modbus_tcp_client.py --host 192.168.1.100 --fc 4 --addr 100 --count 2 --scale 0.1

  # 读线圈（FC01）
  python modbus_tcp_client.py --host 192.168.1.100 --fc 1 --addr 0 --count 8

  # 写单个寄存器（FC06）
  python modbus_tcp_client.py --host 192.168.1.100 --fc 6 --addr 10 --value 1234

  # 写单个线圈（FC05）
  python modbus_tcp_client.py --host 192.168.1.100 --fc 5 --addr 3 --value 1

  # 连续轮询
  python modbus_tcp_client.py --host 192.168.1.100 --fc 3 --addr 0 --count 4 --loop 1.0
"""

import argparse
import socket
import struct
import sys
import time

EXC = {
    0x01: "非法功能码", 0x02: "非法数据地址", 0x03: "非法数据值",
    0x04: "从站设备故障", 0x05: "确认（处理中）", 0x06: "从站设备忙",
    0x08: "存储奇偶校验错", 0x0A: "网关路径不可用",
    0x0B: "网关目标设备无响应（485 侧超时）",
}


def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError("连接被对端关闭")
        b += c
    return b


class ModbusTCP:
    def __init__(self, host, port=502, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.tid = 0

    def request(self, unit, pdu):
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
        mbap = recv_exact(self.sock, 7)
        tid, pid, length, uid = struct.unpack(">HHHB", mbap)
        if tid != self.tid:
            raise RuntimeError(f"事务 ID 不符：发 {self.tid} 收 {tid}")
        return recv_exact(self.sock, length - 1)

    def read_regs(self, unit, fc, addr, count):
        pdu = self.request(unit, struct.pack(">BHH", fc, addr, count))
        if pdu[0] & 0x80:
            raise RuntimeError(f"Modbus 异常 0x{pdu[1]:02X}：{EXC.get(pdu[1], '未知')}")
        bc = pdu[1]
        data = pdu[2:2 + bc]
        return list(struct.unpack(">" + "H" * (bc // 2), data))

    def read_bits(self, unit, fc, addr, count):
        pdu = self.request(unit, struct.pack(">BHH", fc, addr, count))
        if pdu[0] & 0x80:
            raise RuntimeError(f"Modbus 异常 0x{pdu[1]:02X}：{EXC.get(pdu[1], '未知')}")
        bits = []
        for byte in pdu[2:2 + pdu[1]]:
            for i in range(8):
                bits.append((byte >> i) & 1)
        return bits[:count]

    def write_reg(self, unit, addr, value):
        pdu = self.request(unit, struct.pack(">BHH", 6, addr, value & 0xFFFF))
        return not (pdu[0] & 0x80)

    def write_coil(self, unit, addr, on):
        pdu = self.request(unit, struct.pack(">BHH", 5, addr, 0xFF00 if on else 0x0000))
        return not (pdu[0] & 0x80)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def poll_once(mb, a):
    if a.fc in (3, 4):
        vals = mb.read_regs(a.unit, a.fc, a.addr, a.count)
        raw = [f"{v}" for v in vals]
        if a.scale is not None:
            scaled = [round(v * a.scale, 4) for v in vals]
            print(f"  FC{a.fc} addr={a.addr} count={a.count} -> {scaled}  (原始 {raw})")
        else:
            print(f"  FC{a.fc} addr={a.addr} count={a.count} -> {vals}  "
                  f"(hex {' '.join(f'{v:04X}' for v in vals)})")
    elif a.fc in (1, 2):
        bits = mb.read_bits(a.unit, a.fc, a.addr, a.count)
        print(f"  FC{a.fc} addr={a.addr} count={a.count} -> {bits}")
    elif a.fc == 6:
        ok = mb.write_reg(a.unit, a.addr, a.value)
        print(f"  FC06 addr={a.addr} = {a.value} -> {'确认' if ok else '未确认'}")
    elif a.fc == 5:
        ok = mb.write_coil(a.unit, a.addr, bool(a.value))
        print(f"  FC05 addr={a.addr} = {a.value} -> {'确认' if ok else '未确认'}")
    else:
        raise SystemExit(f"本示例不支持功能码 {a.fc}")


def main():
    ap = argparse.ArgumentParser(description="Modbus TCP 客户端示例（B 机侧）")
    ap.add_argument("--host", required=True, help="A 机的 IP")
    ap.add_argument("--port", type=int, default=502, help="A 机网关端口，默认 502")
    ap.add_argument("--unit", type=int, default=1, help="从站地址，默认 1")
    ap.add_argument("--fc", type=int, default=3, help="功能码 1/2/3/4/5/6，默认 3")
    ap.add_argument("--addr", type=int, default=0, help="起始寄存器/线圈地址，默认 0")
    ap.add_argument("--count", type=int, default=1, help="读取数量，默认 1")
    ap.add_argument("--value", type=int, default=0, help="写入值（FC05/06）")
    ap.add_argument("--scale", type=float, default=None, help="倍率换算，如 0.1")
    ap.add_argument("--loop", type=float, default=None, help="轮询周期秒，如 1.0")
    a = ap.parse_args()

    mb = ModbusTCP(a.host, a.port)
    print(f"已连接 {a.host}:{a.port}  unit={a.unit}")
    try:
        if a.loop:
            while True:
                print(time.strftime("%H:%M:%S"), end="")
                try:
                    poll_once(mb, a)
                except Exception as e:
                    print(f"  错误: {e}")
                time.sleep(a.loop)
        else:
            poll_once(mb, a)
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
    finally:
        mb.close()


if __name__ == "__main__":
    main()
