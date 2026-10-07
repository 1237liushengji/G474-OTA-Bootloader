#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ota_host.py 离线自证测试（无需硬件、无需网络）
================================================
运行::

    python ota/tools/test_ota_host.py          # 直接运行(unittest)
    python -m pytest ota/tools/test_ota_host.py -q

覆盖：CRC32 标准向量、OTA 镜像打包/解析/校验、ISO-TP 组帧与重组(SN 回绕/超长报错)、
UDS 0x10/0x22/0x31/0x34/0x36/0x37/0x3E/0x11 字节级编码、NRC 解析、块序号 1..255 循环、
--resume 续传、dry-run 报文序列长度自洽、--replay 日志重建校验、回归重放字节级一致。
"""

import contextlib
import io
import os
import re
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ota_host as oh                                    # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- 测试辅助

def silent(*_a, **_k):
    """吞掉刷写器日志，保持测试输出干净"""


def run_cli(argv):
    """执行 CLI，返回 (退出码, 合并后的stdout+stderr)"""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = oh.main(argv)
    return code, out.getvalue() + err.getvalue()


def write_image(path, size=1000, version=(1, 2), load_addr=oh.OTA_SLOT_A_APP_ADDR):
    data = oh.make_sample_image(size, version=version, load_addr=load_addr)
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def build_stack(timeout=1.0, ecu=None):
    """模拟器 + 隧道 + ISO-TP + UDS 客户端"""
    ecu = ecu if ecu is not None else oh.SimulatedEcu()
    transport = oh.SimulatedLineTransport(ecu)
    tunnel = oh.ConsoleTunnel(transport, timeout=timeout)
    # 【H4】注入假延时器：真实 STmin=2ms 会让满槽测试多花 ~60s（210 块 x 146 帧 x 2ms）。
    # STmin 逻辑本身由TestIsoTpFlowControl 里的专项用例断言（用记录式延时器）。
    uds = oh.UdsClient(oh.IsoTpClient(tunnel, timeout=timeout, sleep_fn=lambda _s: None),
                       timeout=timeout)
    return ecu, transport, tunnel, uds


def extract_rxtx_frames(text, can_id):
    """从 CLI 输出里提取某个 CAN ID 的 RXTX 帧（先剥离 ANSI 颜色）"""
    frames = []
    for raw in text.splitlines():
        line = ANSI_RE.sub("", raw).strip()
        for prefix in ("RX< ", "TX> "):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        m = oh.RXTX_PATTERN.match(oh.strip_prompt(line))
        if m and int(m.group(1), 16) == can_id:
            frames.append((can_id, oh._parse_hex_bytes(m.group(3))))
    return frames


def requests_from_log(path):
    """从 << / >> 日志重建主机发往 0x7E0 的应用报文序列（用于断言刷写时序）"""
    frames = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("<<"):
                continue
            m = oh.TX_CMD_PATTERN.match(oh.strip_prompt(line[2:]))
            if m and int(m.group(1), 16) == oh.ISOTP_REQ_ID:
                frames.append((oh.ISOTP_REQ_ID, oh._parse_hex_bytes(m.group(3))))
    return oh.isotp_reassemble(frames, can_id=oh.ISOTP_REQ_ID)


def run_flow_with_log(image, tmp, ecu=None, **kwargs):
    """用模拟器跑一次刷写并记录收发日志，返回 (result, 请求报文列表, ecu)"""
    log_path = os.path.join(tmp, "flow.log")
    ecu, transport, tunnel, uds = build_stack(ecu=ecu)
    sink = oh.LineSink(verbose=False, log_path=log_path)
    transport.sink = sink
    tunnel.tunnel_on()
    try:
        result = oh.OtaFlasher(uds, image, log=silent, **kwargs).run()
    finally:
        tunnel.tunnel_off()
        sink.close()
    return result, requests_from_log(log_path), ecu


class FakeSerialTransport(oh.LineTransport):
    """把 SimulatedEcu 冒充成"真实串口"，用于无硬件覆盖 cmd_flash 真实模式路径"""

    name = "fake-serial"

    def __init__(self, port, baudrate=115200, sink=None):
        super().__init__(sink)
        self.inner = oh.SimulatedLineTransport(oh.SimulatedEcu(), sink=sink)

    def open(self):
        self.inner.open()

    def close(self):
        self.inner.close()

    def send_line(self, text):
        self.inner.send_line(text)

    def read_line(self, timeout):
        return self.inner.read_line(timeout)


# ---------------------------------------------------------------- 1. CRC32

class TestCrc32(unittest.TestCase):
    def test_standard_vector(self):
        """标准向量: "123456789" -> 0xCBF43926"""
        self.assertEqual(oh.crc32(b"123456789"), 0xCBF43926)

    def test_empty_and_known(self):
        self.assertEqual(oh.crc32(b""), 0x00000000)
        self.assertEqual(oh.crc32(b"a"), 0xE8B7BE43)
        self.assertEqual(oh.crc32(b"The quick brown fox jumps over the lazy dog"),
                         0x414FA339)

    def test_matches_zlib(self):
        import zlib
        for size in (1, 7, 8, 125, 128, 4096):
            data = oh.make_sample_app(size)
            self.assertEqual(oh.crc32(data), zlib.crc32(data) & 0xFFFFFFFF)

    def test_always_unsigned(self):
        self.assertTrue(0 <= oh.crc32(b"\xff" * 32) <= 0xFFFFFFFF)


# ---------------------------------------------------------------- 2. OTA 镜像

class TestOtaImage(unittest.TestCase):
    def test_pack_parse_roundtrip(self):
        app = oh.make_sample_app(1000)
        raw = oh.build_image(app, version=(3, 7), load_addr=0x08040880)
        img = oh.parse_image(raw)
        self.assertEqual(img.app_data, app)
        self.assertEqual(img.version, (3, 7))
        self.assertEqual(img.load_addr, oh.OTA_SLOT_B_APP_ADDR)
        self.assertEqual(img.app_size, 1000)
        self.assertEqual(img.img_size, oh.OTA_HEADER_SIZE + 1000)
        self.assertEqual(raw, img.pack())
        # payloadCrc（0x37 用）就是对净荷算的 CRC32
        self.assertEqual(img.app_crc32, oh.crc32(app))

    def test_header_bytes_layout(self):
        """逐字节核对小端头（头字段仍占前 32B，槽首保留区 0x80），防止契约漂移"""
        app = bytes(range(256)) * 4                            # 1024 字节
        raw = oh.build_image(app, version=(1, 2), load_addr=oh.OTA_SLOT_A_APP_ADDR)
        self.assertEqual(raw[0:4], b"G4OT")
        self.assertEqual(raw[0:4], bytes([0x47, 0x34, 0x4F, 0x54]))
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], oh.OTA_HEADER_SIZE + 1024)   # imgSize 含整头
        self.assertEqual(struct.unpack_from("<I", raw, 8)[0], 1024)        # appSize
        self.assertEqual(raw[12], 1)                                       # major
        self.assertEqual(raw[13], 2)                                       # minor
        self.assertEqual(raw[14:16], b"\x00\x00")                          # reserved
        self.assertEqual(struct.unpack_from("<I", raw, 16)[0], 0x0800C080)
        self.assertEqual(struct.unpack_from("<I", raw, 16)[0], oh.OTA_SLOT_A_APP_ADDR)
        self.assertEqual(struct.unpack_from("<I", raw, 20)[0], oh.crc32(app))
        self.assertEqual(struct.unpack_from("<I", raw, 24)[0], oh.crc32(raw[0:24]))
        self.assertEqual(raw[28:32], b"\x00\x00\x00\x00")                  # reserved
        self.assertEqual(len(raw), oh.OTA_HEADER_SIZE + 1024)
        self.assertEqual(raw[oh.OTA_HEADER_SIZE:], app)

    def test_hdr_crc_covers_first_24_bytes(self):
        raw = oh.build_image(oh.make_sample_app(64))
        hdr_crc = struct.unpack_from("<I", raw, 24)[0]
        self.assertEqual(hdr_crc, oh.crc32(raw[0:24]))
        # 改动 [0:24] 内任意字节都会破坏 hdrCrc32
        bad = bytearray(raw)
        bad[13] ^= 0x01
        with self.assertRaises(oh.ImageFormatError) as ctx:
            oh.parse_image(bytes(bad))
        self.assertIn("hdrCrc32", str(ctx.exception))

    def test_app_crc_detects_corruption(self):
        raw = bytearray(oh.build_image(oh.make_sample_app(300)))
        raw[oh.OTA_HEADER_SIZE + 150] ^= 0xFF
        # 净荷损坏 -> appCrc32 失败（同时 hdrCrc32 仍自洽，故报错应指向 appCrc32）
        with self.assertRaises(oh.ImageFormatError) as ctx:
            oh.parse_image(bytes(raw))
        self.assertIn("appCrc32", str(ctx.exception))

    def test_bad_magic(self):
        raw = bytearray(oh.build_image(oh.make_sample_app(32)))
        raw[1] = 0x00
        with self.assertRaises(oh.ImageFormatError) as ctx:
            oh.parse_image(bytes(raw))
        self.assertIn("magic", str(ctx.exception))

    def test_truncated_and_size_mismatch(self):
        raw = oh.build_image(oh.make_sample_app(64))
        with self.assertRaises(oh.ImageFormatError):
            oh.parse_image(raw[:10])
        with self.assertRaises(oh.ImageFormatError) as ctx:
            oh.parse_image(raw[:-1])
        self.assertIn("文件长度", str(ctx.exception))
        bad = bytearray(raw)
        struct.pack_into("<I", bad, 4, oh.OTA_HEADER_SIZE + 64 + 8)             # imgSize 与 appSize 不自洽
        with self.assertRaises(oh.ImageFormatError):
            oh.parse_image(bytes(bad))

    def test_zero_app_and_reserved(self):
        with self.assertRaises(oh.ImageFormatError):
            oh.parse_image(oh.build_image(b""))
        raw = bytearray(oh.build_image(oh.make_sample_app(16)))
        raw[14] = 0x01
        with self.assertRaises(oh.ImageFormatError):
            oh.parse_image(bytes(raw))
        raw = bytearray(oh.build_image(oh.make_sample_app(16)))
        raw[30] = 0x01
        with self.assertRaises(oh.ImageFormatError):
            oh.parse_image(bytes(raw))

    def test_describe_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "img.bin")
            write_image(path, size=777)
            img = oh.load_image(path)
            text = img.describe()
            self.assertIn("appSize", text)
            self.assertIn(f"0x{oh.OTA_SLOT_A_APP_ADDR:08X}", text)
            self.assertIn("G4OT", text)
            with self.assertRaises(oh.OtaError):
                oh.load_image(os.path.join(tmp, "missing.bin"))

    def test_sample_is_deterministic(self):
        self.assertEqual(oh.make_sample_image(300), oh.make_sample_image(300))
        # 前缀稳定（同一起点、同一发生器），且不同长度内容不同
        self.assertEqual(oh.make_sample_app(300), oh.make_sample_app(301)[:300])
        self.assertNotEqual(oh.crc32(oh.make_sample_app(300)),
                            oh.crc32(oh.make_sample_app(301)))
        self.assertEqual(len(oh.make_sample_app(4096)), 4096)
        self.assertNotEqual(oh.make_sample_app(300)[:8], b"\x00" * 8)


# ---------------------------------------------------------------- 3. ISO-TP 组帧

class TestIsotpSegment(unittest.TestCase):
    def test_single_frame_exact_bytes(self):
        # UDS 10 03 -> SF: 02 10 03 + 5 字节填充
        self.assertEqual(oh.isotp_segment(bytes([0x10, 0x03])),
                         [bytes([0x02, 0x10, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00])])
        # 7 字节正好占满一帧，无填充
        self.assertEqual(oh.isotp_segment(bytes(range(1, 8))),
                         [bytes([0x07]) + bytes(range(1, 8))])
        # 不填充时长度=1+len
        self.assertEqual(oh.isotp_segment(b"\xAA", pad=False), [b"\x01\xAA"])

    def test_first_frame_exact_bytes(self):
        frames = oh.isotp_segment(bytes(range(8)))              # 8 字节 -> FF + 1 CF
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], bytes([0x10, 0x08, 0, 1, 2, 3, 4, 5]))
        self.assertEqual(frames[1], bytes([0x21, 6, 7, 0, 0, 0, 0, 0]))

    def test_max_message_frames_and_sn_wrap(self):
        """上限报文 ISO-TP 报文 1024 字节 -> 1 FF + 146 CF = 147 帧，SN 完整回绕"""
        n = oh.ISOTP_MAX_MSG
        payload = bytes((i * 7) & 0xFF for i in range(n))
        frames = oh.isotp_segment(payload)
        cf_count = (n - 6 + 6) // 7
        self.assertEqual(cf_count, 146)
        self.assertEqual(len(frames), 1 + cf_count)             # 147
        self.assertEqual(frames[0][:2], bytes([0x10 | (n >> 8), n & 0xFF]))
        self.assertEqual(frames[0][:2], bytes([0x14, 0x00]))    # 1024 = 0x400
        sns = [f[0] & 0x0F for f in frames[1:]]
        self.assertEqual(sns[0], 1)                             # FF 后首 CF SN=1
        self.assertEqual(sns[:16], list(range(1, 16)) + [0])    # SN 0..15 回绕
        self.assertEqual(sns[16], 1)
        self.assertTrue(all(len(f) == 8 for f in frames))
        self.assertEqual(oh.isotp_reassemble(frames), [payload])

    def test_all_lengths_roundtrip(self):
        for n in range(1, oh.ISOTP_MAX_MSG + 1):
            payload = bytes((i * 13 + n) & 0xFF for i in range(n))
            frames = oh.isotp_segment(payload)
            self.assertTrue(all(len(f) == 8 for f in frames), f"len={n}")
            self.assertEqual(len(frames), oh.segment_frame_count(n), f"len={n}")
            self.assertEqual(oh.isotp_reassemble(frames), [payload], f"len={n}")

    def test_too_long_must_raise_not_truncate(self):
        with self.assertRaises(oh.IsoTpTooLong):
            oh.isotp_segment(bytes(oh.ISOTP_MAX_MSG + 1))       # 1025 超限
        with self.assertRaises(oh.IsoTpTooLong):
            oh.isotp_segment(b"")
        self.assertEqual(len(oh.isotp_segment(bytes(oh.ISOTP_MAX_MSG))), 147)


class TestIsotpReassembler(unittest.TestCase):
    def test_single_and_multi(self):
        r = oh.IsoTpReassembler()
        self.assertEqual(r.feed(bytes([0x03, 0x62, 0xF1, 0x90, 0, 0, 0, 0])),
                         bytes([0x62, 0xF1, 0x90]))
        # FF 后 SN=1 开始
        self.assertIsNone(r.feed(bytes([0x10, 0x09, 1, 2, 3, 4, 5, 6])))
        self.assertEqual(r.feed(bytes([0x21, 7, 8, 9, 0, 0, 0, 0])), bytes(range(1, 10)))

    def test_ff_over_max_raises(self):
        r = oh.IsoTpReassembler()
        over = oh.ISOTP_MAX_MSG + 1                             # 1025 = 0x401
        with self.assertRaises(oh.IsoTpTooLong) as ctx:
            r.feed(bytes([0x10 | (over >> 8), over & 0xFF] + [0] * 6))
        self.assertIn(str(over), str(ctx.exception))
        legal = oh.ISOTP_MAX_MSG                                  # 1024 = 0x400 正好合法
        self.assertIsNone(r.feed(bytes([0x10 | (legal >> 8), legal & 0xFF] + [0] * 6)))

    def test_sn_error_raises(self):
        r = oh.IsoTpReassembler()
        r.feed(bytes([0x10, 0x20, 1, 2, 3, 4, 5, 6]))
        with self.assertRaises(oh.IsoTpSnError):
            r.feed(bytes([0x23] + [0] * 7))                     # 期望 SN=1, 实收 3

    def test_fc_and_orphan_cf_ignored(self):
        r = oh.IsoTpReassembler()
        self.assertIsNone(r.feed(bytes([0x30, 0x00, 0x02])))     # FC 忽略
        self.assertIsNone(r.feed(bytes([0x21] + [0] * 7)))       # 孤儿 CF 忽略
        with self.assertRaises(oh.IsoTpError):
            r.feed(bytes([0x00, 0x01, 0]))                       # SF 长度为 0 非法

    def test_reassemble_filters_by_id(self):
        payload = bytes(range(20))
        frames = oh.isotp_segment(payload)
        stream = [(0x7E0, f) for f in frames] + [(0x7E8, bytes([0x02, 0x7E, 0x00]))] * 3
        self.assertEqual(oh.isotp_reassemble(stream, can_id=0x7E0), [payload])


# ---------------------------------------------------------------- 4. UDS 编码/解析

class TestUdsEncoding(unittest.TestCase):
    def test_request_builders_byte_exact(self):
        self.assertEqual(oh.uds_session(0x03), bytes([0x10, 0x03]))
        self.assertEqual(oh.uds_session(), bytes([0x10, 0x03]))
        self.assertEqual(oh.uds_tester_present(), bytes([0x3E, 0x80]))
        self.assertEqual(oh.uds_tester_present(False), bytes([0x3E, 0x00]))
        self.assertEqual(oh.uds_read_data_by_id(0xF180), bytes([0x22, 0xF1, 0x80]))
        self.assertEqual(oh.uds_ecu_reset(0x01), bytes([0x11, 0x01]))
        self.assertEqual(oh.uds_transfer_exit(), bytes([0x37]))
        # 0x34: [34, 00, 44, addr4 大端, size4 大端]
        self.assertEqual(oh.uds_request_download(oh.OTA_SLOT_A_APP_ADDR, 0x00010000),
                         bytes([0x34, 0x00, 0x44, 0x08, 0x00, 0xC0, 0x80,
                                0x00, 0x01, 0x00, 0x00]))
        # 0x36: [36, blockSeq, data...]
        self.assertEqual(oh.uds_transfer_data(0x01, b"\xAA\xBB"),
                         bytes([0x36, 0x01, 0xAA, 0xBB]))
        # 0x37 带整镜像 payloadCrc(大端4字节) -> 长度 5
        self.assertEqual(oh.uds_transfer_exit(0x12345678),
                         bytes([0x37, 0x12, 0x34, 0x56, 0x78]))
        # 0x31 RoutineControl: [31, 01, routine2 大端]
        self.assertEqual(oh.uds_routine_control(0x01, oh.UDS_ROUTINE_ERASE),
                         bytes([0x31, 0x01, 0xFF, 0x00]))
        self.assertEqual(oh.uds_routine_control(0x01, oh.UDS_ROUTINE_VERIFY),
                         bytes([0x31, 0x01, 0xFF, 0x01]))
        self.assertEqual(oh.uds_routine_control(0x01, oh.UDS_ROUTINE_ACTIVATE),
                         bytes([0x31, 0x01, 0xFF, 0x02]))
        self.assertEqual(oh.uds_routine_control(0x01, oh.UDS_ROUTINE_ROLLBACK),
                         bytes([0x31, 0x01, 0xFF, 0x03]))

    def test_transfer_data_length_limit(self):
        """区分两个边界：契约块净荷上限 1016 vs ISO-TP 报文上限 1024"""
        bs = oh.ISOTP_MAX_BLOCK_SIZE                            # 1016
        self.assertEqual(bs, 1016)
        # 契约上限：0x36 报文 = 2 + 1016 = 1018 <= 1024 合法
        self.assertEqual(len(oh.uds_transfer_data(1, bytes(bs))), bs + 2)
        self.assertLessEqual(len(oh.uds_transfer_data(1, bytes(bs))), oh.ISOTP_MAX_MSG)
        # 净荷 bs+1 -> 报文 bs+3：ISO-TP 层仍合法（不能靠 isotp 层报错兜住），
        # 必须由 --block-size 的契约校验挡下
        self.assertLessEqual(bs + 3, oh.ISOTP_MAX_MSG)
        self.assertEqual(len(oh.uds_transfer_data(1, bytes(bs + 1))), bs + 3)
        # 净荷大到超出 ISO-TP 报文上限：isotp 层必须报错（绝不截断）
        with self.assertRaises(oh.IsoTpTooLong):
            oh.uds_transfer_data(1, bytes(oh.ISOTP_MAX_MSG + 1))
        # --block-size 校验必须把超契约值（bs+1）挡在外面
        image = oh.parse_image(oh.make_sample_image(64))
        with self.assertRaises(oh.OtaError):
            oh.OtaFlasher(None, image, block_size=bs + 1, log=silent)
        oh.OtaFlasher(None, image, block_size=bs, log=silent)   # 1016 合法

    def test_nrc_parsing(self):
        with self.assertRaises(oh.UdsNegativeResponse) as ctx:
            oh.uds_parse_response(bytes([0x7F, 0x34, 0x33]))
        exc = ctx.exception
        self.assertEqual(exc.sid, 0x34)
        self.assertEqual(exc.nrc, 0x33)
        self.assertEqual(exc.nrc_name, "securityAccessDenied")
        self.assertIn("0x33", str(exc))
        # 正应答原样返回
        self.assertEqual(oh.uds_parse_response(bytes([0x74, 0x20, 0x00, 0x7D])),
                         bytes([0x74, 0x20, 0x00, 0x7D]))
        with self.assertRaises(oh.OtaError):
            oh.uds_parse_response(b"")
        with self.assertRaises(oh.OtaError):
            oh.uds_parse_response(bytes([0x7F, 0x34]))          # NRC 长度不足

    def test_download_response(self):
        # 契约 maxLen = 1016 = 0x03F8（大端）
        self.assertEqual(oh.parse_download_response(bytes([0x74, 0x20, 0x03, 0xF8])), 1016)
        self.assertEqual(oh.parse_download_response(bytes([0x74, 0x20, 0x03, 0xF8])),
                         oh.ISOTP_MAX_BLOCK_SIZE)
        self.assertEqual(oh.parse_download_response(bytes([0x74, 0x20, 0x04, 0x00])), 1024)
        with self.assertRaises(oh.OtaError):
            oh.parse_download_response(bytes([0x74, 0x10, 0x03, 0xF8]))

    def test_routine_response_full_and_short(self):
        crc = oh.crc32(b"payload")
        rsp = bytes([0x71, 0x01, 0xFF, 0x01, 0x00]) + struct.pack(">I", crc)
        self.assertEqual(oh.parse_routine_response(rsp, 0xFF01), (0x00, crc))
        # 5 字节（仅 status，无回读 CRC）: 最小合法长度，可判定成败
        self.assertEqual(oh.parse_routine_response(bytes([0x71, 0x01, 0xFF, 0x01, 0x00]), 0xFF01),
                         (0x00, None))
        # 5 字节（status=01 CRC 不符）: 不在这里抛错，交由调用方用回读 CRC 交叉核对
        self.assertEqual(oh.parse_routine_response(bytes([0x71, 0x01, 0xFF, 0x01, 0x01]), 0xFF01),
                         (0x01, None))
        self.assertEqual(oh.verify_status_name(0x01), "CRC不符")
        with self.assertRaises(oh.OtaError):
            oh.parse_routine_response(bytes([0x71, 0x01, 0xFF, 0x02, 0x00]), 0xFF01)
        with self.assertRaises(oh.UdsNegativeResponse):
            oh.parse_routine_response(bytes([0x7F, 0x31, 0x22]), 0xFF01)

    def test_routine_response_4byte_is_fail_closed(self):
        """M8 回归：4 字节应答 [71,01,FF,01] **不含 status**（status 在索引 4）。

        旧实现 `status = rsp[4] if len(rsp) > 4 else 0x00` 会把这种截断应答
        当成"status=0 校验通过"，导致 CRC 校验被静默跳过却报告成功。
        现在默认必须 fail-closed 抛错；仅显式 allow_missing_status=True 才放行。
        """
        raw4 = bytes([0x71, 0x01, 0xFF, 0x01])
        with self.assertRaises(oh.OtaError) as cm:
            oh.parse_routine_response(raw4, 0xFF01)
        self.assertIn("缺少 status", str(cm.exception))
        # 显式放行才返回 (0x00, None)
        self.assertEqual(
            oh.parse_routine_response(raw4, 0xFF01, allow_missing_status=True), (0x00, None))


class TestBlockSeq(unittest.TestCase):
    def test_cycle_1_to_255(self):
        self.assertEqual(oh.block_seq(1), 1)
        self.assertEqual(oh.block_seq(2), 2)
        self.assertEqual(oh.block_seq(254), 254)
        self.assertEqual(oh.block_seq(255), 255)
        self.assertEqual(oh.block_seq(256), 1)                  # 255 -> 1（不是 0）
        self.assertEqual(oh.block_seq(510), 255)
        self.assertEqual(oh.block_seq(511), 1)
        self.assertNotIn(0, [oh.block_seq(i) for i in range(1, 1000)])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            oh.block_seq(0)
        with self.assertRaises(ValueError):
            oh.block_seq(-3)

    def test_block_count(self):
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE                        # 1016
        self.assertEqual(oh.block_count(4096, bs), 5)           # ceil(4096/1016)
        self.assertEqual(oh.block_count(bs, bs), 1)
        self.assertEqual(oh.block_count(bs + 1, bs), 2)
        self.assertEqual(oh.block_count(1, bs), 1)
        self.assertEqual(oh.block_count(oh.OTA_IMG_MAX_PAYLOAD, bs), 210)   # 满槽 208KB-0x80


# ---------------------------------------------------------------- 5. 行协议层

class TestLineProtocol(unittest.TestCase):
    def test_strip_prompt(self):
        self.assertEqual(oh.strip_prompt("> TUN ON"), "TUN ON")
        self.assertEqual(oh.strip_prompt("> TUN ON\r"), "TUN ON")
        self.assertEqual(oh.strip_prompt(">>> x"), ">>> x")
        self.assertEqual(oh.strip_prompt("tunnel: on"), "tunnel: on")
        self.assertEqual(oh.strip_prompt(">"), "")

    def test_command_line_builders(self):
        self.assertEqual(oh.tx_cmd_line(0x7E0, bytes([0x02, 0x10, 0x03])),
                         "TX 7E0 3 02 10 03")
        self.assertEqual(oh.tx_cmd_line(0x7E8, bytes([0x30, 0x00, 0x02])),
                         "TX 7E8 3 30 00 02")
        self.assertEqual(oh.rxtx_line(0x7E8, bytes([0x06, 0x50, 0x03])),
                         "RXTX 7E8 3 06 50 03")
        self.assertTrue(oh.RXTX_PATTERN.match("RXTX 7E0 8 02 10 03 00 00 00 00 00"))
        self.assertTrue(oh.TX_CMD_PATTERN.match("TX 7E0 8 02 10 03 00 00 00 00 00"))
        self.assertIsNone(oh.RXTX_PATTERN.match("RX 1234 7E0 8 02"))

    def test_missing_pyserial_message(self):
        """缺 pyserial 时应给出可操作的 TransportError，而不是裸 ImportError 崩栈。

        2026-10-07 修正（原 E5）：旧实现用 `sys.modules["serial"] = None` 伪造缺失，
        但 Python 3.12+ 下 `import serial` 见到 None 会抛 KeyError/ImportError 之外的
        行为，且测试尾部 `oh._import_serial()` 硬依赖"本机装了 pyserial"，
        导致在**未安装 pyserial 的机器上整个测试 ERROR**（离线环境跑不了）。
        现在改用 mock.patch.dict 精确控制 __import__，两条路径都不依赖本机环境。
        """
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "serial" or name.startswith("serial."):
                raise ImportError("No module named 'serial'")
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(sys.modules, {}, clear=False), \
                mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(oh.TransportError) as ctx:
                oh._import_serial()
        self.assertIn("pyserial", str(ctx.exception))

        # 有 pyserial 时应能正常导入（用 stub 模拟，不依赖本机是否安装）
        fake_serial = types.ModuleType("serial")
        fake_serial.Serial = object                          # noqa: F841
        fake_tools = types.ModuleType("serial.tools")
        fake_list_ports = types.ModuleType("serial.tools.list_ports")
        fake_list_ports.comports = lambda *a, **k: []        # noqa: F841
        with mock.patch.dict(sys.modules, {
                "serial": fake_serial,
                "serial.tools": fake_tools,
                "serial.tools.list_ports": fake_list_ports,
        }):
            serial_mod, list_ports_mod = oh._import_serial()
            self.assertIs(serial_mod, fake_serial)
            self.assertIs(list_ports_mod, fake_list_ports)


# ---------------------------------------------------------------- 6. 模拟器/隧道契约

class TestTunnelContract(unittest.TestCase):
    def test_tunnel_on_off_and_rxtx_gating(self):
        ecu, _, tunnel, _ = build_stack()
        tunnel.tunnel_on()
        tunnel.tx_frame(0x7E0, bytes([0x02, 0x10, 0x03, 0, 0, 0, 0, 0]))
        _, frame = tunnel.recv_frame(0.5, can_id=0x7E8)
        self.assertEqual(frame[0], 0x06)                        # 50 03 ... 单帧应答
        tunnel.tunnel_off()
        tunnel.tx_frame(0x7E0, bytes([0x02, 0x10, 0x03, 0, 0, 0, 0, 0]))
        with self.assertRaises(oh.IsoTpTimeout):
            tunnel.recv_frame(0.2, can_id=0x7E8)                # 隧道关闭 -> 无 RXTX

    def test_txerr_is_reported(self):
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        with self.assertRaises(oh.TransportError) as ctx:
            tunnel.tx_frame(0x800, b"\x00")                     # ID 越界 -> 节点回 TXERR
        self.assertIn("TXERR", str(ctx.exception))
        with self.assertRaises(oh.OtaError):
            tunnel.tx_frame(0x7E0, bytes(9))                    # 本机就拒绝 >8 字节

    def test_simulator_console_compat(self):
        ecu = oh.SimulatedEcu()
        self.assertIn("switched to 500kbps", ecu.handle_line("bitrate 500")[0])
        self.assertIn("only 125", ecu.handle_line("bitrate 123")[0])
        self.assertIn("unknown cmd", ecu.handle_line("bogus")[0])
        self.assertIn("tunnel: on", ecu.handle_line("TUN ON"))
        self.assertTrue(ecu.tunnel)
        self.assertIn("tunnel: off", ecu.handle_line("TUN OFF"))
        self.assertFalse(ecu.tunnel)
        self.assertEqual(ecu.handle_line("TX 7E0 8 02"), ["TXERR"])   # 字节数不符

    def test_console_bitrate(self):
        _, _, tunnel, _ = build_stack()
        self.assertIn("500kbps", oh.console_bitrate(tunnel, 500))

    def test_uds_client_nrc_surfaces(self):
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        uds.session(0x03)
        with self.assertRaises(oh.UdsNegativeResponse) as ctx:
            uds.read_data_by_id(0xF1FF)                         # 模拟器未知 DID -> NRC 0x31
        self.assertEqual(ctx.exception.nrc, 0x31)
        self.assertEqual(ctx.exception.sid, 0x22)

    def test_uds_client_pending_0x78(self):
        """0x78 应答需被跳过并继续等最终应答（同一请求后紧跟真正的应答）"""

        class PendingFirst(oh.SimulatedEcu):
            def __init__(self):
                super().__init__()
                self._pending_sent = False

            def _respond(self, req):
                if req[0] == 0x22 and not self._pending_sent:
                    self._pending_sent = True
                    real = self._uds_handle(req)
                    # 先回 0x78 单帧，再把真正的应答作为独立 ISO-TP 报文发出
                    return (oh.isotp_segment(bytes([0x7F, 0x22, 0x78]))
                            + oh.isotp_segment(real))
                return super()._respond(req)

        ecu = PendingFirst()
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        rsp = uds.read_data_by_id(0xF182)                       # F182=已装版本(4B) -> SF
        self.assertEqual(rsp[0], 0x62)
        self.assertEqual(rsp[1:3], bytes([0xF1, 0x82]))
        self.assertEqual(len(rsp), 7)                           # 62 F1 82 + 4B
        self.assertEqual(rsp[3:], ecu.ota_version)

    def test_uds_client_pending_0x78_has_total_budget(self):
        """【L6】设备持续回 0x78 时必须按累计预算超时退出，不能无限挂死。

        原实现每次 receive_message(timeout) 都重置完整 timeout，设备一直回 0x78
        就永远等不到最终应答。现改为累计 deadline，超预算抛 IsoTpTimeout。

        注：不复用 SimulatedEcu —— 它的 _respond 是一次性应答机制，发完 0x78 就
        不再产生帧，无法模拟"持续 0x78"。故直接桩掉 receive_message，精准隔离
        被测逻辑（0x78 等待循环的预算控制）。
        """
        isotp = mock.Mock()
        isotp.exchange.return_value = bytes([0x7F, 0x22, 0x78])   # 首个应答就是 0x78
        isotp.receive_message.return_value = bytes([0x7F, 0x22, 0x78])  # 之后永远 0x78
        uds = oh.UdsClient(isotp, timeout=3.0)

        budget_save = oh.UDS_PENDING_TOTAL_BUDGET
        oh.UDS_PENDING_TOTAL_BUDGET = 0.05          # 50ms 足够跑多轮循环
        try:
            t0 = time.monotonic()
            with self.assertRaises(oh.IsoTpTimeout) as ctx:
                uds.read_data_by_id(0xF182)
            elapsed = time.monotonic() - t0
            # 每次传入的应是**剩余**时间，且必须单调递减（这是本次修复的核心）
            passes = [c.args[0] for c in isotp.receive_message.call_args_list]
        finally:
            oh.UDS_PENDING_TOTAL_BUDGET = budget_save

        # 关键 1：必须真的抛超时，而不是挂死或返回垃圾
        self.assertIn("次 0x78", str(ctx.exception))
        # 关键 2：确实循环了多次（不是一次就放弃）
        self.assertGreaterEqual(len(passes), 2, f"0x78 循环次数异常: {len(passes)}")
        # 关键 3：传入的时间必须**递减**——旧实现每次都传完整 timeout(3.0)，
        #         即 3.0 恒定不变，正是"无限挂死"的成因
        self.assertLess(passes[-1], passes[0],
                        f"剩余时间未递减(首={passes[0]:.4f} 末={passes[-1]:.4f})，"
                        f"L6 修复未生效")
        for p in passes:
            self.assertLessEqual(p, 0.05 + 1e-6, f"单次等待 {p:.4f}s 超出累计预算")
        # 关键 4：总耗时应受预算约束
        self.assertLess(elapsed, 5.0, f"累计预算未生效，耗时 {elapsed:.2f}s")


# ---------------------------------------------------------------- 7. 端到端刷写（模拟器）

class TestIsoTpFlowControl(unittest.TestCase):
    """发送方向的流控：BS=0 / BS=N / WT / OVFLW（用桩隧道记录帧序列）"""

    class _Stub:
        def __init__(self, fcs):
            self.fcs = list(fcs)
            self.sent = []

        def tx_frame(self, can_id, frame):
            self.sent.append((can_id, bytes(frame)))

        def recv_frame(self, timeout, can_id=None, pci=None):
            if not self.fcs:
                raise oh.IsoTpTimeout("桩隧道没有更多 FC 帧")
            return (oh.ISOTP_RESP_ID, bytes(self.fcs.pop(0)))

    def test_bs0_sends_all_cfs_after_single_fc(self):
        payload = bytes((i * 5) & 0xFF for i in range(128))
        stub = self._Stub([bytes([0x30, 0x00, 0x02])])
        oh.IsoTpClient(stub, timeout=0.1).send_message(payload)
        self.assertEqual(len(stub.sent), 19)                    # FF + 18 CF
        self.assertEqual([f[0] & 0x0F for _, f in stub.sent[1:]][:3], [1, 2, 3])
        self.assertEqual(oh.isotp_reassemble([f for _, f in stub.sent]), [payload])

    def test_bs1_waits_for_fc_each_block(self):
        payload = bytes(128)
        stub = self._Stub([bytes([0x30, 0x01, 0x02])] * 18)     # 每个 CF 前一个 FC
        oh.IsoTpClient(stub, timeout=0.1).send_message(payload)
        self.assertEqual(len(stub.sent), 19)
        self.assertEqual(stub.fcs, [])

    def test_wait_then_cts(self):
        stub = self._Stub([bytes([0x31, 0x00, 0x02]),           # FS=WT：继续等
                           bytes([0x30, 0x00, 0x02])])
        oh.IsoTpClient(stub, timeout=0.1).send_message(bytes(20))
        self.assertEqual(len(stub.sent), 3)                     # FF + 2 CF

    def test_overflow_aborts(self):
        stub = self._Stub([bytes([0x32, 0x00, 0x00])])           # FS=OVFLW
        with self.assertRaises(oh.IsoTpOverflow):
            oh.IsoTpClient(stub, timeout=0.1).send_message(bytes(20))

    def test_host_fc_bytes_match_contract(self):
        """主机收到 FF 时应回 CTS,BS=0,STmin=2ms（30 00 02），与 isotp.c 一致"""
        ecu = oh.SimulatedEcu()
        _, transport, tunnel, uds = build_stack(ecu=ecu)
        sent = []
        original = transport.send_line

        def spy(text):
            sent.append(text)
            original(text)

        transport.send_line = spy
        tunnel.tunnel_on()
        rsp = uds.read_data_by_id(0xF183)                       # 双槽 CRC(8B) -> 多帧应答
        self.assertEqual(rsp[0], 0x62)
        self.assertEqual(len(rsp[3:]), 8)
        self.assertGreater(len(rsp[3:]), 7)
        self.assertIn("TX 7E0 3 30 00 02", sent)

    # ---------------- 【H4】STmin 必须被遵守 ----------------

    class _RecordingSleeper:
        """记录所有 sleep 调用的假延时器（避免单测真的等）"""

        def __init__(self):
            self.calls = []

        def __call__(self, seconds):
            self.calls.append(seconds)

    def test_stmin_is_honored_between_cfs(self):
        """FC 的 STmin=2ms 必须在 CF 之间真正延时（原实现完全忽略该字段）"""
        payload = bytes(128)                                    # -> FF + 18 CF
        stub = self._Stub([bytes([0x30, 0x00, 0x02])])         # BS=0(全发), STmin=2ms
        sleeper = self._RecordingSleeper()
        oh.IsoTpClient(stub, timeout=0.1, sleep_fn=sleeper).send_message(payload)
        # 18 个 CF -> 17 个间隔（首帧紧跟 FC，不计）
        self.assertEqual(len(sleeper.calls), 17)
        self.assertTrue(all(abs(c - 0.002) < 1e-9 for c in sleeper.calls),
                        sleeper.calls)

    def test_first_cf_not_delayed(self):
        """ISO-TP 语义：STmin 约束 CF **之间**的间隔，FC 到首个 CF 不延时"""
        stub = self._Stub([bytes([0x30, 0x00, 0x02])])
        sleeper = self._RecordingSleeper()
        oh.IsoTpClient(stub, timeout=0.1, sleep_fn=sleeper).send_message(bytes(20))
        self.assertEqual(len(sleeper.calls), 1)                 # FF + 2 CF -> 仅 1 个间隔

    def test_stmin_between_blocks_not_after_last(self):
        """BS=N 分批时每批内部遵守 STmin，且不产生多余的批间延时"""
        payload = bytes(128)                                    # 18 CF
        stub = self._Stub([bytes([0x30, 0x05, 0x02])] * 4)     # BS=5 -> 5+5+5+3
        sleeper = self._RecordingSleeper()
        oh.IsoTpClient(stub, timeout=0.1, sleep_fn=sleeper).send_message(payload)
        # 4 批分别产生 4+4+4+2 = 14 个批内间隔；批与批之间隔着 FC 往返，不额外延时
        self.assertEqual(len(sleeper.calls), 14)

    def test_stmin_zero_means_no_delay(self):
        """STmin=0 -> 合法（对端允许背靠背），不得凭空延时"""
        stub = self._Stub([bytes([0x30, 0x00, 0x00])])
        sleeper = self._RecordingSleeper()
        oh.IsoTpClient(stub, timeout=0.1, sleep_fn=sleeper).send_message(bytes(128))
        self.assertEqual(sleeper.calls, [])

    def test_stmin_encoding_decode(self):
        """ISO 15765-2 STmin 编码解码：0x00..0x7F=ms，0xF1..0xF9=100us步长"""
        f = oh.isotp_stmin_to_seconds
        self.assertEqual(f(0x00), 0.0)
        self.assertAlmostEqual(f(0x02), 0.002)                 # 本工程设备侧用的值
        self.assertAlmostEqual(f(0x7F), 0.127)
        self.assertAlmostEqual(f(0xF1), 0.0001)                 # 100 us
        self.assertAlmostEqual(f(0xF9), 0.0009)                 # 900 us
        self.assertEqual(f(0xFF), 0.0)                # 保留 -> 不人为限速
        self.assertEqual(f(0x80), 0.0)                        # 保留区间

    def test_block_frame_count_matches_stmin_budget(self):
        """硬件尺度自证：满槽 210 块 x 146 帧 x STmin 2ms 的理论下界（仅估算，不 sleep）"""
        frames = oh.block_frame_count(oh.ISOTP_DEFAULT_BLOCK_SIZE)   # 146
        blocks = oh.block_count(oh.OTA_IMG_MAX_PAYLOAD, oh.ISOTP_DEFAULT_BLOCK_SIZE)  # 210
        total_cf = blocks * (frames - 1)
        # 每帧间延时 2ms（首帧不计）
        self.assertEqual(total_cf, 210* 145)
        self.assertGreater(total_cf * 0.002, 30.0)               # >30s，说明 STmin 不是可选项


class TestOtaFlow(unittest.TestCase):
    def test_full_flow_with_verify(self):
        image = oh.parse_image(oh.make_sample_image(4096))
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        flasher = oh.OtaFlasher(uds, image, verify=True, precheck=True, log=silent)
        result = flasher.run()
        self.assertEqual(result.block_size, oh.ISOTP_DEFAULT_BLOCK_SIZE)   # 1016
        self.assertEqual(result.blocks_total, 5)                # ceil(4096/1016)
        self.assertEqual(result.blocks_sent, 5)
        self.assertEqual(result.bytes_sent, 4096)
        self.assertEqual(result.download_addr, oh.OTA_SLOT_A_APP_ADDR)
        self.assertEqual(result.download_size, 4096)
        self.assertEqual(result.exit_crc, image.app_crc32)      # 0x37 带整镜像 payloadCrc
        self.assertTrue(result.verified)
        self.assertTrue(result.activated)
        self.assertTrue(result.reset_done)
        self.assertEqual(ecu.erase_count, 1)
        self.assertEqual(ecu.payload_crc, image.app_crc32)      # 设备侧参考值
        # 设备收到的净荷 == 镜像净荷（不含 32 字节头）
        self.assertEqual(ecu.read(oh.OTA_SLOT_A_APP_ADDR, image.app_size), image.app_data)
        self.assertEqual(result.precheck.count(0x62), 5)        # 一次读 5 个 F18x

    def test_no_header_in_transfer_stream(self):
        """传输流必须只含净荷：0x34 size 与 0x36 累计字节都等于 appSize"""
        image = oh.parse_image(oh.make_sample_image(1000))
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, log=silent).run()
        self.assertEqual(result.download_size, image.app_size)
        self.assertEqual(result.bytes_sent, image.app_size)
        self.assertNotEqual(result.download_size, image.img_size)

    def test_block_size_configurable(self):
        image = oh.parse_image(oh.make_sample_image(1000))
        cases = ((1, 1000), (7, 143), (64, 16), (125, 8), (512, 2), (1000, 1),
                 (oh.ISOTP_MAX_BLOCK_SIZE, 1))                  # 1016 -> 1 块
        for bs, expect in cases:
            ecu, _, tunnel, uds = build_stack()
            tunnel.tunnel_on()
            result = oh.OtaFlasher(uds, image, block_size=bs, log=silent).run()
            self.assertEqual(result.block_size, bs)
            self.assertEqual(result.blocks_total, expect, f"block_size={bs}")
            self.assertEqual(ecu.read(image.load_addr, image.app_size), image.app_data)

    def test_block_size_over_limit_rejected(self):
        """上限由契约决定：1016 合法；1017 在 ISO-TP 层虽合法但必须被拒（超契约）"""
        image = oh.parse_image(oh.make_sample_image(64))
        valid = oh.ISOTP_MAX_BLOCK_SIZE                          # 1016
        oh.OtaFlasher(None, image, block_size=valid, log=silent)
        for bad in (0, -1, valid + 1, valid + 100):
            with self.assertRaises(oh.OtaError):
                oh.OtaFlasher(None, image, block_size=bad, log=silent)

    def test_erase_skipped_on_resume_and_tail_crc(self):
        """断点续传：跳过擦除、34 只声明剩余区段、FF01 比对剩余区段 CRC"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE                        # 1016
        image = oh.parse_image(oh.make_sample_image(bs * 6))    # 6 块
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        ecu.block_fault_index = 5                               # 第 5 块故障 -> NRC 0x73
        first = oh.OtaFlasher(uds, image, do_reset=False, block_retry=2, log=silent)
        with self.assertRaises(oh.UdsNegativeResponse) as ctx:
            first.run()
        self.assertEqual(ctx.exception.nrc, 0x73)
        self.assertEqual(ecu.block_index, 5)                    # 未写入第 5 块
        self.assertEqual(ecu.erase_count, 1)

        ecu.block_fault_index = None
        second = oh.OtaFlasher(uds, image, resume=5, verify=True, do_reset=False, log=silent)
        result = second.run()
        self.assertEqual(result.start_block, 5)
        self.assertEqual(result.download_addr, image.load_addr + 4 * bs)
        self.assertEqual(result.download_size, image.app_size - 4 * bs)
        self.assertTrue(result.verified)
        self.assertEqual(result.crc_expected, oh.crc32(image.app_data[4 * bs:]))   # 剩余区段
        self.assertEqual(result.crc_scope, "剩余区段")
        self.assertEqual(ecu.erase_count, 1)                    # 续传没有再擦除
        self.assertEqual(ecu.read(image.load_addr, image.app_size), image.app_data)

    def test_resume_seq_is_derived_from_address(self):
        """设备按 0x34 的 addr 推导期望块序号（复位/重启后仍能接受续传）"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE                        # 1016
        image = oh.parse_image(oh.make_sample_image(bs * 6))
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        uds.session(oh.UDS_SESSION_EXTENDED)
        max_len = uds.request_download(oh.OTA_SLOT_A_APP_ADDR + 4 * bs, bs * 2)
        self.assertEqual(max_len, bs)
        with self.assertRaises(oh.UdsNegativeResponse) as ctx:
            uds.transfer_data(oh.block_seq(4), bytes(10))       # 错序号 -> NRC 0x73
        self.assertEqual(ctx.exception.nrc, 0x73)
        uds.transfer_data(oh.block_seq(5), bytes(10))           # addr 推导出的序号 = 5
        # 全量下载（offset=0）时期望序号回到 1
        uds.request_download(oh.OTA_SLOT_A_APP_ADDR, image.app_size)
        uds.transfer_data(oh.block_seq(1), bytes(10))

    def test_resume_out_of_range_raises(self):
        image = oh.parse_image(oh.make_sample_image(1000))
        _, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError):
            oh.OtaFlasher(uds, image, resume=99, log=silent).run()   # 超出总块数

    def test_resume_on_fresh_device_only_covers_tail(self):
        """续传只覆盖所声明区段：全新设备上前段不会被写入（操作者须确保是断点续传）"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        image = oh.parse_image(oh.make_sample_image(bs * 6))    # 6 块
        skip = 4 * bs                                           # 前 4 块未写
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, ecu = run_flow_with_log(image, tmp, resume=5, verify=True)
        self.assertEqual(result.download_addr, image.load_addr + skip)
        self.assertEqual(result.download_size, image.app_size - skip)
        self.assertTrue(result.verified)                        # 只校验了剩余区段
        self.assertEqual(result.crc_scope, "剩余区段")
        self.assertEqual(ecu.read(image.load_addr, skip), bytes(skip))    # 前段为空（未写入）
        self.assertEqual(ecu.read(image.load_addr + skip, image.app_size - skip),
                         image.app_data[skip:])

    def test_resume_logs_warning(self):
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        image = oh.parse_image(oh.make_sample_image(bs * 3))
        logs = []
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        oh.OtaFlasher(uds, image, resume=2, do_reset=False, log=logs.append).run()
        self.assertTrue(any("续传" in line and "全新" in line for line in logs), logs)

    def test_verify_detects_corruption(self):
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        image = oh.parse_image(oh.make_sample_image(bs * 3))    # 3 块，损坏第 2 块
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        ecu.corrupt_block_index = 2                             # 设备写入被损坏 1 字节
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, verify=True, log=silent).run()
        self.assertIn("CRC 校验失败", str(ctx.exception))

    def test_full_slot_scale_default_block_size(self):
        """硬件尺度自证：满槽 212960B + 默认块 1016 -> 210 块（<255，不回绕）端到端可刷"""
        image = oh.parse_image(oh.make_sample_image(oh.OTA_IMG_MAX_PAYLOAD))
        ecu, _, tunnel, uds = build_stack(timeout=5.0)
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, verify=True, do_reset=False, log=silent).run()
        self.assertEqual(result.block_size, oh.ISOTP_DEFAULT_BLOCK_SIZE)   # 1016
        self.assertEqual(result.blocks_total, 210)              # ceil(212960/1016)
        self.assertEqual(result.bytes_sent, oh.OTA_IMG_MAX_PAYLOAD)
        self.assertTrue(result.verified)
        self.assertEqual(ecu.read(image.load_addr, image.app_size), image.app_data)

    def test_verify_status_only_response(self):
        """设备只回 status(无回读 CRC) 时按 status 判定"""

        class StatusOnly(oh.SimulatedEcu):
            def _uds_handle(self, req):
                if req[0] == 0x31 and len(req) >= 4 and ((req[2] << 8) | req[3]) == 0xFF01:
                    return bytes([0x71, 0x01, 0xFF, 0x01, 0x00])
                return super()._uds_handle(req)

        image = oh.parse_image(oh.make_sample_image(300))
        ecu = StatusOnly()
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, verify=True, log=silent).run()
        self.assertTrue(result.verified)
        self.assertEqual(result.crc_actual, 0)
        self.assertIn("status", result.crc_scope)

    def test_verify_crc_mismatch_raises(self):
        class BadCrc(oh.SimulatedEcu):
            def _uds_handle(self, req):
                if req[0] == 0x31 and len(req) >= 4 and ((req[2] << 8) | req[3]) == 0xFF01:
                    return bytes([0x71, 0x01, 0xFF, 0x01, 0x00]) + struct.pack(">I", 0xDEADBEEF)
                return super()._uds_handle(req)

        image = oh.parse_image(oh.make_sample_image(300))
        ecu = BadCrc()
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, verify=True, log=silent).run()
        self.assertIn("0xDEADBEEF", str(ctx.exception))

    def test_seq_wraps_after_255_blocks(self):
        """263 块 (>255) 时块序号必须回绕: 设备侧按同一公式校验, 不一致会回 NRC 0x73"""
        bs = 512                                                # 用小块才可能 >255 块
        size = bs * 263                                         # 134656 字节 < 单槽上限
        self.assertLessEqual(size, oh.OTA_IMG_MAX_PAYLOAD)
        image = oh.parse_image(oh.make_sample_image(size))
        ecu, _, tunnel, uds = build_stack(timeout=2.0)
        tunnel.tunnel_on()
        flasher = oh.OtaFlasher(uds, image, block_size=bs, verify=True,
                               do_reset=False, log=silent)
        result = flasher.run()
        self.assertEqual(result.blocks_total, 263)
        self.assertEqual(ecu.read(image.load_addr, image.app_size), image.app_data)
        self.assertTrue(result.verified)

    def test_keepalive_sends_tester_present(self):
        image = oh.parse_image(oh.make_sample_image(300))
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "keepalive.log")
            ecu, transport, tunnel, uds = build_stack()
            sink = oh.LineSink(verbose=False, log_path=log_path)
            transport.sink = sink
            tunnel.tunnel_on()
            # keepalive 极小 -> 每块前都触发 3E 80
            oh.OtaFlasher(uds, image, keepalive=1e-9, log=silent).run()
            tunnel.tunnel_off()
            sink.close()
            with open(log_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        self.assertGreaterEqual(text.count("3E 80"), 1)
        self.assertIn("02 3E 80", text)

    def test_no_reset_flag(self):
        image = oh.parse_image(oh.make_sample_image(300))
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, do_reset=False, log=silent).run()
        self.assertFalse(result.reset_done)
        self.assertTrue(result.activated)
        self.assertTrue(ecu.tunnel)                             # 未复位 -> 隧道仍开

    def test_adaptive_block_size_from_node(self):
        """节点声明 maxLen < 请求块大小时，全量刷写自动缩小块（不改 34 的 size）"""
        image = oh.parse_image(oh.make_sample_image(1000))
        ecu = oh.SimulatedEcu(max_block=64)
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, block_size=125, log=silent).run()
        self.assertEqual(result.max_len, 64)
        self.assertEqual(result.block_size, 64)
        self.assertEqual(result.blocks_total, 16)
        self.assertEqual(result.download_size, 1000)
        self.assertEqual(ecu.read(image.load_addr, image.app_size), image.app_data)

    def test_resume_block_size_mismatch_errors(self):
        """续传时块划分必须与首次一致：节点 maxLen 更小必须报错而不是静默改块"""
        image = oh.parse_image(oh.make_sample_image(1000))
        ecu = oh.SimulatedEcu(max_block=25)                     # 25 整除 125，续传地址对齐
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, block_size=125, resume=3, log=silent).run()
        self.assertIn("block-size", str(ctx.exception))

    def test_rxtx_only_for_diagnostic_ids(self):
        """契约: 隧道只在 7E0/7E8 上打印 RXTX"""
        ecu = oh.SimulatedEcu()
        self.assertEqual(ecu.handle_line("TUN ON"), ["tunnel: on"])
        self.assertEqual(ecu.handle_line("TX 123 2 11 22"), ["TXOK"])   # 非诊断 ID 无 RXTX
        lines = ecu.handle_line("TX 7E0 3 02 10 03")
        self.assertIn("TXOK", lines)
        self.assertTrue(any(line.startswith("RXTX 7E0") for line in lines))


