# 12 · BL → APP 终于跑通：里程碑与剩余瓶颈

> 状态：**`ota/docs/09` 里悬了很久的"APP 能否被跳转启动"已经有确定答案：能，而且已在两块板上跑通。**
> 本文记录这一刻的实测证据、本轮一共修掉的四个根因、以及**当前唯一的剩余瓶颈**。

---

## 1. 里程碑：APP 在两块板上都跑起来了

串口实测（两块板各自的口）：

```
BL: jump to APP slot 0 base=0x0800C080 size=0xCB78       ← BL 校验通过并跳转
=== STM32G474 CAN Node v3.0.0 | role:A(master) | Oct  5 2026 17:15:57 ===
type 'help' for commands
> main: led-bsrr-state M3                                ← main.c 构建指纹行
```

板 B 同样是 `type 'help' for commands` + `> main: led-bsrr-state M3`
（角色镜像不同：A 的 payload=`0xCB78`、B 的 payload=`0xD6C8`）。

**这条链路上此前一共挡着四个根因，现在全部清掉：**

| # | 根因 | 现象 | 修法 | 文档 |
|---|---|---|---|---|
| 1 | BL 代码被链接进控制区页 | BL 第一次写控制区就把自己复位向量擦成 `0xFF` → 变砖 | `bl.sct` 页 0 只放向量表 + `umfTarg=0` + `BL_ParamStore` 整页恢复 | `docs/10` |
| 2 | 散列文件 `UNINIT` 导致 `.bss` 不清零 | UART 句柄 `ClockPrescaler` 是随机值 → 越界查表 → 总线错误 → HardFault | 去掉 `UNINIT` + 句柄显式清零 | `docs/10` |
| 3 | `VTOR = 槽基址 + 0x20` 不是 0x80 对齐 | 硬件吞掉低 7 位 → 向量表落到镜像头 → INVSTATE/IBUSERR → lockup | 槽首保留区 32 → `0x80`（APP 基址 `0x0800C080`） | `docs/11` |
| 4 | APP 启动比 BL 留下的看门狗慢 | APP 刚配好串口就被狗拉回，横幅永远打不出来 | 启动早期延时切块喂狗（`App_EarlyDelay`） | 本文 §2 |
| 4b | `HAL_IWDG_Init` 在 IWDG 已运行时必然超时 | 走进 `Error_Handler()` 关中断死循环 → 3 秒后复位 | 该失败改为**非致命**（沿用 BL 留下的同配置） | 本文 §2 |

---

## 2. 根因 4/4b：看门狗交接（这是"最后一公里"）

BL 跳转前，**独立看门狗已经在跑**（IWDG 3 秒），而 IWDG 一旦启动**无法关闭、也无法重配置**。
APP 必须自己接着喂，直到主循环接管。可 APP 启动早期偏偏有几段长延时：

* 3 段 LED 打点（HSI 16MHz 下合计约 2~3 秒）
* `LCD_Init()` 内含约 760ms 延时

累计超过 3 秒 → 还没跑到主循环就被狗拉回。

**两个具体修法**（都在 `G474/Core/Src/main.c`）：

1. `App_EarlyDelay(nops)`：把空转延时**切块**，每块 ≤20 万次空转（16MHz 下约 0.1s）就
   `HAL_IWDG_Refresh()` 一次；三段 LED 打点全部改用它。
   ⚠️ 必须加 `__attribute__((noinline))`：内联后"切块上界"成了编译期常量，
   ARMCLANG 会把空转循环**整体展开** —— 实测 `main` 从 1KB 涨到 **12552 字节**；
   加 `noinline` 后回到 **1104 字节**（与 BL 侧 `BL_DiagDelay` 是同一个坑）。
