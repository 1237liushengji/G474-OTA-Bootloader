# Bootloader（BL）构建配置说明

> 本文与唯一真相源 `ota/common/ota_common.h` 严格对齐；任何地址冲突以 `ota_common.h` 为准。
> BL 是**自包含工程**：只依赖 `ota/common/ota_common.h` + STM32G4 HAL/CMSIS，不引用应用层任何文件。

---

## 1. 目标与硬件事实

| 项目 | 取值 | 来源 |
|---|---|---|
| MCU | STM32G474VETx | 既有工程 `.uvprojx` |
| Flash / RAM | 512 KB @ `0x08000000` / 128 KB @ `0x20000000` | `ota_common.h` |
| Flash 页 | **2 KB**；编程粒度 **64 位（双字）** | RM0440；`HAL_FLASH_Program(FLASH_TYPEPROGRAM_DOUBLEWORD, ...)` |
| 时钟 | HSE 8 MHz → PLL(M=2,N=75,R=2) → SYSCLK/HCLK/PCLK1 = **150 MHz**，`FLASH_LATENCY_4` | 与既有 `main.c` 完全一致（`bl_clock.c`） |
| FDCAN | FDCAN2 经典 CAN 500 kbit/s，PB12=RX / PB13=TX（AF9），内核时钟 PCLK1=150 MHz，`NominalPrescaler=30`（10 tq/位、采样点 80%、SJW=2） | 与既有 `can.c`/`msp.c` 一致（`bl_can.c`） |
| 诊断地址 | 请求 `0x7E0`（BL 收）/ 应答 `0x7E8`（BL 发） | 契约 |
| 看门狗 | IWDG：LSI/32 = 1 kHz，Reload = 3000（3 s）+ `DBGMCU_APB1FZR1_DBG_IWDG_STOP` | 与既有 `main.c` 一致 |

---

## 2. 分区表（与 `ota_common.h` 一致）

| 地址 | 大小 | 名称 | 说明 |
|---|---|---|---|
| `0x08000000` | 2 KB | BL_HEADER | **BL 向量表（页首 0x1D8 字节）+ 控制区主份两份副本**（`+0x1D8` / `+0x400`）。BL 绝不整体擦除本页，只在写配置时"读整页→擦→回写向量表→写副本" |
| `0x08000800` | 30 KB (`0x7800`) | BL_CODE | **Bootloader 代码区**（本文档核心，见 §3） |
| `0x0800C000` | 208 KB (`0x34000`) | SLOT_A | APP 槽 A（含槽首 32 B 镜像头）；**APP 链接基址 = `0x0800C080`**；上界恰好 `0x08040000` |
| `0x08040000` | 2 KB | BL_HEADER_MIR | 控制区镜像份（两份副本 `+0x1D8` / `+0x400`） |
| `0x08040800` | 208 KB (`0x34000`) | SLOT_B | APP 槽 B；**APP 链接基址 = `0x08040880`**；落在 Bank2 |
| `0x08078800` | 30 KB | 预留 | 未分配 |

控制区页内偏移（契约已用 `#error` 锁死）：

* `OTA_CFG_SLOT_OFFSET` = **`0x1D8`**（紧接 BL 向量表之后；`OTA_CFG_VECTAB_BYTES = 0x1D8`）
* `OTA_CFG_SLOT_MIRROR_OFF` = **`0x400`**（2 KB 页的后半；`0x400 + 256 ≤ 0x800`）
* 4 个候选 = {`0x08000000`, `0x08040000`} × {`0x1D8`, `0x400`}

---

## 3. 为什么 BL 的向量表在 `0x08000000`，而代码必须从 `0x08000800` 起链接

三段事实叠加，结论是唯一的：

1. **MCU 复位硬件固定从 `0x08000000` 取初始 SP 与复位向量**（BOOT0=0，主 Flash 映射）。所以 BL 的**向量表必须位于 `0x08000000`**，这也是契约不变式 1 要求 `main()` 第一句写 `SCB->VTOR = OTA_FLASH_BASE` 的原因。
2. **`0x08000000` 这一页（2 KB）同时是控制区主份**：BL 向量表只占页首 `0x1D8` 字节，两份配置副本放在 `+0x1D8` 与 `+0x400`，三者互不重叠（契约 `#error` 已断言 `0x1D8 + 256 ≤ 0x400`、`0x400 + 256 ≤ 0x800`）。
3. 因此**页 0 不能拿来放代码**（否则配置副本会被代码覆盖，或擦配置会擦掉代码）。BL 代码只能放在页 0 之后 → **`0x08000800` 起**，到 `0x0800B800` 为止（30 KB），正好与 Slot A 相接。

