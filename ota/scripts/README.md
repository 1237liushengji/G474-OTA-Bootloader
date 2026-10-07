# ota/scripts · 构建与复核脚本

这些脚本是**本次 OTA 落地过程中的工具**，不是固件的一部分，但对"改完能自证"很重要。
全部为纯 Python 标准库，无需网络（`compare_with_github.py` 需要你已下载官方归档）。

## 常用（建议保留在流程里）

| 脚本 | 作用 | 何时跑 |
|---|---|---|
| `verify_layout.py` | **收尾必跑**：核对 BL / APP 的链接地址与体积是否与分区表一致 | 每次改链接配置或分区后 |
| `audit_bl_layout.py` | **审计链接产物与片上实况**：镜像有没有压进控制区页（会变砖）、复位向量指向的代码是否还在。`--on-chip` 连芯片一起查 | 每次重编 BL 之后、上板之前 |
| `bl_layout.py` | BL 链接布局**单一真相源**（scatter 文本 + 不变量检查）；`--write` 幂等写入 `bl.sct` | 布局需要修正时（通常由 `fix_bl_region.py --apply` 调起） |
| `serial_capture.py` | 串口取证：带时间戳文本 + 原始字节双落盘，可触发复位，**掉线自动重连**（掉电测试必需） | 上板验证、掉电测试、判断"到底在不在跑" |
| `make_bl_project.py` | 从已验证的 APP 工程**派生** Bootloader 的 Keil 工程 | BL 工程需要重建时 |
| `fix_bl_region.py` | 幂等修正 `bl.sct` 与工程的 `<umfTarg>`/IROM 并复核 | 用 Keil GUI 打开保存过工程之后 |
| `fix_main_c.py` | 幂等重放 `main.c` 的 5 处 OTA 接线改动 | `main.c` 被 Keil 覆盖回原版后 |
| `inventory.py` | 盘点工作区（分类统计 + 健康检查） | 整理前后 |
| `tidy_workspace.py` | 清理构建产物 / 缓存 / 临时文件 | 工作区变乱时 |
| `check_hal_symbols.py` | 逐个核对代码用到的 HAL 符号是否存在于真实头文件 | 无编译器环境下的替代验证 |

## 历史（保留供追溯，一般不需要再跑）

| 脚本 | 作用 |
|---|---|
| `apply_ota_patches.py` | 初版补丁脚本（18 处锚点替换），已被实际落地取代 |
| `sync_partition_docs.py` | 分区表变更时同步文档/上位机/测试里的地址 |
| `sync_slot_numbers.py` | 槽尺寸变更时同步"满槽块数/耗时"等数字 |
| `compare_with_github.py` | 与 GitHub 官方归档逐文件对比（需先下载并解包） |

## 关于 `paths.py`

脚本被收纳到本目录后，不能再简单用 `dirname(__file__)` 当工作区根，
因此统一 `from paths import workspace_root`：从脚本位置向上查找含 `G474/MDK-ARM` 的目录。

## 工程构建（命令行，无需打开 Keil GUI）

```powershell
$uv = 'D:\U5\UV4\UV4.exe'   # 按你的 Keil 安装路径调整
# 重建 Bootloader
& $uv -r "ota\bootloader\MDK-ARM\BL.uvprojx" -j0 -o "$env:TEMP\bl.log"
# 重建 APP（Slot A）
& $uv -r "G474\MDK-ARM\G474.uvprojx"         -j0 -o "$env:TEMP\app.log"
# 核对链接地址与体积
python ota\scripts\verify_layout.py
```

> ⚠️ **重要**：Keil 打开/构建工程时会**重写 `.uvprojx` 与它管理的源文件**。
> 本项目中它曾多次把 `IROM1`、`VECT_TAB_OFFSET` 宏、`main.c` 的改动覆盖回原版。
> 因此**每次用 GUI 打开并保存过工程后，请重跑 `verify_layout.py` 与 `fix_main_c.py` 确认**。
