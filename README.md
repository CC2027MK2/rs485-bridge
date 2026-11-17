# RS485 网络网关

让没有 485 接口的 B 机，通过同一局域网里 A 机的 485 接口，操控总线上的物联网设备。

零硬件成本，纯 Python，一个文件，依赖只有 `pyserial`。

---

## 一、先搞清楚你要哪种模式

| 你手上的设备 | 用哪个模式 | B 机怎么访问 |
|---|---|---|
| **Modbus RTU**（最常见：电表、温控器、PLC、变频器、传感器） | `modbus` | 当成一台标准 Modbus TCP 设备，端口 502 |
| **私有协议 / 未知协议 / 想先抓包** | `raw` | 当成一根"网络串口线"，发字节收字节 |
| **要让网页、低代码平台、Excel 直接调** | `http` | HTTP 接口，浏览器/脚本都能用 |

不确定设备用什么协议？先用 `raw` 模式抓一帧看看。Modbus RTU 的帧特征很明显：功能码 01–06/0F/10，帧尾两字节是 CRC。

---

## 二、A 机部署（三步）

### 1. 确认串口

```bash
pip install pyserial
python rs485_bridge.py --list
```

输出示例：

```
发现 3 个串口：

端口      描述                                    硬件ID
----------------------------------------------------------------------------------------
COM1      Communications Port (COM1)             ACPI\PNP0501\1
COM7      USB-SERIAL CH340 (COM7)                 USB\VID_1A86&PID_7523\0000
```

USB-485 转换器通常显示为 `CH340` / `CH343` / `FT232` / `CP210x` / `USB-SERIAL`。
**记下这个 COM 号**（比如 COM7）。

> 如果设备管理器里能看到但 `--list` 没有，多半是驱动没装好。CH340 驱动搜 `CH341SER`，FTDI 搜 `CDM21228_Setup`。

### 2. 启动网关

```bash
# Modbus 设备（最常用）
python rs485_bridge.py --port COM7 --baud 9600 --mode modbus

# 私有协议
python rs485_bridge.py --port COM7 --baud 9600 --mode raw

# 网页调用
python rs485_bridge.py --port COM7 --baud 9600 --mode http --token 你的口令
```

启动后会打印 B 机该填的地址：

```
==================================================================
  RS485 网络网关已启动
==================================================================
  串口      : COM7 @ 9600 8N1
  模式      : modbus
  协议      : Modbus TCP -> Modbus RTU
  监听      : 0.0.0.0:502
------------------------------------------------------------------
  B 机请连接以下任一地址：
      192.168.1.101:502
      192.168.1.100:502
==================================================================
```

### 3. 放行防火墙

Windows 第一次监听会弹窗，选「允许访问」。如果没弹或者点错了，用管理员 PowerShell 手动加：

```powershell
New-NetFirewallRule -DisplayName "RS485 Bridge" -Direction Inbound `
  -Protocol TCP -LocalPort 502,5000,8080 -Action Allow -Profile Private
```

只对专用网络放行，别开到公网。

---

## 三、B 机访问

### 方式 A：Modbus TCP（推荐）

任何 Modbus TCP 客户端都能直连，**B 机不需要装任何东西**：

- **Modbus Poll**（Windows 图形工具，调试首选）
- **pymodbus**（Python）
- **Node-RED** 的 `node-red-contrib-modbus`
- **组态王 / 力控 / WinCC** 的 Modbus TCP 驱动
- 我给的 `B端示例/modbus_tcp_client.py`

```bash
python modbus_tcp_client.py --host 192.168.1.100 --unit 1 --fc 3 --addr 0 --count 10

# 输出
已连接 192.168.1.100:502  unit=1
  FC3 addr=0 count=10 -> [4660, 22136, 39612, 0, 0, 0, 0, 0, 0, 0]  (hex 1234 5678 9ABC ...)
