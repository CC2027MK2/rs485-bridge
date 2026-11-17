#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RS485 网络网关 / RS485-to-Network Bridge
============================================================
场景：A 机有 RS485 接口，B 机没有，两者在同一局域网。
     在 A 机运行本程序，B 机即可通过网络操控 RS485 总线上的物联网设备。

三种工作模式（--mode）：

  raw     TCP <-> 串口 原始字节双向透传
          适合私有协议、未知协议、需要抓包调试的场合。
          B 机拿到的是一个"透明管道"，发什么就上总线，总线上回来什么就发回。

  modbus  Modbus TCP <-> Modbus RTU 协议转换
          B 机用标准 Modbus TCP 客户端（pymodbus / Modbus Poll / SCADA /
          Node-RED / 组态王）直连，协议层由本程序翻译。
          这是工业现场最省事的做法，B 机侧代码改动最小。

  http    REST API
          适合网页前端、脚本、低代码平台。用 POST /api/modbus 或 /api/raw 调用。

依赖：pyserial（pip install pyserial）

启动示例：
  python rs485_bridge.py --list
  python rs485_bridge.py --port COM7 --baud 9600 --mode modbus
  python rs485_bridge.py --port COM7 --baud 9600 --mode raw --tcp-port 5000
  python rs485_bridge.py --port COM7 --baud 9600 --mode http --http-port 8080 --token mytoken

