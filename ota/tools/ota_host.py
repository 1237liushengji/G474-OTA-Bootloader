#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STM32G474 OTA 上位机刷写工具（PC 侧：串口行协议隧道 + ISO-TP/UDS）
==================================================================
链路拓扑::

    PC(本工具) --USART1 115200 8N1--> 桥节点 console(行协议)
               桥节点 --经典CAN 500kbit/s--> ECU(应用固件)

桥节点在隧道开启期间把 CAN 总线上收到的诊断帧打印成
``RXTX <id3hex> <dlc> <hex...>``；本工具用 ``TX <id3hex> <n> <hex...>`` 把诊断帧
发到总线。ISO-TP 拆包/流控、UDS 服务、块序号与 CRC 校验全部在 PC 侧完成，
因此本文件与固件实现解耦，可用 --dry-run / --replay 在无硬件环境下自证。

标准刷写流程（Lead 仲裁后冻结，--boot-request 默认开启 = 真机冷启动安全路径）::

    10 03                        切扩展会话（APP 内）
    [--precheck] 22 F180/F181/F182/F183/F184  读 OTA状态/活动槽/版本/双槽CRC/能力
    2E F1 90 01                  置"下次进 Bootloader"请求
    11 01                        ECUReset → 等待设备重启进入 BL（BL 启动窗口约 1s）
    10 03 ...                    轮询重建会话：100ms 间隔连发 10 03 直到 50 03，预算 3s
    31 01 FF00                   擦除目标槽(--resume N 时自动跳过, 避免擦掉已写入数据)
    34 00 44 <addr4> <size4>     请求下载(addr=槽 APP 基址+偏移, size=剩余净荷字节;
                                 SlotA APP=0x0800C020 / SlotB APP=0x08040820)
    36 <seq> <data...> * N       分块传输(默认块 1021 字节, 单报文 <=1024 字节)
    37 <payloadCrc32 大端4字节>  传输结束并携带整镜像 payloadCrc(不含 32 字节头)
    [--verify] 31 01 FF01        回读 CRC 与镜像 appCrc32 比对
    31 01 FF02                   激活/提交(设备侧据此合成槽首 32B 镜像头)
    11 01                        ECU 复位(--no-reset 可跳过)
    [--commit] 31 01 FF04        复位后新固件试运行启动: 先 10 03 再落定(TRIAL→IDLE)

热升级（`--no-boot-request`）：跳过 2E/11 01 与重建会话，10 03 后直接 34/36/37，由 APP
把固件写入备用槽，省一次复位但要求 APP 在线且允许写非活动槽。

真机注意（BL 启动窗口）::

    复位后 Bootloader 只监听约 1 秒。若窗口内没建立起会话，设备会跳转 APP，再刷写
    就会被 NRC 0x22（conditionsNotCorrect）拒绝。因此本工具用 100ms 间隔轮询 10 03
    直到收到 50 03；超时会明确提示"设备可能已跳转 APP，请断电重试或使用
    --no-boot-request 热升级"。`--session-retry` 可调整总预算（默认 3s）。

契约要点（与 ota/common/ota_common.h 冻结值一致，不得改动）::

    * 分区: BL 独占 0x08000800~0x0800B7FF；Slot A 槽基 0x0800C000、Slot B 槽基
      0x08040800，单槽 208KB(0x34000)。0x34 目标 = 槽基址 + OTA_IMG_HDR_SIZE
      （当前 = 槽基址 + **0x80**，不是 +32B；见血线 12——头结构体 32B，
      槽首保留区 0x80，因为 VTOR 必须 0x80 对齐）= APP 链接基址。
    * 吞吐优化: 应用报文上限 OTA_ISOTP_MAX_MSG = 1024 字节；0x36 单块净荷上限
      UDS_DL_MAX_BLOCK_PAYLOAD = 1016（= (1024-3) & ~7，向下对齐到 8 字节）。
      **必须 8 字节倍数**：STM32G4Flash 只支持双字(8B)编程，设备要求续传起始
      地址 8 对齐；若块大小非 8 倍数（如 1021，mod 8 = 5），--resume N 的偏移
      (N-1)*1021 在 8 个连续起点里有 7 个不对齐 -> 设备回 NRC 0x31。
      0x34 通告的 maxNumberOfBlockLength 亦为 1016。净荷 1016 时 0x36 报文
      1018 字节 <= 1024（ISO-TP 合法）。默认块大小 = 1016。
    * 32 字节头**结构体**只在本机做校验与携带元数据，**不进 0x34/0x36 传输流**；
      传输的净荷就是 app_data（0x34 的 size = len(app_data)）。注意 32 是结构体
      大小，与 0x80 保留区是两回事。
    * 0x37 必须带 4 字节大端 payloadCrc32（= crc32(app_data)），设备用它作为
      FF01 参考值并写进合成镜像头；不带时设备只能按长度校验。
    * 0x31 01 FF01 应答: [0x71,0x01,0xFF,0x01,status(00通过/01 CRC不符/02 长度不符),
      crc32(4B 大端)]；也兼容 4/5 字节（无回读 CRC，按 status 判定）。status 与
      回读 CRC 都给出时，以本机对镜像的 CRC 交叉核对为准。
    * blockSeq = ((blockIndex-1) % 255) + 1（255 后回 1，0 非法）；收到 NRC 0x73
      视为块序号不符，重发该块（--block-retry 控制次数）。
    * --resume N: 跳过擦除，0x34 addr = 槽地址+(N-1)*blockSize、size = 剩余净荷，
      FF01 比对 crc32(app_data[(N-1)*blockSize:])（设备按所声明区段重算）。

离线自证（无硬件、无网络）::

    --dry-run        内置确定性 ECU 模拟器，完整打印将发送的报文序列并自校验
    --replay LOG     从保存的 << / >> 收发日志离线重建并校验整条刷写流程

依赖: Python3 标准库；仅真实串口模式需要 ``pip install pyserial``（缺失时给出
清晰提示，不会抛栈）。
"""

import argparse
import os
import re
import struct
import sys
import time
import zlib
from collections import deque
from dataclasses import dataclass, field

# ============================== 1. 常量（协议契约，勿改） ==============================

TOOL_NAME = "ota_host"
TOOL_VERSION = "1.0"

# ---- OTA 镜像格式（32 字节小端头）----
OTA_MAGIC = b"G4OT"                 # [0:4]  0x47 0x34 0x4F 0x54
OTA_HEADER_SIZE = 0x80
OTA_OFF_IMG_SIZE = 4                # [4:8]  u32 含整头
OTA_OFF_APP_SIZE = 8                # [8:12] u32 纯固件
OTA_OFF_VERSION = 12                # [12:14] major, minor
OTA_OFF_RESERVED0 = 14              # [14:16] 保留 0
OTA_OFF_LOAD_ADDR = 16              # [16:20] u32
OTA_OFF_APP_CRC = 20                # [20:24] u32 标准 CRC32
OTA_OFF_HDR_CRC = 24                # [24:28] u32 对 [0:24] 计算
OTA_OFF_RESERVED1 = 28              # [28:32] 保留 0
# ---- 分区（与 ota/common/ota_common.h 冻结值逐项一致）----
# 槽基址含槽首保留区 OTA_IMG_HDR_SIZE（**0x80**，不是头结构体的 32B）；
# 0x34 的 addr 用"APP 链接基址" = 槽基址 + OTA_HEADER_SIZE。
# ⚠️ 血线 12：头结构体 32B ≠ 槽首保留区 0x80。本项目曾因"32 vs 0x80"变砖
#    （Cortex-M4 SCB->VTOR[6:0] 是 RES0，向量表必须 0x80 对齐）。凡涉及
#    "APP 基址 = 槽基址 + N"的表述，N 一律用 OTA_HEADER_SIZE（0x80）。
OTA_SLOT_SIZE = 0x34000                     # 单槽 208KB
OTA_SLOT_A_BASE = 0x0800C000                # Slot A 槽基址（BL 占 0x08000800~0x0800B7FF）
OTA_SLOT_B_BASE = 0x08040800                # Slot B 槽基址（Bank2 起始，取指不跨 Bank）
OTA_SLOT_A_APP_ADDR = OTA_SLOT_A_BASE + OTA_HEADER_SIZE   # 0x0800C080（0x34 目标）
OTA_SLOT_B_APP_ADDR = OTA_SLOT_B_BASE + OTA_HEADER_SIZE   # 0x08040880（0x34 目标）
OTA_SLOT_ADDRS = {"A": OTA_SLOT_A_APP_ADDR, "B": OTA_SLOT_B_APP_ADDR}
OTA_SLOT_BASES = {"A": OTA_SLOT_A_BASE, "B": OTA_SLOT_B_BASE}
OTA_IMG_MAX_PAYLOAD = OTA_SLOT_SIZE - OTA_HEADER_SIZE     # 单槽最大净荷 = 208KB - 0x80 槽首保留区
OTA_DEFAULT_LOAD_ADDR = OTA_SLOT_A_APP_ADDR  # 0x34 传输起始地址（--slot/--load-addr 可覆盖）

# ---- ISO-TP（ISO 15765-2, 经典 CAN 8 字节）----
CAN_PAYLOAD_LEN = 8
# 吞吐优化契约（ota_common.h: OTA_ISOTP_MAX_MSG / UDS_DL_MAX_BLOCK_PAYLOAD / UDS_MAXLEN_FIELD）
#   应用报文上限 1024；0x36 单块净荷上限 = 1024 - 1(SID) - 1(blockSeq) - 1(余量) = 1020，
#   再向下对齐到 8 字节 = 1016。
#   ⚠️ 8 字节对齐是硬约束：STM32G4 Flash 只支持双字(8B)编程，设备要求续传起始地址 8 对齐。
#      若块大小非 8 倍数（如 1021，mod 8 = 5），则 --resume N 的偏移 (N-1)*1021
#      在 8 个连续起点里有 7 个不对齐 → 设备回 NRC 0x31，续传形同虚设。
#      1016 + 2(SID+seq) = 1018 <= 1024，吞吐仅损失 0.5%。推导式与契约 ota_common.h 一致。
ISOTP_MAX_MSG = 1024                # 与固件 ISOTP_MAX_MSG / OTA_ISOTP_MAX_MSG 一致
ISOTP_REQ_ID = 0x7E0                # 诊断仪 -> ECU
ISOTP_RESP_ID = 0x7E8               # ECU -> 诊断仪
ISOTP_FC_CTS = 0x0                  # 流控 FS=继续
ISOTP_FC_WAIT = 0x1                 # 流控 FS=等待
ISOTP_FC_OVFLW = 0x2                # 流控 FS=溢出
ISOTP_FC_BS = 0x00                  # 主机回 FC 的块大小(0=不限块, 与 isotp.c 一致)
ISOTP_FC_STMIN_MS = 0x02            # 主机回 FC 的最小间隔(2ms, 与 isotp.c 一致)
ISOTP_DEFAULT_BLOCK_SIZE = (ISOTP_MAX_MSG - 3) & ~7   # 1016: 0x36 报文 = 2+1016 = 1018 <= 1024，且 8 字节对齐
ISOTP_MAX_BLOCK_SIZE = ISOTP_DEFAULT_BLOCK_SIZE  # 契约上限 1016（8 字节倍数，非 8 倍数会使续传不可用）
BLOCK_SEQ_MOD = 255                 # 块序号 1..255 循环

# ---- 真机物理约束（提示与预检用，来源见注释）----
# 桥节点 can.c 的 CAN_RX_RING_LEN = 16：隧道模式下每个 CAN 帧要打印一行 RXTX
# （约 26~30 字符 @115200 ≈ 2.3ms），而 CF 到达间隔由 STmin 固定为 2ms，
# 打印速度追不上接收速度，只能靠这 16 深度环形缓冲撑住。
NODE_RX_RING_DEPTH = 16
# 隧道模式建议的起步块大小（仍 >16 帧/块，但把突发减半；随后用 22 F002 看 rxOverruns）
TUNNEL_SUGGESTED_BLOCK_SIZE = 500
# 擦除耗时估算（保守）：G4 页大小 2048B，单页擦除典型 20~40ms，取 25ms
FLASH_PAGE_SIZE = 2048
FLASH_PAGE_ERASE_MS = 25

# ---- UDS（ISO 14229）----
UDS_SUPPRESS_POS_RSP = 0x80
UDS_SESSION_DEFAULT = 0x01
UDS_SESSION_EXTENDED = 0x03
UDS_ROUTINE_ERASE = 0xFF00          # 擦除
UDS_ROUTINE_VERIFY = 0xFF01         # 校验(回读 CRC)
UDS_ROUTINE_ACTIVATE = 0xFF02       # 激活/提交(置试运行)
UDS_ROUTINE_ROLLBACK = 0xFF03       # 回滚
UDS_ROUTINE_COMMIT = 0xFF04         # 试运行确认: TRIAL_x -> IDLE_x（需扩展会话）
UDS_DID_BOOT_REQ = 0xF190           # 2E F190 01 = 置"下次进 Bootloader"请求
UDS_BOOT_REQ_VALUE = 0x01
# 复位后重建会话：BL 启动窗口约 1s，100ms 间隔连发 10 03 直到 50 03，总预算 3s
SESSION_RETRY_INTERVAL = 0.1
SESSION_RETRY_TOTAL = 3.0
SESSION_ATTEMPT_TIMEOUT = 0.3       # 单次 10 03 等 50 03 的超时（避免一次吃掉全部预算）
UDS_DEFAULT_PRECHECK_DID = 0xF180   # 22 预检默认从 F180 起
# OTA 相关 DID（ota_common.h 冻结）：
#   F180 OTA状态(1B) / F181 活动槽(1B,0=A) / F182 版本(4B) / F183 双槽CRC(8B) / F184 能力位图(1B)
UDS_PRECHECK_DIDS = (0xF180, 0xF181, 0xF182, 0xF183, 0xF184)
UDS_RSP_PENDING = 0x78              # NRC 0x78 requestCorrectlyReceived-ResponsePending
UDS_XFEREXIT_WITH_CRC = True        # 0x37 必须携带 4 字节大端 payloadCrc32
UDS_VERIFY_RESP_FULL_LEN = 9        # 71 01 FF 01 status crc32(4)
UDS_VERIFY_RESP_MIN_LEN = 4         # 仅够识别 71 01 FF 01（**不含 status**，不能据此判定成败）
# status 字节索引：[0]=71 [1]=01 [2]=rHi [3]=rLo [4]=status
# 契约侧 ota_common.h 的 UDS_VERIFY_RESP_MIN_LEN 应同步为 5（4 字节无法表达 status）。
UDS_VERIFY_RESP_STATUS_IDX = 4
UDS_VERIFY_STATUS_NAMES = {0x00: "通过", 0x01: "CRC不符", 0x02: "长度不符"}

# 【L6】0x78（responsePending）连续等待的**累计**预算，单位秒。
# 单次 receive 的 timeout 会被 0x78 反复重置，若不设累计上限，设备只要一直回
# 0x78 主机就永远等不到最终应答（挂死，且 Ctrl+C 之外无法中断）。
# 量级依据：BL 最长单步是 FF00 擦整个槽 = 104 页 x 25ms = 2.6s，30s 已留足余量。
# 注：当前 BL 固件实际**从不**发 0x78（bl_diag.c 无任何 0x78 发送点，全 bootloader
# 目录 grep 仅命中 CRC 查表常量），故此项属防御性加固，不影响现有真机时序。
UDS_PENDING_TOTAL_BUDGET = 30.0

# ---- 控制台行协议正则 ----
RXTX_PATTERN = re.compile(r"^RXTX ([0-9A-Fa-f]{3}) (\d)((?: [0-9A-Fa-f]{2})*)$")
TX_CMD_PATTERN = re.compile(r"^TX ([0-9A-Fa-f]{3}) (\d)((?: [0-9A-Fa-f]{2})*)$")

# ---- NRC 名称表（便于错误提示）----
NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class Clr:
    """ANSI 颜色（非终端自动降级，与 can_console.py 风格一致）"""
    ENABLED = bool(getattr(sys.stdout, "isatty", lambda: False)())

    @classmethod
    def _w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.ENABLED else s

    @classmethod
    def cyan(cls, s):   return cls._w("36", s)

    @classmethod
    def green(cls, s):  return cls._w("32", s)

    @classmethod
    def yellow(cls, s): return cls._w("33", s)

    @classmethod
    def red(cls, s):    return cls._w("31", s)


# ============================== 2. 异常 ==============================

class OtaError(Exception):
    """本工具所有可预期错误的基类（main 捕获后打印清晰提示，不抛栈）"""


class ImageFormatError(OtaError):
    """OTA 镜像格式错误"""


class TransportError(OtaError):
    """串口/行协议传输错误"""


class IsoTpError(OtaError):
    """ISO-TP 协议错误"""


class IsoTpTooLong(IsoTpError):
    """报文超过 ISOTP_MAX_MSG（1024 字节）——绝不静默截断"""


class IsoTpSnError(IsoTpError):
    """连续帧序号错序"""


class IsoTpOverflow(IsoTpError):
    """对端流控溢出(FC OVFLW)"""


class IsoTpTimeout(IsoTpError):
    """等待响应超时"""


class UdsNegativeResponse(OtaError):
    """UDS 否定应答 7F <sid> <nrc>"""

    def __init__(self, sid, nrc, raw=None):
        self.sid = sid
        self.nrc = nrc
        self.raw = raw
        self.nrc_name = NRC_NAMES.get(nrc, "unknownNrc")
        super().__init__(f"UDS 否定应答: SID=0x{sid:02X} NRC=0x{nrc:02X}({self.nrc_name})")


class ReplayError(OtaError):
    """日志重放错误"""


class ReplayMismatch(ReplayError):
    """重放时主机报文与日志记录不一致"""


# ============================== 3. CRC 与镜像 ==============================

def crc32(data) -> int:
    """标准 CRC-32（反射多项式 0xEDB88320, 初值/终值异或 0xFFFFFFFF）。

    标准向量: b"123456789" -> 0xCBF43926
    """
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


@dataclass
class OtaImage:
    """OTA 镜像（32 字节头**结构体** + 应用固件）

    注意区分两个数字（血线 12）：这里说的是 .ota **文件**里的头结构体大小 32B，
    而设备侧槽首保留区/ APP 链接基址偏移是 OTA_HEADER_SIZE = 0x80。
    """
    app_data: bytes = b""
    version: tuple = (1, 0)
    load_addr: int = OTA_DEFAULT_LOAD_ADDR

    @property
    def app_size(self) -> int:
        return len(self.app_data)

    @property
    def img_size(self) -> int:
        return OTA_HEADER_SIZE + self.app_size

    @property
    def app_crc32(self) -> int:
        return crc32(self.app_data)

    def pack(self) -> bytes:
        """按契约打包成字节流（小端头 + 固件数据）"""
        app = bytes(self.app_data)
        vmaj, vmin = int(self.version[0]) & 0xFF, int(self.version[1]) & 0xFF
        head = bytearray(OTA_HEADER_SIZE)
        head[0:4] = OTA_MAGIC
        struct.pack_into("<I", head, OTA_OFF_IMG_SIZE, OTA_HEADER_SIZE + len(app))
        struct.pack_into("<I", head, OTA_OFF_APP_SIZE, len(app))
        head[OTA_OFF_VERSION] = vmaj
        head[OTA_OFF_VERSION + 1] = vmin
        # [14:16] 保留 0；[16:20] loadAddr
        struct.pack_into("<I", head, OTA_OFF_LOAD_ADDR, int(self.load_addr) & 0xFFFFFFFF)
        struct.pack_into("<I", head, OTA_OFF_APP_CRC, crc32(app))
        struct.pack_into("<I", head, OTA_OFF_HDR_CRC, crc32(bytes(head[0:OTA_OFF_HDR_CRC])))
        # [28:32] 保留 0
        return bytes(head) + app

    def describe(self) -> str:
        vmaj, vmin = self.version
        hdr_crc = struct.unpack_from("<I", self.pack(), OTA_OFF_HDR_CRC)[0]
        return "\n".join([
            "OTA镜像信息",
            f"  magic    : {OTA_MAGIC.decode('ascii')}",
            f"  imgSize  : {self.img_size} 字节(32字节头 + {self.app_size}字节固件)",
            f"  appSize  : {self.app_size} 字节",
            f"  version  : {vmaj}.{vmin}",
            f"  loadAddr : 0x{self.load_addr:08X}",
            f"  appCrc32 : 0x{self.app_crc32:08X}",
            f"  hdrCrc32 : 0x{hdr_crc:08X}",
        ])


def parse_image(data: bytes) -> OtaImage:
    """解析并完整校验 OTA 镜像（magic/imgSize/appCrc32/hdrCrc32 全部校验）"""
    data = bytes(data)
    if len(data) < OTA_HEADER_SIZE:
        raise ImageFormatError(f"镜像过短: {len(data)} 字节 < 32 字节头")
    if data[0:4] != OTA_MAGIC:
        raise ImageFormatError(
            f"magic 错误: {data[0:4].hex(' ')}，应为 {OTA_MAGIC.hex(' ')}('G4OT')")
    if data[OTA_OFF_RESERVED0:OTA_OFF_RESERVED0 + 2] != b"\x00\x00":
        raise ImageFormatError("头 [14:16] reserved 必须为 0")
    if data[OTA_OFF_RESERVED1:OTA_OFF_RESERVED1 + 4] != b"\x00\x00\x00\x00":
        raise ImageFormatError("头 [28:32] reserved 必须为 0")

    img_size = struct.unpack_from("<I", data, OTA_OFF_IMG_SIZE)[0]
    app_size = struct.unpack_from("<I", data, OTA_OFF_APP_SIZE)[0]
    if app_size == 0:
        raise ImageFormatError("appSize 为 0：镜像不含固件数据")
    if img_size != OTA_HEADER_SIZE + app_size:
        raise ImageFormatError(
            f"imgSize({img_size}) != 32 + appSize({app_size}) = {OTA_HEADER_SIZE + app_size}")
    if img_size != len(data):
        raise ImageFormatError(f"文件长度 {len(data)} 与 imgSize {img_size} 不一致")

    app = data[OTA_HEADER_SIZE:OTA_HEADER_SIZE + app_size]
    app_crc = struct.unpack_from("<I", data, OTA_OFF_APP_CRC)[0]
    if crc32(app) != app_crc:
        raise ImageFormatError(
            f"appCrc32 校验失败: 头内 0x{app_crc:08X}, 实算 0x{crc32(app):08X}")
    hdr_crc = struct.unpack_from("<I", data, OTA_OFF_HDR_CRC)[0]
    if crc32(data[0:OTA_OFF_HDR_CRC]) != hdr_crc:
        raise ImageFormatError(
            f"hdrCrc32 校验失败: 头内 0x{hdr_crc:08X}, 实算 0x{crc32(data[0:OTA_OFF_HDR_CRC]):08X}")

    vmaj = data[OTA_OFF_VERSION]
    vmin = data[OTA_OFF_VERSION + 1]
    load_addr = struct.unpack_from("<I", data, OTA_OFF_LOAD_ADDR)[0]
    return OtaImage(app_data=app, version=(vmaj, vmin), load_addr=load_addr)


def build_image(app_data: bytes, version=(1, 0), load_addr=OTA_DEFAULT_LOAD_ADDR) -> bytes:
    """便捷函数：打包 OTA 镜像"""
    return OtaImage(app_data=bytes(app_data), version=tuple(version), load_addr=load_addr).pack()


def make_sample_app(size=4096) -> bytes:
    """生成确定性样例固件数据（测试/无真实 bin 时的演示用，非随机）"""
    if size <= 0:
        raise OtaError("样例固件长度必须 > 0")
    return bytes(((i * 31 + 7) ^ (i >> 3) ^ (i % 251)) & 0xFF for i in range(size))


def make_sample_image(size=4096, version=(1, 0), load_addr=OTA_DEFAULT_LOAD_ADDR) -> bytes:
    """生成确定性样例 OTA 镜像"""
    return build_image(make_sample_app(size), version=version, load_addr=load_addr)


def load_image(path: str) -> OtaImage:
    """读文件 + 解析校验；IO/格式错误统一转 OtaError"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise OtaError(f"读取镜像失败: {path}: {exc}") from None
    return parse_image(data)


