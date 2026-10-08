#!/usr/bin/env bash
# The four newest 樊登读书会 episodes (downloaded after the copy pass finished on 2026-10-07
# 17:47) are classified `copy`: H.264 video (one of them is actually an MPEG-TS named .mp4) with
# **mp3** audio, which no iPad can play. They need video-bit-identical remux + AAC audio.
#
# A full `--tier copy` pass would redo all 193 already-converted copy-tier files (the copy tier's
# only skip test is "already HEVC", which a copy output is not), so these four go through the
# single-file path instead - same tier rule, same gates, same hash-verified install.
#
# Sequential, one lock (run-copy.lock), and they never touch the snapshot originals.
set -u
TOOLS=/c/Users/hoelee/AppData/Local/hermes/tools
PY="$(ls -d $TOOLS/python-*/python.exe 2>/dev/null | head -1)"
LOG=/d/tmp/xcode/run_new4.log
BASE='Z:\Class\樊登读书\樊登读书会（每周更新）\樊登读书2026年（更新中）'

cd /d/dev/universal-video-transcode || exit 1
{
  echo "=== new-episode copy pass start $(date '+%F %T')  python=$PY"
  for r in '1004 反内卷\反内卷.mp4' \
           '0912 感官觉醒\感官觉醒.mp4' \
           '0905 如何成为情绪稳定的父母\如何成为情绪稳定的父母.mp4' \
           '0822 超赞的一代\超赞的一代.mp4'; do
    echo "--- $r"
    "$PY" -u scripts/library_run.py --tier copy --path "$BASE\\$r" --keep-source
    echo "    rc=$?"
  done
  echo "=== new-episode copy pass done $(date '+%F %T')"
} >> "$LOG" 2>&1