# ---------------------------------------------------------------- 7b. 进BL/落定时序

class TestBootRequestAndCommit(unittest.TestCase):
    """--boot-request（默认）与 --commit（31 01 FF04）的真机时序"""

    def test_uds_encoders(self):
        self.assertEqual(oh.uds_write_data_by_id(oh.UDS_DID_BOOT_REQ, b"\x01"),
                         bytes([0x2E, 0xF1, 0x90, 0x01]))
        self.assertEqual(oh.uds_routine_control(0x01, oh.UDS_ROUTINE_COMMIT),
                         bytes([0x31, 0x01, 0xFF, 0x04]))
        self.assertEqual(oh.UDS_DID_BOOT_REQ, 0xF190)
        self.assertEqual(oh.UDS_ROUTINE_COMMIT, 0xFF04)

    def test_boot_request_sequence_and_session_retry(self):
        """默认路径: 10 03 → 2E F190 01 → 11 01 → 轮询 10 03 → 再刷写"""
        image = oh.parse_image(oh.make_sample_image(1000))
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, ecu = run_flow_with_log(image, tmp, verify=True)
        self.assertTrue(result.boot_requested)
        self.assertTrue(result.boot_reset_done)
        self.assertEqual(result.session_attempts, 2)            # 模拟器吞掉首次 10 03
        self.assertEqual(ecu.boot_req_seen, True)
        self.assertEqual(requests[0], bytes([0x10, 0x03]))
        self.assertEqual(requests[1], bytes([0x2E, 0xF1, 0x90, 0x01]))
        self.assertEqual(requests[2], bytes([0x11, 0x01]))
        self.assertEqual(requests[3], bytes([0x10, 0x03]))      # 落在 BL 启动窗口内（无应答）
        self.assertEqual(requests[4], bytes([0x10, 0x03]))      # 重发并拿到 50 03
        # B2 回归：不再单独发 31 01 FF00（擦除由 0x34 隐式完成）
        self.assertFalse(any(r[:4] == bytes([0x31, 0x01, 0xFF, 0x00]) for r in requests))
        self.assertEqual(requests[5][:1], bytes([0x34]))
        # 序列中 11 01 出现两次（进 BL + 最后复位）
        self.assertEqual(sum(1 for r in requests if r == bytes([0x11, 0x01])), 2)
        self.assertTrue(result.verified)

    def test_no_boot_request_hot_upgrade(self):
        """--no-boot-request: 不发 2E/不复位进 BL，直接 10 03 → 刷写"""
        image = oh.parse_image(oh.make_sample_image(1000))
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, ecu = run_flow_with_log(image, tmp, verify=True,
                                                      boot_request=False)
        self.assertFalse(result.boot_requested)
        self.assertFalse(result.boot_reset_done)
        self.assertEqual(result.session_attempts, 0)
        self.assertFalse(ecu.boot_req_seen)
        self.assertNotIn(bytes([0x2E, 0xF1, 0x90, 0x01]), requests)
        self.assertEqual(requests[0], bytes([0x10, 0x03]))
        # B2 回归：不再单独发 31 01 FF00
        self.assertFalse(any(r[:4] == bytes([0x31, 0x01, 0xFF, 0x00]) for r in requests))
        self.assertEqual(requests[1][:1], bytes([0x34]))
        self.assertEqual(requests[-1], bytes([0x11, 0x01]))     # 只有最后那次复位

    def test_session_retry_success_counts_attempts(self):
        image = oh.parse_image(oh.make_sample_image(300))
        ecu = oh.SimulatedEcu()
        ecu.bl_ready_in = 3                                     # BL 需 3 次轮询后才应答
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        flasher = oh.OtaFlasher(uds, image, session_retry=3.0, log=silent)
        self.assertEqual(flasher.reestablish_session("单测"), 4)

    def test_session_retry_timeout_message(self):
        """BL 窗口内没建立起会话 -> 明确提示跳转 APP / 断电重试 / --no-boot-request"""
        image = oh.parse_image(oh.make_sample_image(300))
        ecu = oh.SimulatedEcu()
        ecu.bl_ready_in = 10 ** 6
        _, _, tunnel, uds = build_stack(ecu=ecu)
        tunnel.tunnel_on()
        flasher = oh.OtaFlasher(uds, image, session_retry=0.35, log=silent)
        with self.assertRaises(oh.OtaError) as ctx:
            flasher.reestablish_session("复位进 BL 后")
        message = str(ctx.exception)
        self.assertIn("跳转 APP", message)
        self.assertIn("--no-boot-request", message)
        self.assertIn("10 03", message)

    def test_boot_request_requires_extended_session(self):
        """2E F190 在默认会话下必须被 NRC 0x22 拒绝（否则说明时序错了）"""
        ecu = oh.SimulatedEcu()
        nrc = ecu._uds_handle(bytes([0x2E, 0xF1, 0x90, 0x01]))
        self.assertEqual(nrc, bytes([0x7F, 0x2E, 0x22]))
        ecu._uds_handle(bytes([0x10, 0x03]))
        self.assertEqual(ecu._uds_handle(bytes([0x2E, 0xF1, 0x90, 0x01])),
                         bytes([0x6E, 0xF1, 0x90]))
        self.assertTrue(ecu.boot_req_seen)

    def test_commit_flow_sets_idle_state(self):
        """--commit: 复位后先 10 03 再 FF04，TRIAL -> IDLE"""
        image = oh.parse_image(oh.make_sample_image(1000))
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, ecu = run_flow_with_log(image, tmp, verify=True, commit=True)
        self.assertTrue(result.committed)
        self.assertEqual(result.commit_status, 0x00)
        self.assertEqual(ecu.commit_count, 1)
        self.assertEqual(ecu.ota_state, 0x00)                   # IDLE_A（试运行已落定）
        commit = bytes([0x31, 0x01, 0xFF, 0x04])
        self.assertIn(commit, requests)
        # FF04 之前必须有一条 10 03（默认会话下 FF04 会被 NRC 0x22 拒绝）
        commit_idx = requests.index(commit)
        self.assertIn(bytes([0x10, 0x03]), requests[:commit_idx])
        self.assertEqual(requests[-1], commit)                  # 落定是最后一条

    def test_commit_off_by_default(self):
        image = oh.parse_image(oh.make_sample_image(300))
        with tempfile.TemporaryDirectory() as tmp:
            result, requests, ecu = run_flow_with_log(image, tmp)
        self.assertFalse(result.committed)
        self.assertEqual(ecu.commit_count, 0)
        self.assertNotIn(bytes([0x31, 0x01, 0xFF, 0x04]), requests)

    def test_ff04_rejected_in_default_session(self):
        ecu = oh.SimulatedEcu()
        self.assertEqual(ecu._uds_handle(bytes([0x31, 0x01, 0xFF, 0x04])),
                         bytes([0x7F, 0x31, 0x22]))             # conditionsNotCorrect
        ecu._uds_handle(bytes([0x10, 0x03]))
        self.assertEqual(ecu._uds_handle(bytes([0x31, 0x01, 0xFF, 0x04])),
                         bytes([0x71, 0x01, 0xFF, 0x04, 0x00]))

    def test_commit_status_failure_raises(self):
        class CommitFails(oh.SimulatedEcu):
            def _uds_handle(self, req):
                if req[:4] == bytes([0x31, 0x01, 0xFF, 0x04]):
                    return bytes([0x71, 0x01, 0xFF, 0x04, 0x01])   # status=01 失败
                return super()._uds_handle(req)

        image = oh.parse_image(oh.make_sample_image(300))
        ecu = CommitFails()
        with self.assertRaises(oh.OtaError) as ctx:
            with tempfile.TemporaryDirectory() as tmp:
                run_flow_with_log(image, tmp, ecu=ecu, commit=True)
        self.assertIn("FF04", str(ctx.exception))
        self.assertIn("0x01", str(ctx.exception))

    def test_boot_wait_option_accepted(self):
        image = oh.parse_image(oh.make_sample_image(300))
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = run_flow_with_log(image, tmp, boot_wait=0.05)
        self.assertTrue(result.boot_requested)


