#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
window_rx_test.py —— 在「MCU 上电后的活动窗口期」验证 RX 到底通不通。

背景：
  本固件主循环里唯一的周期性串口输出是 BSP_UART_DiagTask()，它最多打印 30 次
  （每次间隔 3s ≈ 90 秒），之后永久静默（s_rxIrqCnt 一旦非 0 就立即停）。
  因此要验证 RX，必须在**刚上电的活动窗口内**做。

流程：
  1) 打开端口（dtr=False rts=False）
  2) 提示用户拔插 USB 线（触发真实上电复位）
  3) 上电后持续：
       - 后台收数据（统计 [uart] 诊断行的 edges / rxIrq / PA10）
       - 每隔 1s 向板子写一行命令（\r\n 结尾），观察是否有回显/响应
  4) 结束时给出判据

用法：
  python tools/window_rx_test.py COM3
  python tools/window_rx_test.py COM3 20     # 抓 20 秒
"""
import re
import sys
import time

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)


def open_safe(port, baud=115200):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.05
    ser.write_timeout = 0.5
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


EDGE_RE = re.compile(r"edges/20ms=(\d+)")
IRQ_RE = re.compile(r"rxIrq=(\d+)")
PA10_RE = re.compile(r"PA10=(\d)")
CR1_RE = re.compile(r"CR1=([0-9A-Fa-f]+)")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    print("=" * 68)
    print("window_rx_test：%s —— 上电窗口期 RX 验证（抓 %.0f 秒）" % (port, secs))
    print("=" * 68)

    try:
        ser = open_safe(port)
    except Exception as e:
        print("!! 打开失败：%s" % e)
        return

    print("\n>>> 请现在【拔掉 USB 线，等 2 秒，再插回去】，然后按回车 <<<")
    print("    （按回车后脚本开始接收 + 周期发送）", end="")
    try:
        input()
    except EOFError:
        pass

    t0 = time.time()
    buf = bytearray()
    lines = []
    edges_vals, irq_vals, pa10_vals, cr1_vals = [], [], [], []
    tx_count = 0
    last_tx = 0.0

    print("\n开始。板子刚上电，正在接收并每 1s 发一行 'hello' 测试...\n")

    while (time.time() - t0) < secs:
        # ---- 收 ----
        chunk = ser.read(256)
        if chunk:
            buf.extend(chunk)
            txt = bytes(chunk).decode("utf-8", "replace")
            for ln in txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                ln = ln.strip()
                if not ln:
                    continue
                lines.append((time.time() - t0, ln))
                m = EDGE_RE.search(ln)
                if m:
                    edges_vals.append(int(m.group(1)))
                m = IRQ_RE.search(ln)
                if m:
                    irq_vals.append(int(m.group(1)))
                m = PA10_RE.search(ln)
                if m:
                    pa10_vals.append(int(m.group(1)))
                m = CR1_RE.search(ln)
                if m:
                    cr1_vals.append(m.group(1))

        # ---- 发（每 1s 一行）----
        now = time.time() - t0
        if (now - last_tx) >= 1.0:
            try:
                ser.write(b"hello\r\n")
                tx_count += 1
            except Exception as e:
                pass
            last_tx = now

    ser.close()

    print("-" * 68)
    print("收到 %d 字节，%d 行；发出 %d 次 'hello'" % (len(buf), len(lines), tx_count))
    print("\n打印全部收到内容（前 40 行）：")
    for dt, ln in lines[:40]:
        print("  t=%5.2fs | %s" % (dt, ln[:76]))

    print("\n" + "=" * 68)
    print("判据汇总")
    print("=" * 68)
    print("  edges/20ms 历史 : %s" % (edges_vals[:15] if edges_vals else "无"))
    print("  rxIrq 历史      : %s" % (irq_vals[:15] if irq_vals else "无"))
    print("  PA10 电平历史   : %s" % (pa10_vals[:15] if pa10_vals else "无"))
    print("  CR1 历史        : %s" % (cr1_vals[:8] if cr1_vals else "无"))
    print()

    if not lines:
        print("  [X] 窗口期内零输出 —— 板子要么没复位，要么 TX 通路断。")
        print("      请确认：拔插的是板子的 USB 线（不是 ST-Link）。")
    else:
        max_e = max(edges_vals) if edges_vals else None
        max_i = max(irq_vals) if irq_vals else None
        if max_e is not None and max_e > 0:
            print("  [!] edges/20ms 曾达到 %d —— **PA10 上有电平跳变**！" % max_e)
            if max_i == 0:
                print("      但 rxIrq 仍为 0 ⟹ 信号到了引脚，USART/中断/波特率问题。")
            else:
                print("      rxIrq=%d ⟹ **RX 完全正常**，数据已被 MCU 收到！" % max_i)
        elif max_e == 0:
            print("  [X] edges/20ms 恒为 0 —— 我发送的 'hello' **没有到达 PA10**。")
            print("      本机 TX→板 RX 这段（CH340K TXD → PA10）不通，或板子那时没在跑。")
        else:
            print("  [?] 未捕获到 [uart] 诊断行（可能窗口过期/已收到数据）。")
            print("      看上面打印内容，若出现 'rxIrq' 非 0 即 RX 通。")
    print("=" * 68)


if __name__ == "__main__":
    main()
