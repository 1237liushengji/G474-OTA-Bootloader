# 02 · Bootloader 框架与 APP 集成补丁

> 本文分两部分：
> **A** — Bootloader 如何工作（设计层面，对应 `ota/bootloader/**` 的实现）；
> **B** — 现有工程要改哪几处**确切代码**（逐处给出可直接套用的补丁）。
>
> 所有涉及的既有文件均来自仓库实际内容：`G474/Drivers/User/Src/can.c`、`uds.c`、`console.c`、`G474/Core/Src/main.c`、`G474/Core/Src/system_stm32g4xx.c`。

---

## A. Bootloader 工作原理

### A.1 为什么 Bootloader 是**独立自包含**工程

BL 需要 CAN 收帧、ISO-TP 拆包、UDS 应答。现有工程的 `can.c` 把过滤器硬编码为"按角色收一个业务 ID + 一个诊断 ID"，`isotp.c` 的收发缓冲与回调注册是**单例**（`s_rxbuf[ISOTP_MAX_MSG]`、`s_rxId` 等均为文件级 static），`uds.c` 依赖 `console.c`/`bsp_uart.c`/`led.c`。若让 BL 直接复用，会把整个应用层拖进 BL，既浪费 ROM 又让"BL 失效 = 变砖"的风险面扩大到全部代码。

因此 BL 自带 11 个精简源文件（`ota/bootloader/Core/Src/`），只共享 `ota/common/ota_common.h` 的常量。**代价**：ISO-TP 有约 300 行重复代码；**收益**：BL 体积可控、故障面小、升级 BL 自身时不必考虑应用层。

> 若希望消除重复：把 `can.c` 的过滤器 ID 改成由 `ota_common.h` 宏驱动，再把 `isotp.c` 从"单例 static"改成"实例结构体"，两个工程共用。这属于第二步重构，本方案不强制。

### A.2 启动流程（`main.c`）

```
第一条语句：SCB->VTOR = OTA_FLASH_BASE     ← 必须早于 HAL_Init()
     │        （否则从 APP 软复位后 VTOR 仍指向 APP 的向量表，BL 中断会跳错）
     ▼
HAL_Init → SystemClock_Config(150MHz) → BL_Uart_Init → BL_FlashInit → BL_ParamLoad
     ▼
读控制区（4 候选取 seq 最大且 CRC 有效）
     ▼
state ∈ {TRIAL_A, TRIAL_B} ?  ──是──► trialLeft-- 并写回；减后为 0 → 切另一槽(回滚)
     │否                                        │
     ▼                                          ▼
等待启动窗口最多 1000ms（TRIAL 状态跳过等待）
     │
     ├─ 收到诊断请求 ──► recovery 模式（保持 CAN 在线，等刷写）
     │
     └─ 超时 ──► BL_ImageValidate(活动槽)
                   ├─ 通过 ──► BL_JumpToApp（重设 MSP、VTOR、跳复位向量）
                   └─ 失败 ──► 试另一槽；两槽都失败 ──► recovery
```

### A.3 跳转前的四重校验（`bl_image.c`）

```c
/* 1. 镜像头自洽 */
magic == OTA_IMG_MAGIC
hdrCrc == CRC32(hdr[0..24])
payloadSize <= OTA_IMG_MAX_PAYLOAD
loadAddr == 槽基址 + 32            /* 必须是本槽的 APP 链接基址 */

/* 2. 净荷完整性 */
CRC32((uint8_t*)loadAddr, payloadSize) == hdr.payloadCrc

/* 3. 栈顶指针合法性（独立校验，不能只看 CRC） */
sp = *(uint32_t*)loadAddr;
(sp & 0x7U) == 0                 /* 8 字节对齐 */
sp >= 0x20000000 && sp <= 0x20020000

/* 4. 复位向量合法性 */
rv = *(uint32_t*)(loadAddr + 4);
(rv & 1U) == 1U                  /* Thumb 位 */
rv >= loadAddr && rv < (loadAddr + payloadSize)
```

第 3、4 项为什么要**独立于 CRC**：CRC 只能证明"数据没被传错"，不能证明"传上来的是一份能跑的固件"。若上位机误传了别的 `.bin`（例如链接在 `0x08000000` 的 BL 自身镜像），CRC 依然正确，但跳进去会立刻 HardFault 且**没有任何输出**。SP/复位向量检查能挡住这一类。

