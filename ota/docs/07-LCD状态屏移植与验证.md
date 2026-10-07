# 07 · ST7789 240×280 状态屏：移植与验证

> 目标：把 LCD 接进 OTA 系统，让**现场不接 PC 也能看出设备状态**。
>
> 本文件记录方案 C（最小验证）的落地情况与上板判据；方案 A（Bootloader 状态屏）见 §5。

---

## 1. 硬件与接线

**LCD 排座**接入（与厂商 LCD 例程完全一致，无需改线）：

| 信号 | 引脚 | 说明 |
|---|---|---|
| `LCD_CS` | `PD11` | 片选，低有效 |
| `LCD_SCL` | `PB3` | 软件 SPI 时钟 |
| `LCD_SDA` | `PB5` | 软件 SPI 数据 |
| `LCD_DC` | `PD12` | 数据/命令选择 |
| `LCD_BL` | `PD13` | 背光 |
| — | **无 RST 引脚** | 复位由模块内部上电复位电路处理 |

**与既有外设无冲突**：

```
FDCAN2 = PB12/PB13      USART1 = PA9/PA10      LED = PE0
LCD    = PB3/PB5/PD11/PD12/PD13
```

> ⚠️ `PB3` 是 `SWO`、`PB4` 是 `NJTRST`，属 JTAG 复用脚。
> **SWD 调试（PA13/PA14）不受影响**，照样能下载与断点；只是放弃 SWO 追踪输出。
> 若你的调试器配的是 JTAG-DP Enabled，需改为 **JTAG-DP Disabled + SWD Enable**。

---

## 2. 驱动来源与移植决策

直接用**用户产品"工业数据采集终端"里已在跑的 `BSP/bsp_lcd.c`**，而不是参考例程那套。理由：

| | 产品的 `bsp_lcd.c` | 参考例程 `spi.c` |
|---|---|---|
| 是否在本板跑过 | ✅ 是 | 是（但属厂商例程） |
| 接口 | `LCD_Print(row,col,...)` / `LCD_PrintField` / `LCD_ClearRow` | 画线/画圆/时钟等图形 API |
| 尺寸 | 19 KB | 54 KB |
| 中间缓冲 | 无 | `LCD_Buff[1024]`（多占 2 KB RAM） |
| 状态屏契合度 | **高**（按字符格排版 + 定宽覆盖防残影） | 低 |

**为什么用软件 SPI**：那颗 W25Q128 占着硬件 SPI1 的 `PA5/6/7`，LCD 走 `PB3/PB5`；
位操作保持两条总线独立、代码自包含。软 SPI 的代价是刷新慢（全屏约 1 秒），
但状态屏只改动的行，完全够用。

---

## 3. 本工程实际改动

| 文件 | 改动 |
|---|---|
| `G474/Drivers/User/Inc/app_board_cfg.h` | **新增**：只摘出 LCD 引脚宏（最小化，不拖进整套产品配置） |
| `G474/Drivers/User/Inc/bsp_lcd.h` | **新增**：从产品复制 |
| `G474/Drivers/User/Src/bsp_lcd.c` | **新增**：从产品复制，去掉未使用的 `bsp_tick.h` 依赖 |
| `G474/Core/Src/main.c` | 加 `#include "bsp_lcd.h"` / `<stdio.h>`；`LCD_Init()` + 启动画面；主循环加 `LcdUi_Task()` |
| `G474/MDK-ARM/G474.uvprojx` | 注册 `bsp_lcd.c` |

**关键顺序**：`LCD_Init()` 内部有约 **760 ms** 的 `HAL_Delay` 初始化序列，
**必须放在 `HAL_IWDG_Init()` 之前**——否则 3 秒看门狗会在初始化中途把 MCU 复位。
当前实现中 `LCD_Init()` 在第 381 行、`HAL_IWDG_Init()` 在第 392 行，顺序正确。

**HAL 模块**：只用 `HAL_GPIO_*` 与 `HAL_Delay`，`HAL_GPIO_MODULE_ENABLED` 已开；
**不需要** `HAL_SPI_MODULE_ENABLED`（软件 SPI）。

### 资源开销（实测）

| | 移植前 | 移植后 | 增量 |
|---|---|---|---|
| Code | 39 642 | **51 402** | +11 760 |
| RO-data | 2 622 | 3 126 | +504 |
| RW-data | 56 | 56 | 0 |
| ZI-data | 7 888 | 7 896 | +8 |
| **Flash 占用** | 50 208 | **62 480** | +12 272 |
| Slot A 余量 | 162 784 | **150 512** | — |

结论：**只占 Slot A 的 30%**，不影响分区表。

---

## 4. 上板验证（方案 C）

### 4.1 预期现象

| # | 现象 | 说明 |
|---|---|---|
| 4.1 | 上电后**背光先亮**，随后屏幕由白变黑 | `LCD_Init()` 开头即点亮背光 |
| 4.2 | 第 1 行显示青色 `STM32G474 OTA SYSTEM` | 若**全白**或**花屏** → 初始化时序/接线问题 |
| 4.3 | 第 2 行显示绿色 `LCD OK / soft-SPI 240x280` | 文字能显示说明字库与坐标都正常 |
| 4.4 | 第 6/7 行每秒更新 `uptime` 与 `tick` | **证明不是在画死画面** |
| 4.5 | 第 9 行 `|` 与空格每秒交替（红色） | 最小刷新指征 |
| 4.6 | 颜色正确（青/绿/白/黄/红分明） | 若红蓝互换 → MADCTL 的 BGR 位需调整 |

