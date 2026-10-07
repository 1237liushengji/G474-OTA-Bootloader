# ISO-TP 传输层设计（ISO 15765-2）

> 实现：`G474/Drivers/User/Src/isotp.c`　接口：`G474/Drivers/User/Inc/isotp.h`
> 本文对照仓库实际代码编写，所有参数与行为均可在源码中逐条核对。

---

## 1. 为什么需要 ISO-TP

经典 CAN 单帧有效载荷只有 **8 字节**，而诊断服务经常需要传输远大于此的数据：

| 典型场景 | 长度 |
|---|---|
| `0x22` 读版本号 DID（本工程 F000） | 16 字节 |
| `0x22` 读通信统计 DID（本工程 F002） | 19 字节 |
| `0x19` 读 DTC 列表（本工程 3 条故障码） | 12 字节 |

ISO-TP（ISO 15765-2）在 CAN 之上提供**面向消息的分段与重组**：把长报文拆成首帧 + 连续帧发送，由接收方用流控帧控制节奏，并保证顺序与完整性。

---

## 2. 在协议栈中的位置

```
        ┌─────────────────────────────────────────┐
        │  应用层：UDS 诊断（uds.c）               │
        │  0x10 / 0x19 / 0x22 / 0x2E / 0x3E        │
        └────────────────┬────────────────────────┘
                         │ 应用报文（最长 128 字节）
        ┌────────────────▼────────────────────────┐
        │  传输层：ISO-TP（isotp.c）  ← 本文        │
        │  SF / FF / CF / FC 分段与重组            │
        └────────────────┬────────────────────────┘
                         │ 8 字节 CAN 帧
        ┌────────────────▼────────────────────────┐
        │  数据链路层：FDCAN2 驱动（can.c）         │
        └─────────────────────────────────────────┘
```

`main.c` 的主循环中 `IsoTp_Task()` 与 `Uds_Task()` 依次轮询；CAN 收到的帧由 `CAN_PollRx()` 取出后经 `IsoTp_OnCanFrame()` 喂入传输层。

---

## 3. 帧格式

按 PCI（Protocol Control Information，首字节高 4 位）区分四种帧类型：

| 类型 | PCI | 布局 | 说明 |
|---|---|---|---|
| **SF** 单帧 | `0x0` | `[0x0L, data...]` | `L` = 有效长度 1..7 |
| **FF** 首帧 | `0x1` | `[0x1H, LLL, data0..5]` | 12 位总长度 = `H<<8 \| LLL`，携带前 6 字节 |
| **CF** 连续帧 | `0x2` | `[0x2N, data...]` | `N` = SN 序号 0..15 循环，FF 之后首帧 SN=1 |
| **FC** 流控帧 | `0x3` | `[0x3FS, BS, STmin]` | `FS`：0=CTS 继续 / 1=Wait 等待 / 2=OVFLW 溢出 |

流控帧只有 3 字节有效载荷，其余字节填充 0。

---

## 4. 配置参数

| 宏 | 值 | 含义 |
|---|---|---|
| `ISOTP_MAX_MSG` | 128 | 应用层最大报文长度（收发缓冲大小） |
| `ISOTP_DIAG_REQ_ID` | `0x7E0` | 板 A（诊断仪）→ 板 B（ECU）请求 ID |
| `ISOTP_DIAG_RESP_ID` | `0x7E8` | 板 B（ECU）→ 板 A（诊断仪）应答 ID |
| `ISOTP_TIMEOUT_MS` | 1000 | N_Bs / N_Cr / 客户端应答超时 |

`0x7E0` / `0x7E8` 是 **OBD-II 标准诊断地址对**，与真实诊断仪（CANoe、PCAN-Diag、元征 X431 等）使用的地址一致。

角色决定了哪一端收哪个 ID（`uds.c: Uds_Init()`）：

```c
#if (CAN_NODE_ROLE == CAN_NODE_A)          /* 诊断仪：收应答 */
  IsoTp_Init(ISOTP_DIAG_RESP_ID, ISOTP_DIAG_REQ_ID, Uds_ClientOnMsg, Uds_ClientErr);
#else                                       /* ECU：收请求 */
  IsoTp_Init(ISOTP_DIAG_REQ_ID, ISOTP_DIAG_RESP_ID, Uds_ServerOnMsg, NULL);
#endif
```

`IsoTp_OnCanFrame()` 只接受 `id == s_rxId` 的帧，其余 ID 直接忽略。

---

## 5. 状态机

### 5.1 接收方向

```
RX_IDLE ──SF──────────────────────────────────► 投递（不改变状态）
   │
   └──FF──► RX_COLLECT ──CF(SN 正确)──► 累积
                 │                        │
                 │                        └─ s_rxGot >= s_rxExpect ──► 投递，回 RX_IDLE
                 │
                 ├─ SN 错序 ──► 复位 RX_IDLE + errCb(ISOTP_SN_ERROR)
                 └─ 超 N_Cr  ──► 复位 RX_IDLE + errCb(ISOTP_TIMEOUT)
```

