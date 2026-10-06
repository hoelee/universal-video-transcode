---
name: universal-video-transcode
description: Use when transcoding video for iPad+Android playback.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [ffmpeg, hevc, h264, mp4, ipad, ios, android, transcode, nvenc, cfr, frame-timing]
    related_skills: [tubesync-selfhosted, local-audio-to-text]
---

# Universal video transcode — anything in, one MP4 that plays on iPad AND Android

One tool: `scripts/transcode.py`. Hand it a **path**; a file converts, a folder converts
everything in it. It reads the source, picks the recipe itself, encodes, and verifies the
result before accepting it.

```sh
python scripts/transcode.py "D:/video/clip.mkv"
python scripts/transcode.py "D:/video" --recursive --dry-run   # plan only
python scripts/transcode.py "D:/video" --out D:/converted
```

## Scope — read this before applying the size policy

This policy is for **H.264 (and older MPEG-4/DivX) sources**, where the source is fat enough that
HEVC at matched quality lands 25-50% smaller (the course-library case: 1080p H.264 at
550-2400 kbps, mp3 audio, files 300 MB-1.3 GB).

**Do NOT run the size hunt on YouTube-sourced libraries (VP9/AV1).** YouTube already serves those
at a near-optimal bitrate for their quality, so re-encoding an already-lean VP9/AV1 file can only
grow it or lose quality - "the source is already as small as it gets, and that is accepted". The
job there is narrower and different:

| | H.264 library (this policy) | YouTube VP9/AV1 library |
|---|---|---|
| goal | same look, **smaller** file | same look, same size - no shrink sought |
| decode | any correct decoder | **never `*_cuvid`** (wrong frames, see below) |
| quality gate | SSIM >= 0.996 + size ceiling | **per-frame content check vs the software decode** |
| source for a repair | the file itself | **the pre-conversion snapshot backup**, never the already-converted file |
| frame cadence | `-fps_mode cfr` | `-fps_mode cfr` |

## What "playable everywhere" actually means

The intersection that both platforms decode **without a third-party player**:

| | iPad / iOS | Android |
|---|---|---|
| Container | MP4/MOV only — **no Matroska** | MP4 and MKV both fine |
| Video | H.264 (all), HEVC `hvc1` (A9+) | H.264 Baseline mandatory; HEVC 5.0+ |
| Audio | AAC — **never Opus** | AAC and Opus both fine |

**Apple is the constraint, not Android.** iOS has no Matroska demuxer and cannot play Opus
at all, so MP4 + AAC is what forces the conversion. Android would have accepted the `.mkv`.

Two facts that decide the video codec:

- **HEVC must be tagged `hvc1`.** ffmpeg defaults to `hev1`, which Apple refuses. `-tag:v hvc1`
  is not optional.
- **Android only guarantees HEVC *Main profile Level 3*** (~SD). 1080p output is Level 4.0
  and 4K is 5.0 — fine on modern phones, but a low-end/old device may only reach Level 3.
  When the audience includes cheap Android hardware, prefer H.264 (`--codec x264`), which is
  mandatory from Android 3.0 and universal on iOS.

Full matrix, per-platform citations, and the HDR/10-bit caveats: **`references/compatibility-matrix.md`**.

## The three recipes it picks automatically

Decided from the **source file**, never from its filename or extension:

| recipe | when | video | audio |
|---|---|---|---|
| `copy` | H.264/HEVC with a usable timeline | `-c copy` — **bit-identical** | copy if AAC |
| `fix-audio` | same, audio is not AAC | `-c copy` | AAC 256k |
| `encode` | VP9/AV1, or H.264/HEVC whose timeline is unusable | HEVC (or x264) | copy if AAC, else AAC 256k |

`copy` is used whenever a copy is possible, because a copy is lossless **by definition** —
that is the strongest possible answer to "keep the quality the same".

## Quality policy ("as close to the original as possible")

- **Never upscale.** The source resolution is preserved; there is no `scale` in the pipeline.
- **HEVC** targets `-cq 27`, measured on this hardware at **SSIM 0.9937** (visually transparent)
  while landing at **~source size**. `-cq 28-30` buys 14-32% smaller at SSIM still >=0.992.
- **H.264** (when chosen for reach) targets `-crf 20`.
- **Audio**: copied when already AAC (bit-identical); otherwise **AAC 256k**, where the
  residual error is the *source's own* lossy error — 320k buys nothing measurable.
