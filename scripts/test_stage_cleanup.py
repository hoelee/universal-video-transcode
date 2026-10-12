"""Regression for the engine's own stage cleanup (library_run.purge_item_artifacts / sweep_stage).

The engine shares the stage dir between lanes, so:
  * purge_item_artifacts may only delete files carrying THIS item's tag or THIS process's pid,
  * sweep_stage may only delete leftovers older than max_age_h, and must skip every file of a tag
    that still has a fresh `<tag>_out.mp4.err` progress file (that tag is encoding right now).

    python scripts/test_stage_cleanup.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import library_run as LR                                                        # noqa: E402

MINE, OTHER = "aa11bb22cc33", "998877665544"
root = tempfile.mkdtemp(prefix="stage_cleanup_")
OLD = 12 * 3600


def touch(name, age_s=0.0, size=1024):
    p = os.path.join(root, name)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


# purge: only MINE / this pid
touch("ssim_%s.log" % MINE)
touch("ssim_%d_%s_ref.yuv" % (os.getpid(), MINE))
touch("pair_a_%s.yuv" % MINE)
touch("vmaf_%s.json" % os.getpid())
touch("ssim_%s.log" % OTHER)                     # another lane's item -> keep
touch("pair_a_%s.yuv" % OTHER)                   # another lane's item -> keep
n = LR.purge_item_artifacts(MINE, workdir=root)
left = sorted(os.listdir(root))
purge_checks = {
    "purged_mine": n == 4,
    "other_lane_untouched": all(("ssim_%s.log" % OTHER) in left and ("pair_a_%s.yuv" % OTHER) in left
                                for _ in [0]),
}

# sweep: old leftovers go, a live tag (fresh .err) stays whole
root2 = tempfile.mkdtemp(prefix="stage_sweep_")
root = root2
touch("ssim_1_dead1_ref.yuv", OLD, 2048)
touch("dead99_src.mp4", OLD, 4096)
touch("ssim_2_live42_ref.yuv", OLD, 2048)       # same tag as the fresh .err below -> keep
touch("live42_src.mp4", OLD, 4096)
touch("live42_out.mp4.err", 30, 256)            # fresh -> tag live42 is encoding now
touch("ledger.jsonl", OLD, 8192)
st = LR.sweep_stage(workdir=root2, max_age_h=6)
left2 = sorted(os.listdir(root2))
sweep_checks = {
    "dead_leftovers_removed": "ssim_1_dead1_ref.yuv" not in left2 and "dead99_src.mp4" not in left2,
    "live_tag_kept_whole": "ssim_2_live42_ref.yuv" in left2 and "live42_src.mp4" in left2,
    "ledger_never_touched": "ledger.jsonl" in left2,
    "freed_bytes_reported": st["bytes"] == 2048 + 4096,
}

ok = True
for k, v in list(purge_checks.items()) + list(sweep_checks.items()):
    print("%-26s %s" % (k, "PASS" if v else "FAIL"))
    ok = ok and v
print("purge removed %d, sweep %s" % (n, st))
shutil.rmtree(tempfile.gettempdir() + "/" + os.path.basename(root), ignore_errors=True)
sys.exit(0 if ok else 1)
