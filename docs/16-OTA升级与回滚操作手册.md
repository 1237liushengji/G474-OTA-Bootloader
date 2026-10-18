# OTA 升级与回滚操作手册

> 适用工程：STM32G474VET6 双板 CAN OTA（Bootloader + APP 双槽）
> 最后实测：2026-10-18，板A(SN=8) → 板B(SN=49FF6D…) 全链路通过
> 相关文档：`01-ISO-TP传输层设计.md`、`02-UDS诊断栈设计.md`、`03-诊断演示与验收手册.md`

---

## 1. 系统概览

### 1.1 Flash 分区

| 地址 | 长度 | 用途 |
|---|---|---|
| `0x08000000` | 2 KB | BL 向量表（页首 ~0x1D8）+ 控制区主份（副本 @+0x1D8、@+0x400） |
| `0x08000800` | 46 KB | Bootloader 代码 |
| `0x0800C000` | 208 KB | **Slot A**（含槽首 0x80 保留区：32B 镜像头 + 填充） |
| `0x08040000` | 2 KB | 控制区镜像份 |
| `0x08040800` | 208 KB | **Slot B**（同上） |

关键常量：`OTA_SLOT_SIZE=0x34000`、`OTA_IMG_HDR_SIZE=0x80`、`OTA_IMG_HDR_STRUCT_SIZE=32`、
`OTA_IMG_MAX_PAYLOAD = 0x34000-0x80 = 0x33F80`。

### 1.2 镜像头契约（32 字节，小端）

| 偏移 | 字段 | 要求 |
|---|---|---|
| 0 | `magic` | `0x47344F54`（"G4OT"） |
| 4 | `imgSize` | **`payloadSize + 0x80`**（不是 +32！） |
| 8 | `payloadSize` | 纯 payload 字节数 |
| 12 | `version` | 4 字节，协议允许全 0 |
| 16 | `loadAddr` | 目标槽 APP 基址 = 槽基址 + 0x80 |
| 20 | `payloadCrc` | payload 的 CRC-32/ISO-HDLC |
| 24 | `reserved` | 0 |
| 28 | `hdrCrc` | CRC32(头[0,28)) |

BL 的 `BL_ImageReadHeader()` 还会独立校验：
- `magic` 正确
- `imgSize == payloadSize + 0x80`
- `loadAddr` 与目标槽一致
- 初始 SP：8 字节对齐、落在 `0x20000000 < SP ≤ 0x20020000`
- **复位向量：bit0=1 且必须落在 `[appBase, appBase+payloadSize)` 内**

### 1.3 启动决策（BL）

```
上电
 └─ 时钟/CAN/参数初始化
     └─ 启动窗口 1s：若收到"表达刷写意图"的诊断请求 → recovery（等待 OTA）
         ※ TesterPresent(0x3E) 不计入，它只是保活帧
     └─ 校验活动槽 → 成功则跳转；失败则试另一槽；都失败 → recovery
     └─ TRIAL 状态：先递减 trialLeft 并落盘，为 0 时回滚到另一槽
```

---

## 2. 首次烧录（产线 / 换板）

**顺序不能颠倒**，且必须先擦除整片再写 BL —— BL 的控制区（`0x08000000+0x1D8`）
只能编程不能擦除（同页含向量表），若该区域残留旧代码，控制区永远写不进去。

```bash
P="D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"
SN=<ST-Link 序列号>

# 1) 整片擦除
"$P" -c port=SWD sn=$SN mode=UR -e all

# 2) 写 Bootloader
"$P" -c port=SWD sn=$SN mode=UR -w firmware/BL.hex -v

# 3) 写 APP（带 32B 头的工厂镜像，自带地址 0x0800C000）
"$P" -c port=SWD sn=$SN mode=UR -w <APP_factory.hex> -v -rst
```

> ⚠️ 绝不能直接烧裸 APP hex。`0x0800C000` 无头 → BL 判无效 → 停在 recovery，
> 表现为"上电没反应"。

生成工厂镜像：

```bash
python ota/scripts/make_factory_image.py --hex <raw_app.hex> --slot A --version 8.2
# 输出 <raw_app>_factory.hex / .bin，自校验通过后才可烧录
```

---

## 3. 在线升级（板A → 板B）

### 3.1 前提

- 板A 是 **UDS 客户端**（`CAN_NODE_ROLE=CAN_NODE_A`、`OTA_CLIENT_ENABLE=1`）
- 板B 是 **ECU**（`CAN_NODE_ROLE=CAN_NODE_B`、`OTA_CLIENT_ENABLE=0`）
- 板A 内嵌的 payload **必须与目标槽链接地址一致**（见 §4）
- 两板 CAN 已连通（500 kbps，PB12/PB13，终端电阻 120Ω）

