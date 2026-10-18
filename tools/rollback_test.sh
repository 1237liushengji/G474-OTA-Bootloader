#!/bin/bash
# 回滚验证：在 OTA 触发对端复位后，抢在 3s 自检窗口内连续复位，
# 迫使 trialLeft 递减到 0，观察 BL 是否自动回退到 Slot A。
cd "D:/WorkBuddy Data/Claw/OTA_Bootloader" || exit 1
PY="C:/Users/13925/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
P="D:/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"
SN="49FF6D068065575312232167"
LOG=logs/rb_a.txt
OUT=logs/rb_b.txt

rm -f "$LOG" "$OUT"
"$PY" tools/quiet_capture.py --port COM3 --secs 70 > "$LOG" 2>&1 &

echo "[..] 等待 OTA 走到 'peer reset sent' ..."
hit=0
for i in $(seq 1 350); do
  if grep -qa "peer reset sent" "$LOG" 2>/dev/null; then hit=1; break; fi
  sleep 0.2
done
if [ "$hit" != "1" ]; then echo "[ERR] 未等到 OTA 复位事件"; exit 1; fi
echo "[OK ] 捕获到 OTA 复位，开始抢复位"

"$PY" tools/quiet_capture.py --port COM5 --secs 12 > "$OUT" 2>&1 &
sleep 0.5
for i in 1 2 3 4; do
  "$P" -c port=SWD sn=$SN mode=UR -rst >/dev/null 2>&1
  echo "  preempt reset $i"
  sleep 1.2
done
wait
echo "=== BL 决策序列 ==="
grep -a "BL:" "$OUT" | head -12
