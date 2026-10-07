# 工作区说明（OTA_Bootloader）

> 本目录 = **上游仓库 `stm32g474-dual-can` 的完整副本** + **本次会话产出的 OTA/Bootloader 交付物**。
> 上游文件已与 GitHub 官方归档逐字节核对一致（仅 7 个文件因 OTA 改动而不同，见下）。

---

## 目录结构

```
OTA_Bootloader/
├── G474/                  ← 上游：APP 工程（Keil），已接入 OTA 并链接到 Slot A (0x0800C080)
│   ├── Core/                    CubeMX 生成的 Core
│   ├── Drivers/
│   │   ├── User/                业务代码：can/isotp/uds/console/bsp_uart/led + 新增 ota_agent/ota_tunnel
│   │   ├── STM32G4xx_HAL_Driver/
│   │   └── CMSIS/
│   └── MDK-ARM/G474.uvprojx     ← APP Keil 工程（IROM1 = 0x800C080）
├── ota/                   ← 本次交付物
│   ├── README.md                方案总入口
│   ├── docs/01..05-*.md         设计文档（分区/集成/协议/回滚/测试）
│   ├── common/ota_common.h      ★ 三方共用契约头（分区表、镜像格式、编译期断言）
│   ├── app/ota_agent.* ota_tunnel.*   APP 侧源码（已同步到 G474/Drivers/User/）
│   ├── bootloader/              Bootloader 自包含工程
│   │   ├── Core/Src|Inc/        11 个 .c + 12 个 .h（含 CubeMX 生成的 RTE_Components.h）
│   │   ├── MDK-ARM/BL.uvprojx   ← BL Keil 工程（IROM1 = 0x08000000）
│   │   └── BL_BUILD.md, verify_bl.py
│   ├── tools/ota_host.py        PC 上位机刷写工具（+ 单元测试）
│   ├── verify/                  跨库静态守卫 + 反例自证
│   └── scripts/                 构建与复核脚本（见其 README.md）
├── docs/                  ← 上游设计文档（ISO-TP / UDS / 验收手册）
├── tools/can_console.py   ← 上游串口控制台工具
├── README.md, LICENSE, .gitignore
```

---

## Flash 分区表（唯一真相源：`ota/common/ota_common.h`）

| 地址 | 大小 | 名称 | 说明 |
|---|---|---|---|
| `0x08000000` | 2 KB | BL_HEADER | BL 向量表（页首 ~0x1D8）+ 控制区主份两份副本 |
| `0x08000800` | 46 KB | BL_CODE | Bootloader 代码（实测 32 KB，余量 ~6 KB） |
| `0x0800C000` | 208 KB | SLOT_A | APP 槽 A；**APP 链接基址 `0x0800C080`**；恰好抵 Bank1 上界 |
| `0x08040000` | 2 KB | BL_HEADER_MIR | 控制区镜像份（更新 Slot B 时随 Bank2 被擦，故需镜像） |
| `0x08040800` | 208 KB | SLOT_B | APP 槽 B；APP 链接基址 `0x08040880` |

编译期断言（`ota_common.h`）会在分区越界/重叠/跨 Bank 时直接 `#error`。

---

## 两个 Keil 工程

| 工程 | 路径 | 链接基址 | VECT_TAB_OFFSET | 状态 |
|---|---|---|---|---|
| **Bootloader** | `ota/bootloader/MDK-ARM/BL.uvprojx` | `0x08000000` | `0x0` | 已编译 0 Error / 0 Warning |
| **APP (Slot A)** | `G474/MDK-ARM/G474.uvprojx` | `0x0800C080` | `0xC080` | 已编译 0 Error / 0 Warning |

> Slot B 版 = 复制 APP 工程，改 `IROM1` 为 `0x8040820`、`VECT_TAB_OFFSET` 为 `0x40880`。

---

## 与上游的差异（权威核对结果）

以 GitHub 官方归档（`codeload.github.com` 下载）为基准逐文件对比：

* **缺失：0**
* **内容不同：8 个**
  * `G474/Core/Src/main.c`、`system_stm32g4xx.c`
  * `G474/Drivers/User/Inc/isotp.h`、`Src/can.c`、`Src/uds.c`、`Src/console.c`
  * `G474/MDK-ARM/G474.uvprojx`
  * `G474/MDK-ARM/G474.uvoptx`（Keil 本地会话文件，本就逐人不同）
* **新增：4 个** —— `G474/Drivers/User/{Inc,Src}/ota_agent.*`、`ota_tunnel.*`
* 其余 300+ 个上游文件**逐字节未改动**

---

## 快速验证（不需要硬件）

```powershell
python ota\verify\verify_all.py       # 29 项静态守卫（BL 15 + 跨库 14）
python ota\verify\verify_selftest.py  # 守卫自身的反例自证（10/10）
python ota\tools\test_ota_host.py     # 上位机单元测试（111 条）
python ota\scripts\verify_layout.py   # 核对两个工程的链接地址与体积（需先构建）
python ota\scripts\audit_bl_layout.py # ★ 审计 BL 镜像有没有压进控制区页（会变砖）
python ota\scripts\inventory.py       # 盘点工作区结构
```

## 上板前必做

1. **Keil 编译两个工程**，确认 0 Error；
2. `python ota/scripts/verify_layout.py` 确认链接地址与分区表一致；
3. 🔴 `python ota/scripts/audit_bl_layout.py` 确认 **BL 镜像没有压进控制区页**
   （页 0 只能有 472 字节向量表；一旦压进去，第一次写控制区就会把 BL 自己擦成砖）；
