#!/usr/bin/env bash
# Start ONLY the nvenc (GPU) tier, DETACHED from Hermes.
#
# run/relaunch_tiers.sh hardcodes `for t in x265 nvenc`, so it cannot be used to start the GPU tier
# alone; this is its nvenc-only twin. One lock per tier (C:\AI\transcode-run\run-nvenc.lock, and
# transcode.Lock is staleness-aware so a lock left by a reboot needs no cleanup).
#
# 2026-10-10: the run is governed by C:\AI\transcode-run\gate_policy.json (owner decision):
#   accept = items where the FRAME-SHARE test is the verdict and the mean bar only a warning
#            (7 items that failed by 0.2-1.7 thousandths of mean while keeping 0.20-0.93% of frames
#            below 0.85; 0912 洞见 at 1.33% is deliberately NOT on the list and keeps being refused)
#   park   = failed quality ROUNDS per item; at PARK_AFTER=2 the item is skipped instead of
#            re-encoded, which stops the ~3.3 GPU-hours/pass those items used to burn. Clear an
#            item's entry there to retry it.
#
# Detached because a Hermes-scoped job dies with the session (measured 2026-10-08). No exit
# notification: watch /d/tmp/xcode/run_nvenc.log. Stop by PID only, never by image name.

set -u
TOOLS=/c/Users/hoelee/AppData/Local/hermes/tools
PY="$(ls -d $TOOLS/python-*/python.exe 2>/dev/null | head -1)"
LIB='Z:\Class\#snapshot\GMT+08-2026.09.25-09.50.20\樊登读书\樊登读书会（每周更新）'
NLOG=/d/tmp/xcode/run_nvenc.log

exec >> "$NLOG" 2>&1
cd /d/dev/universal-video-transcode || { echo "FATAL cannot cd to the repo"; exit 1; }
echo "=== nvenc-only relauncher start $(date '+%F %T')  py=$PY"
echo "=== origin root: $LIB"
echo "=== gate policy: $(cat /c/AI/transcode-run/gate_policy.json 2>/dev/null | tr -d '\n' | cut -c1-160)"
if [ ! -x "$PY" ]; then echo "FATAL python not found under $TOOLS"; exit 1; fi

for i in 1 2 3 4 5 6 7 8; do
  echo "=== relaunch attempt $i $(date '+%F %T')"
  "$PY" -u scripts/library_run.py --tier nvenc --origin-root "$LIB" --reverse
  rc=$?
  echo "nvenc PASS-DONE rc=$rc attempt=$i $(date '+%F %T')"
  [ $rc -eq 0 ] && break
  sleep 60
done
