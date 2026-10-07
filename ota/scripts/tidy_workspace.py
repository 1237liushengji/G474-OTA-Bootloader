#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作区整理脚本（一次性，可重复运行）。

做的事：
  1. 删除构建产物（.o/.d/.crf/.axf/.map/.htm/.lnp/.dep/.bak/.iex 等）——保留 .hex 以便烧录；
  2. 删除 _baseline_backup/（上游文件已可从 GitHub 官方归档恢复，无需备份）；
  3. 删除 src_ref/（改用 codeload 官方 zip 后已无用；保留说明见 ota/scripts/README.md）；
  4. 删除 __pycache__ 与临时脚本 tools_wait_and_fetch.py / tools_fetch_repo.py（已被 codeload 方案取代）；
  5. 把根目录散落的 tools_*.py 收纳到 ota/scripts/，并改写其"工作区根"解析方式；
  6. 更新 .gitignore（补 BL 工程输出目录与 ota 下的中间产物）。

不含：修改源码/文档（那些是交付内容）。
"""
import os
import re
import shutil
import sys

from paths import workspace_root

ROOT = workspace_root()

ARTIFACT_EXT = ('.o', '.d', '.crf', '.axf', '.map', '.htm', '.lnp', '.dep',
                '.bak', '.iex', '.build_log.htm')
BUILD_DIRS = [
    'G474/MDK-ARM/G474',
    'ota/bootloader/MDK-ARM/G474',
]
DELETE_DIRS = [
    '_baseline_backup',
    'src_ref',
]
DELETE_FILES = [
    'tools_wait_and_fetch.py',      # codeload 方案已取代它
    'tools_fetch_repo.py',          # 同上（逐文件走 API 太慢且撞配额）
    'tools_inventory.py',           # 合入 ota/scripts/inventory.py
    'tools_inventory2.py',          # 同上
    'tools_check_main.py',          # 一次性排查，已删
]
# 收纳到 ota/scripts/ 的脚本（保留全部，便于复现与复核）
KEEP_SCRIPTS = [
    'tools_verify_layout.py',       # 收尾必跑：核对链接地址与体积
    'tools_make_bl_project.py',     # 从 APP 工程派生 BL 工程
    'tools_check_hal_symbols.py',   # HAL 符号存在性检查
    'tools_compare_with_github.py', # 与 GitHub 官方归档对比
    'tools_fix_main_c.py',          # 幂等重放 main.c 接线改动
    'tools_inventory.py',           # 盘点工作区（改名 inventory.py）
    'tools_apply_ota_patches.py',   # 初版补丁脚本（历史，保留供追溯）
    'tools_sync_partition_docs.py', # 分区变更时的文档同步（历史）
    'tools_sync_slot_numbers.py',   # 槽尺寸变更时的数字同步（历史）
    'tools_tidy_workspace.py',      # 本脚本自身（整理可复现）
]

GITIGNORE_ADD = """
# Keil MDK 构建产物（输出目录/用户界面状态/依赖缓存）
G474/MDK-ARM/G474/
**/MDK-ARM/G474/
*.uvguix.*
*.uvoptx
*.bak
*_sct.Bak
*.dep
*.crf
*.o
*.d
*.axf
*.lnp
*.htm
*.build_log.htm
*.log

# 本工程的双工程产物默认输出名
BL.hex
G474.hex

# Python
__pycache__/
*.pyc

# 上位机生成的报文记录
tools/can_log_*.csv

