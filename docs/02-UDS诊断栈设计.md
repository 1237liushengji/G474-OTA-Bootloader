# UDS 诊断栈设计（ISO 14229）

> 实现：`G474/Drivers/User/Src/uds.c`　接口：`G474/Drivers/User/Inc/uds.h`
> 本文对照仓库实际代码编写，所有服务、DID、NRC 与超时值均可在源码中逐条核对。

---

## 1. 角色分配

同一份固件通过编译期宏 `CAN_NODE_ROLE` 切换角色，两块板烧录不同角色：

| 板 | 角色 | 职责 |
|---|---|---|
| **板 A** | **诊断仪**（UDS Client） | 构造诊断请求、解析应答、维护会话镜像、自动保活 |
| **板 B** | **ECU**（UDS Server） | 解析请求、分发服务、组装应答、管理会话与 S3 超时 |

这使得一块开发板即可扮演真实诊断仪（CANoe / PCAN-Diag 的角色），另一块扮演被诊断的 ECU —— **无需额外诊断硬件即可完整演示 UDS 交互**。

请求/应答经 [ISO-TP 传输层](01-ISO-TP传输层设计.md)承载，地址对为 `0x7E0` / `0x7E8`。

---

## 2. 支持的服务

| SID | 服务 | 请求 | 正响应 | 说明 |
|---|---|---|---|---|
| `0x10` | DiagnosticSessionControl | `10 01` / `10 03` | `50 <sub> 00 32 01 F4` | 默认 / 扩展会话 |
| `0x22` | ReadDataByIdentifier | `22 <DID_H> <DID_L>` | `62 <DID> <data...>` | 单 DID 请求 |
| `0x2E` | WriteDataByIdentifier | `2E F0 10 <v>` | `6E F0 10` | **仅扩展会话** |
| `0x19` | ReadDTCInformation | `19 01 <mask>` | `59 01 01 <DTC×3>` | 子功能 01 |
| `0x3E` | TesterPresent | `3E 00` / `3E 80` | `7E 00` 或无应答 | `0x80` 抑制正响应 |

`0x10` 正响应中的 **P2 / P2\*** 按标准编码：

```
50 03 00 32 01 F4
   │  └──┬──┘ └──┬──┘
   │     │       └─ P2* = 0x01F4 = 500 × 10ms = 5000 ms（增强型超时）
   │     └───────── P2  = 0x0032 = 50 ms（默认响应时间）
   └─────────────── 子功能回显 0x03（扩展会话）
```

---

## 3. 数据标识符（DID）

| DID | 内容 | 编码 | 响应长度 | 传输 |
|---|---|---|---|---|
| `F000` | 版本字符串 | ASCII `"G474-CAN v3.0"` | 16 字节 | **多帧**（FF+FC+CF×2） |
| `F001` | 运行时间 | 4 字节大端（秒） | 7 字节 | 单帧 |
| `F002` | 通信统计 | 4 × 4 字节大端 | 19 字节 | **多帧** |
| `F010` | LED 状态 / 控制 | 1 字节（0 灭 / 1 亮） | 4 字节 | 单帧 |

`F000` 与 `F002` 的响应长度超过单帧 7 字节上限，**因此每次读取都会真实走一遍 ISO-TP 分段与流控协商** —— 这不是为了凑演示，而是数据长度决定的。

`F002` 四个字段依次为：

| 字段 | 来源 |
|---|---|
| 1. 发送帧数 | `CAN_Stats.txCount` |
| 2. 接收帧数 | `CAN_Stats.rxCount` |
| 3. 发送确认数 | `CAN_Stats.txAckCount` |
| 4. 错误合计 | `txErrors + rxOverruns + busOffCount + errPassiveCount + protocolErrors` |

`F010` 的读写分别由 `App_GetLed()` / `App_SetLed()` 两个应用层钩子实现（定义在 `main.c`），使 UDS 服务层与具体硬件解耦。

---

## 4. 会话管理

### 4.1 S3 超时

进入扩展会话后，若 **5 秒**内没有任何有效请求，服务器自动退回默认会话：

```c
#define UDS_S3_TIMEOUT_MS 5000U

if ((s_session == UDS_SESSION_EXTENDED) &&
    ((now - s_lastActivityTick) > UDS_S3_TIMEOUT_MS)) {
  s_session = UDS_SESSION_DEFAULT;
}
```

