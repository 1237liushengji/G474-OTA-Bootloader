#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正 BL 分步自检：把"被完全展开"的延时循环改成紧凑写法。

问题（实测）：
    for (volatile uint32_t i = 0U; i < 24000U; i++) { __NOP(); }
  volatile 计数器 + 编译期常量上界，使 ARMCLANG 把整个循环**完全展开**，
  main.o 从基线 1066 字节膨胀到 24270 字节（+23KB），直接把 BL 顶出分区。

修法：
  1. 计数器改为非 volatile 的运行时变量（上界来自函数参数），避免展开；
  2. 给自检函数加 __attribute__((noinline))，防止被复制到多个调用点；
  3. 减少 LED 闪烁次数上限，缩短启动时间。

用法: python ota/scripts/fix_bl_diag_delay.py [--apply]
"""
import os
import re
import sys

from paths import workspace_root

ROOT = workspace_root()
MAIN = os.path.join(ROOT, 'ota', 'bootloader', 'Core', 'Src', 'main.c')

OLD_DELAY = '''/* 粗略延时：72 次空循环约 1ms @150MHz（诊断用，精度不重要） */
static void BL_DiagDelay(uint32_t ms)
{
  while (ms-- != 0U)
  {
    for (volatile uint32_t i = 0U; i < 24000U; i++)
    {
      __NOP();
    }
  }
}'''

NEW_DELAY = '''/* 粗略延时（诊断用，精度不重要）。
 * ⚠️ 不要写成 `for (volatile uint32_t i = 0; i < 24000U; i++)`：
 *    volatile 计数器 + 编译期常量上界会让 ARMCLANG **完全展开**该循环，
 *    实测使 main.o 从 1KB 膨胀到 24KB，直接把 BL 顶出分区。
 *    这里上界由参数传入（运行期值）+ noinline，编译器不会展开。 */
__attribute__((noinline))
static void BL_DiagDelay(uint32_t units)
{
  while (units-- != 0U)
  {
    uint32_t i = BL_DIAG_UNITS_PER_MS;
    while (i-- != 0U)
    {
      __NOP();
    }
  }
}'''

OLD_STAGE = '''/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤） */
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{'''

NEW_STAGE = '''/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤） */
__attribute__((noinline))
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{'''

# 在宏定义区加入单位常量
ANCHOR_MACRO = '#define BL_LED_PIN           0U'
NEW_MACRO = ANCHOR_MACRO + '\n\n/* 每毫秒的空循环次数（约 1ms @150MHz，仅用于诊断指示） */\n#define BL_DIAG_UNITS_PER_MS 8000U'


def main():
    apply_ = '--apply' in sys.argv
    t = open(MAIN, encoding='utf-8', errors='replace').read()
    orig = t
    steps = []

    for old, new, label in ((OLD_DELAY, NEW_DELAY, '延时函数改为不展开写法'),
                            (OLD_STAGE, NEW_STAGE, 'BL_DiagStage 加 noinline'),
                            (ANCHOR_MACRO, NEW_MACRO, '加入 BL_DIAG_UNITS_PER_MS')):
        done = False
        for conv in (lambda s: s, lambda s: s.replace('\n', '\r\n')):
            o, n = conv(old), conv(new)
            if t.count(o) == 1:
                t = t.replace(o, n, 1)
                steps.append(label)
                done = True
                break
        if not done:
            if old == OLD_DELAY and 'BL_DiagUnitsPerMs' in t:
                steps.append('%s（已改过）' % label)
            elif t.count(old.replace('\n', '\r\n')) > 1 or t.count(old) > 1:
                print('  !! %s: 锚点匹配多次' % label); return 2
            else:
                steps.append('%s（未找到锚点，跳过）' % label)

    # 减少总闪烁时长
    t, n1 = re.subn(r'BL_DiagDelay\(120U\)', 'BL_DiagDelay(60U)', t)
    t, n2 = re.subn(r'BL_DiagDelay\(180U\)', 'BL_DiagDelay(90U)', t)
    if n1 or n2:
        steps.append('缩短闪烁时长（%d/%d 处）' % (n1, n2))

    for s in steps:
        print('  - %s' % s)
    if t != orig:
        if apply_:
            open(MAIN, 'w', encoding='utf-8', newline='').write(t)
            print()
            print('已写入 %s' % os.path.relpath(MAIN, ROOT))
        else:
            print()
            print('干跑（加 --apply 生效）')
    else:
        print()
        print('无需改动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
