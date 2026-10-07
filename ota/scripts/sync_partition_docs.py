#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分区表变更同步（定向、带语境、可干跑）。

背景：BL 实测固件 32752 字节，30KB 预算不够，故 BL 区扩到 46KB，Slot A 由 224KB 收到 208KB：
  Slot A 槽基址   0x08008000 -> 0x0800C000     (uvprojx 无前导 0 写作 0x800C000)
  Slot A APP 基址 0x08008020 -> 0x0800C020     (uvprojx: 0x800C020)
  槽大小          0x38000    -> 0x34000        (224KB -> 208KB)
  APP IROM 大小   0x37FE0    -> 0x33FE0
  VECT_TAB_OFFSET 0x8020     -> 0xC020
  BL 代码区上界   0x08008000 -> 0x0800B800，预算 30KB -> 46KB

安全策略：每条规则都要求"仅在指定文件"且"带足够语境"，避免误伤
（例如 0x38000 会命中 ADC_SMPR 等无关寄存器值，绝不能裸替换）。

用法: python tools_sync_partition_docs.py [--apply]
      不带 --apply 只干跑并打印将要发生的变化。
"""
import os

from paths import workspace_root
import re
import sys

ROOT = workspace_root()

# (文件, 正则, 替换, 期望次数, 说明)
RULES = [
    # ---------- 上位机 ota_host.py ----------
    ('ota/tools/ota_host.py',
     r'OTA_SLOT_SIZE = 0x38000\b', 'OTA_SLOT_SIZE = 0x34000', 1, '槽大小常量'),
    ('ota/tools/ota_host.py',
     r'OTA_SLOT_A_BASE = 0x08008000\b', 'OTA_SLOT_A_BASE = 0x0800C000', 1, 'Slot A 槽基址'),
    ('ota/tools/ota_host.py',
     r'BL 独占 0x08000800~0x08007FFF；Slot A 槽基 0x08008000、Slot B 槽基\n',
     'BL 独占 0x08000800~0x0800B7FF；Slot A 槽基 0x0800C000、Slot B 槽基\n', 1, 'docstring 分区描述'),
    ('ota/tools/ota_host.py',
     r'0x08040800，单槽 224KB\(0x38000\)', '0x08040800，单槽 208KB(0x34000)', 1, 'docstring 单槽大小'),
    ('ota/tools/ota_host.py',
     r'SlotA APP=0x08008020', 'SlotA APP=0x0800C020', 1, 'docstring APP 基址'),
    ('ota/tools/ota_host.py',
     r'# Slot A 槽基址（BL 占 0x08000800~0x08007FFF）',
     '# Slot A 槽基址（BL 占 0x08000800~0x0800B7FF）', 1, '常量注释'),
    ('ota/tools/ota_host.py',
     r'# 单槽最大净荷 = 224KB - 32B', '# 单槽最大净荷 = 208KB - 32B', 1, '净荷注释'),
    ('ota/tools/ota_host.py',
     r'默认 SlotA 0x08008020', '默认 SlotA 0x0800C020', 1, 'help 文本'),

    # ---------- 上位机测试 ----------
    ('ota/tools/test_ota_host.py',
     r'0x08008020', '0x0800C020', 5, '测试里的 APP 基址'),
    ('ota/tools/test_ota_host.py',
     r'oh\.OTA_SLOT_A_BASE, 0x08008000', 'oh.OTA_SLOT_A_BASE, 0x0800C000', 1, 'Slot A 槽基'),
    ('ota/tools/test_ota_host.py',
     r'oh\.OTA_SLOT_SIZE, 0x38000', 'oh.OTA_SLOT_SIZE, 0x34000', 1, '槽大小断言'),
    ('ota/tools/test_ota_host.py',
     r'0x38000 - 32', '0x34000 - 32', 1, '净荷断言'),
    ('ota/tools/test_ota_host.py',
     r'# 满槽 224KB-32', '# 满槽 208KB-32', 1, '块数注释'),

    # ---------- BL_BUILD.md ----------
    ('ota/bootloader/BL_BUILD.md',
     r'\| `0x08008000` \| 224 KB \(`0x38000`\)',
     '| `0x0800C000` | 208 KB (`0x34000`)', 1, '分区表 Slot A 行'),
    ('ota/bootloader/BL_BUILD.md',
     r'\| `0x08040800` \| 224 KB \(`0x38000`\)',
     '| `0x08040800` | 208 KB (`0x34000`)', 1, '分区表 Slot B 行'),
    ('ota/bootloader/BL_BUILD.md',
     r'APP 链接基址 = `0x08008020`', 'APP 链接基址 = `0x0800C020`', 1, 'APP 链接基址'),
    ('ota/bootloader/BL_BUILD.md',
     r'`addr < OTA_SLOT_A_ADDR`（= `0x08008000`）',
     '`addr < OTA_SLOT_A_ADDR`（= `0x0800C000`）', 1, '保护门限'),
    ('ota/bootloader/BL_BUILD.md',
     r'到 `0x08008000`', '到 `0x0800B800`', 1, 'BL 代码上界'),

    # ---------- docs/01 ----------
    ('ota/docs/01-分区规划与链接配置.md',
     r'\| `0x08008000` \| `0x0803FFFF` \| 224 KB \| 112 \|',
     '| `0x0800C000` | `0x0803FFFF` | 208 KB | 104 |', 1, '分区表 Slot A 行'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'\| `0x08040800` \| `0x080787FF` \| 224 KB \| 112 \|',
     '| `0x08040800` | `0x08074800` | 208 KB | 104 |', 1, '分区表 Slot B 行'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'两槽大小完全相同（224 KB = `0x38000`）',
     '两槽大小完全相同（208 KB = `0x34000`）', 1, '两槽同尺寸'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'Slot A 的 APP 链接基址 = `0x08008020`', 'Slot A 的 APP 链接基址 = `0x0800C020`', 1, 'APP 基址'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'\| `0x08008020` \| `0x37FE0`（≈223\.99 KB） \| `0x00008020` \|',
     '| `0x0800C020` | `0x33FE0`（≈207.99 KB） | `0x0000C020` |', 1, '构建配置表 A 行'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'\| `0x08040820` \| `0x37FE0` \| `0x00040820` \|',
     '| `0x08040820` | `0x33FE0` | `0x00040820` |', 1, '构建配置表 B 行'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'<StartAddress>0x8008020</StartAddress>', '<StartAddress>0x800C020</StartAddress>', 1, 'XML 示例 IROM'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'<Size>0x37FE0</Size>', '<Size>0x33FE0</Size>', 1, 'XML 示例 Size'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'单槽可用净空间 \| 224 KB − 32 B ≈ \*\*223\.97 KB\*\*',
     '单槽可用净空间 | 208 KB − 32 B ≈ **207.97 KB**', 1, '容量核算'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'A/B 双槽（各 224 KB）\+ 30 KB BL', 'A/B 双槽（各 208 KB）+ 46 KB BL', 1, '结论行'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'≤ 224 KB（`0x38000`）', '≤ 208 KB（`0x34000`）', 1, 'map 复核项'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'0x08008000 └─', '0x0800C000 └─', 1, '内存布局图'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'`0x08008000` 之后）', '`0x0800C000` 之后）', 1, '血线描述'),
    ('ota/docs/01-分区规划与链接配置.md',
     r'本版已给 BL 预留独立 30 KB（`0x08000800~0x08007FFF`），Slot A 后移到 `0x08008000`',
     '本版已给 BL 预留独立 46 KB（`0x08000800~0x0800B7FF`），Slot A 后移到 `0x0800C000`',
     1, '风险表'),

    # ---------- docs/02 ----------
    ('ota/docs/02-Bootloader框架与APP集成.md',
     r'`StartAddress = 0x8008020`、`Size = 0x37FE0`',
     '`StartAddress = 0x800C020`、`Size = 0x33FE0`', 1, 'uvprojx 示例'),

    # ---------- docs/03 ----------
    ('ota/docs/03-OTA协议与刷写时序.md',
     r'Slot A → `0x08008020`', 'Slot A → `0x0800C020`', 1, '0x34 addr 说明'),
    ('ota/docs/03-OTA协议与刷写时序.md',
     r'`224KB − 32B`', '`208KB − 32B`', 1, 'size 约束'),
    ('ota/docs/03-OTA协议与刷写时序.md',
     r'### 5\.3 端到端时间（满槽 224 KB 镜像', '### 5.3 端到端时间（满槽 208 KB 镜像', 1, '5.3 标题'),
    ('ota/docs/03-OTA协议与刷写时序.md',
     r'镜像净荷上限 = 224 KB − 32 B = \*\*229 344 B\*\*',
     '镜像净荷上限 = 208 KB − 32 B = **212 960 B**', 1, '净荷上限'),
    ('ota/docs/03-OTA协议与刷写时序.md',
     r'目标槽 224 KB = 112 页', '目标槽 208 KB = 104 页', 1, '擦除耗时'),

    # ---------- docs/05 ----------
    ('ota/docs/05-测试与验收方法.md',
     r'--load-addr 0x08008020', '--load-addr 0x0800C020', 1, '打包示例'),
    ('ota/docs/05-测试与验收方法.md',
     r'请求写 A（`0x08008020`）', '请求写 A（`0x0800C020`）', 1, '用例 2.5'),
    ('ota/docs/05-测试与验收方法.md',
     r'满槽 224KB 镜像传输 \| 1835 块', '满槽 208KB 镜像传输 | 209 块', 1, '吞吐表'),
    ('ota/docs/05-测试与验收方法.md',
     r'\*\*225 个 `36`（每块 1021B）\*\*', '**209 个 `36`（每块 1021B）**', 1, '干跑期望'),
    ('ota/docs/05-测试与验收方法.md',
     r'擦除 → 225 块', '擦除 → 209 块', 1, '用例 4.1'),

    # ---------- README ----------
    ('ota/README.md',
     r'`ISOTP_MAX_MSG`（原 128） \| `Drivers/User/Inc/isotp.h`', '`ISOTP_MAX_MSG`（原 128） | `Drivers/User/Inc/isotp.h`',
     1, '占位保持'),
    ('ota/README.md',
     r'Slot A  \(224KB\)', 'Slot A  (208KB)', 1, '分区图 A'),
    ('ota/README.md',
     r'Slot B  \(224KB\)', 'Slot B  (208KB)', 1, '分区图 B'),
    ('ota/README.md',
     r'0x08008000  Slot A', '0x0800C000  Slot A', 1, '分区图地址'),
    ('ota/README.md',
     r'─ 74 20 03 FD── maxLen=1021', '─ 74 20 03 FD── maxLen=1021', 1, '占位保持'),
    ('ota/README.md',
     r'（225 块，满 2KB 页才落盘）', '（209 块，满 2KB 页才落盘）', 1, '时序图块数'),
    ('ota/README.md',
     r'满槽 224 KB 镜像传输 \| 1835 块 × 20 帧 × 2 ms \| \*\*≈ 73 s\*\*',
     '满槽 208 KB 镜像传输 | 209 块 × 296 ms | **≈ 62 s**', 1, '吞吐表'),
    ('ota/README.md',
     r'Flash 编程 \| 112 页 × 25 ms \| ≈ 2\.9 s', 'Flash 编程 | 104 页 × 25 ms | ≈ 2.6 s', 1, '编程耗时'),
    ('ota/README.md',
     r'\| 满槽 224 KB 镜像的 `0x36` 报文数 \| 1 835 \| \*\*225\*\* \|',
     '| 满槽 208 KB 镜像的 `0x36` 报文数 | 1 835 | **209** |', 1, '对比表报文数'),
    ('ota/README.md',
     r'\| \*\*CAN 直连合计\*\* \| \*\*≈ 76 s\*\* \| \*\*≈ 9 s\*\*（↑ 约 8\.4×） \|',
     '| **CAN 直连合计** | **≈ 76 s** | **≈ 9 s**（↑ 约 8.4×） |', 1, '占位保持'),
    ('ota/README.md',
     r'`--pack app.bin --load-addr 0x08008020`', '`--pack app.bin --load-addr 0x0800C020`', 1, '打包示例'),
]


def main():
    apply = '--apply' in sys.argv
    total_ok = total_miss = 0
    by_file = {}
    for rel, pat, new, want, desc in RULES:
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isfile(p):
            print('[跳过] %s 不存在' % rel)
            continue
        raw = open(p, encoding='utf-8', errors='replace').read()
        raw2, n = re.subn(pat, new, raw)
        if n != want:
            print('[不匹配] %-46s %-22s 命中 %d 期望 %d' % (rel, desc, n, want))
            total_miss += 1
            continue
        if new == re.search(pat, raw).group(0):
            print('[无需改] %-46s %s' % (rel, desc))
            total_ok += 1
            continue
        by_file.setdefault(p, []).append(desc)
        total_ok += 1
        if apply:
            open(p, 'w', encoding='utf-8', newline='').write(raw2)

    print()
    if apply:
        print('已应用 %d 条规则，涉及 %d 个文件；未匹配 %d 条' % (total_ok, len(by_file), total_miss))
    else:
        print('干跑：可应用 %d 条规则，涉及 %d 个文件；未匹配 %d 条' % (total_ok, len(by_file), total_miss))
        for p, descs in sorted(by_file.items()):
            print('  %s' % p.replace(ROOT + os.sep, ''))
            for d in descs:
                print('      - %s' % d)
    return 1 if total_miss else 0


if __name__ == '__main__':
    sys.exit(main())
