# Compatibility matrix — what actually plays where

Sources: Android's [Supported media formats](https://developer.android.com/media/platform/supported-formats)
(read 2026-10-04) and Apple's HEVC hardware baseline (A9, announced 2015-09-09 with the
iPhone 6S — its first chip with a hardware HEVC decoder, **Main 8 and Main 10**).

## The intersection

The target is the set that plays on **both** platforms with **no third-party player**:

```text
Container : MP4 (ISO BMFF)
Video     : H.264 High 8-bit yuv420p  ── universal ──
            or HEVC Main 8-bit yuv420p, tagged hvc1  ── smaller ──
Audio     : AAC-LC
Extras    : moov before mdat (+faststart), uniform frame cadence
```

**Apple is the constraint, not Android.** Android decodes Matroska and Opus natively; iOS has
no Matroska demuxer and cannot play Opus at all. So `.mkv` + Opus → MP4 + AAC is the change
that forces the whole conversion.

## Android (from the official table)

| Format | Decoder | Containers | Notes |
|---|---|---|---|
| H.264 AVC **Baseline** | **YES** (all versions) | 3GP, MP4, MPEG-TS | the only unconditional guarantee |
| H.264 AVC **Main** | **YES** | — | "The decoder is required, the encoder is recommended"; encoder Main is Android 6.0+ |
| **H.265 HEVC** | **Android 5.0+** | MP4, Matroska | **"Main Profile Level 3 for mobile devices and Main Profile Level 4.1 for Android TV"** |
| VP8 | Android 2.3.3+ | WebM, Matroska | |
| VP9 | Android 4.4+ | WebM, Matroska, MP4 | |
| AV1 | Android 10+ (mandatory 14+) | MP4, Matroska | |
| AAC-LC | YES | 3GP, MP4, m4a, ADTS | mono/stereo/5.0/5.1, 8–48 kHz |
| Opus | Android 5.0+ | Ogg, MP4, Matroska | irrelevant for iOS targets |

### The HEVC level caveat — read this before promising "HEVC plays on Android"

Android's guarantee is **Main profile Level 3**, which is roughly SD. Our 1080p output declares
**Level 4.0** and 4K declares **5.0** (measured). Modern phones decode far beyond that, but a
low-end or old device is only *required* to reach Level 3 — so "Android supports HEVC" does not
imply "Android supports *this* HEVC".

- Audience includes cheap/old Android hardware → prefer **H.264** (`--codec x264`).
- Modern devices only → HEVC is fine and buys a large size reduction.

## Apple / iOS

| Format | Support |
|---|---|
| Container | **MP4 / MOV only.** No Matroska demuxer in Photos, Files, TV or Safari. |
| H.264 | All devices. |
| HEVC | **A9 and newer** (iPhone 6S / 2015 iPad and later). Main 8 and Main 10. |
| `hvc1` vs `hev1` | **`hvc1` required.** Apple refuses `hev1`; ffmpeg defaults to `hev1`. |
| VP9 / VP09 | **Never.** |
| AV1 | Only A17 Pro / M3 and newer. |
| Opus | **Never.** |
| AAC | Yes. |

`-tag:v hvc1` is the single most commonly missed flag: a file that is otherwise perfect will be
refused by an iPad purely because it carries the `hev1` sample entry.

## Pixel format and bit depth

| pix_fmt | Verdict |
|---|---|
| **`yuv420p`** | **use this** — 8-bit 4:2:0, universal on both platforms |
| `yuv420p10le` | Apple accepts it (A9+, Main 10); Android's guarantee is Main profile, so treat as a compatibility risk. Not needed for SDR sources. |
| `yuv422p` / `yuv444p` | **avoid** — outside the guaranteed profile for both |

**HDR is a separate question.** All VP9/VP09/AV1 sources sampled from this library were
**Profile 0 = 8-bit, yuv420p, bt709**; HDR was 1 file in 4431. For SDR sources there is nothing
to preserve, and tone-mapping is out of scope for the tool.

## Container and streaming details

- `moov` **before** `mdat` (`-movflags +faststart`) so playback starts without downloading the
  whole file. Verify: the first 32 bytes are `…ftyp isom`, then `moov`, then `mdat`.
- **Range requests must work** — the player seeks with them. Expect `206` +
  `Content-Range: bytes …` + `Accept-Ranges: bytes`.
- `Content-Type` should be `video/mp4`. A converted file served as `video/matroska` can be
  refused by AVPlayer even though the bytes are a valid MP4.
- **The filename extension is not authoritative.** A `.mkv` name on an MP4 body streams fine
  when `Content-Type` follows the container. Do not "fix" the extension; do fix the MIME.

## Audio decisions

| source audio | action | result |
|---|---|---|
| AAC | **copy** (`-c:a copy`) | bit-identical to the original |
| Opus / Vorbis / anything else | **AAC 256k** | required — Opus is invalid in MP4 and iOS cannot play it |

Measured cascade error (source Opus decode vs AAC decode, sample-aligned): 128k = 22.7 dB,
192k = 29.0 dB, **256k = 33.5 dB**, 320k = 36.1 dB. Above 256k the residual is the source's own
Opus loss, so 320k buys almost nothing. Cost of 256k over a 626-hour library: +0.5 MB/min
≈ +19 GB (~3%), which is why audio stops being a concern at 256k.
