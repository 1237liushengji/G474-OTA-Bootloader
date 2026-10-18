#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2「串口 RX 不通」一体化判据脚本。

设计目标：一次运行，同时回答三个问题——
  ① PC 侧到底发出去了没有？（写成功字节数）
  ② MCU 侧 RX 引脚 PA10 上到底有没有电平跳变？（解析固件周期打印的 `edges/20ms`）
  ③ MCU 侧 USART1 中断到底进了没有？（解析 `rxIrq`）

背景（本项目已定性）：
  * 该板 **没有板载 USB 转串口芯片**，PA9/PA10 只引到 J4(2x4) 调试座，
    必须外接 USB-TTL，且只能接 3 根：TXD→PA10、RXD→PA9、GND↔GND。
  * 固件的 BSP_UART_DiagTask 每 3 秒打印一次：
      [uart] CR1=.. CR3=.. BRR=.. ISR=..
      [uart] MODER=.. IDR=.. PA10=.. edges/20ms=..
      [uart] ISER1=.. rxIrq=.. drop=.. head=.. tail=..
      [uart] gState=.. RxState=.. RxCnt=..
    其中 `edges/20ms` 与 `rxIrq` 是判据核心。
  * 注意：RxState=34(0x22=HAL_UART_STATE_BUSY_RX) 是 HAL_UART_Receive_IT 挂起后的
    **正常**状态，不是故障。

用法:
  python tools/p2_rx_probe.py --port COM3 --secs 12
  python tools/p2_rx_probe.py --port COM5 --secs 12 --interval 2.5
"""
import argparse
import re
import sys
import threading
import time

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial")
    sys.exit(1)


def open_safe(port, baud):
    """open 前先设 dtr/rts=False —— 否则 CH340 会把板子按在复位上。"""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.1
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


RE_EDGES = re.compile(r"edges/20ms=(\d+)")
RE_RXIRQ = re.compile(r"rxIrq=(\d+)")
RE_PA10 = re.compile(r"PA10=(\d)")
RE_DROP = re.compile(r"drop=(\d+)")
RE_CORESET = re.compile(r"Reset timing|Software reset|Reset mode")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=12.0)
    ap.add_argument("--interval", type=float, default=2.5,
                    help="持续发数据的间隔（秒）")
    ap.add_argument("--payload", default="P2RXPROBE")
    args = ap.parse_args()

    print(f"=== P2 RX 判据 @ {args.port} {args.baud} 8N1，"
          f"共 {args.secs}s，每 {args.interval}s 发一组 ===\n", flush=True)

    try:
        ser = open_safe(args.port, args.baud)
    except Exception as exc:  # noqa: BLE001
        print(f"[打不开] {args.port}: {exc}")
        print("  · 端口被占用？关掉 XCOM / 串口助手 / 其它脚本")
        return 2

    text = ""
    lock = threading.Lock()
    stop = threading.Event()

    def reader():
        nonlocal text
        while not stop.is_set():
            try:
                d = ser.read(4096)
            except Exception:  # noqa: BLE001
                break
            if d:
                with lock:
                    text += d.decode("utf-8", "replace")

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    sent = 0
    t_end = time.time() + args.secs
    n = 0
    try:
        while time.time() < t_end:
            msg = f"{args.payload}_{n}\r\n".encode()
            try:
                sent += ser.write(msg)
                ser.flush()
            except Exception as exc:  # noqa: BLE001
                print(f"  [写失败] {exc}")
                break
            n += 1
            print(f"  [{n:02d}] PC 侧已发 {len(msg)} 字节：{msg.decode().strip()}", flush=True)
            time.sleep(args.interval)
    finally:
        time.sleep(0.5)
        stop.set()
        th.join(timeout=1.0)
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass

    edges = [int(x) for x in RE_EDGES.findall(text)]
    rxirq = [int(x) for x in RE_RXIRQ.findall(text)]
    pa10 = [x for x in RE_PA10.findall(text)]
    drop = [int(x) for x in RE_DROP.findall(text)]
    resets = len(RE_CORESET.findall(text))

    print("\n" + "=" * 62)
    print(f"PC 侧共写出 {sent} 字节，{n} 组消息")
    print(f"MCU 侧诊断行：edges 样本 {len(edges)} 个，rxIrq 样本 {len(rxirq)} 个")
    print(f"MCU 侧复位迹象（Reset 行）：{resets}")

    if not edges and not rxirq:
        print("\n⚠️ 没有解析到 [uart] 诊断行 —— 可能：")
        print("   · 板子没在跑（先确认能收到 BL/APP 横幅）")
        print("   · 诊断任务已经打满 30 次停了（上电后约 90s 内有效，可复位板子重来）")
        print("   · 串口被 DTR/RTS 复位中（确认脚本已 dtr/rts=False）")
        return 3

    emax = max(edges) if edges else -1
    rmax = max(rxirq) if rxirq else -1

    print("\n--- 判据 ---")
    print(f"  edges/20ms 最大值 = {emax}")
    print(f"  rxIrq      最大值 = {rmax}")
    print(f"  PA10 电平样本     = {sorted(set(pa10))}  (期望全为 '1' 空闲高)")
    print(f"  drop 样本         = {sorted(set(drop))}")

    print()
    if emax > 0 and rmax > 0:
        print("✅ 结论：RX 已打通！PA10 有跳变，且 USART1 中断已进入。")
        print("   → 继续：敲回车应能看到控制台提示符。")
        return 0
    if emax > 0 and rmax == 0:
        print("🟡 结论：信号已到达 PA10（有跳变），但 USART1 未产生中断。")
        print("   → 查：PC 波特率是否 115200 8N1？PA10 波形幅度是否到 3.3V？")
        return 4
    print("🔴 结论：PA10 零跳变 —— PC 的 TX 物理上没到 PA10。")
    print("   → 按 docs/14 §5 步骤 2B 逐项排查：")
    print("     1) 只接 3 根：TXD→J4 pin5(PA10)、RXD→J4 pin6(PA9)、GND→J4 pin3")
    print("     2) 拔掉 DTR / RTS / CTS / DSR / VCC 全部冗余线")
    print("     3) 万用表量通断；把模块 TXD-RXD 短接做回路自测")
    print("     4) 换到另一块板/另一个模块做对比")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