# ============================== 4. ISO-TP 组帧 / 重组 ==============================

def block_seq(block_index: int) -> int:
    """块序号 1..255 循环：第 1 块=1 ... 第 255 块=255，第 256 块=1。"""
    if block_index < 1:
        raise ValueError("block_index 从 1 开始")
    return ((block_index - 1) % BLOCK_SEQ_MOD) + 1


def block_count(total_bytes: int, block_size: int) -> int:
    """按块大小计算总块数"""
    if block_size <= 0:
        raise ValueError("block_size 必须 > 0")
    return (total_bytes + block_size - 1) // block_size


def _pad_frame(frame: bytearray) -> bytes:
    """经典 CAN 帧补齐到 8 字节（与固件 isotp.c 的填充一致）"""
    if len(frame) < CAN_PAYLOAD_LEN:
        frame = bytearray(frame) + bytes(CAN_PAYLOAD_LEN - len(frame))
    return bytes(frame)


def isotp_stmin_to_seconds(raw: int) -> float:
    """把 FC 帧第 3 字节（STmin）解码为秒。

    编码规则来自 ISO 15765-2（与固件 isotp.c 一致）：
      * ``0x00``..``0x7F`` -> 0..127 ms（十进制）
      * ``0x80``..``0xF0`` -> 保留（本实现按 0 处理，不人为加延时）
      * ``0xF1``..``0xF9`` -> 100..900 us（``raw - 0xF0`` 个 100us）
      * ``0xFA``..``0xFE`` -> 保留（按 0 处理）
      * ``0xFF``-> 127 ms（标准规定为"最大间隔"）

    保留区间刻意返回 0 而不是取 127ms 上限：保留值本不应出现，取 0 可避免因对端
    填了保留值而把整槽刷写时间无谓拉长一个数量级（而 BS 才是真正决定丢帧风险
    的字段，STmin 保留值不应作为限速依据）。
    """
    raw &= 0xFF
    if raw <= 0x7F:
        return raw / 1000.0
    if 0xF1 <= raw <= 0xF9:
        return (raw - 0xF0) * 100e-6
    return 0.0


def isotp_segment(payload: bytes, pad: bool = True) -> list:
    """应用报文 -> 经典 CAN 帧序列（SF / FF+CF）。

    FF: 0x1H LL（12 位长度）；CF: 0x2N（N=SN 0..15，FF 后首个 SN=1）；SF: 0x0L（<=7 字节）。
    ``payload`` 长度必须 1..ISOTP_MAX_MSG(1024)，超长抛 IsoTpTooLong（不截断）。
    """
    data = bytes(payload)
    n = len(data)
    if n == 0:
        raise IsoTpTooLong("ISO-TP 报文长度不能为 0")
    if n > ISOTP_MAX_MSG:
        raise IsoTpTooLong(f"ISO-TP 报文 {n} 字节 > 固件上限 {ISOTP_MAX_MSG} 字节（不截断）")

    if n <= 7:
        frame = bytearray([n & 0x0F]) + data
        return [_pad_frame(frame) if pad else bytes(frame)]

    frames = []
    ff = bytearray(CAN_PAYLOAD_LEN)
    ff[0] = 0x10 | ((n >> 8) & 0x0F)
    ff[1] = n & 0xFF
    ff[2:CAN_PAYLOAD_LEN] = data[0:6]
    frames.append(bytes(ff))

    sn = 1
    off = 6
    while off < n:
        chunk = data[off:off + 7]
        cf = bytearray([0x20 | sn]) + chunk
        frames.append(_pad_frame(cf) if pad else bytes(cf))
        sn = (sn + 1) & 0x0F
        off += len(chunk)
    return frames


def segment_frame_count(payload_len: int) -> int:
    """计算某个应用报文长度会占用多少 CAN 帧（长度自洽校验用）"""
    if payload_len <= 0 or payload_len > ISOTP_MAX_MSG:
        raise IsoTpTooLong(f"报文长度 {payload_len} 非法（1..{ISOTP_MAX_MSG}）")
    if payload_len <= 7:
        return 1
    return 1 + (payload_len - 6 + 6) // 7


def block_frame_count(block_size: int) -> int:
    """一个 0x36 块报文占用的 CAN 帧数 = 1 + ceil((2+blockSize-6)/7)（含 SID 与 blockSeq）"""
    return segment_frame_count(2 + int(block_size))


def rx_ring_hint(block_size: int):
    """隧道模式 RX 环形缓冲提示；帧数不超过环形深度时返回 None。

    依据（写入 docs/03 §5.4）: 桥节点 can.c 的 CAN_RX_RING_LEN =
    NODE_RX_RING_DEPTH(16)；隧道模式下每个 CAN 帧要打印一行 RXTX（约 26~30 字符
    @115200 ≈ 2.3ms），而 CF 到达间隔由 STmin 固定为 2ms -> 打印速度追不上接收速度，
    只能靠 16 深度环形缓冲撑住，故建议先调小块并用 22 F002 确认 rxOverruns == 0。
    """
    frames = block_frame_count(block_size)
    if frames <= NODE_RX_RING_DEPTH:
        return None
    return (f"提示: 本工具经串口控制台与节点通信；单块 {block_size}B = {frames} 个 CAN 连续帧，"
            f"而节点 RX 环形缓冲仅 {NODE_RX_RING_DEPTH} 深度。\n"
            f"      若你是通过\"桥节点隧道\"连接（而非 USB-CAN 直连），建议 --block-size "
            f"{TUNNEL_SUGGESTED_BLOCK_SIZE}，并在刷写后用 22 F002 确认 rxOverruns == 0。")


def erase_estimate_ms(region_bytes: int) -> int:
    """保守估算擦除耗时：ceil(字节/页大小) * 每页 25ms（G4 单页擦除典型 20~40ms）"""
    pages = (int(region_bytes) + FLASH_PAGE_SIZE - 1) // FLASH_PAGE_SIZE
    return pages * FLASH_PAGE_ERASE_MS


