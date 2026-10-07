#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把工作区与 GitHub 官方 zip（codeload 下载）逐文件对比。

用途：
  1. 找出工作区缺失/多余/内容不同的文件；
  2. 验证"从 E 盘副本补齐的 G474 与 GitHub 主线等价"这一判断是否成立
     （此前用 SHA256+行数证明过，这里用官方归档再确认一次）。

用法: python tools_compare_with_github.py <解包出的仓库根目录>
"""
import hashlib
import os

from paths import workspace_root
import sys

ROOT = workspace_root()

# 工作区里"不属于上游仓库"的路径（本会话产出/工具/备份）
EXTRA_PREFIXES = ('ota/', 'src_ref/', '_baseline_backup/', 'build_', 'tools_', 'WORKSPACE_SOURCES.md')


def walk_files(base):
    out = {}
    for dp, dn, fns in os.walk(base):
        dn[:] = [d for d in dn if d not in ('__pycache__', '.git')]
        for fn in fns:
            if fn.endswith('.pyc'):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, base).replace('\\', '/')
            out[rel] = p
    return out


def norm_hash(path, normalize_eol=True):
    b = open(path, 'rb').read()
    if normalize_eol:
        b = b.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
    return hashlib.sha256(b).hexdigest()


def main():
    if len(sys.argv) < 2:
        print('用法: python tools_compare_with_github.py <官方仓库根>')
        return 2
    gh_root = sys.argv[1]
    if not os.path.isdir(gh_root):
        print('!! 目录不存在: %s' % gh_root)
        return 2

    gh = walk_files(gh_root)
    me = walk_files(ROOT)

    extra = [k for k in me if k.startswith(EXTRA_PREFIXES) or k.split('/')[0] in ('ota', 'src_ref', '_baseline_backup')]
    me_repo = {k: v for k, v in me.items() if k not in extra}

    missing = sorted(set(gh) - set(me_repo))
    surplus = sorted(set(me_repo) - set(gh))
    common = sorted(set(gh) & set(me_repo))

    diff_eol_only = []
    diff_content = []
    for rel in common:
        if norm_hash(gh[rel]) != norm_hash(me_repo[rel]):
            # 区分"仅换行符不同"与"内容不同"
            if norm_hash(gh[rel], False) == norm_hash(me_repo[rel], False):
                diff_content.append((rel, '内容相同但原始字节不同(编码?)'))
            elif open(gh[rel], 'rb').read().replace(b'\r\n', b'\n') == \
                 open(me_repo[rel], 'rb').read().replace(b'\r\n', b'\n'):
                diff_eol_only.append(rel)
            else:
                diff_content.append((rel, '内容不同'))

    print('=== 官方仓库文件数: %d ===' % len(gh))
    print('=== 工作区(排除本会话产出)文件数: %d ===' % len(me_repo))
    print()
    print('[缺失] 官方有、工作区没有: %d' % len(missing))
    for m in missing:
        print('    %s' % m)
    print()
    print('[多余] 工作区有、官方没有: %d' % len(surplus))
    for s in surplus[:20]:
        print('    %s' % s)
    if len(surplus) > 20:
        print('    ...(共 %d)' % len(surplus))
    print()
    print('[仅换行符不同] %d 个文件（内容等价，可接受）' % len(diff_eol_only))
    for d in diff_eol_only[:15]:
        print('    %s' % d)
    if len(diff_eol_only) > 15:
        print('    ...(共 %d)' % len(diff_eol_only))
    print()
    print('[内容不同] %d 个文件' % len(diff_content))
    for d, why in diff_content:
        print('    %-52s %s' % (d, why))
    print()
    print('=' * 70)
    if not missing and not diff_content:
        print('结论: 工作区与 GitHub 官方归档一致（除 %d 个仅换行符差异）' % len(diff_eol_only))
    else:
        print('结论: 需处理 —— 缺失 %d 个, 内容不同 %d 个' % (len(missing), len(diff_content)))
    print('=' * 70)
    return 0 if (not missing and not diff_content) else 1


if __name__ == '__main__':
    sys.exit(main())
