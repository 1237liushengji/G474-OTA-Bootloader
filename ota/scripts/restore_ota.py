#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从上游官方归档恢复被覆盖的文件，并重新施加全部 OTA 改动（有序、含校验）。

背景：一个后台作业在整理之后跑了逐文件取回，把 can.c / uds.c / console.c /
G474.uvprojx 覆盖回了上游原版（main.c 等因尺寸不同侥幸保留）。
本脚本用官方 zip 作"干净锚点"，按顺序重新施加改动，最后自检。

用法:
    python ota/scripts/restore_ota.py <官方归档根>            # 干跑
    python ota/scripts/restore_ota.py <官方归档根> --apply    # 执行
"""
import os
import re
import shutil
import sys

from paths import workspace_root

ROOT = workspace_root()

# 需要从归档恢复原版的文件（这些文件里有我的改动，且已被覆盖）
RESTORE = [
    'G474/Drivers/User/Src/can.c',
    'G474/Drivers/User/Src/uds.c',
    'G474/Drivers/User/Src/console.c',
    'G474/MDK-ARM/G474.uvprojx',
]

# ---------------------------------------------------------------- 文本改动
# can.c：两处（过滤器数量 + 第三过滤器）
CAN_C = [
    ("  hfdcan2.Init.StdFiltersNbr                  = (s_promisc || s_loopbackSelfTest) ? 1U : 2U;",
     "  /* OTA 扩展：正常模式需要同时收 0x7E0(请求) 与 0x7E8(应答) 两个诊断 ID。\n"
     "   * 一条掩码型过滤器只能匹配\"一个 ID + 一个掩码\"（FilterID2 是掩码而非第二个 ID），\n"
     "   * 因此由 2 个过滤器增加到 3 个（业务帧 + 0x7E0 + 0x7E8）。 */\n"
     "  hfdcan2.Init.StdFiltersNbr                  = (s_promisc || s_loopbackSelfTest) ? 1U : 3U;"),
    ("    filterConfig.FilterID1   = ISOTP_DIAG_REQ_ID;       /* 板B接收诊断请求0x7E0 */\n"
     "#endif\n"
     "    filterConfig.FilterID2   = 0x7FFU;\n"
     "    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);\n"
     "    if (status != HAL_OK)\n"
     "    {\n"
     "      return status;\n"
     "    }\n"
     "  }\n"
     "#endif",
     "    filterConfig.FilterID1   = ISOTP_DIAG_REQ_ID;       /* 板B接收诊断请求0x7E0 */\n"
     "#endif\n"
     "    filterConfig.FilterID2   = 0x7FFU;\n"
     "    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);\n"
     "    if (status != HAL_OK)\n"
     "    {\n"
     "      return status;\n"
     "    }\n"
     "\n"
     "    /* 第三过滤器：诊断应答 ID（0x7E8）。\n"
     "     * 用途：① 串口隧道模式需回吐 0x7E0/0x7E8 两个方向；\n"
     "     *       ② 单板自测时本节点既是 ECU 又是诊断仪；\n"
     "     *       ③ 双 CAN / 多诊断 ID 并存场景。 */\n"
     "    filterConfig.FilterIndex = 2U;\n"
     "    filterConfig.FilterID1   = ISOTP_DIAG_RESP_ID;      /* 0x7E8 */\n"
     "    filterConfig.FilterID2   = 0x7FFU;\n"
     "    status = HAL_FDCAN_ConfigFilter(&hfdcan2, &filterConfig);\n"
     "    if (status != HAL_OK)\n"
     "    {\n"
     "      return status;\n"
     "    }\n"
     "  }\n"
     "#endif"),
]

# uds.c：五处
UDS_C = [
    ('#include "can.h"\n#include "bsp_uart.h"\n#include <string.h>',
     '#include "can.h"\n#include "bsp_uart.h"\n'
     '#include "ota_agent.h"   /* OTA：0x34/0x36/0x37/0x31/0x11 与 F180~F190 */\n'
     '#include <string.h>'),
    ("  uint8_t  resp[64];\n  uint16_t rlen = 0U;",
     "  /* OTA 扩展：ISOTP_MAX_MSG 已提升到 1024，应答缓冲必须同步放大，\n"
     "   * 否则一旦某个 DID 的应答超过 64 字节，rlen 会越界写栈（静默破坏内存）。 */\n"
     "  uint8_t  resp[ISOTP_MAX_MSG];\n  uint16_t rlen = 0U;"),
    ("    case UDS_DID_LED:\n"
     "      resp[rlen++] = App_GetLed();\n"
     "      break;\n"
     "    default:\n"
     "      Uds_SendNrc(0x22U, 0x31U);\n"
     "      return;",
     "    case UDS_DID_LED:\n"
     "      resp[rlen++] = App_GetLed();\n"
     "      break;\n"
     "    default:\n"
     "    {\n"
     "      /* OTA 只读 DID（F180 状态 / F181 活动槽 / F182 版本 / F183 槽CRC / F184 能力位图）：\n"
     "       * 交 OTA 模块填充；返回 0 表示该 DID 不归 OTA 管，按未知 DID 处理。 */\n"
     "      uint16_t n = Ota_ReadDid(did, &resp[3], (uint16_t)(sizeof(resp) - 3U));\n"
     "      if (n == 0U)\n"
     "      {\n"
     "        Uds_SendNrc(0x22U, 0x31U);\n"
     "        return;\n"
     "      }\n"
     "      rlen = n;                       /* Ota_ReadDid 已含 62 <DID> 头 */\n"
     "      break;\n"
     "    }"),
    ("  if ((len != 4U) || ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) != UDS_DID_LED))\n"
     "  {\n"
     "    Uds_SendNrc(0x2EU, 0x31U);\n"
     "    return;\n"
     "  }\n"
     "\n"
     "  App_SetLed(req[3]);",
     "  /* 长度守卫：DID 在 req[1..2]，长度不足 3 时不得读取（否则越界读） */\n"
     "  if (len < 3U)\n"
     "  {\n"
     "    Uds_SendNrc(0x2EU, 0x13U);            /* incorrectMessageLengthOrInvalidFormat */\n"
     "    return;\n"
     "  }\n"
     "\n"
     "  /* OTA 扩展：F190 写 01 = 置\"下次复位进 Bootloader\"请求；长度由 Ota_OnWriteBootReq 校验 */\n"
     "  if ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) == UDS_DID_BOOT_REQ)\n"
     "  {\n"
     "    Ota_OnWriteBootReq(req, len);\n"
     "    return;\n"
     "  }\n"
     "\n"
     "  if ((len != 4U) || ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) != UDS_DID_LED))\n"
     "  {\n"
     "    Uds_SendNrc(0x2EU, 0x31U);\n"
     "    return;\n"
     "  }\n"
     "\n"
     "  App_SetLed(req[3]);"),
    ("    case 0x19U: Uds_Service19(req, len); break;\n"
     "    case 0x3EU:                                   /* TesterPresent */",
     "    case 0x19U: Uds_Service19(req, len); break;\n"
     "    /* ---- OTA 刷写服务（会话约束在各 handler 内部检查）---- */\n"
     "    case 0x11U: Ota_OnEcuReset(req, len);        break;   /* ECUReset      */\n"
     "    case 0x31U: Ota_OnRoutineControl(req, len);  break;   /* RoutineControl*/\n"
     "    case 0x34U: Ota_OnRequestDownload(req, len); break;   /* RequestDownload*/\n"
     "    case 0x36U: Ota_OnTransferData(req, len);    break;   /* TransferData  */\n"
     "    case 0x37U: Ota_OnTransferExit(req, len);    break;   /* TransferExit  */\n"
     "    case 0x3EU:                                   /* TesterPresent */"),
]

# console.c：三处
CONSOLE_C = [
    ('#include "uds.h"',
     '#include "uds.h"\n#include "ota_tunnel.h"\n#include "ota_agent.h"'),
    ('  else if (strcmp(cmd, "tick") == 0)\n'
     '  {\n'
     '    Cmd_Tick();\n'
     '  }',
     '  else if (strcmp(cmd, "tick") == 0)\n'
     '  {\n'
     '    Cmd_Tick();\n'
     '  }\n'
     '  else if (strcmp(cmd, "tun") == 0)                 /* OTA：串口<->诊断CAN隧道 */\n'
     '  {\n'
     '    (void)OtaTunnel_OnLine("tun", args);\n'
     '  }\n'
     '  else if (strcmp(cmd, "tx") == 0)                  /* OTA：隧道下发原始CAN帧 */\n'
     '  {\n'
     '    (void)OtaTunnel_OnLine("tx", args);\n'
     '  }\n'
     '  else if (strcmp(cmd, "ota") == 0)                 /* OTA：状态/槽位/版本 */\n'
     '  {\n'
     '    Ota_PrintStatus();\n'
     '  }'),
    ('  Put("  led                LED/GPIOE diagnose: regs + toggle test\\r\\n");',
     '  Put("  led                LED/GPIOE diagnose: regs + toggle test\\r\\n");\n'
     '  Put("  ota                OTA state / slot / version\\r\\n");\n'
     '  Put("  tun <on|off>       serial<->diagnostic-CAN tunnel (PC OTA tool)\\r\\n");\n'
     '  Put("  tx <id> <n> <hex>  send raw frame, e.g. tx 7E0 8 02 10 03 00 00 00 00 00\\r\\n");'),
]

# uvprojx：三处（BL 工程的生成脚本会处理 IncludePath；这里补 IROM 与文件注册）
UVPROJX = [
    ("<IncludePath>../Core/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc;"
     "../Drivers/STM32G4xx_HAL_Driver/Inc/Legacy;"
     "../Drivers/CMSIS/Device/ST/STM32G4xx/Include;../Drivers/CMSIS/Include;"
     "..\\Drivers\\User\\Inc</IncludePath>",
     "<IncludePath>../Core/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc;"
     "../Drivers/STM32G4xx_HAL_Driver/Inc/Legacy;"
     "../Drivers/CMSIS/Device/ST/STM32G4xx/Include;../Drivers/CMSIS/Include;"
     "..\\Drivers\\User\\Inc;..\\..\\ota\\common</IncludePath>"),
    ("              <IROM>\n                <Type>1</Type>\n"
     "                <StartAddress>0x8000000</StartAddress>\n"
     "                <Size>0x80000</Size>\n              </IROM>",
     "              <IROM>\n                <Type>1</Type>\n"
     "                <!-- ⚠️ 过期值，请勿使用（分区与保留区均已变更，见血线 12）。\n"
     "                     APP 链接基址 = 槽基址 + OTA_IMG_HDR_SIZE = 槽基址 + 0x80，**不是 +0x20**；\n"
     "                     用 +0x20 会让 VTOR 被吞成槽基址 -> 向量表落到镜像头上 -> lockup。\n"
     "                     现行正确值见 ota/common/ota_common.h: 0x0800C080 / 0x33F80 -->\n"
     "                <StartAddress>0x800C020</StartAddress>\n"
     "                <Size>0x33FE0</Size>\n              </IROM>"),
    ("            <File>\n              <FileName>uds.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\uds.c</FilePath>\n            </File>",
     "            <File>\n              <FileName>uds.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\uds.c</FilePath>\n            </File>\n"
     "            <File>\n              <FileName>ota_agent.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\ota_agent.c</FilePath>\n            </File>\n"
     "            <File>\n              <FileName>ota_tunnel.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\ota_tunnel.c</FilePath>\n            </File>"),
]

# 另外两处必须存在的宏（uvprojx 的 <Define>）
UVPROJX_DEFINE_OLD = "<Define>USE_HAL_DRIVER,STM32G474xx</Define>"
UVPROJX_DEFINE_NEW = ("<Define>USE_HAL_DRIVER,STM32G474xx,"
                      "USER_VECT_TAB_ADDRESS,VECT_TAB_OFFSET=0xC020</Define>")

JOBS = [
    ('G474/Drivers/User/Src/can.c', CAN_C),
    ('G474/Drivers/User/Src/uds.c', UDS_C),
    ('G474/Drivers/User/Src/console.c', CONSOLE_C),
    ('G474/MDK-ARM/G474.uvprojx', UVPROJX + [(UVPROJX_DEFINE_OLD, UVPROJX_DEFINE_NEW)]),
]


def eol_of(raw):
    crlf = raw.count(b'\r\n')
    lf = raw.count(b'\n') - crlf
    return b'\r\n' if crlf > lf else b'\n'


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    archive = sys.argv[1]
    apply_ = '--apply' in sys.argv
    if not os.path.isdir(archive):
        print('!! 归档目录不存在: %s' % archive)
        return 2

    ok = bad = 0
    for rel, patches in JOBS:
        src = os.path.join(archive, rel.replace('/', os.sep))
        dst = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isfile(src):
            print('!! 归档中缺 %s' % rel); bad += 1; continue
        raw_bytes = open(src, 'rb').read()
        eol = eol_of(raw_bytes).decode()
        text = raw_bytes.decode('utf-8', 'replace')
        for old, new in patches:
            ob = old.replace('\n', eol)
            nb = new.replace('\n', eol)
            n = text.count(ob)
            if n != 1:
                print('  !! %s: 锚点匹配 %d 次(期望1): %s' % (rel.split("/")[-1], n, old.split('\n')[0][:56]))
                bad += 1
                continue
            text = text.replace(ob, nb, 1)
            ok += 1
        if apply_:
            with open(dst, 'w', encoding='utf-8', newline='') as fh:
                fh.write(text)
        print('  %s %s' % ('已恢复' if apply_ else '待恢复', rel))

    print()
    print('%s: %d 处补丁应用，%d 处失败' % ('已应用' if apply_ else '干跑', ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