### 3.2 流程与判据

板A 上电 5s 后自动开始（留出对端 BL 的 1s 启动窗口），日志应为：

```
[ota] === start OTA to peer, payload=73824 crc=0x........ ===
[ota] tester present OK
[ota] session=0x03 OK
[ota] peer active slot=0 -> target slot=1 addr=0x08040880
[ota] pending(0x78) state=4 len=3
[ota] download OK, payload=73824
[ota] all 73824 bytes sent
[ota] transfer exit OK, crc=0x........
[ota] FF01 verify OK
[ota] FF02 activate OK, requesting peer reset
[ota] peer reset sent, flow DONE
```

对端复位后 BL 应打印：

```
BL: boot state=3 trialLeft=2 activeSlot=1 lastErr=0x00000000
BL: jump to APP slot 1 base=0x08040880
```

### 3.3 验收判据（三条都要满足）

1. 板A 日志走到 `flow DONE`
2. 板B 的 BL 打印 `jump to APP slot 1` 且 `lastErr=0x00000000`
3. SWD 读 `0xE000ED08`（VTOR）应为 **`0x08040880`**，且 PC 落在该槽范围内

---

## 4. 换槽升级（A ⇄ B 反复刷）

APP 不是位置无关代码：Slot A 链接在 `0x0800C080`、Slot B 链接在 `0x08040880`，
相差 `0x34800`。**一份二进制不可能在两个槽都运行**（复位向量会落在槽外被 BL 拒绝）。

也无法把两份 payload 同时内嵌：`2×73.9KB + APP 本体 ≈ 238KB > 208KB`（实测超 25.5KB）。

因此：**用构建脚本按目标槽生成 payload**。

```bash
# 目标 = Slot B（板B 当前跑在 Slot A 时用这个）
python tools/make_ota_payload.py --target-slot B --version 8.3

# 目标 = Slot A（板B 当前跑在 Slot B 时用这个）
python tools/make_ota_payload.py --target-slot A --version 8.3
```

脚本自动完成：切 uvprojx 链接地址 → 编译 → 生成带 32B 头的镜像 →
转成内嵌数组 → **还原工程** → 重建板A。之后按 §2 把新的板A 镜像烧进板A 即可。

> 脚本会在 `finally` 中还原 `G474.uvprojx`，异常中断也不会把工程留在错误构型。
> 产物：`tmp/OTA_payload_slot{A,B}_factory.bin`。

---

## 5. 试运行 / 提交 / 回滚

| 阶段 | 触发 | 行为 |
|---|---|---|
| 激活 | `31 01 FF02` | 合成槽首镜像头并回读校验 → 写控制区 `state=TRIAL_x`、`trialLeft=3` |
| 试运行 | 复位 | BL 递减 `trialLeft` 并落盘，跳转新槽 |
| 提交 | APP 自检 | `Ota_Task()` 在 `OTA_SELFCHECK_MS=3000` 后若 CAN 收发正常 → `Ota_ParamCommitTrial()`，`state→IDLE_x`、`trialLeft=0` |
| 回滚 | `trialLeft` 耗尽 | BL 校验另一槽有效 → 切回并置 `state=IDLE_other`、`lastErr=OTA_ERR_TRANSFER(0x0A)` |

**实测**（`tools/rollback_test.sh`，在 3s 自检窗口内抢复位）：

```
BL: boot state=3 trialLeft=2 activeSlot=1     ← 第1次运行
BL: jump to APP slot 1 base=0x08040880
BL: boot state=3 trialLeft=1 activeSlot=1     ← 第2次运行
BL: trial exhausted -> rollback to slot 0     ← 回滚
BL: jump to APP slot 0 base=0x0800C080        ← 回到旧槽，系统可用
```

> 注：`trialLeft=3` 只提供 **2 次**运行机会。递减发生在本次跳转判定之前，
> 这是已知且已注释的设计，若要跑满 3 次请把 `OTA_TRIAL_BOOT_LIMIT` 提到 4。

**失效路径（镜像本身不合法）**：BL 在跳转前就判无效，直接试另一槽，
不会进入 TRIAL。实测 `err=4 (SIZE_RANGE)` / `err=9 (RESET_VEC)` 均能正确回退。

---

## 6. 故障码对照表

### 6.1 UDS 负响应（`7F <SID> <NRC>`）

| NRC | 含义 | 本项目典型原因 |
|---|---|---|
| `0x13` | 报文长度错 | 0x34 请求不是 11 字节 |
| `0x22` | 条件不满足 | 目标是当前活动槽；或未进入扩展会话 |
| `0x31` | 请求超出范围 | 0x34 的 addr 不在任何槽内 / size 越界 |
| `0x70` | 下载不被接受 | 擦除失败 |
| `0x78` | 响应挂起 | **正常**，设备在擦除/校验，客户端应继续等待 |