4. 核对 `ota/bootloader/BL_BUILD.md §9` 的 HAL 名称清单（本机无编译器时唯一无法自证的部分）；
5. **烧录顺序：先烧 BL（`0x08000000`）→ 再用上位机刷 Slot A**。
   若先烧 APP 而无 BL，`0x08000000` 为空 → 上电 HardFault。
6. **判断"板子在不在跑"必须断开调试器**：CubeProgrammer 的 `mode=UR` **和** `mode=normal`
   连接都会把内核停在复位向量（实测 `PC = 0x0800092C`），连着调试器读状态必然得到错误结论。

---

## 四个必须知道的操作陷阱

**① Keil 会重写工程与源文件。** 它曾多次把 `IROM1`、`VECT_TAB_OFFSET` 宏、`main.c` 的接线改动
覆盖回原版。**每次用 GUI 打开并保存过工程后，重跑 `verify_layout.py` 与 `ota/scripts/fix_main_c.py`。**

**② 吞吐优化是三处联动。** `ISOTP_MAX_MSG`（APP 的 `isotp.h`）、契约头 `OTA_ISOTP_MAX_MSG`、
上位机 `ota_host.py` 必须同值，否则会出现"上位机按 1021 发、设备只收 128"的静默溢流失败。
契约头已加 `#if` 断言，跨库数值由 `verify_all.py` 守卫。

**③ Keil 会用 Target 对话框的 IROM/IRAM 悄悄覆盖分散加载文件。**
工程里 `<umfTarg>`（"使用 Target 对话框内存布局"）**必须为 0**，否则 `bl.sct` 形同虚设，
µVision 会按对话框重新生成 —— 而对话框里的 `0x08000000 + 0xB800` 会让代码铺进页 0，
**直接导致变砖**（2026-10-05 事故的另一半原因，详见 `ota/docs/10`）。
改完用 `python ota/scripts/fix_bl_region.py`（不带 `--apply` 只检查）复核。

**④ 串口助手会占住 COM 口。** XCOM/串口助手打开着端口时，任何抓取工具都会
`PermissionError(13)`。抓日志前先在助手里点"关闭串口"。

---

## 当前硬件验证状态（重要）

| 项 | 状态 |
|---|---|
| 两块板 BL | ⚠️ **曾变砖**：BL 代码被链接进控制区页，`BL_ParamStore` 写配置时整页擦除把它自己擦掉 → 复位向量指向 `0xFF`。**根因已修**（scatter + `umfTarg=0` + 整页恢复），见 [`ota/docs/10`](ota/docs/10-变砖根因与修复-镜像压入控制区页.md) |
| 硬件（LED/串口/ST-Link/供电） | ✅ 已验证（裸机诊断固件 100% 通过）；断电重上电时 LED 会闪几下 = MCU 在跑 |
| 镜像头字节序 bug | ✅ **已修**（见 `ota/docs/09`） |
| 调试器的影响 | ⚠️ **已定性**：`mode=UR` 与 `mode=normal` 连接都会把内核停在复位向量（实测 `Core is halted`，`PC = 0x0800092C`）→ 判断"是否在跑"必须**断开调试器** |
| APP 能否被跳转启动 | ❓ **仍待确认**（缺一次不接调试器的干净取证；横幅会被"USB 转串口随板断电重枚举"错过，见 `ota/docs/10` §7.1） |
| OTA 全流程（`0x34/36/37` + 回滚 + 掉电） | ❌ **尚未在硬件上跑过** |

**重新上板时请先读 [`ota/docs/09-上板启动问题排查与交接.md`](ota/docs/09-上板启动问题排查与交接.md)**
—— 里面有完整恢复步骤、判据表、以及**已被证明是错误做法的清单**（避免重走弯路）。

### 烧录三件事（缺一不可）

```powershell
# 1) 产线镜像必须带 32B 头（裸 APP 烧进去 BL 会拒绝，表现为"上电没反应"）
python ota/scripts/make_factory_image.py --hex firmware/G474_A.hex --slot A --version 3.0

# 2) 用 flash.py 烧（绕开 Keil"只能烧工程输出"的限制）；默认只写不擦，BL 与 APP 互不影响
python ota/scripts/flash.py --file firmware/G474_A_factory.hex --yes

# 3) 读回核对芯片实际内容（并按 BL 判据判断"BL 会不会接受"）
python ota/scripts/dump_flash.py --all
```

> ⚠️ **判断"是否在运行"必须断开调试器冷启动**：CubeProgrammer 的 `mode=UR`（复位下连接）
> 是"连接即 halt"，PC 会永远停在复位向量，用它判断运行状态必然得到错误结论。

---

## 本工作区的来源历史（供追溯）

上游文件最初因 `git clone` 与 `raw.githubusercontent.com` 被网络阻断，改用 `api.github.com`
逐文件下载（受 60 次/小时配额限制，中途耗尽）。其余部分从本地 E 盘同源副本补齐，
事后用 **`codeload.github.com` 官方 zip** 做了全量逐字节核对：**除有意修改的 7 个文件外完全一致**。

> 教训：`codeload.github.com`（仓库 zip 打包域名）不受 API 配额限制，一次请求即可取全仓。
> 遇到 `raw.githubusercontent.com` / `git clone` 不通时，应优先试它。