# ---------------------------------------------------------------- 7c. 真机物理约束

class TestRealWorldGuards(unittest.TestCase):
    """(a) 隧道模式 RX 环形缓冲提示  (b) 擦除耗时估算 + --timeout 预检"""

    # ---- (a) ----
    def test_frame_count_helper(self):
        self.assertEqual(oh.NODE_RX_RING_DEPTH, 16)             # can.c 的 CAN_RX_RING_LEN
        self.assertEqual(oh.block_frame_count(1016), 146)       # 单块 1016B -> 146 帧
        self.assertEqual(oh.block_frame_count(500), 72)
        self.assertEqual(oh.block_frame_count(1), 1)            # SF

    def test_rx_ring_hint_threshold(self):
        """帧数 == 环形深度(16) 不提示；== 17 提示（边界）"""
        bs16 = next(b for b in range(8, 200) if oh.block_frame_count(b) == 16)
        bs17 = next(b for b in range(8, 200) if oh.block_frame_count(b) == 17)
        self.assertIsNone(oh.rx_ring_hint(bs16))
        hint = oh.rx_ring_hint(bs17)
        self.assertIsNotNone(hint)
        self.assertIn("RX 环形缓冲", hint)
        self.assertIn("22 F002", hint)
        self.assertIn("rxOverruns", hint)
        self.assertIn(f"--block-size {oh.TUNNEL_SUGGESTED_BLOCK_SIZE}", hint)
        # 默认块 1016 -> 146 帧，必然提示
        self.assertIn("146 个 CAN 连续帧", oh.rx_ring_hint(oh.ISOTP_DEFAULT_BLOCK_SIZE))

    def test_dry_run_never_prints_rx_ring_hint(self):
        """--dry-run 保持离线序列输出干净，不打印隧道提示"""
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli(["--dry-run", "--verify", img_path])
        self.assertEqual(code, 0, out)
        self.assertNotIn("RX 环形缓冲", out)
        self.assertNotIn("rxOverruns", out)

    def test_real_mode_prints_hint_and_completes(self):
        """真实串口模式（用假串口桥接模拟器）打印提示并完整刷完"""
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            with mock.patch.object(oh, "SerialLineTransport", FakeSerialTransport):
                code, out = run_cli(["--port", "COM_FAKE", "--verify", "--quiet", img_path])
        self.assertEqual(code, 0, out)
        self.assertIn("RX 环形缓冲", out)
        # 帧数 <=16 的块大小不提示（同一路径）
        bs16 = next(b for b in range(8, 200) if oh.block_frame_count(b) == 16)
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            with mock.patch.object(oh, "SerialLineTransport", FakeSerialTransport):
                code, out = run_cli(["--port", "COM_FAKE", "--verify", "--quiet",
                                     "--block-size", str(bs16), img_path])
        self.assertEqual(code, 0, out)
        self.assertNotIn("RX 环形缓冲", out)

    # ---- (b) ----
    def test_parse_session_response_p2star(self):
        """50 03 00 32 01 F4 -> P2=50ms, P2*=0x01F4*10=5000ms"""
        self.assertEqual(oh.parse_session_response(bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])),
                         (0x03, 50, 5000))
        self.assertEqual(oh.parse_session_response(bytes([0x50, 0x03])), (0x03, None, None))
        with self.assertRaises(oh.OtaError):
            oh.parse_session_response(bytes([0x51, 0x01]))

    def test_erase_estimate(self):
        self.assertEqual(oh.FLASH_PAGE_SIZE, 2048)
        self.assertEqual(oh.erase_estimate_ms(1), 25)
        self.assertEqual(oh.erase_estimate_ms(2048), 25)
        self.assertEqual(oh.erase_estimate_ms(2049), 50)
        self.assertEqual(oh.erase_estimate_ms(oh.OTA_IMG_MAX_PAYLOAD), 104 * 25)   # 2.6s

    def test_erase_estimate_printed_and_budget_ok(self):
        """timeout/P2* 都足够时：打印估算并通过"""
        image = oh.parse_image(oh.make_sample_image(4096))
        logs = []
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, timeout=6.0, log=logs.append).run()
        self.assertEqual(result.erase_est_ms, 50)               # 2 页 × 25ms
        self.assertEqual(result.p2star_ms, 5000)                # 来自模拟器 50 03 应答
        self.assertTrue(any("预计擦除" in line for line in logs), logs)

    def test_erase_budget_rejects_small_timeout(self):
        """timeout < 估算擦除时间 -> 动手前拒绝（超时是必然事件），且未发起擦除"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        image = oh.parse_image(oh.make_sample_image(oh.OTA_IMG_MAX_PAYLOAD))
        ecu, _, tunnel, uds = build_stack(timeout=5.0)
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, timeout=2.0, log=silent).run()
        self.assertIn("--timeout", str(ctx.exception))
        self.assertIn("2.6s", str(ctx.exception))               # 估算值给出可操作指引
        self.assertIn("4.0", str(ctx.exception))                # 最小安全建议值
        self.assertIn("6.0", str(ctx.exception))                # 文档建议的全槽值
        self.assertEqual(ecu.erase_count, 0)                    # 未发 FF00
        self.assertIsNone(ecu.dl_region)                        # 也未发 0x34

    def test_erase_budget_timeout_exactly_enough(self):
        """边界：timeout*1000 == 估算值 -> 通过（不小于即合法）"""
        image = oh.parse_image(oh.make_sample_image(oh.OTA_IMG_MAX_PAYLOAD))
        ecu, _, tunnel, uds = build_stack(timeout=5.0)
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, timeout=2.8, do_reset=False, log=silent).run()
        self.assertEqual(result.erase_est_ms, 2600)
        # B2/M9 回归：上位机不再单独发 FF00，擦除只由 0x34 隐式触发一次
        # （旧实现会擦两次，第二次纯浪费且吃掉超时余量）
        self.assertEqual(ecu.erase_count, 1)

    def test_erase_budget_rejects_when_over_p2star(self):
        """设备声明的 P2* 是权威上限：估算 > P2* 时拒绝（即使 --timeout 很宽）"""

        class ShortP2Star(oh.SimulatedEcu):
            def _uds_handle(self, req):
                if req[:2] == bytes([0x10, 0x03]):
                    self.session = oh.UDS_SESSION_EXTENDED      # 父类逻辑要自己补
                    # P2=50ms, P2*=0x0064*10 = 1000ms
                    return bytes([0x50, 0x03, 0x00, 0x32, 0x00, 0x64])
                return super()._uds_handle(req)

        image = oh.parse_image(oh.make_sample_image(oh.OTA_IMG_MAX_PAYLOAD))
        ecu = ShortP2Star()
        _, _, tunnel, uds = build_stack(ecu=ecu, timeout=5.0)
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, timeout=10.0, log=silent).run()
        self.assertIn("P2*", str(ctx.exception))
        self.assertEqual(ecu.erase_count, 0)

    def test_erase_budget_guard_also_covers_resume(self):
        """续传区段也要预检（固件从 addr 起擦页）"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        image = oh.parse_image(oh.make_sample_image(bs * 6))
        logs = []
        ecu, _, tunnel, uds = build_stack(timeout=5.0)
        tunnel.tunnel_on()
        result = oh.OtaFlasher(uds, image, resume=5, timeout=6.0,
                               do_reset=False, log=logs.append).run()
        self.assertEqual(result.erase_est_ms, 25)               # 剩余 2042B -> 1 页
        self.assertTrue(any("擦除预算" in line for line in logs), logs)


