#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口取证工具：把设备串口输出**带时间戳**全量落盘，并可在采集期间触发 ST-Link 硬件复位。

为什么要专门做一个（都不是小事）：

  1) **判断"板子到底在不在跑"必须看复位之后的输出。**
     CubeProgrammer 的 `mode=UR`（复位下连接）是"连接即 halt"，用它读 PC 会得到
     "PC 永远停在复位向量"的假象（见 `ota/docs/09` §3）。
     本工具的做法相反：**先开串口监听，再发一次硬件复位，然后只读串口**——
     全程不读 PC、不 halt，因此不受调试器影响。

  2) **回滚验证要"连续复位 N 次"，人工手速做不准。**
     `--resets N` 可以精确控制复位次数与间隔，用来观察 `trialLeft` 递减与自动回滚。

  3) **串口助手关窗即丢证据。**
     本工具把**带时间戳文本**与**原始字节**双份落盘，便于事后逐字节复核。

  4) **掉电测试会让 USB 转串口芯片从总线上消失。**
     第一版没接住这个异常，"让用户断电重上电"这个最关键的测试恰好把采集器弄死
     （日志只剩一条 traceback）。现在串口消失会被记成带时间戳的事件，
     并按 `--reopen-interval` 自动重连，恢复后继续采集 —— 掉电时刻因此可追溯。

用法:
    # 只监听 5 秒（完全不碰调试器，最安全）
    python ota/scripts/serial_capture.py --port COM5 --seconds 5

    # 监听 COM5 + COM7，0.5s 后触发一次硬件复位（采集 8 秒）
    python ota/scripts/serial_capture.py --port COM5 --port COM7 --seconds 8 --reset

    # 连续复位 4 次，间隔 2.5s（回滚验证：看 trialLeft 递减 / 自动回滚）
    python ota/scripts/serial_capture.py --port COM5 --seconds 14 --resets 4 --interval 2.5

输出（默认落在工作区 `logs/`）:
    serial_<时间戳>.log     带时间戳文本（人看）
    serial_<时间戳>.raw     原始字节（机器复核）
    serial_<时间戳>.reset   复位命令的原始回显（若触发了复位）
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import serial

from paths import workspace_root

CLI = r'D:\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe'
ROOT = workspace_root()

# 控制台默认是 GBK（cp936），本工具会打印中文与符号，必须显式改成 UTF-8，
# 否则一条 UnicodeEncodeError 会在"本来就已经没有输出"的时候再叠一层假故障。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def _ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def available_ports():
    """列出本机串口，便于排查"打错端口名"。不使用 serial.tools，避免额外导入开销。"""
    try:
        from serial.tools import list_ports
        return sorted(p.device for p in list_ports.comports())
    except Exception:
        return []


def trigger_reset(reset_log, mode='normal'):
    """发一次**硬件复位并运行**。

    刻意用 `mode=normal` 而不是 `mode=UR`：
      * `mode=UR` 是复位下连接，连接后 CPU 被 halt，且 NRST 被拉住；
      * `mode=normal` 连接→复位→运行，CLI 退出后 SWD 释放，内核自由运行。

    这也正是"用调试器做一次等效冷启动"的手法：复位是真复位（NRST 拉低），
    只是没有断电——RAM 不会掉电，但固件从向量表重新开始执行。
    """
    cmd = [CLI, '-c', 'port=SWD', 'mode=%s' % mode, '-rst']
    with open(reset_log, 'a', encoding='utf-8', errors='replace') as f:
        f.write('\n=== RESET @ %s ===\n' % datetime.now().isoformat(timespec='milliseconds'))
        f.write('CMD: %s\n' % ' '.join(cmd))
        f.flush()
        try:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=60)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -1
            f.write('TIMEOUT after 60s\n')
        f.write('RETURNCODE: %s\n' % rc)
    return rc


