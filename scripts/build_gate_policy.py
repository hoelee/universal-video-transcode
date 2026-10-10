#!/usr/bin/env python3
"""Build C:\\AI\\transcode-run\\gate_policy.json from today's nvenc run log.

Owner decision 2026-10-10: items that failed the quality gate today are split by the FRAME-SHARE
test (>1% of frames below 0.85 = the documented real defect test, and the only one of the eight that
trips it is 0912 洞见 at 1.33%). Items passing the frame-share test go on the accept list, so the
mean bar stops stranding them; every item that failed a round today gets park=1, so one more failure
parks it (PARK_AFTER=2) instead of re-encoding it forever.

Keys are taken verbatim from the log's "--- i/n <disp key>" lines, never retyped.
"""
import json, os, re, subprocess, sys

LOG = r"D:\tmp\xcode\run_nvenc.log"
POLICY = r"C:\AI\transcode-run\gate_policy.json"
MARK = "nvenc relaunch attempt 1"      # only today's run

items = []          # (disp key, bad share %, mean, bar)
last = None
seen = False
for line in open(LOG, encoding="utf-8", errors="replace"):
    if MARK in line:
        seen = True
    if not seen:
        continue
    m = re.match(r"^\d\d:\d\d:\d\d --- \d+/\d+ (.+?)\s*$", line)
    if m:
        last = m.group(1)
        continue
    if "!! FAIL quality" in line and last:
        sh = re.search(r"([\d.]+)% of frames below", line)
        mn = re.search(r"mean ([\d.]+) < bar ([\d.]+)", line)
        items.append((last, float(sh.group(1)) if sh else None,
                      float(mn.group(1)) if mn else None, float(mn.group(2)) if mn else None))
        last = None

# de-duplicate (an item may fail more than once in a log), keep the worst share
worst = {}
for k, sh, mn, bar in items:
    if k not in worst or (sh or 0) > (worst[k] or 0):
        worst[k] = sh

accept, park, refused = [], {}, []
for k, sh in worst.items():
    park[k] = 1                     # one failed round today; the next failure parks it
    if sh is not None and sh > 1.0:
        refused.append((k, sh))
    else:
        accept.append(k)

os.makedirs(os.path.dirname(POLICY), exist_ok=True)
with open(POLICY, "w", encoding="utf-8") as fh:
    json.dump({"accept": sorted(accept), "park": park}, fh, ensure_ascii=False, indent=1)

print("failed items today:", len(worst))
print("ACCEPT (frame-share <= 1%%): %d" % len(accept))
for k in sorted(accept):
    print("   ", k, "  (worst share %.2f%%)" % worst[k])
print("REFUSED (frame-share > 1%%, keeps failing): %d" % len(refused))
for k, sh in refused:
    print("   ", k, "  (%.2f%%)" % sh)
print("park seeded at 1 round for all of them -> parked at PARK_AFTER=2")
print("wrote", POLICY)
