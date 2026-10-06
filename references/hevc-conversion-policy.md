# HEVC conversion policy — playable everywhere first, shrink where there is headroom, no judder

Owner's standing requirements for a HEVC conversion. Read this before starting a library job;
the default settings in `transcode.py` do **not** satisfy them. Note the title: the size rule is
**conditional** (section 1), not a blanket "never grow".

## 1. Size: shrink where there is headroom, growth is acceptable where there is not

**Playability on iPad + Android is the primary goal. Size is secondary and conditional** — it is
NOT a universal "must never grow" rule. Two regimes, and they need opposite handling:

| regime | detector | expectation | `-maxrate` | output assertion |
|---|---|---|---|---|
| **headroom** — an ordinary H.264 encode that is not yet at the transparency floor | bpp > ~0.030, or a sampled pre-flight encode needs **fewer** bits than the source | output **should be smaller**. If it grows, that is a **recipe bug**, not an acceptable outcome | 1.0x the source **video** bitrate | `out <= src` (hard) |
| **already compressed** — YouTube / streaming downloads, near-optimal 2-pass sources | bpp < ~0.030, or a sampled pre-flight encode needs **>=** the source bits | growth is **acceptable**. The file already sits at the transparency floor; only playability matters | 1.5x the source video bitrate | `out <= 1.25x src` (runaway-bloat guard only) |

The authority is the **sampled pre-flight** (section 2): encode a 60-120 s window at the target
quality and compare bits. bpp is just the cheap prior that tells you which way you expect it to go.

**Do not clamp to 1.0x on an already-compressed source.** Capping at the source bitrate forces the
encoder *below* the transparency point, which is a visible quality loss **on top of** the
second-generation loss — strictly worse than accepting a slightly larger file. A hard cap is only
correct in the headroom regime, where shrinking is the actual goal.

Why an already-compressed source can legitimately grow: HEVC's ~40% efficiency advantage is
measured against H.264 at *equal encoding effort*. A near-optimal 2-pass AVC stream is already
closer to the floor than a single-pass hardware HEVC encode is, so at matched perceived quality
there may simply be no bits left to win.

### An H.264 source with headroom is expected to come out smaller

When the source is ordinary H.264 with real headroom and the output still grows, the recipe is
wrong, not the source. The two measured causes, both in this history:

- a quality target **above the source** (`-cq 27` on NVENC = SSIM 0.9971 while the source is
  already at ~0.9963-equivalent), landing the video at 109% of the source bitrate;
- audio re-encoded to a **fixed 256k** when the source ships mp3 at 128k: ~+10% for no audible gain.

### HEVC is not what makes a file playable

H.264 + AAC in MP4 **already plays on both iPad and Android** — no conversion needed. HEVC is only
*required* for sources that are not already in that shape: Matroska, Opus/Vorbis audio, DivX/Xvid,
MPEG-TS, or a broken frame cadence. For an already-playable, already-lean H.264 file the only
benefit is size; where there is no headroom a re-encode buys nothing and costs a generation, so
`-c copy` (or leaving it alone) is the better answer. Say this to the owner before spending days of
GPU/CPU on files that are already fine.

The two derived values every rule below needs, from one ffprobe of the source:

```python
src_video_bps  = format_bit_rate - audio_stream_bit_rate   # NOT the file's total bitrate
src_audio_kbps = max(96, min(256, audio_stream_bit_rate // 1000))
```

Comparing against the file's **total** bitrate instead of the video's is the specific mistake to
avoid: it turned a real 109% growth into a false 86% shrink in one analysis.

## 2. Same picture quality — pin it to a number

"Same quality" is meaningless unless it is measurable, and with an already-lossy source and no
master, the only self-consistent definition is **SSIM against the source**.

- Target **SSIM >= 0.996** on a sampled window. Measured on 1080p25 H.264: this lands the video
  near **70% of the source bitrate** — that is where the saving is. `cq 27` on NVENC gives 0.9971
  and *grows*; `cq 30` gives 0.9963 at 76%; `cq 32` gives 0.9955 at 58%.
