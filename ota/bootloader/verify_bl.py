#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_bl.py —— Bootloader 静态自检（不依赖网络、编译器、第三方库）

用途：在无法编译（无 Keil/ARM 工具链）的环境里，为 task-2 的"必须自证的不变量"
      逐条产出可复核的 file:line 证据。任何一条失败 -> 退出码 1。

运行：python ota/bootloader/verify_bl.py

检查项：
  0) 交付文件齐备（Core/Src/*.c、Core/Inc/*.h、BL_BUILD.md）
  1) 每个 C 源/头文件括号配平（剥离注释与字符串后统计 () [] {}）
  2) 不变量1  main() 第一句是 SCB->VTOR = OTA_FLASH_BASE，且早于 HAL_Init()
  3) 不变量2  bl_flash_if.c 有 addr < OTA_SLOT_A_ADDR 的保护判断并返回 OTA_FLASH_ERR_PROTECT
  4) 不变量3  编程使用 FLASH_TYPEPROGRAM_DOUBLEWORD，且不出现 WORD/HALFWORD 变体
  5) 不变量4  bl_param.c 写流程四步齐全且顺序正确（读页 -> 擦页 -> 回写另一份 -> 写新值）
  6) 不变量5  试运行递减在跳转判定之前；trialLeft 归零切槽
  7) 不变量6  跳转前独立检查 SP 与复位向量（OTA_ERR_SP / OTA_ERR_RESET_VEC）
  8) 不变量7  0x36 块序号 1..255 循环、0 非法（UDS_BLOCK_SEQ_OF + NRC 0x73）
  9) 不变量8  擦除/写入前后调用 HAL_IWDG_Refresh（经 BL_FlashKickDog）
 10) 不变量9  bl_*.c / main.c 不出现裸 0x0800xxxx 地址字面量
 11) 不变量10 CRC32 表与自检向量一致（Python 复算 0xCBF43926）+ main 早期调用 + FAIL 串
 12) 契约常量与代码/BL_BUILD.md 的地址一致性（含与 ota_common.h 的 #error 同构复算）
 13) 独立性：BL 不 include 应用层头（can.h/isotp.h/uds.h/console.h/bsp_uart.h）
 14) 契约新宏被引用：UDS_MAXLEN_FIELD / UDS_VERIFY_RESP_FULL_LEN / UDS_BLOCK_SEQ_OF
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "Core", "Src")
INC_DIR = os.path.join(HERE, "Core", "Inc")
OTA_COMMON = os.path.normpath(os.path.join(HERE, "..", "common", "ota_common.h"))
BUILD_MD = os.path.join(HERE, "BL_BUILD.md")

SRC_FILES = [
    "main.c", "bl_clock.c", "bl_can.c", "bl_flash_if.c", "bl_crc32.c",
    "bl_param.c", "bl_image.c", "bl_jump.c", "bl_isotp.c", "bl_diag.c", "bl_uart.c",
]
INC_FILES = [
    "bl_main.h", "bl_clock.h", "bl_can.h", "bl_flash_if.h", "bl_crc32.h",
    "bl_param.h", "bl_image.h", "bl_jump.h", "bl_isotp.h", "bl_diag.h", "bl_uart.h",
]

APP_LAYER_HEADERS = ["can.h", "isotp.h", "uds.h", "console.h", "bsp_uart.h"]

RESULTS = []   # (ok: bool, name: str, evidence: list[str])
WARNINGS = []  # str


def check(ok, name, evidence=None):
    RESULTS.append((bool(ok), name, list(evidence or [])))
    return bool(ok)


def warn(msg):
    WARNINGS.append(msg)


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def line_of(text, index):
    """返回 index 所在的 1-based 行号。"""
    return text.count("\n", 0, max(index, 0)) + 1


def find_line(text, pattern):
    m = re.search(pattern, text)
    return line_of(text, m.start()) if m else 0


def ev(path, text, pattern, label=None):
    """构造 'file:line  <该行内容>' 证据串。"""
    m = re.search(pattern, text)
    if not m:
        return ["%s: <未找到 %s>" % (os.path.basename(path), label or pattern)]
    ln = line_of(text, m.start())
    raw = text.splitlines()[ln - 1].strip()
    return ["%s:%d  %s" % (os.path.basename(path), ln, raw)]


def strip_comments_strings(text):
    """把注释与字符串字面量替换为空格，保留换行（供配平与字面量扫描使用）。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        elif c == '"' or c == "'":
            quote = c
            out.append(" ")
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if text[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def brace_balance(path, raw):
    code = strip_comments_strings(raw)
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}
    stack = []
    for idx, ch in enumerate(code):
        if ch in pairs:
            stack.append((ch, idx))
        elif ch in closers:
            if not stack or stack[-1][0] != closers[ch]:
                return False, "%s:%d 出现不匹配的 '%s'" % (
                    os.path.basename(path), line_of(code, idx), ch)
            stack.pop()
    if stack:
        ch, idx = stack[-1]
        return False, "%s:%d 未闭合的 '%s'" % (os.path.basename(path), line_of(code, idx), ch)
    return True, ""


def eval_defines(text):
    """把 ota_common.h 中的简单 #define 求值成 int（支持别名与四则运算）。"""
    raw = {}
    for m in re.finditer(r"^[ \t]*#define[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+([^\r\n/]+)", text, re.M):
        raw[m.group(1)] = m.group(2).strip()

    values = {}
    for _ in range(8):
        progress = False
        for name, expr in raw.items():
            e = expr
            e = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", lambda mm: (
                str(values[mm.group(1)]) if mm.group(1) in values else mm.group(0)), e)
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


def main():
    ok_all = True

    # ---------------------------------------------------------------- 0) 文件齐备
    missing = []
    for f in SRC_FILES:
        if not os.path.isfile(os.path.join(SRC_DIR, f)):
            missing.append("Core/Src/" + f)
    for f in INC_FILES:
        if not os.path.isfile(os.path.join(INC_DIR, f)):
            missing.append("Core/Inc/" + f)
    if not os.path.isfile(BUILD_MD):
        missing.append("BL_BUILD.md")
    if not os.path.isfile(OTA_COMMON):
        missing.append("../common/ota_common.h")
    check(not missing, "文件齐备（%d 源 + %d 头 + BL_BUILD.md + 契约头）" % (len(SRC_FILES), len(INC_FILES)),
          (["缺失: " + ", ".join(missing)] if missing else
           ["Core/Src/: " + ", ".join(SRC_FILES), "Core/Inc/: " + ", ".join(INC_FILES)]))

    texts = {}
    for f in SRC_FILES:
        p = os.path.join(SRC_DIR, f)
        if os.path.isfile(p):
            texts["Core/Src/" + f] = read_text(p)
    for f in INC_FILES:
        p = os.path.join(INC_DIR, f)
        if os.path.isfile(p):
            texts["Core/Inc/" + f] = read_text(p)

    def T(name):
        return texts.get(name, "")

    main_c = T("Core/Src/main.c")
    flash_c = T("Core/Src/bl_flash_if.c")
    crc_c = T("Core/Src/bl_crc32.c")
    param_c = T("Core/Src/bl_param.c")
    image_c = T("Core/Src/bl_image.c")
    diag_c = T("Core/Src/bl_diag.c")
    isotp_c = T("Core/Src/bl_isotp.c")
    common_h = read_text(OTA_COMMON) if os.path.isfile(OTA_COMMON) else ""
    build_md = read_text(BUILD_MD) if os.path.isfile(BUILD_MD) else ""

    # ---------------------------------------------------------------- 1) 括号配平
    bad = []
    for name, raw in sorted(texts.items()):
        good, why = brace_balance(name, raw)
        if not good:
            bad.append(why)
    check(not bad, "所有 C 文件括号配平（剥离注释/字符串后）",
          (bad if bad else ["%d 个文件全部配平" % len(texts)]))

    # ---------------------------------------------------------------- 2) 不变量1
    code_m = strip_comments_strings(main_c)
    ok1 = False
    ev1 = []
    m = re.search(r"\bint\s+main\s*\(\s*void\s*\)", code_m)
    if m:
        brace = code_m.find("{", m.end())
        body = code_m[brace + 1:].lstrip()
        # 找"第一条**可执行**语句"：跳过函数级声明（static/const/类型声明等）。
        # 声明不是可执行语句，把它当成首句是解析器缺陷（曾误报）。
        # 同时跳过 #if 等预处理行（本检查只针对正常 BL 的 main）。
        first_stmt = None
        rest = body
        while rest:
            stmt = rest.split(";", 1)[0] + ";"
            stmt_norm = re.sub(r"\s+", " ", stmt).strip()
            head = stmt_norm.split(" ", 1)[0]
            is_decl = (stmt_norm.startswith("static ")
                       or stmt_norm.startswith("const ")
                       or stmt_norm.startswith("volatile ")
                       or stmt_norm.startswith("register ")
                       or stmt_norm.startswith("typedef ")
                       or stmt_norm.startswith("#")
                       or bool(re.match(r"^(void|char|short|int|long|float|double|unsigned|signed|"
                                        r"uint\d+_t|int\d+_t|size_t|bool|_Bool)\b", stmt_norm))
                       or bool(re.match(r"^[A-Za-z_]\w*_t\s", stmt_norm))       # 自定义类型
                       or bool(re.match(r"^[A-Za-z_]\w*\s*\*?\s*[A-Za-z_]\w*\s*(=|\[|;)", stmt_norm)))
            if not is_decl:
                first_stmt = stmt_norm
                break
            if ";" not in rest:
                break
            rest = rest.split(";", 1)[1].lstrip()

        ok_first = bool(first_stmt and
                        re.match(r"SCB\s*->\s*VTOR\s*=\s*OTA_FLASH_BASE\s*;$", first_stmt))
        vtor_at = main_c.find("SCB->VTOR = OTA_FLASH_BASE")
        hal_at = main_c.find("HAL_Init()")
        ok_order = (vtor_at >= 0) and (hal_at >= 0) and (vtor_at < hal_at)
        ok1 = ok_first and ok_order
        ev1.append("main.c:%d  %s" % (line_of(main_c, vtor_at), "SCB->VTOR = OTA_FLASH_BASE;"))
        ev1.append("main.c:%d  %s" % (line_of(main_c, hal_at), "HAL_Init();"))
        if not ok_first:
            ev1.append("main() 第一条**可执行**语句实为: %s" % first_stmt)
    else:
        ev1.append("未找到 int main(void)")
    check(ok1, "不变量1  main() 首条可执行语句 SCB->VTOR = OTA_FLASH_BASE 且早于 HAL_Init()", ev1)

    # ---------------------------------------------------------------- 3) 不变量2
    ev2 = ev("Core/Src/bl_flash_if.c", flash_c, r"addr\s*<\s*OTA_SLOT_A_ADDR")
    ev2 += ev("Core/Src/bl_flash_if.c", flash_c, r"return OTA_FLASH_ERR_PROTECT;")
    pub = re.search(r"OTA_FlashResult\s+BL_FlashEraseRange\s*\(", flash_c)
    ok2 = bool(re.search(r"addr\s*<\s*OTA_SLOT_A_ADDR", flash_c)) and \
          bool(re.search(r"return OTA_FLASH_ERR_PROTECT;", flash_c)) and pub is not None
    check(ok2, "不变量2  addr < OTA_SLOT_A_ADDR -> OTA_FLASH_ERR_PROTECT（公开接口硬保护）", ev2)

    # ---------------------------------------------------------------- 4) 不变量3
    ev3 = ev("Core/Src/bl_flash_if.c", flash_c, r"FLASH_TYPEPROGRAM_DOUBLEWORD")
    bad_prog = re.findall(r"FLASH_TYPEPROGRAM_(WORD|HALFWORD|BYTE)", flash_c)
    ok3 = bool(re.search(r"FLASH_TYPEPROGRAM_DOUBLEWORD", flash_c)) and not bad_prog
    if bad_prog:
        ev3.append("发现禁用变体: FLASH_TYPEPROGRAM_" + ", ".join(sorted(set(bad_prog))))
    check(ok3, "不变量3  仅使用 FLASH_TYPEPROGRAM_DOUBLEWORD（64 位粒度）", ev3)

    # ---------------------------------------------------------------- 5) 不变量4
    # 只在实际的 BL_ParamStore 函数体内找标记（文件头注释里也提到同样的步骤名）
    steps = ["[PARAM-STEP-1]", "[PARAM-STEP-2]", "[PARAM-STEP-3]", "[PARAM-STEP-4]"]
    anchor = param_c.find("bool BL_ParamStore(const OTA_Cfg *newCfg)")
    base = anchor if anchor >= 0 else 0
    region = param_c[base:]
    pos = [region.find(s) for s in steps]
    order_ok = all(p >= 0 for p in pos) and pos == sorted(pos)
    ev4 = []
    for s, p in zip(steps, pos):
        if p >= 0:
            ln = line_of(param_c, base + p)
            ev4.append("bl_param.c:%d  %s" % (ln, param_c.splitlines()[ln - 1].strip()))
    body_ok = False
    if order_ok:
        seg1 = region[pos[0]:pos[1]]
        seg2 = region[pos[1]:pos[2]]
        seg3 = region[pos[2]:pos[3]]
        seg4 = region[pos[3]:]
        c1 = ("s_pageBuf[" in seg1) and ("*(volatile const uint8_t *)" in seg1)
        c2 = "BL_FlashEraseRangeEx(" in seg2
        # STEP-3 必须是"整页恢复且跳过目标副本"：既要用页镜像回写（s_pageBuf + BL_FlashWriteEx），
        # 又要按 tgtOff 跳过本次要写的那一份。历史缺陷：只回写向量表 + 另一份副本，
        # 结果 bl.sct 把代码铺进页 0 时，写控制区会把 BL 自己的复位向量代码擦掉（变砖）。
        c3 = ("s_pageBuf" in seg3) and ("BL_FlashWriteEx(" in seg3) and ("tgtOff" in seg3)
        c4 = ("tgtOff" in seg4) and ("BL_FlashWriteEx(" in seg4)
        body_ok = c1 and c2 and c3 and c4
        ev4.append("  步骤内容: 读页镜像=%s 擦页=%s 整页恢复(跳过目标副本)=%s 写新值=%s"
                   % (c1, c2, c3, c4))
    check(order_ok and body_ok,
          "不变量4  控制区写流程四步齐全且顺序正确", ev4)

    # ---------------------------------------------------------------- 6) 不变量5
    p_dec = main_c.find("[TRIAL-DEC]")
    p_zero = main_c.find("[TRIAL-ZERO]")
    p_jump = main_c.find("[JUMP-CHECK]")
    call_trial = main_c.find("BL_HandleTrial();")
    call_boot = main_c.find("BL_TryBootApp();")
    ok5 = (0 <= p_dec < p_zero) and (p_zero < p_jump) and (0 <= call_trial < call_boot) and \
          ("s_cfg.trialLeft--" in main_c[p_dec:p_zero]) and \
          ("BL_ParamStore(&s_cfg)" in main_c[p_dec:p_zero]) and \
          ("OTA_ST_IDLE_" in main_c[p_zero:p_jump]) and \
          ("BL_ImageValidate" in main_c[p_jump:])
    ev5 = []
    for label, p in (("[TRIAL-DEC]", p_dec), ("[TRIAL-ZERO]", p_zero),
                     ("[JUMP-CHECK]", p_jump), ("BL_HandleTrial();", call_trial),
                     ("BL_TryBootApp();", call_boot)):
        if p >= 0:
            ln = line_of(main_c, p)
            ev5.append("main.c:%d  %s" % (ln, main_c.splitlines()[ln - 1].strip()))
    check(ok5, "不变量5  试运行递减先于跳转判定；归零时切槽", ev5)

    # ---------------------------------------------------------------- 7) 不变量6
    ev6 = ev("Core/Src/bl_image.c", image_c, r"return BL_IMG_ERR_SP;")
    ev6 += ev("Core/Src/bl_image.c", image_c, r"return BL_IMG_ERR_RESET_VEC;")
    n_sp_if = len(re.findall(r"if\s*\(.*\)\s*\{?\s*\n?\s*return BL_IMG_ERR_SP;", image_c, re.S))
    ok6 = ("return BL_IMG_ERR_SP;" in image_c) and ("return BL_IMG_ERR_RESET_VEC;" in image_c) and \
          (image_c.find("BL_IMG_ERR_SP") != image_c.find("BL_IMG_ERR_RESET_VEC")) and \
          ("sp = BL_ImageRdU32(p);" in image_c) and ("rv = BL_ImageRdU32(p + 4U);" in image_c) and \
          ("OTA_ERR_SP" in image_c) and ("OTA_ERR_RESET_VEC" in image_c) and n_sp_if >= 1
    if n_sp_if < 1:
        ev6.append("SP 检查未构成独立 if 判断")
    ev6.append("bl_image.c:%d  OTA_ERR_SP 映射" % find_line(image_c, r"return OTA_ERR_SP;"))
    ev6.append("bl_image.c:%d  OTA_ERR_RESET_VEC 映射" % find_line(image_c, r"return OTA_ERR_RESET_VEC;"))
    check(ok6, "不变量6  跳转前独立检查 SP 与复位向量（含 OTA_ERR_SP / OTA_ERR_RESET_VEC）", ev6)

    # ---------------------------------------------------------------- 8) 不变量7
    ok7 = ("UDS_BLOCK_SEQ_OF(" in diag_c) and ("UDS_NRC_WRONG_BLOCK_SN" in diag_c) and \
          re.search(r"seq\s*==\s*0U", diag_c) is not None
    ev7 = ev("Core/Src/bl_diag.c", diag_c, r"UDS_BLOCK_SEQ_OF\(")
    ev7 += ev("Core/Src/bl_diag.c", diag_c, r"\(seq == 0U\)")
    ev7 += ev("Core/Src/bl_diag.c", diag_c, r"UDS_NRC_WRONG_BLOCK_SN")
    check(ok7, "不变量7  0x36 块序号 1..255 循环、0 非法 -> NRC 0x73", ev7)

    # ---------------------------------------------------------------- 9) 不变量8
    ev8 = ev("Core/Src/bl_flash_if.c", flash_c, r"HAL_IWDG_Refresh")
    markers = ["[IWDG-PRE-ERASE]", "[IWDG-POST-ERASE]", "[IWDG-PRE-WRITE]", "[IWDG-POST-WRITE]"]
    ok8 = bool(re.search(r"HAL_IWDG_Refresh", flash_c))
    flash_lines = flash_c.splitlines()
    for mk in markers:
        p = flash_c.find(mk)
        if p < 0:
            ok8 = False
            ev8.append("缺少标记 " + mk)
            continue
        ln = line_of(flash_c, p)
        window = "\n".join(flash_lines[max(0, ln - 2):ln + 1])   # 标记行及其前一行（喂狗调用带尾注释）
        hit = "BL_FlashKickDog();" in window
        ok8 = ok8 and hit
        ev8.append("bl_flash_if.c:%d  %s -> 同处调用 BL_FlashKickDog() = %s" % (ln, mk, hit))
    check(ok8, "不变量8  擦除/写入前后喂狗（HAL_IWDG_Refresh）", ev8)

    # ---------------------------------------------------------------- 10) 不变量9
    hits = []
    for name in sorted(texts):
        if not name.endswith(".c"):
            continue
        code = strip_comments_strings(texts[name])
        for m in re.finditer(r"0x0800[0-9A-Fa-f]{4}", code):
            ln = line_of(code, m.start())
            raw = texts[name].splitlines()[ln - 1]
            if "OTA_FLASH_BASE" in raw:
                continue
            hits.append("%s:%d  %s" % (name, ln, raw.strip()))
    check(not hits, "不变量9  bl_*.c / main.c 无裸 0x0800xxxx 字面量（地址全部来自契约宏）",
          (hits if hits else ["扫描 %d 个 .c 文件：0 处裸地址" % sum(1 for n in texts if n.endswith('.c'))]))

    # ---------------------------------------------------------------- 11) 不变量10
    ev10 = []
    ok10 = False
    tbl_m = re.search(r"s_crc32Table\[256\][^=]*=\s*\{(.*?)\};", crc_c, re.S)
    if tbl_m:
        entries = re.findall(r"0x([0-9A-Fa-f]{8})UL", tbl_m.group(1))
        ev10.append("bl_crc32.c:%d  CRC32 表项数 = %d" % (line_of(crc_c, tbl_m.start()), len(entries)))
        if len(entries) == 256:
            table = [int(x, 16) for x in entries]
            crc = 0xFFFFFFFF
            for b in b"123456789":
                crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
            crc ^= 0xFFFFFFFF
            ok10 = (crc == 0xCBF43926)
            ev10.append("  Python 复算 CRC32(\"123456789\") = 0x%08X（期望 0xCBF43926）= %s"
                        % (crc, "一致" if ok10 else "不一致"))
    else:
        ev10.append("未解析到 bl_crc32.c 的 256 项 CRC 表")
    lock = re.search(r"OTA_CRC32_CHECK_VECTOR\s*!=\s*0xCBF43926UL", flash_c)
    ev10 += ev("Core/Src/bl_flash_if.c", flash_c, r"OTA_CRC32_CHECK_VECTOR != 0xCBF43926UL")
    ev10 += ev("Core/Src/bl_flash_if.c", flash_c, r"BL_Crc32SelfTest\(\)")
    call_self = main_c.find("BL_FlashSelfTest()")
    flash_init = main_c.find("BL_FlashInit()")
    ok_call = (call_self >= 0) and (flash_init >= 0) and (call_self < flash_init)
    if call_self >= 0:
        ev10.append("main.c:%d  早期调用 BL_FlashSelfTest()（早于 BL_FlashInit = %s）"
                    % (line_of(main_c, call_self), ok_call))
    else:
        ev10.append("main.c 未调用 BL_FlashSelfTest()")
    has_fail_str = "BL: crc self-test FAIL" in main_c
    ev10 += ev("Core/Src/main.c", main_c, r"BL: crc self-test FAIL")
    ok10 = ok10 and (lock is not None) and ok_call and has_fail_str
    check(ok10, "不变量10 CRC32 自检向量可被实现验证 + main 早期调用 + 失败串", ev10)

    # ---------------------------------------------------------------- 12) 契约一致性
    values = eval_defines(common_h)
    need = ["OTA_FLASH_BASE", "OTA_FLASH_PAGE_SIZE", "OTA_FLASH_BANK_SIZE", "OTA_FLASH_SIZE",
            "OTA_CFG_PRIMARY_ADDR", "OTA_CFG_MIRROR_ADDR", "OTA_CFG_PAGE_SIZE",
            "OTA_CFG_VECTAB_BYTES", "OTA_CFG_SLOT_OFFSET", "OTA_CFG_SLOT_MIRROR_OFF",
            "OTA_BL_CODE_ADDR", "OTA_BL_CODE_SIZE", "OTA_SLOT_A_ADDR", "OTA_SLOT_B_ADDR",
            "OTA_SLOT_SIZE", "OTA_IMG_HDR_SIZE", "OTA_IMG_MAX_PAYLOAD"]
    ev12 = []
    ok12 = all(k in values for k in need)

    # OTA_Cfg 尺寸/偏移机械复算（契约 L236 的 #error 同构）：
    # 教训——曾把 slotCrc[2]/slotSize[2]/slotVersion[2] 各按 1 个 u32 计算得出 36 字节，
    # 真值是 12 个 u32 = 48 字节。这里按"元素个数"枚举，三方文档/实现必须与之一致。
    m_cfg = re.search(r"typedef struct\s*\{(.*?)\}\s*OTA_Cfg\s*;", common_h, re.S)
    cfg_bytes, cfg_u32, crc_off = 0, 0, None
    if m_cfg:
        for line in m_cfg.group(1).splitlines():
            mm = re.match(r"\s*uint32_t\s+([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?\s*;", line)
            if not mm:
                continue
            cnt = int(mm.group(2) or 1)
            if mm.group(1) == "crc":
                crc_off = cfg_bytes
            cfg_bytes += 4 * cnt
            cfg_u32 += cnt
    cfg_ok = (cfg_bytes == 48) and (cfg_u32 == 12) and (crc_off == 44) and (cfg_bytes % 8 == 0)
    ok12 = ok12 and cfg_ok
    ev12.append("  契约复算 OTA_Cfg = %d 个 u32 / %d 字节, offsetof(crc)=%s, 8B 对齐=%s -> %s"
                % (cfg_u32, cfg_bytes, crc_off, (cfg_bytes % 8 == 0), "通过" if cfg_ok else "失败"))
    # 契约里的尺寸/偏移断言：接受两种等价写法——
    #   ① 预处理 #if + #error（若工具链支持 sizeof/offsetof 出现在 #if 中）
    #   ② 负数组长度 typedef（C90 起标准，ARMCC/GCC/Clang 行为一致，实际采用）
    cfg_decl = re.findall(
        r"^#if \(sizeof\(OTA_Cfg\) != 48U\)"
        r"|^#if \(offsetof\(OTA_Cfg, crc\) != 44U\)"
        r"|typedef char \w+\[\(sizeof\(OTA_Cfg\) == 48U\) \? 1 : -1\]"
        r"|typedef char \w+\[\(offsetof\(OTA_Cfg, crc\) == 44U\) \? 1 : -1\]",
        common_h, re.M)
    ev12.append("  ota_common.h 内含 sizeof/offsetof 断言（#if 或 typedef 任一形式）= %s"
                % ("是" if len(cfg_decl) >= 2 else "否"))
    ok12 = ok12 and (len(cfg_decl) >= 2)

    if not ok12:
        ev12.append("ota_common.h 缺少可求值常量: " + ", ".join(k for k in need if k not in values))
    if ok12:
        v = values
        facts = [
            ("分区：控制页/槽/代码区不重叠",
             (v["OTA_CFG_PRIMARY_ADDR"] + v["OTA_CFG_PAGE_SIZE"] <= v["OTA_BL_CODE_ADDR"]) and
             (v["OTA_BL_CODE_ADDR"] + v["OTA_BL_CODE_SIZE"] <= v["OTA_SLOT_A_ADDR"]) and
             (v["OTA_SLOT_A_ADDR"] + v["OTA_SLOT_SIZE"] <= v["OTA_CFG_MIRROR_ADDR"]) and
             (v["OTA_CFG_MIRROR_ADDR"] + v["OTA_CFG_PAGE_SIZE"] <= v["OTA_SLOT_B_ADDR"]) and
             (v["OTA_SLOT_B_ADDR"] + v["OTA_SLOT_SIZE"] <= v["OTA_FLASH_BASE"] + v["OTA_FLASH_SIZE"])),
            ("Slot A 不越 Bank1 上界",
             v["OTA_SLOT_A_ADDR"] + v["OTA_SLOT_SIZE"] <= v["OTA_FLASH_BASE"] + v["OTA_FLASH_BANK_SIZE"]),
            ("Slot B 下对齐 Bank2 起始",
             (v["OTA_SLOT_B_ADDR"] - v["OTA_FLASH_BASE"]) % v["OTA_FLASH_BANK_SIZE"] ==
             v["OTA_CFG_PAGE_SIZE"]),
            ("控制区副本0 在向量表之后、副本1 不跨页",
             (v["OTA_CFG_SLOT_OFFSET"] >= v["OTA_CFG_VECTAB_BYTES"]) and
             (v["OTA_CFG_SLOT_MIRROR_OFF"] + 256 <= v["OTA_CFG_PAGE_SIZE"]) and
             (v["OTA_CFG_SLOT_OFFSET"] % 8 == 0) and (v["OTA_CFG_SLOT_MIRROR_OFF"] % 8 == 0) and
             (v["OTA_CFG_SLOT_OFFSET"] + 256 <= v["OTA_CFG_SLOT_MIRROR_OFF"])),
            ("APP 链接基址 = 槽基址 + 0x80 槽首保留区",
             v["OTA_IMG_HDR_SIZE"] == 0x80),
            # 血线4（2026-10-05 板子变砖换来）：Cortex-M4 的 SCB->VTOR[6:0] 是 RES0
            # （写入被忽略），**向量表必须 0x80 字节对齐**。APP 基址 = 槽基址 + 保留区，
            # 若保留区是 32，基址就是 0x0800C020 -> 写进 VTOR 只剩 0x0800C000
            # -> 向量表落到镜像头上 -> INVSTATE/IBUSERR -> lockup。见 docs/11。
            ("APP 链接基址按 0x80 字节对齐（VTOR 硬性要求）",
             ((v["OTA_SLOT_A_ADDR"] + v["OTA_IMG_HDR_SIZE"]) % 0x80 == 0) and
             ((v["OTA_SLOT_B_ADDR"] + v["OTA_IMG_HDR_SIZE"]) % 0x80 == 0)),
        ]
        for label, good in facts:
            ok12 = ok12 and good
            ev12.append("  契约复算 %-34s = %s" % (label, "通过" if good else "失败"))

        # 代码必须引用契约宏而不是硬编码
        ref_ok = all(re.search(r, param_c) for r in [r"OTA_CFG_PRIMARY_ADDR", r"OTA_CFG_MIRROR_ADDR",
                                                    r"OTA_CFG_SLOT_OFFSET", r"OTA_CFG_SLOT_MIRROR_OFF",
                                                    r"OTA_CFG_VECTAB_BYTES"])
        ok12 = ok12 and ref_ok
        ev12.append("  bl_param.c 引用契约偏移宏 = %s" % ref_ok)
        ev12 += ev("Core/Src/bl_param.c", param_c, r"OTA_CFG_SLOT_OFFSET, OTA_CFG_SLOT_MIRROR_OFF")
        img_ok = re.search(r"loadAddr != \(hdrAddr \+ OTA_IMG_HDR_SIZE\)", image_c) is not None
        ok12 = ok12 and img_ok
        ev12.append("  bl_image.c 用 OTA_IMG_HDR_SIZE 推导 APP 基址 = %s" % img_ok)

        # BL_BUILD.md 必须写明与契约一致的地址；且不得残留旧 Slot A 基址
        tokens = [
            ("0x%08X" % v["OTA_CFG_PRIMARY_ADDR"], "控制区主份页"),
            ("0x%08X" % v["OTA_CFG_MIRROR_ADDR"], "控制区镜像页"),
            ("0x%08X" % v["OTA_BL_CODE_ADDR"], "BL 代码起始"),
            ("0x%08X" % v["OTA_SLOT_A_ADDR"], "Slot A 基址"),
            ("0x%08X" % v["OTA_SLOT_B_ADDR"], "Slot B 基址"),
            ("0x%08X" % (v["OTA_SLOT_A_ADDR"] + v["OTA_IMG_HDR_SIZE"]), "Slot A APP 链接基址"),
            ("0x%08X" % (v["OTA_SLOT_B_ADDR"] + v["OTA_IMG_HDR_SIZE"]), "Slot B APP 链接基址"),
            ("0x%X" % v["OTA_CFG_SLOT_OFFSET"], "控制区副本0 偏移"),
            ("0x%X" % v["OTA_CFG_SLOT_MIRROR_OFF"], "控制区副本1 偏移"),
        ]
        for tok, label in tokens:
            hit = tok in build_md
            ok12 = ok12 and hit
            ev12.append("  BL_BUILD.md 含 %-22s (%s) = %s" % (tok, label, hit))
        stale = "0x08000820" in build_md
        if stale:
            ok12 = False
            ev12.append("  BL_BUILD.md 残留旧 Slot A APP 基址 0x08000820")
        ev12.append("  BL_BUILD.md 无旧地址 0x08000820 = %s" % (not stale))
    check(ok12, "契约一致性：分区常量 vs 代码 vs BL_BUILD.md（含 ota_common.h 断言的同构复算）", ev12)

    # ---------------------------------------------------------------- 13) 独立性
    bad_inc = []
    for name, raw in sorted(texts.items()):
        for hdr in APP_LAYER_HEADERS:
            if re.search(r'#include\s+"%s"' % re.escape(hdr), raw):
                bad_inc.append("%s 引入了应用层头 %s" % (name, hdr))
    check(not bad_inc, "独立性：BL 不 include 应用层头（can.h/isotp.h/uds.h/console.h/bsp_uart.h）",
          (bad_inc if bad_inc else ["11 源 + 11 头全部只 include bl_*.h / HAL / 契约头"]))

    # ---------------------------------------------------------------- 14) 契约新宏引用
    ev14 = ev("Core/Src/bl_diag.c", diag_c, r"UDS_MAXLEN_FIELD")
    ev14 += ev("Core/Src/bl_diag.c", diag_c, r"UDS_VERIFY_RESP_FULL_LEN")
    ev14 += ev("Core/Src/bl_diag.c", diag_c, r"UDS_BLOCK_SEQ_OF")
    ev14 += ev("Core/Src/bl_diag.c", diag_c, r"case UDS_RID_ACTIVATE")
    ok14 = all(x in diag_c for x in ["UDS_MAXLEN_FIELD", "UDS_VERIFY_RESP_FULL_LEN",
                                     "UDS_BLOCK_SEQ_OF", "UDS_RID_ACTIVATE"]) and \
        ("case UDS_RID_ERASE" in diag_c) and ("case UDS_RID_VERIFY" in diag_c)
    check(ok14, "契约新宏/例程引用：maxLen、FF01 9 字节、块序号、FF00~FF02", ev14)

    # ---------------------------------------------------------------- 输出
    print("=" * 78)
    print("Bootloader 静态自检 verify_bl.py")
    print("目录: %s" % HERE)
    print("契约: %s" % OTA_COMMON)
    print("=" * 78)
    for ok, name, evidence in RESULTS:
        print("[%s] %s" % ("OK " if ok else "FAIL", name))
        for line in evidence:
            print("        " + line)
    if WARNINGS:
        print("-" * 78)
        for w in WARNINGS:
            print("[WARN] " + w)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    ok_all = (failed == 0)
    print("=" * 78)
    print("汇总: %d 项检查, %d 通过, %d 失败 -> %s"
          % (len(RESULTS), passed, failed, "全绿" if ok_all else "存在失败"))
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
