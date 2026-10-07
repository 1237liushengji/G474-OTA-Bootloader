#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STM32G474 双机CAN 上位机控制台（迷你CAN分析仪）
================================================
通过板上USB转串口(115200)连接CAN节点，功能：
  1. 透传命令到节点控制台（help/version/stats/bitrate/sniff/send）
  2. 解析节点输出的报文行 "RX <ms> <ID> <DLC> <data...>"，彩色高亮+计数
  3. 自动CSV记录（can_log_时间戳.csv）
  4. 快捷发帧：/send 321 11 22 33

依赖：pip install pyserial
用法：
  python can_console.py               # 交互选择串口
  python can_console.py -p COM5       # 直接指定串口
  python can_console.py --list        # 仅列出串口
节点命令在板上输入无效时注意：两块板的波特率/过滤器状态需匹配。
"""
import argparse
import csv
import datetime
import os
import re
import sys

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("缺少pyserial，请先执行: pip install pyserial")
    sys.exit(1)

RX_PATTERN = re.compile(r"^RX (\d+) ([0-9A-Fa-f]{3}) (\d)((?: [0-9A-Fa-f]{2})*)$")
BAUDRATE = 115200

# ANSI颜色（Windows 10+终端支持；不支持时自动降级）
class Clr:
    ENABLED = sys.stdout.isatty()
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


def choose_port(preferred=None):
    ports = list(list_ports.comports())
    if not ports:
        print("未发现串口，请检查USB连接/驱动。")
        sys.exit(1)
    if preferred:
        for p in ports:
            if p.device.upper() == preferred.upper():
                return p.device
        print(f"找不到 {preferred}，可用串口：")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  {p.description}")
    while True:
        try:
            idx = int(input("选择串口序号: "))
            return ports[idx].device
        except (ValueError, IndexError):
            print("输入无效，重试。")


class CanLogger:
    """CSV报文记录"""
    def __init__(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(os.getcwd(), f"can_log_{ts}.csv")
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(["host_time", "node_ms", "id", "dlc", "data"])
        self.count = 0

    def log(self, node_ms, can_id, dlc, data):
        host = datetime.datetime.now().isoformat(timespec="milliseconds")
        self._w.writerow([host, node_ms, can_id, dlc, " ".join(data)])
        self.count += 1

    def close(self):
        self._fh.close()


def handle_line(line, logger, stats):
    m = RX_PATTERN.match(line.strip())
    if not m:
        print(Clr.cyan(line))                        # 普通控制台输出
        return
    ms, can_id, dlc, datastr = m.groups()
    data = datastr.split()
    stats[can_id] = stats.get(can_id, 0) + 1
    logger.log(ms, can_id, dlc, data)
    print(Clr.green(f"[{ms:>8}ms] ID:{can_id} DLC:{dlc} "
                    + " ".join(f"{b:>2}" for b in data))
          + Clr.yellow(f"   (ID {can_id} 累计{stats[can_id]}帧)"))


def main():
    ap = argparse.ArgumentParser(description="STM32 双机CAN 上位机")
    ap.add_argument("-p", "--port", help="串口，如 COM5")
    ap.add_argument("--list", action="store_true", help="仅列出串口")
    args = ap.parse_args()

    if args.list:
        for p in list_ports.comports():
            print(f"{p.device}  {p.description}")
        return

    port = args.port or choose_port()
    print(f"连接 {port} @ {BAUDRATE}（Ctrl+C退出；快捷发帧：/send 321 11 22）")
    try:
        ser = serial.Serial(port, BAUDRATE, timeout=0.05)
    except serial.SerialException as e:
        print(Clr.red(f"打开串口失败: {e}"))
        return

    logger = CanLogger()
    stats = {}
    buf = b""
    print(f"CSV记录: {logger.path}")
    try:
        import threading
        stop = threading.Event()

        def reader():
            nonlocal buf
            while not stop.is_set():
                chunk = ser.read(256)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        handle_line(raw.decode("ascii", "replace").rstrip("\r"),
                                    logger, stats)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        ser.write(b"\r")                             # 唤醒提示符
        while True:
            line = input()
            if line.startswith("/send"):
                line = "send" + line[5:]
            ser.write((line + "\r").encode("ascii"))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        ser.close()
        logger.close()
        print(f"\n退出。共记录 {logger.count} 帧 -> {logger.path}")
        if stats:
            print("各ID统计:", {k: v for k, v in sorted(stats.items())})


if __name__ == "__main__":
    main()
