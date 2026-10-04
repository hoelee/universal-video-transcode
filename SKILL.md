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

## Support files

- `scripts/transcode.py` — the tool. `--dry-run`, `--codec`, `--cq/--crf`, `--audio`,
  `--channels`, `--cpu`, `--keep`, `--overwrite`, `--no-decode-check`, `--lock`.
- `references/compatibility-matrix.md` — per-platform codec/container facts with citations,
  the HEVC level caveat, and what breaks on each device.
- `references/verification-and-automation.md` — the defect classes, the structural gate, the
  browser arbiter and its blind spot, and the 2x2 causality proof.