# ---------------------------------------------------------------- 8. CLI

class TestCli(unittest.TestCase):
    def test_dry_run_output_self_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            raw = write_image(img_path, size=4096)
            image = oh.parse_image(raw)
            code, out = run_cli(["--dry-run", "--verify", img_path])
            self.assertEqual(code, 0, out)
            bs = oh.ISOTP_DEFAULT_BLOCK_SIZE                    # 1016
            total_blocks = oh.block_count(image.app_size, bs)
            # 摘要数字自洽
            self.assertIn(f"块数={total_blocks}", out)
            self.assertIn("传输字节=4096", out)
            self.assertIn("与镜像一致: 是", out)
            # 完整报文序列: 重组 7E0 流 -> 净荷拼接必须等于镜像净荷
            requests = oh.isotp_reassemble(extract_rxtx_frames(out, oh.ISOTP_REQ_ID),
                                           can_id=oh.ISOTP_REQ_ID)
            blocks = [m for m in requests if m[0] == 0x36]
            self.assertEqual(len(blocks), total_blocks)
            payload = b"".join(m[2:] for m in blocks)
            self.assertEqual(payload, image.app_data)
            self.assertEqual(len(payload), image.app_size)      # 传输流不含 32 字节头
            # 每条 0x36 报文 <=1024 字节且长度与块大小自洽（末块 <=1016）
            for i, msg in enumerate(blocks):
                self.assertLessEqual(len(msg), oh.ISOTP_MAX_MSG)
                self.assertLessEqual(len(msg) - 2, oh.ISOTP_MAX_BLOCK_SIZE)
                expect = min(bs, image.app_size - i * bs)
                self.assertEqual(len(msg) - 2, expect, f"block {i + 1}")
                self.assertEqual(msg[1], oh.block_seq(i + 1))
            # 请求序列完整（--boot-request 默认开启）:
            # 10 03 / 2E F190 01 / 11 01 / 10 03(轮询, 首次无应答) / 10 03 / 34
            # / 36*33 / 37(5B) / 31 FF01 / 31 FF02 / 11 01
            # B2 回归：不再有 31 01 FF 00（擦除由 0x34 隐式完成）
            self.assertEqual(requests[0], bytes([0x10, 0x03]))
            self.assertEqual(requests[1], bytes([0x2E, 0xF1, 0x90, 0x01]))
            self.assertEqual(requests[2], bytes([0x11, 0x01]))
            self.assertEqual(requests[3], bytes([0x10, 0x03]))
            self.assertEqual(requests[4], bytes([0x10, 0x03]))   # 复位后轮询重发
            self.assertFalse(any(r[:3] == bytes([0x31, 0x01, 0xFF, 0x00][:3]) and
                                 r[:2] == bytes([0x31, 0x01]) and r[2:4] == b"\xFF\x00"
                                 for r in requests),
                             "不应再发送 31 01 FF00（擦除由 0x34 隐式完成）")
            self.assertEqual(requests[5][:3], bytes([0x34, 0x00, 0x44]))
            self.assertEqual(struct.unpack_from(">I", requests[5], 3)[0], oh.OTA_SLOT_A_APP_ADDR)
            self.assertEqual(struct.unpack_from(">I", requests[5], 7)[0], image.app_size)
            xfer_exit = requests[-4]                            # ... 37 / FF01 / FF02 / 11 01
            self.assertEqual(xfer_exit[:1], bytes([0x37]))
            self.assertEqual(len(xfer_exit), 5)                 # 0x37 必须带 payloadCrc
            self.assertEqual(struct.unpack_from(">I", xfer_exit, 1)[0], image.app_crc32)
            self.assertEqual(requests[-3], bytes([0x31, 0x01, 0xFF, 0x01]))
            self.assertEqual(requests[-2], bytes([0x31, 0x01, 0xFF, 0x02]))
            self.assertEqual(requests[-1], bytes([0x11, 0x01]))
            # 36 报文应全部位于 34 与 37 之间
            self.assertEqual(requests[6],
                             bytes([0x36, oh.block_seq(1)]) + image.app_data[:bs])
            # 块序号必须 1..255 循环且 0 非法（契约）
            seqs = [r[1] for r in requests if r[:1] == bytes([0x36])]
            self.assertTrue(all(1 <= x <= 255 for x in seqs), "块序号越界")
            # 每个 CAN 帧 <= 8 字节
            for _, frame in extract_rxtx_frames(out, oh.ISOTP_REQ_ID):
                self.assertLessEqual(len(frame), 8)

    def test_dry_run_quiet_and_block_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli(["--dry-run", "--quiet", "--block-size", "64", img_path])
        self.assertEqual(code, 0, out)
        self.assertNotIn("TX> ", out)
        self.assertIn("块大小=64", out)
        self.assertIn("块数=5", out)

    def test_dry_run_with_precheck_bitrate_slot_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli(["--dry-run", "--quiet", "--precheck", "--bitrate", "500",
                                 "--slot", "B", "--no-reset", img_path])
        self.assertEqual(code, 0, out)
        self.assertIn("0x08040880", out)
        self.assertIn("precheck", out)

    def test_invalid_block_size_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            # 1017 在 ISO-TP 层合法但超契约块上限 1016，必须拒绝
            code, out = run_cli(["--dry-run", "--block-size",
                                 str(oh.ISOTP_MAX_BLOCK_SIZE + 1), img_path])
        self.assertEqual(code, 1)
        self.assertIn("block-size", out)

    def test_missing_and_bad_image(self):
        code, out = run_cli(["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("缺少镜像", out)
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.bin")
            with open(bad, "wb") as fh:
                fh.write(b"XXXX" + bytes(oh.OTA_HEADER_SIZE - 4))   # 长度够，但魔数错
            code, out = run_cli(["--dry-run", bad])
            self.assertEqual(code, 1)
            self.assertIn("magic", out)

    def test_info_make_sample_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "sample.bin")
            code, out = run_cli(["--make-sample", sample, "--sample-size", "512",
                                 "--img-version", "2.5"])
            self.assertEqual(code, 0, out)
            img = oh.parse_image(open(sample, "rb").read())
            self.assertEqual(img.version, (2, 5))
            self.assertEqual(img.app_size, 512)
            code, out = run_cli(["--info", sample])
            self.assertEqual(code, 0, out)
            self.assertIn("appSize  : 512", out)
            # --pack: 裸固件 -> .ota，payloadCrc = CRC32(净荷)
            payload = os.path.join(tmp, "app.bin")
            with open(payload, "wb") as fh:
                fh.write(oh.make_sample_app(2048))
            out_path = os.path.join(tmp, "app.ota")
            code, out = run_cli(["--pack", payload, "--pack-out", out_path])
            self.assertEqual(code, 0, out)
            packed = oh.load_image(out_path)
            self.assertEqual(packed.app_data, oh.make_sample_app(2048))
            self.assertIn(f"0x{packed.app_crc32:08X}", out)

    def test_list_serial_ports(self):
        code, out = run_cli(["--list"])
        self.assertIn(code, (0, 1, 2))                          # 有/无串口都算正常
        self.assertNotIn("Traceback", out)

    def test_dry_run_resume_only_covers_tail(self):
        """全新模拟器上 --resume 5：只写尾部区段（前段为空属预期，工具会告警）"""
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=bs * 6)                   # 6 块，从第 5 块续传
            code, out = run_cli(["--dry-run", "--resume", "5", "--quiet", img_path])
        self.assertEqual(code, 0, out)
        self.assertIn("已发块=2", out)                            # 第 5、6 块
        self.assertIn(f"传输字节={2 * bs}", out)
        self.assertIn("续传假定设备此前已写入前段数据", out)

    def test_cli_boot_request_default_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli(["--dry-run", "--verify", img_path])
            self.assertEqual(code, 0, out)
            self.assertIn("--boot-request", out)
            self.assertIn("2E F190 01", out)
            self.assertIn("会话重建成功", out)
            code, out = run_cli(["--dry-run", "--verify", "--no-boot-request", img_path])
            self.assertEqual(code, 0, out)
            self.assertIn("--no-boot-request: 热升级模式", out)
            self.assertNotIn("2E F190 01", out)

    def test_cli_commit_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli(["--dry-run", "--verify", "--commit", img_path])
        self.assertEqual(code, 0, out)
        self.assertIn("31 01 FF04", out)
        self.assertIn("试运行已落定", out)

    def test_help_documents_boot_path_and_window(self):
        parser = oh.build_parser()
        # argparse 会按终端宽度折行，先归一化空白再断言整句
        text = " ".join(parser.format_help().split())
        self.assertIn("--boot-request", text)
        self.assertIn("--no-boot-request", text)
        self.assertIn("--commit", text)
        self.assertIn("BL 启动窗口", text)
        self.assertIn("热升级", text)
        self.assertIn("--session-retry", text)
        self.assertIn("NRC 0x22", text)

    def test_real_mode_requires_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "img.bin")
            write_image(img_path, size=300)
            code, out = run_cli([img_path])
        self.assertEqual(code, 1)
        self.assertIn("--port", out)


