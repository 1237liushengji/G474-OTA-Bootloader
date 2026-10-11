#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全的串口打开方式（务必照抄）。

【坑】绝不能写成：
    ser = serial.Serial(port, baud, rtscts=False, dsrdtr=False)
    ser.dtr = False          # ← 太晚了！
    ser.rts = False
Serial 构造/ open() 的瞬间，DTR/RTS 会先被拉高，CH340 板上这两根
常接 NRST/BOOT0 -> 板子被按在复位上（表现为"上电没反应/串口全空白"）。

正确做法：先在未打开状态下设 dtr/rts=False，再 open()。
"""
import serial

def open_safe(port, baud=115200, timeout=0.05):
    ser = serial.Serial()
    ser.port      = port
    ser.baudrate  = baud
    ser.timeout   = timeout
    ser.rtscts    = False
    ser.dsrdtr    = False
    ser.dtr       = False     # 必须先设，再 open
    ser.rts       = False
    ser.open()
    return ser