`s_lastActivityTick` 在 `Uds_ServerOnMsg()` 入口处刷新 —— **任何合法长度的请求都算活动**，不限于 `0x3E`。

### 4.2 会话约束体现的安全语义

`0x2E`（写）在默认会话下被拒绝，返回 NRC `0x22`：

```c
if (s_session != UDS_SESSION_EXTENDED) {
  Uds_SendNrc(0x2EU, 0x22U);   /* conditionsNotCorrect */
  return;
}
```

这是 UDS 的典型安全设计：**读操作默认可做，写/执行类操作必须先提升会话权限**。量产 ECU 会在此基础上叠加 `0x27` SecurityAccess 种子密钥认证，本工程以会话控制演示同一语义。

### 4.3 完整交互时序

```
诊断仪 (板A)                         ECU (板B)
    │                                    │
    │── 2E F0 10 01 ────────────────────►│  默认会话 → 拒绝
    │◄─ 7F 2E 22 ────────────────────────│  NRC 0x22 条件不满足
    │                                    │
    │── 10 03 ──────────────────────────►│  进入扩展会话
    │◄─ 50 03 00 32 01 F4 ───────────────│  P2=50ms  P2*=5000ms
    │                                    │  ┌── S3 计时启动
    │── 2E F0 10 01 ────────────────────►│  │
    │◄─ 6E F0 10 ────────────────────────│  └── 板B LED 点亮
    │                                    │
    │── 3E 80 ──────────────────────────►│  每 2s 自动保活（无应答）
    │── 3E 80 ──────────────────────────►│
    │                                    │
    │   （若停止保活 5s → 自动回默认会话）  │
```

---

## 5. 否定响应码（NRC）

统一以 `7F <SID> <NRC>` 三字节返回：

| NRC | 名称 | 触发条件 |
|---|---|---|
| `0x11` | serviceNotSupported | 收到未实现的服务 SID |
| `0x12` | subFunctionNotSupported | `0x10` 子功能非 `0x01`/`0x03`；`0x3E` 子功能非 `0x00` |
| `0x22` | conditionsNotCorrect | `0x2E` 在默认会话下执行 |
| `0x31` | requestOutOfRange | `0x22` 长度≠3 或未知 DID；`0x2E` 长度≠4 或 DID≠F010；`0x19` 长度≠3 或子功能≠`0x01` |

诊断仪侧会解码并打印，例如未知 DID：

```
> diag raw 22 F0 FF
diag>> 22 F0 FF (via ISO-TP 0x7E0)
diag<< (3 bytes): 7F 22 31
  NRC: sid=22 code=31
```

---

## 6. 故障码（DTC）

`0x19 01` 返回 **3 条由真实驱动事件合成的故障码**，而非固定演示数据：

| DTC | 来源计数 | 含义 |
|---|---|---|
| `C100` | `busOffCount > 0` | CAN 总线进入 bus-off |
| `C101` | `errPassiveCount > 0` | 节点进入错误被动 |
| `C102` | `protocolErrors + txEvtLost > 0` | 协议错误 / 发送事件丢失 |

状态字节 `0x01` 表示**当前故障**（testFailed, this operation cycle）；`statusAvail` 声明本机支持的状态位集合为 `0x01`。

正响应布局：`59 01 <statusAvail> [DTC_H DTC_L status] × 3` = 12 字节 → 多帧传输。

**故障码会随总线状态真实变化**：拔掉 CANH 制造总线错误后再次 `diag dtc`，可以看到对应 DTC 状态位置 1，恢复接线后重新上电则清零。

---

## 7. 诊断仪（客户端）行为

### 7.1 请求与应答

`Uds_ClientRequest()` 发送请求并置 `s_pendingResp`；应答经 ISO-TP 拼装完整后由 `Uds_ClientOnMsg()` 处理并打印：

```
diag<< (16 bytes): 62 F0 00 47 34 37 34 2D 43 41 4E 20 76 33 2E 30
  ver: G474-CAN v3.0
```

友好解码覆盖四类应答：

| 首字节 | 解码输出 |
|---|---|
| `0x62` + DID `F000` | `ver: <ASCII 字符串>` |
| `0x62` + DID `F001` | `uptime: <n>s` |
| `0x62` + DID `F010` | `led: <n>` |
| `0x7F` | `NRC: sid=XX code=XX` |
| `0x59` | `DTC list (id,active):` + 逐条列表 |

