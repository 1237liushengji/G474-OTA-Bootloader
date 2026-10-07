#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_all.py —— OTA 三方静态自检聚合入口（契约 + APP + BL + 文档 + 上位机常量）

为什么需要它：吞吐优化把**同一个数值**（应用报文上限 1024 / 单块净荷 1021）同时压在
三个代码库上（app 的 isotp.h、契约 ota_common.h、上位机 ota_host.py）。靠人记住
"三处一起改"不可靠，所以这里把这件事变成可执行断言。纯 Python 静态解析：
不联网、不依赖编译器、**不修改任何被检查的文件**。

用法：
    python ota/verify/verify_all.py                 # 校验当前仓库（BL 自检 15 项 + 跨库 8 项）
    python ota/verify/verify_all.py --root <dir>    # 校验一份仓库副本（verify_selftest.py 注入反例用）
    python ota/verify/verify_all.py --skip-bl       # 只跑跨库检查

退出码：全部通过 = 0；任一 FAIL = 1。

检查项：
    [BL]  由 ota/bootloader/verify_bl.py 提供（15 项，原样保留，不重复实现）
    [C1]  契约自洽：OTA_ISOTP_MAX_MSG==1024、块长==MAX-3、maxLen 字段==块长、12 位上限、isotp.h 一致性 #error 在位
    [C2]  三方数值一致：BL 的 BL_ISOTP_MAX_MSG 引用契约宏；APP 无 125/128 报文上限字面量
    [C3]  APP 无冗余上限宏：不得 #define OTA_MSG_MAX 之类，必须走契约 UDS_MAXLEN_FIELD
    [C4]  0x34 maxLen 字节级自证：应答取自 UDS_MAXLEN_FIELD，且 Python 独立算出 1021 -> 0x03FD
    [C5]  越界保护存在性：Ota_ReadDid 查 respMax、控制区写查 sizeof(OTA_Cfg)/OTA_CFG_PAGE_SIZE、
          页缓冲尺寸 == Flash 页、隧道解析器查 outMax
    [C6]  文档数字一致性：docs/*.md + README.md 不得残留与新契约矛盾的数字（允许"优化前/后"对比语境）
    [C7]  槽首保留区/CRC 等跨库常量仍与契约同源（且 APP 基址保持 0x80 对齐）
    [C8]  上位机 ota_host.py 的应用报文上限/块大小与契约同值
"""

import argparse
import contextlib
import importlib.util
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # <root>/ota/verify
DEFAULT_ROOT = os.path.dirname(os.path.dirname(HERE))        # <root>

RESULTS = []      # (ok, name, [evidence...])
WARNINGS = []
INFOS = []


def force_utf8_console():
    """把 stdout/stderr 固定成 UTF-8 + errors="replace"。

    本机默认控制台是 GBK(cp936)：一旦输出里出现 U+FFFD（例如解码子进程输出时用
    errors="replace" 产生的替换字符），print 会抛 UnicodeEncodeError 直接把脚本打崩。
    显式切 UTF-8 既能防崩溃，也让中文在 UTF-8 终端里正常显示。
    Python < 3.7 没有 TextIOWrapper.reconfigure，用 io.TextIOWrapper 兜底。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                buf = getattr(stream, "buffer", None)
                if buf is not None:
                    setattr(sys, name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace"))
        except Exception:                       # noqa: BLE001
            pass


def check(ok, name, evidence=None):
    RESULTS.append((bool(ok), name, list(evidence or [])))
    return bool(ok)


def warn(msg):
    WARNINGS.append(msg)


def info(msg):
    INFOS.append(msg)


# ----------------------------------------------------------------------------- 文本工具
def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def line_of(text, index):
    return text.count("\n", 0, max(index, 0)) + 1


def find_line(text, pattern):
    m = re.search(pattern, text)
    return line_of(text, m.start()) if m else 0


def ev(path, text, pattern, label=None):
    """构造 'file:line  该行内容' 证据。"""
    m = re.search(pattern, text)
    if not m:
        return ["%s: <未找到 %s>" % (path, label or pattern)]
    ln = line_of(text, m.start())
    return ["%s:%d  %s" % (path, ln, text.splitlines()[ln - 1].strip())]


def parse_defines(text):
    """把 C 头里的简单 #define 求值成 int（支持别名与四则运算）。"""
    raw = {}
    for m in re.finditer(r"^[ \t]*#define[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+([^\r\n/]+)", text, re.M):
        raw[m.group(1)] = m.group(2).strip()

    values = {}
    for _ in range(8):
        progress = False
        for name, expr in raw.items():
            e = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", lambda mm: (
                str(values[mm.group(1)]) if mm.group(1) in values else mm.group(0)), expr)
            e = re.sub(r"([0-9A-Fa-fxX]+)[uUlL]+\b", r"\1", e)
            e = e.replace("U", "").replace("L", "")
            if not re.fullmatch(r"[0-9A-Fa-fxX()\s+\-*/%<>|&^~]+", e):
                continue
            try:
                values[name] = int(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 (受控表达式)
                progress = True
            except Exception:
                continue
        if not progress:
            break
    return values


# ----------------------------------------------------------------------------- BL 自检
def load_bl_module(root):
    path = os.path.join(root, "ota", "bootloader", "verify_bl.py")
    if not os.path.isfile(path):
        return None, "缺少 ota/bootloader/verify_bl.py"
    try:
        spec = importlib.util.spec_from_file_location("verify_bl_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # 模块用自己的 __file__ 定位路径 -> 自动跟随副本
        return mod, ""
    except Exception as exc:                  # noqa: BLE001
        return None, "导入 verify_bl.py 失败: %r" % (exc,)


def run_bl_checks(mod):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main()
    return rc, buf.getvalue(), list(getattr(mod, "RESULTS", []))


# ----------------------------------------------------------------------------- C1
def check_c1_contract(root, v, common_h):
    ev_list = []
    ok = True

    def want(name, expect):
        nonlocal ok
        got = v.get(name)
        good = (got == expect)
        ok = ok and good
        ev_list.append("  ota_common.h: %s = %s（期望 %s）= %s"
                       % (name, got, expect, "通过" if good else "失败"))
        return good

    want("OTA_ISOTP_MAX_MSG", 1024)
    # 块大小必须按契约推导式 ((MAX - 3) & ~7) 求值，而不是写死 1021：
    # 2026-10-07 起块大小归一到 8 的倍数（1016），以支持断点续传（血线 5）。
    # 1021（mod 8 = 5）会让 8 个续传起点里有 7 个落在非 8 对齐地址上被设备回 NRC 0x31。
    expect_block = (1024 - 3) & ~7
    want("UDS_DL_MAX_BLOCK_PAYLOAD", expect_block)
    want("UDS_MAXLEN_FIELD", expect_block)
    want("UDS_XFEREXIT_WITH_CRC", 1)
    # 12 位首帧长度上限（ISO-TP FF 的长度字段只有 12 位）
    ok = ok and (v.get("OTA_ISOTP_MAX_MSG", 0) <= 4095)
    ev_list.append("  ISO-TP 12 位长度上限 4095 >= OTA_ISOTP_MAX_MSG = %s"
                   % (v.get("OTA_ISOTP_MAX_MSG", 0) <= 4095))
    # 块长与报文上限的关系自洽：块 + SID + blockSeq <= MAX
    blk = v.get("UDS_DL_MAX_BLOCK_PAYLOAD", 0)
    rel_ok = ((v.get("OTA_ISOTP_MAX_MSG", 0) - blk) >= 2)
    ok = ok and rel_ok
    ev_list.append("  块长推导关系 MAX - BLOCK >= 2(SID+seq) = %s（差 %d）"
                   % (rel_ok, v.get("OTA_ISOTP_MAX_MSG", 0) - blk))
    # 血线 5：块大小必须是 8 的倍数，否则断点续传不可用
    align_ok = (blk % 8 == 0)
    ok = ok and align_ok
    ev_list.append("  血线5：块大小 %d %% 8 == 0 = %s（续传地址 8 对齐前提）"
                   % (blk, align_ok))
    # 契约里必须有对应 #error 锁死
    has_align_err = bool(re.search(r'#error\s+"UDS_DL_MAX_BLOCK_PAYLOAD[^"]*8\s*字节倍数', common_h))
    ok = ok and has_align_err
    ev_list.append("  契约含「块大小必须 8 字节倍数」的 #error = %s" % has_align_err)
    # 契约里必须有 UDS_DL_REQ_LEN（0x34 请求体长度）常量与使用点
    has_req_len = bool(re.search(r"#define\s+UDS_DL_REQ_LEN\s+11U", common_h))
    ok = ok and has_req_len
    ev_list.append("  契约含 UDS_DL_REQ_LEN = 11U（0x34 请求体 34 00 44 + 4B + 4B）= %s"
                   % has_req_len)

    # ota_common.h 自己的断言必须在位
    p_if = r"#if\s+defined\(ISOTP_MAX_MSG\)\s*&&\s*\(ISOTP_MAX_MSG\s*!=\s*OTA_ISOTP_MAX_MSG\)"
    has_if = re.search(p_if, common_h) is not None
    has_err = bool(re.search(r'#error\s+"[^"]*ISOTP_MAX_MSG[^"]*不一致', common_h))
    ok = ok and has_if and has_err
    ev_list.append("  isotp.h 一致性断言（#if defined(ISOTP_MAX_MSG) && != OTA_ISOTP_MAX_MSG）= %s" % has_if)
    ev_list.append("  紧随其后的 #error = %s" % has_err)
    ev_list += ev("ota/common/ota_common.h", common_h, r"#define OTA_ISOTP_MAX_MSG\s+1024U")
    ev_list += ev("ota/common/ota_common.h", common_h, p_if)

    return check(ok, "[C1] 契约自洽（1024 / 1021 推导 + isotp.h 一致性 #error 在位）", ev_list)


# ----------------------------------------------------------------------------- C2
BENIGN_NUM_CTX = re.compile(r"(KB|kbit|Bank|RAM|IRAM|页)")
SUSPICIOUS_LIMIT_TOKEN = re.compile(
    r"(maxlen|max_len|max_msg|msg_max|maxmsg|blocklen|block_len|block_payload|blocksize|block_size|"
    r"maxnumberofblocklength)", re.I)


def scan_limit_literals(path, text, limit_values=(125, 128)):
    """返回 (failures, notes)：把 125/128 之类"报文上限字面量"从背景噪声里挑出来。
    规则：
      - 命中 'page < 128U' / '128 KB' / '各 128 页' 这类非报文语境 -> 记为白名单说明
      - 同一行（或前后 2 行）出现 maxLen/MAX_MSG/blockLen 等语义 -> FAIL
    """
    failures, notes = [], []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for val in limit_values:
            if not re.search(r"\b%d\b" % val, line):
                continue
            ctx = "\n".join(lines[max(0, i - 2):i + 3])
            if BENIGN_NUM_CTX.search(line):
                notes.append("%s:%d  [白名单:非报文语境] %s" % (path, i + 1, line.strip()))
                continue
            if SUSPICIOUS_LIMIT_TOKEN.search(ctx):
                failures.append("%s:%d  [疑似报文上限字面量] %s" % (path, i + 1, line.strip()))
            else:
                notes.append("%s:%d  [无明确语义,请人工确认] %s" % (path, i + 1, line.strip()))
    return failures, notes


def check_c2_three_way(root, v, bl_main_h, app_srcs):
    ev_list = []
    ok = True

    # --- BL：必须引用契约宏，不能写字面量
    m = re.search(r"#define\s+BL_ISOTP_MAX_MSG\s+([^\r\n/]+)", bl_main_h)
    bl_rhs = m.group(1).strip() if m else "<未定义>"
    bl_ok = bool(re.search(r"#define\s+BL_ISOTP_MAX_MSG\s+OTA_ISOTP_MAX_MSG\b", bl_main_h))
    guard_ok = bool(re.search(r"#if\s*\(\s*BL_ISOTP_MAX_MSG\s*!=\s*OTA_ISOTP_MAX_MSG\s*\)", bl_main_h))
    ok = ok and bl_ok and guard_ok
    ev_list.append("  bl_main.h: BL_ISOTP_MAX_MSG = %s（必须直接引用契约宏）= %s" % (bl_rhs, bl_ok))
    ev_list.append("  bl_main.h: `#if (BL_ISOTP_MAX_MSG != OTA_ISOTP_MAX_MSG)` 断言 = %s" % guard_ok)
    ev_list += ev("ota/bootloader/Core/Inc/bl_main.h", bl_main_h, r"#define BL_ISOTP_MAX_MSG")

    # --- APP：不得出现报文上限字面量
    all_fail = []
    for rel, text in app_srcs.items():
        fails, notes = scan_limit_literals(rel, text)
        all_fail += fails
        for n in notes:
            info(n)
    ok = ok and (not all_fail)
    ev_list.append("  APP 报文上限字面量扫描：%d 处可疑（125/128 仅允许非报文语境）" % len(all_fail))
    ev_list += all_fail

    # --- APP 必须真的用上契约宏
    agent = app_srcs.get("ota/app/ota_agent.c", "")
    uses = len(re.findall(r"\bUDS_MAXLEN_FIELD\b", agent))
    ok = ok and (uses >= 1)
    ev_list.append("  ota_agent.c 引用 UDS_MAXLEN_FIELD 次数 = %d（必须 >= 1）" % uses)
    ev_list += ev("ota/app/ota_agent.c", agent, r"UDS_MAXLEN_FIELD")

    return check(ok, "[C2] 三方数值一致（BL 引用契约宏 / APP 无 125/128 上限字面量）", ev_list)


# ----------------------------------------------------------------------------- C3
def check_c3_no_redundant_macro(root, app_srcs):
    ev_list = []
    ok = True
    hits = []
    for rel, text in app_srcs.items():
        for m in re.finditer(r"^[ \t]*#define[ \t]+(\w*(?:MSG_MAX|MAX_MSG|MAXLEN|MAX_LEN|BLOCK_LEN|BLOCK_SIZE)\w*)"
                             r"[ \t]+([^\r\n/]+)", text, re.M):
            ln = line_of(text, m.start())
            hits.append("%s:%d  #define %s %s" % (rel, ln, m.group(1), m.group(2).strip()))
    ok = ok and (not hits)
    ev_list.append("  APP 侧本地报文上限宏数量 = %d（必须为 0，统一走契约 UDS_MAXLEN_FIELD）" % len(hits))
    ev_list += (hits if hits else ["  （未发现 OTA_MSG_MAX / MAX_MSG 之类的本地定义）"])
    # 注释里明确"不再本地定义"也算佐证
    agent = app_srcs.get("ota/app/ota_agent.c", "")
    comment_ok = ("不再本地定义" in agent) or ("统一取契约" in agent)
    ev_list.append("  ota_agent.c 有'不再本地定义/统一取契约'说明 = %s" % comment_ok)
    ev_list += ev("ota/app/ota_agent.c", agent, r"不再本地定义")
    return check(ok, "[C3] APP 无冗余上限宏（只通过契约 UDS_MAXLEN_FIELD 取值）", ev_list)


# ----------------------------------------------------------------------------- C4
def check_c4_maxlen_field(root, v, agent):
    ev_list = []
    ok = True

    m = re.search(r"void\s+Ota_OnRequestDownload\s*\([^)]*\)\s*\{(.*?)\n\}", agent, re.S)
    body = m.group(1) if m else ""
    ev_list.append("  定位 Ota_OnRequestDownload 函数体 = %s（%d 字符）"
                   % (bool(m), len(body)))
    p_hi = r"resp\[2\]\s*=\s*\(uint8_t\)\(UDS_MAXLEN_FIELD\s*>>\s*8\)"
    p_lo = r"resp\[3\]\s*=\s*\(uint8_t\)\(UDS_MAXLEN_FIELD\s*&\s*0xFFU\)"
    hi_ok = re.search(p_hi, body) is not None
    lo_ok = re.search(p_lo, body) is not None
    literal = re.search(r"resp\[[23]\]\s*=\s*(0x[0-9A-Fa-f]+|\d+)\b", body) is not None
    ok = ok and hi_ok and lo_ok and (not literal)
    ev_list.append("  应答高字节取自 UDS_MAXLEN_FIELD = %s；低字节 = %s；出现字面量 = %s"
                   % (hi_ok, lo_ok, literal))
    if m:
        ev_list += ev("ota/app/ota_agent.c", agent, p_hi)
        ev_list += ev("ota/app/ota_agent.c", agent, p_lo)

    val = v.get("UDS_MAXLEN_FIELD", -1)
    be = "%02X %02X" % ((val >> 8) & 0xFF, val & 0xFF)
    # 期望值按推导式计算：1016 = 0x03F8（1021 = 0x03FD 是旧值）
    expect = (v.get("OTA_ISOTP_MAX_MSG", 1024) - 3) & ~7
    ok = ok and (val == expect)
    ev_list.append("  Python 独立计算：(OTA_ISOTP_MAX_MSG-3)&~7 = %d，大端编码 = 0x%s"
                   "（期望 0x%04X -> 74 20 %02X %02X）"
                   % (expect, be.replace(" ", ""), expect, (expect >> 8) & 0xFF, expect & 0xFF))
    # 文档/上位机里若写了同一串字节，作为交叉证据列出（不参与判定）
    for rel in ("ota/README.md", "ota/docs/03-OTA协议与刷写时序.md"):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            t = read_text(p)
            if re.search(r"03\s*F8", t):
                info("%s 中出现的 0x03F8 报文样本 -> %s:%d"
                     % (rel, rel, find_line(t, r"03\s*F8")))
    return check(ok, "[C4] 0x34 maxLen 字节级自证（UDS_MAXLEN_FIELD -> 0x03F8）", ev_list)


# ----------------------------------------------------------------------------- C5
def check_c5_bounds(root, agent_h, agent, tunnel, v):
    ev_list = []
    ok = True

    # 页缓冲尺寸必须等于 Flash 页（契约推导）
    pagev = v.get("OTA_FLASH_PAGE_SIZE", -1)
    m = re.search(r"#define\s+OTA_PAGE_BUF_SIZE\s+([^\r\n/]+)", agent_h)
    buf_rhs = m.group(1).strip() if m else "<未定义>"
    buf_ok = re.search(r"#define\s+OTA_PAGE_BUF_SIZE\s+OTA_FLASH_PAGE_SIZE\b", agent_h) is not None
    ok = ok and buf_ok
    ev_list.append("  ota_agent.h: OTA_PAGE_BUF_SIZE = %s（必须 = OTA_FLASH_PAGE_SIZE = %d）= %s"
                   % (buf_rhs, pagev, buf_ok))
    ev_list += ev("ota/app/ota_agent.h", agent_h, r"#define OTA_PAGE_BUF_SIZE")
    s_buf = re.search(r"static\s+uint8_t\s+s_pageBuf\s*\[\s*OTA_PAGE_BUF_SIZE\s*\]", agent) is not None
    ok = ok and s_buf
    ev_list.append("  s_pageBuf 用 OTA_PAGE_BUF_SIZE 定尺 = %s" % s_buf)

    # Ota_ReadDid 必须检查 respMax
    sig = re.search(r"Ota_ReadDid\s*\([^)]*respMax[^)]*\)", agent) is not None
    body = ""
    mm = re.search(r"uint16_t\s+Ota_ReadDid\s*\([^)]*\)\s*\{(.*?)\n\}", agent, re.S)
    if mm:
        body = mm.group(1)
    guard = re.search(r"respMax\s*[<>]=?", body) is not None
    ok = ok and sig and guard
    ev_list.append("  Ota_ReadDid 带 respMax 形参 = %s；函数体内用它做边界判断 = %s" % (sig, guard))
    ev_list += ev("ota/app/ota_agent.c", agent, r"respMax\s*<\s*\d+U")

    # 控制区写：白名单 + sizeof(OTA_Cfg)/OTA_CFG_PAGE_SIZE 边界
    cfg_bounds = re.search(r"sizeof\(OTA_Cfg\)\)\s*>\s*OTA_CFG_PAGE_SIZE", agent) is not None
    cfg_white = re.search(r"pageBase\s*!=\s*OTA_CFG_PRIMARY_ADDR", agent) is not None
    ok = ok and cfg_bounds and cfg_white
    ev_list.append("  Ota_CfgBlockWrite: 控制页白名单 = %s；sizeof(OTA_Cfg) 越页判断 = %s"
                   % (cfg_white, cfg_bounds))
    ev_list += ev("ota/app/ota_agent.c", agent, r"sizeof\(OTA_Cfg\)\)\s*>\s*OTA_CFG_PAGE_SIZE")

    # 页缓冲落盘长度必须封顶在缓冲尺寸内
    flush_ok = re.search(r"writeLen\s*=\s*final\s*\?", agent) is not None and \
        re.search(r":\s*OTA_PAGE_BUF_SIZE", agent) is not None
    ok = ok and flush_ok
    ev_list.append("  Ota_PageFlush: writeLen 封顶到 OTA_PAGE_BUF_SIZE（非收尾分支）= %s" % flush_ok)
    ev_list += ev("ota/app/ota_agent.c", agent, r"writeLen\s*=\s*final")

    # 隧道解析器：出参上限 + ID/长度校验
    t_outmax = re.search(r"n\s*>=\s*outMax", tunnel) is not None
    t_want = re.search(r"want\s*>\s*CAN_PAYLOAD_LEN", tunnel) is not None
    t_line = re.search(r"sizeof\(line\)", tunnel) is not None
    ok = ok and t_outmax and t_want and t_line
    ev_list.append("  ota_tunnel.c: ParseHexBytes 查 outMax = %s；tx 长度查 CAN_PAYLOAD_LEN = %s；"
                   "打印行缓冲用 sizeof(line) = %s" % (t_outmax, t_want, t_line))
    ev_list += ev("ota/app/ota_tunnel.c", tunnel, r"n\s*>=\s*outMax")

    return check(ok, "[C5] 越界保护存在性（respMax / sizeof(OTA_Cfg) / 页缓冲 / 隧道 outMax）", ev_list)


# ----------------------------------------------------------------------------- C6
DOC_CANDIDATE = re.compile(r"\b(125|128|1835|1048)\b|\b76\s*(?:s|秒)\b|\b164\s*(?:s|秒)\b")
DOC_BENIGN = re.compile(r"(KB|kbit|Bank|RAM|IRAM|页)")
DOC_WHITELIST = re.compile(r"(1021|1024|优化前|优化后|原工程|原为|旧版|AS_DELIVERED|OPTIMIZED|降到|降至|提升)")


def check_c6_docs(root):
    ev_list = []
    fails = []
    notes = []
    targets = [os.path.join(root, "ota", "README.md")]
    doc_dir = os.path.join(root, "ota", "docs")
    if os.path.isdir(doc_dir):
        targets += [os.path.join(doc_dir, f) for f in sorted(os.listdir(doc_dir)) if f.endswith(".md")]

    for path in targets:
        if not os.path.isfile(path):
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        text = read_text(path)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not DOC_CANDIDATE.search(line):
                continue
            window = "\n".join(lines[max(0, i - 6):i + 7])
            if DOC_BENIGN.search(line):
                notes.append("%s:%d  [白名单:KB/页/Bank 语境] %s" % (rel, i + 1, line.strip()[:110]))
            elif DOC_WHITELIST.search(window):
                notes.append("%s:%d  [白名单:优化前/后对比语境] %s" % (rel, i + 1, line.strip()[:110]))
            else:
                fails.append("%s:%d  [疑似与新契约(1024/1021)矛盾] %s" % (rel, i + 1, line.strip()[:130]))

    ok = not fails
    ev_list.append("  扫描 %d 个 Markdown：可疑残留 %d 处，白名单放行 %d 处" % (len(targets), len(fails), len(notes)))
    ev_list += fails
    ev_list.append("  —— 白名单样例（最多 8 条）——")
    ev_list += notes[:8]
    return check(ok, "[C6] 文档数字一致性（docs/*.md + README.md 无 125/1835/76s 残留）", ev_list)


# ----------------------------------------------------------------------------- C7
def check_c7_shared_constants(root, v, common_h, bl_main_h):
    """跨库共用常量必须同源：CRC 自检向量、槽首保留区及其对齐、校验应答长度、块序号回绕。"""
    ev_list = []
    ok = True

    crc_vec = "0x%08X" % v.get("OTA_CRC32_CHECK_VECTOR", 0)
    ok = ok and (v.get("OTA_CRC32_CHECK_VECTOR") == 0xCBF43926)
    ev_list.append("  契约 CRC32 自检向量 = %s（期望 0xCBF43926）" % crc_vec)

    # 槽首保留区：必须是 0x80（不是 32）—— 否则 APP 基址 = 槽基址 + 32 无法 0x80 对齐，
    # Cortex-M4 的 VTOR[6:0] 是 RES0，写入被忽略 -> 向量表落到镜像头上 -> lockup（docs/11）。
    hdr = v.get("OTA_IMG_HDR_SIZE")
    slot_a = v.get("OTA_SLOT_A_ADDR")
    slot_b = v.get("OTA_SLOT_B_ADDR")
    align_ok = (slot_a is not None) and (slot_b is not None) and (hdr is not None) and \
               ((slot_a + hdr) % 0x80 == 0) and ((slot_b + hdr) % 0x80 == 0)
    ok = ok and (hdr == 0x80) and align_ok
    ev_list.append("  契约槽首保留区 = %s 字节（期望 128）= %s"
                   % (hdr, "OK" if hdr == 0x80 else "FAIL"))
    ev_list.append("  APP 基址 0x%08X / 0x%08X 按 0x80 对齐 = %s（VTOR 硬性要求）"
                   % ((slot_a or 0) + (hdr or 0), (slot_b or 0) + (hdr or 0),
                      "OK" if align_ok else "FAIL"))

    # 应用层 isotp.h：优先"真实工程路径"，其次本工作区的冻结快照（后者只报 INFO，不参与判定）
    # 说明：本工作区没有上游 G474 工程，读不到就如实说明，绝不为了"全绿"伪造通过。
    candidates = [
        ("ota/app/isotp.h", os.path.join(root, "ota", "app", "isotp.h"), True),
        ("G474/Drivers/User/Inc/isotp.h",
         os.path.join(root, "G474", "Drivers", "User", "Inc", "isotp.h"), True),
        ("src_ref/G474__Drivers__User__Inc__isotp.h",
         os.path.join(root, "src_ref", "G474__Drivers__User__Inc__isotp.h"), False),
    ]
    found_authoritative = False
    for rel, path, authoritative in candidates:
        if not os.path.isfile(path):
            continue
        t = read_text(path)
        m = re.search(r"#define\s+ISOTP_MAX_MSG\s+(\d+)U?", t)
        app_max = int(m.group(1)) if m else None
        if authoritative:
            found_authoritative = True
            same = (app_max == v.get("OTA_ISOTP_MAX_MSG"))
            ok = ok and same
            ev_list.append("  %s: ISOTP_MAX_MSG = %s（契约 OTA_ISOTP_MAX_MSG = %s）= %s"
                           % (rel, app_max, v.get("OTA_ISOTP_MAX_MSG"), same))
            if not same:
                ev_list.append("     -> 与契约不一致：链接期 #error 会拦住，静态检查也必须报 FAIL")
        else:
            info("%s（上游改动前的冻结快照，非本次改动对象）：ISOTP_MAX_MSG = %s，契约 = %s；"
                 "不同属预期 —— 上线前必须在上游 G474 工程把它改为契约值（见 docs/02 §B.7 第 1 行）"
                 % (rel, app_max, v.get("OTA_ISOTP_MAX_MSG")))
    if not found_authoritative:
        info("未找到应用层 isotp.h（ota/app/isotp.h 或 G474/Drivers/User/Inc/isotp.h）："
             "本工作区不含上游 G474 工程，故 ISOTP_MAX_MSG 的最强一致性核对被跳过 —— 不伪造通过")

    bl_ok = bool(re.search(r"BL_ISOTP_MAX_MSG\s+OTA_ISOTP_MAX_MSG\b", bl_main_h))
    ok = ok and bl_ok
    ev_list.append("  bl_main.h 同源引用 = %s" % bl_ok)
    return check(ok, "[C7] 跨库共用常量同源（CRC 向量 / 0x80 槽首保留区及对齐 / isotp.h 上限）", ev_list)


# ----------------------------------------------------------------------------- C8
def host_const_line(text, name):
    """定位 `NAME = <expr>   # 可选注释`，返回 (行号, 该行原文, RHS 表达式已去注释)。"""
    pat = r"(?m)^[ \t]*%s[ \t]*=[ \t]*([^#\r\n]*)" % re.escape(name)
    m = re.search(pat, text)
    if not m:
        return 0, "", None
    ln = line_of(text, m.start())
    return ln, text.splitlines()[ln - 1].strip(), m.group(1).strip()


def eval_host_expr(expr, known):
    """把已知常量名代入后求值；只放行数字与 + - * / % & | ~ ^ << >> ( ) 与已知常量名。

    2026-10-07：块大小推导式改为 `((OTA_ISOTP_MAX_MSG - 3) & ~7)`（向下对齐到 8 字节倍数，
    以支持断点续传），原求值器的字符白名单 `[0-9+\\-*/%() ]` 不含位运算符会直接返回 None，
    导致 C8 误报失败。故放开 `& | ~ ^ << >>`，仍然不允许任何名字/属性/调用（无builtins）。
    """
    if expr is None:
        return None
    e = expr
    for k, val in known.items():
        e = re.sub(r"\b%s\b" % re.escape(k), str(val), e)
    # 只放行数字、运算符与括号；不允许标识点、调用、字符串、下标
    if not re.fullmatch(r"[0-9+\-*/%&|~^()<>\s]+", e):
        return None
    try:
        return int(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 (受控表达式)
    except Exception:                                  # noqa: BLE001
        return None


def check_c8_host_tool(root, v, host_text):
    ev_list = []
    if not host_text:
        return check(False, "[C8] 上位机 ota_host.py 的应用报文上限/块大小与契约同值",
                     ["  ota/tools/ota_host.py 缺失 -> 无法核对（不伪造通过）"])
    ok = True
    exp_max = v.get("OTA_ISOTP_MAX_MSG")
    exp_block = v.get("UDS_DL_MAX_BLOCK_PAYLOAD")

    ln1, raw1, rhs1 = host_const_line(host_text, "ISOTP_MAX_MSG")
    val1 = eval_host_expr(rhs1, {})
    if ln1 == 0:
        ok = False
        ev_list.append("  ISOTP_MAX_MSG: <确实未找到定义> -> FAIL")
    else:
        same = (val1 == exp_max)
        ok = ok and same
        ev_list.append("  ota/tools/ota_host.py:%d  %s   [解析值=%s 契约=%s 一致=%s]"
                       % (ln1, raw1, val1, exp_max, same))

    known = {"ISOTP_MAX_MSG": val1 if val1 is not None else 0}
    ln2, raw2, rhs2 = host_const_line(host_text, "ISOTP_DEFAULT_BLOCK_SIZE")
    val2 = eval_host_expr(rhs2, known)
    if ln2 == 0:
        ok = False
        ev_list.append("  ISOTP_DEFAULT_BLOCK_SIZE: <确实未找到定义> -> FAIL")
    else:
        same = (val2 == exp_block)
        ok = ok and same
        ev_list.append("  ota/tools/ota_host.py:%d  %s   [表达式=%r 求值=%s 契约块长=%s 一致=%s]"
                       % (ln2, raw2, rhs2, val2, exp_block, same))
        known["ISOTP_DEFAULT_BLOCK_SIZE"] = val2 if val2 is not None else 0

    # ISOTP_MAX_BLOCK_SIZE 若存在也必须等于契约块长（防止"又冒出一个自造上限"）
    ln3, raw3, rhs3 = host_const_line(host_text, "ISOTP_MAX_BLOCK_SIZE")
    if ln3 != 0:
        val3 = eval_host_expr(rhs3, known)
        same3 = (val3 == exp_block)
        ok = ok and same3
        ev_list.append("  ota/tools/ota_host.py:%d  %s   [求值=%s 契约块长=%s 一致=%s]"
                       % (ln3, raw3, val3, exp_block, same3))

    ev_list.append("  说明：常量行允许行尾 # 注释，右侧允许由其它常量推导的表达式；"
                   "比对的是**数值求值结果**而非文本")
    return check(ok, "[C8] 上位机 ota_host.py 的应用报文上限/块大小与契约同值", ev_list)


def _hex_nonff_addrs(path):
    """Intel HEX 里所有非 0xFF 数据的绝对地址集合（处理 02/04 扩展地址记录）。"""
    addrs = set()
    base = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            try:
                rec = bytes.fromhex(line[1:])
            except ValueError:
                continue
            n, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
            data = rec[4:4 + n]
            if typ == 0x00:
                for i, val in enumerate(data):
                    if val != 0xFF:
                        addrs.add(base + addr + i)
            elif typ == 0x02 and len(data) >= 2:
                base = ((data[0] << 8) | data[1]) << 4
            elif typ == 0x04 and len(data) >= 2:
                base = ((data[0] << 8) | data[1]) << 16
    return addrs


def check_c9_bl_layout(root):
    """[C9] BL 链接布局：页 0 只放向量表 + Keil 必须真的在用 bl.sct（+ 顺带审计产物）

    为什么单列一条：`ota_common.h` 的编译期断言**只看宏，看不到链接结果**。
    2026-10-05 的事故是两个开关同时错：
      ① `bl.sct` 的 ER_IROM1 写成 `0x08000000 + 0x0000B800` -> 代码紧挨向量表铺进页 0；
      ② `BL.uvprojx` 的 `<umfTarg>` 是 1（"使用 Target 对话框内存布局"）
         -> µVision **无视 bl.sct**，按对话框的 IROM 重新生成。
    而 `bl_param.c` 写控制区要**整页擦除页 0**，于是第一次写控制区就擦掉了 BL 自己的
    复位向量代码：实测复位向量 = 0x08000325，而 0x08000324 处读回 16 字节全为 0xFF。
    本检查把这三件事（scatter 文本、umfTarg 开关、产物 hex）都钉住。
    """
    ev_list = []
    ok = True

    # --- 载入布局单一真相源 ---
    mod = None
    lay_path = os.path.join(root, "ota", "scripts", "bl_layout.py")
    if not os.path.isfile(lay_path):
        return check(False, "[C9] BL 链接布局（页 0 只放向量表 / 用 bl.sct / 产物审计）",
                     ["  缺少 ota/scripts/bl_layout.py -> 无法判定（不伪造通过）"])
    try:
        spec = importlib.util.spec_from_file_location("bl_layout_under_test", lay_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:                                  # noqa: BLE001
        return check(False, "[C9] BL 链接布局（页 0 只放向量表 / 用 bl.sct / 产物审计）",
                     ["  载入 ota/scripts/bl_layout.py 失败: %r" % (exc,)])

    # --- ① bl.sct 文本 ---
    sct_path = os.path.join(root, "ota", "bootloader", "MDK-ARM", "bl.sct")
    if not os.path.isfile(sct_path):
        ok = False
        ev_list.append("  [!!] 缺少 %s" % os.path.relpath(sct_path, root))
    else:
        sct = read_text(sct_path)
        problems = mod.check_scatter_text(sct)
        if problems:
            ok = False
            for p in problems:
                ev_list.append("  [!!] bl.sct: %s" % p)
        else:
            ev_list.append("  bl.sct 页 0 只放向量表（472B），代码从 0x08000800 起 -> OK")

    # --- ② BL.uvprojx 必须显式关闭"使用 Target 对话框内存布局" ---
    proj_path = os.path.join(root, "ota", "bootloader", "MDK-ARM", "BL.uvprojx")
    if not os.path.isfile(proj_path):
        ok = False
        ev_list.append("  [!!] 缺少 %s" % os.path.relpath(proj_path, root))
    else:
        proj = read_text(proj_path)
        m_umf = re.search(r"<umfTarg>\s*(\d)\s*</umfTarg>", proj)
        m_sct = re.search(r"<ScatterFile>\s*([^<]*?)\s*</ScatterFile>", proj)
        umf_ok = bool(m_umf and m_umf.group(1) == "0")
        ok = ok and umf_ok
        ev_list.append("  BL.uvprojx <umfTarg>=%s（必须 0，否则 bl.sct 被忽略）%s"
                       % (m_umf.group(1) if m_umf else "<缺失>", "  -> OK" if umf_ok else "  -> FAIL"))
        ev_list.append("  BL.uvprojx <ScatterFile>=%s"
                       % (m_sct.group(1) if m_sct else "<缺失>"))
        if not umf_ok:
            ev_list.append("     修法: python ota/scripts/fix_bl_region.py --apply")

    # --- ③ 有产物就审计产物（最能说明问题的一步） ---
    hex_path = os.path.join(root, "firmware", "BL.hex")
    if not os.path.isfile(hex_path):
        ev_list.append("  firmware/BL.hex 不存在 -> 跳过产物审计（仍可用 audit_bl_layout.py 单独跑）")
    else:
        addrs = _hex_nonff_addrs(hex_path)
        if not addrs:
            ok = False
            ev_list.append("  [!!] firmware/BL.hex 解析不到任何数据记录")
        else:
            span = (min(addrs), max(addrs) + 1)
            ev_list.append("  firmware/BL.hex 覆盖 0x%08X ~ 0x%08X" % span)
            for lo_off, hi_off, name, must_blank in mod.RESERVED_IN_PAGE0:
                lo, hi = mod.PAGE0_ADDR + lo_off, mod.PAGE0_ADDR + hi_off
                hit = sorted(a for a in addrs if lo <= a < hi)
                if must_blank:
                    if hit:
                        ok = False
                        ev_list.append("  [!!] 保留区「%s」@0x%08X 被镜像占用 %d 字节（首个 0x%08X）"
                                       % (name, lo, len(hit), hit[0]))
                    else:
                        ev_list.append("  保留区「%s」@0x%08X 干净 -> OK" % (name, lo))
                else:
                    # 向量表：必须存在，否则芯片复位取不到 SP/复位向量
                    want = hi - lo
                    if len(hit) != want:
                        ok = False
                        ev_list.append("  [!!] 向量表 @0x%08X 应占 %d 字节，实际 %d 字节"
                                       % (lo, want, len(hit)))
                    else:
                        ev_list.append("  向量表 @0x%08X 占满 %d 字节 -> OK" % (lo, want))
            if span[1] > mod.BL_CODE_ADDR + mod.BL_CODE_SIZE:
                ok = False
                ev_list.append("  [!!] 镜像结束 0x%08X 越过 BL 代码区上界 0x%08X"
                               % (span[1], mod.BL_CODE_ADDR + mod.BL_CODE_SIZE))

    ev_list.append("  说明：产物改动后请重跑；上板前另跑 python ota/scripts/audit_bl_layout.py --on-chip 看芯片实况")
    return check(ok, "[C9] BL 链接布局（页 0 只放向量表 / 用 bl.sct / 产物审计）", ev_list)


# ---------------------------------------------------------------------------- C10
def check_c10_uds_lengths(root, v, bl_diag, agent, host_text):
    """设备端 UDS 请求长度守卫（2026-10-07 新增，血线 6/7）。

    为什么必须有这一项：2026-10-07 全项目审查发现 **BL 侧 0x34 长度校验写成 10
    而实际报文是 11 字节**（代码自己都在读 req[10]），真机上 0x34 恒回 NRC 0x13，
    一块固件都刷不进去；而 APP 侧与上位机都是 11，且单元测试全跑在模拟器上
    （模拟器也是 11），所以 29 项守卫与 111 条单测**全部测不到**。

    教训：常量一致性 ≠ 长度一致性。本项直接核对"设备端长度校验"与"契约常量"，
    并交叉核对三方（BL / APP / 上位机）是否同值。
    """
    ev_list = []
    ok = True
    req_len = v.get("UDS_DL_REQ_LEN")

    # --- 1) 契约必须定义 UDS_DL_REQ_LEN = 11 ---
    ev_list.append("  契约 UDS_DL_REQ_LEN = %s（0x34: 34 00 44 + addr4 + size4）" % req_len)
    if req_len != 11:
        ok = False
        ev_list.append("  [!!] UDS_DL_REQ_LEN 应为 11")

    # --- 2) BL 与 APP 的 0x34 长度校验都必须引用该常量，且不得写裸数字 ---
    for label, src, fn in (("BL  ota/bootloader/Core/Src/bl_diag.c", bl_diag, "BL_DiagService34"),
                           ("APP ota/app/ota_agent.c", agent, "Ota_OnRequestDownload")):
        if not src:
            ev_list.append("  [!!] %s 源码缺失，无法核对" % label)
            ok = False
            continue
        m = re.search(r"%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(fn), src, re.S)
        body = m.group(1) if m else ""
        if not body:
            ev_list.append("  [!!] %s 未定位到 %s() 函数体" % (label, fn))
            ok = False
            continue
        uses_macro = re.search(r"len\s*!=\s*UDS_DL_REQ_LEN", body) is not None
        # 抓 "len != <数字>" 形式的裸字面量（B1 原始缺陷形态）
        uses_literal = re.search(r"len\s*!=\s*\d+[uU]?\b", body) is not None
        good = uses_macro and not uses_literal
        ok = ok and good
        ev_list.append("  %s 的 %s：引用 UDS_DL_REQ_LEN = %s；出现裸字面量 = %s -> %s"
                       % (label, fn, uses_macro, uses_literal, "OK" if good else "[!!] FAIL"))
        ev_list += ev("%s: %s" % (label, fn), body, r"len\s*!=\s*UDS_DL_REQ_LEN")

    # --- 3) 三方 0x34 长度必须同值（交叉核对）---
    #     BL/APP 已由 (2) 保证；这里核对上位机构造的长度也是 11
    if host_text:
        m = re.search(r"def\s+uds_request_download\s*\(.*?\)\s*->\s*bytes:(.*?)(?=\ndef\s)", host_text, re.S)
        body = m.group(1) if m else ""
        # 构造：bytes([0x34, fmt, alfid]) + pack(">I") + pack(">I") -> 3 + 4 + 4 = 11
        n_sid = len(re.findall(r"bytes\(\[\s*0x34", body))
        n_pack = len(re.findall(r'struct\.pack\(\s*">I"', body))
        good = (n_sid == 1 and n_pack == 2)
        ok = ok and good
        ev_list.append("  上位机 uds_request_download：SID 头 %d 处(应 1) + 大端字段 %d 个(应 2)"
                       " -> 长度 %d = %s"
                       % (n_sid, n_pack, 3 + 4 * n_pack if n_sid else -1,
                          "OK" if good else "[!!] FAIL"))
    else:
        ev_list.append("  [!!] 上位机源码缺失，无法交叉核对 0x34 长度")
        ok = False

    # --- 4) BL 的 FF00 长度校验必须接受 4/6 两档（B2 修复的回归守卫）---
    if bl_diag:
        m = re.search(r"BL_DiagRoutineErase\s*\([^)]*\)\s*\{(.*?)\n\}", bl_diag, re.S)
        body = m.group(1) if m else ""
        has_min = "UDS_RID_ERASE_REQ_LEN_MIN" in body
        has_max = "UDS_RID_ERASE_REQ_LEN_MAX" in body
        literal_only = re.search(r"if\s*\(\s*len\s*!=\s*6U\s*\)", body) is not None
        good = has_min and has_max and (not literal_only)
        ok = ok and good
        ev_list.append("  BL FF00 长度校验：引用 MIN/MAX 契约常量 = %s/%s；仅接受 6U 硬编码 = %s -> %s"
                       % (has_min, has_max, literal_only, "OK" if good else "[!!] FAIL"))

    # --- 5) 上位机不得再单独发 FF00（B2：0x34 已隐式擦除，重复擦除吃超时预算）---
    if host_text:
        # 只看 OtaFlasher.run 的真实刷写流程：routine_control(UDS_ROUTINE_ERASE) 应为 0 次
        n_ff00 = len(re.findall(r"routine_control\(\s*UDS_ROUTINE_ERASE\s*\)", host_text))
        good = (n_ff00 == 0)
        ok = ok and good
        ev_list.append("  上位机刷写流程中 routine_control(UDS_ROUTINE_ERASE) 调用 %d 次"
                       "（期望 0，擦除由 0x34 隐式完成）-> %s" % (n_ff00, "OK" if good else "[!!] FAIL"))

    return check(ok, "[C10] 设备端 UDS 请求长度与三方一致性（血线 6：长度写错一个字节即全流程失败）",
                 ev_list)


# ---------------------------------------------------------------------------- C11
def check_c11_handover_and_fc(root, v, bl_jump, bl_param, host_text):
    """BL->APP 交接与流控节拍守卫（2026-10-07 新增，覆盖 H1/H2/H3/H4）。

    为什么必须有这一项：这四项都属于"代码看起来完全合理、只有在真机特定时序下才
    失效"的类型，单元测试与既有29/30 项守卫全部测不到：

    H1（bl_jump.c）：跳转前只清了 NVIC->ICPR，**没清 SCB->ICSR**。
        Cortex-M4 的系统异常（SysTick/PendSV）挂起位只能通过 SCB->ICSR 清，
        NVIC 对其完全无效。本工程 SysTick 1ms 周期，改VTOR 后残留 pending 会让
        APP 在初始化完成前按自己的向量表取向量 -> 随机 HardFault，约千分之一概率。
    H2/H3（bl_param.c）：BL_ParamSelectTarget 有两条绕过血线 1 的路径
        （H3 全部副本无效时无条件写 PRIMARY 页；H2 置 0 时恒写副本0 致双副本退化）。
    H4（ota_host.py）：_send_remaining 只读 FC 的 BS，**完全忽略 STmin**，
        146 帧背靠背连发，而BL 的 FDCAN RX FIFO0仅 3 深度 -> 丢帧 -> 刷写卡死。

    教训：这类缺陷的共性是"缺少某个必须存在的动作/字段"，而不是"某个值写错了"。
    常量一致性守卫天然抓不到这类问题，必须显式断言"该动作存在"。
    """
    ev_list = []
    ok = True

    # --- 1) H1：bl_jump.c 必须清 SCB->ICSR 的系统异常挂起位 ---
    if not bl_jump:
        ev_list.append("  [!!] bl_jump.c 源码缺失，无法核对 H1")
        ok = False
    else:
        m = re.search(r"void\s+BL_JumpToApp\s*\([^)]*\)\s*\{(.*?)\n\}", bl_jump, re.S)
        body = m.group(1) if m else ""
        if not body:
            ev_list.append("  [!!] 未定位到 BL_JumpToApp() 函数体")
            ok = False
        else:
            # 关键：必须匹配**赋值语句本身**，不能用裸子串 in body ——
                # 否则注释里出现的 "PENDSTCLR"/"PENDSVCLR" 字样会让"只清一半"的
                # 形态蒙混过关（反例 19 就是这么漏网的）。
                ma = re.search(r"SCB\s*->\s*ICSR\s*=\s*([^;]+);", body)
                rhs = ma.group(1) if ma else ""
                has_icsr = bool(ma)
                # 逐个符号在**右值**里出现才算数。注意不要用 \b 词边界：符号名带下划线后缀
                # （PENDSTCLR_Msk），\b 会因下划线属 \w 而产生错误边界导致失配。
                has_st = re.search(r"PENDSTCLR", rhs) is not None
                has_psv = re.search(r"PENDSVCLR", rhs) is not None
                # 误用置位符号（PENDSTSET/PENDSVSET）也算错：ICSR 是"写 1 清除"语义
                uses_set = re.search(r"PEND\w*SET", rhs) is not None
                good = has_icsr and has_st and has_psv and (not uses_set)
                ok = ok and good
                ev_list.append("  BL_JumpToApp 写 SCB->ICSR = %s；右值含 PENDSTCLR = %s；"
                               "含 PENDSVCLR = %s；误用 SET(置位)语义 = %s -> %s"
                               % (has_icsr, has_st, has_psv, uses_set,
                                  "OK" if good else "[!!] FAIL"))
                ev_list.append("    （H1：系统异常挂起位 NVIC 清不到，必须走 SCB->ICSR；"
                               "缺失则 APP 偶发随机 HardFault，约千分之一概率）")
                ev_list.append("    实测右值：%s" % (rhs.strip() if rhs else "<无>"))
                ev_list += ev("bl_jump.c: BL_JumpToApp", body,
                              r"SCB\s*->\s*ICSR\s*=\s*[^;]*PENDSTCLR[^;]*PENDSVCLR")
                if not has_icsr:
                    ev_list.append("    [!!] 只清 NVIC->ICPR 不够 —— 系统异常挂起位必须经 SCB->ICSR")
                elif not (has_st and has_psv):
                    ev_list.append("    [!!] 须同时清 SysTick 与 PendSV：只清一半仍会残留挂起")

    # --- 2) H3：全部副本无效的回退路径必须受 BL_PARAM_PRIMARY_WRITE 约束 ---
    # --- 3) H2：同页分支必须做两副本轮转（不能恒写副本0）---
    if not bl_param:
        ev_list.append("  [!!] bl_param.c 源码缺失，无法核对 H2/H3")
        ok = False
    else:
        m = re.search(r"static\s+void\s+BL_ParamSelectTarget\s*\([^)]*\)\s*\{(.*?)\n\}",
                      bl_param, re.S)
        body = m.group(1) if m else ""
        if not body:
            ev_list.append("  [!!] 未定位到 BL_ParamSelectTarget() 函数体")
            ok = False
        else:
            # H3：oldestIdx < 0 分支内的 page 赋值必须受 #if 条件编译保护。
            # 原始缺陷形态：回退分支里**裸写** PRIMARY_ADDR，完全无视该宏。
            m3 = re.search(r"if\s*\(\s*oldestIdx\s*<\s*0\s*\)\s*\{(.*?)\n\s*\}", body, re.S)
            fb = m3.group(1) if m3 else ""
            # 正确形态：#if (BL_PARAM_PRIMARY_WRITE == 0) -> MIRROR  /  #else -> PRIMARY / #endif
            m3g = re.search(r"#if\s*\(\s*BL_PARAM_PRIMARY_WRITE\s*==\s*0\s*\)(.*?)#else(.*?)#endif",
                            fb, re.S) if fb else None
            has_guard = m3g is not None
            guarded_zero_is_mirror = bool(m3g) and ("OTA_CFG_MIRROR_ADDR" in m3g.group(1))
            guarded_else_is_primary = bool(m3g) and ("OTA_CFG_PRIMARY_ADDR" in m3g.group(2))
            good3 = bool(fb) and has_guard and guarded_zero_is_mirror and guarded_else_is_primary
            ok = ok and good3
            ev_list.append("  H3 回退分支（全部副本无效）：#if 条件保护 = %s；"
                           "置0 分支写 MIRROR = %s；#else 分支写 PRIMARY = %s -> %s"
                           % (has_guard, guarded_zero_is_mirror, guarded_else_is_primary,
                              "OK" if good3 else "[!!] FAIL"))
            ev_list += ev("bl_param.c: 全部副本无效回退分支", fb,
                          r"#if\s*\(?\s*BL_PARAM_PRIMARY_WRITE\s*==\s*0[\s\S]*?OTA_CFG_MIRROR_ADDR")
            if fb and not has_guard:
                ev_list.append("    [!!] 回退分支裸写 PRIMARY_ADDR —— 违反 BL_PARAM_PRIMARY_WRITE 契约，"
                               "首次上电（Flash 全空必走此路径）就会擦 PRIMARY 页，正是该宏最需生效的时刻")

            # H2：同页分支的 *off 赋值必须按 s_candOff[newestIdx] 轮转
            m2 = re.search(r"==\s*s_candPage\[newestIdx\]\s*\)\s*\{(.*?)\n\s*\n", body, re.S)
            same = m2.group(1) if m2 else ""
            rotates = ("s_candOff[newestIdx]" in same) and ("OTA_CFG_SLOT_MIRROR_OFF" in same)
            # 原始缺陷形态：同页分支恒 *off = OTA_CFG_SLOT_OFFSET（副本1 永不写）。
            # 用正则而非字面量比对，避免缩进/空格差异绕过检测。
            fixed_off = re.search(r"\*\s*off\s*=\s*OTA_CFG_SLOT_OFFSET\s*;", same) is not None
            good2 = bool(same) and rotates and (not fixed_off)
            ok = ok and good2
            ev_list.append("  H2 同页分支：按 s_candOff[newestIdx] 轮转 = %s；恒写副本0 = %s -> %s"
                           % (rotates, fixed_off, "OK" if good2 else "[!!] FAIL"))
            ev_list += ev("bl_param.c: 同页分支副本轮转", same,
                          r"s_candOff\[newestIdx\][^;]*OTA_CFG_SLOT_MIRROR_OFF")

    # --- 4) H4：上位机必须读取并遵守 FC 的 STmin ---
    if not host_text:
        ev_list.append("  [!!] 上位机源码缺失，无法核对 H4")
        ok = False
    else:
        m = re.search(r"def\s+_send_remaining\s*\(.*?\n(?=\s*def\s)", host_text, re.S)
        body = m.group(0) if m else ""
        if not body:
            ev_list.append("  [!!] 未定位到 IsoTpClient._send_remaining()")
            ok = False
        else:
            reads_stmin = re.search(r"fr\s*\[\s*2\s*\]", body) is not None
            uses_sleep = re.search(r"self\.sleep_fn\s*\(", body) is not None
            # 同H1：必须是真实调用，不能是注释里提到函数名就算过
            decodes = re.search(r"=\s*isotp_stmin_to_seconds\s*\(", body) is not None
            # 延时必须发生在"发帧之前"：sleep_fn 调用应出现在 tx_frame 之前
            m_sleep = re.search(r"self\.sleep_fn\s*\(", body)
            m_tx = re.search(r"self\.tunnel\.tx_frame\s*\(", body)
            order_ok = bool(m_sleep and m_tx and m_sleep.start() < m_tx.start())
            good4 = reads_stmin and uses_sleep and decodes and order_ok
            ok = ok and good4
            ev_list.append("  _send_remaining 读取 FC[2](STmin) = %s；调用 sleep_fn 延时 = %s；"
                           "真实调用 isotp_stmin_to_seconds 解码 = %s；延时在发帧之前 = %s -> %s"
                           % (reads_stmin, uses_sleep, decodes, order_ok,
                              "OK" if good4 else "[!!] FAIL"))
            ev_list += ev("ota_host.py: _send_remaining STmin", body, r"isotp_stmin_to_seconds")
        # 解码函数本身必须存在（否则调用处会NameError）
        has_decoder = re.search(r"def\s+isotp_stmin_to_seconds\s*\(", host_text) is not None
        ok = ok and has_decoder
        ev_list.append("  isotp_stmin_to_seconds()解码函数存在 = %s -> %s"
                       % (has_decoder, "OK" if has_decoder else "[!!] FAIL"))

    return check(ok, "[C11] BL->APP 交接与流控节拍（缺一个动作即随机失效：H1 清系统异常 / H2H3 控制区轮转 / H4 STmin）",
                 ev_list)


# ----------------------------------------------------------------------------- main
def main():
    force_utf8_console()
    ap = argparse.ArgumentParser(description="OTA 三方静态自检聚合入口（纯 Python）")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="仓库根目录（默认：本脚本的上两级）")
    ap.add_argument("--skip-bl", action="store_true", help="只跑跨库检查，跳过 verify_bl.py")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    common_path = os.path.join(root, "ota", "common", "ota_common.h")
    bl_main_path = os.path.join(root, "ota", "bootloader", "Core", "Inc", "bl_main.h")
    agent_path = os.path.join(root, "ota", "app", "ota_agent.c")
    agent_h_path = os.path.join(root, "ota", "app", "ota_agent.h")
    tunnel_path = os.path.join(root, "ota", "app", "ota_tunnel.c")
    host_path = os.path.join(root, "ota", "tools", "ota_host.py")
    bl_diag_path = os.path.join(root, "ota", "bootloader", "Core", "Src", "bl_diag.c")
    bl_jump_path = os.path.join(root, "ota", "bootloader", "Core", "Src", "bl_jump.c")
    bl_param_path = os.path.join(root, "ota", "bootloader", "Core", "Src", "bl_param.c")

    common_h = read_text(common_path) if os.path.isfile(common_path) else ""
    bl_main_h = read_text(bl_main_path) if os.path.isfile(bl_main_path) else ""
    agent = read_text(agent_path) if os.path.isfile(agent_path) else ""
    agent_h = read_text(agent_h_path) if os.path.isfile(agent_h_path) else ""
    tunnel = read_text(tunnel_path) if os.path.isfile(tunnel_path) else ""
    host_text = read_text(host_path) if os.path.isfile(host_path) else ""
    bl_diag = read_text(bl_diag_path) if os.path.isfile(bl_diag_path) else ""
    bl_jump = read_text(bl_jump_path) if os.path.isfile(bl_jump_path) else ""
    bl_param = read_text(bl_param_path) if os.path.isfile(bl_param_path) else ""

    bl_out, bl_results, bl_rc = "", [], 0
    if not args.skip_bl:
        mod, err = load_bl_module(root)
        if mod is None:
            check(False, "[BL] ota/bootloader/verify_bl.py 可加载并可运行", [err])
        else:
            bl_rc, bl_out, bl_results = run_bl_checks(mod)
            check(bl_rc == 0, "[BL] verify_bl.py 全绿（%d 项，见上方原始输出）" % len(bl_results),
                  ["BL 自检退出码 = %d，检查项 = %d，失败 = %d"
                   % (bl_rc, len(bl_results), sum(1 for ok, _, _ in bl_results if not ok))])

    v = parse_defines(common_h)
    if not v:
        check(False, "[C0] 能解析 ota/common/ota_common.h", ["未解析到任何 #define"])

    # 受保护目录缺失时给出明确 FAIL（而不是静默跳过）
    for label, p in (("ota/common/ota_common.h", common_path), ("ota/app/ota_agent.c", agent_path),
                     ("ota/app/ota_tunnel.c", tunnel_path),
                     ("ota/bootloader/Core/Inc/bl_main.h", bl_main_path)):
        check(os.path.isfile(p), "[C0] 被检查文件存在：%s" % label,
              ["%s = %s" % (p, "存在" if os.path.isfile(p) else "缺失")])

    app_srcs = {}
    for rel, p in (("ota/app/ota_agent.c", agent_path), ("ota/app/ota_tunnel.c", tunnel_path)):
        if os.path.isfile(p):
            app_srcs[rel] = read_text(p)

    if v:
        check_c1_contract(root, v, common_h)
        check_c2_three_way(root, v, bl_main_h, app_srcs)
        check_c3_no_redundant_macro(root, app_srcs)
        check_c4_maxlen_field(root, v, agent)
        check_c5_bounds(root, agent_h, agent, tunnel, v)
        check_c6_docs(root)
        check_c7_shared_constants(root, v, common_h, bl_main_h)
        check_c8_host_tool(root, v, host_text)
        check_c10_uds_lengths(root, v, bl_diag, agent, host_text)
        check_c11_handover_and_fc(root, v, bl_jump, bl_param, host_text)

    # C9 只依赖文件系统与 bl_layout.py，不依赖契约解析结果，因此放在 if v 之外
    check_c9_bl_layout(root)

    # ------------------------------------------------------------------ 输出
    print("=" * 78)
    print("OTA 三方静态自检 verify_all.py")
    print("仓库根: %s" % root)
    print("契约头: %s" % common_path)
    print("=" * 78)
    if bl_out:
        print(bl_out.rstrip())
        print("-" * 78)
    for ok, name, evidence in RESULTS:
        print("[%s] %s" % ("OK " if ok else "FAIL", name))
        for line in evidence:
            print("        " + line)
    if INFOS:
        print("-" * 78)
        for line in INFOS:
            print("[INFO] " + line)
    if WARNINGS:
        print("-" * 78)
        for line in WARNINGS:
            print("[WARN] " + line)

    bl_pass = sum(1 for ok, _, _ in bl_results if ok)
    cross = [r for r in RESULTS]
    cross_pass = sum(1 for ok, _, _ in cross if ok)
    total = len(bl_results) + len(cross)
    passed = bl_pass + cross_pass
    failed = total - passed
    print("=" * 78)
    print("汇总: BL %d 项 + 跨库 %d 项 = %d 项检查, %d 通过, %d 失败 -> %s"
          % (len(bl_results), len(cross), total, passed, failed, "全绿" if failed == 0 else "存在失败"))
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