```

### 方式 B：浏览器控制台（零安装，推荐）

网关自带页面托管。HTTP 模式启动后，在任意机器的浏览器里打开：

```
http://<A机IP>:8080/panel
```

页面与接口同源，不需要填 IP、不涉及跨域、不需要放开 CORS。直接读写寄存器、发原始帧。

若坚持用本地文件方式打开 `B端示例/panel.html`（`file://`），属于跨域请求，需要给网关加 `--cors` 放行，见第七节安全说明。

### 方式 C：原始字节

```bash
python raw_socket_client.py --host 192.168.1.100 --port 5000 --hex "01 03 00 00 00 0A C5 CD"
python raw_socket_client.py --host 192.168.1.100 --port 5000 -i      # 交互模式
```

### 方式 D：HTTP

```bash
# 读 3 个保持寄存器
curl -X POST http://192.168.1.100:8080/api/modbus \
  -H "Content-Type: application/json" -H "X-Token: 你的口令" \
  -d '{"unit":1,"fc":3,"addr":0,"count":3}'

{"ok":true,"regs":[4660,22136,39612],"raw":"12 34 56 78 9A BC"}
```

```bash
# 写单个寄存器
curl -X POST http://192.168.1.100:8080/api/modbus \
  -H "Content-Type: application/json" -d '{"unit":1,"fc":6,"addr":10,"value":1234}'

# 发原始帧
curl -X POST http://192.168.1.100:8080/api/raw \
  -H "Content-Type: application/json" -d '{"hex":"01 03 00 00 00 03 05 CB"}'
```

---

## 四、参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 必填 | `COM7` / `/dev/ttyUSB0`，也支持 `socket://ip:port` |
| `--baud` | 9600 | 必须和设备一致，不一致会收到乱码或超时 |
| `--databits` / `--parity` / `--stopbits` | 8 / N / 1 | 绝大多数 485 设备是 8N1 |
| `--mode` | modbus | `raw` / `modbus` / `http` |
| `--modbus-port` | 502 | Modbus TCP 端口 |
| `--tcp-port` | 5000 | raw 模式端口 |
| `--http-port` | 8080 | HTTP 模式端口 |
| `--resp-timeout` | 1.0 | 等设备响应的总超时（秒） |
| `--idle-ms` | 20 | raw 模式判断"一帧收完了"的静默时间 |
| `--inter-frame-ms` | 4.0 | 两次事务之间的总线空闲（9600bps 下 3.5 字符约 4ms） |
| `--turnaround-ms` | 1.0 | 485 收发方向切换延时 |
| `--rts` | auto | `auto` 自动收发模块；`toggle` 用 RTS 手动控制 DE/RE |
| `--allow` | 空 | IP 白名单，如 `--allow 192.168.1.200` |
| `--token` | 空 | HTTP 模式的 API 口令 |
| `--cors` | 空 | HTTP 允许的跨域来源，逗号分隔。留空则不发 CORS 头（推荐） |
| `--max-body` | 65536 | HTTP 请求体大小上限（字节） |
| `--log` | 关 | 打印每帧收发的十六进制，排障必开 |

---

## 五、物理层：485 接线三个坑

这三个问题占现场故障的九成以上，跟软件无关。

**1. A/B 接反**
绝大多数设备标 `A`/`B`，但有些厂家把 `A` 叫 `D+`、`B` 叫 `D-`，还有反着标的。接反的表现是：**完全无响应，或收到一串乱码**。解决：断电换线，两秒的事。

**2. 没接共地**
485 是差分信号，理论上两线就够，但现场如果两台设备电源不同、地电位差大，必须把 `GND` 也连上，否则通信时好时坏、距离一长就丢包。

**3. 终端电阻和拓扑**
- 总线必须是**手拉手菊花链**，不能星形分叉。
- 总线长度超过 50 米、或波特率高于 38400，在**最远两端**各加一个 120Ω 终端电阻。
- 短距离、低波特率的桌面调试，通常不用加。

---

## 六、性能边界（先说清楚，免得后面返工）

**485 是半双工总线**：同一时刻只能有一个事务在跑。网关内部用一把全局锁串行化所有请求，这是物理约束，不是实现缺陷。