def schedule_resets(times, reset_log, mode, results, stop_flag):
    """在指定时刻各触发一次复位；复位本身放在后台线程，避免阻塞串口读取。"""
    for i, t in enumerate(times, 1):
        while not stop_flag.is_set():
            dt = t - time.monotonic()
            if dt <= 0:
                break
            stop_flag.wait(min(dt, 0.05))
        if stop_flag.is_set():
            break
        print('[%s] --- reset %d/%d ---' % (_ts(), i, len(times)))
        results.append(trigger_reset(reset_log, mode))


class PortWatcher:
    """一个串口的读端，**能扛住 USB 设备中途消失**（掉电测试的必需能力）。

    为什么必须扛：板子一断电，板载/外接的 USB 转串口芯片就会从 USB 上消失，
    句柄立刻失效，`in_waiting` 抛 `ClearCommError / PermissionError(13)`。
    第一版没接住这个异常，于是"让用户断电重上电"这个最关键的测试**恰好把采集器弄死**
    （实测：日志只留下一条 traceback，什么都没收到）。
    现在把"消失"当成一等事件：记时间戳、标 OFFLINE、按 `--reopen-interval` 轮询重开，
    重新枚举成功后继续采集。日志里的 lost/back 时间戳本身就是掉电时刻的证据。
    """

    def __init__(self, name, baud, reopen_interval=0.5, out=None, raw=None):
        self.name = name
        self.baud = baud
        self.reopen_interval = reopen_interval
        self.out = out
        self.raw = raw
        self.ser = None
        self.buf = bytearray()
        self.lines = 0
        self.bytes = 0
        self.drops = 0
        self.next_try = 0.0
        self._next_list_note = 0.0
        self.online = False
        self._open(initial=True)

    def _open(self, initial=False):
        try:
            self.ser = serial.Serial(self.name, self.baud, timeout=0, rtscts=False, dsrdtr=False)
            # ⚠️ 必须显式放开 DTR/RTS：pyserial 打开端口时默认会把它们拉高，
            # 而很多 CH340/CP210x 小板把 DTR/RTS 接到了目标板的 NRST/BOOT0
            # （自动复位下载电路）。拉高就等于**把板子按在复位上** ——
            # 现象是"XCOM 能看到日志、本工具一个字节都收不到"（实测踩过）。
            try:
                self.ser.dtr = False
                self.ser.rts = False
            except Exception:                         # noqa: BLE001
                pass
            self.online = True
            if not initial:
                self._say('串口恢复（设备重新枚举）')
            return True
        except Exception as exc:                      # noqa: BLE001
            self.ser = None
            self.online = False
            if initial:
                raise
            return False

    def _say(self, msg):
        line = '[%s] %-5s ** %s' % (_ts(), self.name, msg)
        print(line)
        if self.out:
            self.out.write(line + '\n')
            self.out.flush()

    def poll(self):
        """返回本次读到的行列表；设备消失时返回空并进入重连倒计时。"""
        if not self.online:
            now = time.monotonic()
            if now >= self.next_try:
                self.next_try = now + self.reopen_interval
                if self._open():
                    return []
                # 重连不上时定期把"现在有哪些串口"打出来 —— 掉电重枚举后
                # 端口号可能变化（COM5 -> COM8），没有这行就只能干等
                if now >= self._next_list_note:
                    self._next_list_note = now + 5.0
                    self._say('重连失败，当前串口: %s' % (', '.join(available_ports()) or '无'))
            return []

        try:
            n = self.ser.in_waiting
            chunk = self.ser.read(n) if n else b''
        except Exception as exc:                      # noqa: BLE001  设备拔掉/断电
            self.drops += 1
            self._say('串口消失（%s）—— 可能是板子断电，转入重连' % type(exc).__name__)
            try:
                self.ser.close()
            except Exception:                         # noqa: BLE001
                pass
            self.online = False
            self.next_try = time.monotonic() + self.reopen_interval
            return []

        if not chunk:
            return []
        self.bytes += len(chunk)
        if self.raw:
            self.raw.write(b'[%s]' % self.name.encode() + chunk)
            self.raw.flush()
        self.buf += chunk
        out = []
        while True:
            idx = None
            for sep in (b'\n', b'\r'):
                j = self.buf.find(sep)
                if j >= 0 and (idx is None or j < idx):
                    idx = j
            if idx is None:
                break
            line = bytes(self.buf[:idx])
            del self.buf[:idx + 1]
            if line.strip():
                out.append(line.decode('utf-8', errors='replace'))
                self.lines += 1
        return out

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:                         # noqa: BLE001
                pass


