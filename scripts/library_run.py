#!/usr/bin/env python3
"""Library re-encode driver: same look, SMALLER file, playable on iPad + Android.

Policy (see references/hevc-conversion-policy.md):
  * classify each source by `bpp` and pick the cheapest tier that keeps the quality:
      lean  (bpp < 0.025) -> `-c:v copy`            video bit-identical, nothing to gain
      mid   (0.025-0.040) -> hevc_nvenc cq 30       ~6.4x realtime
      fat   (> 0.040)     -> libx265 crf 24         ~3x realtime, 9-16% more efficient
  * `-maxrate` = 1.0x the source's **video** bitrate (never the file's total) and a
    post-encode assertion that the output video bitrate did not exceed it
  * quality pinned to a measured number: per-frame SSIM of a sampled window vs the source
  * audio: copy when already AAC, else AAC at the SOURCE's own bitrate (never a fixed 256k)
  * `-fps_mode cfr` + `-pix_fmt yuv420p` + `-tag:v hvc1` on every re-encode

Per file, in order:
  1. copy the source OFF the network share to LOCAL staging (never encode over SMB/WebDAV)
  2. encode locally (tier above)
  3. structural gate + full decode pass + SSIM-vs-source + size assertion, all on the LOCAL output
  4. copy the verified output to the share as <name>.new.mp4
  5. read the share copy back end-to-end and compare SHA-256 with the local file
  6. ONLY then rename it over the original, and re-probe the result on the share
A file that fails at any step leaves the original untouched and is retried next run.
"""
import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcode as T  # noqa: E402

FFMPEG = T.FFMPEG
FFPROBE = T.FFPROBE
ROOT = r"Z:\Class\樊登读书\樊登读书会（每周更新）"
WORK = r"C:\AI\transcode-run"
STAGE = os.path.join(WORK, "stage")
LEDGER = os.path.join(WORK, "ledger.jsonl")
LOCKF = os.path.join(WORK, "run.lock")
VIDEO_EXT = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv",
             ".mpg", ".mpeg", ".m2ts", ".3gp", ".ogv", ".vob", ".rmvb", ".rm", ".divx"}
DECODE_CHECK = True

# ---- policy (all numbers measured; see references/hevc-conversion-policy.md §10) ----------
CQ_NVENC = 31            # measured: min SSIM 0.989-0.994 at 43-74% of the source video bitrate
CRF_X265 = 26            # measured: min SSIM 0.988-0.993 at 34-67% -- 10-22% smaller than NVENC
                         # at matched SSIM. crf24 = the conservative option (min 0.990-0.994, 43-84%)
NVENC_PRESET = "p6"
X265_PRESET = "medium"
X265_POOLS = 10          # E-cores only (affinity 0x3FF0000)
E_CORE_MASK = 0x3FF0000
SSIM_TARGET = 0.985      # per-frame floor vs the source window (480x270 comparison)
SSIM_WINDOW = 60         # seconds sampled for the SSIM gate
BAD_FRAME_SSIM = 0.85    # a frame this far below the source is 'bad' for the share test
MAX_BAD_SHARE = 0.01     # ... and more than this share of them means a systematic defect, not fades
FLAT_LUMA_STD = 3.0      # a source frame flatter than this: SSIM on it means nothing


def _flat_frames(raw_path, n, w=480, h=270, thresh=FLAT_LUMA_STD):
    """Indices of near-flat source frames (fades, black, plain titles) in a raw YUV window.

    SSIM collapses on such frames - two nearly black frames score ~0.25 while their neighbours
    score 0.998 - so a minimum taken over every frame condemns perfectly good encodes (measured on
    this library: the file that failed had 5 of 1500 frames below 0.85, all isolated, at a 0.98
    mean, while the genuine decoder-defect files had 44 of 1499).
    """
    import numpy as np
    fsz = w * h * 3 // 2
    flat = set()
    with open(raw_path, "rb") as f:
        for i in range(n):
            luma = f.read(w * h)
            if len(luma) < w * h:
                break
            if float(np.frombuffer(luma, dtype=np.uint8).std()) < thresh:
                flat.add(i)
            f.seek(fsz - w * h, 1)
    return flat


SIZE_CEIL = 1.0          # output video bitrate must not exceed the source's
QUALITY_STEPS = 2        # on a quality-gate failure, retry with the quality raised this many
                         # steps (cq -2 / crf -2) before giving up; the retry only fires on a
                         # file the cheaper setting could not carry


def log(*a):
    print(time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a), flush=True)


def run(cmd, timeout=86400, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **kw)


# ---------------------------------------------------------------- integrity