- **Never compare cq/crf numbers across encoders.** `-cq 27` (NVENC) = SSIM 0.9971 while
  `-crf 27` (x265) = 0.9952. Match on SSIM, then compare bitrate.
- Software x265 is **9-16% more bit-efficient at matched SSIM**, not 2x — most of the raw
  difference between cq27 and crf27 is a quality difference, not an efficiency difference.
- Verify the claim per file (or per class): encode a 60-120 s window, measure SSIM vs the same
  source window, and fail the item if it is under target.

## 3. Classify the source before choosing a setting

`bpp = video_bitrate / (w * h * fps)`, readable in milliseconds, tells you how much fat is left:

| bpp | verdict |
|---|---|
| < 0.015 | already soft — re-encoding gains nothing, use `-c copy` |
| 0.015-0.025 | lean |
| 0.025-0.040 | normal |
| > 0.040 | real headroom — worth a real re-encode |

One library: 131 files < 0.025 vs 243 files > 0.040 out of 572. A single global quality setting
cannot be right for both — that is precisely how a job ends up growing some files and shrinking
others. Calibrate per class on 1-2 representatives (60-120 s, sweep 2-3 quality levels).

bpp and the size regime are **different axes** and can disagree: bpp picks the *recipe* (how much
effort is worth spending), while the pre-flight sample in section 2 picks the *size expectation*
(hard assertion or not). When they disagree the pre-flight wins — it is measured on the actual file.

## 4. Same sound quality

- Source audio already **AAC** -> `-c:a copy`, bit-identical. Always preferable.
- Source audio **mp3/opus/vorbis** -> must be re-encoded; those are not valid in MP4.
- **`-b:a` must track the source, never a fixed 256k.** A weekly series shipping mp3 at 128 kbps
  re-encoded to 256k AAC added ~128 kbps for zero audible gain: ~+10% file size, enough to cancel
  the video saving by itself. AAC is more efficient than mp3, so **matching** the source bitrate is
  already an upgrade. Clamp to a sane floor (96k).

## 5. Playable on iPad AND Android

MP4 + `-tag:v hvc1` (Apple rejects `hev1`) + `-pix_fmt yuv420p` + AAC. No Matroska, never Opus.
HEVC 1080p is Main profile Level 4.0 — every modern iPhone/iPad (A9+) and Android 5+ handles it,
but a cheap/old Android device is only guaranteed Main Level 3 (~SD).

## 6. No judder — three defect classes, same symptom

`-fps_mode cfr` is mandatory on every re-encode: with the default `auto`, ffmpeg **drops** frames
it cannot place (0.5-1.3% measured), leaving doubled frame intervals that play as judder. CFR
duplicates a frame instead, which costs almost nothing. Never pass `-r` unless you want a
different playing speed.

| class | signature | detector | fix |
|---|---|---|---|
| source-inherited VFR | `r_frame_rate != avg_frame_rate` | ffprobe header, gap > 0.01 | re-encode (remux cannot repair) |
| encoder-dropped | doubled intervals (0.040 + 0.080) | histogram `packet=duration_time` shows 2+ values | `-fps_mode cfr` |
| **flat composition offsets** | `r == avg` **exactly**, uniform `stts`, clean decode, still drops ~23% | `ctts` entry count <= 1 on a B-frame stream, or 0 PTS inversions over 60 s | real re-encode; **no lossless repair exists** |

The third class is the trap: every cadence test passes and the file still stutters on real
hardware. Gate it explicitly (see `scripts/transcode.py`: `_ctts_entries()`, `pts_inversions()`).

**Never decode with a `*_cuvid` hardware decoder.** Measured on this hardware (RTX 3060,
ffmpeg 9.0.1): `av1_cuvid` emitted **the wrong picture on 1.8% of frames** (a frame from a
different moment, not a corrupt one). The container stayed perfect — exact intervals, correct
`ctts`, clean decode — so every structural check passed while the output visibly jumped. Use
software decode (4K AV1 ran ~150 fps on the i9-13900K) or the modern
`-hwaccel cuda -hwaccel_output_format cuda` path, and if you ever must use cuvid, compare its
frames against the software decode with per-frame SSIM (>=0.99 everywhere).

