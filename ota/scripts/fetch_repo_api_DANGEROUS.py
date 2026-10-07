#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚠️⚠️ 危险脚本 —— 不要直接对着工作区运行 ⚠️⚠️

用 GitHub API 逐文件重建仓库。**它会把已存在的文件覆盖回上游原版**，
因此在本工作区直接运行会**抹掉 OTA 改动**（实测发生过：can.c / uds.c /
console.c / G474.uvprojx 被还原成上游版本，靠手工恢复才救回）。

## 正确做法（推荐）

用 `codeload.github.com` 一次下载整仓库 zip —— **不受 API 配额限制**：

    https://codeload.github.com/1237liushengji/stm32g474-dual-can/zip/refs/heads/main

解包后按需取文件，再用 `ota/scripts/compare_with_github.py` 做差异核对，
用 `ota/scripts/check_ota_intact.py` 确认 OTA 改动未被破坏。

## 本脚本仅在这些情况下才用

* 需要按 SHA 精确取某几个**上游原版**文件（且确认不覆盖已改文件）；
* 网络环境只有 `api.github.com` 通、且 `codeload` 不通。

即使用时也要注意：`api.github.com` 未认证配额是 **60 次/小时**，
310 个文件必然中途 403（实测下到第 74 个就断）。

用法:
    python tools_fetch_repo.py                 # 重建到 ./
    python tools_fetch_repo.py --dest <目录>    # 重建到指定目录（**推荐**，避免污染工作区）
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

REPO = "1237liushengji/stm32g474-dual-can"
BRANCH = "main"
API = "https://api.github.com"


def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "dsh-repo-fetch",
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                      # 403/429/5xx 退避重试
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError("请求失败 %s: %s" % (url, last))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=".",
                    help="落地目录；默认 '.'。⚠️ 建议指定独立目录，避免覆盖已改文件")
    ap.add_argument("--root", default=".", help="工作区根")
    args = ap.parse_args()

    dest_root = os.path.join(args.root, args.dest)
    if os.path.abspath(dest_root) == os.path.abspath(args.root):
        print("⚠️  你将把上游原版直接铺在当前目录，会覆盖已存在的同名文件（含 OTA 改动）。")
        print("    强烈建议改用 codeload 下载 zip，或指定 --dest 到独立目录。")
        if input("    确实要继续？输入 YES 确认: ").strip() != "YES":
            print("已取消。")
            return 1

    tree = get_json("%s/repos/%s/git/trees/%s?recursive=1" % (API, REPO, BRANCH))
    if tree.get("truncated"):
        print("!! 警告：GitHub 返回的树被截断，结果可能不完整")
    blobs = [t for t in tree["tree"] if t["type"] == "blob"]
    print("仓库共 %d 个文件，开始重建到: %s" % (len(blobs), os.path.abspath(dest_root)))

    ok = skip = fail = 0
    for i, b in enumerate(blobs, 1):
        rel = b["path"]
        out = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out) and os.path.getsize(out) == b.get("size", -1):
            skip += 1
            continue
        try:
            data = get_json("%s/repos/%s/git/blobs/%s" % (API, REPO, b["sha"]))
            raw = base64.b64decode(data["content"])
            with open(out, "wb") as fh:
                fh.write(raw)
            ok += 1
            if i % 25 == 0 or i == len(blobs):
                print("  [%d/%d] %s" % (i, len(blobs), rel))
        except Exception as e:
            fail += 1
            print("  FAIL %s -> %s" % (rel, e))
        time.sleep(0.05)

    print("\n完成: 新下载 %d, 跳过(已存在) %d, 失败 %d" % (ok, skip, fail))
    print("⚠️  若失败数 >0（通常是 403 配额耗尽），请改用 codeload 下载 zip。")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