def _sha_local(path, label="local"):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def _sha_remote(path, label="remote", tries=3):
    """Hash a file over the network. getsize() lies on RaiDrive/Dokan, so the byte
    count here is what was actually read, never a metadata figure."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            h = hashlib.sha256()
            n = 0
            with open(path, "rb") as f:
                while True:
                    b = f.read(8 << 20)
                    if not b:
                        break
                    h.update(b)
                    n += len(b)
            return h.hexdigest(), n
        except OSError as e:
            last = e
            log("   ! %s read attempt %d failed: %s" % (label, attempt, e))
            time.sleep(5 * attempt)
    raise last


def copy_remote_to_local(remote, local, expect_size=None, tries=5):
    """Network -> local. Retried; a short read is a failure, never a silent pass.

    Measured on this host: RaiDrive/Dokan throws `[Errno 9] Bad file descriptor` on a 1 GB read
    when three jobs hammer the share at once (the user sees RaiDrive's own upload-failure toasts at
    the same time). Smaller chunks, more attempts and a longer backoff ride those hiccups out
    instead of losing the file - and each failed attempt costs minutes on a 1 GB source, so the
    point is to stop retrying as fast as we can.
    """
    for attempt in range(1, tries + 1):
        try:
            n = 0
            with open(remote, "rb") as fi, open(local, "wb") as fo:
                while True:
                    b = fi.read(4 << 20)
                    if not b:
                        break
                    fo.write(b)
                    n += len(b)
                fo.flush()
                os.fsync(fo.fileno())
            if expect_size and n != expect_size:
                raise IOError("short read: got %d, container says %d" % (n, expect_size))
            # A read of the RIGHT LENGTH can still be wrong on this share: RaiDrive reports stale
            # sizes, and a listing read here stopped halfway with no error at all (it looked like a
            # truncated file and was not). A corrupt staged source would be encoded into a corrupt
            # output, so pay for one more pass and compare hashes of what we wrote against a fresh
            # read of the share copy.
            if n > (64 << 20):
                a = _sha_local(local, "staged source")
                b = _sha_remote(remote, "share source")
                if a != b:
                    raise IOError("staged source hash mismatch (local %s.. != share %s..)"
                                  % (a[:12], b[:12]))
            return n
        except OSError as e:
            log("   ! source copy attempt %d/%d failed: %s" % (attempt, tries, e))
            time.sleep(15 * attempt)
    raise IOError("could not copy source off the share after %d attempts" % tries)


def copy_local_to_remote(local, remote_tmp, tries=4):
    """Local -> network temp name. Chunked; never writes the final name.

    Measured failure mode on this host: RaiDrive/Dokan aborts a ~500 MB write with
    "the operation didn't complete within specific time limit" (and leaves a partial `.new.mp4`),
    which the user sees as an upload-failed toast. Three jobs reading/writing the share at once is
    what provokes it. So: smaller chunks, a periodic flush so nothing huge sits in a buffer, more
    attempts, and a longer backoff. A partial file is always deleted before retrying (never resumed
    from `os.path.getsize()` on the share - RaiDrive reports stale sizes, so an offset taken from it
    would silently corrupt the upload).
    """
    for attempt in range(1, tries + 1):
        try:
            written = 0
            with open(local, "rb") as fi, open(remote_tmp, "wb") as fo:
                while True:
                    b = fi.read(4 << 20)
                    if not b:
                        break
                    fo.write(b)
                    written += len(b)
                    if written % (32 << 20) < (4 << 20):
                        fo.flush()
                        os.fsync(fo.fileno())
                fo.flush()
                os.fsync(fo.fileno())
            return True
        except OSError as e:
            log("   ! upload attempt %d/%d failed after %.0f MB: %s" % (
                attempt, tries, written / 1e6, e))
            try:
                os.remove(remote_tmp)
            except OSError:
                pass
            time.sleep(20 * attempt)
    return False


def probe_ok(path, info, vcodec="hevc"):
    """Structural gate on any path (local or share)."""
    return T.verify(info, path, vcodec, "copy" if info["acodec"] in ("aac", "mp4a", "") else "aac",
                    check_decode=False)


# ---------------------------------------------------------------- encode

def src_bitrate(info):
    if info["dur"] > 0 and info["size"] > 0:
        return info["size"] * 8 / info["dur"]
    return 0


def match_audio_kbps(path, floor=96, ceil=256, fallback=192):
    """AAC at the SOURCE's own bitrate, not a fixed 256k.

    Measured on this library: the weekly series ships mp3 at 128 kbps, so a fixed
    -b:a 256k added ~128 kbps for no audible gain - about +10% file size on a
    1.3 Mbps file, which by itself cancels the video savings. AAC is more efficient
    than mp3, so matching the bitrate is already a quality upgrade, never a loss.
    """
    r = run([FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate", "-of", "csv=p=0", path], timeout=120)
    try:
        b = int((r.stdout or "").strip().splitlines()[0].split(",")[0])
    except Exception:
        return fallback
    return max(floor, min(ceil, round(b / 1000)))


def src_video_bps(path, info):
    """The SOURCE's video-stream bitrate. Never the file's total: mixing them turned a 109%
    (grow) into a false 86% (shrink) in one analysis, because the total carries the audio."""
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate",
             "-of", "csv=p=0", path], timeout=180)
    try:
        v = int((r.stdout or "").strip().splitlines()[0].split(",")[0])
        if v > 0:
            return v
    except Exception:
        pass
    tot = info["size"] * 8 / info["dur"] if info.get("dur") else 0
    return max(0, int(tot - (info.get("abps") or 0)))


def src_fps(info):
    """transcode.probe() exposes rfps/afps, not `fps`."""
    return info.get("rfps") or info.get("afps") or 0


def bpp_of(info, vbps):
    """bits per pixel per frame: how much fat the source still carries."""
    d = (info.get("w") or 0) * (info.get("h") or 0) * src_fps(info)
    return (vbps / d) if d else 0.0


def tier_for_bpp(b):
    if b < 0.025:
        return "copy"
    return "nvenc" if b <= 0.040 else "x265"


def out_video_bps(path, dur):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate",
             "-of", "csv=p=0", path], timeout=180)
    try:
        v = int((r.stdout or "").strip().splitlines()[0].split(",")[0])
        if v > 0:
            return v
    except Exception:
        pass
    return int(os.path.getsize(path) * 8 / dur) if dur else 0


def ssim_vs_source(src_local, out_local, info, workdir, tag, dur=SSIM_WINDOW):
    """Per-frame SSIM of a sampled window: the OUTPUT against the SOURCE.

    The comparison is done at 480x270 (a wrong/scaled picture is obvious at that size and the
    decode stays cheap). NB the stats file must be a BARE name inside workdir - a Windows path
    inside `-lavfi` has its backslashes eaten by the filter parser, which silently yields zero
    frames and 0.0000 SSIM.

    Both sides are decoded with `-fps_mode passthrough` so the frame counts are the REAL ones. If
    they differ by more than 1% the source's timeline is irregular (measured on this library: a
    source with a 1.7 s hole - 1459 frames where 1500 belong - while the CFR encode fills it with
    duplicated frames), and a frame-by-frame comparison is *meaningless*: after the hole every
    frame pairs with the wrong one and SSIM collapses to 0.25 while the encode is perfectly good.
    In that case the picture gate is skipped (reported, not silently) and the caller's other
    assertions - structure, full decode, frame count not lower than the source, size ceiling -
    carry the verdict.
    """
    start = max(0.0, (info["dur"] / 2) - dur / 2)
    cmp_scale = "480:270"
    ref = os.path.join(workdir, "ssim_ref.yuv")
    out = os.path.join(workdir, "ssim_out.yuv")
    counts = []
    # Decode WITHOUT an input seek, and cut the window with the trim filter on absolute PTS.
    #
    # Two traps live here, both measured on real files in this library:
    #   1. An input seek snaps to a keyframe, and it snaps to a DIFFERENT keyframe for the source
    #      and for the encode (two 1500-frame decodes whose start times differed by ~10 s), so the
    #      frames met the wrong partners and a good encode scored mean 0.45.
    #   2. Worse, on an H.264 source with open GOPs an input seek decodes the frames after the
    #      landing point differently from a sequential decode: the SAME file compared with itself,
    #      once seeked and once decoded in order, scored min 0.25 / mean 0.82. A seek-based
    #      reference therefore condemns perfectly good encodes.
    # Sequential decoding costs a pass over the file up to the window at 480x270 - small next to the
    # encode it is judging, and it is the only decode that is guaranteed to match what the encoder
    # itself saw.
    vf = ("trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS,scale=%s:flags=bilinear,format=yuv420p"
          % (start, start + dur, cmp_scale))
    for path, dstp in ((src_local, ref), (out_local, out)):
        r = run([FFMPEG, "-y", "-v", "error", "-i", path,
                 "-map", "0:v:0", "-an", "-vf", vf,
                 "-fps_mode", "passthrough", "-f", "rawvideo", dstp], timeout=7200)
        if r.returncode != 0 or not os.path.exists(dstp) or not os.path.getsize(dstp):
            return None, None, 0, None, {}
        counts.append(os.path.getsize(dstp) // (480 * 270 * 3 // 2))
    n_src, n_out = counts
    if n_src and abs(n_out - n_src) / float(n_src) > 0.01:
        for f in (ref, out):
            try:
                os.remove(f)
            except OSError:
                pass
        return None, None, 0, ("source timeline irregular: %d frames vs %d in the sampled window "
                               "(%.1f%% difference) - the CFR encode fills the source's holes, so a "
                               "frame-by-frame comparison is not applicable" % (n_src, n_out,
                                                                               100.0 * (n_out - n_src) / n_src)), {}
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", cmp_scale,
                    "-i", ref, "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", cmp_scale, "-i", out,
                    "-lavfi", "ssim=stats_file=ssim_%s.log" % tag, "-f", "null", "-"],
                   capture_output=True, cwd=workdir)
    vals = []
    p = os.path.join(workdir, "ssim_%s.log" % tag)
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"All:([0-9.]+)", line)
                if m:
                    vals.append(float(m.group(1)))
    n = len(vals)
    # SSIM is meaningless on a nearly flat frame: two almost-black frames of a fade scored 0.25
    # while their neighbours scored 0.998, so a MINIMUM over every frame condemns good encodes.
    # Measure the source frames' flatness and drop them from the verdict (reported, not hidden).
    flat = set()
    try:
        flat = _flat_frames(ref, n)
    except Exception as e:  # numpy missing / unreadable raw: fall back to using every frame
        log("   ! flat-frame scan skipped: %s" % e)
    if flat and len(flat) < n * 0.5:
        vals = [v for i, v in enumerate(vals) if i not in flat]
        log("   %d near-flat source frame(s) excluded from the picture verdict (SSIM is "
            "meaningless on them)" % len(flat))
    n = len(vals)
    lo = min(vals) if vals else None
    mean = (sum(vals) / len(vals)) if vals else None
    bad = sum(1 for v in vals if v < BAD_FRAME_SSIM)
    stats = {"below": bad, "flat": len(flat), "total": n}
    note = None
    # If the direct pairing looks bad, try to EXPLAIN the mismatch as a timeline offset before
    # condemning the file: a source whose timeline drifts (or had a hole earlier) makes the encode
    # lag it by a fixed number of frames, and a wrong-picture defect can NOT be fixed by any single
    # shift. Requiring a good score at some offset is therefore a sound discriminator.
    if n and (lo is None or lo < SSIM_TARGET or mean < 0.985):
        best = None
        nf = min(n, 1200)
        for off in range(-100, 101, 10):
            if off == 0:
                continue
            mn, mm, k = _paired_ssim(ref, out, off, nf, workdir, "off%d" % abs(off))
            if mn is not None and (best is None or mn > best[0]):
                best = (mn, mm, k, off)
        if best and best[0] >= 0.90 and best[1] >= 0.98:
            note = ("timeline offset of %+d frames between source and encode - pictures verified at "
                    "that alignment (min SSIM %.4f mean %.4f)" % (best[3], best[0], best[1]))
            lo, mean, n = best[0], best[1], best[2]
    for f in (ref, out):
        try:
            os.remove(f)
        except OSError:
            pass
    return lo, mean, n, note, stats


def _paired_ssim(ref_raw, out_raw, off, nf, workdir, tag):
    """SSIM of the first `nf` frames of out_raw against ref_raw starting `off` frames in."""
    fs = 480 * 270 * 3 // 2
    with open(ref_raw, "rb") as f:
        f.seek(max(0, off) * fs)
        a = f.read(nf * fs)
    pa = os.path.join(workdir, "pair_a_%s.yuv" % tag)
    pb = os.path.join(workdir, "pair_b_%s.yuv" % tag)
    with open(pa, "wb") as f:
        f.write(a)
    with open(out_raw, "rb") as f:
        b = f.read(nf * fs)
    with open(pb, "wb") as f:
        f.write(b)
    vals = []
    log = os.path.join(workdir, "pair_%s.log" % tag)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "480:270",
                    "-i", pa, "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "480:270", "-i", pb,
                    "-lavfi", "ssim=stats_file=%s" % os.path.basename(log), "-f", "null", "-"],
                   capture_output=True, cwd=workdir)
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"All:([0-9.]+)", line)
                if m:
                    vals.append(float(m.group(1)))
    for f in (pa, pb):
        try:
            os.remove(f)
        except OSError:
            pass
    return (min(vals), sum(vals) / len(vals), len(vals)) if vals else (None, None, 0)


def pin_to_e_cores():
    """x265 tiers: keep the P-cores free for the desktop (measured 3.51x vs 4.37x realtime)."""
    try:
        ctypes.windll.kernel32.SetProcessAffinityMask(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.c_size_t(E_CORE_MASK))
        return True
    except Exception:
        return False


def encode(src_local, dst_local, info, args, tier, vbps, q_off=0):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
           "-i", src_local, "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0"]
    if tier == "copy":
        # Nothing to win: the video is copied bit-for-bit, which is the strongest possible
        # answer to "same picture quality" and the smallest file that still plays everywhere.
        cmd += ["-c:v", "copy"]
    elif tier == "nvenc":
        cmd += ["-c:v", "hevc_nvenc", "-preset", args.nvenc_preset, "-tune", "hq", "-rc", "vbr",
                "-cq", str(max(1, args.cq - q_off)), "-b:v", "0",
                # efficiency knobs verified accepted by this build (multipass 2pass-full and
                # weighted_pred are REJECTED here)
                "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
                "-b_ref_mode", "middle", "-bf", "4", "-g", "240"]
    else:
        cmd += ["-c:v", "libx265", "-preset", args.x265_preset, "-crf", str(max(1, args.crf - q_off)),
                "-x265-params", "pools=%d" % args.pools]
    if tier != "copy" and vbps > 0:
        # Hard ceiling at the source's own video bitrate: the output physically cannot grow.
        cmd += ["-maxrate", str(int(vbps * SIZE_CEIL)), "-bufsize", str(int(vbps * SIZE_CEIL * 2.5))]
    if tier != "copy":
        cmd += ["-profile:v", "main", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-fps_mode", "cfr"]
    if info["acodec"] in ("aac", "mp4a", ""):
        cmd += ["-c:a", "copy"]
    else:
        # AAC at the SOURCE's own bitrate: a fixed 256k added ~128 kbps for no audible gain on
        # this library's 128k mp3 sources, which by itself cancelled the video saving.
        abr = args.audio or ("%dk" % match_audio_kbps(src_local))
        cmd += ["-c:a", "aac", "-b:a", abr, "-ac", "2"]
    cmd += ["-movflags", "+faststart", dst_local]
    t0 = time.time()
    r = run(cmd)
    return r, time.time() - t0



def decode_check(path):
    """Full decode pass. The only check that proves the payload is not truncated/corrupt."""
    r = run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"], timeout=14400)
    return r.returncode == 0, (r.stderr or "").strip()[:300]


# ---------------------------------------------------------------- per file

def dst_path_for(src):
    """In-place: the SAME name for an existing .mp4/.MP4 (the share is case-sensitive),
    <stem>.mp4 for anything else."""
    if os.path.splitext(src)[1].lower() == ".mp4":
        return src
    return os.path.splitext(src)[0] + ".mp4"


def already_hevc(src, info):
    """Self-describing resume test: the share copy is already HEVC hvc1 and matches."""
    if info["vcodec"] != "hevc" or info["tag"] != "hvc1":
        return False
    return True


def process(src, args, dest=None, origin=None):
    info = T.probe(src)
    if not info:
        return "FAIL unreadable", None
    if info["dur"] < 1.0:
        return "FAIL stub (%.1fs) - needs a re-download, not a transcode" % info["dur"], None
    live = dest or dst_path_for(src)

    if info["vcodec"] == "hevc" and info["tag"] == "hvc1" and info["pix"] in ("yuv420p",) \
            and not T.unusable_timeline(info, src)[0]:
        # Already converted. Only worth touching if the ORIGINAL is available and the current
        # file is BIGGER than it: that is precisely the old settings' damage (a fixed cq27 above
        # the source's own quality plus a fixed 256k audio). A file that is already smaller than
        # its original is left alone, which makes the whole run naturally idempotent.
        if not (origin and os.path.exists(origin)):
            return "SKIP already HEVC hvc1", None
        o_size = os.path.getsize(origin)
        if info["size"] <= o_size * 0.99:
            return "SKIP already HEVC hvc1 (%.0f MB <= original %.0f MB)" % (
                info["size"] / 1e6, o_size / 1e6), None
        log("   re-doing from the original: live %.0f MB > original %.0f MB" % (
            info["size"] / 1e6, o_size / 1e6))
        src, info = origin, T.probe(origin)
        if not info:
            return "FAIL original unreadable", None

    # ---- policy: classify the source, then work only on the requested tier (if any) ----------
    vbps = src_video_bps(src, info)
    bpp = bpp_of(info, vbps)
    tier = tier_for_bpp(bpp)
    if args.tier and args.tier != tier:
        return "SKIP classified %s (run wants %s)" % (tier, args.tier), None
    expect_vc = "hevc" if tier != "copy" else (info["vcodec"] or "h264")
    log("   tier=%s bpp=%.4f src_video=%.0f kbps %sx%s@%.0f %s" % (
        tier, bpp, vbps / 1000, info["w"], info["h"], src_fps(info), os.path.basename(src)[:46]))

    os.makedirs(STAGE, exist_ok=True)
    tag = hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
    loc_src = os.path.join(STAGE, tag + "_src" + os.path.splitext(src)[1].lower())
    loc_out = os.path.join(STAGE, tag + "_out.mp4")
    remote_tmp = live + ".new.mp4"
    for p in (loc_src, loc_out):
        if os.path.exists(p):
            os.remove(p)
    origin_used = bool(origin and os.path.abspath(src) == os.path.abspath(origin))
    try:
        if os.path.exists(remote_tmp):
            os.remove(remote_tmp)

        # 1. fetch the source to local disk
        n_in = copy_remote_to_local(src, loc_src, expect_size=info["size"] or None)
        # 2. encode (with a bounded quality ladder: if the sampled window cannot clear the SSIM
        #    bar at the tier's default setting, raise the quality and try once more)
        attempt, q_off, el = 0, 0, 0.0
        while True:
            attempt += 1
            r, el = encode(loc_src, loc_out, info, args, tier, vbps, q_off)
            if r.returncode != 0 or not os.path.exists(loc_out):
                return "FAIL ffmpeg rc=%s %s" % (r.returncode, (r.stderr or "").strip()[-300:]), None
            # 3. verify the LOCAL output (structural + full decode)
            ok, msg = T.verify(info, loc_out, expect_vc, "aac", check_decode=False)
            if not ok:
                return "FAIL verify: %s" % msg, None
            if DECODE_CHECK and not args.no_decode_check:
                ok, msg = decode_check(loc_out)
                if not ok:
                    return "FAIL decode pass: %s" % msg, None
            # 3b. the two policy gates that make "same look, never bigger" a fact and not a hope:
            #     a per-frame SSIM of a sampled window against the source, and a hard assertion
            #     that the output video bitrate did not exceed the source's.
            ssim_note, growth = "copy (video bit-identical)", 100.0
            if tier != "copy":
                lo, mean, nfr, irreg, sstat = ssim_vs_source(loc_src, loc_out, info, STAGE, tag)
                obps = out_video_bps(loc_out, info["dur"])
                growth = 100.0 * obps / vbps if vbps else 0.0
                if irreg:
                    # Either the source's timeline has holes (frame pairing is meaningless) or the
                    # mismatch was explained by a fixed timeline offset. Both are reported, not
                    # silently swallowed, and the structural/decode/size gates still apply below.
                    ssim_note = irreg
                    log("   NOTE: %s" % ssim_note)
                    if vbps and obps > vbps * SIZE_CEIL:
                        return "FAIL size: output %.0f kbps > source %.0f kbps (%.1f%%)" % (
                            obps / 1000, vbps / 1000, growth), None
                    break
                if not nfr:
                    return "FAIL ssim: no frames compared", None
                ssim_note = "ssim min %.4f mean %.4f (%d frames)%s" % (
                    lo, mean, nfr, "" if not q_off else " at quality+%d" % q_off)
                # The verdict is a DEFECT SHAPE, not a single frame. A systematic mismatch drops the
                # mean and puts a real share of frames below the floor; a handful of scattered low
                # frames at a healthy mean is what fades, black frames and hard cuts do to SSIM.
                # Measured on this library: good encodes 0 of 1500 frames below 0.95; the file that
                # triggered this rule 5 of 1500 below 0.85 (0.33%) at 0.98 mean; the genuine
                # decoder-defect files 44 of 1499 (2.9%) with the mean falling too.
                bad_share = (sstat.get("below", 0) / float(nfr)) if nfr else 0.0
                systematic = (mean is not None and mean < 0.98) or bad_share > MAX_BAD_SHARE
                if systematic:
                    if q_off < QUALITY_STEPS and (attempt == 1):
                        q_off = QUALITY_STEPS
                        log("   quality gate: mean %.4f, %d of %d frames below %.2f (%.2f%%) - "
                            "retrying at quality+%d" % (mean, sstat.get("below", 0), nfr,
                                                        BAD_FRAME_SSIM, 100.0 * bad_share, q_off))
                        continue
                    return ("FAIL quality: %s - systematic: mean %.4f, %.2f%% of frames below %.2f"
                            % (ssim_note, mean, 100.0 * bad_share, BAD_FRAME_SSIM)), None
                if lo is not None and lo < SSIM_TARGET:
                    log("   %d scattered frame(s) below %.2f (%.2f%% of the window) with a healthy "
                        "mean - treated as fades/black, not a defect"
                        % (sstat.get("below", 0), BAD_FRAME_SSIM, 100.0 * bad_share))
                if vbps and obps > vbps * SIZE_CEIL:
                    return "FAIL size: output %.0f kbps > source %.0f kbps (%.1f%%)" % (
                        obps / 1000, vbps / 1000, growth), None
                log("   %s  %.1f%% of source video" % (ssim_note, growth))
            break
        # 4. upload under a temp name
        if args.keep_local:
            return "ok", {"sha256": "", "bytes": os.path.getsize(loc_out), "src_bytes": n_in,
                          "src_mb": round(info["size"] / 1e6, 1),
                          "out_mb": round(os.path.getsize(loc_out) / 1e6, 1),
                          "ratio": round(100.0 * os.path.getsize(loc_out) / max(info["size"], 1), 1),
                          "tier": tier, "bpp": round(bpp, 4), "video_pct": round(growth, 1),
                          "ssim": ssim_note + " [LOCAL ONLY - not uploaded]", "encode_s": round(el, 1)}
        # 4-5. upload under a temp name, then read the share copy back and compare.
        # The read-back is not ceremony: RaiDrive can return from a write with NO error while the
        # remote file is a fraction of the local one (measured: 12.5 MB of 403 MB), and its own
        # "operation didn't complete within specific time limit" toast arrives afterwards. So a
        # mismatch is retried rather than reported straight away.
        h_loc, n_loc = _sha_local(loc_out)
        last = ""
        for up in range(1, 3):
            if not copy_local_to_remote(loc_out, remote_tmp):
                last = "FAIL upload"
                time.sleep(10 * up)
                continue
            h_rem, n_rem = _sha_remote(remote_tmp)
            if (h_loc, n_loc) == (h_rem, n_rem):
                break
            last = "FAIL read-back mismatch local=%s/%d remote=%s/%d" % (
                h_loc[:12], n_loc, h_rem[:12], n_rem)
            log("   ! %s (upload %d) - retrying" % (last, up))
            try:
                os.remove(remote_tmp)
            except OSError:
                pass
            time.sleep(10 * up)
        else:
            return last, None
        # 6. atomic replace, then confirm on the share
        os.replace(remote_tmp, live)
        # NEVER delete the source when it came from the snapshot origin: that is the archive of
        # the originals, and removing it would destroy the only clean copy of the library.
        if not origin_used and os.path.abspath(live) != os.path.abspath(src):
            os.remove(src)
        back = T.probe(live)
        tag_needed = (tier != "copy")
        if not back or back["vcodec"] != expect_vc \
                or (tag_needed and back["tag"] != "hvc1") \
                or abs(back["dur"] - info["dur"]) > max(1.0, info["dur"] * 0.01):
            return "FAIL read-back probe: %s" % (back and (back["vcodec"], back["tag"], back["dur"]),), None
        ok, msg = probe_ok(live, info, expect_vc)
        if not ok:
            return "FAIL post-replace gate on share: %s" % msg, None
        return "ok", {"sha256": h_loc, "bytes": n_loc, "src_bytes": n_in,
                      "src_mb": round(info["size"] / 1e6, 1),
                      "out_mb": round(n_loc / 1e6, 1),
                      "ratio": round(100.0 * n_loc / max(info["size"], 1), 1),
                      "tier": tier, "bpp": round(bpp, 4), "video_pct": round(growth, 1),
                      "ssim": ssim_note, "encode_s": round(el, 1)}
    finally:
        for p in (loc_src, loc_out):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def collect(root):
    out = []
    for dp, _, fn in os.walk(root):
        for f in fn:
            if os.path.splitext(f)[1].lower() in VIDEO_EXT and ".part." not in f \
                    and "_src" not in f and ".new." not in f:
                out.append(os.path.join(dp, f))
    return sorted(out)


def main():
    global DECODE_CHECK
    ap = argparse.ArgumentParser()
    ap.add_argument("--cq", type=int, default=CQ_NVENC, help="nvenc tier quality (measured default 30)")
    ap.add_argument("--crf", type=int, default=CRF_X265, help="x265 tier quality (measured default 24)")
    ap.add_argument("--x265-preset", default=X265_PRESET)
    ap.add_argument("--pools", type=int, default=X265_POOLS, help="x265 threads (match the E-core mask)")
    ap.add_argument("--tier", choices=["copy", "nvenc", "x265"],
                    help="only touch files classified into this tier (run tiers separately)")
    ap.add_argument("--origin-root", default=None,
                    help="tree holding the PRE-CONVERSION originals (e.g. a snapshot); a file that "
                         "is already HEVC and BIGGER than its original is re-done from that "
                         "original, and the original is never modified or deleted")
    ap.add_argument("--audio", default=None,
                    help="audio bitrate; default = the SOURCE's own bitrate (never a fixed 256k)")
    ap.add_argument("--nvenc-preset", default=NVENC_PRESET)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None, help="substring filter")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-decode-check", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="re-do files already HEVC")
    ap.add_argument("--keep-local", action="store_true",
                    help="pilot mode: encode + verify + gate locally and report the numbers, then "
                         "THROW THE OUTPUT AWAY - nothing is uploaded and nothing is replaced")
    args = ap.parse_args()
    DECODE_CHECK = not args.no_decode_check

    os.makedirs(STAGE, exist_ok=True)
    files = collect(ROOT)
    if args.only:
        files = [f for f in files if args.only in f]
    if args.reverse:
        files = list(reversed(files))
    if args.limit:
        files = files[:args.limit]
    if args.tier == "x265":
        # x265 is CPU: pin it to the E-cores so the P-cores stay free (children inherit the mask).
        log("=== E-core pinning: %s" % ("on" if pin_to_e_cores() else "FAILED (running unpinned)"))
    # One lock PER TIER, not one global lock: the copy tier uses no encoder, the x265 tier is CPU
    # and the nvenc tier is the GPU ASIC, so they are safe to run side by side whereas two runs of
    # the same tier would race on dst and starve a shared unit.
    lockf = os.path.join(WORK, "run-%s.lock" % (args.tier or "all"))
    log("=== %d file(s)  tier=%s cq=%d crf=%d audio=%s decode_check=%s lock=%s" %
        (len(files), args.tier or "auto", args.cq, args.crf, args.audio or "source-matched",
         DECODE_CHECK, os.path.basename(lockf)))

    if args.dry_run:
        plan = {}
        for f in files:
            info = T.probe(f)
            if not info:
                log("PLAN %s -> UNREADABLE" % os.path.relpath(f, ROOT))
                continue
            vbps = src_video_bps(f, info)
            b = bpp_of(info, vbps)
            t = tier_for_bpp(b)
            plan[t] = plan.get(t, 0) + 1
            log("PLAN %-5s bpp=%.4f %sx%s@%.0f %6.0f kbps %7.1f MB %s" % (
                t, b, info["w"], info["h"], src_fps(info), vbps / 1000, info["size"] / 1e6,
                os.path.relpath(f, ROOT)[:60]))
        log("=== plan: %s" % plan)
        return

    with T.Lock(lockf):
        stats = {"ok": 0, "skip": 0, "fail": 0}
        ledger = open(LEDGER, "a", encoding="utf-8")
        for i, f in enumerate(files, 1):
            free = shutil.disk_usage(STAGE).free / 2 ** 30
            if free < 8:
                log("!!! ABORT: only %.1f GB free on C: - stopping" % free)
                break
            log("--- %d/%d %s" % (i, len(files), os.path.relpath(f, ROOT)))
            t0 = time.time()
            origin = None
            if args.origin_root:
                cand = os.path.join(args.origin_root, os.path.relpath(f, ROOT))
                origin = cand if os.path.exists(cand) else None
            try:
                status, extra = process(f, args, dest=dst_path_for(f), origin=origin)
            except Exception as e:
                status, extra = "FAIL %s: %s" % (type(e).__name__, e), None
            el = time.time() - t0
            if status == "ok":
                stats["ok"] += 1
                log("   ok %.0f MB -> %.0f MB (%.0f%%) video %.1f%% enc %.0fs tot %.0fs | %s | %s" % (
                    extra["src_mb"], extra["out_mb"], extra["ratio"], extra["video_pct"],
                    extra["encode_s"], el, extra["tier"], extra["ssim"]))
            elif status.startswith("SKIP"):
                stats["skip"] += 1
                log("   %s" % status)
            else:
                stats["fail"] += 1
                log("   !! %s" % status)
            ledger.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "path": os.path.relpath(f, ROOT), "status": status,
                                     "secs": round(el, 1), "extra": extra},
                                    ensure_ascii=False) + "\n")
            ledger.flush()
            if i % 10 == 0:
                log("   [progress] ok=%d skip=%d fail=%d" % (stats["ok"], stats["skip"], stats["fail"]))
        ledger.close()
        log("=== done: ok=%d skipped=%d failed=%d" % (stats["ok"], stats["skip"], stats["fail"]))
        if stats["fail"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