- **Bitrate cap**: `-maxrate` = 1.5x the source bitrate, `-bufsize` = 2.5x that cap. Never set
  `bufsize` equal to `maxrate` (a 1-second VBV buffer starves the encoder and collapses quality
  on complex scenes); never cap at exactly the source bitrate (that pushes quality *below* the
  original — a second-generation loss).

## The two flags that must never be dropped

```sh
-fps_mode cfr      # without it the encoder silently drops 0.5-1.3% of frames,
                   # leaving doubled frame intervals that play as judder
-pix_fmt yuv420p   # 8-bit 4:2:0; 10-bit / 4:2:2 / 4:4:4 break device compatibility
```

`-fps_mode cfr` makes ffmpeg *duplicate* a frame it cannot place instead of dropping it.
Duplicated frames cost almost nothing; a gap is visible. The tool adds both automatically and
fails the item if the output comes back non-uniform.

## Verifying "no dropped frames" — and whether a browser is needed

**Short answer: the browser is the arbiter, but it is NOT the gate. Automate the causal
check; use the browser only to spot-check.**

The user-visible symptom (choppy playback) comes from a *deterministic* property of the
container: whether the frame timestamps are in **presentation** order or **decode** order.
That is readable from the header, so it needs no playback at all.

The defect this matters for: a B-frame stream whose MP4 composition-offset table is
unusable — either a single constant `ctts` entry (`PTS == DTS + const`) or **no `ctts` box
at all** (`PTS == DTS`). The decoder still reorders by POC, so the timestamps attached to the
output frames are wrong and the compositor drops **~23%** of them — while
`r_frame_rate == avg_frame_rate` stays **EXACTLY** equal, so every fps-based test calls the
file healthy.

| gate | cost | covers HEVC? | role |
|---|---|---|---|
| `r_frame_rate` vs `avg_frame_rate` | ~50 ms | yes | catches dropped/irregular cadence only |
| **`ctts` / PTS-inversion structural check** | ~50 ms | **yes** | **the automatable gate — causal** |
| full decode (`ffmpeg -f null -`) | minutes | yes | decode integrity |
| real Chrome `droppedVideoFrames` | ~30 s/file | **NO** | arbiter for H.264 spot-checks |

Three traps that make the browser check unsuitable as an automated gate:

1. **Chrome has no HEVC decoder.** `canPlayType('video/mp4; codecs="hvc1…"')` returns `''`, so an
   HEVC file reports **0 decoded / 0 dropped** and *looks perfect while nothing played*. A
   0-frame result is **not** a pass. HEVC can only be confirmed on the Apple device itself.
2. It needs a real Chrome and a real page; ~30 s per file is far too slow for a library.
3. The measurement is statistical (a 1-frame drop is noise), so it needs a threshold, not equality.

The tool therefore runs the structural gate on **every** file automatically, plus a full decode
pass, and refuses the output otherwise. Measurements, the exact thresholds, and the
2x2 experiment that proved causality: **`references/verification-and-automation.md`**.

## Running it unattended (cron)

Safe to schedule, because it is idempotent and self-locking:

- **A lock file** (default `~/.transcode.lock`) refuses to start a second instance. This is not
  optional hygiene: two concurrent NVENC jobs share one encoder unit, starve each other, and
  **drop frames** — the exact defect the tool exists to prevent.
- **Idempotent**: a file that is already a playable MP4 is skipped, so a re-run costs a probe
  and nothing else. `--dry-run` prints the plan without encoding.
- **A failed item leaves the input untouched**, so the next run simply retries it.
- **Disk guard**: aborts below 5 GB free rather than filling the work drive (an ENOSPC
  mid-encode is reported as a misleading codec error and costs every remaining item).

```sh
# hourly; exits non-zero if anything failed
python /path/to/scripts/transcode.py "Z:/docker/tubesync/download" --recursive \
  --out "Z:/docker/tubesync/converted" >> /var/log/transcode.log 2>&1
```

For a TubeSync library specifically, prefer its own DB-driven pipeline (`tubesync-selfhosted`)
— it also updates the database rows. Use this tool for anything outside that library.

## Whole-library runs on a network share (in-place, safe)

`transcode.py` writes `dst + ".part.mp4"` **next to the source**. Over SMB/WebDAV/Dokan that
is the failure mode to avoid, and when the source is already `.mp4` then `dst == src`, so the
only copy of the file is the one being written. For a library run, drive ffmpeg directly
from a small runner instead:

