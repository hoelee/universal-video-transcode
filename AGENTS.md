# AGENTS.md — entry point for any AI session touching this repo

**This repo is the canonical source of the `universal-video-transcode` Hermes skill.**
Read this file first, then `README.md`, then the specific reference you need.

Owner: Lee Teong Hoe (Mr Hoelee) · me@hoelee.com · WhatsApp +60 12-797 2969

---

## 1. What this is

One tool that turns **any** video/audio file into an MP4 that plays on **both** iPad and
Android, at the best quality the target codec allows.

| layer | what | where |
|---|---|---|
| the tool | `scripts/transcode.py` — path in, verified MP4 out | this repo |
| the gate | `scripts/selftest.py` — builds both defect shapes and asserts the detector | this repo |
| the skill | `SKILL.md` + `references/` — loads into Hermes | copied to the skill dir (§4) |

There are **no credentials** in this project, so there is no `SECRETS.md`.

## 2. The non-negotiables (read before you change anything)

1. **`-fps_mode cfr` on every video re-encode.** Without it the encoder silently drops
   0.5–1.3% of frames, leaving doubled frame intervals that play as judder. `verify()`
   fails an item whose output comes back non-uniform — never remove that check.
2. **`-tag:v hvc1` on every HEVC output**, and `-pix_fmt yuv420p`. Apple refuses `hev1`
   (ffmpeg's default) and non-8-bit formats lose device compatibility.
3. **Never copy a video whose timeline is defective.** `-c copy` cannot retime — ffmpeg will
   not rewrite `stts`/`ctts` under stream copy — so a flat input survives into the output.
   `plan_for()` decides this; do not "simplify" it into an unconditional copy.
4. **One encoder at a time.** The lock file is not optional hygiene: two concurrent NVENC
   jobs share one encoder unit, starve each other, and drop frames.
5. **Run `python scripts/selftest.py` after touching the detector.** It is the only thing
   standing between this tool and shipping files that stutter.
6. **Never write encode output straight to a network share.** Encode to local disk, verify,
   then copy the complete file across; a streaming `.part` write leaves a corrupt stub on any
   blip. If the destination is remote, keep the output local.

## 3. Layout

```
SKILL.md                                   the Hermes skill (installed per §4)
README.md                                  human-facing overview
docs/WHY.md                                the benefit case: ten real failures + the guard each produced
scripts/transcode.py                       the tool: plan -> encode -> verify -> keep input on failure
scripts/selftest.py                        proves the detector; both defect shapes, no false positives
references/compatibility-matrix.md         per-platform codec/container facts with citations
references/verification-and-automation.md  the defect classes, the structural gate, the 2x2 proof
```

## 4. Installing the skill

```sh
cp -r . "$LOCALAPPDATA/hermes/skills/media/universal-video-transcode"
```

Keep this repo the source of truth and copy **to** the skill dir, not the reverse — edits
made only in the skill dir are lost the next time it is reinstalled.

## 5. What "verified" means here

An output is accepted only if all of these hold, each read back from the file:

duration within 1% · resolution identical · expected video codec · `hvc1` tag on HEVC ·
audio is AAC · 8-bit `yuv420p` · uniform cadence · **no flat composition offsets** ·
full decode exits 0 · read-back after the write matches.

Do not claim a fix without running the arbiter. `measure_playback.py` in the
`tubesync-selfhosted` skill plays a file in real Chrome and reports
`getVideoPlaybackQuality().droppedVideoFrames` — but note **Chrome cannot decode HEVC**, so
an HEVC file reports 0 decoded / 0 dropped and that is *not* a pass. Judge HEVC by the
structural gate and confirm on the Apple device.

## 6. Related work

- **`hoelee-tubesync-management`** — the TubeSync archive this tool was extracted from. It
  owns the DB-driven pipeline (`tsconv.py` / `tsfix.py`) and the deeper frame-timing
  forensics. `tsconv.flat_timing()` is that repo's copy of the detector: **keep the two in
  step**, and note that the missing-`ctts` shape was once missed by three duplicated copies
  of exactly this logic. One implementation per repo.
- `references/verification-and-automation.md` carries the measured 2x2 that proved the
  defect is container timing and not the bitstream. If you are about to argue that a
  bitstream property is the cause, read that first.