**Judge the picture, not the container, when the complaint is "it jumps".**

## 7. Verification — yes, mandatory, and it must assert on the output

Per item, in this order:

1. **Local staging.** Fetch the source off the share first; never encode over SMB/WebDAV/Dokan.
2. **Structural gate** on the encode: codec/tag/duration/resolution/pixel format, `r` vs `avg`
   within 0.01, `ctts`/PTS inversion check, one distinct frame duration.
3. **Full decode pass** (`ffmpeg -v error -i OUT -f null -`) — the only check that proves the
   payload is not truncated or corrupt.
4. **SSIM vs the source window >= target** (section 2).
5. **Size assertion**, per the regime in section 1: hard `out <= src` where the source has
   headroom; runaway guard `out <= 1.25x src` where the pre-flight says it is already compressed.
6. **Upload to a temp name, read the share copy back end-to-end, compare SHA-256 + byte count**,
   then replace atomically. A duration probe is not enough: with `+faststart` the `moov` is written
   first, so a truncated upload still reports the full duration.
7. **Re-probe on the share** after the replace.

Cheap confidence check on a healthy file, one distinct value expected:

```sh
ffprobe -v error -select_streams v:0 -read_intervals "%+60" \
  -show_entries packet=duration_time -of csv=p=0 FILE | sort -u | wc -l
```

**Do not hand-sample videos before converting** — nothing here is spec-dependent, so the guarantee
belongs in the output assertion. Chrome cannot arbitrate HEVC either: `canPlayType('...hvc1...')`
returns `''`, so an HEVC file reports 0 decoded / 0 dropped and looks perfect while nothing played.
HEVC smoothness is confirmable only on the Apple device, via the frame-duration histogram, or on
an Android player. For an owner-facing A/B, serve the original and the variant side by side in a
real browser and let them compare (the H.264-only variant is the one Chrome can measure).

## 8. CPU (software) encoding — every knob, and what each one costs

**Remind the owner before starting a CPU job and quote the wall-clock time.** Software x265 runs
3.5-4.4x realtime at 1080p depending on cores and preset (vs 6.4x for `hevc_nvenc`), so a 520 h
library is days of CPU. Say so before starting, not after — see the measured table in the topology
section and the tier timings in section 9.

Efficiency first, and the knobs do different jobs:

| goal | knob | effect |
|---|---|---|
| better bits-per-quality | `-preset medium|slow|slower` | **the only lever that buys efficiency**; each step ~5-8% smaller at equal quality, ~1.5-2x slower |
| fewer cores | `-x265-params pools=N` (N threads) — for `libx265`, ffmpeg's `-threads N` maps to frame threads | fewer cores, roughly linear slowdown, **no efficiency gain** |
| fewer frame threads | `-x265-params frame-threads=2` | less parallelism, marginal efficiency *gain*, slower |
| CPU affinity | `start /affinity <hexmask> ffmpeg ...`, or PowerShell `(Get-Process ffmpeg).ProcessorAffinity` | pins to chosen cores; keeps the machine responsive |
| low priority | PowerShell `(Get-Process ffmpeg).PriorityClass='BelowNormal'` | yields to everything else at no efficiency cost |
| **no high frequency** | `powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 60` then `powercfg /setactive SCHEME_CURRENT` | caps clocks: best perf/watt, quietest, slowest — match `pools` to the capped cores |

**Preset buys efficiency; thread count does not.** If the priority is efficiency rather than speed,
prefer a slower preset at a lower core count over a faster preset with all cores. Never run two
encoders of the same kind at once: they compete for the same unit and drop frames — the exact
defect the gates exist for. One CPU encoder running *alongside* one GPU encoder is fine (different
units), see section 9.

### This host's measured topology and the P-core-avoidance recipe