1. copy the source **off the share** to local disk (chunked; treat a short read as failure)
2. encode locally, with the same mandatory flags (`-fps_mode cfr`, `-pix_fmt yuv420p`)
3. run the structural gate **and** a full decode pass on the local output
4. upload to `<name>.new.mp4` (never the final name)
5. read the share copy back end-to-end and compare **SHA-256 + byte count** with the local file
6. only then `os.replace()` it over the original, and re-probe the result on the share

Step 5 costs one extra network read (~1 h per 330 GiB) and is the only thing that actually
proves the remote copy is complete — `getsize()` cannot, and neither can a duration probe:
with `+faststart` the `moov` is written first, so a truncated file still reports full duration.
Keep a JSONL ledger and a per-file skip test ("already HEVC hvc1") so a reboot resumes safely.
A single instance only; the lock matters even more here because two runs would race on `dst`.

### `plan_for()` gaps to know before a library run

- **`mpeg4` (DivX/Xvid) returns `None` = "unsupported video codec"** — it is skipped, not
  converted, so `.avi`/`mp4v` files silently keep a codec no iPad can play. Give them an
  explicit re-encode.
- **Repairing a defective H.264 timeline returns `x264`, not HEVC** (hardcoded in the H.264
  branch). If the requirement is "everything in HEVC", force it — the recipe is a policy, not
  the tool's default.
- A truncated file (`moov atom not found`) is a **re-download**, not a transcode job; report it
  instead of retrying.

### Measured throughput (RTX 3060, hevc_nvenc p6, `-tune hq`, cq 27)

Use these to quote an ETA before starting — the estimate is the user's decision input.

| source | speed |
|---|---|
| 1080p25 H.264 | **6.4x realtime** (~160 fps) |
| 480x270 | ~34x realtime |
| per-file overhead | ~45 s (local copy + decode pass + upload + read-back hash) |

A 520 h / 324 GiB library is therefore ~73 h of wall clock, not the ~40 h a bitrate-based
guess suggests. Encode order is a real choice: newest-first finishes the currently-watched
content in hours instead of days.

## "Same quality" is not the same cq number (measured)

`hevc_nvenc -cq 27` and `libx265 -crf 27` are **not the same quality**. Same number,
different scale: on one 1080p25 source (source video 1.20 Mbps), 8-minute window, video only:

| setting | Mbps | % of source video | SSIM |
|---|---|---|---|
| source H.264 (x264 2-pass) | 1.20 | 100% | ref |
| hevc_nvenc cq27 | 1.31 | **109%** | 0.9971 |
| hevc_nvenc cq30 | 0.91 | 76% | 0.9963 |
| libx265 crf24 | 0.83 | 69.5% | 0.9963 |
| hevc_nvenc cq32 | 0.69 | 58% | 0.9955 |
| libx265 crf27 | 0.58 | 48% | 0.9952 |
| hevc_nvenc cq34 | 0.54 | 45% | 0.9947 |

**Compare only at matched SSIM, never at matched cq/crf numbers.** Matched pairs here:
NVENC cq30 vs x265 crf24 (both 0.9963) -> x265 9% smaller; NVENC cq32 vs x265 crf27
(0.9955/0.9952) -> x265 16% smaller. So software is **9-16% more bit-efficient at equal
quality**, not the 2-2.5x that a raw cq27-vs-crf27 comparison implies - most of that gap is a
quality difference. Price: ~3x realtime (x265 fast, 1080p) vs 6.4x (NVENC).

**Why a library can grow instead of shrink.** On sources that are already efficiently
encoded H.264, NVENC cq27 is a *higher* quality target than the source itself, so the video
comes back at ~109% and the file grows. Quality must be pinned to a number, e.g. "SSIM >= 0.996
against the source", which is the only self-consistent definition when the source is already
lossy and no master exists to measure against. SSIM >= 0.996 lands the video near 70% of the
source bitrate - that is where the actual saving lives.

**Classify the source before choosing a setting.** bpp = bitrate / (w * h * fps) measures how
much fat is left, and it is readable in milliseconds:

| bpp | meaning |
|---|---|
| < 0.015 | already soft - re-encoding gains almost nothing, prefer `-c copy` |
| 0.015-0.025 | lean (typical streaming) |
| 0.025-0.040 | normal |
| > 0.040 | real headroom - worth a real re-encode |