### 4.2 故障对照

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| **全白屏** | SPI 没通 / CS 或 DC 接错 | 查 `PD11`(CS)、`PD12`(DC) 焊接与排座接触 |
| **全黑但背光亮** | 初始化命令序列未生效 | 查 `PB3`(SCL)、`PB5`(SDA) |
| **背光不亮** | `PD13` 或背光供电 | 量 `PD13` 电平；部分模块背光需外部三极管 |
| **花屏/雪花** | 时钟太快或接线过长 | 软 SPI 已在 `HAL_Delay` 节奏内，优先查接触 |
| **画面上下颠倒** | `LCD_MADCTL_VAL` 需改 | 240×280 面板可见区在 GRAM 第 20 行起（`LCD_Y_OFFSET=20`） |
| **左右镜像** | MADCTL 的 MX 位 | 改 `LCD_MADCTL_VAL` |
| **红蓝互换** | MADCTL 的 BGR 位 | 同上 |
| **文字缺列/错位** | 字库与 `LCD_FONT_SCALE` 不匹配 | 当前 5×7 字模放大 2 倍 → 12×16 字符格 |
| **上电约 3 秒后复位** | `LCD_Init()` 被排到 IWDG 之后 | 确认 `LCD_Init()` 在 `HAL_IWDG_Init()` **之前** |
| **整屏刷新很慢（约 1 秒）** | 软 SPI 固有 | 属预期；状态屏只改动的行，不要整屏重绘 |

### 4.3 用命令交叉验证"屏没拖慢系统"

屏接入后，OTA/诊断功能必须**毫发无损**：

```powershell
python ota\tools\ota_host.py --port COM5 --precheck    # 预检仍应正常
```

板 A 串口 `stats`：`tx` 持续增长、`ovr=0`、`busOff=0`。
若 `ovr` 开始增长 → LCD 阻塞太久影响了 CAN 收发，需要降低刷新频率或缩短打印。

---

## 5. 下一步：方案 A（Bootloader 状态屏）

方案 C 通过后，真正的价值点在 **Bootloader**：recovery 模式恰恰是
**设备已出问题、且现场很可能没有 PC** 的时候，而 BL 目前只往串口打印。

### 5.1 需要显示的

| 行 | 内容 | 解决的问题 |
|---|---|---|
| 1 | `RECOVERY` / `APP RUNNING` | 一眼看出卡在哪一步 |
| 2 | 活动槽 `A`/`B` + 版本号 | 不接电脑就知道跑的是哪版 |
| 3 | 升级进度条 + 百分比 | 现场不用守串口 |
| 4 | `CRC FAIL` / `ROLLBACK` / 错误码 | **把"变砖"变成"可诊断"** |

### 5.2 必须解决的两个约束

**① BL 区容量不够**

```
当前 BL 实测           32 752 字节 (32.0 KB)
+ LCD 驱动             约 11.8 KB      （与 APP 实测增量相同）
+ 字符串/排版           约 1 KB
= 约 45.6 KB   >  46 KB 预算（仅剩 0.4 KB，太紧）
```

需把 BL 区由 **46 KB 扩到 64 KB**，Slot A 由 208 KB 收到 **200 KB**：

```
0x08000000   2 KB   向量表 + 控制区主份
0x08000800  64 KB   Bootloader                     ← 46 KB 扩大到 64 KB
0x08010000 192 KB   Slot A（0x0800C000 → 0x08010000，大小 0x30000）
0x08040000   2 KB   控制区镜像
0x08040800 192 KB   Slot B
```

> ⚠️ Slot A 起点从 `0x0800C000` 变为 `0x08010000`、大小 208 KB → 192 KB，
> 因此 **APP 的 `IROM1`/`VECT_TAB_OFFSET` 也要跟着改**（`0x800C080` → `0x8010020`）。
> 这属于**分区表变更**，必须：
> 1. 改 `ota/common/ota_common.h` 的常量与编译期断言；
> 2. 改两个工程的内存布局；
> 3. 重跑 `ota/scripts/verify_layout.py` 与 `ota/verify/verify_all.py`；
> 4. 同步文档中所有地址。

**② 不能用 `printf` 浮点/大格式化**

BL 体积极敏感，状态屏只保留**自造的精简字库**（数字 + 大写字母 + 冒号/斜杠等），
几百字节即可，**不要**搬产品里那 105 KB 的中文字库。

### 5.3 建议的推进顺序

```
1. 先按本文档验完方案 C（屏能亮、能刷新、颜色对）
2. 再改分区表（BL 64 KB / Slot A 192 KB），两个工程重新验证
3. 把 bsp_lcd.c 复制进 BL 工程 + 精简字库
4. BL 状态屏：RECOVERY / 槽位 / 版本 / 进度 / 错误码
5. 重跑全部守卫 + 上板走一遍 ota/docs/06 的六个阶段
```

---

## 6. 相关脚本

| 脚本 | 作用 |
|---|---|
| `ota/scripts/port_lcd.py` | 从产品 BSP 移植 LCD 驱动（可重复运行） |
| `ota/scripts/wire_lcd.py` | 把 LCD 接入 `main.c` 并注册进 Keil 工程（幂等） |
| `ota/scripts/check_ota_intact.py` | 确认 OTA 改动未被覆盖（改完必跑） |
| `ota/scripts/fix_app_irom.py` | 修正 APP 链接基址（`IROM` 与 `OCR_RVCT4` 必须一致） |
| `ota/scripts/verify_layout.py` | 核对链接地址与体积 |