def main():
    ap = argparse.ArgumentParser(add_help=True, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', action='append', required=True,
                    help='串口名，可重复（如 --port COM5 --port COM7）')
    ap.add_argument('--baud', type=int, default=115200, help='波特率，默认 115200')
    ap.add_argument('--seconds', type=float, default=8.0, help='采集时长（秒）')
    ap.add_argument('--reset', action='store_true', help='采集期间触发一次硬件复位')
    ap.add_argument('--resets', type=int, default=0, help='触发 N 次硬件复位（覆盖 --reset）')
    ap.add_argument('--reset-delay', type=float, default=0.5, help='首次复位延迟（秒）')
    ap.add_argument('--interval', type=float, default=2.5, help='多次复位之间的间隔（秒）')
    ap.add_argument('--reset-mode', default='normal', choices=['normal', 'UR', 'hotplug'],
                    help='复位时 CubeProgrammer 的连接模式，默认 normal（UR 是连接即 halt，别用）')
    ap.add_argument('--out', help='输出目录，默认 <工作区>/logs')
    ap.add_argument('--reopen-interval', type=float, default=0.5,
                    help='串口掉线后的重连轮询间隔（秒），默认 0.5')
    ap.add_argument('--loopback', action='store_true',
                    help='回环自检：把 TXD 与 RXD 短接后运行，用来判定"串口/驱动本身是否正常"'
                         '（从而把"工具问题"与"板子接线问题"分开）')
    ap.add_argument('--lp-pattern', default=None,
                    help='回环自检发送的内容，默认 "LOOPBACK-<时间戳>"')
    args = ap.parse_args()

    outdir = args.out or os.path.join(ROOT, 'logs')
    os.makedirs(outdir, exist_ok=True)

    # ---------------------------------------------------------------- 回环自检
    # 用途：把"串口打不开/驱动异常/端口选错"与"板子 TX 没接到适配器 RX"分开。
    # 做法：把适配器的 TXD 与 RXD 短接，本工具发一串内容并读回；读回一致即链路（PC 侧）正常。
    if args.loopback:
        pattern = args.lp_pattern or ('LOOPBACK-%s' % datetime.now().strftime('%H%M%S'))
        data = (pattern + '\r\n').encode('ascii', 'replace')
        print('回环自检 @ %s  %d baud' % (', '.join(args.port), args.baud))
        print('  请确认适配器的 TXD 与 RXD 已经**短接**（板子可以拔掉）')
        print('  发送: %r' % data)
        allok = True
        for name in args.port:
            try:
                with serial.Serial(name, args.baud, timeout=0.2, write_timeout=1.0) as sp:
                    sp.reset_input_buffer()
                    sp.write(data)
                    sp.flush()
                    got = sp.read(len(data) + 16)
            except Exception as exc:                  # noqa: BLE001
                print('  [FAIL] %-5s 打不开/写入失败: %s' % (name, exc))
                allok = False
                continue
            if got == data:
                print('  [ OK ] %-5s 读回一致 -> 串口/驱动正常（问题在板子接线或端口对应）' % name)
            elif got:
                print('  [FAIL] %-5s 读回不一致 -> 发 %r 收 %r（波特率/线序问题）' % (name, data, got))
                allok = False
            else:
                print('  [FAIL] %-5s 什么都没读回 -> TXD/RXD 没短接，或该端口不是这个适配器' % name)
                allok = False
        print()
        print('结论:', '回环正常' if allok else '回环失败 —— 先解决适配器/端口，再查板子接线')
        return 0 if allok else 1

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = os.path.join(outdir, 'serial_%s' % stamp)
    log_path, raw_path, reset_log = base + '.log', base + '.raw', base + '.reset'

    f_log = open(log_path, 'w', encoding='utf-8', errors='replace')
    f_raw = open(raw_path, 'wb')

    watchers = []
    for name in args.port:
        try:
            watchers.append(PortWatcher(name, args.baud, args.reopen_interval,
                                        out=f_log, raw=f_raw))
        except Exception as exc:                      # 端口占用 / 不存在
            print('!! 打不开 %s: %s' % (name, exc))
            print('   本机现有串口: %s' % (', '.join(available_ports()) or '无'))
            print('   （若被串口助手占用，请先在助手那边点"关闭串口"）')
            for w in watchers:
                w.close()
            f_log.close()
            f_raw.close()
            return 2

    n_resets = args.resets if args.resets > 0 else (1 if args.reset else 0)
    times, t0 = [], time.monotonic()
    for i in range(n_resets):
        times.append(t0 + args.reset_delay + i * args.interval)

    print('监听 : %s @ %d' % (', '.join(w.name for w in watchers), args.baud))
    print('时长 : %.1fs   复位: %d 次   掉线自动重连: 开' % (args.seconds, n_resets))
    print('落盘 : %s.{log,raw%s}' % (base, ',reset' if n_resets else ''))
    print('-' * 72)

    stop_flag = threading.Event()
    results = []
    if n_resets:
        threading.Thread(target=schedule_resets,
                         args=(times, reset_log, args.reset_mode, results, stop_flag),
                         daemon=True).start()

    f_log.write('# serial_capture  ports=%s baud=%d  resets=%d  started=%s\n'
                % (','.join(w.name for w in watchers), args.baud, n_resets,
                   datetime.now().isoformat(timespec='milliseconds')))
    f_log.flush()
    try:
        deadline = t0 + args.seconds
        while time.monotonic() < deadline:
            got = False
            for w in watchers:
                for text in w.poll():
                    got = True
                    elapsed = time.monotonic() - t0
                    print('[%s] %-5s +%6.2fs | %s' % (_ts(), w.name, elapsed, text))
                    f_log.write('[%s] %-5s +%6.2fs | %s\n' % (_ts(), w.name, elapsed, text))
                    f_log.flush()
            if not got:
                time.sleep(0.02)
    except KeyboardInterrupt:
        print('\n(中断)')
    finally:
        stop_flag.set()
        for w in watchers:
            w.close()
        f_log.write('# ended %s  lines=%s bytes=%s drops=%s\n'
                    % (datetime.now().isoformat(timespec='milliseconds'),
                       {w.name: w.lines for w in watchers},
                       {w.name: w.bytes for w in watchers},
                       {w.name: w.drops for w in watchers}))
        f_log.close()
        f_raw.close()

    print('-' * 72)
    for w in watchers:
        print('统计 : %-5s 收到 %d 行 / %d 字节，掉线 %d 次' % (w.name, w.lines, w.bytes, w.drops))
    if n_resets:
        print('复位 : %s' % results)
    print('日志 : %s' % log_path)

    total = sum(w.lines for w in watchers)
    if total == 0:
        print()
        print('[!] 一条输出都没有。按优先级排查：')
        print('    1) 板子是否上电 / 串口线是否接在本工具的端口上（本机串口: %s）'
              % (', '.join(available_ports()) or '无'))
        print('    2) 波特率是否 115200（BL/APP 都用 115200）')
        print('    3) 调试器是否仍处于 halt：cube 连接会把内核停住 —— 必须**真断电重上电**')
        print('    4) 固件是否卡在 clock 配置之前（此时串口还没初始化，必然无输出）')
    return 0 if total else 1


if __name__ == '__main__':
    sys.exit(main())