# 设备验证输出（可重新生成）
ota/verify/*_output.txt
ota/bootloader/verify_bl_output.txt
"""

PATHS_MODULE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区根定位：从本文件位置向上找包含 G474/MDK-ARM 的目录。

脚本被收纳到 ota/scripts/ 后，不能再简单用 dirname(__file__) 当工作区根，
因此统一走这里，保证移动/复制后仍可用。
"""
import os


def workspace_root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(d, 'G474', 'MDK-ARM')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # 退化为"上两级"，与收纳位置 ota/scripts/ 匹配
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


ROOT = workspace_root()
'''


def main():
    dry = '--apply' not in sys.argv
    log = []

    def rm_dir(rel):
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isdir(p):
            log.append('  [跳过] %s 不存在' % rel)
            return
        n = sum(len(f) for _d, _s, f in os.walk(p))
        if not dry:
            shutil.rmtree(p)
        log.append('  [删除目录] %-44s %d 个文件' % (rel, n))

    def rm_file(rel):
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isfile(p):
            return
        if not dry:
            os.remove(p)
        log.append('  [删除文件] %s' % rel)

    # 1) 构建产物
    removed_art = 0
    for bd in BUILD_DIRS:
        p = os.path.join(ROOT, bd.replace('/', os.sep))
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            full = os.path.join(p, fn)
            if not os.path.isfile(full):
                continue
            if fn.endswith('.hex'):
                continue                       # 保留 hex 以便烧录
            if fn.endswith(ARTIFACT_EXT) or fn.endswith('.sct') or 'build_log' in fn \
               or fn.endswith('_sct.Bak') or fn.endswith('.uvoptx'):
                if not dry:
                    os.remove(full)
                removed_art += 1
    log.append('  [清理构建产物] 共 %d 个（保留 .hex）' % removed_art)

    # 2) 备份 / 快照目录
    for d in DELETE_DIRS:
        rm_dir(d)

    # 3) 一次性文件
    for f in DELETE_FILES:
        rm_file(f)

    # 4) __pycache__
    for dp, dn, _fn in os.walk(ROOT):
        for d in list(dn):
            if d == '__pycache__':
                rm_dir(os.path.relpath(os.path.join(dp, d), ROOT))

    # 5) 收纳脚本
    scripts_dir = os.path.join(ROOT, 'ota', 'scripts')
    if not dry:
        os.makedirs(scripts_dir, exist_ok=True)
    moved = []
    for name in KEEP_SCRIPTS:
        src = os.path.join(ROOT, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(scripts_dir, name.replace('tools_', ''))
        if os.path.exists(dst):
            # 幂等：目标已存在（上次已搬过，或本脚本正从 scripts/ 运行）
            # -> 保留 scripts/ 下的版本，删掉根目录残留，避免两份分叉
            if not dry:
                os.remove(src)
            moved.append((name, os.path.relpath(dst, ROOT) + '  [已存在,删根目录残留]'))
            continue
        if dry:
            moved.append((name, os.path.relpath(dst, ROOT)))
            continue
        raw = open(src, encoding='utf-8', errors='replace').read()
        # 改写根路径解析：把 dirname(abspath(__file__)) 换成 paths.workspace_root()
        if 'from paths import' not in raw:
            raw = re.sub(r"^import os\n", "import os\n\nfrom paths import workspace_root\n", raw, count=1, flags=re.M)
            if 'from paths import' not in raw:
                raw = "from paths import workspace_root\n" + raw
            raw = re.sub(r"ROOT\s*=\s*os\.path\.dirname\(os\.path\.abspath\(__file__\)\)",
                         "ROOT = workspace_root()", raw)
            raw = re.sub(r"root\s*=\s*os\.path\.dirname\(os\.path\.abspath\(__file__\)\)",
                         "root = workspace_root()", raw)
        open(dst, 'w', encoding='utf-8', newline='').write(raw)
        os.remove(src)
        moved.append((name, os.path.relpath(dst, ROOT)))
    log.append('  [收纳脚本] %d 个 -> ota/scripts/' % len(moved))
    for a, b in moved:
        log.append('        %-32s -> %s' % (a, b))

    # 6) 写 paths.py
    if not dry:
        with open(os.path.join(scripts_dir, 'paths.py'), 'w', encoding='utf-8', newline='') as fh:
            fh.write(PATHS_MODULE)
    log.append('  [写入] ota/scripts/paths.py')

    # 7) 更新 .gitignore（补齐 BL 工程输出目录与中间产物）
    gi = os.path.join(ROOT, '.gitignore')
    if os.path.isfile(gi):
        cur = open(gi, encoding='utf-8', errors='replace').read()
        if '**/MDK-ARM/G474/' not in cur:
            if not dry:
                with open(gi, 'w', encoding='utf-8', newline='') as fh:
                    fh.write(GITIGNORE_ADD.lstrip('\n'))
            log.append('  [更新] .gitignore（补齐构建产物与可重生成输出）')
        else:
            log.append('  [跳过] .gitignore 已更新过')
    else:
        if not dry:
            with open(gi, 'w', encoding='utf-8', newline='') as fh:
                fh.write(GITIGNORE_ADD.lstrip('\n'))
        log.append('  [写入] .gitignore')

    print('\n'.join(log))
    print()
    print('模式: %s' % ('干跑（未改动）' if dry else '已应用'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