### A.4 跳转实现（`bl_jump.c`）

> ⚠️ **血线 9：系统异常挂起位必须走 `SCB->ICSR`（2026-10-07 修复，H1）**
>
> Cortex-M4 的挂起位分两处，`NVIC->ICPR` **只管得着外设中断**：
>
> | 类型 | 清挂起位的方法 |
> |---|---|
> | 外设 IRQ（0..239） | `NVIC->ICPR[i]` |
> | **系统异常**（SysTick / PendSV / SVC） | **只能** `SCB->ICSR`（`PENDSTCLR` bit25 / `PENDSVCLR` bit27），NVIC 对其完全无效 |
>
> 原实现只写了那个 8 次的 NVIC 循环，**没碰 `SCB->ICSR`**。本工程 SysTick 每 1 ms
> 挂起一次（BL 主循环时基），于是改`VTOR` 之后若 SysTick 仍处于 pending，APP 入口后
> 会在其生效的那一瞬按**APP 自己的向量表**取向量 —— 而此刻 APP 还没走完
> `SystemInit`/`main`，表现为"APP 随机 HardFault / 偶发启动即死"。
>
> 这个缺陷最讨厌的地方是**概率极低**（只在跳转瞬间恰处 SysTick 窗口内，约千分之一），
> 现场表现为"昨天刷了几十次都好好的，今天有一两次起不来"，几乎不可能被常规复现抓到。
> 现已补上 `SCB->ICSR` 清理，并由 `verify_all.py` 的 C11 检查项守卫。

```c
void BL_JumpToApp(uint32_t appBase)
{
  /* 按 Flash 实际内容取 APP 向量表前两项（不使用可能过期的 RAM 缓存） */
  uint32_t sp    = *(volatile const uint32_t *)appBase;
  uint32_t rv    = *(volatile const uint32_t *)(appBase + 4U);
  BL_AppEntry entry = (BL_AppEntry)rv;

  __disable_irq();

  /* 2) 停 SysTick */
  SysTick->CTRL = 0U;
  SysTick->LOAD = 0U;
  SysTick->VAL  = 0U;

  /* 关所有外设中断 + 清外设挂起位 */
  for (uint32_t i = 0U; i < 8U; i++)
  {
    NVIC->ICER[i] = 0xFFFFFFFFU;
    NVIC->ICPR[i] = 0xFFFFFFFFU;
  }

  /* 【血线9 / H1】清系统异常挂起位——NVIC 清不到，必须走 SCB->ICSR（写 1 清除） */
  SCB->ICSR = SCB_ICSR_PENDSTCLR_Msk | SCB_ICSR_PENDSVCLR_Msk;

  /* 3) 释放 BL 占用的外设（APP 会重新初始化） */
  BL_CanDeInit();
  BL_UartDeInit();

  /* 4) 向量表切到 APP */
  SCB->VTOR = appBase;

  /* 5) 设置 APP 栈顶 */
  __set_MSP(sp);
  __DSB();
  __ISB();

  /* 6) 恢复中断使能——必须开！见下方说明 */
  __enable_irq();
  __DSB();
  __ISB();

  /* 7) 跳转 */
  entry();

  for (;;) { }                     /* 不返回 */
}
```

> **为什么 `__enable_irq()` 必须在 `entry()` 之前**：只有**复位**才会被硬件自动清
> PRIMASK，APP 不是复位进入的。若带着 `PRIMASK = 1` 跳过去，APP 永远收不到中断
>（SysTick / UART / FDCAN 全哑），后果比"窗口期误触发"严重得多。窗口期误触发的
> 概率已由上面"跳转前清空全部挂起位"（含 `SCB->ICSR`）降到可忽略 —— 能触发中断的
> 前提是新 pending 被重新置位，而此刻 SysTick 已停、外设已 DeInit、`VTOR` 已指向 APP。

### A.4.1 控制区写目标选择（`bl_param.c`）