### 6.2 FF02 激活失败细分码（`71 01 FF 02 <code>`）

| code | 含义 |
|---|---|
| `0x00` | 成功 |
| `0xA0` | 预检失败（下载未完成 / 收到字节数 ≠ 声明值） |
| `0xA1` | 镜像头写入失败（Flash 编程报错） |
| `0xA2` | 回读 magic 不符 |
| `0xA3` | 回读 payloadSize 不符 |
| `0xA4` | 回读 loadAddr 不符 |
| `0xA5` | 回读 hdrCrc 与实算不符 |
| `0xA6` | 控制区页写失败（TRIAL 标志落盘失败） |

### 6.3 BL 判定 `lastErr`（`OTA_ERR_*`）

| 值 | 含义 |
|---|---|
| `0x01` | 镜像头 magic 错 |
| `0x02` | 头自校验失败 |
| `0x03` | 净固件 CRC 不符 |
| `0x04` | 长度越界（`imgSize` 或 `payloadSize` 不合规） |
| `0x05` | loadAddr 与目标槽不符 |
| `0x06` | 擦除失败 |
| `0x07` | 编程失败 |
| `0x08` | 栈顶指针非法 |
| `0x09` | **复位向量非法/不在槽内**（payload 链接地址与目标槽不一致） |
| `0x0A` | 试运行未确认（回滚后的记录值） |

---

## 7. 排障清单

| 现象 | 根因 | 处置 |
|---|---|---|
| 对端一直停在 BL，不跳 APP | 曾有诊断请求落进 1s 启动窗口；旧版 BL 把 `0x3E` 也算作刷写意图 | 已修复（`BL_RxDispatch` 过滤 0x3E）。确认 BL 是最新版本 |
| 重刷 Slot A 后板子不启动 | 同上，被对端 `0x3E` 锁进 recovery | 同上。若仍卡：整片擦除 + 重刷 BL |
| `0x34` 永远 `7F 34 31` | 请求字节错位（CF 帧 PCI 丢失/游标不递增） | 已修复。核对对端串口收到的原始字节 |
| 0x34 内容对但被判 NRC | agent 侧额外的取证帧 `7F 34 E0` 被当负响应 | 已修复（改为只打本端串口） |
| 传输几块后卡住、块重发很多 | ① 重复帧被误判 SN 错误 ② 周期诊断打印阻塞主循环 | 均已修复。确认无 `BSP_UART_LCD_MIRROR=1`、无高频调试打印 |
| FF02 时对端重启 | 控制区擦写在正在执行的那个 Bank | 已修复（按 `s_runningSlot` 反选 Bank） |
| OTA 完成但新固件不生效 | `imgSize` 少 0x60 / payload 链接槽不匹配 | 分别对应 `err=4` / `err=9`，见表 6.3 |
| 板A 收不到任何应答 | Tx FIFO 被未送达帧占满（error-passive 未到 bus-off） | 已加 500ms 停滞看门狗。检查 CAN 接线/终端电阻 |
| 串口完全无输出 | 板载 USB 转串口 **TXD → PA10 物理开路**（本批板硬件问题） | 用 SWD 读 VTOR/PC 判定；或用 `USB设备` 口（需 USB 固件） |

---

## 8. 本次验收记录（2026-10-18）

| 项 | 数据 |
|---|---|
| payload 大小 | 73824 字节（链路层 74 个 0x36 块） |
| 传输 CRC | `0x9F25DF4C`（与 FF01 回读一致） |
| 块重发次数 | **0**（清理调试脚手架前为 17~25 次） |
| FF02 结果 | `71 01 FF 02 00` 成功 |
| BL 跳转 | `jump to APP slot 1 base=0x08040880`，`lastErr=0x00000000` |
| 运行态 | 板B `VTOR=0x08040880`、PC 落在 Slot B 内 |
| 回滚 | `trial exhausted -> rollback to slot 0`，自动回到 Slot A 并正常运行 |

---

## 9. 已知限制

1. **无法双槽同时内嵌 payload**（容量所限），换槽需重跑 `make_ota_payload.py`。
2. **`g_otaImage` 的角色固定为对端（B）**；板A 自身不能被 OTA（它没有对端客户端）。
3. **本批板串口 RX 开路**，现场只能通过 CAN 触发 OTA 或 SWD 烧录。
4. **`.noinit` 区未在 scatter 中声明**，`g_bootCnt`/`g_rstIwdgCnt` 等跨复位累加器无效。
5. 上位机 `ota_host.py` 未在真机验证（一直是板对板 OTA）。