收到 FF 时立即回送流控帧 **`FS=CTS(0x30), BS=0x00, STmin=0x02`**：不限块大小、连续帧间隔 2 ms。

### 5.2 发送方向

```
TX_IDLE ──len<=7（SF）──► 直接发送 ──► 保持 TX_IDLE
   │
   └──len>7（FF）──► TX_WAIT_FC ──FC(CTS)──► TX_SENDING ──每 STmin 发一个 CF
                          │                      │
                          │                      ├─ 块发完(BS 用尽)且未发尽 ──► TX_WAIT_FC
                          │                      └─ 全部发完 ──► TX_IDLE
                          │
                          ├─ FC(Wait)  ──► 刷新 N_Bs，继续等待
                          ├─ FC(OVFLW) ──► TX_IDLE + errCb(ISOTP_OVERFLOW)
                          └─ 超 N_Bs   ──► TX_IDLE + errCb(ISOTP_TIMEOUT)
```

---

## 6. 关键实现细节

### 6.1 单帧路径

长度 ≤ 7 字节时直接发 SF，无需流控握手，状态机保持在 `TX_IDLE`：

```c
frame[0] = (uint8_t)(0x0U | len);
/* 有效字节之后的剩余位置填 0 */
```

### 6.2 首帧与连续帧

发 FF 时记录三个游标：`s_txSent = 6`（FF 已携带前 6 字节）、`s_txNextSn = 1`（FF 后首个 CF 的 SN 为 1）、12 位长度拆进 `frame[0]` 低 4 位与 `frame[1]`。

发 CF 时按 STmin 节拍组帧，**每次只发一帧**，由 `IsoTp_Task()` 反复进入直到发完：

```c
frame[n++] = (uint8_t)(0x20U | s_txNextSn);
s_txNextSn = (uint8_t)((s_txNextSn + 1U) & 0x0FU);   /* 0..15 循环 */
```

若 `CAN_Send()` 返回忙（Tx FIFO 满），**不丢弃该帧**，仅刷新计时基准，下一个节拍重试：

```c
else { s_txLastTick = now; }   /* CAN忙：下个节拍重试该 CF */
```

> 注意此处 SN 已在组帧时递增，重试会复用同一帧内容，因此不会造成序号错乱。

### 6.3 流控帧处理

| FS | 处理 |
|---|---|
| `0x0` CTS | 记录 `BS`（0 = 不限块）与 `STmin`，转 `TX_SENDING` |
| `0x1` Wait | 仅刷新 `s_txLastTick`（续 N_Bs），保持等待 |
| 其他（OVFLW） | 立即放弃本次发送，上报 `ISOTP_OVERFLOW` |

`BS` 用尽且数据未发完时回到 `TX_WAIT_FC` 等下一个流控帧，实现**分块传输**。

### 6.4 STmin 换算

`STmin` 字节按 ISO 15765-2 编码，本实现换算为毫秒：

| STmin 取值 | 标准含义 | 本实现 |
|---|---|---|
| `0x00`–`0x7F` | 0–127 ms | 直接取该值 |
| `0xF1`–`0xF9` | 100–900 µs | **按 1 ms 处理** |
| 其余（保留值） | 保留 | 回退 7 ms |

> **简化说明**：`HAL_GetTick()` 分辨率为 1 ms，无法表达亚毫秒间隔，因此 100 µs 级 STmin 被上取整到 1 ms。这是安全的（发送更慢，不会导致对端溢出），但与标准存在偏差。保留值回退到 7 ms 而非标准的 127 ms，同样属于保守侧的简化。

### 6.5 超时管理

两个方向各自计时，均由 `IsoTp_Task()` 在主循环中检查，**不使用阻塞延时**：

| 计时器 | 触发条件 | 超时值 | 结果 |
|---|---|---|---|
| N_Bs | `TX_WAIT_FC` 状态停留 | 1000 ms | 放弃发送，`ISOTP_TIMEOUT` |
| N_Cr | `RX_COLLECT` 状态停留 | 1000 ms | 放弃接收，`ISOTP_TIMEOUT` |

### 6.6 溢出保护

收到 FF 声明的总长度超过 `ISOTP_MAX_MSG`(128) 时，**立即回送 `FS=OVFLW` 并复位接收机**，不分配也不写入任何缓冲：

```c
if (total > ISOTP_MAX_MSG) {
  IsoTp_SendFc(0x32U, 0U, 0U);   /* FS=OVFLW */
  s_rxState = RX_IDLE;
  return;
}
```

CF 写入时也按 `s_rxExpect` 截断（`for (...; s_rxGot < s_rxExpect; ...)`），防止对端多送导致越界。

