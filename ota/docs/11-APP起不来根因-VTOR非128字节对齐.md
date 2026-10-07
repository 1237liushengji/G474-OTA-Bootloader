# 11 · APP 起不来的根因：向量表偏移不是 0x80 字节对齐（VTOR 低 7 位被吞）

> 状态：**根因已实测确认，并已按选项 A 修复**（槽首保留区 32 → `0x80`，
> APP 基址变为 `0x0800C080` / `0x08040880`；修复记录见 §4.1）。
> 这不是"偶发"，而是**设计层面的必然**：
> 只要 APP 基址 = 槽基址 + `0x20`，APP 在开中断的那一刻就必然 lockup。
> 本文只讲这一个根因与修法选项；前面两个（镜像压入控制区页 / UNINIT）见 `docs/10`。

---

## 1. 现象

BL 日志显示它**每轮都成功校验并跳转**，但 APP 一行输出都没有，3 秒后被看门狗拉回重来：

```
BL: clock HSE+PLL 150MHz OK
BL: cfg invalid, use default IDLE_A
BL: boot state=0 trialLeft=0 activeSlot=0 lastErr=0x00000000
BL: after trial state=0 trialLeft=0 activeSlot=0 lastErr=0x00000000
BL: jump to APP slot 0 base=0x0800C020 size=0xCE58     ← 跳转成功（板A，A 角色镜像）
（3 秒后重复）
```

板 B 同理（`size=0xD9D0`，B 角色镜像），只是它控制区里还留着旧的 `lastErr=0x1`。
**两个角色镜像的 `size` 与本地产物逐字节一致 → 烧录、镜像、BL 全都没问题。**

肉眼现象：**LED 长亮约 3 秒 → 闪一阵 → 循环**（两块板一致）。

---

## 2. 定案证据（调试器直读内核寄存器）

```
VTOR = 0x0800C000      ← 代码里写的是 0x0800C020；低 7 位被硬件吞掉
CFSR = 0x00020100      ← UFSR=0x0002 INVSTATE（跳到 bit0=0 的地址）
                          BFSR=0x01   IBUSERR （取指总线错误）
HFSR = 0x40000000      ← FORCED：可配置错误升级为 HardFault
```

`VTOR` 读回值是最直接的一条：**写进去的 0x0800C020 没有存住。**

Cortex-M4（ARMv7-M）的 `SCB->VTOR`：位 `[31:7]` 是向量表基址，位 `[6:0]` 是 **RES0**
（保留、读作 0，写入被忽略）。也就是说 **向量表必须 0x80 字节对齐**。

而本方案的 APP 基址是"槽基址 + 32 字节镜像头"：

| 槽 | 槽基址 | APP 基址（=+0x20） | 0x80 字节对齐？ |
|---|---|---|---|
| Slot A | `0x0800C000` | `0x0800C020` | ❌（只 32 字节对齐） |
| Slot B | `0x08040800` | `0x08040820` | ❌ |

`0x20` 永远不可能 0x80 字节对齐 —— **所以这不是"这块板的问题"，是契约本身的问题。**

---

## 3. 失效链条

```
① BL 校验通过 -> BL_JumpToApp(0x0800C020)
   └─ SCB->VTOR = 0x0800C020      （bl_jump.c 第 58 行）
② 硬件把 VTOR 存成 0x0800C000      （RES0 位被忽略）
③ APP 的 SystemInit 又写一次同样的值（system_stm32g4xx.c，VECT_TAB_OFFSET=0xC020），
   结果一样：VTOR 仍是 0x0800C000
④ APP 的 HAL_Init() 打开 SysTick 中断
⑤ 1ms 后第一次 SysTick 异常 -> 按 VTOR 取向量 -> 落到 0x0800C000 的【32 字节镜像头】上
   └─ 镜像头第 15 个字（SysTick 向量位置）里是 magic/长度/CRC 等数据，不是合法入口
⑥ 取到 bit0=0 的地址 -> INVSTATE + IBUSERR -> fault 升级 -> **lockup**
⑦ 内核锁死 -> APP 再也无法喂狗 -> 3 秒后 IWDG 复位 -> 回到 ①
```

### 为什么肉眼看是"长亮 3 秒后闪烁"

* APP 早期打点里先把 PE0 配成输出但**没设 ODR** → ODR 复位值为 0 →
  **低电平点亮 LED = 常亮**；随后立刻 lockup，所以"长亮"（其实一直亮到看门狗复位）。
* 复位后 BL 重新跑它的分步自检（走到第 N 步闪 N 下）→ 看起来就是"闪一阵"。
* 两块板跑的是同一套确定性流程 → 看起来"同步"（**与 CAN 无关**）。

---

## 4. 修法（三个选项，按推荐度排序）

### 选项 A（推荐）：把镜像头保留区从 32B 扩到 128B，APP 基址 = 槽基址 + `0x80`

* `0x80` 是 0x80 字节对齐 ✓，且保留区仍是"一整块"，语义干净。
* 镜像头结构体本身**不用改**（仍是 32 字节），只是保留区扩到 0x80，中间填 `0xFF`。
* 需要同步改的地方（跨库契约，漏一处就会失败）：

  | 位置 | 现值 | 改为 |
  |---|---|---|
  | `ota/common/ota_common.h` | APP 偏移 `0x20` | `0x80` |
  | APP `G474.uvprojx` | `IROM1/OCR_RVCT4 = 0x800C020`、`VECT_TAB_OFFSET=0xC020` | `0x800C080` / `0xC080` |
  | BL `bl_image.c` 的 `appBase` | 槽基址 + 契约偏移 | 同左（跟随宏） |
  | `ota/scripts/make_factory_image.py` | `loadAddr = 槽基址 + 0x20` | `+ 0x80` |
  | `ota/tools/ota_host.py` | `--load-addr 0x0800C020` 等 | `+0x80` |
  | 文档/守卫里所有 `0xC080 / 0x40880 / 0x20` | —— | 统一替换 |