class IsoTpReassembler:
    """接收方向重组：SF 直接投递；FF+CF 按 SN(0..15) 拼装。

    - SN 错序抛 IsoTpSnError（不静默丢帧）；
    - FF 声明长度 > 1024 抛 IsoTpTooLong（不静默截断）；
    - FC(0x3) 与保留 PCI 返回 None。
    """

    def __init__(self, max_msg: int = ISOTP_MAX_MSG):
        self.max_msg = max_msg
        self.reset()

    def reset(self):
        self.total = 0
        self.buf = bytearray()
        self.next_sn = 1
        self.collecting = False

    def feed(self, frame: bytes, frame_len=None):
        if not frame:
            raise IsoTpError("空 CAN 帧")
        n = len(frame) if frame_len is None else min(int(frame_len), len(frame))
        pci = (frame[0] >> 4) & 0x0F

        if pci == 0x0:                                   # 单帧
            sf_len = frame[0] & 0x0F
            if sf_len == 0 or sf_len > 7:
                raise IsoTpError(f"非法单帧长度 {sf_len}")
            if sf_len > n - 1:
                raise IsoTpError(f"单帧声明 {sf_len} 字节但帧内只有 {n - 1} 字节")
            self.reset()
            return bytes(frame[1:1 + sf_len])

        if pci == 0x1:                                   # 首帧
            if n < 2:
                raise IsoTpError("首帧不足 2 字节，无长度字段")
            total = ((frame[0] & 0x0F) << 8) | frame[1]
            if total > self.max_msg:
                self.reset()
                raise IsoTpTooLong(
                    f"首帧声明 {total} 字节 > 本机上限 {self.max_msg} 字节（不截断）")
            if total <= 7:
                raise IsoTpError(f"首帧声明长度 {total} <= 7，应为单帧")
            self.total = total
            self.buf = bytearray(frame[2:2 + min(6, total, max(n - 2, 0))])
            self.next_sn = 1
            self.collecting = True
            return None

        if pci == 0x2:                                   # 连续帧
            if not self.collecting:
                return None                              # 无接收事务，丢弃
            sn = frame[0] & 0x0F
            if sn != self.next_sn:
                self.reset()
                raise IsoTpSnError(f"连续帧序号错序: 期望 SN={self.next_sn} 实收 SN={sn}")
            self.next_sn = (self.next_sn + 1) & 0x0F
            need = self.total - len(self.buf)
            self.buf += frame[1:1 + max(need, 0)]
            if len(self.buf) >= self.total:
                msg = bytes(self.buf[:self.total])
                self.reset()
                return msg
            return None

        return None                                      # FC(0x3) / 保留 PCI：忽略


def isotp_reassemble(frames, can_id=None) -> list:
    """把 (can_id, frame) 或裸 frame 序列重组成应用报文列表"""
    reasm = IsoTpReassembler()
    out = []
    for item in frames:
        if isinstance(item, (tuple, list)):
            cid, frame = item
        else:
            cid, frame = can_id, item
        if can_id is not None and cid != can_id:
            continue
        msg = reasm.feed(frame)
        if msg is not None:
            out.append(msg)
    return out


# ============================== 5. UDS 请求编码 / 应答解析 ==============================

def uds_session(session=UDS_SESSION_EXTENDED) -> bytes:
    """10 <session>"""
    return bytes([0x10, session & 0xFF])


def uds_tester_present(suppress=True) -> bytes:
    """3E 80（抑制正应答）/ 3E 00"""
    return bytes([0x3E, UDS_SUPPRESS_POS_RSP if suppress else 0x00])


def uds_request_download(addr, size, data_format=0x00, alfid=0x44) -> bytes:
    """34 00 44 <addr4 大端> <size4 大端>"""
    return (bytes([0x34, data_format & 0xFF, alfid & 0xFF])
            + struct.pack(">I", int(addr) & 0xFFFFFFFF)
            + struct.pack(">I", int(size) & 0xFFFFFFFF))


def uds_transfer_data(seq, data) -> bytes:
    """36 <blockSeq> <data...>（整条报文必须 <=1024 字节）"""
    payload = bytes([0x36, int(seq) & 0xFF]) + bytes(data)
    if len(payload) > ISOTP_MAX_MSG:
        raise IsoTpTooLong(
            f"0x36 报文 {len(payload)} 字节 > {ISOTP_MAX_MSG}（块净荷 {len(data)} 过大）")
    return payload


def uds_transfer_exit(payload_crc=None) -> bytes:
    """37 [<payloadCrc32 大端4字节>]

    契约要求 0x37 携带整镜像 payloadCrc（= crc32(净荷)，不含 32 字节头），
    设备用它作为 FF01 校验参考值与合成镜像头的 payloadCrc。
    """
    if payload_crc is None:
        return bytes([0x37])
    return bytes([0x37]) + struct.pack(">I", int(payload_crc) & 0xFFFFFFFF)


def uds_routine_control(sub=0x01, routine=UDS_ROUTINE_VERIFY, data=b"") -> bytes:
    """31 <sub> <routine2 大端> [data...]"""
    return (bytes([0x31, sub & 0xFF, (int(routine) >> 8) & 0xFF, int(routine) & 0xFF])
            + bytes(data))


def uds_read_data_by_id(did) -> bytes:
    """22 <DID2 大端>"""
    return bytes([0x22, (int(did) >> 8) & 0xFF, int(did) & 0xFF])


def uds_write_data_by_id(did, data) -> bytes:
    """2E <DID2 大端> <data...>（F190 01 = 置"下次进 Bootloader"）"""
    return bytes([0x2E, (int(did) >> 8) & 0xFF, int(did) & 0xFF]) + bytes(data)


def uds_ecu_reset(kind=0x01) -> bytes:
    """11 01（hardReset）"""
    return bytes([0x11, kind & 0xFF])


def uds_parse_response(rsp: bytes) -> bytes:
    """应答总入口：7F <sid> <nrc> 抛 UdsNegativeResponse，否则原样返回"""
    rsp = bytes(rsp or b"")
    if not rsp:
        raise OtaError("收到空应答")
    if rsp[0] == 0x7F:
        if len(rsp) < 3:
            raise OtaError(f"NRC 应答长度不足: {rsp.hex(' ')}")
        raise UdsNegativeResponse(rsp[1], rsp[2], raw=rsp)
    return rsp


def parse_routine_response(rsp: bytes, routine: int, *, allow_missing_status: bool = False):
    """解析 31 01 正应答 -> (status, crc_or_None)。

    契约格式: [0x71, 0x01, routineHi, routineLo, status(00通过/01 失败/02 长度不符),
              crc32(4 字节大端)]，共 9 字节。

    ⚠️ 2026-10-07 修正（M8，fail-open -> fail-closed）：
       status 位于**第 5 字节**（索引 4），故 4 字节应答 `[71,01,FF,01]`
       **根本不含 status**。旧代码 `rsp[4] if len(rsp) > 4 else 0x00` 会把这种
       截断/畸形应答当成"status=0 校验通过"，导致 CRC 校验被彻底跳过却报告成功。
       现在：只有显式 allow_missing_status=True 才允许缺失（仅用于探测类场景），
       其余情况一律报格式错误。

    status 非 0 不在这里直接抛错——调用方要能用回读 CRC 与本机镜像 CRC 交叉核对
    （续传时设备的 status 参考值与所声明区段不同，属预期情况）。
    """
    rsp = uds_parse_response(rsp)
    if len(rsp) < UDS_VERIFY_RESP_MIN_LEN or rsp[0] != 0x71 or rsp[1] != 0x01:
        raise OtaError(f"0x31 正应答格式错误: {rsp.hex(' ')}")
    got = (rsp[2] << 8) | rsp[3]
    if got != routine:
        raise OtaError(f"子例程号不符: 期望 0x{routine:04X} 实收 0x{got:04X}")
    if len(rsp) < UDS_VERIFY_RESP_STATUS_IDX + 1:
        # 走到这里说明应答只有 4 字节：缺 status，无法判定成败
        if not allow_missing_status:
            raise OtaError(
                f"0x31 01 {routine:04X} 应答仅 {len(rsp)} 字节，缺少 status 字段"
                f"（status 位于第 5 字节；契约要求 9 字节）: {rsp.hex(' ')}"
                f"——按 fail-closed 拒绝，不视为通过")
        return 0x00, None
    status = rsp[UDS_VERIFY_RESP_STATUS_IDX]
    crc = struct.unpack(">I", rsp[5:9])[0] if len(rsp) >= UDS_VERIFY_RESP_FULL_LEN else None
    return status, crc


def verify_status_name(status: int) -> str:
    """FF01 status 名称（未知值原样显示）"""
    return UDS_VERIFY_STATUS_NAMES.get(status & 0xFF, f"未知(0x{status & 0xFF:02X})")


def ascii_hint(payload: bytes) -> str:
    """净荷全为可打印 ASCII 时给出文本提示，否则返回空串（避免打印乱码）"""
    if payload and all(0x20 <= byte < 0x7F for byte in payload):
        return "  ASCII=" + payload.decode("ascii")
    return ""


def parse_download_response(rsp: bytes) -> int:
    """解析 0x34 正应答 [0x74, 0x20, maxLen2 大端] -> maxLen"""
    rsp = uds_parse_response(rsp)
    if len(rsp) < 4 or rsp[0] != 0x74:
        raise OtaError(f"0x34 正应答格式错误: {rsp.hex(' ')}")
    if rsp[1] != 0x20:
        raise OtaError(f"0x34 应答 lengthFormatIdentifier 应为 0x20, 实收 0x{rsp[1]:02X}")
    return struct.unpack(">H", rsp[2:4])[0]


def parse_session_response(rsp: bytes):
    """解析 10 正应答 [0x50, sub, P2(2B), P2*(2B)] -> (sub, p2_ms, p2star_ms)。

    P2 单位为 1ms（0x0032 -> 50ms）；P2* 单位为 10ms（0x01F4 -> 500*10 = 5000ms）——
    即 ISO 14229 的 P2/P2*Server_max。设备用它声明自己承诺的最长响应时间上限，
    本工具据此判断"擦除耗时是否可接受"。兼容短应答（缺字段时返回 None）。
    """
    rsp = uds_parse_response(rsp)
    if not rsp or rsp[0] != 0x50:
        raise OtaError(f"0x10 正应答格式错误: {rsp.hex(' ')}")
    sub = rsp[1] if len(rsp) > 1 else 0
    p2_ms = struct.unpack(">H", rsp[2:4])[0] if len(rsp) >= 4 else None
    p2star_ms = struct.unpack(">H", rsp[4:6])[0] * 10 if len(rsp) >= 6 else None
    return sub, p2_ms, p2star_ms


# ============================== 6. 行协议传输层 ==============================

def strip_prompt(line: str) -> str:
    """去掉 console 提示符前缀与首尾空白（提示符可能粘在下一行行首）"""
    s = (line or "").strip()
    while s.startswith("> "):
        s = s[2:].strip()
    if s == ">":
        s = ""
    return s


def tx_cmd_line(can_id, frame) -> str:
    """构造 TX 命令: TX <id3hex> <n> <2*n hex字节>"""
    frame = bytes(frame)
    return f"TX {int(can_id):03X} {len(frame)}" + "".join(f" {b:02X}" for b in frame)


def rxtx_line(can_id, frame) -> str:
    """构造 RXTX 行（模拟器输出，与固件打印格式一致）"""
    frame = bytes(frame)
    return f"RXTX {int(can_id):03X} {len(frame)}" + "".join(f" {b:02X}" for b in frame)


def _parse_hex_bytes(text: str) -> bytes:
    return bytes.fromhex((text or "").replace(" ", ""))


class LineSink:
    """统一的收发输出：控制台逐行打印（可静默）+ 文本日志（--replay 的输入）"""

    def __init__(self, verbose=True, log_path=None, stream=None):
        self.verbose = verbose
        self.stream = stream if stream is not None else sys.stdout
        self.fh = None
        self.count = 0
        if log_path:
            self.fh = open(log_path, "w", encoding="utf-8", newline="\n")
            self.fh.write(f"# {TOOL_NAME} v{TOOL_VERSION} 行协议收发日志（--replay 输入）\n")
            self.fh.write("# '<<' = 主机->节点, '>>' = 节点->主机\n")

    def __call__(self, direction, text):
        self.count += 1
        prefix = "<< " if direction == "TX" else ">> "
        if self.fh is not None:
            self.fh.write(prefix + text + "\n")
            self.fh.flush()
        if self.verbose:
            if direction == "TX":
                print(Clr.cyan(f"TX> {text}"), file=self.stream)
            else:
                print(Clr.green(f"RX< {text}"), file=self.stream)

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


class LineTransport:
    """行传输抽象：send_line 发一行（工具自动补 CR），read_line 读一行（超时返回 None）"""
    name = "abstract"

    def __init__(self, sink=None):
        self.sink = sink

    def _emit(self, direction, text):
        if self.sink is not None:
            self.sink(direction, text)

    def open(self):
        pass

    def close(self):
        pass

    def send_line(self, text):
        raise NotImplementedError

    def read_line(self, timeout):
        raise NotImplementedError


def _import_serial():
    """按需导入 pyserial（仅真实串口模式），失败给出清晰提示而不是崩栈"""
    try:
        import serial                                     # noqa: F401
        from serial.tools import list_ports               # noqa: F401
        return serial, list_ports
    except ImportError:
        raise TransportError(
            "缺少 pyserial，请先执行: pip install pyserial"
            "（仅真实串口模式需要；--dry-run/--replay/--make-sample/--info 无需）") from None


class SerialLineTransport(LineTransport):
    """真实串口：115200 8N1 行协议；按 \\n 切行，容忍半行缓冲与提示符粘连"""
    name = "serial"

    def __init__(self, port, baudrate=115200, sink=None):
        super().__init__(sink)
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self._buf = b""

    def open(self):
        serial, _ = _import_serial()
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.05, write_timeout=1.0)
        except Exception as exc:                          # serial.SerialException / OSError
            raise TransportError(f"打开串口 {self.port} 失败: {exc}") from None
        self._buf = b""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def send_line(self, text):
        if self.ser is None:
            raise TransportError("串口未打开")
        try:
            self.ser.write((text + "\r").encode("ascii"))
            self.ser.flush()
        except Exception as exc:
            raise TransportError(f"串口写入失败: {exc}") from None
        self._emit("TX", text)

    def read_line(self, timeout):
        if self.ser is None:
            raise TransportError("串口未打开")
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None                               # 超时（可能残留半行，等下一次拼接）
            self.ser.timeout = max(0.01, min(0.25, remain))
            try:
                chunk = self.ser.read(256)
            except Exception as exc:
                raise TransportError(f"串口读取失败: {exc}") from None
            if chunk:
                self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        line = raw.decode("ascii", "replace").rstrip("\r")
        self._emit("RX", line)
        return line