### 7.2 应答超时

请求发出后 **1 秒**未收到应答即报超时，并清除挂起标志：

```c
if (s_pendingResp && ((now - s_pendingTick) > ISOTP_TIMEOUT_MS)) {
  s_pendingResp = false;
  BSP_UART_Send("diag: response timeout\r\n", 25U);
}
```

ISO-TP 层上报的错误也会映射为可读文本：`diag: isotp timeout` / `diag: isotp SN error` / `diag: isotp overflow`。

### 7.3 自动 TesterPresent 保活

这是**与真实诊断仪行为对齐的关键设计**。扩展会话期间，客户端每 2 秒自动发送 `3E 80`（抑制正响应）维持 S3 计时：

```c
if (s_clientExtended && ((now - s_lastTpTick) >= 2000U)) {
  uint8_t tp[2] = {0x3EU, 0x80U};
  s_lastTpTick = now;
  (void)IsoTp_Send(tp, 2U);      /* 不经 pending 流程：无应答是正常的 */
}
```

否则手工命令之间只要间隔超过 5 秒，ECU 就会退回默认会话，`diag led` 会莫名返回 NRC 0x22 —— CANoe、PCAN-Diag 等工具正是用同样的机制避免这个问题。

客户端通过应答维护会话镜像：收到 `50 03` 置 `s_clientExtended = true`；收到针对 `0x10` 的 NRC 则清 false。

> **注意**：`3E 80` 使用抑制位（suppressPosRspMsgIndicationBit），服务器**不应答**。因此该请求不进入 pending 流程，否则会被误判为应答超时。

---

## 8. API

| 函数 | 说明 |
|---|---|
| `void Uds_Init(void)` | 按 `CAN_NODE_ROLE` 初始化：A=客户端，B=服务器 |
| `void Uds_Task(void)` | 主循环轮询：服务器 S3 回退、客户端超时与保活 |
| `void Uds_ServerOnMsg(const uint8_t *req, uint16_t len)` | 服务器：处理一条完整诊断请求 |
| `void Uds_ClientOnMsg(const uint8_t *rsp, uint16_t len)` | 客户端：处理一条完整诊断应答 |
| `IsoTp_Result Uds_ClientRequest(const uint8_t *req, uint16_t len)` | 客户端：发起一次诊断请求 |
| `void App_SetLed(uint8_t on)` / `uint8_t App_GetLed(void)` | 应用层钩子（`main.c` 实现），供 `0x2E` / `0x22 F010` 使用 |

---

## 9. 时间参数汇总

| 参数 | 值 | 位置 |
|---|---|---|
| S3（扩展会话保持） | 5000 ms | 服务器 |
| P2（默认响应时间，应答中声明） | 50 ms | 服务器 |
| P2\*（增强型超时，应答中声明） | 5000 ms | 服务器 |
| 客户端等待应答超时 | 1000 ms | 客户端 |
| TesterPresent 自动发送间隔 | 2000 ms | 客户端 |

---

## 10. 相对标准的简化

| 简化项 | 说明 |
|---|---|
| 无 `0x27` SecurityAccess | 以「会话控制」演示写保护语义，未实现种子/密钥认证 |
| `0x22` 单 DID | 一次只读一个 DID；标准支持一条请求读多个 DID |
| `0x19` 仅子功能 01 | 未实现 `0x02`（按状态掩码报告）、`0x04`（快照）等 |
| `0x19` 未校验状态掩码 | 请求第三字节被接收但未参与过滤 |
| 无功能寻址 | 仅物理寻址 `0x7E0`，未实现 `0x7DF` 广播 |
| 无 `0x11` ECUReset / `0x14` ClearDTC 等 | 演示所需服务子集 |
| 无 `0x3E` 之外的 S3 保活策略 | 与真实诊断仪一致，已足够 |

服务实现思路对标 `driftregion/iso14229`，本工程为自研精简版，状态与术语命名对照 ISO 14229 标准。

---

## 相关文档

- [ISO-TP 传输层设计](01-ISO-TP传输层设计.md)
- [诊断演示与验收手册](03-诊断演示与验收手册.md)