单次事务耗时构成：

| 环节 | 9600bps 下的量级 | 说明 |
|---|---|---|
| USB 轮询延迟 | 1–2 ms | 已用 `set_latency_timer(1)` 压到底 |
| 发 8 字节请求 | ≈ 8.3 ms | 10 bit/字节 ÷ 9600 |
| 设备处理 + 响应 | 10–50 ms | 取决于设备，说明书上一般有 |
| 收 11 字节响应 | ≈ 11.5 ms | 同上 |
| **合计** | **约 30–70 ms/次** | |

也就是说理论吞吐上限约 **15–30 次事务/秒**（9600bps）。要提高：
- 提波特率（19200 → 翻倍，115200 → 十几倍）
- 一次多读几个寄存器（FC03 一次读 10 个，比分 10 次读快得多）

**多客户端并发**：可以同时连，但请求会排队，不会冲突。B 机两个程序同时轮询同一个设备时，看到的是"变慢"，不是"出错"。

---

## 七、故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `PermissionError: 拒绝访问` | 串口被别的程序占了（Modbus Poll、串口助手） | 关掉那个程序 |
| `could not open port` | COM 号错 / 驱动没装 | `--list` 确认，装 CH340 或 FTDI 驱动 |
| B 机连不上，A 机本机可以 | 防火墙 | 见第二节第 3 步 |
| 网关返回 `0x0B 网关目标设备无响应` | 485 侧没响应：A/B 接反、波特率错、从站地址错、设备没上电 | 开 `--log` 看 TX 帧，对照设备手册 |
| 返回 `0x04 从站设备故障` / `CRC 错误` | 总线干扰、终端电阻缺失、线太长 | 降波特率、加终端电阻、检查屏蔽 |
| raw 模式收到半截数据 | 帧尾静默时间太短 | 加大 `--idle-ms`，比如 50 |
| 设备偶发丢包 | 帧间隔太短 | 加大 `--inter-frame-ms`，比如 10 |

排障第一步永远是加 `--log`：

```
[09:06:05] [TX] 01 03 00 00 00 03 05 CB
[09:06:05] [RX] 01 03 06 12 34 56 78 9A BC 69 43
```

TX 有、RX 空 → 总线/设备侧问题。TX 帧本身不对 → B 机侧请求构造问题。

---

## 八、安全说明

这套网关的本质是**把一条 485 总线的写权限暴露到网络上**。任何能连上端口的人，都能读写你总线上的所有设备。请按这个前提做部署。

**默认配置已采取的措施**

| 项目 | 行为 |
|---|---|
| CORS | 默认**不发送**任何 `Access-Control-Allow-Origin`，第三方网页无法通过浏览器跨域调用 |
| 界面 | 内置控制台走 `/panel` 同源提供，不需要为了用界面而放开跨域 |
| Token | 使用 `hmac.compare_digest` 常量时间比较，避免逐字节试探 |
| 请求体 | 上限 64KB，超限直接拒绝 |
| 错误响应 | 只回通用描述，内部异常与路径仅写入本地日志 |
| 启动横幅 | Token 掩码显示，避免启动日志或截图带出完整口令 |

**部署建议**

```bash
# 最小暴露：只监听内网某块网卡 + 只允许指定 B 机 + 设口令
python rs485_bridge.py --port COM7 --baud 9600 --mode http \
  --listen 192.168.1.100 --allow 192.168.1.50 --token 换成你的长口令
```

- **不要**把 502 / 5000 / 8080 直接映射到公网。Modbus 协议本身没有任何认证机制。
- 必须跨网访问时，走 VPN 或 SSH 隧道，不要靠 `--cors *` 或弱口令硬撑。
- `--cors *` 只在完全隔离的实验网段使用。它等于允许任意网页借用户浏览器操作你的总线。
- Windows 防火墙只对"专用网络"放行，不要开"公用网络"。

**已知局限（诚实说明）**