class SimulatedEcu:
    """确定性 OTA ECU 模拟器：行协议控制台 + 隧道 + 服务端 ISO-TP/UDS。

    只用于 --dry-run 与单元测试，不连任何硬件；行为与契约一一对应。
    """

    def __init__(self, max_msg=ISOTP_MAX_MSG, max_block=ISOTP_MAX_BLOCK_SIZE):
        self.max_msg = max_msg
        self.max_block = max_block
        self.mem = {}                    # addr -> byte（模拟 Flash，复位/擦除语义独立）
        # 与 ota_common.h 冻结语义一致的 DID 内容
        self.ota_state = 0x00            # OTA_ST_IDLE_A
        self.active_slot = 0x00          # 0=A
        self.ota_version = bytes([1, 0, 1, 0])
        self.slot_crc_a = 0
        self.slot_crc_b = 0
        self.capability = 0x07           # 位0=0x34/36/37, 位1=31例程, 位2=双槽
        self.block_fault_index = None    # 指定绝对块号 -> 返回 NRC 0x73（故障注入）
        self.corrupt_block_index = None  # 指定绝对块号 -> 写入时翻转1字节（CRC 失败注入）
        self.erase_count = 0             # 诊断计数（不随复位清零）
        self.payload_crc = None          # 0x37 携带的整镜像 payloadCrc（FF01 参考值）
        self.bl_ready_in = 0             # >0 时吞掉前 N 次 10 03（模拟 BL 约1s启动窗口）
        self.boot_req_seen = False       # 收到过 2E F190 01
        self.commit_count = 0            # 收到过 31 01 FF04 的次数
        self.block_index = 1             # 当前期望的 1-based 块号（由 0x34 的 addr 推导）
        self.tunnel = False
        self.reset_session()

    # ---- 状态 ----
    def reset_session(self):
        """复位 UDS/ISO-TP 协议状态（模拟 ECU 复位，Flash 与隧道不受影响）。

        隧道属于桥节点（PC↔桥 的行协议），不随 ECU 复位而关闭，因此这里不动物 tunnel。
        """
        self.session = UDS_SESSION_DEFAULT
        self.rx = IsoTpReassembler(self.max_msg)
        self.tx_pending_cfs = deque()
        self.awaiting_fc = False
        self.dl_region = None            # (addr, size) 供 FF01 校验
        self.dl_cursor = 0
        self.dl_remaining = 0

    def _app_base_of(self, addr):
        """addr 所属槽的 APP 链接基址（不在已知槽内时按原地址处理）"""
        for name, app_addr in OTA_SLOT_ADDRS.items():
            base = OTA_SLOT_BASES[name]
            if base <= addr < base + OTA_SLOT_SIZE:
                return app_addr
        return addr

    # ---- 命令行处理 ----
    def handle_line(self, text) -> list:
        s = strip_prompt(text)
        if not s:
            return []
        toks = s.split()
        cmd = toks[0].lower()
        args = toks[1:]

        if cmd == "help":
            return ["commands:", "  help / version / stats / bitrate <kbps>",
                    "  sniff <on|off> / send <id> [b..] / TUN <on|off>",
                    "  TX <id3hex> <n> <hex...>"]
        if cmd == "version":
            return [f"v1.0 role=SIM-ECU build=sim bitrate=500000bps"]
        if cmd == "stats":
            return ["uptime=1s bitrate=500000bps", "tx=0 rx=0 txErr=0 ovr=0 busOff=0"]
        if cmd == "bitrate":
            if args and args[0] in ("125", "250", "500", "1000"):
                return [f"switched to {args[0]}kbps (set the SAME on the other node!)"]
            return ["only 125/250/500/1000 kbps supported"]
        if cmd == "sniff":
            if args and args[0].lower() == "on":
                return ["sniff ON, format: RX <ms> <ID> <DLC> <data...>"]
            if args and args[0].lower() == "off":
                return ["sniff OFF"]
            return ["usage: sniff <on|off>"]
        if cmd == "tun":
            if args and args[0].lower() == "on":
                self.tunnel = True
                return ["tunnel: on"]
            if args and args[0].lower() == "off":
                self.tunnel = False
                return ["tunnel: off"]
            return ["usage: TUN <on|off>"]
        if cmd == "tx":
            return self._handle_tx(args)
        return [f"unknown cmd: {toks[0]} (try 'help')"]

    def _handle_tx(self, args) -> list:
        """TX <id3hex> <n> <2*n hex字节> -> TXOK / TXERR"""
        if len(args) < 2:
            return ["TXERR"]
        try:
            can_id = int(args[0], 16)
            n = int(args[1], 10)
            data = _parse_hex_bytes(" ".join(args[2:]))
        except ValueError:
            return ["TXERR"]
        if can_id < 0 or can_id > 0x7FF or n < 1 or n > CAN_PAYLOAD_LEN or len(data) != n:
            return ["TXERR"]

        out = ["TXOK"]
        if not self.tunnel or can_id not in (ISOTP_REQ_ID, ISOTP_RESP_ID):
            return out
        out.append(rxtx_line(can_id, data))               # 桥节点把总线上收到的诊断帧打印出来
        if can_id != ISOTP_REQ_ID:
            return out
        for frame in self._server_feed(data):             # ECU 发出的诊断帧同样打印
            out.append(rxtx_line(ISOTP_RESP_ID, frame))
        return out

    # ---- 服务端 ISO-TP ----
    def _server_feed(self, frame: bytes) -> list:
        pci = (frame[0] >> 4) & 0x0F
        if pci == 0x3:                                    # 主机对我方多帧应答的流控
            if not self.awaiting_fc:
                return []
            self.awaiting_fc = False
            out = list(self.tx_pending_cfs)
            self.tx_pending_cfs.clear()
            return out
        if pci == 0x1:
            self.rx.reset()
            self.rx.feed(frame)
            return [bytes([0x30 | ISOTP_FC_CTS, 0x00, ISOTP_FC_STMIN_MS])]   # CTS,BS=0,STmin=2ms
        try:
            payload = self.rx.feed(frame)
        except IsoTpError:
            self.rx.reset()
            return []
        if payload is None:
            return []
        return self._respond(payload)

    def _respond(self, req: bytes) -> list:
        rsp = self._uds_handle(req)
        if rsp is None:
            return []
        frames = isotp_segment(rsp)
        if len(frames) == 1:
            return frames
        self.tx_pending_cfs = deque(frames[1:])           # 等主机 FC 后再发 CF
        self.awaiting_fc = True
        return [frames[0]]

    # ---- 服务端 UDS ----
    @staticmethod
    def _nrc(sid, code) -> bytes:
        return bytes([0x7F, sid, code])

    def _uds_handle(self, req: bytes):
        sid = req[0]
        if sid == 0x10:
            if len(req) != 2:
                return self._nrc(sid, 0x13)
            sub = req[1]
            if self.bl_ready_in > 0 and sub == UDS_SESSION_EXTENDED:
                # 模拟 BL 约1s启动窗口：窗口内的 10 03 无人应答（主机须轮询重发）
                self.bl_ready_in -= 1
                return None
            if sub in (UDS_SESSION_DEFAULT, UDS_SESSION_EXTENDED):
                self.session = sub
                return bytes([0x50, sub, 0x00, 0x32, 0x01, 0xF4])          # P2=50ms P2*=5000ms
            return self._nrc(sid, 0x12)
        if sid == 0x3E:
            if len(req) != 2:
                return self._nrc(sid, 0x13)
            if req[1] & UDS_SUPPRESS_POS_RSP:
                return None                                                # 抑制正应答
            return bytes([0x7E, req[1]])
        if sid == 0x2E:
            if len(req) != 4:
                return self._nrc(sid, 0x13)
            if self.session != UDS_SESSION_EXTENDED:
                return self._nrc(sid, 0x22)                                # 需扩展会话
            did = (req[1] << 8) | req[2]
            if did != UDS_DID_BOOT_REQ:
                return self._nrc(sid, 0x31)
            if req[3] != UDS_BOOT_REQ_VALUE:
                return self._nrc(sid, 0x31)
            self.boot_req_seen = True
            return bytes([0x6E, req[1], req[2]])
        if sid == 0x22:
            if len(req) != 3:
                return self._nrc(sid, 0x13)
            did = (req[1] << 8) | req[2]
            # DID 语义与 ota_common.h 冻结一致（模拟器不给"看起来像真机"的假字符串）
            known = {
                0xF180: bytes([self.ota_state]),                       # 1B OTA 状态
                0xF181: bytes([self.active_slot]),                     # 1B 活动槽 0=A 1=B
                0xF182: bytes(self.ota_version),                       # 4B 已装版本
                0xF183: struct.pack(">II", self.slot_crc_a, self.slot_crc_b),  # 8B 双槽 CRC
                0xF184: bytes([self.capability]),                      # 1B 能力位图
                0xF186: bytes([self.session]),                         # 1B 当前会话(标准DID)
            }
            if did in known:
                return bytes([0x62, req[1], req[2]]) + known[did]
            return self._nrc(sid, 0x31)
        if sid == 0x34:
            if len(req) != 11:
                return self._nrc(sid, 0x13)
            if self.session != UDS_SESSION_EXTENDED:
                return self._nrc(sid, 0x22)                                # 编程服务需扩展会话
            if req[1] != 0x00 or req[2] != 0x44:
                return self._nrc(sid, 0x31)
            addr = struct.unpack(">I", req[3:7])[0]
            size = struct.unpack(">I", req[7:11])[0]
            if size == 0:
                return self._nrc(sid, 0x31)
            self.dl_region = (addr, size)
            self.dl_cursor = addr
            self.dl_remaining = size
            # 期望块序号推导（与 UDS_BLOCK_SEQ_OF 一致）：
            # 优先按 0x34 的 addr 推导 —— BL 据 addr 与通告的块大小即可算出断点位置，
            # 因此复位/重启后仍能正确接受续传；地址不是通告块大小的整数倍（主机用了
            # 更小的 --block-size）时沿用会话内计数，不误判 NRC 0x73。
            offset = addr - self._app_base_of(addr)
            if offset <= 0:
                self.block_index = 1
            elif offset % self.max_block == 0:
                self.block_index = (offset // self.max_block) + 1
            # 与固件一致：0x34 的 BL_DiagPrepareDownload **隐式擦除** addr 起始的页区间。
            # 续传（offset > 0）只擦该页及其后，不清前面已收的块。
            if offset <= 0:
                self.mem.clear()
                self.payload_crc = None
                self.erase_count += 1
            return bytes([0x74, 0x20]) + struct.pack(">H", self.max_block)
        if sid == 0x36:
            if self.dl_region is None:
                return self._nrc(sid, 0x24)
            if self.session != UDS_SESSION_EXTENDED:
                return self._nrc(sid, 0x22)
            if len(req) < 2:
                return self._nrc(sid, 0x13)
            seq = req[1]
            data = req[2:]
            if self.block_fault_index == self.block_index:
                return self._nrc(sid, 0x73)                                # 故障注入
            if seq != block_seq(self.block_index):
                return self._nrc(sid, 0x73)
            if len(data) > self.dl_remaining:
                return self._nrc(sid, 0x31)
            for i, byte in enumerate(data):
                if self.corrupt_block_index == self.block_index and i == 0:
                    byte ^= 0xFF                                          # 损坏注入
                self.mem[self.dl_cursor + i] = byte
            self.dl_cursor += len(data)
            self.dl_remaining -= len(data)
            self.block_index += 1
            return bytes([0x76, seq])
        if sid == 0x37:
            if self.dl_region is None:
                return self._nrc(sid, 0x24)
            if self.session != UDS_SESSION_EXTENDED:
                return self._nrc(sid, 0x22)
            if self.dl_remaining != 0:
                return self._nrc(sid, 0x24)
            if len(req) == 5:                             # 契约: [37, payloadCrc32 大端4字节]
                self.payload_crc = struct.unpack(">I", req[1:5])[0]
            elif len(req) == 1:
                self.payload_crc = None                   # 不带 CRC 时只能按长度校验
            else:
                return self._nrc(sid, 0x13)
            return bytes([0x77])
        if sid == 0x31:
            if len(req) < 4:
                return self._nrc(sid, 0x13)
            sub = req[1]
            routine = (req[2] << 8) | req[3]
            if sub != 0x01:
                return self._nrc(sid, 0x12)
            if routine == UDS_ROUTINE_ERASE:
                self.mem.clear()
                self.dl_region = None
                self.dl_cursor = 0
                self.dl_remaining = 0
                self.payload_crc = None
                self.block_index = 1
                self.erase_count += 1
                return bytes([0x71, 0x01, 0xFF, 0x00, 0x00])
            if routine == UDS_ROUTINE_VERIFY:
                if self.dl_region is None:
                    return self._nrc(sid, 0x24)
                addr, size = self.dl_region
                blob = bytes(self.mem.get(addr + i, 0) for i in range(size))
                region_crc = crc32(blob)
                # 设备语义: 对本轮实际接收字节流重算 CRC；status 与 0x37 参考值比对
                status = 0x00
                if self.payload_crc is not None and region_crc != self.payload_crc:
                    status = 0x01                         # CRC 不符（续传时区段 != 整镜像, 属预期）
                return (bytes([0x71, 0x01, 0xFF, 0x01, status])
                        + struct.pack(">I", region_crc))
            if routine == UDS_ROUTINE_ACTIVATE:
                # FF02 语义：设备侧合成槽首 32B 镜像头（payloadCrc 来自 0x37）并置试运行
                if self.payload_crc is not None:
                    if self.active_slot == 0:
                        self.slot_crc_b = self.payload_crc
                    else:
                        self.slot_crc_a = self.payload_crc
                self.ota_state = 0x02 if self.active_slot == 0 else 0x03      # TRIAL_A / TRIAL_B
                return bytes([0x71, 0x01, 0xFF, 0x02, 0x00])
            if routine == UDS_ROUTINE_ROLLBACK:
                return bytes([0x71, 0x01, 0xFF, 0x03, 0x00])
            if routine == UDS_ROUTINE_COMMIT:
                # FF04 试运行确认：TRIAL_x -> IDLE_x；默认会话下按契约回 NRC 0x22
                if self.session != UDS_SESSION_EXTENDED:
                    return self._nrc(sid, 0x22)
                self.ota_state = self.active_slot                  # IDLE_A(0) / IDLE_B(1)
                self.commit_count += 1
                return bytes([0x71, 0x01, 0xFF, 0x04, 0x00])
            return self._nrc(sid, 0x31)
        if sid == 0x11:
            if len(req) != 2:
                return self._nrc(sid, 0x13)
            if req[1] not in (0x01, 0x02, 0x03):
                return self._nrc(sid, 0x12)
            kind = req[1]
            self.reset_session()                          # Flash 保留；隧道属于桥节点不关闭
            self.bl_ready_in = 1                          # 复位后首次 10 03 落在 BL 启动窗口内
            return bytes([0x51, kind])
        return self._nrc(sid, 0x11)

    # ---- 供测试/自检读取 ----
    def read(self, addr, size) -> bytes:
        return bytes(self.mem.get(addr + i, 0) for i in range(size))


class SimulatedLineTransport(LineTransport):
    """把 SimulatedEcu 包装成行传输：自动回显命令行（与 console.c 行为一致）"""
    name = "sim"

    def __init__(self, ecu=None, sink=None):
        super().__init__(sink)
        self.ecu = ecu if ecu is not None else SimulatedEcu()
        self.out = deque()

    def send_line(self, text):
        self._emit("TX", text)
        self.out.append("> " + text)                      # console.c 的回显（提示符粘在行首）
        for line in self.ecu.handle_line(text):
            self.out.append(line)

    def read_line(self, timeout):
        if not self.out:
            return None                                   # 模拟器只在被命令后输出
        line = self.out.popleft()
        self._emit("RX", line)
        return line


class ReplayLineTransport(LineTransport):
    """离线重放行传输：设备行按日志顺序喂给主机，并严格校验主机行与记录一致。

    用于回归验证"同样的镜像 -> 同样的报文字节序列"。
    """

    name = "replay"

    @staticmethod
    def _is_skippable(text):
        """只有周期性保活命令(3E 80)允许"日志里有、重放时没发"（时间相关，不算回归）"""
        m = TX_CMD_PATTERN.match(strip_prompt(text))
        if not m:
            return False
        data = _parse_hex_bytes(m.group(3))
        return len(data) >= 2 and data[0] == 0x3E

    def __init__(self, log_path, sink=None):
        super().__init__(sink)
        # 日志按"主机命令 -> 该命令之后的设备输出"切段：这样某条命令若在真机上
        # 根本没有应答（如复位后 10 03 轮询落在 BL 启动窗口内），重放时该段耗尽即
        # 返回 None（超时），主机就会像真机一样重发 —— 报文序列才能逐字节对齐。
        self.host_lines = deque()
        self.segments = deque()    # 与 host_lines 一一对应
        self.current = deque()
        self.skipped = 0
        self.matched = 0
        self.dropped_rx = 0        # 未被主机读取的 RXTX 7E8 帧（应恒为 0）
        self._cur_host = None
        banner = []
        pending_dev = []
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\r\n")
                if line.startswith("<<"):
                    if self._cur_host is None:
                        banner = pending_dev
                    else:
                        self.segments.append(pending_dev)
                    self._cur_host = line[2:].strip()
                    self.host_lines.append(self._cur_host)
                    pending_dev = []
                elif line.startswith(">>"):
                    pending_dev.append(line[2:].strip())
        if self._cur_host is None:
            self.segments.append(pending_dev)
        else:
            self.segments.append(pending_dev)
        self.current = deque(banner)
        if not self.host_lines:
            raise ReplayError(f"日志中没有主机行(<<): {log_path}")
        if not any(self.segments) and not banner:
            raise ReplayError(f"日志中没有设备输出行(>>): {log_path}")

    def send_line(self, text):
        want = text.strip()
        self._emit("TX", text)
        if not self.host_lines:
            raise ReplayMismatch(f"日志中的主机行已用尽，但仍要发送: {want}")
        # 上一条命令的设备输出若还有剩余：其中未被读取的 RXTX 7E8 说明主机漏读应答
        for leftover in self.current:
            m = RXTX_PATTERN.match(strip_prompt(leftover))
            if m and int(m.group(1), 16) == ISOTP_RESP_ID:
                self.dropped_rx += 1
        index = 0
        if self.host_lines[0] != want:
            # 只跳过年鉴里的保活命令（3E 80 与墙钟时间相关，重放不会重发）
            while self.host_lines and self.host_lines[0] != want \
                    and self._is_skippable(self.host_lines[0]):
                self.host_lines.popleft()
                self.segments.popleft()
                self.skipped += 1
            if not self.host_lines or self.host_lines[0] != want:
                expected = self.host_lines[0] if self.host_lines else "<无>"
                raise ReplayMismatch(
                    f"重放不一致: 期望主机行 {expected!r}, 实际发送 {want!r}")
        for _ in range(index):
            self.host_lines.popleft()
            self.segments.popleft()
            self.skipped += 1
        self.host_lines.popleft()
        self.current = deque(self.segments.popleft())
        self.matched += 1

    def read_line(self, timeout):
        if not self.current:
            return None                                     # 该命令段已耗尽 -> 等价真机超时
        line = self.current.popleft()
        self._emit("RX", line)
        return line

    def leftovers(self):
        dev_left = len(self.current) + sum(len(seg) for seg in self.segments)
        return len(self.host_lines), dev_left


class ConsoleTunnel:
    """串口行协议隧道客户端：TUN ON/OFF + TX 命令 + RXTX 帧接收/缓存"""

    def __init__(self, transport, timeout=3.0):
        self.t = transport
        self.timeout = timeout
        self.pending = deque()          # 已收到但未被消费的 (can_id, frame)
        self.recent = deque(maxlen=4)   # 最近收行（超时提示用）
        self.frames_sent = 0
        self.frames_recv = 0
        self.dropped = 0

    # ---- 内部 ----
    def _dispatch(self, line):
        self.recent.append(line)
        s = strip_prompt(line)
        m = RXTX_PATTERN.match(s)
        if not m:
            return None
        can_id = int(m.group(1), 16)
        dlc = int(m.group(2))
        data = _parse_hex_bytes(m.group(3))
        frame = data[:dlc]
        self.pending.append((can_id, frame))
        self.frames_recv += 1
        return can_id, frame

    def _read_until(self, predicate, timeout, what):
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise IsoTpTimeout(
                    f"等待 {what} 超时（{timeout:.1f}s），最近收到: {list(self.recent)}")
            line = self.t.read_line(remain)
            if line is None:
                raise IsoTpTimeout(
                    f"等待 {what} 超时（{timeout:.1f}s），最近收到: {list(self.recent)}")
            self._dispatch(line)
            s = strip_prompt(line)
            if predicate(s):
                return s

    def _prune(self, can_id):
        """丢弃不关心的方向（桥节点会把主机自己发的 7E0 也回显成 RXTX）"""
        if can_id is None:
            return
        keep = deque((cid, fr) for cid, fr in self.pending if cid == can_id)
        self.dropped += len(self.pending) - len(keep)
        self.pending = keep

    # ---- 对外 ----
    def wait_for(self, predicate, timeout, what):
        return self._read_until(predicate, timeout, what)

    def tunnel_on(self):
        self.t.send_line("TUN ON")
        self._read_until(lambda s: s.lower() == "tunnel: on", self.timeout, "TUN ON 应答")

    def tunnel_off(self):
        self.t.send_line("TUN OFF")
        self._read_until(lambda s: s.lower() == "tunnel: off", self.timeout, "TUN OFF 应答")

    def tx_frame(self, can_id, frame):
        """发一帧到 CAN 总线（TX 命令），TXERR 抛 TransportError"""
        frame = bytes(frame)
        if not 1 <= len(frame) <= CAN_PAYLOAD_LEN:
            raise OtaError(f"CAN 帧长度 {len(frame)} 非法（1..8）")
        cmd = tx_cmd_line(can_id, frame)
        self.t.send_line(cmd)
        line = self._read_until(
            lambda s: s.upper() in ("TXOK", "TXERR"), self.timeout, f"{cmd} 的 TXOK/TXERR")
        if line.upper() == "TXERR":
            raise TransportError(f"节点拒绝发送 CAN 帧(TXERR): {cmd}")
        self.frames_sent += 1

    def recv_frame(self, timeout, can_id=None, pci=None):
        """等一帧 RXTX（可按 CAN ID / PCI 过滤），超时抛 IsoTpTimeout"""
        deadline = time.monotonic() + timeout
        while True:
            for idx, (cid, frame) in enumerate(self.pending):
                if can_id is not None and cid != can_id:
                    continue
                if pci is not None and ((frame[0] >> 4) & 0x0F) != pci:
                    continue
                del self.pending[idx]
                return cid, frame
            self._prune(can_id)
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise IsoTpTimeout(
                    f"等待 RXTX 帧(ID={can_id if can_id is None else hex(can_id)}) 超时"
                    f"（{timeout:.1f}s），最近收到: {list(self.recent)}")
            line = self.t.read_line(remain)
            if line is None:
                raise IsoTpTimeout(
                    f"等待 RXTX 帧(ID={can_id if can_id is None else hex(can_id)}) 超时"
                    f"（{timeout:.1f}s），最近收到: {list(self.recent)}")
            self._dispatch(line)

    def close(self):
        self.t.close()


# ============================== 7. ISO-TP / UDS 客户端 ==============================

class IsoTpClient:
    """ISO-TP 客户端（诊断仪侧，复用 ID 0x7E0/0x7E8，单通道半双工）"""

    def __init__(self, tunnel: ConsoleTunnel, timeout=3.0,
                 fc_bs=ISOTP_FC_BS, fc_stmin=ISOTP_FC_STMIN_MS, sleep_fn=time.sleep):
        self.tunnel = tunnel
        self.timeout = timeout
        self.fc_bs = fc_bs
        self.fc_stmin = fc_stmin
        # 【H4】STmin 延时器可注入：生产用 time.sleep，单测注入假延时器避免真的等
        self.sleep_fn = sleep_fn

    def send_message(self, payload: bytes):
        """只发不收（例如 3E 80）：FF 场景下仍按对端 FC 发送后续 CF"""
        frames = isotp_segment(payload)
        self._send_sequence(frames)

    def exchange(self, payload: bytes, timeout=None) -> bytes:
        """发一条应用报文并返回对端应用报文"""
        frames = isotp_segment(payload)
        self._send_sequence(frames)
        return self.receive_message(timeout)

    def _send_sequence(self, frames):
        self.tunnel.tx_frame(ISOTP_REQ_ID, frames[0])
        if (frames[0][0] >> 4) == 0x1:                    # FF：等对端 FC
            self._send_remaining(frames[1:])

    def _send_remaining(self, cfs):
        """按对端 FC 逐批发送 CF。

        【H4 修复】FC 的第三个字节是 STmin（连续帧最小间隔），原实现**只读了 BS**，
        把 146 帧背靠背连发出去。对本工程这是致命的：本桥节点的 FDCAN RX FIFO0
        只有 3 深度（bl_can.c），BL 每帧的接收都是在中断里搬走的，一旦连续帧到达
        速率超过 BL 的处理速率，FIFO 溢出 -> 丢帧 -> ISO-TP 重组超时
        （表现为"刷写到某一帧卡住"，且随 CAN 负载/中断延迟波动，极难复现）。
        设备侧 isotp.c 明确要求 STmin=2ms（ISOTP_FC_STMIN_MS），必须遵守。
        """
        idx = 0
        while idx < len(cfs):
            _, fr = self.tunnel.recv_frame(self.timeout, can_id=ISOTP_RESP_ID, pci=0x3)
            fs = fr[0] & 0x0F
            if fs == ISOTP_FC_WAIT:
                continue                                  # WT：继续等
            if fs == ISOTP_FC_OVFLW:
                raise IsoTpOverflow("对端流控溢出(FC OVFLW)，中止发送")
            bs = fr[1] if len(fr) > 1 else 0
            # STmin（ISO 15765-2 标准编码）：
            #   0x00..0x7F -> 0..127 ms
            #   0xF1..0xF9 -> 100..900 us
            #   其余值保留 -> 按标准取 127 ms 上限（保守，宁慢勿丢帧）
            raw_stmin = fr[2] if len(fr) > 2 else 0
            stmin_s = isotp_stmin_to_seconds(raw_stmin)
            n = len(cfs) - idx if bs == 0 else min(bs, len(cfs) - idx)
            # 连续帧之间遵守 STmin。第一帧紧跟 FC 立即发（ISO-TP 语义：STmin 约束的是
            # CF 之间的间隔，不是 FC 到首个 CF 的等待），故从 k=1 起才延时。
            for k in range(n):
                if k > 0 and stmin_s > 0:
                    self.sleep_fn(stmin_s)
                self.tunnel.tx_frame(ISOTP_REQ_ID, cfs[idx + k])
            idx += n

    def receive_message(self, timeout=None) -> bytes:
        """等对端应用报文；收到 FF 时立即回 FC(CTS,BS=0,STmin=2ms)"""
        tmo = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + tmo
        reasm = IsoTpReassembler()
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise IsoTpTimeout("等待 ISO-TP 应答超时")
            _, fr = self.tunnel.recv_frame(remain, can_id=ISOTP_RESP_ID)
            pci = (fr[0] >> 4) & 0x0F
            if pci == 0x3:
                continue                                  # 无我方多帧发送上下文，忽略FC
            if pci == 0x1:
                self.tunnel.tx_frame(
                    ISOTP_REQ_ID, bytes([0x30 | ISOTP_FC_CTS, self.fc_bs, self.fc_stmin]))
            msg = reasm.feed(fr)
            if msg is not None:
                return msg


class UdsClient:
    """UDS 客户端：请求/应答、NRC 异常、多帧应答、0x78 等待"""

    def __init__(self, isotp: IsoTpClient, timeout=3.0):
        self.isotp = isotp
        self.timeout = timeout

    def request(self, payload: bytes, expect_response=True, timeout=None):
        """发请求；expect_response=False 用于 3E 80；NRC 抛 UdsNegativeResponse

        【L6 修复】0x78 等待改为**累计 deadline**，不再每次重置完整 timeout。
        原实现 `receive_message(timeout)` 每次都传同一个完整 timeout，设备只要
        持续回 0x78，主机就无限期重置计时器 -> 永不超时、挂死。
        现改为：首次收到 0x78 时建立一次 deadline，之后每次只传**剩余**时间；
        累计超 UDS_PENDING_TOTAL_BUDGET 即抛 IsoTpTimeout，并报出已收到的 0x78 次数。
        """
        if not expect_response:
            self.isotp.send_message(payload)
            return None
        rsp = self.isotp.exchange(payload, timeout)
        if not (len(rsp) >= 3 and rsp[0] == 0x7F and rsp[2] == UDS_RSP_PENDING):
            return uds_parse_response(rsp)

        # 进入 0x78 等待序列：建立累计 deadline（只建一次，不在循环里重置）
        deadline = time.monotonic() + UDS_PENDING_TOTAL_BUDGET
        pending_cnt = 1
        while len(rsp) >= 3 and rsp[0] == 0x7F and rsp[2] == UDS_RSP_PENDING:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise IsoTpTimeout(
                    f"等待 0x78 最终应答超时：累计预算 {UDS_PENDING_TOTAL_BUDGET:.1f}s 用尽，"
                    f"期间收到 {pending_cnt} 次 0x78（设备可能卡在擦除/校验，或反复重试）")
            # 传剩余时间而非完整 timeout —— 这是本次修复的核心
            rsp = self.isotp.receive_message(remain)
            pending_cnt += 1
        return uds_parse_response(rsp)

    # ---- 服务封装 ----
    def session(self, session=UDS_SESSION_EXTENDED, timeout=None) -> bytes:
        """10 <session>；timeout 用于复位后重建会话时的短超时重试"""
        return self.request(uds_session(session), timeout=timeout)

    def tester_present(self, suppress=True):
        return self.request(uds_tester_present(suppress), expect_response=not suppress)

    def read_data_by_id(self, did) -> bytes:
        return self.request(uds_read_data_by_id(did))

    def write_data_by_id(self, did, data, timeout=None) -> bytes:
        """2E <DID> <data>，正应答必须是 6E <DID>"""
        rsp = self.request(uds_write_data_by_id(did, data), timeout=timeout)
        want = bytes([0x6E, (int(did) >> 8) & 0xFF, int(did) & 0xFF])
        if rsp[:3] != want:
            raise OtaError(f"0x2E 正应答格式错误: 期望 {want.hex(' ')}, 实收 {rsp.hex(' ')}")
        return rsp

    def request_download(self, addr, size) -> int:
        return parse_download_response(self.request(uds_request_download(addr, size)))

    def transfer_data(self, seq, data) -> int:
        rsp = self.request(uds_transfer_data(seq, data))
        if len(rsp) < 2 or rsp[0] != 0x76:
            raise OtaError(f"0x36 正应答格式错误: {rsp.hex(' ')}")
        if rsp[1] != (seq & 0xFF):
            raise OtaError(f"0x36 应答块序号不符: 期望 0x{seq & 0xFF:02X} 实收 0x{rsp[1]:02X}")
        return rsp[1]

    def transfer_exit(self, payload_crc=None) -> bytes:
        """0x37 携带整镜像 payloadCrc（契约要求），返回 0x77 正应答"""
        rsp = self.request(uds_transfer_exit(payload_crc))
        if not rsp or rsp[0] != 0x77:
            raise OtaError(f"0x37 正应答格式错误: {rsp.hex(' ')}")
        return rsp

    def routine_control(self, routine, sub=0x01, data=b"", timeout=None) -> bytes:
        return self.request(uds_routine_control(sub, routine, data), timeout=timeout)

    def ecu_reset(self, kind=0x01) -> bytes:
        rsp = self.request(uds_ecu_reset(kind))
        if not rsp or rsp[0] != 0x51:
            raise OtaError(f"0x11 正应答格式错误: {rsp.hex(' ')}")
        return rsp


# ============================== 8. 刷写流程 ==============================

@dataclass
class OtaResult:
    """一次刷写的结果（供 CLI 摘要与测试断言）"""
    block_size: int = 0
    blocks_total: int = 0
    blocks_sent: int = 0
    bytes_sent: int = 0
    frames_sent: int = 0
    start_block: int = 1
    download_addr: int = 0
    download_size: int = 0
    max_len: int = 0
    verified: bool = False
    crc_expected: int = 0
    crc_actual: int = 0
    crc_scope: str = ""
    verify_status: int = 0x00
    exit_crc: int = 0
    precheck: bytes = b""
    activated: bool = False
    activate_status: int = 0x00        # FF02 设备返回码（0x00=成功；非 0 见激活失败分支）
    reset_done: bool = False
    boot_requested: bool = False        # 已发 2E F190 01
    boot_reset_done: bool = False       # 已发 11 01 请求进 BL
    session_attempts: int = 0           # 复位后重建扩展会话的尝试次数
    committed: bool = False             # 已发 31 01 FF04
    commit_status: int = 0x00
    p2star_ms: int = None               # 设备在 50 应答里声明的 P2*（10ms 单位 -> ms）
    erase_est_ms: int = 0               # 擦除耗时保守估算


class OtaFlasher:
    """把 OtaImage 按 UDS 流程刷进 ECU（进 BL / 块划分 / 续传 / 校验 / 激活 / 落定）"""

    def __init__(self, uds: UdsClient, image: OtaImage, block_size=ISOTP_DEFAULT_BLOCK_SIZE,
                 load_addr=None, resume=None, verify=False, precheck=False,
                 precheck_did=None, keepalive=0.0, do_reset=True, block_retry=3,
                 boot_request=True, boot_wait=0.0, session_retry=SESSION_RETRY_TOTAL,
                 commit=False, timeout=None, log=None):
        if not 1 <= int(block_size) <= ISOTP_MAX_BLOCK_SIZE:
            raise OtaError(
                f"--block-size 必须在 1..{ISOTP_MAX_BLOCK_SIZE}（0x36 报文 2+块大小 需 <= "
                f"{ISOTP_MAX_MSG} 字节 ISO-TP 上限）")
        if resume is not None and int(resume) < 1:
            raise OtaError("--resume N 的 N 必须 >= 1")
        if image.app_size == 0:
            raise OtaError("镜像不含固件数据")
        self.uds = uds
        self.image = image
        self.block_size = int(block_size)
        self.load_addr = int(image.load_addr if load_addr is None else load_addr)
        self.resume = None if resume is None else int(resume)
        self.verify = bool(verify)
        self.precheck = bool(precheck)
        # --precheck-did 未显式指定时，一次读全部 F18x
        self.precheck_dids = ((int(precheck_did),) if precheck_did is not None
                              else tuple(UDS_PRECHECK_DIDS))
        self.keepalive = float(keepalive or 0.0)
        self.do_reset = bool(do_reset)
        self.block_retry = max(1, int(block_retry))
        self.boot_request = bool(boot_request)
        self.boot_wait = max(0.0, float(boot_wait or 0.0))
        self.session_retry = max(0.0, float(session_retry or SESSION_RETRY_TOTAL))
        self.commit = bool(commit)
        self.timeout = None if timeout is None else float(timeout)
        self.p2star_ms = None            # 由 50 应答解析（设备侧权威上限）
        self._log = log if callable(log) else (lambda msg: print(msg))

    def log(self, msg):
        self._log(f"[OTA] {msg}")

    def host_timeout_ms(self) -> int:
        """主机等待单条应答的实际预算（ms）：优先显式 timeout，其次取传输链上的超时"""
        if self.timeout is not None:
            return int(self.timeout * 1000)
        try:
            return int(self.uds.isotp.timeout * 1000)
        except (AttributeError, TypeError):
            return int(SESSION_RETRY_TOTAL * 1000)      # 兜底 3s

    def enter_session(self, timeout=None):
        """切扩展会话并记录设备声明的 P2*（擦除预检的设备侧权威判据）"""
        rsp = self.uds.session(UDS_SESSION_EXTENDED, timeout=timeout)
        _, p2_ms, p2star_ms = parse_session_response(rsp)
        if p2star_ms is not None:
            self.p2star_ms = p2star_ms
        self.log(f"切扩展会话 10 {UDS_SESSION_EXTENDED:02X} -> {rsp.hex(' ')}"
                 f"（P2={p2_ms}ms P2*={p2star_ms}ms）")
        return rsp

    def check_erase_budget(self, region_bytes: int, what: str):
        """擦除前估算耗时并快速失败（全槽擦除 ≈2.8s，贴近默认 --timeout 3s）。

        两个判据都要过：
          1) 主机等待预算 --timeout >= 估算擦除时间，否则超时是必然事件；
          2) 设备在 50 应答里声明的 P2* 是它自己承诺的上限，同样必须 >= 估算值。
        估算保守：页大小 2048B、每页 25ms（G4 单页擦除典型 20~40ms）。
        """
        est_ms = erase_estimate_ms(region_bytes)
        pages = (int(region_bytes) + FLASH_PAGE_SIZE - 1) // FLASH_PAGE_SIZE
        host_ms = self.host_timeout_ms()
        p2star = self.p2star_ms
        p2_text = f"{p2star}ms" if p2star is not None else "设备未声明"
        self.log(f"{what}: 目标区段 {region_bytes} 字节 ≈ {pages} 页，预计擦除 "
                 f"~{est_ms / 1000:.1f}s（保守估算 {FLASH_PAGE_ERASE_MS}ms/页；"
                 f"设备声明 P2*={p2_text}）")
        if host_ms < est_ms:
            advice = f"{(est_ms + 999) // 1000 + 1}.0"
            extra = "（全槽刷写文档建议 --timeout 6.0）" if est_ms >= 2000 else ""
            raise OtaError(
                f"单条应答超时 {host_ms / 1000:.1f}s 小于预计擦除时间 {est_ms / 1000:.1f}s，"
                f"请提高 --timeout（例如 --timeout {advice}）{extra}")
        if p2star is not None and est_ms > p2star:
            raise OtaError(
                f"预计擦除 {est_ms / 1000:.1f}s 超过设备声明的 P2* {p2star / 1000:.1f}s，"
                f"设备会在擦除完成前中止；请与固件侧确认页擦除时间或减小单次擦除区段")
        return est_ms

    def reestablish_session(self, reason=""):
        """复位后重建扩展会话：100ms 间隔连发 10 03 直到收到 50 03，总预算 session_retry。

        BL 启动窗口只有约 1s：若在窗口内没建立起会话，设备会跳转 APP，此时再刷会被
        NRC 0x22（conditionsNotCorrect）拒绝。故这里轮询而不是等一次长超时。
        """
        deadline = time.monotonic() + self.session_retry
        attempt = 0
        last = None
        if self.boot_wait > 0:
            self.log(f"等待设备重启（{self.boot_wait:.1f}s）后开始重建会话")
            time.sleep(self.boot_wait)
        while True:
            attempt += 1
            try:
                self.enter_session(timeout=SESSION_ATTEMPT_TIMEOUT)
                self.log(f"会话重建成功（第 {attempt} 次 10 03 收到 50 03）")
                return attempt
            except OtaError as exc:
                last = exc
                if time.monotonic() + SESSION_RETRY_INTERVAL >= deadline:
                    break
                time.sleep(SESSION_RETRY_INTERVAL)
        raise OtaError(
            f"{reason or '复位后'} {self.session_retry:.1f}s 内未重建扩展会话"
            f"（连发 {attempt} 次 10 03 均无 50 03 应答）：设备可能已跳转 APP，"
            f"请断电重试或使用 --no-boot-request 热升级。最近错误: {last}")

    def run(self) -> OtaResult:
        app = bytes(self.image.app_data)
        bs = self.block_size
        total = block_count(len(app), bs)
        start = self.resume if self.resume is not None else 1
        if start > total:
            raise OtaError(f"--resume {start} 超出总块数 {total}（appSize={len(app)}, 块={bs}）")
        offset = (start - 1) * bs
        result = OtaResult(block_size=bs, blocks_total=total, start_block=start)

        addr = self.load_addr + offset
        size = len(app) - offset
        result.download_addr = addr
        result.download_size = size

        # 【M10 修复】续传前置校验 —— 把"三处互相依赖的隐式约束"变成显式检查。
        #
        # 背景：续传能工作依赖三个条件同时成立，但它们在原实现里**都不检查**，
        # 任何一条不满足都只在设备侧回 NRC 0x73（块序号错）或 0x31（地址不对齐），
        # 报错信息与真实原因之间隔着三层推导，现场极难定位：
        #   (a) 块大小必须是 8 的倍数 —— 设备要求续传起始地址 8 对齐（Flash 双字编程），
        #       而 addr = load_addr + (start-1)*bs，只有 bs%8==0 才能保证任意 start 都对齐；
        #   (b) 实际偏移 offset 必须 8 对齐；
        #   (c) 序号基线必须是"绝对块号" —— 设备按 (addr - appBase)/块大小 + 1 反推期望 seq。
        # 这三条里任意一条错了，设备都无法区分"上位机算错了"和"设备算错了"，
        # 只能回一个笼统的 NRC。这里在发第一帧之前就拦下来并说清原因。
        if self.resume is not None:
            if (bs % 8) != 0:
                raise OtaError(
                    f"续传失败（前置校验）：块大小 {bs} 不是 8 的倍数（{bs} mod 8 = {bs % 8}）。"
                    f"设备要求续传起始地址 8 字节对齐（STM32G4 Flash 只支持双字 8B 编程），"
                    f"而 offset = (N-1)*{bs}，只有 bs 是 8 的倍数才能保证任意 N 都对齐。"
                    f"请改用 --block-size 1016（契约推荐值）。")
            if (offset % 8) != 0:
                raise OtaError(
                    f"续传失败（前置校验）：起始偏移 offset = (N-1)*块大小 = "
                    f"({start}-1)*{bs} = {offset}，不是 8 的倍数（{offset} mod 8 = {offset % 8}）。"
                    f"设备会回 NRC 0x31（请求地址不对齐）。请把 --resume 调整到使偏移 8 对齐的块号。")
            if (addr % 8) != 0:
                raise OtaError(
                    f"续传失败（前置校验）：续传起始地址 0x{addr:08X} 不是 8 对齐。")

        # 槽位容量护栏：绝不把下载区段伸到槽尾之外（越界擦除可能擦掉 Bootloader）
        for name, app_addr in OTA_SLOT_ADDRS.items():
            if self.load_addr == app_addr:
                slot_end = OTA_SLOT_BASES[name] + OTA_SLOT_SIZE
                if addr < app_addr or addr + size > slot_end:
                    raise OtaError(
                        f"下载区段 0x{addr:08X}..+{size} 超出 Slot {name} 容量"
                        f"（槽基 0x{OTA_SLOT_BASES[name]:08X}, 单槽 {OTA_SLOT_SIZE // 1024}KB, "
                        f"净荷上限 {OTA_IMG_MAX_PAYLOAD} 字节, 槽尾 0x{slot_end:08X}）")
                break
        else:
            self.log(f"提示: 加载地址 0x{self.load_addr:08X} 不是已知槽"
                     f"(SlotA=0x{OTA_SLOT_A_APP_ADDR:08X}/SlotB=0x{OTA_SLOT_B_APP_ADDR:08X})，"
                     f"按 --load-addr 原样使用，不做容量护栏")

        if self.boot_request:
            # ---- 阶段 0：在 APP 内请求"下次进 Bootloader"，复位后重新建立会话 ----
            self.log("--boot-request: 先复位进 Bootloader 再刷写（默认安全路径；"
                     "热升级请用 --no-boot-request）")
            self.enter_session()
            if self.precheck:
                self._run_precheck(result)
            self.log(f"置下次进 BL 请求 2E {UDS_DID_BOOT_REQ:04X} "
                     f"{UDS_BOOT_REQ_VALUE:02X}")
            self.uds.write_data_by_id(UDS_DID_BOOT_REQ, bytes([UDS_BOOT_REQ_VALUE]))
            result.boot_requested = True
            self.log("ECU 复位 11 01（进入 Bootloader，BL 启动窗口约 1s）")
            self.uds.ecu_reset(0x01)
            result.boot_reset_done = True
            result.session_attempts = self.reestablish_session("复位进 BL 后")
            result.p2star_ms = self.p2star_ms
        else:
            self.log("--no-boot-request: 热升级模式（APP 直接写入备用槽，不复位进 BL）")
            self.enter_session()
            if self.precheck:
                self._run_precheck(result)
            result.p2star_ms = self.p2star_ms

        # 擦除预算预检：整段擦除可能耗时接近 P2*/默认 timeout（快速失败 + 可操作指引）
        result.erase_est_ms = self.check_erase_budget(size, "擦除预算")

        # 不再单独发 31 01 FF00：
        #   1) 设备侧 0x34 的 BL_DiagPrepareDownload 已隐式无条件擦除目标槽；
        #   2) FF00 的 size 字段只有 16 位（上限 65535 < 单槽净荷 212864），
        #      本就无法表达整槽擦除；
        #   3) 两次擦除叠加会吃掉超时预算（单次全槽擦除估算 ~2.6s vs 默认 timeout 3.0s，
        #      真机页擦落到 30~40ms 即超时）。
        # 详见 docs/项目检查报告-2026-10-07.md B2/M9。
        if self.resume is None:
            self.log("擦除由 0x34 隐式完成（不再单独发 FF00）")
        else:
            self.log(f"续传模式 --resume {start}：0x34 只擦 addr 所在页及其后，不重擦前段")
            self.log("注意: 续传假定设备此前已写入前段数据（本次不擦除）；若设备处于全新"
                     "擦除状态，前段会缺失且 FF01 只覆盖本次声明的区段，请先做全量刷写")

        self.log(f"请求下载 34 addr=0x{addr:08X} size={size}")
        max_len = self.uds.request_download(addr, size)
        result.max_len = max_len

        if max_len and max_len < bs:
            if self.resume is not None:
                raise OtaError(
                    f"续传失败（前置校验）：节点声明最大块 {max_len} < 请求块 {bs}。"
                    f"续传的块划分必须与首次**完全一致**，否则 (N-1)*块大小 的偏移与"
                    f"设备侧反推的绝对块号就不匹配，设备只会回 NRC 0x73（块序号错）。"
                    f"请改用 --block-size {max_len}（即首次使用的块大小）后重试。")
            bs = min(max_len, ISOTP_MAX_BLOCK_SIZE)
            total = block_count(len(app), bs)
            start = 1
            offset = 0
            result.block_size = bs
            result.blocks_total = total
            self.log(f"按节点能力调整块大小 -> {bs} 字节（共 {total} 块）")

        last_activity = time.monotonic()
        for index in range(start, total + 1):
            if self.keepalive > 0 and (time.monotonic() - last_activity) >= self.keepalive:
                self.log("发送 3E 80 保持会话")
                self.uds.tester_present(suppress=True)
                last_activity = time.monotonic()
            off = (index - 1) * bs
            chunk = app[off:off + bs]
            seq = block_seq(index)
            attempt = 0
            while True:
                attempt += 1
                try:
                    self.uds.transfer_data(seq, chunk)
                    break
                except UdsNegativeResponse as exc:
                    # NRC 0x73 = wrongBlockSequenceCounter：设备不回传期望序号，直接重发该块
                    if exc.nrc == 0x73 and attempt < self.block_retry:
                        self.log(f"第 {index} 块收到 NRC 0x73，重发（第 {attempt + 1}/"
                                 f"{self.block_retry} 次）")
                        continue
                    raise
            result.blocks_sent += 1
            result.bytes_sent += len(chunk)
            last_activity = time.monotonic()

        self.log(f"传输结束 37（{result.blocks_sent}/{total} 块, {result.bytes_sent} 字节, "
                 f"payloadCrc=0x{self.image.app_crc32:08X}）")
        self.uds.transfer_exit(self.image.app_crc32)
        result.exit_crc = self.image.app_crc32

        if self.verify:
            rsp = self.uds.routine_control(UDS_ROUTINE_VERIFY)
            status, crc = parse_routine_response(rsp, UDS_ROUTINE_VERIFY)
            result.verify_status = status
            expected = crc32(app[offset:])
            result.crc_expected = expected
            if crc is not None:
                result.crc_actual = crc
                # 以本机对镜像的 CRC 交叉核对为准（续传时比对剩余区段）
                if crc != expected:
                    raise OtaError(
                        f"CRC 校验失败: 期望 0x{expected:08X}"
                        f"（{'剩余区段' if offset else '整镜像'} app[{offset}:]）, "
                        f"节点回读 0x{crc:08X}, 设备 status={status:#04x}"
                        f"({verify_status_name(status)})")
                result.verified = True
                result.crc_scope = "剩余区段" if offset else "整镜像"
                if status != 0x00:
                    self.log(f"注意: 设备 FF01 status={status:#04x}"
                             f"({verify_status_name(status)})，但回读 CRC 与本机一致，判为通过")
            else:
                # 设备只回 status（无回读 CRC）：按 status 判定
                if status != 0x00:
                    raise OtaError(f"FF01 校验失败: status={status:#04x}"
                                   f"({verify_status_name(status)})，且无回读 CRC 可交叉核对")
                result.verified = True
                result.crc_expected = expected
                result.crc_scope = "仅status(无回读CRC)"
                self.log("提示: 设备未回传 CRC，仅按 status=0 判定通过")

        # FF02 激活/提交：必须解析设备返回码。
        # 设备侧 bl_diag.c 在合成镜像头、回读校验、BL_ImageValidate、配置区落盘任一
        # 失败时都回 status=0x01。旧代码丢弃返回码直接 activated=True，
        # 会导致"激活失败却打印刷写成功"，用户看到成功而设备仍跑旧固件 —— 静默失败。
        self.log(f"激活/提交 31 01 {UDS_ROUTINE_ACTIVATE:04X}")
        rsp = self.uds.routine_control(UDS_ROUTINE_ACTIVATE)
        status, _ = parse_routine_response(rsp, UDS_ROUTINE_ACTIVATE)
        result.activate_status = status
        if status != 0x00:
            raise OtaError(
                f"31 01 {UDS_ROUTINE_ACTIVATE:04X} 激活失败: status=0x{status:02X}"
                f"（0x01 = 设备侧合成镜像头/回读校验/BL_ImageValidate/配置落盘失败）"
                f"——固件**未生效**，设备仍运行旧固件。"
                f"可读 22 F180(OTA 状态) / 22 F182(版本) / 22 F183(槽 CRC) 进一步确认，"
                f"并查串口日志中 BL: 行的具体失败原因")
        result.activated = True

        if self.do_reset:
            self.log("ECU 复位 11 01")
            self.uds.ecu_reset(0x01)
            result.reset_done = True

        if self.commit:
            # 复位后新固件以试运行启动：先重建扩展会话（FF04 在默认会话会被 NRC 0x22 拒绝），
            # 再发 31 01 FF04 立即落定（TRIAL_x -> IDLE_x），不必等 APP 自身自检窗口。
            self.log(f"落定试运行 31 01 {UDS_ROUTINE_COMMIT:04X}"
                     f"（需先重建扩展会话）")
            if self.do_reset:
                result.session_attempts = self.reestablish_session("复位试运行后")
            else:
                self.enter_session()
            rsp = self.uds.routine_control(UDS_ROUTINE_COMMIT)
            status, _ = parse_routine_response(rsp, UDS_ROUTINE_COMMIT)
            result.commit_status = status
            if status != 0x00:
                raise OtaError(
                    f"31 01 FF04 落定失败: status=0x{status:02X}"
                    f"（0x01=失败，通常是未在扩展会话/镜像未通过自检）")
            result.committed = True
            self.log("试运行已落定（TRIAL -> IDLE）")
        return result

    def _run_precheck(self, result: OtaResult):
        """22 预检：默认一次读 F180/F181/F182/F183/F184"""
        for did in self.precheck_dids:
            rsp = self.uds.read_data_by_id(did)
            payload = rsp[3:] if len(rsp) > 3 else b""
            result.precheck += rsp
            self.log(f"precheck 22 {did:04X} -> {rsp.hex(' ')}{ascii_hint(payload)}")


# ============================== 9. 日志离线重放/校验 ==============================

@dataclass
class ReplayReport:
    """日志重建结果"""
    requests: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    tx_frames: int = 0
    rx_frames: int = 0
    ignored_lines: int = 0
    block_count: int = 0
    block_bytes: int = 0
    download_size: int = 0
    crc_exit: int = None
    crc_image: int = None
    crc_log: int = None
    crc_scope: str = ""
    retries: int = 0                    # 连续重复的同一请求（复位后重建会话等正常重试）

    def ok(self) -> bool:
        return not self.problems

    def format(self) -> str:
        lines = [
            "日志重放校验",
            f"  主机请求报文 : {len(self.requests)} 条（TX 帧 {self.tx_frames}）",
            f"  节点应答报文 : {len(self.responses)} 条（RXTX 帧 {self.rx_frames}）",
            f"  0x36 数据块   : {self.block_count} 块 / {self.block_bytes} 字节"
            f"（0x34 声明 {self.download_size} 字节）",
            f"  忽略非协议行 : {self.ignored_lines}",
        ]
        if self.retries:
            lines.append(f"  无应答重发   : {self.retries} 次（如复位后 10 03 轮询）")
        if self.crc_exit is not None:
            lines.append(f"  0x37 payloadCrc: 0x{self.crc_exit:08X}")
        if self.crc_log is not None:
            lines.append(f"  FF01 回读CRC : 0x{self.crc_log:08X}")
        if self.crc_image is not None:
            lines.append(f"  本机期望CRC  : 0x{self.crc_image:08X}"
                         f"{'  ' + self.crc_scope if self.crc_scope else ''}")
        if self.problems:
            lines.append("  结果: 失败")
            lines += [f"    - {p}" for p in self.problems]
        else:
            lines.append("  结果: 通过（请求/应答配对、块序号循环、长度与CRC自洽）")
        return "\n".join(lines)


def replay_report(log_path: str, image: OtaImage = None) -> ReplayReport:
    """从 << / >> 日志离线重建 ISO-TP 报文并校验整条 UDS 刷写流程。

    校验项：请求/应答配对(含 NRC)、0x36 块序号 1..255 连续循环、块字节总数与
    0x34 声明一致、0x37 必带 payloadCrc 且等于镜像 CRC、FF01 回读 CRC 与
    期望区段一致（给出镜像时；续传按剩余区段比对）。
    """
    rep = ReplayReport()
    host_lines, dev_lines = [], []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\r\n")
                if line.startswith("<<"):
                    host_lines.append(line[2:].strip())
                elif line.startswith(">>"):
                    dev_lines.append(line[2:].strip())
                elif line.strip() and not line.lstrip().startswith("#"):
                    rep.ignored_lines += 1
    except OSError as exc:
        raise ReplayError(f"读取日志失败: {log_path}: {exc}") from None

    tx_frames = []
    for line in host_lines:
        m = TX_CMD_PATTERN.match(strip_prompt(line))
        if m:
            tx_frames.append((int(m.group(1), 16), _parse_hex_bytes(m.group(3))))
    rx_frames = []
    for line in dev_lines:
        m = RXTX_PATTERN.match(strip_prompt(line))
        if m:
            rx_frames.append((int(m.group(1), 16), _parse_hex_bytes(m.group(3))))
    rep.tx_frames = len(tx_frames)
    rep.rx_frames = len(rx_frames)

    try:
        rep.requests = isotp_reassemble(tx_frames, can_id=ISOTP_REQ_ID)
        rep.responses = isotp_reassemble(
            [(cid, fr) for cid, fr in rx_frames if cid == ISOTP_RESP_ID], can_id=ISOTP_RESP_ID)
    except IsoTpError as exc:
        rep.problems.append(f"ISO-TP 重组失败: {exc}")
        return rep

    if not rep.requests:
        rep.problems.append("日志中没有主机请求报文（缺少 << TX 7E0 ... 行）")
        return rep

    # 把"连续重复的同一请求"折叠成一条逻辑请求（复位后 10 03 轮询重建会话属正常重试）
    logical = []
    for req in rep.requests:
        if req[0] == 0x3E and len(req) >= 2 and (req[1] & UDS_SUPPRESS_POS_RSP):
            continue                                      # 抑制应答，无配对
        if logical and logical[-1] == req:
            rep.retries += 1
            continue
        logical.append(req)

    rsp_idx = 0
    prev_seq = None
    download_size = None
    xfer_exit_seen = False
    xfer_exit_crc = None
    for req in logical:
        sid = req[0]
        if rsp_idx >= len(rep.responses):
            rep.problems.append(f"请求 0x{sid:02X} 没有对应应答（应答数量不足）")
            break
        rsp = rep.responses[rsp_idx]
        rsp_idx += 1
        if rsp[0] == 0x7F:
            nrc = rsp[2] if len(rsp) > 2 else 0xFF
            rep.problems.append(
                f"请求 0x{sid:02X} 收到否定应答 NRC=0x{nrc:02X}"
                f"({NRC_NAMES.get(nrc, 'unknown')})")
            continue
        if rsp[0] != ((sid + 0x40) & 0xFF):
            rep.problems.append(
                f"请求 0x{sid:02X} 的应答 SID 异常: {rsp.hex(' ')}")
            continue
        if sid == 0x34 and len(req) == 11:
            download_size = struct.unpack(">I", req[7:11])[0]
        if sid == 0x37:
            xfer_exit_seen = True
            if len(req) == 5:
                xfer_exit_crc = struct.unpack(">I", req[1:5])[0]
            elif len(req) != 1:
                rep.problems.append(f"0x37 报文长度异常: {req.hex(' ')}")
        if sid == 0x36:
            rep.block_count += 1
            rep.block_bytes += len(req) - 2
            seq = req[1]
            if prev_seq is not None:
                expect = 1 if prev_seq == BLOCK_SEQ_MOD else prev_seq + 1
                if seq != expect:
                    rep.problems.append(
                        f"0x36 块序号不连续: 上一块 0x{prev_seq:02X}, 本块 0x{seq:02X}"
                        f"（期望 0x{expect:02X}）")
            prev_seq = seq
            if len(rsp) >= 2 and rsp[1] != seq:
                rep.problems.append(
                    f"0x36 应答块序号不符: 请求 0x{seq:02X}, 应答 0x{rsp[1]:02X}")
        if sid == 0x31 and len(req) >= 4:
            routine = (req[2] << 8) | req[3]
            if routine == UDS_ROUTINE_VERIFY:
                if len(rsp) >= 9:
                    rep.crc_log = struct.unpack(">I", rsp[5:9])[0]
                else:
                    rep.problems.append(f"FF01 应答未包含 CRC: {rsp.hex(' ')}")
    if rsp_idx < len(rep.responses):
        rep.problems.append(f"有 {len(rep.responses) - rsp_idx} 条应答没有对应请求")

    rep.download_size = download_size if download_size is not None else 0
    if download_size is not None and rep.block_bytes != download_size:
        rep.problems.append(
            f"块字节总数 {rep.block_bytes} != 0x34 声明长度 {download_size}")

    # 0x37 必须携带整镜像 payloadCrc（契约）
    if not xfer_exit_seen:
        rep.problems.append("日志中没有 0x37 传输结束报文")
    elif xfer_exit_crc is None:
        rep.problems.append("0x37 未携带 4 字节 payloadCrc（设备 FF01 只能按长度校验）")
    elif image is not None:
        rep.crc_exit = xfer_exit_crc
        if xfer_exit_crc != image.app_crc32:
            rep.problems.append(
                f"0x37 payloadCrc 0x{xfer_exit_crc:08X} != 镜像 CRC 0x{image.app_crc32:08X}")

    # FF01 回读 CRC：按 0x34 声明的区段与本机镜像交叉核对
    if rep.crc_log is not None and image is not None:
        expected = image.app_crc32
        if download_size is not None and 0 <= download_size <= image.app_size:
            off = image.app_size - download_size
            expected = crc32(image.app_data[off:])
            if download_size != image.app_size:
                rep.crc_scope = f"剩余区段 app[{off}:]（{download_size} 字节）"
        rep.crc_image = expected
        if rep.crc_log != expected:
            rep.problems.append(
                f"FF01 回读 CRC 0x{rep.crc_log:08X} != 期望 0x{expected:08X}"
                f"{'（' + rep.crc_scope + '）' if rep.crc_scope else ''}")
    if not rep.problems and rep.block_count == 0:
        rep.problems.append("日志中没有任何 0x36 数据块")
    return rep


# ============================== 10. CLI ==============================

EXAMPLES = """\
示例:
  python ota_host.py --list                                  列出串口
  python ota_host.py --make-sample sample.bin                生成确定性样例镜像
  python ota_host.py --pack app.bin --pack-out app.ota       把裸固件打包成 .ota
  python ota_host.py --info sample.bin                       查看镜像头信息
  python ota_host.py --dry-run sample.bin                    离线演练(打印全部报文并自校验)
  python ota_host.py --dry-run --verify --commit sample.bin  演练: 进BL刷写+立即落定
  python ota_host.py --dry-run --no-boot-request sample.bin  演练: 热升级(不复位进BL)
  python ota_host.py --dry-run --log run.log sample.bin      演练并保存日志
  python ota_host.py --replay run.log sample.bin             从日志离线重建并校验
  python ota_host.py --port COM5 --bitrate 500 --precheck --verify sample.bin
  python ota_host.py --port COM5 --resume 17 --verify sample.bin    断点续传

默认安全路径(--boot-request 默认开启)::
  10 03 -> [--precheck] -> 2E F190 01(置"下次进BL") -> 11 01(复位)
        -> 轮询 10 03(100ms 间隔, 最多 --session-retry 秒)直到 50 03
        -> 31 01 FF00 -> 34/36.../37 -> [--verify] 31 01 FF01 -> 31 01 FF02 -> 11 01
  注意: BL 启动窗口只有约 1s。若窗口内没建立起会话，设备会跳转 APP，
        此时刷写会被 NRC 0x22 拒绝 —— 请断电重试，或改用 --no-boot-request 热升级。
--commit 默认关闭: 试运行由 APP 自身自检窗口自动确认更安全；需要立即落定时，
        复位后先 10 03 再发 31 01 FF04（TRIAL_x -> IDLE_x）。
"""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="ota_host.py",
        description="STM32G474 OTA 上位机刷写工具（串口行协议隧道 + ISO-TP/UDS）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES)
    ap.add_argument("image", nargs="?", help="OTA 镜像文件（32字节 G4OT 头 + 固件）")
    ap.add_argument("--port", help="串口，如 COM5（真实刷写必需）")
    ap.add_argument("--baudrate", type=int, default=115200, help="串口波特率，默认 115200")
    ap.add_argument("--list", action="store_true", help="列出可用串口后退出")
    ap.add_argument("--info", action="store_true", help="解析并打印镜像头信息后退出")
    ap.add_argument("--make-sample", metavar="PATH", help="生成确定性样例镜像后退出")
    ap.add_argument("--pack", metavar="PAYLOAD",
                    help="把裸固件文件打包成 .ota 镜像后退出（payloadCrc=CRC32(净荷)）")
    ap.add_argument("--pack-out", metavar="PATH", help="--pack 的输出路径(默认 <PAYLOAD>.ota)")
    ap.add_argument("--sample-size", type=int, default=4096, help="样例固件字节数，默认 4096")
    ap.add_argument("--img-version", default="1.0", help="镜像版本 major.minor，默认 1.0")
    ap.add_argument("--slot", choices=sorted(OTA_SLOT_ADDRS), default=None,
                    help=f"目标槽位(A=0x{OTA_SLOT_A_APP_ADDR:08X}, B=0x{OTA_SLOT_B_APP_ADDR:08X})，"
                         f"默认 A")
    ap.add_argument("--load-addr", default=None,
                    help="应用区加载地址（优先于 --slot 与镜像头；默认 SlotA 0x0800C020）")
    ap.add_argument("--bitrate", type=int, choices=[125, 250, 500, 1000],
                    help="刷写前用控制台命令切换 CAN 波特率(kbps)")
    ap.add_argument("--block-size", type=int, default=ISOTP_DEFAULT_BLOCK_SIZE,
                    help=f"0x36 单块净荷字节数(1..{ISOTP_MAX_BLOCK_SIZE})，默认 "
                         f"{ISOTP_DEFAULT_BLOCK_SIZE}")
    ap.add_argument("--precheck", action="store_true",
                    help="刷写前用 22 读版本（默认依次读 " +
                         "/".join(f"{d:04X}" for d in UDS_PRECHECK_DIDS) + "）")
    ap.add_argument("--precheck-did", default=None,
                    help="只读指定 DID（默认读全部 F18x）")
    ap.add_argument("--resume", type=int, metavar="N",
                    help="从第 N 块续传（跳过擦除；块划分须与首次刷写一致）")
    ap.add_argument("--verify", action="store_true", help="传输后用 31 01 FF01 回读 CRC 比对")
    ap.add_argument("--boot-request", dest="boot_request", action="store_true", default=True,
                    help="默认开启：10 03 → 2E F190 01(置下次进BL) → 11 01 复位 → 轮询 10 03 "
                         "重建会话(BL 窗口约1s) → 再刷写。这是真机冷启动的安全路径")
    ap.add_argument("--no-boot-request", dest="boot_request", action="store_false",
                    help="热升级：不复位进 BL，由 APP 直接把固件写入备用槽（省一次复位）")
    ap.add_argument("--boot-wait", type=float, default=0.0, metavar="SEC",
                    help="复位后先等 SEC 秒再开始轮询 10 03（默认 0=立即轮询）")
    ap.add_argument("--session-retry", type=float, default=SESSION_RETRY_TOTAL, metavar="SEC",
                    help=f"复位后重建会话总预算秒数，默认 {SESSION_RETRY_TOTAL}"
                         f"（100ms 间隔连发 10 03 直到 50 03）")
    ap.add_argument("--commit", action="store_true",
                    help="刷写复位后由上位机发 31 01 FF04 立即落定试运行(TRIAL→IDLE)；"
                         "默认关闭，交给 APP 自身自检窗口确认")
    ap.add_argument("--block-retry", type=int, default=3, metavar="N",
                    help="收到 NRC 0x73(块序号不符)时重发该块的次数，默认 3")
    ap.add_argument("--keepalive", type=float, default=0.0, metavar="SEC",
                    help="空闲超过 SEC 秒自动发 3E 80 保持会话（默认关闭）")
    ap.add_argument("--no-reset", action="store_true", help="激活后不发送 11 01 复位")
    ap.add_argument("--timeout", type=float, default=3.0, help="单条响应超时秒数，默认 3.0")
    ap.add_argument("--dry-run", action="store_true",
                    help="不连硬件：用内置 ECU 模拟器完整打印报文序列并自校验")
    ap.add_argument("--replay", metavar="LOG", help="离线重放并校验日志（配合镜像可校验CRC）")
    ap.add_argument("--log", metavar="PATH", help="把收发原始行写入日志（供 --replay）")
    ap.add_argument("--quiet", action="store_true", help="不逐行打印收发报文，仅打印摘要")
    ap.add_argument("--tool-version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return ap


def _int_auto(text, what="整数"):
    """解析十进制/0x 十六进制整数"""
    try:
        return int(str(text), 0)
    except (TypeError, ValueError):
        raise OtaError(f"无法解析{what}: {text!r}") from None


def _parse_version(text):
    """'1.0' -> (1, 0)"""
    parts = str(text).split(".")
    if len(parts) != 2:
        raise OtaError(f"版本格式应为 major.minor，实收 {text!r}")
    try:
        vmaj, vmin = int(parts[0]), int(parts[1])
    except ValueError:
        raise OtaError(f"版本号必须为数字: {text!r}") from None
    if not (0 <= vmaj <= 255 and 0 <= vmin <= 255):
        raise OtaError("版本号必须在 0..255")
    return vmaj, vmin


def console_bitrate(tunnel: ConsoleTunnel, kbps: int):
    """用控制台命令切换 CAN 波特率（两节点需一致）"""
    cmd = f"bitrate {kbps}"
    tunnel.t.send_line(cmd)

    def pred(s):
        low = s.lower()
        return low != cmd.lower() and ("kbps" in low or "switch failed" in low or "only" in low)

    line = tunnel.wait_for(pred, tunnel.timeout, f"{cmd} 应答")
    low = line.lower()
    if "only" in low or "failed" in low:
        raise OtaError(f"切换波特率失败: {line}")
    return line


def cmd_list() -> int:
    try:
        _, list_ports = _import_serial()
    except OtaError as exc:
        print(f"错误: {exc}")
        return 2
    ports = list(list_ports.comports())
    if not ports:
        print("未发现串口，请检查 USB 连接/驱动。")
        return 1
    for port in ports:
        print(f"  {port.device}  {port.description}")
    return 0


def resolve_load_addr(args, default=None):
    """加载地址优先级: --load-addr > --slot > 镜像头 > 默认 SlotA"""
    if args.load_addr:
        return _int_auto(args.load_addr, "地址")
    if args.slot:
        return OTA_SLOT_ADDRS[args.slot]
    return default


def cmd_make_sample(args) -> int:
    version = _parse_version(args.img_version)
    load_addr = resolve_load_addr(args, OTA_DEFAULT_LOAD_ADDR)
    if args.sample_size <= 0:
        raise OtaError("--sample-size 必须 > 0")
    data = make_sample_image(args.sample_size, version=version, load_addr=load_addr)
    path = os.path.abspath(args.make_sample)
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"已生成确定性样例镜像: {path}")
    print(parse_image(data).describe())
    return 0


def cmd_pack(args) -> int:
    """把裸固件打包成 .ota（32 字节头仅本机校验用，传输时不进 0x34/0x36 流）"""
    payload_path = args.pack
    try:
        with open(payload_path, "rb") as fh:
            payload = fh.read()
    except OSError as exc:
        raise OtaError(f"读取固件失败: {payload_path}: {exc}") from None
    if not payload:
        raise OtaError(f"固件文件为空: {payload_path}")
    version = _parse_version(args.img_version)
    load_addr = resolve_load_addr(args, OTA_DEFAULT_LOAD_ADDR)
    out = args.pack_out or (os.path.splitext(payload_path)[0] + ".ota")
    data = build_image(payload, version=version, load_addr=load_addr)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"已打包: {os.path.abspath(out)}")
    print(parse_image(data).describe())
    print(f"传输净荷 payloadSize={len(payload)} payloadCrc32=0x{crc32(payload):08X}")
    return 0


