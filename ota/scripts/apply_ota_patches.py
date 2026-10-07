#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OTA 改动落地脚本（字节级、逐处校验）。

设计原则：
  1. 每处替换要求"恰好匹配 1 次"，否则整脚本中止、不写任何文件（避免静默改错）；
  2. 保持各文件原有行尾（CRLF / LF），避免产生整文件 diff；
  3. 先备份到 _baseline_backup\，可随时回退；
  4. 只做文本替换，不重新格式化。

用法:  python tools_apply_ota_patches.py [--check]
       --check  只校验所有锚点可匹配，不写文件
"""
import argparse
import os

from paths import workspace_root
import shutil
import sys

ROOT = workspace_root()
BACKUP = os.path.join(ROOT, "_baseline_backup")

# ---------------------------------------------------------------------------
# 补丁定义： (文件, 说明, 原文, 新文, 期望匹配次数)
# 原文/新文一律用 \n 书写，写入前按目标文件的原始行尾转换。
# ---------------------------------------------------------------------------
PATCHES = [
    # ============ 1) isotp.h : 吞吐优化 128 -> 1024 ============
    ("G474/Drivers/User/Inc/isotp.h",
     "ISOTP_MAX_MSG 128 -> 1024（吞吐优化，块净荷 125->1021）",
     "#define ISOTP_MAX_MSG            128U   /* 应用层最大报文长度 */",
     "#define ISOTP_MAX_MSG            1024U  /* 应用层最大报文长度 */\n"
     "/* 2026 吞吐优化：128 -> 1024。单块净荷 = 1024 - 1(SID) - 1(blockSeq) - 1(余量) = 1021 字节，\n"
     " * 满槽 224KB 镜像的 0x36 报文数由 1835 降到 225（端到端 ~76s -> ~9s）。\n"
     " * ⚠️ 必须与 ota/common/ota_common.h 的 OTA_ISOTP_MAX_MSG 保持一致（该头文件有 #if 断言）；\n"
     " * ⚠️ 同时必须放大 uds.c 里 Uds_Service22() 的应答缓冲，见其注释。 */",
     1),

    # ============ 2) can.c : 放开诊断过滤器（收 0x7E8） ============
    ("G474/Drivers/User/Src/can.c",
     "StdFiltersNbr 2 -> 3（新增 0x7E8 过滤器，OTA/诊断应答必需）",
     "  hfdcan2.Init.StdFiltersNbr                  = (s_promisc || s_loopbackSelfTest) ? 1U : 2U;",
     "  /* OTA 扩展：正常模式需要同时收 0x7E0(请求) 与 0x7E8(应答) 两个诊断 ID。\n"
     "   * 一条掩码型过滤器只能匹配\"一个 ID + 一个掩码\"（FilterID2 是掩码而非第二个 ID），\n"
     "   * 因此由 2 个过滤器增加到 3 个（业务帧 + 0x7E0 + 0x7E8）。 */\n"
     "  hfdcan2.Init.StdFiltersNbr                  = (s_promisc || s_loopbackSelfTest) ? 1U : 3U;",
     1),

    ("G474/Drivers/User/Src/can.c",
     "追加第三个过滤器：诊断应答 0x7E8",
     "    filterConfig.FilterID1   = ISOTP_DIAG_REQ_ID;       /* 板B接收诊断请求0x7E0 */\n"
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
     "#endif",
     1),

    # ============ 3) uds.c : 应答缓冲放大 + 5 个新服务 + F190 ============
    ("G474/Drivers/User/Src/uds.c",
     "包含 OTA 代理头",
     '#include "can.h"\n#include "bsp_uart.h"\n#include <string.h>',
     '#include "can.h"\n#include "bsp_uart.h"\n#include "ota_agent.h"   /* OTA：0x34/0x36/0x37/0x31/0x11 与 F180~F190 */\n#include <string.h>',
     1),

    ("G474/Drivers/User/Src/uds.c",
     "Uds_Service22 的 resp[64] -> resp[ISOTP_MAX_MSG]（否则大 DID 越界写栈）",
     "  uint8_t  resp[64];\n  uint16_t rlen = 0U;",
     "  /* OTA 扩展：ISOTP_MAX_MSG 已提升到 1024，应答缓冲必须同步放大，\n"
     "   * 否则一旦某个 DID 的应答超过 64 字节，rlen 会越界写栈（静默破坏内存）。 */\n"
     "  uint8_t  resp[ISOTP_MAX_MSG];\n"
     "  uint16_t rlen = 0U;",
     1),

    ("G474/Drivers/User/Src/uds.c",
     "Uds_Service22 增加 OTA 只读 DID（F180~F184）兜底",
     "    case UDS_DID_LED:\n"
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
     "    }",
     1),

    ("G474/Drivers/User/Src/uds.c",
     "Uds_Service2E 放宽以支持 F190（请求下次进 Bootloader）",
     "  if ((len != 4U) || ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) != UDS_DID_LED))\n"
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
     "  App_SetLed(req[3]);",
     1),

    ("G474/Drivers/User/Src/uds.c",
     "Uds_ServerOnMsg 注册 OTA 五个新服务",
     "    case 0x19U: Uds_Service19(req, len); break;\n"
     "    case 0x3EU:                                   /* TesterPresent */",
     "    case 0x19U: Uds_Service19(req, len); break;\n"
     "    /* ---- OTA 刷写服务（会话约束在各 handler 内部检查）---- */\n"
     "    case 0x11U: Ota_OnEcuReset(req, len);        break;   /* ECUReset      */\n"
     "    case 0x31U: Ota_OnRoutineControl(req, len);  break;   /* RoutineControl*/\n"
     "    case 0x34U: Ota_OnRequestDownload(req, len); break;   /* RequestDownload*/\n"
     "    case 0x36U: Ota_OnTransferData(req, len);    break;   /* TransferData  */\n"
     "    case 0x37U: Ota_OnTransferExit(req, len);    break;   /* TransferExit  */\n"
     "    case 0x3EU:                                   /* TesterPresent */",
     1),

    # ============ 4) main.c : 接线（hiwdg 去 static / Ota 初始化 / 主循环 / 隧道回吐）============
    ("G474/Core/Src/main.c",
     "包含 OTA 头",
     '#include "isotp.h"\n#include "uds.h"',
     '#include "isotp.h"\n#include "uds.h"\n#include "ota_agent.h"\n#include "ota_tunnel.h"',
     1),

    ("G474/Core/Src/main.c",
     "hiwdg 去 static（ota_agent.c 需在 Flash 页编程前喂狗）",
     "static IWDG_HandleTypeDef hiwdg;                   /* 独立看门狗：LSI/32=1kHz，重装载3s */",
     "IWDG_HandleTypeDef hiwdg;                          /* 独立看门狗：LSI/32=1kHz，重装载3s\n"
     "                                                    * 非 static：ota_agent.c 在 Flash 擦写前喂狗需访问 */",
     1),

    ("G474/Core/Src/main.c",
     "main() 初始化 OTA 与隧道",
     "  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */",
     "  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */\n"
     "  OtaTunnel_Init();                                /* 串口<->诊断CAN隧道（默认关闭） */\n"
     "  Ota_Init();                                      /* OTA：读控制区/识别本槽/置试运行待确认 */",
     1),

    ("G474/Core/Src/main.c",
     "主循环增加 Ota_Task",
     "    Uds_Task();                                    /* UDS：S3会话/客户端超时 */\n"
     "    HAL_IWDG_Refresh(&hiwdg);                      /* 喂狗（主循环健康证明） */",
     "    Uds_Task();                                    /* UDS：S3会话/客户端超时 */\n"
     "    Ota_Task();                                    /* OTA：试运行自检通过后落定 TRIAL->IDLE */\n"
     "    HAL_IWDG_Refresh(&hiwdg);                      /* 喂狗（主循环健康证明） */",
     1),

    ("G474/Core/Src/main.c",
     "App_Process 增加隧道帧回吐（须在 App_HandleRx 之前）",
     "    if (Console_SniffEnabled())\n"
     "    {\n"
     "      Console_PrintFrame(&s_rxMsg);\n"
     "    }\n"
     "    App_HandleRx();",
     "    if (Console_SniffEnabled())\n"
     "    {\n"
     "      Console_PrintFrame(&s_rxMsg);\n"
     "    }\n"
     "    OtaTunnel_PrintFrame(&s_rxMsg);                /* 隧道开启时回吐 0x7E0/0x7E8（原始帧） */\n"
     "    App_HandleRx();",
     1),

    # ============ 5) console.c : tun / tx / ota 命令 ============
    ("G474/Drivers/User/Src/console.c",
     "包含隧道头",
     '#include "uds.h"',
     '#include "uds.h"\n#include "ota_tunnel.h"\n#include "ota_agent.h"',
     1),

    ("G474/Drivers/User/Src/console.c",
     "Console_Execute 增加 tun/tx/ota 命令",
     '  else if (strcmp(cmd, "tick") == 0)\n'
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
     '  }',
     1),

    ("G474/Drivers/User/Src/console.c",
     "help 增加三条命令说明",
     '  Put("  led                LED/GPIOE diagnose: regs + toggle test\\r\\n");',
     '  Put("  led                LED/GPIOE diagnose: regs + toggle test\\r\\n");\n'
     '  Put("  ota                OTA state / slot / version\\r\\n");\n'
     '  Put("  tun <on|off>       serial<->diagnostic-CAN tunnel (PC OTA tool)\\r\\n");\n'
     '  Put("  tx <id> <n> <hex>  send raw frame, e.g. tx 7E0 8 02 10 03 00 00 00 00 00\\r\\n");',
     1),

    # ============ 6) system_stm32g4xx.c : APP 向量表偏移 ============
    ("G474/Core/Src/system_stm32g4xx.c",
     "打开 USER_VECT_TAB_ADDRESS 并设 VECT_TAB_OFFSET=0x8020（Slot A）",
     "/* #define USER_VECT_TAB_ADDRESS */",
     "/* OTA 扩展：本 APP 链接在 Slot A（0x08008020），必须重定位向量表。\n"
     " * Slot B 版改为 0x00040820U。Bootloader 侧保持默认（偏移 0）。 */\n"
     "#define USER_VECT_TAB_ADDRESS",
     1),

    ("G474/Core/Src/system_stm32g4xx.c",
     "VECT_TAB_OFFSET 0 -> 0x8020（仅 FLASH 版，SRAM 版不动）",
     "#define VECT_TAB_BASE_ADDRESS   FLASH_BASE      /*!< Vector Table base address field.\n"
     "                                                     This value must be a multiple of 0x200. */\n"
     "#define VECT_TAB_OFFSET         0x00000000U     /*!< Vector Table base offset field.\n"
     "                                                     This value must be a multiple of 0x200. */",
     "#define VECT_TAB_BASE_ADDRESS   FLASH_BASE      /*!< Vector Table base address field.\n"
     "                                                     This value must be a multiple of 0x200. */\n"
     "#define VECT_TAB_OFFSET         0x00008020U     /*!< Vector Table base offset field.\n"
     "                                                     OTA: Slot A APP 链接基址 0x08008020\n"
     "                                                     （Slot B 版改 0x00040820U） */",
     1),

    # ============ 7) uvprojx : IncludePath / IROM1 / 注册新源文件 ============
    ("G474/MDK-ARM/G474.uvprojx",
     "IncludePath 增加契约头目录 ../../ota/common",
     "<IncludePath>../Core/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc/Legacy;"
     "../Drivers/CMSIS/Device/ST/STM32G4xx/Include;../Drivers/CMSIS/Include;..\\Drivers\\User\\Inc</IncludePath>",
     "<IncludePath>../Core/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc;../Drivers/STM32G4xx_HAL_Driver/Inc/Legacy;"
     "../Drivers/CMSIS/Device/ST/STM32G4xx/Include;../Drivers/CMSIS/Include;..\\Drivers\\User\\Inc;..\\..\\ota\\common</IncludePath>",
     1),

    # ⚠️【L1 修正，2026-10-07】原补丁写的是 0x08008020 / 0x37FE0（槽基址 + **32B**），
    #   这是**错误**的值：本项目曾因"32 vs 0x80"变砖（Cortex-M4 SCB->VTOR[6:0] 是 RES0，
    #   向量表必须 0x80 对齐，见血线 12）。APP 链接基址必须是槽基址 + 0x80。
    #   另：本条目用的是**旧分区**（槽基 0x08008000 / 224KB），已与现行契约
    #   （槽基 0x0800C000 / 208KB，见 ota/common/ota_common.h）不符 ——
    #   整个脚本属于历史恢复工具，勿直接用于新工程。正确值以契约为唯一真相源。
    ("G474/MDK-ARM/G474.uvprojx",
     "IROM1 起始/大小 -> Slot A（**已过期：旧分区 0x08008000/224KB，现行为 0x0800C000/208KB**）",
     "              <IROM>\n                <Type>1</Type>\n                <StartAddress>0x8000000</StartAddress>\n"
     "                <Size>0x80000</Size>\n              </IROM>",
     "              <IROM>\n                <Type>1</Type>\n"
     "                <!-- ⚠️ 过期补丁，请勿使用（分区与保留区均已变更）。\n"
     "                     APP 链接基址 = 槽基址 + OTA_IMG_HDR_SIZE = 槽基址 + 0x80（**不是 +0x20**），\n"
     "                     否则 VTOR 被吞 -> 向量表落到镜像头上 -> lockup（见血线 12）。\n"
     "                     现行正确值见 ota/common/ota_common.h: OTA_SLOT_A_ADDR + 0x80 = 0x0800C080 -->\n"
     "                <StartAddress>0x8008020</StartAddress>\n"
     "                <Size>0x37FE0</Size>\n              </IROM>",
     1),

    ("G474/MDK-ARM/G474.uvprojx",
     "注册新源文件 ota_agent.c / ota_tunnel.c",
     "            <File>\n              <FileName>uds.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\uds.c</FilePath>\n            </File>",
     "            <File>\n              <FileName>uds.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\uds.c</FilePath>\n            </File>\n"
     "            <File>\n              <FileName>ota_agent.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\ota_agent.c</FilePath>\n            </File>\n"
     "            <File>\n              <FileName>ota_tunnel.c</FileName>\n              <FileType>1</FileType>\n"
     "              <FilePath>..\\Drivers\\User\\Src\\ota_tunnel.c</FilePath>\n            </File>",
     1),
]


def detect_eol(raw: bytes) -> bytes:
    """返回该文件的主行尾（b'\r\n' 或 b'\n'）。"""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return b"\r\n" if crlf > lf else b"\n"


def apply():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验锚点，不写文件")
    args = ap.parse_args()

    # 先全部校验
    staged = {}
    errors = []
    for rel, desc, old, new, want in PATCHES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            errors.append("%s: 文件不存在" % rel)
            continue
        raw = staged.get(rel)
        if raw is None:
            raw = open(path, "rb").read()
        eol = detect_eol(raw)
        ob = old.replace("\n", eol.decode()).encode("utf-8")
        nb = new.replace("\n", eol.decode()).encode("utf-8")
        cnt = raw.count(ob)
        if cnt != want:
            errors.append("%s\n    说明: %s\n    期望匹配 %d 次，实际 %d 次\n    锚点首行: %s"
                          % (rel, desc, want, cnt, old.split("\n")[0][:90]))
            continue
        staged[rel] = raw.replace(ob, nb, want)
        if args.check:
            continue

    if errors:
        print("!! 校验失败，未写入任何文件：")
        for e in errors:
            print("  - " + e)
        return 2

    if args.check:
        print("所有 %d 处锚点均可匹配（未写文件）" % len(PATCHES))
        return 0

    os.makedirs(BACKUP, exist_ok=True)
    for rel, data in staged.items():
        path = os.path.join(ROOT, rel)
        bkp = os.path.join(BACKUP, rel.replace("/", "__").replace("\\", "__"))
        if not os.path.exists(bkp):
            shutil.copy2(path, bkp)
        with open(path, "wb") as fh:
            fh.write(data)
        print("  已改 %-46s (%d bytes -> %d)" % (rel, os.path.getsize(bkp), len(data)))
    print("\n完成：共修改 %d 个文件，%d 处替换。备份在 %s" % (len(staged), len(PATCHES), BACKUP))
    return 0


if __name__ == "__main__":
    sys.exit(apply())