### 选项 B（改动最小）：APP 把向量表搬到 RAM 后再设 VTOR

在 APP 的 `SystemInit()` 里（**必须早于 `HAL_Init()`**）：把 472 字节向量表从
`0x0800C080` 复制到一块 0x80 字节对齐的 RAM（如 `0x20000000`），然后
`SCB->VTOR = (uint32_t)ramTable;`。

* 只动 APP 一个文件，**契约与烧录流程全都不用改**。
* 代价：472 字节 RAM（本机 128KB，占用可忽略）+ 一段启动拷贝。
* 缺点：属于"绕过"，契约里那个非对齐基址仍然是错的，以后换人接手容易再踩。

### 选项 C（不推荐）：给 APP 工程加 scatter，把 RESET 段单独放到 `槽基址 + 0x80`

* 执行域从 `0x0800C080` 起、但向量表在 `0x0800C080` —— 浪费 0x60 字节，且工程要多一个散列文件。

---

## 5. 复现与验证方法（改完怎么确认修好了）

1. `python ota/scripts/build_all_firmware.py`（重编 BL + 两个角色 APP）
2. `python ota/scripts/make_factory_image.py --hex firmware/G474_A.hex --slot A --version 3.0`（B 同理）
3. `python ota/scripts/flash.py bl --yes` + `--file firmware/G474_A_factory.hex --yes`
4. **断电上电**（调试器连着会把内核停在复位向量）
5. 串口应出现**APP 横幅**（`=== STM32G474 CAN Node v3.0.0 | role:... ===`）与
   `main: led-bsrr-state M3` —— 这两行一出现，就说明"BL → APP"这一段彻底通了。
6. 用调试器读 `VTOR` 应等于新的 APP 基址（`0x0800C080`），且 `CFSR/HFSR` 保持 0。

---

## 6. 这条根因连带解释的旧问题

| 旧记录 | 现在的解释 |
|---|---|
| `docs/09` §1「APP 能否被跳转启动 ❓ 未确认」 | **已定性：BL 跳转正常，APP 因 VTOR 非对齐必然 lockup** |
| `docs/06` §1.1 判据 1.2「烧 APP 后应出现横幅」 | 一直没出现，原因就在这里 |
| "上电没反应" | 除 `docs/10` 的两个根因外，这一条是"跳过去之后没反应"的那一半 |
| 两块板行为"同步" | 同一套确定性 lockup + 看门狗复位周期，与 CAN 无关 |

---

## 4.1 修复记录（2026-10-05 已落地）

按选项 A 实施，改动与复核如下：

| 位置 | 改动 |
|---|---|
| `ota/common/ota_common.h` | `OTA_IMG_HDR_SIZE 32 → 0x80`；新增 `OTA_IMG_HDR_STRUCT_SIZE 32`（头字段仍占前 32B）；新增编译期断言 **`OtaAppBaseAlignAssert`**：两个槽的 APP 基址都必须 0x80 对齐（这条断言本该在事故前就存在） |
| `G474/MDK-ARM/G474.uvprojx` | `IROM` / `OCR_RVCT4` = `0x800C080` / `0x33F80`；`VECT_TAB_OFFSET=0xC080` |
| `G474/MDK-ARM/G474/G474.sct` | `LR/ER_IROM1 = 0x0800C080`，大小 `0x33F80` |
| BL `bl_image.c` / APP `ota_agent.c` | 均按 `OTA_IMG_HDR_SIZE` 推导 APP 基址，改宏即自动跟随 |
| 工具 | `make_factory_image.py` / `dump_flash.py` / `ota_host.py` / `fix_app_irom.py` / `check_ota_intact.py` / `check_doc_samples.py` 同步到 `0x80` / `0x0800C080` / `0x08040880` |
| 守卫 | `verify_bl.py` 新增"APP 链接基址按 0x80 字节对齐"不变量；`verify_all.py` 的 `[C7]` 由"镜像头==32"改为"保留区==0x80 且两个 APP 基址 0x80 对齐" |
| 自证 | `verify_selftest.py` 10/10 反例仍全部被抓；`test_ota_host.py` 111 条全部通过 |

复核结果（全部实测）：

```
python ota/verify/verify_all.py      -> 29 项检查, 29 通过, 0 失败
python ota/verify/verify_selftest.py -> 10 个反例, 10 个被抓到
python ota/tools/test_ota_host.py    -> Ran 111 tests, OK
python ota/scripts/verify_layout.py  -> APP 向量表 = 0x0800c080（期望 0x0800C080）OK
python ota/scripts/audit_bl_layout.py-> 镜像未压入控制区保留区
python ota/scripts/make_factory_image.py -> loadAddr = 0x0800C080，自校验通过
```

> ⚠️ 契约变更后**两块板都必须重烧**（BL + 产线镜像）。
> 判定修好的标志：串口出现 APP 横幅 `=== STM32G474 CAN Node v3.0.0 | role:... ===`
> 与 `main: led-bsrr-state M3`，并且 `VTOR` 读回 `0x0800C080`、`CFSR/HFSR` 保持 0。