def _make_stack(args, image):
    """按参数构造刷写器（dry-run / 真实模式共用）"""
    load_addr = resolve_load_addr(args, None)
    precheck_did = (_int_auto(args.precheck_did, "DID") if args.precheck_did else None)
    flasher = OtaFlasher(
        uds=None, image=image, block_size=args.block_size, load_addr=load_addr,
        resume=args.resume, verify=args.verify, precheck=args.precheck,
        precheck_did=precheck_did, keepalive=args.keepalive,
        do_reset=not args.no_reset, block_retry=args.block_retry,
        boot_request=args.boot_request, boot_wait=args.boot_wait,
        session_retry=args.session_retry, commit=args.commit, timeout=args.timeout)
    return flasher


def _attach_clients(flasher: OtaFlasher, tunnel: ConsoleTunnel, timeout: float):
    isotp = IsoTpClient(tunnel, timeout=timeout)
    uds = UdsClient(isotp, timeout=timeout)
    flasher.uds = uds
    return flasher


def _print_summary(result: OtaResult, extra=()):
    print(f"[摘要] 块大小={result.block_size} 块数={result.blocks_total} "
          f"已发块={result.blocks_sent} 传输字节={result.bytes_sent} "
          f"下载地址=0x{result.download_addr:08X} 节点最大块={result.max_len}")
    if result.precheck:
        print(f"[摘要] precheck 应答={result.precheck.hex(' ')}")
    if result.verified:
        read_back = (f"节点回读=0x{result.crc_actual:08X}" if result.crc_actual
                     else "设备未回读CRC")
        print(f"[摘要] CRC校验=通过({result.crc_scope}) 期望=0x{result.crc_expected:08X} "
              f"{read_back} 设备status=0x{result.verify_status:02X}")
    elif result.crc_actual:
        print(f"[摘要] 节点回读CRC=0x{result.crc_actual:08X}（未启用 --verify 比对）")
    print(f"[摘要] 0x37 payloadCrc=0x{result.exit_crc:08X} 激活={result.activated} "
          f"复位={result.reset_done}")
    if result.boot_requested or result.session_attempts:
        print(f"[摘要] 进BL模式={result.boot_requested}（2E F190 已置位={result.boot_requested}, "
              f"复位后重建会话尝试={result.session_attempts} 次）")
    if result.committed or result.commit_status:
        print(f"[摘要] 试运行落定 FF04：status=0x{result.commit_status:02X} "
              f"({verify_status_name(result.commit_status) if result.commit_status else '成功'})")
    if result.p2star_ms is not None or result.erase_est_ms:
        p2_text = f"{result.p2star_ms}ms" if result.p2star_ms is not None else "未声明"
        print(f"[摘要] 设备P2*={p2_text} 擦除估算={result.erase_est_ms}ms")
    for line in extra:
        print(line)