> 保护逻辑与分区表的对齐关系（"血线"）：`addr < OTA_SLOT_A_ADDR`（= `0x0800C000`）这一条判断同时护住了 **BL 向量表页 + 控制区主份 + 整个 BL 代码区（30 KB）**。所以 `ota_common.h` 里 `#error` 断言 `OTA_BL_CODE_ADDR + OTA_BL_CODE_SIZE <= OTA_SLOT_A_ADDR`——一旦 BL 代码超出 30 KB，或 Slot A 前移，编译期就会报错，不会带着"下载 A 槽擦掉 Bootloader"的隐患上线。

---

## 4. Keil（MDK-ARM）Target 建立步骤

1. 复制一份既有 `G474.uvprojx`，新建 Target 名为 `Bootloader`，Device 选 `STM32G474VETx`。
2. **Target → Target 选项卡**：勾掉 `Use Memory Layout from Target Dialog`，改用下面的 scatter（关键：把向量表钉在 `0x08000000` 的整页里，代码区从 `0x08000800` 开始）：

```
LR_IROM1 0x08000000 0x00008000  {          ; 32 KB = 页0(2KB) + BL 代码区(30KB)
  ER_VECTAB 0x08000000 0x00000800  {       ; 页0：只放向量表，其余留给控制区副本(运行时写入)
    *(RESET, +First)
  }
  ER_BLCODE 0x08000800 0x00007800  {       ; 30 KB：BL 代码
    *(InRoot$$Sections)
    .ANY (+RO)
  }
  RW_IRAM1 0x20000000 0x00020000  {        ; 128 KB RAM
    .ANY (+RW +ZI)
  }
}
```

   * 若坚持用 Target 对话框的 IROM1，则填 `Start 0x08000000 / Size 0x8000`，**并且**必须配合上面的 scatter 才能保证代码不落进页 0；只填 IROM1 而不写 scatter，链接器会把代码紧排在向量表之后（即 `0x1D8` 起），会与控制区副本冲突。
   * 等价的另一种写法：IROM1 `Start 0x08000800 / Size 0x7800` + 自定义 scatter 里额外放 `ER_VECTAB 0x08000000 0x800`。两者效果相同。
3. **C/C++ 选项卡**：
   * `Define`：`USE_HAL_DRIVER,STM32G474xx`
   * **不要**定义 `USER_VECT_TAB_ADDRESS` / `VECT_TAB_OFFSET`（那是 APP 的事；BL 的 `VTOR` 由 `main()` 第一句显式设置为 `OTA_FLASH_BASE`）
   * `Include Paths`：`Drivers/STM32G4xx_HAL_Driver/Inc`、`Drivers/CMSIS/Device/ST/STM32G4xx/Include`、`Drivers/CMSIS/Include`（`ota_common.h` 已在 `bl_main.h` 里用相对路径 `../../common/ota_common.h` 引用，**无需**额外 include 路径）
   * `Optimization`：`-O1`（默认即可；BL 不追求极致尺寸）
4. **需要加入工程的源文件**：

| 分组 | 文件 |
|---|---|
| BL 自身 | `ota/bootloader/Core/Src/` 下全部 11 个：`main.c`、`bl_clock.c`、`bl_can.c`、`bl_flash_if.c`、`bl_crc32.c`、`bl_param.c`、`bl_image.c`、`bl_jump.c`、`bl_isotp.c`、`bl_diag.c`、`bl_uart.c` |
| BL 头 | `ota/bootloader/Core/Inc/` 下 11 个 `.h`（无需手动添加，随源文件包含） |
| 启动/系统 | 既有工程的 `startup_stm32g474xx.s`、`system_stm32g4xx.c` |
| HAL | 既有工程 `Drivers/STM32G4xx_HAL_Driver/Src/` 下的 HAL 源（与 APP 用同一套；至少需要 `stm32g4xx_hal.c/.h`、`_rcc`、`_gpio`、`_flash`、`_flash_ex`、`_fdcan`、`_uart`、`_iwdg`、`_pwr`、`_pwr_ex`、`_cortex`、`_dma`(UART 依赖)） |

