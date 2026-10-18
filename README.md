# STM32G474 A/B 双槽 OTA Bootloader

在经典 CAN 总线上用标准 UDS 完成固件升级：**只写非活动槽、跳转前四重校验、试运行不通过自动回滚**。
目标不是"能把固件传过去"，而是**现场升级失败时设备还能自己站起来**。

| 项 | 值 |
|---|---|
| MCU | STM32G474VET6（512 KB Flash / 128 KB RAM，双 Bank） |
| 总线 | 经典 CAN 500 kbit/s（FDCAN2，PB12/PB13，SN65HVD230） |
| 传输层 | 自研 ISO-TP（ISO 15765-2），应用报文上限 1024 B |
| 诊断层 | 自研 UDS（ISO 14229）子集：`0x10/0x11/0x22/0x31/0x34/0x36/0x37/0x3E` |
| 分区 | Bootloader 46 KB + Slot A / Slot B 各 208 KB |
| 数据块 | 每块 1008 B（通告 1016 B，8 字节对齐以匹配 G4 双字编程） |
| 状态 | 真机全链路通过，含试运行回滚实测 |

---

## 1. 与上游诊断项目的关系

本仓库 = [`G474-DUAL-CAN`](https://github.com/1237liushengji/G474-DUAL-CAN) 的 CAN / ISO-TP / UDS
底座 + **本次新增的 Bootloader 与双槽 OTA**。

诊断协议栈本身的实现细节（FDCAN 位时序、ID 过滤、串口控制台、Python 分析仪）不在这里重复，
见上游仓库与 [`docs/01-ISO-TP传输层设计.md`](docs/01-ISO-TP传输层设计.md)、
[`docs/02-UDS诊断栈设计.md`](docs/02-UDS诊断栈设计.md)。

**本仓库的增量集中在三处：**

| 路径 | 内容 |
|---|---|
| `ota/bootloader/` | 全新 Bootloader 工程（11 个模块，独立 Keil 工程） |
| `ota/common/ota_common.h` | 分区表 / 镜像头 / 错误码的**唯一真相源**，BL、APP、Python 三方共引 |
| `G474/Drivers/User/Src/ota_agent.c`<br>`ota_client.c` | 服务端（被刷方）与客户端（发起方）状态机 |

板 A 编成 UDS 客户端（`CAN_NODE_ROLE=CAN_NODE_A`，含 `ota_client.c`），
板 B 编成 ECU（`CAN_NODE_ROLE=CAN_NODE_B`，只含 `ota_agent.c`），同一套代码两种角色。

---

## 2. 三条不变量

这套设计的所有取舍都围绕三条：

1. **只写非活动槽** —— 正在运行的固件永远不会成为擦写目标，断电最多损失一次升级，不会变砖
2. **跳转前四重校验** —— 魔数 / 头部 CRC / payload CRC32 / **独立**的栈顶与复位向量检查
3. **试运行不确认就回滚** —— 新固件跑起来后必须在窗口内"报平安"，否则 BL 把控制权交还旧槽

第 1、2 条防"写坏"，第 3 条防"写得进但跑不起来"。

---

## 3. Flash 分区

```
0x08000000  ┌──────────────────────┐
            │ 控制区（页首 ~0x1D8） │  副本0 @+0x1D8  副本1 @+0x400
0x08000800  ├──────────────────────┤
            │ Bootloader (46 KB)   │  向量表在页首，代码从 0x08000800 起
0x0800C000  ├──────────────────────┤
            │ Slot A (0x34000)     │  槽首 0x80 为保留区：32B 头 + 填充
0x08040000  ├──────────────────────┤
            │ 控制区镜像（2 KB）    │  与 Bank1 那份互为备份
0x08040800  ├──────────────────────┤
            │ Slot B (0x34000)     │
0x08074800  └──────────────────────┘
```

关键值（`ota/common/ota_common.h`）：

```c
#define OTA_SLOT_A_ADDR      0x0800C000U   /* Bank1，BL 区结束后页对齐 */
#define OTA_SLOT_B_ADDR      0x08040800U   /* Bank2 起始，取指不跨 Bank */
#define OTA_SLOT_SIZE        0x00034000U   /* 208 KB */
#define OTA_IMG_HDR_SIZE     0x00000080U   /* 槽首保留区，必须 0x80 对齐 */
```

**为什么控制区要两份、且分居两个 Bank**：写控制区需要擦整页；若只在一处，
擦除瞬间掉电就丢掉了"哪个槽有效"的唯一记录。两份 + 单调递增 `seq`，
每次只覆盖较旧的那份，任何时刻至少有一份完整。

**为什么写控制区必须落在另一个 Bank**：STM32G4 对**正在执行代码的 Bank** 擦写会冻结取指。
FF02 里 `pageBase` 按**运行槽的反侧**选择（看 `s_runningSlot`，不是目标槽）。

---

## 4. 镜像头契约（32 字节，小端）

| 偏移 | 字段 | 要求 |
|---|---|---|
| 0 | `magic` | `0x47344F54`（"G4OT"，小端落盘为 `54 4F 34 47`） |
| 4 | `imgSize` | **`payloadSize + 0x80`** |
| 8 | `payloadSize` | 纯固件字节数 |
| 12 | `version` | `0xMMmm0000`（major 在高字节） |
| 16 | `loadAddr` | 目标槽 APP 基址 = 槽基址 + 0x80 |
| 20 | `payloadCrc` | payload 的 CRC-32 |
| 24 | `hdrCrc` | CRC32(头 [0,24)) |
| 28 | `reserved` | 0 |

BL 的 `BL_ImageReadHeader()` 会独立再查四件事：

- `imgSize == payloadSize + 0x80`（写成 `+32` 会导致 `SIZE_RANGE`）
- `loadAddr` 与目标槽一致
- 初始 SP：8 字节对齐，`0x20000000 < SP ≤ 0x20020000`
- **复位向量 bit0=1 且落在 `[appBase, appBase + payloadSize)` 内**

最后一条最容易被忽略：CRC 全对、头部全对，但只要镜像是按另一个槽链接的，
复位向量就落在槽外，跳过去必然 HardFault。BL 在跳转前就拦住它。

---

## 5. 启动流程

```
上电
 ├─ 时钟 + 控制区加载（两份取 seq 较新且 CRC 通过的）
 ├─ CAN 上线
 ├─ 启动窗口 1000 ms
 │    └─ 只有"表达刷写意图"的请求才进入 recovery
 │       ※ TesterPresent(0x3E) 是保活帧，不算 —— 否则上位机一探活就把对端锁死在 BL
 ├─ TRIAL 递减（若处于试运行态）
 └─ 跳转判定：校验活动槽 → 失败试另一槽 → 都失败进 recovery
```

BL 自己也是一个 UDS 服务端（`bl_diag.c`），所以**BL 状态下同样可以被刷**——
升级中途断电、两个槽都废，仍能重新灌一份进去。

---

## 6. 试运行 / 提交 / 回滚

| 阶段 | 触发 | 行为 |
|---|---|---|
| 激活 | `31 01 FF02` | 合成槽首镜像头并回读校验 → 写控制区 `state=TRIAL_x`、`trialLeft=3` |
| 试运行 | 复位 | BL 每次启动递减 `trialLeft` 并落盘，然后跳转新槽 |
| 提交 | APP 自检 | `Ota_Task()` 在 3 s 窗口后若 CAN 收发正常 → 置 `state=IDLE_x`、`trialLeft=0` |
| 回滚 | `trialLeft` 耗尽 | BL 校验另一槽有效 → 切回并记 `lastErr=OTA_ERR_TRANSFER` |

实测回滚日志（`tools/rollback_test.sh`，在自检窗口内抢复位）：

```
BL: boot state=3 trialLeft=2 activeSlot=1
BL: jump to APP slot 1 base=0x08040880     ← 第 1 次运行
BL: boot state=3 trialLeft=1 activeSlot=1
BL: trial exhausted -> rollback to slot 0  ← 回滚
BL: jump to APP slot 0 base=0x0800C080     ← 回到旧槽，系统可用
```

> 注：`trialLeft=3` 实际提供 **2 次**运行机会——递减发生在本次跳转判定之前。
> 这是已知且已注释的行为；要跑满 3 次需把 `OTA_TRIAL_BOOT_LIMIT` 提到 4。

镜像本身不合法（CRC / 向量错）时不会进入 TRIAL，BL 在跳转前就判无效并试另一槽。

---

## 7. 刷写时序

```
诊断仪(板A)                          ECU(板B)
    │  3E 00            ────────────▶│  TesterPresent
    │  10 03             ────────────▶│  进扩展会话
    │  22 F1 81          ────────────▶│  读活动槽 → 决定目标槽
    │  34 00 44 + addr + size ────────▶│  擦除目标槽
    │  ◀──────────── 7F 34 78         │  responsePending（擦除中）
    │  ◀──────────── 74 20 xx xx xx   │  maxNumberOfBlockLength = 1016
    │  36 + seq + 1008B  ────────────▶│  逐块写入（ISO-TP 多帧）
    │  ◀──────────── 76 + seq         │
    │        ⋮  重复 74 次  ⋮          │
    │  37                ────────────▶│  收尾
    │  ◀──────────── 77 + crc32       │
    │  31 01 FF01        ────────────▶│  按 CRC32 校验整槽
    │  ◀──────────── 71 01 FF 01 00 + crc32
    │  31 01 FF02        ────────────▶│  合成镜像头 + 置 TRIAL
    │  ◀──────────── 71 01 FF 02 00   │
    │  11 01             ────────────▶│  复位，进入试运行
```

`0x78`（responsePending）必须被客户端识别为"继续等"而不是失败——
擦一个 208 KB 槽的时间远超 P2 默认的 50 ms。

---

## 8. 工程结构

```
ota/
├── common/ota_common.h      ← 分区表 / 镜像头 / 错误码，三方共引的唯一真相源
├── bootloader/              ← BL 独立 Keil 工程
│   └── Core/Src/
│       ├── main.c           启动状态机（窗口 / TRIAL / 跳转判定）
│       ├── bl_image.c       镜像头解析与四重校验
│       ├── bl_param.c       控制区双副本 + seq 管理
│       ├── bl_jump.c        跳转前收尾
│       ├── bl_diag.c        BL 内的 UDS 子集
│       ├── bl_flash_if.c    擦写封装（页对齐 / 双字编程）
│       └── bl_can / bl_isotp / bl_uart / bl_clock / bl_crc32
├── scripts/                 ← 构建与镜像工具
└── tools/ota_host.py        ← PC 上位机（镜像检查 / 打包）

G474/Drivers/User/Src/
├── ota_agent.c              ← ECU 侧：0x34/0x36/0x37/0x31 实现 + 页缓冲落盘
├── ota_client.c             ← 诊断仪侧：完整升级状态机 + 块级重发
├── ota_tunnel.c             ← 串口 ↔ 诊断 CAN 隧道
├── isotp.c / uds.c          ← 上行协议栈（见上游仓库）
└── can.c / bsp_uart.c / ...

tools/                       ← 开发期脚本（抓包 / SWD 取证 / 回滚验证）
docs/                        ← 设计文档，16 号是本项目的操作手册
```

---

## 9. 构建与烧录

### 9.1 首次烧录（产线 / 换板）

**顺序不能颠倒**：必须先整片擦除。BL 的控制区位于 `0x08000000 + 0x1D8`，
与向量表同页——该页只能编程、不能单独擦除，若此处残留旧代码，控制区永远写不进去。

```bash
P=STM32_Programmer_CLI            # 不在 PATH 时用 CUBEPROG 指定全路径
SN=<ST-Link 序列号>

"$P" -c port=SWD sn=$SN mode=UR -e all                       # 1) 整片擦除
"$P" -c port=SWD sn=$SN mode=UR -w firmware/BL.hex -v        # 2) Bootloader
"$P" -c port=SWD sn=$SN mode=UR -w <APP_factory.hex> -v -rst # 3) APP（带 32B 头的工厂镜像）
```

生成工厂镜像：

```bash
python ota/scripts/make_factory_image.py --hex <raw_app.hex> --slot A --version 8.2
```

> 不要直接烧裸 APP hex：`0x0800C000` 处没有镜像头，BL 判无效后停在 recovery，
> 表现为"上电没反应"。

### 9.2 在线升级

两板各自烧好后上电，板 A 等待 5 s（让对端 BL 的 1 s 启动窗口过去）自动开始。
板 A 串口应依次出现：

```
tester present OK → session=0x03 OK → download OK
→ all ... bytes sent → FF01 verify OK → FF02 activate OK → flow DONE
```

三条验收判据：

1. 板 A 日志走到 `flow DONE`
2. 板 B 的 BL 打印 `jump to APP slot <目标槽>` 且 `lastErr=0x00000000`
3. SWD 读 `0xE000ED08`（VTOR）等于目标槽向量表地址

### 9.3 换槽升级

APP 不是位置无关代码：Slot A 链接在 `0x0800C080`、Slot B 在 `0x08040880`，
相差 `0x34800`，**一份二进制不可能两槽通用**。两份同时内嵌也放不下
（`2×73.9 KB + APP ≈ 238 KB > 208 KB`）。

因此按目标槽重新生成内嵌 payload：

```bash
python tools/make_ota_payload.py --target-slot B --version 8.3   # 刷到 Slot B
python tools/make_ota_payload.py --target-slot A --version 8.3   # 刷回 Slot A
```

脚本会切 uvprojx 链接地址 → 编译 → 生成带头的镜像 → 转内嵌数组 → 还原工程 → 重建板 A；
异常中断也不会把工程留在错误构型（`finally` 还原）。

---

## 10. 工具

| 脚本 | 用途 |
|---|---|
| `ota/scripts/make_factory_image.py` | 给裸 APP 加 32B 头并自校验 |
| `ota/scripts/gen_ota_image_c.py` | 工厂镜像 → 内嵌 C 数组 |
| `tools/make_ota_payload.py` | 按目标槽一键生成并内嵌 payload |
| `ota/tools/ota_host.py` | PC 上位机：`--info` / `--pack` / `--make-sample` |
| `tools/rollback_test.sh` | 抢复位构造"未确认"场景，验证自动回滚 |
| `tools/quiet_capture.py` | 串口抓包（DTR/RTS 保持低，避免误复位） |
| `tools/serial_rx_test.py` | 判定串口 RX 是否物理连通 |

开发期脚本依赖 ST-Link + STM32CubeProgrammer + pyserial，
路径可用 `CUBEPROG` / `KEIL_UV4` / `PYTHON` 环境变量覆盖。

---

## 11. 实测数据

| 项 | 结果 |
|---|---|
| payload | 73824 B（74 个数据块 × 1008 B） |
| 传输 CRC32 | `0x9F25DF4C`，与 FF01 回读一致 |
| 数据块重传次数 | **0** |
| FF02 | `71 01 FF 02 00` 成功 |
| BL 跳转 | `jump to APP slot 1 base=0x08040880`，`lastErr=0x00000000` |
| 运行态 | `VTOR=0x08040880`，PC 落在 Slot B 内 |
| 回滚 | `trial exhausted -> rollback to slot 0`，自动回到 Slot A 并正常运行 |

---

## 12. 已知限制

1. **不支持 A/B 双向反复升级** —— 受单槽 payload 容量限制，换槽需重跑 `make_ota_payload.py`
2. **板 A 自身不能被 OTA** —— 它只有客户端角色，没有对端客户端给它发固件
3. **本批开发板串口 RX 走线开路**（PA10 无外部驱动），现场只能靠 CAN 触发升级或 SWD 烧录
4. **`.noinit` 区未在 linker 中声明**，`g_bootCnt` / `g_rstIwdgCnt` 等跨复位计数无效
5. **上位机物理链路未验证** —— `ota_host.py` 的镜像格式已与固件对齐并离线验证，
   但串口 / CAN 会话未在真机跑通，受第 3 条制约

---

## 13. 文档

| 文档 | 内容 |
|---|---|
| [`docs/16-OTA升级与回滚操作手册.md`](docs/16-OTA升级与回滚操作手册.md) | **本项目主文档**：分区表、镜像头契约、升级/回滚 SOP、四张故障码表、排障清单 |
| [`docs/01-ISO-TP传输层设计.md`](docs/01-ISO-TP传输层设计.md) | 传输层状态机与流控 |
| [`docs/02-UDS诊断栈设计.md`](docs/02-UDS诊断栈设计.md) | 诊断服务与 NRC 设计 |
| [`docs/03-诊断演示与验收手册.md`](docs/03-诊断演示与验收手册.md) | 诊断功能的验收步骤 |

---

## 14. 许可

MIT，见 [LICENSE](LICENSE)。