Windows reports the i9-13900K VM as "13 cores / 26 logical" — a misreport of a heterogeneous
topology. A per-logical-CPU microbenchmark settles it in 30 seconds:

| logical CPU | relative single-thread throughput |
|---|---|
| **0-15** (8 P-cores x HT) | 0.43-0.52 s for a fixed workload |
| **16-25** (10 E-cores) | 0.79-0.91 s — **56% of a P-core** |

Pin to the E-cores only (best perf/watt, keeps the P-cores free for the desktop):

```sh
# affinity mask = bits 16-25 set = 0x3FF0000 (= 67,043,328)
start /affinity 3FF0000 ffmpeg ...          # cmd
```
or set the mask on the driving Python process (children inherit it):

```python
ctypes.windll.kernel32.SetProcessAffinityMask(
    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.c_size_t(0x3FF0000))
```

Match the thread count to the mask (`-x265-params pools=10`). **`pools` does not choose cores —
the affinity mask does**; `pools` only caps threads.

Measured cost of E-core-only encoding (x265 preset medium, 1080p25, 120 s sample):

| cores | speed | output |
|---|---|---|
| all 26 threads | 4.37x realtime | 0.76 Mbps |
| **E-cores only (10 threads)** | **3.51x realtime** | 0.76 Mbps |

Only **20% slower**, not the ~3x that per-core throughput suggests: the 10 E-cores are real cores
while half of the P-core threads are SMT siblings, and x265 does not scale linearly anyway. So
E-cores-only is the right default when the priority is efficiency over speed.

## 11. Measured: what H.264 -> HEVC actually saves (3 real sources, 2026-10-07)

A 60 s window from the middle of three real library files, video-only, SSIM vs the source at
480x270. **Read this before promising a saving**: it ranges from 16% to 66% on the same settings,
and the setting that a fixed `-cq 27` implies does not exist.

| source | src video | cq27 | cq30 | cq31 | x265 crf24 | **x265 crf26** | x265 crf28 |
|---|---|---|---|---|---|---|---|
| 1080p30 music, bpp 0.0437 | 2720 kbps | 72.4% | 51.0% | 43.5% | 43.0% | **33.7%** | 26.4% |
| 720p30, bpp 0.0437 | 1208 kbps | **115.2%** | 82.6% | 72.9% | 71.4% | **56.7%** | 45.0% |
| 1080x1920@30 (vertical), bpp 0.0722 | 4490 kbps | **119.3%** | 83.8% | 74.3% | 84.3% | **67.0%** | 53.2% |

(SSIM min/mean at those settings: cq27 0.993-0.996; cq30 0.990-0.995; cq31 0.989-0.994;
crf24 0.990-0.993; crf26 0.988-0.993; crf28 0.985-0.991.)

Three findings that change the recipe:

1. **`-cq 27` GROWS two of the three files (115%, 119%).** That is the same defect that made a
   whole library land at 111%: a fixed cq is a quality target, and on an already-efficient H.264
   source it targets a quality *above* the source. Start at **cq 31 / crf 26**, not 27.
2. **"Same quality" and "smaller" cannot both be pushed to the limit.** Pinning min SSIM >= 0.996
   (the earlier target) leaves exactly one option on these files - cq27 - which is *bigger* than
   the source. The usable frontier is min SSIM ~0.99: **x265 crf26 at 34-67% of the source**, i.e.
   33-66% smaller. Put that choice to the owner explicitly instead of quietly dropping quality.
3. **x265 beats NVENC by 10-22% at MATCHED SSIM** (720p: cq31 72.9% vs crf26 56.7% at min SSIM
   0.9894/0.9879; 1080p: 43.5% vs 33.7% at 0.9938/0.9927) - consistent with the earlier 9-16%
   estimate. So: NVENC for the mid tier (speed), x265 for the fat tier (size). Note the cost is real:
   on these files x265 ran ~1.9-3x realtime vs 5x+ for hevc_nvenc.