5. **绝对不要加入 BL 工程的应用层文件**（会重复定义符号或行为错误）：
   `Core/Src/main.c`(APP)、`Core/Src/stm32g4xx_it.c`、`Core/Src/stm32g4xx_hal_msp.c`、`Drivers/User/Src/can.c`、`isotp.c`、`uds.c`、`console.c`、`bsp_uart.c`、`led.c`、`ota_agent.c`。
   BL 自带了 `HAL_MspInit`（`bl_clock.c`）、`HAL_FDCAN_MspInit/MspDeInit`（`bl_can.c`）、`SysTick_Handler`（`main.c`）。
6. 输出 `.hex`/`.bin`；**必须**打开 `.map` 记录 `Total RO Size` 与 `Total RW Size`：
   * `Total RO Size` ≤ 30 KB（`0x7800`）——超出会被 scatter 报 `L6406E/L6220E`，也会违反契约 `#error` 的意图；
   * `Total RW + ZI Size` ≤ 约 8 KB（本工程静态数组合计约 **6.3 KB**，见 §5）。
7. 首次烧录用 Keil 的 Flash Download 正常下载即可（会写入 `0x08000000` 的向量表 + `0x08000800` 起的代码）。**不要**在量产环节执行"整片擦除后只烧 APP"——那样会抹掉 BL 与向量表。

---

## 5. BL 体积/内存估算（本机无编译器，只能静态估算，**必须用 .map 复核**）

| 项 | 估算 | 说明 |
|---|---|---|
| Flash（代码 + RO） | 约 12–18 KB + 1 KB（CRC 表） | 11 个源文件约 2 700 行；HAL 只链接被用到的模块。**上限 30 KB**（`OTA_BL_CODE_SIZE`） |
| RAM（静态） | 2 KB（下载页缓冲）+ 2 KB（控制区页暂存）+ 1 KB（ISO-TP 收）+ 1 KB（ISO-TP 发）+ 少量状态 ≈ **6.3 KB** | 远低于 128 KB。ISO-TP 缓冲随契约 `OTA_ISOTP_MAX_MSG=1024` 放大（块净荷 125→1021，全槽刷写报文数 ÷8.2） |
| 栈 | 默认 `Stack_Size`（1 KB 量级）足够：最深路径是 `BL_ParamStore`（无大局部变量） | |

---

## 6. 控制区（OTA_Cfg）——两条血线

`bl_param.c` 的写入算法（四步，不可省）：

```
[PARAM-STEP-1] 把目标页整页读入 2KB RAM 暂存（s_pageBuf）
[PARAM-STEP-2] 擦除该页（Flash 只能按 2KB 页擦）
[PARAM-STEP-3] 先回写"另一份副本"（内容取自 STEP-1 页镜像）；
               若目标页是 0x08000000，**先**回写页首 [0, 0x1D8) 的 BL 向量表
[PARAM-STEP-4] 写新值（seq = 当前 seq+1，结构 CRC 重算）
最后回读校验（magic/CRC + 逐字节 memcmp），失败返回 false（不静默）
```

* 血线 1（写目标选择）：写"**最旧的有效副本**所在位置"，但**绝不擦掉唯一最新副本所在页**——若最旧与最新同页，则改写到另一物理页的副本 0。保证任何时刻 Flash 中至少有一份有效配置。
* 血线 2（页 0 与向量表同页）：擦 `0x08000000` 前必须整页暂存，擦后**先于**配置副本回写向量表；并且**从擦除前到向量表回写完成必须全程关中断**（`__disable_irq()` / `__enable_irq()`）。
  原因（很容易漏掉、漏了就必挂）：擦除期间 Flash 忙会 stall CPU 取指，而 **SysTick 每 1 ms 就挂起一次**；一旦总线释放，挂起的 SysTick 会立刻按"已被擦成 `0xFFFFFFFF` 的向量表"取向量 → HardFault → lockup，而且复位后 SP/PC 仍读到 `0xFFFFFFFF`，等于**变砖**。关中断窗口约 30 ms（擦 25 ms + 回写 472 B 约 5 ms），远小于 IWDG 3 s，期间照常喂狗。
  该窗口不关中断时**不是小概率竞争，而是必然故障**（1 ms SysTick 对 25 ms 擦除），这是本方案必须照做的硬约束。