### 6.7 并发语义（半双工）

模块同时只允许**一个发送事务**：`IsoTp_Send()` 在 `s_txState != TX_IDLE` 时返回 `ISOTP_BUSY`，由调用方决定重试策略。UDS 客户端据此提示 `diag: busy (previous request pending)`。

接收方向同理只维护一套 `s_rxbuf`，因此**不支持并行接收**——这与单 ECU 单诊断通道的实际场景一致。

### 6.8 流控帧不重发

`IsoTp_SendFc()` 的返回值被显式丢弃：

```c
(void)CAN_Send(s_txId, frame, 3U);
```

FC 一旦丢失，由**对端的 N_Bs 超时兜底**（对端会放弃本次发送）。这是有意的简化：省去 FC 重传定时器，代价是丢失时该次传输失败而非自愈。

---

## 7. API

| 函数 | 说明 |
|---|---|
| `void IsoTp_Init(uint32_t rxId, uint32_t txId, IsoTp_RxCallback rxCb, IsoTp_ErrCallback errCb)` | 初始化通道，绑定收/发 ID 与两个回调 |
| `IsoTp_Result IsoTp_Send(const uint8_t *data, uint16_t len)` | 发起一次发送；返回 `OK` / `BUSY` / `TOOLONG` |
| `void IsoTp_OnCanFrame(uint32_t id, const uint8_t *data, uint8_t len)` | 喂入一帧接收到的 CAN 报文（ID 不匹配则忽略） |
| `void IsoTp_Task(void)` | 主循环轮询：STmin 节拍发送 + N_Bs / N_Cr 超时 |
| `IsoTp_RxCallback(const uint8_t *data, uint16_t len)` | 收到完整应用报文时回调 |
| `IsoTp_ErrCallback(IsoTp_Result result)` | 发送失败或接收异常时回调 |

`IsoTp_Result` 取值：`ISOTP_OK` / `ISOTP_BUSY` / `ISOTP_TOOLONG` / `ISOTP_TIMEOUT` / `ISOTP_SN_ERROR` / `ISOTP_OVERFLOW`。

---

## 8. 时间参数汇总

| 参数 | 值 | 方向 |
|---|---|---|
| N_Bs（等流控帧） | 1000 ms | 发送方 |
| N_Cr（等连续帧） | 1000 ms | 接收方 |
| 本机发出的 FC：BS | 0（不限块） | 接收方 |
| 本机发出的 FC：STmin | 2 ms | 接收方 |
| 客户端等待诊断应答 | 1000 ms（`uds.c`） | 诊断仪 |

---

## 9. 相对标准的简化

本实现为**自研精简版**，状态与参数命名对照 ISO 15765-2 术语，但有意省略了以下内容：

| 省略项 | 影响 | 兜底方式 |
|---|---|---|
| 亚毫秒 STmin | 100 µs 级间隔被上取整为 1 ms | 发送偏慢，无溢出风险 |
| FC 重传 | FC 丢失则该次传输失败 | 对端 N_Bs 超时 |
| 多通道 / 多会话 | 单通道半双工 | 符合单诊断通道场景 |
| CAN FD 与转义寻址 | 仅支持经典 CAN 8 字节帧 | 本工程使用经典 CAN 500 kbit/s |
| 接收侧 FC.Wait | 本机始终回 CTS | 128 字节缓冲足够 |

这些取舍在**教学演示与单 ECU 诊断**场景下是合理的；若用于量产 ECU，需要补齐 FC 重传与 CAN FD 支持。

---

## 10. 与 UDS 的衔接

传输层对上层完全透明：`Uds_ServerOnMsg()` / `Uds_ClientOnMsg()` 收到的永远是**已拼装完整的应用报文**，不需要关心分段。因此一个 16 字节的版本号读取（`0x22 F000`）在总线上表现为：

```
板A ──► 0x7E0  SF  [03 22 F0 00 00 00 00 00]           请求（3 字节有效，补 0 至 8）
板B ──► 0x7E8  FF  [10 10 62 F0 00 47 34 37]           声明总长 0x010=16，携带前 6 字节
板A ──► 0x7E0  FC  [30 00 02]                          流控：CTS / 不限块 / STmin 2ms
板B ──► 0x7E8  CF  [21 34 2D 43 41 4E 20 76]           SN=1，7 字节（余 4）
板B ──► 0x7E8  CF  [22 33 2E 30 00 00 00 00]           SN=2，3 字节，拼装完成
```

拼装结果：`62 F0 00` + `"G474-CAN v3.0"` = 16 字节，正是 `0x22 F000` 的正响应。

用板 B 的 `sniff on` 可以直接在总线上观察到上述完整协商过程。

---

## 相关文档

- [UDS 诊断栈设计](02-UDS诊断栈设计.md)
- [诊断演示与验收手册](03-诊断演示与验收手册.md)
