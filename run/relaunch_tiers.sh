#!/usr/bin/env bash
# Relaunch the two remaining HEVC tiers of the 樊登读书会 conversion, DETACHED from Hermes
# so a session ending cannot kill them: the previous incarnation was killed mid-encode at
# 10:37 on 2026-10-08 when its Hermes session was closed.
#
# Idempotent: files already converted are skipped, a failure leaves the live file untouched and
# the next attempt retries it. One lock per tier (run-<tier>.lock).
set -u
TOOLS=/c/Users/hoelee/AppData/Local/hermes/tools
PY="$(ls -d $TOOLS/python-*/python.exe 2>/dev/null | head -1)"
LIB='Z:\Class\#snapshot\GMT+08-2026.09.25-09.50.20\樊登读书\樊登读书会（每周更新）'
LOG=/d/tmp/xcode

cd /d/dev/universal-video-transcode || exit 1
if [ ! -x "$PY" ]; then echo "FATAL python not found under $TOOLS" >> $LOG/relauncher.out; exit 1; fi
echo "=== relauncher start $(date '+%F %T')  python=$PY" >> $LOG/relauncher.out

for t in x265 nvenc; do
  (
    for i in 1 2 3 4 5 6 7 8; do
      echo "=== relaunch attempt $i $(date '+%F %T')" >> "$LOG/run_$t.log"
      "$PY" -u scripts/library_run.py --tier "$t" --origin-root "$LIB" --reverse \
            >> "$LOG/run_$t.log" 2>&1
      rc=$?
      echo "$t PASS-DONE rc=$rc attempt=$i $(date '+%F %T')" >> "$LOG/run_$t.log"
      [ $rc -eq 0 ] && break
      sleep 60
    done
  ) &
  sleep 2
done
wait
