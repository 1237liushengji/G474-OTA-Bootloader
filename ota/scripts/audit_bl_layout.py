#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
审计 Bootloader 镜像与"控制区保留区"的冲突 —— 这条冲突会**把 BL 自己擦掉**。

背景（为什么必须单独审计）：

  `ota_common.h` 把页 0（0x08000000~0x080007FF）划给"向量表 + 控制区 4 个候选副本"：

      0x08000000 + 0x000  向量表            [0, 0x1D8)
      0x08000000 + 0x1D8  控制区副本 0       [0x1D8, 0x208)
      0x08000000 + 0x400  控制区副本 1       [0x400, 0x430)
      0x08040000 + 0x1D8  镜像页副本 0
      0x08040000 + 0x400  镜像页副本 1

  而 `bl_param.c` 的 `BL_ParamStore()` 写配置时是"**整页擦除**再回写"，
  回写的内容只有两样：**页首向量表** [0, 0x1D8) 与两份配置副本。
  **它不会回写 BL 自己的代码。**

  因此只要链接器把 BL 代码放进了页 0，第一次写控制区就会：
      擦掉页 0 → 只补回向量表与配置 → **BL 代码永久丢失 → 变砖**。

  编译期断言（`ota_common.h` 的血线 1/2）只看宏，**看不到链接结果**，
  所以这类冲突必须对**链接产物**（.hex）另做检查 —— 本脚本就是这道检查。

用法:

    # 只查构建产物（不需要硬件）
    python ota/scripts/audit_bl_layout.py

    # 连芯片一起查：读回页 0 与镜像页，判断"现在这颗片子会不会自毁"
    python ota/scripts/audit_bl_layout.py --on-chip

退出码: 0=无冲突, 1=发现冲突, 2=无法判断（文件缺失等）
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import zlib

from paths import workspace_root

ROOT = workspace_root()
CLI = r'D:\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe'

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---- 契约（与 ota_common.h 同值；这里只做检查，因此允许出现字面量并集中在此） ----
VECTAB_BYTES     = 0x000001D8
CFG_SLOT_OFFSET  = 0x000001D8
CFG_MIRROR_OFF   = 0x00000400
CFG_SIZE         = 48
CFG_MAGIC        = 0x4734424C
PAGE_SIZE        = 0x800
PRIMARY_PAGE     = 0x08000000
MIRROR_PAGE      = 0x08040000
BL_CODE_ADDR     = 0x08000800

CANDIDATES = [
    ('PRIMARY 副本0', PRIMARY_PAGE, CFG_SLOT_OFFSET),
    ('PRIMARY 副本1', PRIMARY_PAGE, CFG_MIRROR_OFF),
    ('MIRROR  副本0', MIRROR_PAGE,  CFG_SLOT_OFFSET),
    ('MIRROR  副本1', MIRROR_PAGE,  CFG_MIRROR_OFF),
]


# ------------------------------------------------------------------ Intel HEX
def load_hex(path):
    """返回 {绝对地址: 字节值}。正确处理 02/04 扩展地址记录。"""
    mem = {}
    base = 0
    with open(path, 'r', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line.startswith(':'):
                continue
            try:
                rec = bytes.fromhex(line[1:])
            except ValueError:
                continue
            n, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
            data = rec[4:4 + n]
            if typ == 0x00:
                for i, v in enumerate(data):
                    mem[base + addr + i] = v
            elif typ == 0x02:
                base = ((data[0] << 8) | data[1]) << 4
            elif typ == 0x04:
                base = ((data[0] << 8) | data[1]) << 16
    return mem


def hex_span(mem):
    if not mem:
        return None
    return min(mem), max(mem) + 1


def non_ff_ranges(mem, lo, hi):
    """[lo,hi) 内非 0xFF 的连续区间（缺记录视作 0xFF）。"""
    out = []
    cur = None
    for a in range(lo, hi):
        if mem.get(a, 0xFF) != 0xFF:
            if cur is None:
                cur = a
        else:
            if cur is not None:
                out.append((cur, a))
                cur = None
    if cur is not None:
        out.append((cur, hi))
    return out


# ------------------------------------------------------------------ 片上读取
def read_onchip(addr, length, outfile):
    cmd = [CLI, '-c', 'port=SWD', 'mode=normal',
           '-u', hex(addr), str(length), outfile]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors='replace', timeout=180)
    if r.returncode != 0 or not os.path.isfile(outfile):
        return None, r.stdout
    with open(outfile, 'rb') as f:
        return f.read(), r.stdout


