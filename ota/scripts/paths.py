#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区根定位：从本文件位置向上找包含 G474/MDK-ARM 的目录。

脚本被收纳到 ota/scripts/ 后，不能再简单用 dirname(__file__) 当工作区根，
因此统一走这里，保证移动/复制后仍可用。
"""
import os


def workspace_root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(d, 'G474', 'MDK-ARM')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # 退化为"上两级"，与收纳位置 ota/scripts/ 匹配
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


ROOT = workspace_root()
