# -*- coding: utf-8 -*-
"""一次性修复：把 11 号文档里被"全局地址替换"误改的**历史观测值**还原。

背景：文档同步脚本把 0x0800C020/0x08040820 全量替换成了新值，
但 11 号文档是**事故复盘**，其中"修复前"的地址是历史证据，必须保持原样；
只有"修法/新契约"部分才该用新地址。本脚本按行语义精确还原。
"""
import io
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

P = r'ota\docs\11-APP起不来根因-VTOR非128字节对齐.md'

REPS = [
    # ---- §1 日志：这是修复前那份日志，必须是旧地址 ----
    ('BL: jump to APP slot 0 base=0x0800C080 size=0xCE58',
     'BL: jump to APP slot 0 base=0x0800C020 size=0xCE58'),
    # ---- §2 定案证据：观测到的是旧值 ----
    ('VTOR = 0x0800C000      ← 代码里写的是 0x0800C080；低 7 位被硬件吞掉',
     'VTOR = 0x0800C000      ← 代码里写的是 0x0800C020；低 7 位被硬件吞掉'),
    ('**写进去的 0x0800C080 没有存住。**',
     '**写进去的 0x0800C020 没有存住。**'),
    ('| Slot A | `0x0800C000` | `0x0800C080` | ❌（只 32 字节对齐） |',
     '| Slot A | `0x0800C000` | `0x0800C020` | ❌（只 32 字节对齐） |'),
    ('| Slot B | `0x08040800` | `0x08040880` | ❌ |',
     '| Slot B | `0x08040800` | `0x08040820` | ❌ |'),
    # ---- §3 失效链条：当时发生的事 ----
    ('① BL 校验通过 -> BL_JumpToApp(0x0800C080)',
     '① BL 校验通过 -> BL_JumpToApp(0x0800C020)'),
    ('   └─ SCB->VTOR = 0x0800C080      （bl_jump.c 第 58 行）',
     '   └─ SCB->VTOR = 0x0800C020      （bl_jump.c 第 58 行）'),
    ('system_stm32g4xx.c，VECT_TAB_OFFSET=0xC080），',
     'system_stm32g4xx.c，VECT_TAB_OFFSET=0xC020），'),
    # ---- §4 修法表：左列"现值"应为旧值 ----
    ('| APP `G474.uvprojx` | `IROM1/OCR_RVCT4 = 0x800C080`、`VECT_TAB_OFFSET=0xC080` | `0x800C080` / `0xC080` |',
     '| APP `G474.uvprojx` | `IROM1/OCR_RVCT4 = 0x800C020`、`VECT_TAB_OFFSET=0xC020` | `0x800C080` / `0xC080` |'),
    ('| `ota/tools/ota_host.py` | `--load-addr 0x0800C080` 等 | `+0x80` |',
     '| `ota/tools/ota_host.py` | `--load-addr 0x0800C020` 等 | `+0x80` |'),
    # ---- 顶部状态：标注已修复 ----
    ('> 状态：**根因已实测确认**。这不是"偶发"，而是**设计层面的必然**：',
     '> 状态：**根因已实测确认，并已按选项 A 修复**（槽首保留区 32 → `0x80`，\n'
     '> APP 基址变为 `0x0800C080` / `0x08040880`；修复记录见 §4.1）。\n'
     '> 这不是"偶发"，而是**设计层面的必然**：'),
]

APPLIED_NOTE = """
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
"""


def main():
    t = io.open(P, encoding='utf-8').read()
    miss = 0
    for a, b in REPS:
        if a not in t:
            print('[!] 未找到: %s' % a[:52].replace('\n', '|'))
            miss += 1
            continue
        t = t.replace(a, b, 1)
    if '## 4.1 修复记录' not in t:
        t = t.rstrip() + '\n' + APPLIED_NOTE
    io.open(P, 'w', encoding='utf-8', newline='').write(t)
    print('还原/补充完成；未命中 %d 处' % miss)
    return 0 if miss == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