def cfg_from(buf, off):
    raw = buf[off:off + CFG_SIZE]
    if len(raw) < CFG_SIZE:
        return None
    magic, seq, state, trialLeft = struct.unpack_from('<4I', raw, 0)
    crc = struct.unpack_from('<I', raw, 44)[0]
    return {
        'raw': raw, 'magic': magic, 'seq': seq, 'state': state,
        'trialLeft': trialLeft, 'crc': crc,
        'valid': (magic == CFG_MAGIC) and (crc == zlib.crc32(raw[:44])),
    }


STATE_NAME = {0: 'IDLE_A', 1: 'IDLE_B', 2: 'TRIAL_A', 3: 'TRIAL_B'}


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--hex', default=os.path.join(ROOT, 'firmware', 'BL.hex'),
                    help='要审计的 BL hex（默认 firmware/BL.hex）')
    ap.add_argument('--on-chip', action='store_true', help='同时读回芯片页 0 与镜像页')
    args = ap.parse_args()

    if not os.path.isfile(args.hex):
        print('[!!] 找不到 %s' % args.hex)
        return 2

    mem = load_hex(args.hex)
    span = hex_span(mem)
    if span is None:
        print('[!!] %s 里没有任何数据记录' % args.hex)
        return 2

    print('=' * 74)
    print('BL 镜像: %s' % os.path.relpath(args.hex, ROOT))
    print('  覆盖范围: 0x%08X ~ 0x%08X  (%d 字节)' % (span[0], span[1], span[1] - span[0]))
    print('  契约要求: BL 代码区 = 0x%08X 起（页 0 留给向量表 + 控制区）' % BL_CODE_ADDR)
    print('=' * 74)

    fails = []

    # ---- 检查 1：镜像是否压进控制区保留区 ----
    print()
    print('检查 1 · 镜像是否占用控制区保留区')
    for name, page, off in CANDIDATES:
        if page == MIRROR_PAGE:
            continue                                  # 镜像页不在 BL 镜像范围内，单列
        lo, hi = page + off, page + off + CFG_SIZE
        hit = non_ff_ranges(mem, lo, hi)
        if hit:
            n = sum(b - a for a, b in hit)
            print('  [!!] %s @ 0x%08X 被镜像占用 %d/%d 字节  %s'
                  % (name, lo, n, CFG_SIZE,
                     ' '.join('%02X' % mem.get(a, 0xFF) for a in range(lo, lo + 8))))
            fails.append('%s 被 BL 镜像占用' % name)
        else:
            print('  [OK] %s @ 0x%08X 干净（0xFF）' % (name, lo))

    # ---- 检查 2：页 0 里除了向量表还有多少代码 ----
    print()
    print('检查 2 · 页 0（0x%08X~0x%08X）占用情况' % (PRIMARY_PAGE, PRIMARY_PAGE + PAGE_SIZE - 1))
    used = non_ff_ranges(mem, PRIMARY_PAGE, PRIMARY_PAGE + PAGE_SIZE)
    total = sum(b - a for a, b in used)
    print('  非 0xFF 字节: %d/%d' % (total, PAGE_SIZE))
    for a, b in used:
        tag = ''
        if b <= PRIMARY_PAGE + VECTAB_BYTES:
            tag = '  <- 向量表'
        elif a >= BL_CODE_ADDR:
            tag = '  <- BL 代码区（正常）'
        else:
            tag = '  <- ⚠ 落在控制区页内（向量表之后）'
        print('    0x%08X ~ 0x%08X  (%d 字节)%s' % (a, b, b - a, tag))
    code_in_page0 = [(a, b) for a, b in used if b > PRIMARY_PAGE + VECTAB_BYTES]
    if code_in_page0:
        n = sum(b - a for a, b in code_in_page0)
        fails.append('页 0 向量表之后仍有 %d 字节镜像内容' % n)

    # ---- 检查 3：镜像结束地址是否越过 Slot A ----
    print()
    print('检查 3 · 镜像是否越界到 Slot A')
    slot_a = 0x0800C000
    if span[1] > slot_a:
        print('  [!!] 镜像结束 0x%08X 已进入 Slot A（0x%08X）' % (span[1], slot_a))
        fails.append('镜像越过 Slot A')
    else:
        print('  [OK] 结束于 0x%08X，未触及 Slot A（0x%08X），余量 %d 字节'
              % (span[1], slot_a, slot_a - span[1]))

    # ---- 检查 4（可选）：片上实况 ----
    chip_verdict = None
    if args.on_chip:
        print()
        print('检查 4 · 片上实况（读回页 0 与镜像页）')
        tmp = tempfile.mkdtemp(prefix='bl_audit_')
        p0 = os.path.join(tmp, 'page0.bin')
        pm = os.path.join(tmp, 'mirror.bin')
        buf0, out0 = read_onchip(PRIMARY_PAGE, PAGE_SIZE, p0)
        if buf0 is None:
            print('  [!!] 读取页 0 失败：')
            print('       ' + (out0 or '').strip().replace('\n', '\n       '))
            return 2
        bufm, _ = read_onchip(MIRROR_PAGE, PAGE_SIZE, pm)

        used0 = non_ff_ranges({i: b for i, b in enumerate(buf0)}, 0, PAGE_SIZE)
        print('  页 0 片上有数据: %d 字节' % sum(b - a for a, b in used0))
        code_after_vectab = [(a, b) for a, b in used0 if b > VECTAB_BYTES]
        if code_after_vectab:
            a, b = code_after_vectab[0]
            print('    最早的非向量表内容: 0x%08X（%d 字节）' % (PRIMARY_PAGE + a, b - a))

        print('  4 个控制区候选:')
        any_valid = False
        for name, page, off in CANDIDATES:
            src = buf0 if page == PRIMARY_PAGE else bufm
            if src is None:
                print('    %-14s 镜像页读取失败' % name)
                continue
            c = cfg_from(src, off)
            if c and c['valid']:
                any_valid = True
                print('    %-14s ✅ magic/CRC 有效  seq=%d state=%s trialLeft=%d'
                      % (name, c['seq'], STATE_NAME.get(c['state'], '?'), c['trialLeft']))
            else:
                print('    %-14s -- 无效（magic=0x%08X）'
                      % (name, c['magic'] if c else 0))

        if code_after_vectab:
            n = sum(b - a for a, b in code_after_vectab)
            chip_verdict = ('片上页 0 在向量表之后仍有 %d 字节内容：'
                            '若那是代码，则**下一次写控制区就会擦掉它**' % n)
        elif any_valid:
            chip_verdict = '片上页 0 只有向量表 + 控制区，布局正确'
        else:
            chip_verdict = '片上页 0 无有效控制区，但布局（未压代码）尚可'
        print('  结论: %s' % chip_verdict)

    # ---- 汇总 ----
    print()
    print('=' * 74)
    if fails:
        print('结论: ❌ 发现 %d 项冲突 —— **先修链接配置再烧录**' % len(fails))
        for x in fails:
            print('   * %s' % x)
        print()
        print('为什么危险：BL_ParamStore() 写控制区时整页擦除页 0，只回写向量表与配置副本，')
        print('            不回写代码。页 0 里若有代码，第一次写控制区（OTA 落定/回滚/试运行）')
        print('            就会把 BL 自己的代码擦掉 → 变砖。')
        print('修法：让 BL 的执行区从 0x%08X 开始（散列文件把 ROM 页 0 只留给向量表），' % BL_CODE_ADDR)
        print('      或把控制区改为只写 MIRROR 页（BL_PARAM_PRIMARY_WRITE 0），二者取一。')
        print('=' * 74)
        return 1

    print('结论: ✅ 无冲突（镜像未压入控制区保留区）')
    if chip_verdict:
        print('      片上: %s' % chip_verdict)
    print('=' * 74)
    return 0


if __name__ == '__main__':
    sys.exit(main())