> ⚠️ **血线 10：`BL_PARAM_PRIMARY_WRITE = 0` 的两条绕过路径（2026-10-07 修复，H2/H3）**
>
> `BL_ParamSelectTarget()` 负责"写哪一份、绝不擦掉唯一最新副本"。修复前有两条路径
> 绕过这条约束，导致文件头"置0 可彻底避免擦 PRIMARY 页"的说法**并不成立**：
>
> | 编号 | 缺陷 | 典型触发场景 |
> |---|---|---|
> | **H3** | "全部副本无效"回退路径**裸写** `OTA_CFG_PRIMARY_ADDR`，完全无视该宏 | **全新芯片首次上电**（Flash 全 `0xFF`）—— 恰恰是该宏最需要生效的时刻 |
> | **H2** | 同页分支的 `*off` 恒为 `OTA_CFG_SLOT_OFFSET`（副本0）。因置 0 时所有有效副本都在 MIRROR 页，`oldestIdx == newestIdx` 恒成立，于是**永远走这一分支、永远写副本0** —— 副本1 永不写、永远无效 | 任何一次正常配置落盘 |
>
> H2 的后果不是"少一份冗余"，而是**双副本机制名存实亡**：每次写都擦掉唯一幸存者，
> 只靠 STEP-3 的整页恢复续命。
>
> 修复后：
> * H3 —— 回退路径改为 `#if (BL_PARAM_PRIMARY_WRITE == 0)` 条件编译分流；
> * H2 —— 同页分支按 `s_candOff[newestIdx]` 选**非最新**的那一份，形成真正的
>   ping-pong：写 copy0 → STEP-3 恢复 copy1 → copy1 成为最新 → 下次写 copy1……
>
> 效果：擦除窗口之外**恒有两份**有效配置，血线 1 才真正成立。修复后该宏的语义才
> 名副其实（绝不碰 PRIMARY 页 → 向量表变砖风险为零），代价是冗余从"跨页"降级为
> "同页两副本"，这是可接受的取舍。

> **注意**：`HAL_DeInit()` 会复位 SysTick 与 NVIC，APP 的 `HAL_Init()` 随后会重新配置，因此计数时基从 0 重新开始——这是正常且期望的行为。

### A.5 ROM / RAM 预算

| 组件 | 估算 | 说明 |
|---|---|---|
| `bl_isotp.c` + `bl_diag.c` | 6~9 KB | 协议栈主体 |
| `bl_flash_if.c` + `bl_param.c` + `bl_image.c` + `bl_crc32.c` | 3~4 KB | Flash 与校验 |
| `bl_can.c` + `bl_uart.c` + `main.c` + HAL 子集 | 4~6 KB | 驱动与状态机 |
| **合计** | **约 13~19 KB** | 预算 32 KB，余量充足 |

RAM 占用：`s_pageBuf[2048]` + 控制区页暂存 `[2048]` + ISO-TP 收发各 1 KB（`OTA_ISOTP_MAX_MSG = 1024`）+ CAN FIFO/状态 ≈ **6.3 KB**。

---

## B. 现有工程的确切改动（APP 侧）

### B.1 新增文件

| 文件 | 内容 |
|---|---|
| `G474/Drivers/User/Inc/ota_agent.h` | 本方案 `ota/app/ota_agent.h` |
| `G474/Drivers/User/Src/ota_agent.c` | 本方案 `ota/app/ota_agent.c` |
| `G474/Drivers/User/Inc/ota_tunnel.h` | 本方案 `ota/app/ota_tunnel.h` |
| `G474/Drivers/User/Src/ota_tunnel.c` | 本方案 `ota/app/ota_tunnel.c` |
| `ota/common/ota_common.h` | 契约头（放仓库根 `ota/common/`，被上述文件包含） |

Keil 工程需在 **IncludePath** 追加 `..\..\ota\common`（相对于 `MDK-ARM`），并把 2 个新 `.c` 加入 `User` 分组。

### B.2 `can.c`：放开诊断 ID 过滤器（**必须改，否则收不到 0x7E8**）

现有 `CAN_Configure()` 中，正常模式下只配 `StdFiltersNbr = 2`，FilterIndex1 只匹配**一个**诊断 ID。改法：

**改动 1** — 过滤器数量由 2 改为 3：

```c
/* 原：hfdcan2.Init.StdFiltersNbr = (s_promisc || s_loopbackSelfTest) ? 1U : 2U; */
hfdcan2.Init.StdFiltersNbr = (s_promisc || s_loopbackSelfTest) ? 1U : 3U;
```

