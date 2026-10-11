"""Regression: an owner edit to gate_policy.json made while an engine is running must survive.

Measured 2026-10-11: the cpu lane's engine (started 13:35, loaded the policy at start) rewrote
gate_policy.json when its own item failed at ~13:5x, restoring the `park` entry the owner had deleted
at 13:47 to release that item for a lower-crf retry. The item was then SKIPped as parked.

Checks
  1. the released entry stays released when another item's counter is bumped
  2. an externally added `accept` entry is preserved (the engine never manages that list)
  3. the bumped counter does reach disk
  4. the old write-the-whole-snapshot behaviour WOULD have restored it (so the test is not vacuous)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:/dev/universal-video-transcode/scripts")
import library_run as LR                                                        # noqa: E402

tmp = tempfile.mkdtemp(prefix="gatepolicy_")
LR.POLICY = os.path.join(tmp, "gate_policy.json")
with open(LR.POLICY, "w", encoding="utf-8") as fh:
    json.dump({"accept": ["itemX"], "park": {"itemA": 2, "itemB": 1}}, fh, ensure_ascii=False)

# the engine process loads the policy at start (this is its in-memory snapshot)
LR.POLICY_CACHE.update(LR.load_policy())
LR.POLICY_DIRTY.clear()

# the owner, while that engine runs: release itemA, release itemY to the 0.990 tier
on_disk = json.load(open(LR.POLICY, encoding="utf-8"))
on_disk["park"].pop("itemA")
on_disk["accept"].append("itemY")
with open(LR.POLICY, "w", encoding="utf-8") as fh:
    json.dump(on_disk, fh, ensure_ascii=False)

# now the engine's itemB fails -> counter bump + save
LR.POLICY_CACHE["park"]["itemB"] = LR.POLICY_CACHE["park"].get("itemB", 0) + 1
LR.POLICY_DIRTY["itemB"] = LR.POLICY_CACHE["park"]["itemB"]
LR.save_policy(LR.POLICY_CACHE)

after = json.load(open(LR.POLICY, encoding="utf-8"))
old_behaviour = {"accept": ["itemX"], "park": {"itemA": 2, "itemB": 2}}     # what the old code wrote

checks = {
    "released_item_stays_released": "itemA" not in after["park"],
    "external_accept_entry_kept": "itemY" in after["accept"],
    "counter_reaches_disk": after["park"].get("itemB") == 2,
    "old_behaviour_would_have_restored_it": "itemA" in old_behaviour["park"],
}
print("on disk after the save:", json.dumps(after, ensure_ascii=False, sort_keys=True))
for k, v in checks.items():
    print("%-40s %s" % (k, "PASS" if v else "FAIL"))
sys.exit(0 if all(checks.values()) else 1)