Defaults that follow from this, and the gates that enforce them, are in `scripts/library_run.py`:
`CQ_NVENC=31`, `CRF_X265=26`, `SSIM_TARGET=0.985` (per-frame min), `SIZE_CEIL=1.0` (the output's
video bitrate may not exceed the source's), a one-step quality retry when the SSIM gate fails, and
`-maxrate` = 1.0x the source's **video** bitrate.

## 9. CPU and GPU tiers in parallel

Different tiers can encode **at the same time** — NVENC is a fixed-function ASIC, x265 is CPU.
Pair an E-core-pinned CPU job with a GPU job and the P-cores stay free for the user. Wall clock
becomes `max(cpu_tier, gpu_tier)` instead of the sum:

| tier | files | GiB | content h | recipe | time |
|---|---|---|---|---|---|
| fat (bpp > 0.040) | 252 | 153 | 219 | x265 crf24, E-cores | 65 h |
| mid (0.025-0.040) | 198 | 117 | 183 | hevc_nvenc cq30 | 31 h |
| lean (< 0.025) | 131 | 54 | 118 | `-c copy` + audio match | <1 h |

serial 96 h (4.0 days) -> parallel **66 h (2.7 days)**. Requirements to run it safely:

- **a lock per tier**, not one global lock, and a separate local staging dir per instance;
- the share is the shared resource: two instances double the WebDAV/Dokan traffic (staging read +
  upload + read-back hash). Test a small batch in parallel before committing the library;
- run the fast `-c copy` tier on its own first, so it is not contending while the two big jobs settle;
- `-c copy` only where the timing gate passes — a file in the flat-ctts class must be re-encoded
  whatever its bpp says.

## 10. The three recipes, ready to run

`SRC` = the local staged source, `SRC_V` = `src_video_bps` from section 1, `A` = `src_audio_kbps`
(section 4). Affinitize the process to the E-cores (`0x3FF0000`) before the tier-C command.
`-maxrate SRC_V` below is the **headroom** setting; use `1.5x SRC_V` when the pre-flight says the
source is already compressed and growth is acceptable (section 1).

```sh
# ---- tier A: lean (bpp < 0.025) — no video re-encode at all, size cannot change
ffmpeg -y -i SRC -map 0:v:0 -map 0:a:0? -map_metadata 0 \
  -c:v copy -c:a copy -movflags +faststart OUT.mp4     # -c:a aac -b:a A when the source is mp3

# ---- tier B: mid (0.025-0.040) — GPU, quality pinned to SSIM >= 0.996
ffmpeg -y -i SRC -map 0:v:0 -map 0:a:0? -map_metadata 0 \
  -c:v hevc_nvenc -preset p6 -tune hq -rc vbr -cq 30 -b:v 0 \
  -maxrate SRC_V -bufsize <2.5x SRC_V> \
  -profile:v main -pix_fmt yuv420p -tag:v hvc1 -fps_mode cfr \
  -c:a copy -movflags +faststart OUT.mp4               # -c:a aac -b:a A when the source is mp3

# ---- tier C: fat (bpp > 0.040) — CPU, E-cores only, efficiency first
ffmpeg -y -i SRC -map 0:v:0 -map 0:a:0? -map_metadata 0 \
  -c:v libx265 -preset medium -crf 24 -x265-params pools=10 \
  -maxrate SRC_V -bufsize <2.5x SRC_V> \
  -pix_fmt yuv420p -tag:v hvc1 -fps_mode cfr \
  -c:a copy -movflags +faststart OUT.mp4
```

Why these exact numbers: `cq 30` (NVENC) and `crf 24` (x265) both land near **SSIM 0.9963** — the
matched-quality point where the video comes back at ~70-76% of the source bitrate. Neither is
`cq 27`/`crf 27`: those are different qualities from each other, and `cq 27` is *above* an
already-efficient source, which is what made a library grow.

Every tier still runs the whole verification chain from section 7 — tier A included: a `-c copy`
can still originate a truncation, and a flat-ctts source cannot be copied at all.