**残余风险（请知情）**：即便按上面做了，擦页 0 到回写向量表之间仍存在约 30 ms 窗口，期间**掉电**会丢失 BL 向量表（需 SWD 救回）。
`bl_param.c` 顶部提供开关 `BL_PARAM_PRIMARY_WRITE`：

* `1`（默认，按 Lead 裁定）：跨页冗余，允许擦 `0x08000000`，带"关中断 + 向量表回写"；
* `0`：配置只在镜像页 `0x08040000` 的两份副本间轮转，**完全不碰页 0**（向量表零风险，代价是失去跨页冗余，掉电最坏情况退化为丢失配置→按默认 IDLE_A 启动）。

---

## 7. 刷写时序与上位机（ota_host.py）约束

| 项 | 取值/要求 |
|---|---|
| 启动窗口 | BL 上电后 `OTA_BOOT_WINDOW_MS = 1000 ms` 内收到**任一完整诊断请求**（或正在收多帧 FF）即停在 recovery，否则校验并跳转 APP |
| 会话 | 扩展会话 `10 03` 后，`0x34/0x36/0x37/0x31/0x2E/0x11` 才可用；S3 = 5 s 无请求自动回默认会话 |
| `0x34` | `34 00 44 <addr4> <size4>`；`addr` 落在目标槽 `[APP基址, APP基址+OTA_IMG_MAX_PAYLOAD)` 且 **8 字节对齐**；`size` 不越槽尾。正应答 `74 20 03 FD`（maxLen = **1021**） |
| `0x34`/`FF00` 擦除耗时 | 全槽擦除 104 页 × 约 25 ms ≈ **2.6 s**（< P2* = 5 s）。上位机必须按 `50 03 00 32 01 F4` 给出的 P2*=5000 ms 等待，不能按 50 ms 超时 |
| `0x36` | `36 <seq> <data...>`；`seq = ((blockIndex-1) % 255) + 1`，1..255 循环（**255 之后回 1，0 非法**）。序号不符回 NRC `0x73`，按设备期望序号重发该块。单块净荷 ≤ **1021 B**（契约 `UDS_MAXLEN_FIELD = OTA_ISOTP_MAX_MSG - 3`） |
| `0x37` | **建议 `37 <crc32(4B 大端)>`（长度 5）**：它同时充当 FF01 的参考值与合成镜像头的 `payloadCrc`。只发 `37`（长度 1）也接受，但 FF01 只能按"长度校验"通过，日志会告警 |
| `FF01` | 正应答 **9 字节**：`71 01 FF 01 <status> <crc32(4B 大端)>`；`status`：`00` 通过 / `01` CRC 不符 / `02` 长度不符 |
| `FF02` | 设备在槽首合成 32 B 镜像头（`magic`/`imgSize=32+total`/`payloadSize=total`/`loadAddr=槽APP基址`/`payloadCrc`/`hdrCrc`），并**回读校验**（`BL_ImageValidate`：头 + 净固件 CRC + SP/复位向量）；通过回 `71 01 FF 02 00`，失败回 `... 01`。BL 不主动复位，需上位机再发 `11 01` |
| `FF03` | 回滚到另一槽；若另一槽镜像校验不通过则回结果码 `01`（拒绝把设备切进 recovery 死循环） |
| `FF04` | BL 回 NRC `0x12`（试运行确认语义在 APP 侧） |
| 续传 | `addr = 槽APP基址 + N`，`size` = 剩余长度。**注意页粒度**：擦除自"包含 addr 的整页"开始，因此若 addr 不是物理页边界，该页前半（addr 之前）也会被擦成 `0xFF`。建议续传点取物理页边界：`addr = 槽基址 + j*2048`（净荷偏移 = `j*2048 - 32`），或每次续传把该页前半一并重发 |
| 镜像头 | 由**设备侧**在 FF02 合成（模式 A）。上位机只传净荷，**不要把 32 B 头放进传输流** |

