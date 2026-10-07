#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 BL main.c 里带 printf 的诊断块换成纯 LED 版本（避免拉进完整 printf）。"""
import os
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

OLD = '''/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤） */
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{
  BL_UartPrintf("BL-DIAG stage %lu\\r\\n", (unsigned long)stage);
  for (uint32_t i = 0U; i < stage; i++)
  {
    BL_DiagLedOn();
    BL_DiagDelay(120U);
    BL_DiagLedOff();
    BL_DiagDelay(180U);
  }
  BL_DiagDelay(offMs);
}

static void BL_DiagMark(uint32_t stage)
{
  BL_DiagStage(stage, 200U);
}
'''

NEW = '''/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤）。
 * 刻意不调用 BL_UartPrintf：它会拉进完整 vsnprintf（含浮点 bigflt0/btod、
 * locale lc_ctype_c），在 BL 的 46KB 预算里代价过高。LED 闪次足以定步骤。 */
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{
  for (uint32_t i = 0U; i < stage; i++)
  {
    BL_DiagLedOn();
    BL_DiagDelay(120U);
    BL_DiagLedOff();
    BL_DiagDelay(180U);
  }
  BL_DiagDelay(offMs);
}
'''


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    found = False
    for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
        o, n = conv(OLD), conv(NEW)
        if t.count(o) == 1:
            t = t.replace(o, n, 1)
            found = True
            break
    if not found:
        print('!! 未找到带 printf 的诊断块（可能已修正）')
        if 'BL-DIAG stage' in t:
            print('   仍残留 BL-DIAG 打印，需手工检查')
            return 2
        return 0
    print('  已替换为纯 LED 版本（去掉 vsnprintf 依赖）')
    if apply_:
        open(MAIN, 'w', encoding='utf-8', newline='').write(t)
        print('  已写入')
    else:
        print('  干跑（加 --apply 生效）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
