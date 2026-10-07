#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘点工作区：按类别统计文件数与体积。

用途：整理前后对比、确认没有意外残留（构建产物、缓存、临时文件）。

用法: python ota/scripts/inventory.py
"""
import collections
import os
import sys

from paths import workspace_root

ROOT = workspace_root()

ART_EXT = ('.o', '.d', '.crf', '.axf', '.map', '.htm', '.lnp', '.dep',
           '.bak', '.iex', '.build_log.htm')

CATEGORIES = [
    ('ota', 'OTA 交付物（本次会话产出）'),
]


def classify(rel):
    low = rel.lower()
    if rel.startswith('firmware/'):
        return 'B_到位固件'
    if rel.startswith('ota/verify/') or 'output.txt' in low:
        return 'A_OTA交付物/验证输出'
    if rel.startswith('ota/scripts/'):
        return 'F_构建与复核脚本'
    if rel.startswith('ota/'):
        return 'A_OTA交付物'
    if low.endswith(ART_EXT) or '_sct.bak' in low or 'build_log' in low:
        return 'D_构建产物'
    if low.endswith('.uvoptx') or '.uvguix.' in low:
        return 'E_Keil会话文件'
    if low.endswith('.log'):
        return 'H_构建日志'
    if rel.startswith(('G474/', 'docs/', 'tools/')) or rel in ('README.md', 'LICENSE', '.gitignore'):
        return 'G_上游仓库文件'
    return 'I_其他'


def main():
    buckets = collections.Counter()
    sizes = collections.Counter()
    other = []
    for dp, dn, fns in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in ('__pycache__', '.git')]
        for fn in fns:
            if fn.endswith('.pyc'):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, ROOT).replace('\\', '/')
            b = classify(rel)
            buckets[b] += 1
            sizes[b] += os.path.getsize(p)
            if b == 'I_其他':
                other.append(rel)

    print('%-28s %6s %12s' % ('类别', '文件数', '大小'))
    for b in sorted(buckets):
        print('%-28s %6d %10.1f KB' % (b, buckets[b], sizes[b] / 1024))
    print('%-28s %6d %10.1f KB' % ('总计', sum(buckets.values()), sum(sizes.values()) / 1024))

    if other:
        print()
        print('未分类文件（I_其他）：')
        for r in sorted(other):
            print('    %s' % r)

    # 健康检查
    print()
    bad = []
    if 'D_构建产物' in buckets:
        bad.append('存在 %d 个构建产物（可运行 ota/scripts/tidy_workspace.py 清理）' % buckets['D_构建产物'])
    if 'H_构建日志' in buckets:
        bad.append('存在 %d 个构建日志（可删除）' % buckets['H_构建日志'])
    essential = ['ota/common/ota_common.h', 'ota/bootloader/MDK-ARM/BL.uvprojx',
                 'G474/MDK-ARM/G474.uvprojx', 'LICENSE', 'README.md',
                 'docs/01-ISO-TP传输层设计.md']
    for e in essential:
        if not os.path.exists(os.path.join(ROOT, e.replace('/', os.sep))):
            bad.append('缺少关键文件: %s' % e)
    if bad:
        print('提示:')
        for b in bad:
            print('    - %s' % b)
    else:
        print('工作区结构健康：无构建产物残留，关键文件齐全。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
