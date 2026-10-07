#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正（并在每次运行后**复核**）BL 工程的链接布局 —— 幂等。

它守的两件事：

  1) `bl.sct`：**页 0 只放向量表，代码从 0x08000800 开始**。
     页 0（0x08000000~0x080007FF）是控制区保留页，`bl_param.c` 写配置时会**整页擦除**
     且只回写向量表与另一份副本。页 0 里若放了代码，第一次写控制区就会把 BL 自己
     擦成砖（2026-10-05 实测事故：复位向量 = 0x08000325，而 0x08000324 处全为 0xFF）。
     正文与不变量见 `ota/scripts/bl_layout.py`（单一真相源）。

  2) `BL.uvprojx` 的 IROM/OCR_RVCT4 必须是 `0x08000000 + 0x0000B800`。
     两者与 scatter 不一致时，Keil 会按 Target 对话框**重新生成**分散加载文件，
     把 bl.sct 的改动悄悄覆盖掉（本项目反复踩过的坑）。

用法:
    python ota/scripts/fix_bl_region.py            # 只检查（默认，安全）
    python ota/scripts/fix_bl_region.py --apply    # 检查并修正
"""
import os
import re
import sys

from bl_layout import BL_REGION_SIZE, SCATTER_TEXT, check_scatter_text, write_scatter
from paths import workspace_root

ROOT = workspace_root()
SCT = os.path.join(ROOT, 'ota', 'bootloader', 'MDK-ARM', 'bl.sct')
PROJ = os.path.join(ROOT, 'ota', 'bootloader', 'MDK-ARM', 'BL.uvprojx')

REGION = '0x%08X' % BL_REGION_SIZE


def main():
    apply_ = '--apply' in sys.argv
    ok = True

    # ---- 1) bl.sct：写成规范文本并复核 ----
    if apply_:
        changed = write_scatter(SCT)
        print('  [%s] bl.sct %s' % ('OK' if not changed else '修正', '已是规范内容' if not changed else '已按规范重写'))
    try:
        cur = open(SCT, encoding='utf-8', errors='replace').read()
    except FileNotFoundError:
        print('  [!!] 找不到 %s' % SCT)
        return 1

    problems = check_scatter_text(cur)
    if problems:
        ok = False
        for p in problems:
            print('  [!!] bl.sct: %s' % p)
        print('       → 运行 --apply 按规范重写')
    else:
        print('  [OK] bl.sct 布局正确：页 0 只放向量表，代码从 0x08000800 起')

    if cur != SCATTER_TEXT and not apply_:
        print('  [i]  bl.sct 与规范文本有差异（不影响判据，但建议 --apply 对齐）')

    # ---- 2) 工程内存布局：与 scatter 的加载域保持一致 ----
    p = open(PROJ, encoding='utf-8', errors='replace').read()
    porig = p

    def fix(tag_txt, text):
        return re.subn(
            r'(<' + tag_txt + r'>\s*<Type>1</Type>\s*)(?:<!--.*?-->\s*)?'
            r'<StartAddress>[^<]*</StartAddress>\s*<Size>[^<]*</Size>',
            r'\g<1><StartAddress>0x8000000</StartAddress>\n                <Size>' + REGION + r'</Size>',
            text, flags=re.S | re.I)

    for tag in ('IROM', 'OCR_RVCT4'):
        p, n = fix(tag, p)
        if n:
            print('  [OK] %s 设为 0x8000000 / %s' % (tag, REGION))
        else:
            # 已经是目标值时上面的替换会失配，单独确认一次
            if re.search(r'<' + tag + r'>.*?<Size>\s*' + REGION + r'\s*</Size>', p, re.S | re.I):
                print('  [OK] %s 已是 0x8000000 / %s' % (tag, REGION))
            else:
                print('  [!]  %s 未匹配，请人工检查' % tag)
                ok = False

    # ---- 3) 关键开关：必须**关闭**"使用 Target 对话框内存布局"，否则 bl.sct 被无视 ----
    #   µVision 的标签名是 `<umfTarg>`（1=用 Target 对话框的 IROM/IRAM 重新生成分散加载文件，
    #   0=用工程里指定的 ScatterFile）。名字叫 `<UseMemoryLayoutFromTargetDialog>` 的标签
    #   **根本不存在** —— 早期生成脚本写的是后者，所以这个开关一直是 1，
    #   bl.sct 里"页 0 只放向量表"的布局从未生效（这正是变砖事故的另一半原因）。
    if re.search(r'<umfTarg>\s*0\s*</umfTarg>', p, re.I):
        print('  [OK] umfTarg=0（链接器使用 bl.sct）')
    else:
        p2, n = re.subn(r'<umfTarg>\s*[01]\s*</umfTarg>', '<umfTarg>0</umfTarg>', p, flags=re.I)
        if n == 0:
            p2, n = re.subn(r'(<LDads>)', r'\g<1>\n              <umfTarg>0</umfTarg>', p, count=1, flags=re.I)
        if n:
            print('  [修正] umfTarg -> 0（关闭 Target 对话框内存布局，改用 bl.sct）')
            p = p2
        else:
            print('  [!!] 找不到 <umfTarg> 也无法插入 —— 请人工确认链接器用的是 bl.sct')
            ok = False

    if apply_ and p != porig:
        open(PROJ, 'w', encoding='utf-8', newline='').write(p)
        print('  [OK] 已写回 %s' % os.path.relpath(PROJ, ROOT))
    elif not apply_ and p != porig:
        print('  [i]  %s 需要修正（--apply 生效）' % os.path.relpath(PROJ, ROOT))

    print()
    print('结论:', '布局正确' if ok else '需要修正（--apply）')
    print('提示: 改完务必重编 BL 并跑 `python ota/scripts/audit_bl_layout.py` 审计产物 ——')
    print('      编译期断言只看宏，看不到链接结果，只有审计 .hex 才能发现这类冲突。')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
