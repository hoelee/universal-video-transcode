# Why this exists — the benefit, measured

Every item below is a failure this pipeline **actually hit**, not a hypothetical. The tool
exists because each one was either invisible to the checks in place, or caused by an
operation that looked safe.

## The headline case

One video in a 4,435-file library "played choppy" on an iPad. Every check said it was fine:

| check | result |
|---|---|
| `Content-Type`, `Accept-Ranges`, Range `206` | correct |
| `moov` before `mdat` (faststart) | correct |
| file downloaded and played locally | stutters **identically** — so not the network |
| decode integrity (`ffmpeg -f null -`) | exit 0 |
| decode speed | 57x realtime — *faster* than a clean 1080p control |
| `r_frame_rate` vs `avg_frame_rate` | **25.000 / 25.000 — exactly equal** |
| full library scan | **not flagged** |

And yet, measured in real Chrome: **23.0% of frames dropped**, while a clean control dropped
**0.0%**.

The cause was decidable from the header all along. The video track had B-frames, but its MP4
composition-offset table was a single constant entry — so `PTS == DTS + const`, and the frame
timestamps were in *decode* order rather than presentation order. The decoder still reorders
by POC, so the timestamps attached to the output frames are wrong, and the compositor throws
away about a quarter of them.

**`r == avg` exactly equal is the tell.** It is not a clean bill of health; it means the
timeline is uniform *and* flat, which is precisely the broken shape.

| | before | after `transcode.py` |
|---|---|---|
| dropped frames (real Chrome) | **23.0%** | **0.0%** |
| `ctts` entries | 1 | 29,468 |
| PTS inversions | 0 | 35 |

## What it prevents — and what each guard came from

| past failure | what happened | the guard |
|---|---|---|
| **27 invisible stutterers** | 1.2% of the library dropped ~23% of frames while scanning clean, because the fps test is blind to flat composition offsets | `unusable_timeline()` gates on the header — and tests **both** shapes: a single constant `ctts` entry *and* a missing `ctts` box |
| **A quarter of the defect shape missed** | a detector written as `ctts == 1` skipped files with **no** `ctts` box at all — measured 22.8–23.5% dropped, reading as healthy | three return states: entry count / `-1` (MP4, no ctts → flat) / `None` (not an MP4 sample table → **not** flat, or every `.mkv` gets flagged) |
| **A "lossless" remux that kept the stutter** | `-c copy` cannot retime: ffmpeg will not rewrite `stts`/`ctts` under stream copy, so a flat input produces a flat output | `plan_for()` refuses to copy a defective timeline and forces a real re-encode |
| **Re-download made it worse, not better** | the `.mkv` yt-dlp writes is *already* flat, so a fresh download reproduces the defect | the fix is an encode, never a re-download |
| **A video that played only its first 10 seconds** | a copy to a network share came up truncated; `os.path.getsize()` returned **stale metadata from the previous file handle**, so size-based verification passed | verify by reading the file back (duration + resolution via probe), never by size |
| **A 115-file batch lost to a full disk** | ENOSPC mid-encode left a truncated `.part.mp4`; every remaining item then died with a misleading codec error | abort below 5 GB free, before the encode, not after |
| **Two encoders starving each other** | two concurrent NVENC jobs share one encoder unit, drop frames, and *cause* the judder the pipeline exists to remove | a lock file refuses a second instance |
| **An iPad refused a perfect file** | ffmpeg defaults to `hev1`; Apple requires `hvc1` | `-tag:v hvc1` on every HEVC output, asserted in verify |
| **Opus copied into an MP4** | Opus is not valid in MP4 and iOS cannot play it; the file was written but unplayable | copy audio only when it is already AAC, else AAC 256k |
| **Judder from the encoder itself** | without `-fps_mode cfr`, NVENC silently drops 0.5–1.3% of frames, leaving doubled intervals | `-fps_mode cfr` on every re-encode, asserted in verify |

## Reproduce it

The detector is the whole claim, so it is tested against files built on the spot:

```sh
python scripts/selftest.py
```

```
fixture                            expect   got      verdict
src.mp4                            False    False    ok    healthy MP4, proper ctts
src.mkv                            False    False    ok    Matroska - no ctts by design
flat_noctts.mp4                    True     True     ok    MP4 with NO ctts box (PTS == DTS)
flat_ctts1.mp4                     True     True     ok    MP4 with a single constant ctts entry
```

`flat_ctts1.mp4` is made by folding a real 75-entry `ctts` table into one constant entry and
padding the difference with a `free` box, so the `moov` keeps its exact size — the same shape
YouTube/yt-dlp produced in the field. No fixture is checked in; both are generated per run.

## Why a browser is not the gate

The browser is the *arbiter* of the visible symptom, but it is the wrong tool for automation:

- **Chrome cannot decode HEVC.** An HEVC file reports **0 decoded / 0 dropped** and looks
  perfect while nothing played. A 0-frame result is not a pass.
- It needs a real Chrome and a real page — ~30 s per file, against ~50 ms for the header check.
- It is statistical: a 1-frame drop is noise, so it needs a threshold rather than equality.

The structural gate is **causal**, which is what licenses automating it. That was not assumed —
it was proven by crossing container timing against bitstream health:

|  | flat container | correct offsets |
|---|---|---|
| **healthy bitstream** | **23.5% dropped** (stutter) | 0.0% |
| **defective bitstream** | 22.8% dropped | **0.0%** (fixed) |

Writing the same flat container around a *healthy* bitstream reproduces the stutter; writing
proper offsets around the *broken* bitstream fixes it. The bitstream is irrelevant — the
container timing is the cause.

## Automate it

The tool is built to run unattended, so "did anyone remember to convert this?" stops being a
question:

```sh
# every 15 minutes; the lock makes overlap impossible, so a long encode is never doubled
*/15 * * * * /usr/bin/python3 /opt/universal-video-transcode/scripts/transcode.py \
  /srv/media/incoming --recursive --out /srv/media/ready \
  >> /var/log/transcode.log 2>&1
```

```bat
:: Windows Task Scheduler, every 15 minutes
python D:\dev\universal-video-transcode\scripts\transcode.py "Z:\media\incoming" ^
  --recursive --out "D:\media\ready" >> D:\logs\transcode.log 2>&1
```

Why this is safe to schedule:

- **idempotent** — an already-playable file is skipped, so a re-run costs one probe
- **self-locking** — overlap is refused, which also removes the two-encoders-drop-frames failure
- **failure-isolated** — a failed item leaves its input untouched and is retried next run
- **non-zero exit** when anything failed, so the scheduler's own alerting works
- **disk-guarded** — refuses to fill the work drive

Before pointing it at a live library, `--dry-run` prints the plan and encodes nothing.

> **TubeSync specifically:** use that project's own DB-driven pipeline
> (`hoelee-tubesync-management`: `tsconv.py` / `tsfix.py`) — it also updates the database
> rows. This tool is for everything outside it.