**改动 2** — 在现有第二个过滤器之后（`can.c` 中 FilterIndex = 1U 的那段 `#if` 之后）追加：

```c
  /* 第三过滤器：诊断应答 ID（0x7E8）。
   * 必要性：过滤器是"一条过滤器 = 一个 ID + 一个掩码"，
   * FilterID2 是掩码而非第二个 ID，因此要同时收 0x7E0 与 0x7E8 必须再加一条。
   * 用途：① 隧道模式需回吐 7E0/7E8 两个方向；② 单板自测时本节点既是 ECU 又是诊断仪；
   *      ③ 双 CAN/多诊断 ID 并存场景。 */
  if (!s_promisc && !s_loopbackSelfTest)
  {
    filterConfig.FilterIndex = 2U;
    filterConfig.FilterID1   = ISOTP_DIAG_RESP_ID;   /* 0x7E8 */
    filterConfig.FilterID2   = 0x7FFU;
    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);
    if (status != HAL_OK)
    {
      return status;
    }
  }
```

> **为什么不用 `FDCAN_FILTER_RANGE`**：范围型过滤器（`0x7E0~0x7E8`）确实能省一条过滤器，但会顺带收进 `0x7E1~0x7E7` 共 7 个无关 ID，在本工程 16 深度的 RX 环形缓冲上白占带宽与缓冲。**本方案选择精确匹配 + 多一条过滤器**，语义更干净。

### B.3 `uds.c`：注册 5 个新服务

**改动 1** — 文件头部 `#include` 区加一行：

```c
#include "ota_agent.h"        /* 复用 ota_common.h 的 SID/RID/DID/NRC 宏定义 */
```

**改动 2** — `Uds_ServerOnMsg()` 的 `switch (req[0])` 中，在 `case 0x3EU:` **之前**插入：

```c
    /* ---- OTA 刷写服务：仅在扩展会话下可用，检查放在各 handler 内部 ---- */
    case 0x11U: Ota_OnEcuReset(req, len);        break;
    case 0x31U: Ota_OnRoutineControl(req, len);  break;
    case 0x34U: Ota_OnRequestDownload(req, len); break;
    case 0x36U: Ota_OnTransferData(req, len);    break;
    case 0x37U: Ota_OnTransferExit(req, len);    break;
```

**改动 3** — `Uds_Service22()` 的 `switch (did)` 中，在 `default:` 之前插入：

```c
    default:
    {
      /* OTA 只读 DID（F180~F184）：交 OTA 模块填充 */
      uint16_t n = Ota_ReadDid(did, &resp[3], (uint16_t)(sizeof(resp) - 3U));
      if (n == 0U)
      {
        Uds_SendNrc(0x22U, 0x31U);
        return;
      }
      rlen = (uint16_t)(3U + (n - 3U));   /* Ota_ReadDid 已写入 62 <DID> 头 */
      break;
    }
```

> ⚠️ 注意 `resp` 长度：原工程 `Uds_Service22()` 里是 `uint8_t resp[64]`。**若把 `ISOTP_MAX_MSG` 提到 1024（本方案已采纳），这个数组必须同步放大为 `uint8_t resp[ISOTP_MAX_MSG]`**，否则一旦某个 DID 的应答超过 64 字节，`rlen` 就会越界写栈（`ISOTP_MAX_MSG` 变大并不会自动保护它）。见 §B.7 联动表第 1 行。

**改动 4** — `Uds_Service2E()` 增加 F190 分支（把现有"仅 F010"的判断放宽）：

```c
static void Uds_Service2E(const uint8_t *req, uint16_t len)
{
  uint16_t did;

  if (s_session != UDS_SESSION_EXTENDED)
  {
    Uds_SendNrc(0x2EU, 0x22U);
    return;
  }

  did = (uint16_t)(((uint16_t)req[1] << 8) | req[2]);

  if (did == UDS_DID_BOOT_REQ)                 /* F190：请求下次进 Bootloader */
  {
    Ota_OnWriteBootReq(req, len);
    return;
  }

  if ((len != 4U) || (did != UDS_DID_LED))     /* 原有 F010 逻辑保持不变 */
  {
    Uds_SendNrc(0x2EU, 0x31U);
    return;
  }

  App_SetLed(req[3]);

  uint8_t resp[3];
  resp[0] = 0x6EU;
  resp[1] = req[1];
  resp[2] = req[2];
  Uds_SendResp(resp, 3U);
}
```

