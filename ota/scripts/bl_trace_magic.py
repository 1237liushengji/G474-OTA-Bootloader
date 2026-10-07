#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 BL 里打印"它实际读到的槽基址与魔数"——用于打破以下矛盾：

  dump_flash.py 读回芯片显示 0x0800C000 处有 `G4OT`，
  但 BL 运行时 BL_ImageReadHeader() 返回 HDR_MAGIC（找不到魔数）。

可能性：
  * 读到的是 0xFFFFFFFF（该区域被擦）
  * 读到别的值（地址算错 / 读到别处）
  * 读到 0x47344F54 却仍判失败（比较逻辑问题）

把实际值打出来即可一次分辨。

另外打印 SRAM 起始字与选项字节相关的 RCC/FLASH 状态，
便于与"冷启动不运行"的现象关联。

用法: python ota/scripts/bl_trace_magic.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

ANCHOR = '    BL_UART_SEND_STR(attempt == 0U ? "TRACE [8a] try slot A(active)\\r\\n"\n                                   : "TRACE [8b] try other slot\\r\\n");'

NEW = '''    /* [TRACE] 打印 BL 实际读到的槽基址与魔数 —— 用于判定"读不到"还是"读到别的" */
    {
      uint32_t base = (slot == 0U) ? OTA_SLOT_A_ADDR : OTA_SLOT_B_ADDR;
      uint32_t m0   = *(volatile uint32_t *)(base);
      uint32_t m1   = *(volatile uint32_t *)(base + 0x100U);
      BL_UartPrintf("TRACE magic: slot=%u base=0x%08lX m[0]=0x%08lX m[+0x100]=0x%08lX\\r\\n",
                    (unsigned)slot, (unsigned long)base,
                    (unsigned long)m0, (unsigned long)m1);
      /* 顺便打印净荷首两个字（SP / 复位向量），确认能否读到 APP 区 */
      BL_UartPrintf("TRACE payload: sp=0x%08lX rv=0x%08lX\\r\\n",
                    (unsigned long)(*(volatile uint32_t *)(base + 32U)),
                    (unsigned long)(*(volatile uint32_t *)(base + 36U)));
    }

''' + ANCHOR


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()

    if 'TRACE magic:' in t:
        print('  魔数打点已存在，跳过')
        return 0

    done = False
    for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
        a, n = conv(ANCHOR), conv(NEW)
        if t.count(a) == 1:
            t = t.replace(a, n, 1)
            done = True
            break
    if not done:
        print('  !! 未找到 [8a] 锚点')
        return 2

    print('  已插入魔数/净荷打点')
    if not apply_:
        print('  干跑（加 --apply 生效）')
        return 0
    open(MAIN, 'w', encoding='utf-8', newline='').write(t)
    print('  已写入')
    return 0


if __name__ == '__main__':
    sys.exit(main())
