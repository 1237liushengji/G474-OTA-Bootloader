#!/bin/bash
# 回滚验证：在 OTA 触发对端复位后，抢在 3s 自检窗口内连续复位，
# 迫使 trialLeft 递减到 0，观察 BL 是否自动回退到 Slot A。
#
# 用法：
#   bash tools/rollback_test.sh <ST-Link序列号> [串口A] [串口B]
# 例：
#   bash tools/rollback_test.sh 49FF6D068065575312232167 COM3 COM5
#
# 依赖：python3 + pyserial；STM32CubeProgrammer
#       （路径不在 PATH 里时用环境变量覆盖：CUBEPROG=/path/to/STM32_Programmer_CLI）
#
# 判定标准：BL 日志应出现
#   "trial exhausted -> rollback to slot 0"  然后 "jump to APP slot 0 ..."

set -u

SN="${1:?用法: rollback_test.sh <ST-Link序列号> [串口A] [串口B]}"
PORT_A="${2:-COM3}"
PORT_B="${3:-COM5}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PY="${PYTHON:-python3}"
CLI="${CUBEPROG:-STM32_Programmer_CLI}"

LOG=logs/rb_a.txt
OUT=logs/rb_b.txt
mkdir -p logs
rm -f "$LOG" "$OUT"

"$PY" tools/quiet_capture.py --port "$PORT_A" --secs 70 > "$LOG" 2>&1 &

echo "[..] 等待 OTA 走到 'peer reset sent' ..."
hit=0
for _ in $(seq 1 350); do
  if grep -qa "peer reset sent" "$LOG" 2>/dev/null; then hit=1; break; fi
  sleep 0.2
done
if [ "$hit" != "1" ]; then echo "[ERR] 未等到 OTA 复位事件"; exit 1; fi
echo "[OK ] 捕获到 OTA 复位，开始抢复位"

"$PY" tools/quiet_capture.py --port "$PORT_B" --secs 12 > "$OUT" 2>&1 &
sleep 0.5
for i in 1 2 3 4; do
  "$CLI" -c port=SWD sn="$SN" mode=UR -rst >/dev/null 2>&1
  echo "  preempt reset $i"
  sleep 1.2
done
wait
echo "=== BL 决策序列 ==="
grep -a "BL:" "$OUT" | head -12