### B.4 `console.c`：隧道命令（**方式二必需**）

**改动 1** — 头部 `#include "ota_tunnel.h"`，并在文件内定义：

```c
static void Cmd_Ota(void)      { Ota_PrintStatus(); Cmd_Stats(); }
static void Cmd_Tun(char *args){ (void)OtaTunnel_OnLine("tun", args); }
static void Cmd_Tx(char *args) { (void)OtaTunnel_OnLine("tx", args); }
```

**改动 2** — `Console_Execute()` 的 if-else 链中，在 `else { unknown cmd }` **之前**插入：

```c
  else if (strcmp(cmd, "tun") == 0)   { Cmd_Tun(args); }
  else if (strcmp(cmd, "tx") == 0)    { Cmd_Tx(args); }
  else if (strcmp(cmd, "ota") == 0)   { Cmd_Ota(); }
```

**改动 3** — `Console_PrintHelp()` 增加三行说明：

```c
  Put("  ota                OTA state / slot / version\r\n");
  Put("  tun <on|off>       serial<->diagnostic-CAN tunnel (for PC OTA tool)\r\n");
  Put("  tx <id> <n> <hex>  send raw frame, e.g. tx 7E0 8 02 10 03 00 00 00 00 00\r\n");
```

### B.5 `main.c`：接线（**4 处小改动**）

**改动 1** — `#include` 区加：

```c
#include "ota_agent.h"
#include "ota_tunnel.h"
```

**改动 2** — 把看门狗句柄改为全局（`ota_agent.c` 以 `extern` 引用它，用于页编程前喂狗）：

```c
/* 原：static IWDG_HandleTypeDef hiwdg; */
IWDG_HandleTypeDef hiwdg;        /* 非 static：ota_agent.c 在 Flash 页编程前喂狗需要访问 */
```

**改动 3** — `Uds_Init();` 之后加：

```c
  OtaTunnel_Init();                                /* 串口↔诊断CAN隧道（默认关闭） */
  Ota_Init();                                      /* 读控制区、识别本槽、置试运行待确认 */
```

**改动 4** — 主循环内追加两个调用：

```c
  while (1)
  {
    App_Process();
    Console_Task();
    CAN_Task();
    IsoTp_Task();
    Uds_Task();
    Ota_Task();                                    /* 试运行自检通过后落定 TRIAL -> IDLE */
    HAL_IWDG_Refresh(&hiwdg);
  }
```

**改动 5** — `App_Process()` 中取帧后追加隧道回吐：

```c
  if (CAN_PollRx(&s_rxMsg))
  {
    g_dbgPoll++;
    if (Console_SniffEnabled())
    {
      Console_PrintFrame(&s_rxMsg);
    }
    OtaTunnel_PrintFrame(&s_rxMsg);               /* 隧道开启时回吐 7E0/7E8 */
    App_HandleRx();
  }
```

> 顺序要点：`OtaTunnel_PrintFrame` 必须在 `App_HandleRx()`（内部调用 `IsoTp_OnCanFrame`）**之前**调用，这样隧道看到的是"原始到达帧"，与本地协议处理解耦。

### B.6 `system_stm32g4xx.c`：APP 的向量表偏移

```c
/* 打开宏（或在 uvprojx 的 Define 里定义，二者取一） */
#define USER_VECT_TAB_ADDRESS

#if defined(USER_VECT_TAB_ADDRESS)
  #define VECT_TAB_BASE_ADDRESS   FLASH_BASE
  #define VECT_TAB_OFFSET         0x00000820U   /* Slot A；Slot B 版改 0x00040820U */
#endif
```

对应 `uvprojx` 的 IROM1：`StartAddress = 0x800C080`、`Size = 0x33F80`。

**建议加一条运行期断言**（放在 `main()` 里 `HAL_Init()` 之后）：