# ---------------------------------------------------------------- 9. --replay / 重放

class TestReplay(unittest.TestCase):
    def _make_log(self, tmp, size=1000, extra_args=()):
        img_path = os.path.join(tmp, "img.bin")
        log_path = os.path.join(tmp, "run.log")
        write_image(img_path, size=size)
        code, out = run_cli(["--dry-run", "--verify", "--quiet", "--log", log_path,
                             *extra_args, img_path])
        self.assertEqual(code, 0, out)
        return img_path, log_path

    def test_replay_report_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            size = oh.ISOTP_DEFAULT_BLOCK_SIZE * 3              # 3 块（1016 字节/块）
            img_path, log_path = self._make_log(tmp, size=size)
            image = oh.load_image(img_path)
            rep = oh.replay_report(log_path, image=image)
            self.assertTrue(rep.ok(), rep.format())
            self.assertEqual(rep.block_count, 3)
            self.assertEqual(rep.block_bytes, size)
            self.assertEqual(rep.download_size, size)
            self.assertEqual(rep.crc_log, image.app_crc32)
            self.assertEqual(rep.crc_image, image.app_crc32)
            self.assertEqual(rep.crc_exit, image.app_crc32)     # 0x37 带 payloadCrc
            # 请求: 10 03 / 2E F190 01 / 11 01 / 10 03(轮询无应答) / 10 03 / 34
            #       / 36*3 / 37 / FF01 / FF02 / 11 01 = 13 条请求，其中 1 条被重发（无应答）
            # B2 回归：FF00 已从流程中移除，故请求数由 14 降为 13
            self.assertEqual(len(rep.requests), 13)
            self.assertEqual(len(rep.responses), 12)
            self.assertEqual(rep.retries, 1)
            code, out = run_cli(["--replay", log_path, img_path])
            self.assertEqual(code, 0, out)
            self.assertIn("结果: 通过", out)

    def test_replay_detects_truncated_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path, log_path = self._make_log(tmp, size=500)
            with open(log_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            # 丢掉最后 4 条设备行（应答缺失）
            trimmed = lines[:-4]
            bad_log = os.path.join(tmp, "bad.log")
            with open(bad_log, "w", encoding="utf-8") as fh:
                fh.writelines(trimmed)
            rep = oh.replay_report(bad_log, image=oh.load_image(img_path))
            self.assertFalse(rep.ok())
            self.assertTrue(any("应答" in p or "没有" in p for p in rep.problems), rep.problems)
            code, out = run_cli(["--replay", bad_log, img_path])
            self.assertEqual(code, 1)
            self.assertIn("结果: 失败", out)

    def test_replay_detects_crc_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path, log_path = self._make_log(tmp, size=500)
            image = oh.load_image(img_path)
            with open(log_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            # 用同一个 0x37 请求但换掉 payloadCrc，重建该行（确定性篡改）
            bad_crc = image.app_crc32 ^ 0xFFFFFFFF
            bad_line = "<< " + oh.tx_cmd_line(0x7E0, bytes([0x05, 0x37])
                                              + struct.pack(">I", bad_crc))
            lines = text.splitlines()
            idx = next(i for i, line in enumerate(lines) if line.startswith("<< TX 7E0 8 05 37"))
            lines[idx] = bad_line
            bad_log = os.path.join(tmp, "crc.log")
            with open(bad_log, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            rep = oh.replay_report(bad_log, image=image)
            self.assertFalse(rep.ok())
            self.assertTrue(any("payloadCrc" in p for p in rep.problems), rep.problems)

    def test_replay_exec_is_byte_exact(self):
        """用 ReplayLineTransport 重跑整条流程，主机报文必须与日志逐字节一致"""
        with tempfile.TemporaryDirectory() as tmp:
            img_path, log_path = self._make_log(tmp, size=1000)
            image = oh.load_image(img_path)
            transport = oh.ReplayLineTransport(log_path)
            tunnel = oh.ConsoleTunnel(transport, timeout=1.0)
            uds = oh.UdsClient(oh.IsoTpClient(tunnel, timeout=1.0, sleep_fn=lambda _s: None),
                               timeout=1.0)
            tunnel.tunnel_on()
            result = oh.OtaFlasher(uds, image, verify=True, log=silent).run()
            tunnel.tunnel_off()
            self.assertTrue(result.verified)
            self.assertEqual(transport.skipped, 0)
            self.assertEqual(transport.dropped_rx, 0)
            host_left, dev_left = transport.leftovers()
            self.assertEqual((host_left, dev_left), (0, 0))

    def test_replay_exec_detects_tampered_host_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path, log_path = self._make_log(tmp, size=1000)
            with open(log_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            first_tx = text.find("<< TX 7E0")
            line_end = text.find("\n", first_tx)
            orig = text[first_tx:line_end]
            bad = orig.replace(orig.split()[-1], "FF", 1)       # 篡改最后一个数据字节
            self.assertNotEqual(orig, bad)
            tampered = os.path.join(tmp, "tampered.log")
            with open(tampered, "w", encoding="utf-8") as fh:
                fh.write(text[:first_tx] + bad + text[line_end:])
            transport = oh.ReplayLineTransport(tampered)
            tunnel = oh.ConsoleTunnel(transport, timeout=1.0)
            uds = oh.UdsClient(oh.IsoTpClient(tunnel, timeout=1.0, sleep_fn=lambda _s: None),
                               timeout=1.0)
            tunnel.tunnel_on()
            with self.assertRaises(oh.ReplayMismatch):
                oh.OtaFlasher(uds, oh.load_image(img_path), log=silent).run()

    def test_replay_report_without_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, log_path = self._make_log(tmp, size=300)
            rep = oh.replay_report(log_path)
            self.assertTrue(rep.ok(), rep.format())
            self.assertIsNone(rep.crc_image)
            self.assertEqual(rep.crc_exit, None)                # 无镜像时不做比对

    def test_replay_missing_log(self):
        with self.assertRaises(oh.ReplayError):
            oh.replay_report(os.path.join(tempfile.gettempdir(), "no_such_log_xyz.log"))


# ---------------------------------------------------------------- 10. 采样固件/边界

class TestMisc(unittest.TestCase):
    def test_simulator_block_size_zero_not_allowed(self):
        with self.assertRaises(ValueError):
            oh.block_count(100, 0)

    def test_isotp_max_block_matches_contract(self):
        """与 ota_common.h 冻结的吞吐优化常量一致：1024 报文上限 / 1016 块净荷上限"""
        self.assertEqual(oh.ISOTP_MAX_MSG, 1024)                # OTA_ISOTP_MAX_MSG
        self.assertEqual(oh.ISOTP_MAX_BLOCK_SIZE, 1016)         # UDS_DL_MAX_BLOCK_PAYLOAD
        self.assertEqual(oh.ISOTP_DEFAULT_BLOCK_SIZE, 1016)     # UDS_MAXLEN_FIELD
        # 块大小 = (MAX - 3) 向下对齐到 8 的倍数（契约 ota_common.h 同一推导式）
        self.assertEqual(oh.ISOTP_MAX_BLOCK_SIZE, (oh.ISOTP_MAX_MSG - 3) & ~7)
        # 血线 5：块大小必须是 8 的倍数，否则断点续传起点大面积落在非对齐地址被拒
        self.assertEqual(oh.ISOTP_MAX_BLOCK_SIZE % 8, 0)
        # 块净荷 + 2(SID+seq) 不得超过 ISO-TP 报文上限
        self.assertLessEqual(oh.ISOTP_MAX_BLOCK_SIZE + 2, oh.ISOTP_MAX_MSG)
        # 0x36 报文 = 2 + 1016 = 1018 <= 1024
        self.assertEqual(len(oh.uds_transfer_data(oh.block_seq(1),
                                                  bytes(oh.ISOTP_MAX_BLOCK_SIZE))), 1018)
        self.assertLessEqual(len(oh.uds_transfer_data(oh.block_seq(1),
                                                      bytes(oh.ISOTP_MAX_BLOCK_SIZE))),
                             oh.ISOTP_MAX_MSG)

    def test_resume_offsets_are_all_8byte_aligned(self):
        """B4 回归：块大小 1016 是 8 的倍数 -> **任意**续传起点偏移都 8 对齐。

        旧实现块大小取 1021（mod 8 = 5），--resume 18~22 的偏移全部非对齐，
        会被设备 BL_DiagRangeOk 回 NRC 0x31，续传形同虚设。
        """
        bs = oh.ISOTP_DEFAULT_BLOCK_SIZE
        for start in (2, 17, 18, 19, 20, 21, 22, 100, 255, 256):
            offset = (start - 1) * bs
            self.assertEqual(offset % 8, 0,
                             f"续传起点 {start} 的偏移 {offset} 未 8 对齐，会被设备拒")

    def test_block_payload_limit_vs_isotp_message_limit(self):
        """两个边界必须分清：块净荷上限 1016（契约）vs ISO-TP 报文上限 1024"""
        bs = oh.ISOTP_MAX_BLOCK_SIZE                            # 1016
        # 净荷 1016 -> 0x36 报文 1018 合法
        self.assertEqual(len(oh.uds_transfer_data(1, bytes(bs))), bs + 2)
        # 净荷 1017 -> 报文 1019：ISO-TP 层仍合法，但超契约块上限，工具必须拒绝
        self.assertLessEqual(bs + 3, oh.ISOTP_MAX_MSG)
        self.assertEqual(len(oh.uds_transfer_data(1, bytes(bs + 1))), bs + 3)
        image = oh.parse_image(oh.make_sample_image(bs * 2))
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(None, image, block_size=bs + 1, log=silent)
        self.assertIn("block-size", str(ctx.exception))
        # 净荷大到使整条报文超过 ISO-TP 上限：isotp 层报错（绝不截断）
        with self.assertRaises(oh.IsoTpTooLong):
            oh.uds_transfer_data(1, bytes(oh.ISOTP_MAX_MSG))
        # 上限块在真实流程里可用：满槽 212928B 恰好 210 块
        self.assertEqual(oh.block_count(oh.OTA_IMG_MAX_PAYLOAD, bs), 210)
        self.assertLess(210, oh.BLOCK_SEQ_MOD + 1)              # 210 块不触发 seq 回绕

    def test_slot_addresses(self):
        """与 ota/common/ota_common.h 冻结值逐项一致"""
        self.assertEqual(oh.OTA_SLOT_A_BASE, 0x0800C000)        # 槽基址(含槽首 0x80 保留区)
        self.assertEqual(oh.OTA_SLOT_B_BASE, 0x08040800)
        self.assertEqual(oh.OTA_SLOT_A_APP_ADDR, 0x0800C080)    # 0x34 目标 = 槽基址 + 0x80 保留区
        self.assertEqual(oh.OTA_SLOT_B_APP_ADDR, 0x08040880)
        self.assertEqual(oh.OTA_DEFAULT_LOAD_ADDR, oh.OTA_SLOT_A_APP_ADDR)
        self.assertEqual(oh.OTA_SLOT_SIZE, 0x34000)             # 208KB
        self.assertEqual(oh.OTA_IMG_MAX_PAYLOAD, 0x34000 - oh.OTA_HEADER_SIZE)   # 保留区 0x80
        # BL 独占 0x08000800~0x08007FFF；Slot A 恰好抵 Bank1 上界；Slot B 对齐 Bank2
        self.assertEqual(oh.OTA_SLOT_A_BASE + oh.OTA_SLOT_SIZE, 0x08040000)
        self.assertEqual(oh.OTA_SLOT_B_BASE, 0x08040000 + 0x800)
        self.assertEqual(oh.OTA_SLOT_ADDRS["A"], oh.OTA_SLOT_A_APP_ADDR)
        self.assertEqual(oh.OTA_SLOT_BASES["B"], oh.OTA_SLOT_B_BASE)
        # 预检 DID 与 ota_common.h 的 F18x 定义一致
        self.assertEqual(oh.UDS_PRECHECK_DIDS, (0xF180, 0xF181, 0xF182, 0xF183, 0xF184))

    def test_slot_capacity_guard(self):
        """净荷超出单槽容量必须报错（越界擦除会擦掉 Bootloader）"""
        image = oh.OtaImage(app_data=bytes(oh.OTA_IMG_MAX_PAYLOAD + 1),
                            load_addr=oh.OTA_SLOT_A_APP_ADDR)
        ecu, _, tunnel, uds = build_stack()
        tunnel.tunnel_on()
        with self.assertRaises(oh.OtaError) as ctx:
            oh.OtaFlasher(uds, image, log=silent).run()
        self.assertIn("超出 Slot", str(ctx.exception))
        # 恰好装满槽是允许的（此处只验证构造/容量计算，不发真实数据）
        self.assertEqual(oh.OTA_IMG_MAX_PAYLOAD, 208 * 1024 - oh.OTA_HEADER_SIZE)

    def test_cli_parser_defaults(self):
        args = oh.build_parser().parse_args(["img.bin"])
        self.assertEqual(args.block_size, oh.ISOTP_DEFAULT_BLOCK_SIZE)   # 1016（吞吐默认生效）
        self.assertEqual(args.block_size, 1016)
        self.assertEqual(args.baudrate, 115200)
        self.assertEqual(args.precheck_did, None)
        self.assertFalse(args.verify)
        self.assertIsNone(args.resume)
        self.assertTrue(args.boot_request)                      # 默认进 BL（安全路径）
        self.assertFalse(args.commit)                           # 默认由 APP 自检确认
        self.assertEqual(args.boot_wait, 0.0)
        self.assertEqual(args.session_retry, oh.SESSION_RETRY_TOTAL)
        args = oh.build_parser().parse_args(["--no-boot-request", "--commit", "img.bin"])
        self.assertFalse(args.boot_request)
        self.assertTrue(args.commit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