作者：巴蒂
"""

import argparse
import hmac
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.stderr.write("缺少 pyserial，请先执行：pip install pyserial\n")
    sys.exit(1)

# ---------------------------------------------------------------- 控制台编码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 内置面板
def _load_panel() -> str:
    """
    读取同目录下的 panel.html，供 HTTP 模式在 /panel 路径直接提供。
    页面与接口同源，因此不需要开启 CORS 也能正常调用。
    找不到就返回空串，功能降级但不影响其它模式。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("panel.html", os.path.join("B端示例", "panel.html")):
        p = os.path.join(here, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return ""


PANEL_HTML = _load_panel()


# ---------------------------------------------------------------- 工具函数
def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(tag: str, msg: str) -> None:
    print(f"[{ts()}] [{tag}] {msg}", flush=True)


def hexs(b: bytes) -> str:
    return b.hex(" ").upper() if b else ""


def crc16_modbus(data: bytes) -> int:
    """标准 Modbus CRC16，多项式 0xA001，初值 0xFFFF。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def add_crc(frame: bytes) -> bytes:
    """在 RTU 帧（unit + pdu）尾部追加 CRC，低字节在前。"""
    c = crc16_modbus(frame)
    return frame + bytes([c & 0xFF, (c >> 8) & 0xFF])


def check_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return crc16_modbus(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


def local_ipv4() -> list:
    """列出本机可用于对外服务的 IPv4 地址（用于启动横幅提示 B 机填什么地址）。"""
    out = []
    try:
        import subprocess
        # 用 hostname 解析，不依赖第三方库
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out:
                out.append(ip)
    except Exception:
        pass
    # 兜底：通过 UDP 探测默认出口地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in out:
            out.append(ip)
    except Exception:
        pass
    return [ip for ip in out if not ip.startswith("127.")]


# ---------------------------------------------------------------- 串口总线层
class SerialBus:
    """
    RS485 是半双工总线：同一时刻只允许一个事务在总线上跑。
    所有访问都必须经过 self.lock，这是物理约束，不是实现选择。
    吞吐上限 = 1 / 单次事务耗时（含帧间隔与设备响应时间）。
    """

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.ser = None
        self.open_count = 0
        self._open()

    # ---- 打开 / 重连
    def _open(self) -> None:
        a = self.args
        kwargs = dict(
            baudrate=a.baud,
            bytesize=a.databits,
            parity=a.parity,
            stopbits=a.stopbits,
            timeout=0.002,       # 短超时，靠循环累积
            write_timeout=3,
        )
        if "://" in a.port:
            # 支持 pyserial URL：loop:// 回环、socket://host:port 连串口服务器、rfc2217://
            self.ser = serial.serial_for_url(a.port, **kwargs)
        else:
            self.ser = serial.Serial(port=a.port, **kwargs)
        # 关键优化：USB 转串口芯片默认轮询延迟 16ms，会把"发完立刻读"拖慢一个数量级。
        # CH340 / FTDI / CP210x 大多支持降延迟，失败不影响功能。
        try:
            self.ser.set_latency_timer(1)
        except Exception:
            pass
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        self.open_count += 1
        log("SERIAL", f"已打开 {a.port} @ {a.baud} {a.databits}{a.parity}{a.stopbits:g}"
                     f"  (第 {self.open_count} 次打开)")

    def _reopen(self) -> None:
        log("SERIAL", "串口异常，尝试重连 ...")
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        time.sleep(0.5)
        self._open()

    # ---- 底层写（含 485 方向切换）
    def _write(self, data: bytes) -> None:
        a = self.args
        self.ser.reset_input_buffer()
        if a.rts == "toggle":
            # 部分 MAX485 模块需要手动控制 DE/RE
            self.ser.rts = True
            time.sleep(0.001)
        self.ser.write(data)
        self.ser.flush()
        if a.rts == "toggle":
            # 必须在设备回话之前释放总线
            time.sleep(a.turnaround_ms / 1000.0)
            self.ser.rts = False
        else:
            # 自动收发模块只需给总线一点空闲时间
            time.sleep(a.turnaround_ms / 1000.0)

    # ---- 精确读 N 字节
    def _read_exact(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        deadline = time.time() + timeout
        while len(buf) < n:
            remain = deadline - time.time()
            if remain <= 0:
                break
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    # ---- 读到总线静默为止
    def _read_until_idle(self, idle_ms: int, max_wait: float) -> bytes:
        buf = bytearray()
        t0 = last = time.time()
        while True:
            chunk = self.ser.read(4096)
            now = time.time()
            if chunk:
                buf.extend(chunk)
                last = now
            elif buf and (now - last) * 1000.0 >= idle_ms:
                break
            if now - t0 > max_wait:
                break
        return bytes(buf)

    # ---- 通用事务入口（带锁 + 自动重连）
    def transact(self, request: bytes, reader) -> bytes:
        """
        request: 要写进总线的字节
        reader : 可调用对象，负责读取响应，签名 () -> bytes
        """
        with self.lock:
            for attempt in (1, 2):
                try:
                    time.sleep(self.args.inter_frame_ms / 1000.0)
                    self._write(request)
                    if self.args.log:
                        log("TX", hexs(request))
                    resp = reader()
                    if self.args.log:
                        log("RX", hexs(resp) if resp else "(空)")
                    return resp
                except (serial.SerialException, OSError) as e:
                    log("SERIAL", f"第 {attempt} 次事务失败: {e}")
                    if attempt == 2:
                        raise
                    self._reopen()
        return b""

    # ---- raw 模式事务
    def transact_raw(self, request: bytes) -> bytes:
        def reader():
            return self._read_until_idle(self.args.idle_ms, self.args.resp_timeout)
        return self.transact(request, reader)

    # ---- modbus 模式事务（按功能码精确判断响应长度，比靠超时可靠）
    def transact_modbus(self, request_rtu: bytes) -> bytes:
        unit = request_rtu[0]
        fc = request_rtu[1]
        payload = request_rtu[2:-2]   # 去掉 unit 与 crc

        def reader():
            head = self._read_exact(2, self.args.resp_timeout)
            if len(head) < 2:
                return b""
            rfc = head[1]
            if rfc & 0x80:
                # 异常响应：unit + (fc|0x80) + code + crc2
                return head + self._read_exact(3, self.args.frame_timeout)
            if rfc in (0x01, 0x02, 0x03, 0x04):
                bc = self._read_exact(1, self.args.frame_timeout)
                if not bc:
                    return head
                return head + bc + self._read_exact(bc[0] + 2, self.args.frame_timeout)
            if rfc in (0x05, 0x06, 0x0F, 0x10):
                # 回显请求：unit + fc + addr2 + value2 + crc2
                return head + self._read_exact(6, self.args.frame_timeout)
            # 未知功能码，退回空闲判定
            return head + self._read_until_idle(self.args.idle_ms, self.args.frame_timeout)

        return self.transact(request_rtu, reader)

    # ---- 状态
    def status(self) -> dict:
        return {
            "port": self.args.port,
            "baud": self.args.baud,
            "frame": f"{self.args.databits}{self.args.parity}{self.args.stopbits:g}",
            "is_open": bool(self.ser and self.ser.is_open),
            "open_count": self.open_count,
            "mode": self.args.mode,
        }

    def close(self) -> None:
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------- IP 白名单
def ip_allowed(peer: str, allow: list) -> bool:
    if not allow:
        return True
    return peer in allow


def peer_of(handler) -> str:
    try:
        return handler.client_address[0]
    except Exception:
        return "?"


# ---------------------------------------------------------------- 模式一：raw
class RawTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        peer = self.client_address[0]
        bus: SerialBus = self.server.bus
        if not ip_allowed(peer, self.server.allow):
            log("RAW", f"拒绝 {peer}（不在白名单）")
            return
        log("RAW", f"{peer} 已连接")
        sock.settimeout(0.2)
        try:
            while True:
                try:
                    first = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not first:
                    break

                # 打包：连续到达的短包合并成一帧（等同串口服务器的"打包时间"）
                buf = bytearray(first)
                deadline = time.time() + self.server.pack_ms / 1000.0
                sock.settimeout(self.server.pack_ms / 1000.0)
                while len(buf) < self.server.max_pack:
                    try:
                        more = sock.recv(4096)
                    except socket.timeout:
                        break
                    except OSError:
                        more = b""
                    if not more:
                        break
                    buf.extend(more)
                    if time.time() > deadline + 1.0:
                        break
                sock.settimeout(0.2)

                resp = bus.transact_raw(bytes(buf))
                if resp:
                    try:
                        sock.sendall(resp)
                    except OSError:
                        break
        finally:
            log("RAW", f"{peer} 断开")
            try:
                sock.close()
            except Exception:
                pass


class RawTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, bus, allow, pack_ms, max_pack):
        self.bus = bus
        self.allow = allow
        self.pack_ms = pack_ms
        self.max_pack = max_pack
        super().__init__(addr, handler)


# ---------------------------------------------------------------- 模式二：modbus
def recv_exact(sock, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class ModbusTCPHandler(socketserver.BaseRequestHandler):
    """
    MBAP (7B) + PDU  <->  UnitID + PDU + CRC16
    MBAP: TransactionID(2) ProtocolID(2) Length(2) UnitID(1)
    """

    def handle(self):
        sock = self.request
        peer = self.client_address[0]
        bus: SerialBus = self.server.bus
        if not ip_allowed(peer, self.server.allow):
            log("MBTCP", f"拒绝 {peer}（不在白名单）")
            return
        log("MBTCP", f"{peer} 已连接")
        try:
            while True:
                mbap = recv_exact(sock, 7)
                if not mbap:
                    break
                tid, pid, length, uid = struct.unpack(">HHHB", mbap)
                if pid != 0 or length < 2:
                    log("MBTCP", f"非法 MBAP: tid={tid} pid={pid} len={length}")
                    break
                pdu = recv_exact(sock, length - 1)
                if pdu is None:
                    break

                rtu_req = add_crc(bytes([uid]) + pdu)
                try:
                    rtu_resp = bus.transact_modbus(rtu_req)
                except Exception as e:
                    log("MBTCP", f"总线异常: {e}")
                    rtu_resp = b""

                if not rtu_resp:
                    # Modbus 标准异常码 0x0B: Gateway target device failed to respond
                    pdu_resp = bytes([pdu[0] | 0x80, 0x0B])
                elif not check_crc(rtu_resp):
                    log("MBTCP", f"CRC 校验失败: {hexs(rtu_resp)}")
                    pdu_resp = bytes([pdu[0] | 0x80, 0x04])   # SLAVE DEVICE FAILURE
                elif rtu_resp[0] != uid:
                    log("MBTCP", f"从站地址不符 期望 {uid} 实收 {rtu_resp[0]}")
                    pdu_resp = bytes([pdu[0] | 0x80, 0x04])
                else:
                    pdu_resp = rtu_resp[1:-2]

                out = struct.pack(">HHHB", tid, 0, len(pdu_resp) + 1, uid) + pdu_resp
                sock.sendall(out)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log("MBTCP", "处理异常:\n" + traceback.format_exc())
        finally:
            log("MBTCP", f"{peer} 断开")
            try:
                sock.close()
            except Exception:
                pass


class ModbusTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, bus, allow):
        self.bus = bus
        self.allow = allow
        super().__init__(addr, ModbusTCPHandler)


# ---------------------------------------------------------------- 模式三：http
class HTTPHandler(BaseHTTPRequestHandler):
    server_version = "rs485-bridge/1.0"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            log("HTTP", f"{self.client_address[0]} {fmt % args}")

    # ---- 响应工具
    def _cors_origin(self):
        """
        返回应回填的 Origin。默认不发 CORS 头。

        规则：
          - 未配置 --cors 时，不发送任何 Access-Control-Allow-Origin。
            这样浏览器仍可同源访问（例如直接打开本机页面），但第三方网站
            发起的跨域请求会被浏览器拦截，避免任意网页驱动 485 总线。
          - --cors "*" 表示显式放行所有来源，仅建议在完全隔离的实验网段使用。
          - 其余情况按逗号分隔的白名单精确匹配 Origin，匹配才回填。
        """
        allow = getattr(self.server, "cors", "")
        if not allow:
            return None
        origin = self.headers.get("Origin", "")
        if allow.strip() == "*":
            return "*"
        items = [x.strip() for x in allow.split(",") if x.strip()]
        return origin if origin in items else None

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            if origin != "*":
                self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        token = self.server.token
        if not token:
            return True
        got = self.headers.get("X-Token", "")
        if not got:
            q = self.path.split("?", 1)
            if len(q) == 2:
                for kv in q[1].split("&"):
                    if kv.startswith("token="):
                        got = kv[6:]
        # 常量时间比较，避免通过响应时间差逐字节试探口令
        if not hmac.compare_digest(got.encode("utf-8", "replace"),
                                   token.encode("utf-8", "replace")):
            self._send(401, {"ok": False, "error": "token 无效"})
            return False
        return True

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        # 限制请求体大小：本网关的正常请求只有几十字节，超过 64KB 一律拒绝
        if n > self.server.max_body:
            raise ValueError(f"请求体超过 {self.server.max_body} 字节上限")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    # ---- 路由
    def do_OPTIONS(self):
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
            if origin != "*":
                self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # 内置控制台页面：同源提供，因此不需要放开 CORS。
        # 这也是推荐用法——浏览器直接打开 A 机地址即可，不必把 panel.html 拷来拷去。
        if path in ("/panel", "/panel.html"):
            body = PANEL_HTML.encode("utf-8") if PANEL_HTML else None
            if body is None:
                self._send(404, {"ok": False,
                                 "error": "未找到内置面板，请把 panel.html 放到脚本同目录"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._auth():
            return
        if path in ("/", "/api/status"):
            st = self.server.bus.status()
            st["ok"] = True
            st["endpoints"] = ["GET /api/status", "POST /api/raw",
                               "POST /api/modbus", "GET /panel"]
            self._send(200, st)
        else:
            self._send(404, {"ok": False, "error": "未知路径"})

    def do_POST(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        bus: SerialBus = self.server.bus
        try:
            if path == "/api/raw":
                self._handle_raw(bus)
            elif path == "/api/modbus":
                self._handle_modbus(bus)
            else:
                self._send(404, {"ok": False, "error": "未知路径"})
        except Exception as e:
            # 异常细节只进本地日志，不回传给客户端，避免泄露路径与实现细节
            log("HTTP", f"处理 {path} 出错:\n{traceback.format_exc()}")
            self._send(500, {"ok": False, "error": "内部错误，详见网关日志"})

    # ---- /api/raw
    def _handle_raw(self, bus: SerialBus) -> None:
        body = self._body()
        h = body.get("hex") or body.get("_raw", "")
        h = "".join(h.split()).replace("0x", "")
        if not h:
            self._send(400, {"ok": False, "error": "缺少 hex 字段"})
            return
        try:
            req = bytes.fromhex(h)
        except ValueError:
            self._send(400, {"ok": False, "error": "hex 格式错误"})
            return
        t0 = time.time()
        resp = bus.transact_raw(req)
        self._send(200, {
            "ok": True,
            "tx": req.hex(" ").upper(),
            "rx": resp.hex(" ").upper(),
            "rx_len": len(resp),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        })

    # ---- /api/modbus
    def _handle_modbus(self, bus: SerialBus) -> None:
        b = self._body()
        unit = int(b.get("unit", 1))
        fc = int(b.get("fc", b.get("function", 3)))
        addr = int(b.get("addr", b.get("address", 0)))

        if fc in (1, 2, 3, 4):
            count = int(b.get("count", 1))
            req = struct.pack(">BBHH", unit, fc, addr, count)
            resp = bus.transact_modbus(add_crc(req))
            if not resp:
                self._send(200, {"ok": False, "error": "设备无响应 / 总线超时"})
                return
            if not check_crc(resp):
                self._send(200, {"ok": False, "error": "CRC 错误", "rx": resp.hex(" ").upper()})
                return
            if resp[1] & 0x80:
                self._send(200, {"ok": False, "exception": resp[2],
                                 "error": f"Modbus 异常码 0x{resp[2]:02X}",
                                 "rx": resp.hex(" ").upper()})
                return
            bc = resp[2]
            data = resp[3:3 + bc]
            out = {"ok": True, "unit": unit, "fc": fc, "addr": addr,
                   "raw": data.hex(" ").upper()}
            if fc in (3, 4):
                out["regs"] = list(struct.unpack(">" + "H" * (bc // 2), data[:bc // 2 * 2]))
                if b.get("signed"):
                    out["regs"] = [v - 0x10000 if v >= 0x8000 else v for v in out["regs"]]
                if b.get("scale") is not None:
                    s = float(b["scale"])
                    out["values"] = [round(v * s, 6) for v in out["regs"]]
            elif fc in (1, 2):
                bits = []
                for byte in data:
                    for i in range(8):
                        bits.append((byte >> i) & 1)
                out["bits"] = bits[:count]
            self._send(200, out)

        elif fc == 6:
            value = int(b.get("value", 0))
            req = struct.pack(">BBHH", unit, fc, addr, value)
            resp = bus.transact_modbus(add_crc(req))
            ok = bool(resp) and check_crc(resp)
            self._send(200, {"ok": ok, "rx": resp.hex(" ").upper() if resp else "",
                             "error": None if ok else "写入未确认"})

        elif fc == 5:
            value = 0xFF00 if b.get("value") in (1, True, "1", "on") else 0x0000
            req = struct.pack(">BBHH", unit, fc, addr, value)
            resp = bus.transact_modbus(add_crc(req))
            ok = bool(resp) and check_crc(resp)
            self._send(200, {"ok": ok, "rx": resp.hex(" ").upper() if resp else "",
                             "error": None if ok else "写入未确认"})

        elif fc in (15, 16):
            values = b.get("values") or []
            if fc == 16:
                data = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
            else:
                # 位写入：按字节打包
                raw = bytearray((len(values) + 7) // 8)
                for i, v in enumerate(values):
                    if int(v):
                        raw[i // 8] |= 1 << (i % 8)
                data = bytes(raw)
            req = struct.pack(">BBHHB", unit, fc, addr, len(values), len(data)) + data
            resp = bus.transact_modbus(add_crc(req))
            ok = bool(resp) and check_crc(resp)
            self._send(200, {"ok": ok, "rx": resp.hex(" ").upper() if resp else "",
                             "error": None if ok else "写入未确认"})
        else:
            self._send(400, {"ok": False, "error": f"不支持的功能码 {fc}"})


class HTTPGateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, bus, token, verbose, cors="", max_body=65536):
        self.bus = bus
        self.token = token
        self.cors = cors
        self.max_body = max_body
        self.verbose = verbose
        super().__init__(addr, HTTPHandler)


# ---------------------------------------------------------------- 启动
def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("未发现任何串口设备。请检查 USB-485 转换器是否插好、驱动是否安装。")
        return
    print(f"发现 {len(ports)} 个串口：\n")
    print(f"{'端口':<8}{'描述':<40}{'硬件ID'}")
    print("-" * 88)
    for p in ports:
        print(f"{p.device:<8}{(p.description or ''):<40}{p.hwid}")
    print("\n提示：USB-485 转换器通常显示为 CH340 / CH343 / FT232 / CP210x / USB-SERIAL。")


def banner(args, bus: SerialBus, listen_port: int, proto: str) -> None:
    ips = local_ipv4()
    print("")
    print("=" * 66)
    print("  RS485 网络网关已启动")
    print("=" * 66)
    print(f"  串口      : {args.port} @ {args.baud} {args.databits}{args.parity}{args.stopbits:g}")
    print(f"  模式      : {args.mode}")
    print(f"  协议      : {proto}")
    print(f"  监听      : {args.listen}:{listen_port}")
    print(f"  485 方向  : {args.rts}   帧间隔 {args.inter_frame_ms}ms   响应超时 {args.resp_timeout}s")
    if args.allow:
        print(f"  IP 白名单 : {', '.join(args.allow)}")
    if args.mode == "http" and args.token:
        # 只露头尾，避免启动日志/截图把完整口令带出去
        t = args.token
        masked = (t[:2] + "*" * max(3, len(t) - 4) + t[-2:]) if len(t) > 6 else "***"
        print(f"  API Token : {masked}（已设置）")
    if args.mode == "http":
        cors = args.cors or "关闭（同源可用，跨域被浏览器拦截）"
        print(f"  跨域策略  : {cors}")
    print("-" * 66)
    print("  B 机请连接以下任一地址：")
    for ip in ips:
        print(f"      {ip}:{listen_port}")
    if not ips:
        print("      (未探测到局域网 IP，请用 ipconfig 查看)")
    print("=" * 66)
    print("  Ctrl+C 停止\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RS485 网络网关：把 A 机的 RS485 接口共享给局域网内的 B 机使用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python rs485_bridge.py --list
  python rs485_bridge.py --port COM7 --baud 9600 --mode modbus
  python rs485_bridge.py --port COM7 --baud 9600 --mode raw --tcp-port 5000
  python rs485_bridge.py --port COM7 --baud 9600 --mode http --token abc123 --allow 192.168.1.200
        """)

    ap.add_argument("--list", action="store_true", help="列出本机所有串口后退出")
    ap.add_argument("--port",
                    help="串口名：Windows 如 COM7，Linux 如 /dev/ttyUSB0；"
                         "也可用 pyserial URL：socket://ip:port、loop://")
    ap.add_argument("--baud", type=int, default=9600, help="波特率，默认 9600")
    ap.add_argument("--databits", type=int, default=8, choices=[5, 6, 7, 8], help="数据位，默认 8")
    ap.add_argument("--parity", default="N", choices=["N", "E", "O", "M", "S"],
                    help="校验位 N/E/O/M/S，默认 N")
    ap.add_argument("--stopbits", type=float, default=1, choices=[1, 1.5, 2], help="停止位，默认 1")

    ap.add_argument("--mode", default="modbus", choices=["raw", "modbus", "http"],
                    help="工作模式，默认 modbus")
    ap.add_argument("--listen", default="0.0.0.0", help="监听地址，默认 0.0.0.0（所有网卡）")
    ap.add_argument("--tcp-port", type=int, default=5000, help="raw 模式端口，默认 5000")
    ap.add_argument("--modbus-port", type=int, default=502, help="modbus 模式端口，默认 502")
    ap.add_argument("--http-port", type=int, default=8080, help="http 模式端口，默认 8080")

    ap.add_argument("--idle-ms", type=int, default=20,
                    help="raw 模式帧尾判定静默时间(ms)，默认 20")
    ap.add_argument("--resp-timeout", type=float, default=1.0,
                    help="等待设备响应的总超时(秒)，默认 1.0")
    ap.add_argument("--frame-timeout", type=float, default=0.3,
                    help="帧内字节间超时(秒)，默认 0.3")
    ap.add_argument("--turnaround-ms", type=float, default=1.0,
                    help="485 收发切换延时(ms)，默认 1.0")
    ap.add_argument("--inter-frame-ms", type=float, default=4.0,
                    help="两次事务之间的总线空闲(ms)，默认 4.0（9600bps 下 3.5 字符约 4ms）")
    ap.add_argument("--rts", default="auto", choices=["auto", "toggle"],
                    help="485 方向控制：auto=自动收发模块；toggle=用 RTS 手动切换 DE/RE")
    ap.add_argument("--pack-ms", type=int, default=20,
                    help="raw 模式 TCP 侧打包时间(ms)，默认 20")
    ap.add_argument("--max-pack", type=int, default=512, help="单帧最大字节数，默认 512")

    ap.add_argument("--allow", nargs="*", default=[], help="IP 白名单，留空则不限制")
    ap.add_argument("--token", default="", help="HTTP 模式 API Token")
    ap.add_argument("--cors", default="",
                    help="HTTP 模式允许的跨域来源，逗号分隔，如 http://localhost:3000。"
                         "留空则不发 CORS 头（推荐）；填 * 放行所有来源（不安全）")
    ap.add_argument("--max-body", type=int, default=65536,
                    help="HTTP 请求体大小上限（字节），默认 65536")
    ap.add_argument("--log", action="store_true", help="打印每一帧收发的十六进制")

    args = ap.parse_args()

    if args.list:
        list_ports()
        return
    if not args.port:
        ap.error("必须指定 --port（可先用 --list 查看可用串口）")

    bus = SerialBus(args)

    if args.mode == "raw":
        srv = RawTCPServer((args.listen, args.tcp_port), RawTCPHandler,
                           bus, args.allow, args.pack_ms, args.max_pack)
        banner(args, bus, args.tcp_port, "原始字节透传 (TCP)")

    elif args.mode == "modbus":
        srv = ModbusTCPServer((args.listen, args.modbus_port), bus, args.allow)
        banner(args, bus, args.modbus_port, "Modbus TCP -> Modbus RTU")

    else:
        srv = HTTPGateway((args.listen, args.http_port), bus, args.token, True,
                          cors=args.cors, max_body=args.max_body)
        banner(args, bus, args.http_port, "REST API (HTTP)")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出 ...")
    finally:
        srv.server_close()
        bus.close()
        log("EXIT", "已停止")


if __name__ == "__main__":
    main()