```c
  /* 向量表偏移自检：漏改 VECT_TAB_OFFSET 会让 APP 中断全部跳回 BL 的向量表，
   * 现象是"能跑但中断行为诡异"，极难排查 —— 因此在启动时就把它变成显式报错。 */
  {
    const uint32_t expect = (uint32_t)(FLASH_BASE + VECT_TAB_OFFSET);
    if ((SCB->VTOR & 0xFFFFFE00U) != (expect & 0xFFFFFE00U))
    {
      BSP_UART_Printf("FATAL: VTOR=0x%08lX expect 0x%08lX (VECT_TAB_OFFSET wrong)\r\n",
                      (unsigned long)SCB->VTOR, (unsigned long)expect);
      Error_Handler();
    }
  }
```

### B.7 `isotp.h`：吞吐优化（**已采纳，必改**）

```c
/* 原：128。提到 1024 后单条 UDS 报文上限 1024，0x36 净荷可达 1016 字节，
 * 0x36 报文数从 1835 降到 209（满槽 208KB 镜像），端到端从 ~76s 降到 ~64s（见 03 号文档 §5）。
 * 代价：RAM 增加约 1.8KB（收发缓冲各 1KB）。 */
#define ISOTP_MAX_MSG            1024U
```

**必须同步的四处联动**（任何一处漏改都会造成静默故障）：

| # | 位置 | 改法 | 漏改后果 |
|---|---|---|---|
| 1 | `uds.c` 的 `Uds_Service22()` | `uint8_t resp[64]` → `uint8_t resp[ISOTP_MAX_MSG]` | 大 DID 应答时 `rlen` **越界写栈** |
| 2 | `ota_common.h` | `OTA_ISOTP_MAX_MSG = 1024U`；`UDS_DL_MAX_BLOCK_PAYLOAD = OTA_ISOTP_MAX_MSG - 3` | 上位机按 1016 发、设备只收 128 → 回 FC=OVFLW |
| 3 | 上位机 `ota_host.py` | `ISOTP_MAX_MSG = 1024`、`ISOTP_DEFAULT_BLOCK_SIZE = 1016`、`ISOTP_MAX_BLOCK_SIZE = 1016` | 同上 |
| 4 | 契约头的一致性断言 | `#if defined(ISOTP_MAX_MSG) && (ISOTP_MAX_MSG != OTA_ISOTP_MAX_MSG) #error ...` | 三方数值漂移无法在编译期暴露 |

> 加第 4 条断言的意义：本次优化涉及三个代码库的同一个数值，**靠人记住是不可靠的**。
> 有了它，任何一处被改而另两处没跟，会在 Keil 编译时直接 `#error`，而不是在现场表现为
> "固件收到一半就回 OVFLW"。

---

## C. 改动清单汇总（可直接当 checklist）

| # | 文件 | 改动 | 必需性 |
|---|---|---|---|
| 1 | `can.c` | `StdFiltersNbr` 2→3；追加 `0x7E8` 过滤器 | **必需**（否则收不到应答） |
| 2 | `uds.c` | `include ota_agent.h`；`switch` 加 5 个 case；`0x22` 加 default 兜底；`0x2E` 放宽 F190 | **必需** |
| 3 | `uds.c` | `Uds_Service22()` 的 `resp[64]` → `resp[ISOTP_MAX_MSG]` | **必需**（随 7 联动，否则越界写栈） |
| 4 | `main.c` | 2 个 include、`hiwdg` 去 static、`Ota_Init`/`OtaTunnel_Init`、主循环 `Ota_Task`、`OtaTunnel_PrintFrame` | **必需** |
| 5 | `system_stm32g4xx.c` | 打开 `USER_VECT_TAB_ADDRESS`，`VECT_TAB_OFFSET` 改 `0x8020`/`0x40880` | **必需** |
| 6 | `uvprojx` | IROM1 起始/大小、`IncludePath` 加 `..\..\ota\common`、加入 2 个新 `.c` | **必需** |
| 7 | `isotp.h` | `ISOTP_MAX_MSG` 128→1024 | **必需**（吞吐优化） |
| 8 | `ota_common.h` | `OTA_ISOTP_MAX_MSG` 与由它推导的 2 个宏 + 一致性断言 | **必需**（随 7） |
| 9 | `console.c` | `tun` / `tx` / `ota` 命令 | 隧道模式必需 |