def cmd_dry_run(args, image: OtaImage) -> int:
    """离线演练：确定性 ECU 模拟器，完整打印将发送的报文序列并自校验"""
    sink = LineSink(verbose=not args.quiet, log_path=args.log)
    ecu = SimulatedEcu()
    transport = SimulatedLineTransport(ecu, sink=sink)
    transport.open()
    tunnel = ConsoleTunnel(transport, timeout=args.timeout)
    flasher = _attach_clients(_make_stack(args, image), tunnel, args.timeout)
    print(f"[模式] --dry-run 离线演练（内置 ECU 模拟器，不连硬件）"
          f"{'；日志=' + args.log if args.log else ''}")
    try:
        if args.bitrate:
            line = console_bitrate(tunnel, args.bitrate)
            print(f"[控制台] {line}")
        tunnel.tunnel_on()
        result = flasher.run()
        result.frames_sent = tunnel.frames_sent
        tunnel.tunnel_off()
    finally:
        sink.close()
        transport.close()

    extra = [f"[摘要] 发送CAN帧={result.frames_sent} 接收RXTX帧={tunnel.frames_recv}"]
    addr, size = result.download_addr, result.download_size
    if size:
        off = addr - flasher.load_addr
        expect = image.app_data[off:off + size]
        actual = ecu.read(addr, size)
        same = actual == expect
        extra.append(f"[自检] 模拟写入区段 0x{addr:08X}..+{size} 与镜像一致: "
                     f"{'是' if same else '否'} (CRC32=0x{crc32(actual):08X})")
        if not same:
            raise OtaError("dry-run 自检失败: 模拟器写入内容与镜像不一致"
                           f"（区段 0x{addr:08X} 共 {size} 字节）")
    _print_summary(result, extra)
    return 0


