# Verifying "it plays smoothly" — the gate, the arbiter, and what is automatable

## The question this answers

"Is there a dropped frame?" and "do I have to play it in a browser to know?" — no. The
browser is the *arbiter* of the user-visible symptom; it is not the *gate*. Automate the
causal check, spot-check with the browser.

The symptom comes from a deterministic property of the container, readable from the header:
whether the frame timestamps are in **presentation** order or **decode** order.

## The defect classes

Three distinct causes produce the same complaint ("一卡一卡 / choppy"), and a file can have
any combination. Only the first is visible to an fps test.

| # | class | signature | fps test sees it? |
|---|---|---|---|
| 1 | non-uniform cadence | `r_frame_rate` != `avg_frame_rate` (gap > 0.01) | **yes** |
| 2 | encoder dropped frames | doubled intervals (0.040 and 0.080 where 0.040 belongs) | yes (avg drops) |
| 3 | **unusable composition offsets** | `r == avg` **exactly** | **NO** |

Class 3 is the one that hides. Two shapes, both the same defect:

- MP4 with a **single constant `ctts` entry** → `PTS == DTS + const`
- MP4 with **no `ctts` box at all** → `PTS == DTS`

Either way the timestamps are in decode order. Decoders still reorder by POC, so the
timestamps attached to the *output* frames are wrong, and the compositor drops ~23% of them.

> **A missing `ctts` is only a defect on an MP4 whose video stream reorders.** Matroska stores
> presentation timestamps per block and legitimately has no `ctts` box — so a detector that
> treats "no ctts" as flat will flag every `.mkv`. Distinguish "no MP4 sample table" from
> "MP4 with no ctts box", and use the packet-PTS test for Matroska.

## The gate (automatable, runs on every file)

Both checks are header-only (~50 ms) and **work on HEVC**, unlike the browser.

**MP4 — count the `ctts` entries.** Walk the `moov` from the first 2 MB (the `moov` is at the
front after `+faststart`). `<= 1` entry on a stream with `has_b_frames > 0` is the defect.
Healthy files in this library carry **1,500–43,000** entries.

**Any container — count PTS inversions.** A stream with B-frames *must* present frames out of
decode order, so its container PTS go backwards somewhere:

```sh
ffprobe -v error -select_streams v:0 -read_intervals "%+3" \
  -show_entries packet=pts -of csv=p=0 FILE
```

Count steps where `pts[i+1] < pts[i]`. **0 inversions = flat/broken**; 20–45 = healthy.
This is the only option for Matroska, which has no `ctts`.

Measured separation, no overlap:

| file | ctts entries | PTS inversions | Chrome dropped |
|---|---|---|---|
| defective | **1** | **0** | **22.8–27.3%** |
| defective (no ctts box) | **absent** | **0** | **22.8–23.5%** |
| same file, x264 CFR re-encode | 985 | 35 | **0.0%** |
| known-good 1080p | 1549 | 23 | 0.0% |
| known-good 360p | 4262 | 35 | 0.2% |
| our HEVC 2160p output | many | 44 | (not measurable in Chrome) |

A file that is *already* a playable MP4 must also be checked before being skipped, or a
"nothing to do" verdict is issued for a file that stutters.

## The arbiter (real Chrome) and its blind spot

```sh
python measure_playback.py <url> [seconds]     # add a second url to A/B in one run
```

Plays muted in real headless Chrome over CDP and reports `getVideoPlaybackQuality()` —
decoded frames, `droppedVideoFrames`, and the media-time/wall-clock ratio.

- **The ratio is not the verdict.** A defective file advances at ratio **1.000** while dropping
  20–23% of frames. Only the drop count separates healthy from broken.
- **Threshold, not equality.** A 1–2 frame drop at startup is noise (healthy files measure
  0.0–0.2%); a real defect measures ~20%+. Treat `< 1%` as smooth.
- **Chrome cannot decode HEVC.** `canPlayType('video/mp4; codecs="hvc1…"')` returns `''`, so an
  HEVC file reports **0 decoded / 0 dropped** and looks perfect while nothing played. A 0-frame
  result is **not a pass** — it means "not measurable here". Judge HEVC by the structural gate
  and confirm on the Apple device.

So the browser is: too slow (~30 s/file), unable to judge the main output codec, and
statistical. It is the right tool for an H.264 A/B spot-check, not for a library gate.

## Why this is causal, not a heuristic — the 2x2

Correlation would not be enough (a defect could be a symptom of something else). The 2x2
crosses the container timing against the bitstream:

|  | flat container | correct offsets |
|---|---|---|
| **healthy bitstream** | **23.5% dropped** (stutter) | 0.0% |
| **defective bitstream** | 22.8% dropped | **0.0%** (fixed) |

Writing the same flat container around a *healthy* bitstream reproduces the stutter, and
writing proper offsets around the *broken* bitstream fixes it. The bitstream is irrelevant —
**the container timing is the cause**. That is what licenses a header-only gate.

## What cannot fix it

- `-c copy` cannot retime. ffmpeg will not rewrite `stts`/`ctts` under stream copy; output
  timing stays byte-identical and the drop count is unchanged.
- Repacking the raw H.264 (`-fflags +genpts -r 25 -c copy`) stays flat — measured.
- `-bsf:v h264_metadata=tick_rate=…` retimes only the SPS; timestamps unchanged.
- Re-downloading reproduces it: the `.mkv` yt-dlp writes is itself flat.

Only a real video re-encode regenerates the timeline. Cost on a copy-tier file: 1.28x at
crf 23, 1.74x at crf 20, 2.17x at crf 18.

## Provenance: inherited, not introduced by the remux

The defect arrives with the download. Some `.mkv` files yt-dlp writes are already flat (PTS
monotonic in file order, `dts = pts - 40`), and `-c copy` carries that through unchanged. An
mp4→mkv→mp4 round trip does not repair it.

Measured on one channel folder: **8 of 8** `360p-avc1` source `.mkv` files were flat, and the
same key was flat in both its `.mkv` and its converted `.mp4`. It is source-specific, not
library-wide: ~1.3% of AVC1 files, and **0 of 60** in the re-encoded HEVC tier (a re-encode
regenerates timing, so the HEVC path is structurally immune).
