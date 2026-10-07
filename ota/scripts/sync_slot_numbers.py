#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
槽大小 224KB -> 208KB 后，更新所有"满槽"相关的硬编码数值。

新数值（由契约推导，可用 tools_verify_layout.py 复核）：
  净荷上限 229344 -> 212960 字节 (0x34000-32)
  满槽块数 1835/225 -> 209
  擦除页数 112 -> 104 页，2.8s -> 2.6s
  传输耗时 ~73s -> ~62s；合计 ~76s -> ~64s
"""
import os

from paths import workspace_root
import re
import sys

ROOT = workspace_root()

RULES = [
    # ---------- 测试 ----------
    ('ota/tools/test_ota_host.py', r'\b229344\b', '212960', 6, '净荷字节数'),
    ('ota/tools/test_ota_host.py', r'225 块（<255，不回绕）', '209 块（<255，不回绕）', 1, '注释'),
    ('ota/tools/test_ota_host.py', r'result\.blocks_total, 225\)', 'result.blocks_total, 209)', 1, '块数断言'),
    ('ota/tools/test_ota_host.py', r'# ceil\(229344/1021\)', '# ceil(212960/1021)', 1, '推导注释'),
    ('ota/tools/test_ota_host.py', r'oh\.OTA_IMG_MAX_PAYLOAD, bs\), 225\)', 'oh.OTA_IMG_MAX_PAYLOAD, bs), 209)', 2, '块数断言x2'),
    ('ota/tools/test_ota_host.py', r'assertLess\(225, oh\.BLOCK_SEQ_MOD \+ 1\)', 'assertLess(209, oh.BLOCK_SEQ_MOD + 1)', 1, '不回绕断言'),
    ('ota/tools/test_ota_host.py', r'满槽 229344B 恰好 225 块', '满槽 212960B 恰好 209 块', 1, '注释'),
    ('ota/tools/test_ota_host.py', r'erase_estimate_ms\(oh\.OTA_IMG_MAX_PAYLOAD\), 112 \* 25\)', 'erase_estimate_ms(oh.OTA_IMG_MAX_PAYLOAD), 104 * 25)', 1, '擦除估算'),
    ('ota/tools/test_ota_host.py', r'oh\.OTA_IMG_MAX_PAYLOAD, 224 \* 1024 - 32\)', 'oh.OTA_IMG_MAX_PAYLOAD, 208 * 1024 - 32)', 1, '净荷满载断言'),
    ('ota/tools/test_ota_host.py', r'# 224KB', '# 208KB', 1, '注释'),
    ('ota/tools/test_ota_host.py', r'0x08008020', '0x0800C020', 3, 'APP 基址'),
    # ---------- 上位机代码注释 ----------
    ('ota/tools/ota_host.py', r'# 单槽 224KB\b', '# 单槽 208KB', 1, '常量注释'),
    ('ota/tools/ota_host.py', r'# 0x08008020（0x34 目标）', '# 0x0800C020（0x34 目标）', 1, '常量注释'),
    # ---------- 文档 ----------
    ('ota/README.md', r'--load-addr 0x08008020', '--load-addr 0x0800C020', 1, '打包示例'),
    ('ota/README.md', r'\*\*≈ 73 s\*\*', '**≈ 62 s**', 1, '传输耗时'),
    ('ota/README.md', r'≈ 2\.9 s \|', '≈ 2.6 s |', 1, '擦除耗时'),
    ('ota/README.md', r'\| 1 835 \| \*\*225\*\* \|', '| 1 835 | **209** |', 1, '报文数对比'),
    ('ota/README.md', r'112 页 × 25 ms', '104 页 × 25 ms', 1, '页数'),
]

MANUAL_NOTE = []


def main():
    apply = '--apply' in sys.argv
    ok = miss = 0
    for rel, pat, new, want, desc in RULES:
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isfile(p):
            print('[跳过] %s' % rel); continue
        raw = open(p, encoding='utf-8', errors='replace').read()
        raw2, n = re.subn(pat, new, raw)
        if n != want:
            print('[不匹配] %-40s %-16s 命中 %d 期望 %d' % (rel, desc, n, want))
            miss += 1
            continue
        ok += 1
        if apply:
            open(p, 'w', encoding='utf-8', newline='').write(raw2)
    print()
    print('%s：%d 条可应用，%d 条未匹配' % ('已应用' if apply else '干跑', ok, miss))
    return 1 if miss else 0


if __name__ == '__main__':
    sys.exit(main())
