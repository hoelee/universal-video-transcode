#!/usr/bin/env bash
# Start ONLY the x265 tier of the 樊登读书会 conversion, DETACHED from Hermes.
#
# Why a separate launcher: run/relaunch_tiers.sh hardcodes `for t in x265 nvenc`, and the nvenc
# tier must NOT start while another job owns the GPU -- the TubeSync 58-file repair batch has held
# the GPU encoder since 2026-10-09 20:36. x265 is pure CPU pinned to the E-cores (0x3FF0000), so it
# shares nothing with the GPU job; the only shared resource is the RaiDrive mount, and 2 concurrent
# share jobs is the documented safe limit (3 produced `[Errno 9] Bad file descriptor`).
#
# Detached for the reason recorded in STATE.md: a Hermes-scoped background job is killed when the
# session ends (measured 2026-10-08 10:37, both tiers killed mid-encode), so the launcher is started
# by `powershell Start-Process ... -WindowStyle Hidden` and its parent exits immediately. No exit
# notification -- watch the log; stop by PID only, never by image name.
#
# Idempotent: already-converted files are skipped, a failure leaves the live file untouched and the
# next attempt retries it. Lock: C:\AI\transcode-run\run-x265.lock (stale-aware: age > 300 s AND a
# dead pid is taken over automatically, so the lock left by the 16:51 reboot needs no cleanup).
#
# Started correctly (the argument form matters): the script path goes in -ArgumentList ALONE.
#   Start-Process -FilePath "C:\Program Files\Git\bin\bash.exe" -ArgumentList "<this script>" -WindowStyle Hidden
# NOT "-lc","bash <script>" -- that runs an interactive bash instead and starts nothing.

set -u
TOOLS=/c/Users/hoelee/AppData/Local/hermes/tools
PY="$(ls -d $TOOLS/python-*/python.exe 2>/dev/null | head -1)"
LIB='Z:\Class\#snapshot\GMT+08-2026.09.25-09.50.20\樊登读书\樊登读书会（每周更新）'
LOG=/d/tmp/xcode
OUT="$LOG/run_x265.log"

# The launcher's own banners go into the log too: under -WindowStyle Hidden stdout goes nowhere,
# so an unredirected banner leaves no evidence of how a run ended.
exec >> "$OUT" 2>&1
cd /d/dev/universal-video-transcode || { echo "FATAL cannot cd to the repo"; exit 1; }
echo "=== x265-only relauncher start $(date '+%F %T')  py=$PY"
echo "=== origin root: $LIB"
if [ ! -x "$PY" ]; then echo "FATAL python not found under $TOOLS"; exit 1; fi

for i in 1 2 3 4 5 6 7 8; do
  echo "=== relaunch attempt $i $(date '+%F %T')"
  "$PY" -u scripts/library_run.py --tier x265 --origin-root "$LIB" --reverse
  rc=$?
  echo "x265 PASS-DONE rc=$rc attempt=$i $(date '+%F %T')"
  [ $rc -eq 0 ] && break
  sleep 60
done