- 本网关不做 TLS，HTTP 模式的口令与数据在局域网内明文传输。同一广播域内可被嗅探。需要加密请在前面套一层 Nginx/Caddy 反代或走 VPN。
- IP 白名单基于源 IP 判断，可被同网段的 IP 伪造场景绕过（需要攻击者已能控制内网主机）。
- 无速率限制。若暴露面较大，建议在反代层加限流。
- `rs485_bridge.ps1` 是裸透传实现，不含上述任何加固，仅适用于可信内网。

---

## 九、自测

不用真实硬件就能验证网关逻辑：

```bash
pip install -r requirements.txt
python selftest.py      # 协议层 12 项
python sec_test.py      # 安全加固 12 项
```

`selftest.py` 起一个假的 Modbus 从站（用 TCP 扮演串口另一端），验证协议转换：

```
[1] Modbus RTU <-> Modbus TCP 协议转换
  [OK]   FC03 读保持寄存器
  [OK]   事务 ID 原样回填
  [OK]   FC06 写单寄存器回显
  [OK]   写入值可读回
  [OK]   FC01 读线圈
  [OK]   从站无响应时返回异常码 0x0B
  [OK]   连续 20 次事务无错帧
[2] raw 原始字节透传
  [OK]   raw 模式原样往返
[3] REST API
  [OK]   POST /api/modbus 读寄存器
  [OK]   scale 换算
  [OK]   POST /api/raw
  [OK]   错误 token 返回 401

 通过 12 项，失败 0 项
```

`sec_test.py` 验证上面那张安全表里的每一条确实生效：

```
[A] 默认跨域策略（未指定 --cors）
  [OK]   默认响应不含 Access-Control-Allow-Origin
  [OK]   OPTIONS 预检也不放行任意来源
  [OK]   GET /panel 返回内置控制台
[B] --cors 白名单
  [OK]   白名单内来源被回填
  [OK]   白名单外来源不回填
[C] Token 鉴权与掩码
  [OK]   无 Token 返回 401
  [OK]   错误 Token 被拒
  [OK]   正确 Token 通过
  [OK]   启动横幅不明文打印 Token
[D] 请求体大小上限
  [OK]   超限请求体被拒
  [OK]   拒绝时不回传内部异常类型
[E] 异常细节不外泄
  [OK]   异常响应不含 Python 类型名/Traceback

 通过 12 项，失败 0 项
```

---

## 十、文件清单

```
rs485-bridge/
├── rs485_bridge.py              A 机运行的主程序（单文件网关，Python）
├── rs485_bridge.ps1             PowerShell 版（PS 2.0 / Win7 兼容的纯透传实现）
├── selftest.py                  端到端自测（不需要硬件）
├── sec_test.py                  安全回归测试（CORS / Token / 边界 / 异常外泄）
├── requirements.txt             依赖清单
├── LICENSE                      MIT
├── README.md                    本文
├── 方案说明.html                方案对比与选型说明
└── B端示例/
    ├── modbus_tcp_client.py     Modbus TCP 客户端（零依赖）
    ├── raw_socket_client.py     原始字节客户端
    ├── http_client.py           REST API 客户端
    └── panel.html               浏览器控制台（推荐由网关 /panel 提供）
```

> `rs485_bridge.ps1` 是另一份独立实现，只做 raw 透传，不支持 Modbus 协议转换，用于无法安装 Python 的老系统（Windows 7 / PowerShell 2.0）。功能以 `.py` 版为准。

---

## 十一、什么时候该换硬件方案

这套软件网关适合：单台 A 机、局域网内、调试与中小规模采集。

以下情况建议直接买串口服务器（USR-TCP232-410s、有人 NPort 替换品、MOXA NPort 5650 之类，几百块）：

- A 机不能长期开机（笔记本要带走）
- 需要 7×24 稳定运行，不希望有个 Python 进程和 Windows 更新较劲
- 现场不止一台 B 机，且有多人同时操作
- 需要工业级隔离、防雷、宽温

串口服务器干的就是同一件事，只是把它固化进了一个不关机的盒子。协议层做法完全一样——B 机照样用 Modbus TCP 或虚拟串口连。
