#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bootloader 链接布局的**单一真相源**（scatter 文本 + 不变量检查）。

为什么需要它（这不是洁癖，是一次真实变砖事故的固化）：

  页 0（0x08000000~0x080007FF）在契约里是"向量表 + 控制区 4 个候选副本"的保留页：
      0x000  向量表          [0, 0x1D8)
      0x1D8  控制区副本 0
      0x400  控制区副本 1
  而 `bl_param.c` 的 `BL_ParamStore()` 写控制区时是"**整页擦除**，
  再只回写向量表与另一份副本"——它**不会**回写 BL 自己的代码。

  2026-10-05 的 `bl.sct` 把 ER_IROM1 写成 `0x08000000 0x0000B800`，
  于是链接器把代码紧接着向量表从 0x080001D8 开始铺，正好压住两个副本区。
  后果：BL 第一次写控制区就擦掉了自己的复位向量代码 ——
  实测复位向量 = 0x08000325，而 0x08000324 处 16 字节读回全为 0xFF，板上固件成砖。

  修法：**页 0 只放向量表，代码从 0x08000800 开始**。
  本模块把这条布局固定下来，供生成脚本、修复脚本与审计脚本共用，
  避免"三个脚本各写一份、改了一个忘了另一个"。
"""
import sys

# 控制台默认 GBK，本模块会打印中文/表格；显式改 UTF-8，避免 UnicodeEncodeError 叠加假故障。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---- 页 0 保留区（与 ota/common/ota_common.h 同值） ----
VECTAB_BYTES    = 0x000001D8      # 向量表占用（G474 实测，正好 0x1D8）
CFG_SLOT_OFFSET = 0x000001D8      # 控制区副本 0
CFG_MIRROR_OFF  = 0x00000400      # 控制区副本 1
CFG_SIZE        = 48              # sizeof(OTA_Cfg)
PAGE_SIZE       = 0x800

PAGE0_ADDR      = 0x08000000
BL_CODE_ADDR    = 0x08000800      # BL 代码起始（= OTA_BL_CODE_ADDR）
BL_CODE_SIZE    = 0x0000B000      # 46 KB
BL_REGION_SIZE  = 0x0000B800      # 加载域总长：页 0 + 代码区（0x08000000~0x0800B800）

# 页 0 内的关键区间： (起始偏移, 结束偏移, 名称, **是否必须保持空白**)
#   * 向量表：必须被占用（MCU 复位固定从 0x08000000 取初始 SP 与复位向量）
#   * 两个控制区副本：必须**保持空白**，否则第一次写控制区就会把它们（以及同页的
#     任何其它内容）连同 BL 代码一起擦掉 —— 这正是变砖事故的形态。
RESERVED_IN_PAGE0 = [
    (0x000, VECTAB_BYTES,                        'BL 向量表',     False),
    (CFG_SLOT_OFFSET, CFG_SLOT_OFFSET + CFG_SIZE, '控制区副本 0', True),
    (CFG_MIRROR_OFF,  CFG_MIRROR_OFF + CFG_SIZE,  '控制区副本 1', True),
]

SCATTER_TEXT = """; Bootloader scatter file —— STM32G474VET6
; 布局依据 ota/common/ota_common.h（本文件由 ota/scripts/bl_layout.py 统一生成）：
;   0x08000000  页 0（2KB）  向量表 [0,0x1D8) + 控制区副本 0 @0x1D8 / 副本 1 @0x400
;   0x08000800  46KB         Bootloader 代码（OTA_BL_CODE_ADDR ~ +OTA_BL_CODE_SIZE）
;
; ⚠️ 血的教训（2026-10-05，板上固件因此变砖）：
;   ER_IROM1 曾写成 `0x08000000 0x0000B800`，链接器就把代码**紧接着向量表**
;   从 0x080001D8 开始铺，正好压住控制区两个副本区。而 bl_param.c 的
;   BL_ParamStore() 写控制区是"整页擦除 + 只回写向量表与另一份副本"，
;   **不回写代码** —— 于是第一次写控制区就把 BL 自己的复位向量代码擦成 0xFF：
;   实测复位向量 = 0x08000325，而 0x08000324 处读回 16 字节全为 0xFF → 上电 HardFault。
;   ⇒ 页 0 只允许放向量表，代码区必须从 0x08000800 开始。改这里前先读上面这段。
;
; ⚠️ 必须与 BL.uvprojx 里 IROM/OCR_RVCT4 的 `0x08000000 + 0x0000B800` 一致：
;   不一致时 Keil 会按 Target 对话框重新生成分散加载文件，把本文件覆盖掉。
;   另一个开关同样致命：`<umfTarg>` 必须为 0，否则 µVision 会**无视本文件**，
;   改用 Target 对话框的 IROM/IRAM 重新生成（就是变砖那版的布局）。
;
; 为什么必须写成**两个加载域**（而不是一个 LR 里放两个 ER）：
;   同一个加载域内，链接器把各执行域的**加载地址**紧挨着排；
;   若 ER_IROM2 的执行地址是 0x08000800 而加载地址被排到 0x080001D8，
;   armlink 直接报 L6788E（"加载内容会在运行时被自身覆盖"）。
;   拆成两个加载域后，ER_IROM2 成为**根区**（root region），
;   加载地址 = 执行地址 = 0x08000800，L6788E/L6202E/L6203E 一并消除。
LR_IROM1 0x08000000 0x00000800  {              ; 加载域 1 = 页 0（2KB，只放向量表）
  ER_IROM1 0x08000000 0x000001D8  {            ; 向量表 472B，正好放满
    *.o (RESET, +First)
  }
}
LR_IROM2 0x08000800 0x0000B000  {              ; 加载域 2 = BL 代码区（页 1 起，46KB）
  ER_IROM2 0x08000800 0x0000B000  {            ; 根区：加载地址=执行地址，避开控制区
    *(InRoot$$Sections)
    .ANY (+RO)
    .ANY (+XO)
  }
  RW_IRAM1 0x20000000 0x00020000  {            ; 128KB RAM（须嵌在加载域内，否则会被当成加载域）
    .ANY (+RW +ZI)
  }
}
"""
# ⚠️ 这里**刻意不加 `UNINIT`**（曾经有，是第二个变砖级缺陷的根源）：
#   `UNINIT` 让启动代码**不清零 .bss**，于是所有"没写初值的静态变量"上电后是**随机值**。
#   BL 的 `bl_uart.c` 只设了 UART_InitTypeDef 的部分字段（没设 `ClockPrescaler`），
#   HAL 用它去查 `UARTPrescTable[ClockPrescaler]` —— 越界索引直接把总线访问打到
#   0x20FB0EA4（非法地址）→ 精确总线错误 → HardFault。
#   实测：PC 停在 `HardFault_Handler`（0x08000936），CFSR.BFSR=0x82、BFAR=0x20FB0EA4。
#   代价对比：清零 8KB .bss 约几十微秒，而 UNINIT 换来的是"随机上电行为"。
#   APP 侧没这个问题，因为它的 .bss 正常清零（ClockPrescaler 恰好为 0 = DIV1）。


def check_scatter_text(text):
    """静态检查 scatter 文本是否守住了"页 0 只放向量表"。返回问题列表（空=OK）。"""
    problems = []
    import re

    # 1) 必须存在把代码放到 0x08000800 的执行域
    if not re.search(r'ER_IROM\w*\s+0x0*8000800\s', text, re.I):
        problems.append('缺少"代码区从 0x08000800 开始"的执行域（ER_IROM2）')

    # 2) 不允许存在"从 0x08000000 开始且长度 > 向量表"的执行域（那会把代码铺进控制区）
    for m in re.finditer(r'ER_\w+\s+0x0*8000000\s+0x0*([0-9A-Fa-f]+)\s*\{', text, re.I):
        size = int(m.group(1), 16)
        if size > VECTAB_BYTES:
            problems.append('执行域 %s 从 0x08000000 起、长度 0x%X > 向量表 0x%X —— '
                            '代码会铺进控制区副本区' % (m.group(0).split()[0], size, VECTAB_BYTES))

    # 3) 必须有覆盖页 0 的加载域，且代码区加载域从 0x08000800 起
    if not re.search(r'LR_\w+\s+0x0*8000000\s+0x0*%X\s*\{' % PAGE_SIZE, text, re.I):
        problems.append('缺少"加载域 = 0x08000000 / 0x%X（页 0）"' % PAGE_SIZE)
    if not re.search(r'LR_\w+\s+0x0*8000800\s+0x0*%X\s*\{' % BL_CODE_SIZE, text, re.I):
        problems.append('缺少"加载域 = 0x08000800 / 0x%X（代码区）"' % BL_CODE_SIZE)

    return problems


def write_scatter(path):
    """把规范 scatter 写入 path（幂等）。返回 True=内容有变化。"""
    old = None
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            old = f.read()
    except FileNotFoundError:
        pass
    if old == SCATTER_TEXT:
        return False
    with open(path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(SCATTER_TEXT)
    return True


if __name__ == '__main__':
    import os
    import sys

    from paths import workspace_root

    root = workspace_root()
    sct = os.path.join(root, 'ota', 'bootloader', 'MDK-ARM', 'bl.sct')

    print('页 0 保留区:')
    for lo, hi, name, must_blank in RESERVED_IN_PAGE0:
        print('  0x%08X ~ 0x%08X  %-14s %s'
              % (PAGE0_ADDR + lo, PAGE0_ADDR + hi, name,
                 '必须保持空白' if must_blank else '（向量表，必须被占用）'))
    print('BL 代码区: 0x%08X + 0x%X' % (BL_CODE_ADDR, BL_CODE_SIZE))
    print()

    problems = check_scatter_text(SCATTER_TEXT)
    print('规范文本自检:', problems or 'OK')
    if problems:
        sys.exit(2)

    if '--write' in sys.argv:
        changed = write_scatter(sct)
        print('%s %s' % ('已更新' if changed else '无需改动', os.path.relpath(sct, root)))
        cur = open(sct, encoding='utf-8', errors='replace').read()
        bad = check_scatter_text(cur)
        print('落盘后复查:', bad or 'OK')
        sys.exit(1 if bad else 0)

    if os.path.isfile(sct):
        cur = open(sct, encoding='utf-8', errors='replace').read()
        bad = check_scatter_text(cur)
        print('现网 %s:' % os.path.relpath(sct, root), bad or 'OK')
        sys.exit(1 if bad else 0)
    sys.exit(0)