One library measured 131 files < 0.025 against 243 files > 0.040 out of 572 - a single global
cq cannot be right for both. Calibrate per class (1-2 representatives, 60-120 s sample, sweep 2-3
cq values, keep the cheapest that hits the SSIM target); per-file calibration costs 1-2 min each.
A `-maxrate` of 1.0x the source bitrate is a cheap hard guarantee that output never grows.

**Audio must match the source, not a fixed 256k.** A weekly series shipping mp3 at 128 kbps was
re-encoded to `-b:a 256k`: that added ~128 kbps for no audible gain, ~+10% file size on a
1.3 Mbps file - enough to cancel the video saving on its own. AAC is more efficient than mp3, so
`-b:a` = the source's own audio bitrate (clamped, e.g. 96-256k) is already a quality upgrade.

## Pitfalls

- **Never write the encode output directly to a network share.** A streaming `.part` write over
  SMB/SFTP/Dokan leaves a corrupt stub when the connection blips. Encode to local disk, verify,
  then copy the complete file across. The tool writes `dst + ".part.mp4"` and renames only after
  verification — keep it on a local path if the destination is remote.
- **`os.path.getsize()` lies on RaiDrive/Dokan** — it returns cached metadata from the *previous*
  file handle. Verify a network copy by sequential chunked read, never by size.
- **A `.mkv` extension on a converted file is cosmetic.** TubeSync's rename pass rewrites
  finished `.mp4` files back to `.mkv` while `downloaded_container` still reads `mp4`, and
  `Content-Type` follows the container. Do not "fix" it and do not report it as a playback bug.
- **Do not re-download to fix a timing defect.** The mkv that yt-dlp writes is itself flat, so a
  re-download reproduces the defect exactly.
- **A `-c copy` remux cannot repair timing.** ffmpeg will not rewrite `stts`/`ctts` under stream
  copy, and retiming only the SPS (`-bsf:v h264_metadata=tick_rate=…`) changes nothing either.
  Repairing a copy-tier file means re-encoding its video — quote the size cost (1.28x at crf 23,
  1.74x at crf 20) before applying it to a whole library.
- **Unicode paths**: on Windows, drive ffmpeg from Python `subprocess` with an argv list. MSYS
  bash cannot hand a Chinese filename to native `ffmpeg.exe` even when `ls` shows it.
- **`-hwaccel cuda` alone may still software-decode VP9**; `-c:v vp9_cuvid` forces NVDEC.

## The owner's standing requirements — read this first

When the ask is "convert this library to HEVC, same look, same sound, playable on iPad+Android,
never bigger, no judder", the defaults here are **not** enough: a fixed `-cq 27` targets a quality
*above* an already-efficient H.264 source, so the library grows instead of shrinking.

| requirement | the answer |
|---|---|
| never bigger than the source | `-maxrate` = 1.0x the source **video** bitrate + a post-encode size assertion |
| same picture quality | pin it to a number: **SSIM >= 0.996** vs the source (video lands near 70% of the source bitrate) |
| same sound quality | copy AAC; for mp3/opus, AAC at **the source's own bitrate**, never a fixed 256k |
| iPad + Android | MP4 + `hvc1` + `yuv420p` + AAC |
| no judder | `-fps_mode cfr` on every re-encode, the three cadence defects gated, and **no `*_cuvid` decoders** |
| verification | mandatory, and it must assert on the output, not on the input |
| CPU encoding | allowed, but **tell the owner first** and quote the wall clock (~3x realtime vs 6.4x) |

Full policy, including the measured numbers, the source-classification table, the judder detector
table and every CPU/threading knob: **`references/hevc-conversion-policy.md`**.

## Support files

- `scripts/transcode.py` — the tool. `--dry-run`, `--codec`, `--cq/--crf`, `--audio`,
  `--channels`, `--cpu`, `--keep`, `--overwrite`, `--no-decode-check`, `--lock`.
- `references/compatibility-matrix.md` — per-platform codec/container facts with citations,
  the HEVC level caveat, and what breaks on each device.
- `references/verification-and-automation.md` — the defect classes, the structural gate, the
  browser arbiter and its blind spot, and the 2x2 causality proof.
- `references/hevc-conversion-policy.md` — the owner's standing requirements for a HEVC library job
  (size ceiling, SSIM-pinned quality, bpp classification, audio matched to the source, the three
  judder classes, the mandatory verification chain, and the CPU/throttling knobs, efficiency first).