---

## 8. 中断与向量

* BL **只提供 `SysTick_Handler`**（`main.c`，HAL 时基必需）；其余异常/中断向量使用启动文件的默认弱定义。
* FDCAN 采用**主循环轮询**（`BL_CanPoll`），不调用 `HAL_FDCAN_ActivateNotification`，`HAL_FDCAN_MspInit` 里显式 `HAL_NVIC_DisableIRQ(FDCAN2_IT0_IRQn)`，因此**不需要** `FDCAN2_IT0_IRQHandler`。
* USART1 只做阻塞发送，不开接收中断（BL 只需要单向日志）。
* 跳转 APP 前：关中断 → 停 SysTick → 清 NVIC → 释放 FDCAN/USART → `VTOR=appBase` → 设置 MSP → **开中断** → 跳转（APP 不是复位进入，PRIMASK 不会被硬件清零）。

---

## 9. 待核实清单（无法编译/无网络，需在 Keil 首次编译时确认）

1. `FLASH_EraseInitTypeDef` 是否含 `Banks` 字段（G4 HAL 应为 `FLASH_BANK_1/2`）；`FLASH_OPTR_DBANK` 宏名是否正确（代码已用 `#if defined(FLASH_OPTR_DBANK)` 兜底退化）。
2. 双 Bank（DBANK=1）时页号是否按 Bank 内偏移计数——`bl_flash_if.c` 的 `BL_FlashPageOf()` 已按此实现；若实测 HAL 期望"线性页号"，只需改这一个函数。
3. `FLASH_WaitForLastOperation()` 在 `stm32g4xx_hal_flash.h` 中是否为非 static 声明（若为 static，删除两处调用即可，HAL 内部已等待）。
4. `FLASH_FLAG_ALL_ERRORS` 宏是否包含 EOP 之外的全部错误位。
5. `HAL_FDCAN_MspInit` 中 `HAL_RCCEx_PeriphCLKConfig(RCC_PERIPHCLK_FDCAN, RCC_FDCANCLKSOURCE_PCLK1)` 的字段名与既有 `stm32g4xx_hal_msp.c` 一致（已照抄）。
6. Keil scatter 中 `*(RESET, +First)` / `*(InRoot$$Sections)` 的写法与所用启动文件段名一致。
7. `sizeof(OTA_Cfg)`：**代码全程用 `sizeof`/`offsetof`，不含任何长度字面量**，因此与任何字节数字面量无关。
   权威值（`ota_common.h` 以**负数组长度 typedef** 编译期锁死，verify_bl.py 亦机械复算）：`magic/seq/state/trialLeft` 4 个 + `slotCrc[2]/slotSize[2]/slotVersion[2]` 6 个 + `lastError/crc` 2 个 = **12 个 u32 = 48 字节**，`offsetof(crc)=44`，`48 % 8 == 0` 天然满足双字对齐（无需额外补零分支，补零循环天然 0 次）。
   为何不用 `#if (sizeof(OTA_Cfg) != 48)`：`sizeof`/`offsetof` 能否出现在预处理 `#if` 表达式中属实现自定义行为，ARMCC 上不保证；负数组长度是 C90 起标准的写法，三种工具链行为一致。verify_bl.py 对两种形式都接受（见第 12 项）。
   历史教训：曾误算为 "9 个 u32 = 36 字节"（把 3 个 `[2]` 数组各按 1 个 u32 计）并由此推出"36 不是 8 的倍数"——若三方照 36 写会截断记录、`lastError`/`crc` 留在 0xFF，配置副本静默失效。契约注释、docs/01、docs/04 均已改正。

---

## 10. 静态自检

```
python ota/bootloader/verify_bl.py
```

脚本不需要网络与编译器，逐条打印 file:line 证据：括号配平、task-2 F 节 10 条不变量、
`ota_common.h` 分区常量与 `bl_flash_if.c`/`BL_BUILD.md` 的地址一致性、CRC32 表复算（Python 独立实现验证 `0xCBF43926`）、
以及"BL 不 include 应用层头"的独立性检查。全绿时退出码 0。
