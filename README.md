# universal-video-transcode

**Any video or audio file in, one MP4 out that plays on both an iPad and an Android
device — at the best quality the target codec allows.**

Give it a path. A file converts; a folder converts everything in it.

```sh
python scripts/transcode.py "D:/video/clip.mkv"
python scripts/transcode.py "D:/video" --recursive --dry-run    # plan only, no encoding
python scripts/transcode.py "D:/video" --out D:/converted
```

## What it does

It reads the **source file** (never the filename) and picks one of three recipes:

| recipe | when | video | audio |
|---|---|---|---|
| `copy` | H.264/HEVC with a usable timeline | `-c copy` — **bit-identical** | copy if AAC |
| `fix-audio` | same, but the audio is not AAC | `-c copy` | AAC 256k |
| `encode` | VP9/AV1, or a timeline that cannot be copied | HEVC (or x264) | copy if AAC, else AAC 256k |

A copy is used whenever a copy is possible, because a copy is lossless *by definition* —
the strongest possible answer to "keep the quality the same".

Then it **verifies** the output and refuses to accept it otherwise: duration, resolution,
codec, `hvc1` tag, 8-bit pixel format, uniform frame cadence, no flat composition offsets,
and a full decode pass.

## Why it exists

A file can drop **~23% of its frames** in a browser while `r_frame_rate == avg_frame_rate`
reads **exactly** equal — so every fps-based check calls it healthy. The cause is
deterministic and header-readable: the MP4's composition-offset table is unusable, either a
single constant `ctts` entry (`PTS == DTS + const`) or **no `ctts` box at all** (`PTS == DTS`).
The timestamps are then in *decode* order, and the decoder's reordered output frames carry
the wrong times.

This repo gates on that, structurally, without playing anything.

```sh
python scripts/selftest.py     # builds both defect shapes and asserts the detector
```

```
fixture                            expect   got      verdict
src.mp4                            False    False    ok    healthy MP4, proper ctts
src.mkv                            False    False    ok    Matroska - no ctts by design
flat_noctts.mp4                    True     True     ok    MP4 with NO ctts box
flat_ctts1.mp4                     True     True     ok    MP4 with a single constant ctts entry
```

**Measured on the file that prompted this:** 23.0% of frames dropped before, **0.0% after**.

The full benefit case — the ten real failures this pipeline hit and the guard each one
produced — is in [`docs/WHY.md`](docs/WHY.md).

## Compatibility

| | iPad / iOS | Android |
|---|---|---|
| Container | MP4/MOV only — **no Matroska** | MP4 and MKV both fine |
| Video | H.264 (all), HEVC `hvc1` (A9+) | H.264 Baseline mandatory; HEVC 5.0+ |
| Audio | AAC — **never Opus** | AAC and Opus both fine |

Apple is the constraint, not Android: iOS has no Matroska demuxer and cannot play Opus, so
MP4 + AAC is what forces the conversion. Two traps: HEVC must be tagged **`hvc1`** (ffmpeg
defaults to `hev1`, which Apple refuses), and Android only *guarantees* HEVC Main profile
**Level 3** — use `--codec x264` when cheap/old Android hardware is in scope.

Details and citations: [`references/compatibility-matrix.md`](references/compatibility-matrix.md).

## Quality policy

- **Never upscale** — the source resolution is preserved; there is no `scale` in the pipeline.
- **HEVC** at `-cq 27`: measured **SSIM 0.9937** (visually transparent) at **~source size**.
- **H.264** (when chosen for reach) at `-crf 20`.
- **Audio** copied when already AAC; otherwise AAC 256k, where the residual error is the
  source's own lossy error — 320k buys nothing measurable.
- `-fps_mode cfr` on every re-encode: without it the encoder silently drops 0.5–1.3% of
  frames, leaving doubled intervals that play as judder.

## Running it unattended

Safe to schedule — it is idempotent and self-locking:

- a **lock file** refuses a second instance (two concurrent NVENC jobs starve one encoder and
  *drop frames*, the exact defect this tool removes)
- an already-playable file is skipped, so a re-run costs one probe
- a failed item leaves the input untouched, so the next run retries it
- aborts below 5 GB free rather than filling the work drive

```sh
python scripts/transcode.py "Z:/media/incoming" --recursive \
  --out "Z:/media/converted" >> /var/log/transcode.log 2>&1
```

## Install as a Hermes skill

This repo is the canonical source. Copy it into the skill directory rather than editing the
skill copy in place:

```sh
cp -r . "$LOCALAPPDATA/hermes/skills/media/universal-video-transcode"
```

See [`AGENTS.md`](AGENTS.md) for the non-negotiables.

## Options

| flag | meaning |
|---|---|
| `--dry-run` | print the plan, encode nothing |
| `--codec auto\|hevc\|x264` | force the video codec (`x264` for maximum reach) |
| `--cq N` / `--crf N` | quality (HEVC default 27 / H.264 default 20) |
| `--audio 256k`, `--channels 0` | audio bitrate; `0` keeps the source layout |
| `--cpu` | force libx265/libx264 (no NVENC) |
| `--keep` | keep the input file |
| `--overwrite` | replace an existing output |
| `--no-decode-check` | skip the full decode pass (faster, less safe) |
| `--lock PATH` | lock file location |

`ffmpeg`/`ffprobe` are taken from `PATH`, or from `TRANSCODE_FFMPEG` / `TRANSCODE_FFPROBE`.