2. `HAL_IWDG_Init()` 的失败分支**不再 `Error_Handler()`**：从 BL 跳进来时 IWDG 已在跑，
   PR/RLR 变只读，HAL 等 PVU/RVU 必然超时返回 `HAL_TIMEOUT`。
   本工程 APP 与 BL 的 IWDG 配置完全相同（LSI/32 = 1kHz、reload 3000 = 3s），
   直接沿用即可，主循环照常喂狗。
   **曾经的后果**：`Error_Handler()` → `__disable_irq()` 死循环 → SysTick 停摆、
   APP 滴答冻结在 3482ms → 3 秒后复位 → 无限循环。

---

## 3. 顺带解决的一个"工具级"大坑：DTR/RTS 把板子按在复位上

整场排查里有一件事一直很反直觉：**用户的 XCOM 能看到日志，而 `serial_capture.py` 一个字节都收不到。**

原因：pyserial **打开串口时默认会把 DTR/RTS 拉高**，而很多 CH340/CP210x 小板把
DTR/RTS 接到了目标板的 **NRST/BOOT0**（自动复位下载电路）。于是"工具一打开端口，
板子就被按在复位上" —— 看不到任何输出，而且**还会让人误判成固件没跑**。

修法（`ota/scripts/serial_capture.py`）：打开后立刻 `dtr = False; rts = False`。
改完当场就收到了两块板的完整启动日志。

> 教训：**取证工具本身会改变被观测对象**（这次是电平层面）。
> 串口收不到数据时，除了查线序，也要查 DTR/RTS。

---

## 4. 当前唯一的剩余瓶颈：APP 死在 `CAN_Init()`

现在的循环是**规律 3.15 秒**（正好是 IWDG 的 3 秒 + 启动耗时）：

```
BL: jump to APP ...            (t=0)
=== STM32G474 CAN Node ... === (t=+0.2s)   ← APP 起来了
type 'help' for commands
> main: led-bsrr-state M3
（3.15s 后）
BL: clock HSE+PLL 150MHz OK    ← 被看门狗复位，重新来过
```

判据有两条，指向同一个结论：

1. **`CAN self-test: PASS/FAIL` 这一行始终没有出现** —— 而它就印在 `CAN_Init()` 之后
   （`main.c`：`CAN_Init()` → `CAN_SelfTest()` → 打印 PASS/FAIL），说明**卡在 `CAN_Init()`**；
2. 之前用调试器测到 **APP 滴答冻结在 3482ms 不再增长** —— 滴答停摆 = **中断被关**，
   正是 `Error_Handler()`（`__disable_irq(); for(;;)`）的形态，
   即 `CAN_Init() != HAL_OK` 走了 `Error_Handler()`。

**下一步（待 ST-Link 接回后做）**：

1. 把 `CAN_Init()` 失败分支也改成**非致命 + 打印失败步骤**（现在只有一个笼统的 `Error_Handler()`，
   现场什么也看不到）—— 先让它把"哪一步失败/返回什么"打出来；
2. 重点怀疑对象：BL 跳转前调用了 `BL_CanDeInit()`，APP 再从该状态重新 `HAL_FDCAN_Init`
   是否被卡在 init 模式（FDCAN 的 CCE/INIT 位等待超时）；
3. 拿到具体失败码后对症修，然后就能回到 `docs/06` 的阶段二/三
   （CAN 双向、LED 同步、`0x34/0x36/0x37` 刷写门槛）。

---

## 5. 本轮验证快照（全绿）

```
python ota/verify/verify_all.py       -> 29 项检查, 29 通过, 0 失败
python ota/verify/verify_selftest.py  -> 10 个反例, 10 个被抓到
python ota/tools/test_ota_host.py     -> Ran 111 tests, OK
python ota/scripts/verify_layout.py   -> BL 与 APP 链接地址/体积均与分区表一致
python ota/scripts/audit_bl_layout.py -> 镜像未压入控制区保留区
```

两块板当前固件（均已实测跳转成功）：

| 板 | 角色镜像 payload | 域 | 状态 |
|---|---|---|---|
| A | `0xCB78`（A/master） | `loadAddr=0x0800C080` | ✅ APP 启动并打印横幅 |
| B | `0xD6C8`（B/slave）  | `loadAddr=0x0800C080` | ✅ APP 启动并打印横幅 |