def cmd_flash(args, image: OtaImage) -> int:
    """真实串口刷写"""
    sink = LineSink(verbose=not args.quiet, log_path=args.log)
    transport = SerialLineTransport(args.port, args.baudrate, sink=sink)
    print(f"[模式] 串口刷写 {args.port} @ {args.baudrate}"
          f"{'；日志=' + args.log if args.log else ''}")
    # 隧道模式 RX 环形缓冲提示：只在真实串口模式打印（--dry-run 保持输出干净）
    hint = rx_ring_hint(args.block_size)
    if hint:
        print(hint)
    transport.open()
    tunnel = ConsoleTunnel(transport, timeout=args.timeout)
    flasher = _attach_clients(_make_stack(args, image), tunnel, args.timeout)
    try:
        if args.bitrate:
            print(f"[控制台] {console_bitrate(tunnel, args.bitrate)}")
        tunnel.tunnel_on()
        result = flasher.run()
        result.frames_sent = tunnel.frames_sent
        tunnel.tunnel_off()
    finally:
        sink.close()
        transport.close()
    _print_summary(result, [f"[摘要] 发送CAN帧={result.frames_sent} 接收RXTX帧={tunnel.frames_recv}"])
    return 0


def cmd_replay(args, image) -> int:
    """离线重放日志并校验流程"""
    print(f"[模式] --replay 离线重建校验: {args.replay}")
    report = replay_report(args.replay, image=image)
    print(report.format())
    return 0 if report.ok() else 1


def _dispatch(args) -> int:
    # 提前校验参数，保证离线模式也能立刻报错
    if args.block_size is None or not 1 <= args.block_size <= ISOTP_MAX_BLOCK_SIZE:
        raise OtaError(f"--block-size 必须在 1..{ISOTP_MAX_BLOCK_SIZE}，实收 {args.block_size}")
    _parse_version(args.img_version)

    if args.list:
        return cmd_list()
    if args.make_sample:
        return cmd_make_sample(args)
    if args.pack:
        return cmd_pack(args)

    image = load_image(args.image) if args.image else None
    if args.info:
        if image is None:
            raise OtaError("--info 需要镜像文件参数")
        print(image.describe())
        return 0
    if args.replay:
        return cmd_replay(args, image)
    if image is None:
        raise OtaError("缺少镜像文件参数（例: python ota_host.py --port COM5 image.bin）")
    if args.dry_run:
        return cmd_dry_run(args, image)
    if not args.port:
        raise OtaError("真实刷写需要 --port COMx；离线请用 --dry-run 或 --replay")
    return cmd_flash(args, image)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except OtaError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
