#!/usr/bin/env python3
"""transcode - turn ANY video/audio file into an MP4 that plays on BOTH iPad and Android,
keeping quality as close to the source as the target codec allows.

Give it a path. A file converts; a folder converts everything in it. Nothing else is needed.

    python transcode.py "D:/video/clip.mkv"
    python transcode.py "D:/video" --recursive
    python transcode.py "D:/video" --dry-run          # plan only, no encoding

Three recipes are chosen automatically from the SOURCE FILE (never from a filename):

  copy-video  source is H.264/HEVC with a usable timeline -> remux to MP4, bit-identical
  fix-audio   same, but the audio is not AAC        -> copy video, re-encode audio to AAC
  encode      VP9/AV1, or H.264 whose timeline is unusable -> full HEVC (or x264) re-encode

Quality policy ("as close to the original as possible"):
  * never upscale; the source resolution is preserved
  * video is COPIED whenever a copy is possible - that is lossless by definition
  * HEVC re-encode targets -cq 27, measured at SSIM 0.9937 == visually transparent,
    landing at ~source size. x264 (for browser-only targets) targets -crf 20.
  * audio is COPIED when it is already AAC (bit-identical); otherwise AAC 256k, where the
    residual error is the source's own lossy error, so more bits buy nothing.

Every output is verified before it is accepted; a failed item leaves the input untouched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

FFMPEG = os.environ.get("TRANSCODE_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("TRANSCODE_FFPROBE", "ffprobe")
MIN_FREE_GB = 5
VIDEO_EXT = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv",
             ".mpg", ".mpeg", ".m2ts", ".3gp", ".ogv", ".vob"}
AUDIO_EXT = {".m4a", ".aac", ".mp3", ".opus", ".flac", ".wav", ".ogg", ".wma"}
SKIP_EXT = {".jpg", ".jpeg", ".png", ".nfo", ".vtt", ".srt", ".json", ".txt", ".part",
            ".ytdl", ".tmp"}


def log(*a):
    print(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a), flush=True)


def run(cmd, timeout=86400):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _fps(v):
    try:
        n, d = (v or "0/0").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        return 0.0


def probe(path):
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path], timeout=120)
    try:
        j = json.loads(r.stdout)
    except Exception:
        return None
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return None
    return {
        "vcodec": (v.get("codec_name") or "").lower(),
        "tag": (v.get("codec_tag_string") or "").lower(),
        "w": v.get("width"), "h": v.get("height"),
        "pix": (v.get("pix_fmt") or "").lower(),
        "acodec": (a.get("codec_name") or "").lower() if a else "",
        "channels": a.get("channels") if a else 0,
        "dur": float(j.get("format", {}).get("duration") or 0),
        "size": int(j.get("format", {}).get("size") or 0),
        "fmt": (j.get("format", {}).get("format_name") or "").lower(),
        "rfps": _fps(v.get("r_frame_rate")), "afps": _fps(v.get("avg_frame_rate")),
        "hbf": int(v.get("has_b_frames") or 0),
    }


def _ctts_entries(path):
    """ctts entry count from the moov; -1 if the MP4 video track has NO ctts box at all;
    None if there is no MP4 sample table (not an MP4, or unreadable).

    The -1/None distinction matters: a missing ctts is a DEFECT on an MP4 with B-frames,
    but is perfectly normal in Matroska, which stores presentation timestamps per block.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(2_000_000)
    except Exception:
        return None
    p, saw_stbl = 0, False
    while p + 8 <= len(head):
        size = int.from_bytes(head[p:p + 4], "big")
        typ = head[p + 4:p + 8]
        hdr = 8
        if size == 1:
            size = int.from_bytes(head[p + 8:p + 16], "big"); hdr = 16
        elif size == 0:
            break
        if size < 8:
            break
        if typ == b"ctts":
            return int.from_bytes(head[p + 12:p + 16], "big")
        if typ == b"stbl":
            saw_stbl = True
        if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
            p += hdr
            continue
        p += size
    return -1 if saw_stbl else None


def flat_timing(info, path):
    """True when a B-frame MP4 track carries no usable composition offsets.

    Either a single constant ctts entry (PTS == DTS + const) or no ctts box at all
    (PTS == DTS). Both put the frame timestamps in DECODE order; the decoder still
    reorders by POC, so the timestamps on the output frames are wrong and the compositor
    drops ~23% of them. r_frame_rate == avg_frame_rate EXACTLY, so no fps test sees it.
    """
    if not info or info["hbf"] <= 0:
        return False
    ce = _ctts_entries(path)
    if ce is None:
        return False            # not an MP4 video track - not our call to make
    return ce <= 1


def pts_inversions(path, seconds=3):
    """(inversions, packets) over the first `seconds` of video packets.

    A stream with B-frames MUST present frames out of decode order, so its container PTS
    go backwards somewhere: 0 inversions means the timestamps are in decode order.
    """
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", "%%+%d" % seconds,
             "-show_entries", "packet=pts", "-of", "csv=p=0", path], timeout=180)
    vals = []
    for line in (r.stdout or "").splitlines():
        t = line.strip().split(",")[0]
        if t.lstrip("-").isdigit():
            vals.append(int(t))
    return sum(1 for i in range(len(vals) - 1) if vals[i + 1] < vals[i]), len(vals)


def unusable_timeline(info, path):
    """(bool, why) - frame timestamps that no player can present smoothly.

    Covers BOTH defect shapes in one gate, across containers:
      * MP4      -> ctts missing, or a single constant entry (PTS == DTS + const)
      * Matroska -> no ctts by design, so read the packet PTS instead; 0 inversions on a
                    B-frame stream means decode-order timestamps
    Either way the decoder reorders by POC and the compositor drops ~23% of frames, while
    r_frame_rate == avg_frame_rate stays EXACTLY equal, so no fps test can see it.
    """
    if not info or info["hbf"] <= 0:
        return False, ""
    ce = _ctts_entries(path)
    if ce is not None:
        if ce <= 1:
            return True, "flat composition offsets (ctts=%s)" % (ce if ce >= 0 else "missing")
        return False, ""
    inv, n = pts_inversions(path)
    if n >= 20 and inv == 0:
        return True, "flat timestamps in %s (0 PTS inversions over %d packets)" % (info["fmt"], n)
    return False, ""


def has_nvenc():
    try:
        r = run([FFMPEG, "-hide_banner", "-encoders"], timeout=60)
        return "hevc_nvenc" in (r.stdout or "")
    except Exception:
        return False


def plan_for(info, path, force_codec):
    """Return (video_mode, audio_mode, reason). video_mode: copy | x264 | hevc."""
    if info["dur"] < 1.0:
        return None, None, "stub (needs re-download, not a transcode)"
    if info["vcodec"] not in ("h264", "avc1", "hevc", "vp9", "vp09", "av1", "av01"):
        return None, None, "unsupported video codec %s" % info["vcodec"]

    acopy = info["acodec"] in ("aac", "mp4a", "")
    amode = "copy" if acopy else "aac"

    is_mp4 = ("mp4" in info["fmt"]) or ("mov" in info["fmt"])
    flat, why_flat = unusable_timeline(info, path)
    uniform = abs(info["rfps"] - info["afps"]) <= 0.01

    if info["vcodec"] in ("h264", "avc1"):
        # A copy is lossless, but only if the timeline is already correct: ffmpeg cannot
        # retime under -c copy, so a defective one would survive into the output.
        if flat:
            return "x264", amode, "H.264 but %s - copy cannot retime" % why_flat
        if not uniform:
            return "x264", amode, "H.264 but non-uniform cadence - copy cannot retime"
        if not is_mp4:
            return "copy", amode, "H.264 in %s -> lossless remux to MP4" % (info["fmt"] or "?")
        if info["tag"] and info["tag"] != "avc1":
            return "copy", amode, "re-tag %s -> avc1" % info["tag"]
        if not acopy:
            return "copy", amode, "H.264 + %s audio -> copy video, fix audio" % info["acodec"]
        return None, None, "already playable MP4 (H.264 + AAC, correct timeline)"

    if info["vcodec"] == "hevc":
        if is_mp4 and info["tag"] == "hvc1" and not flat and uniform and acopy:
            return None, None, "already playable MP4 (HEVC hvc1 + AAC, correct timeline)"
        if flat or not uniform:
            return "hevc", amode, "HEVC but %s - must re-encode" % (why_flat or "non-uniform cadence")
        return "copy", amode, "HEVC -> remux/tag hvc1 + fix audio"

    want = force_codec if force_codec != "auto" else ("x264" if info["vcodec"] == "h264" else "hevc")
    return want, amode, "%s -> %s re-encode" % (info["vcodec"].upper(), want)


def build_cmd(src, dst, info, vmode, amode, args, use_nvenc):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    if vmode == "hevc" and use_nvenc:
        dec = {"vp9": "vp9_cuvid", "vp09": "vp9_cuvid", "av1": "av1_cuvid",
               "av01": "av1_cuvid", "h264": "h264_cuvid", "avc1": "h264_cuvid",
               "hevc": "hevc_cuvid"}.get(info["vcodec"])
        if dec:
            cmd += ["-c:v", dec]
    cmd += ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0"]

    if vmode == "copy":
        cmd += ["-c:v", "copy"]
    elif vmode == "x264":
        cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                "-profile:v", "high", "-level:v", "4.0", "-pix_fmt", "yuv420p"]
    else:
        if use_nvenc:
            br = info["size"] * 8 / max(info["dur"], 1)
            cmd += ["-c:v", "hevc_nvenc", "-preset", args.nvenc_preset, "-tune", "hq",
                    "-rc", "vbr", "-cq", str(args.cq), "-b:v", "0",
                    "-maxrate", str(int(br * 1.5)), "-bufsize", str(int(br * 1.5 * 2.5))]
        else:
            cmd += ["-c:v", "libx265", "-preset", args.preset, "-crf", str(args.cq)]
        cmd += ["-profile:v", "main", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]

    # -fps_mode cfr is MANDATORY on every re-encode: without it the encoder silently drops
    # the frames it cannot place (measured 0.5-1.3%), leaving doubled frame intervals that
    # play as judder. Copy modes keep the source's own timing, which was verified above.
    if vmode != "copy":
        cmd += ["-fps_mode", "cfr"]

    if amode == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", args.audio]
        if args.channels:
            cmd += ["-ac", str(args.channels)]

    cmd += ["-movflags", "+faststart", dst]
    return cmd


def verify(src_info, out, vmode, amode, check_decode=True):
    """Structural gate. Returns (ok, message). Every claim is read back from the file."""
    oi = probe(out)
    if not oi:
        return False, "output unreadable"
    if oi["dur"] < 1.0:
        return False, "output is a stub"
    if abs(oi["dur"] - src_info["dur"]) > max(1.0, src_info["dur"] * 0.01):
        return False, "duration %.2f vs source %.2f" % (oi["dur"], src_info["dur"])
    if (oi["w"], oi["h"]) != (src_info["w"], src_info["h"]):
        return False, "resolution %sx%s vs %sx%s" % (oi["w"], oi["h"], src_info["w"], src_info["h"])
    if vmode == "hevc":
        if oi["vcodec"] != "hevc":
            return False, "vcodec %s (want hevc)" % oi["vcodec"]
        if oi["tag"] != "hvc1":
            return False, "codec tag %s (Apple requires hvc1)" % oi["tag"]
    if vmode == "x264" and oi["vcodec"] not in ("h264", "avc1"):
        return False, "vcodec %s (want h264)" % oi["vcodec"]
    if src_info["acodec"] and oi["acodec"] != "aac":
        return False, "audio %s (want aac)" % oi["acodec"]
    if oi["pix"] not in ("yuv420p", "yuvj420p"):
        return False, "pixel format %s (want 8-bit 4:2:0)" % oi["pix"]
    if vmode != "copy":
        if abs(oi["rfps"] - oi["afps"]) > 0.01:
            return False, "non-uniform cadence r=%.3f avg=%.3f (is -fps_mode cfr missing?)" % (
                oi["rfps"], oi["afps"])
    if flat_timing(oi, out):
        return False, "flat composition offsets survived (ctts<=1 on a B-frame stream)"
    if check_decode:
        r = run([FFMPEG, "-v", "error", "-i", out, "-f", "null", "-"], timeout=7200)
        if r.returncode != 0:
            return False, "decode errors: %s" % (r.stderr or "").strip()[:200]
    return True, "ok"


def out_path(src, args):
    stem = os.path.splitext(os.path.basename(src))[0]
    d = args.out or os.path.dirname(os.path.abspath(src))
    return os.path.join(d, stem + ".mp4")


class Lock:
    """One encoder at a time. Two concurrent NVENC jobs starve each other and drop frames -
    the exact defect this tool exists to prevent - so cron runs must never overlap."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
        except FileExistsError:
            try:
                pid = int(open(self.path).read().strip() or 0)
            except Exception:
                pid = 0
            if pid and _alive(pid):
                raise SystemExit("another transcode is already running (pid %d, %s) - refusing "
                                 "to start a second encoder" % (pid, self.path))
            log("stale lock (pid %s gone) - taking it over" % pid)
            os.remove(self.path)
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.remove(self.path)
        except OSError:
            pass


def _alive(pid):
    try:
        if os.name == "nt":
            r = run(["tasklist", "/FI", "PID eq %d" % pid], timeout=30)
            return str(pid) in (r.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def collect(paths, recursive):
    out = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(p):
            if recursive:
                for dp, _, fn in os.walk(p):
                    out += [os.path.join(dp, f) for f in fn]
            else:
                out += [os.path.join(p, f) for f in os.listdir(p)]
        else:
            log("!! not found: %s" % p)
    keep = []
    for f in sorted(out):
        e = os.path.splitext(f)[1].lower()
        if e in SKIP_EXT or e not in (VIDEO_EXT | AUDIO_EXT):
            continue
        if "_src" in os.path.basename(f) or ".part." in f:
            continue
        keep.append(f)
    return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--recursive", "-r", action="store_true")
    ap.add_argument("--codec", choices=["auto", "hevc", "x264"], default="auto")
    ap.add_argument("--cq", type=int, default=27, help="NVENC HEVC quality (default 27)")
    ap.add_argument("--crf", type=int, default=20, help="x264 CRF (default 20)")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--nvenc-preset", default="p6")
    ap.add_argument("--audio", default="256k")
    ap.add_argument("--channels", type=int, default=2, help="0 = keep the source layout")
    ap.add_argument("--cpu", action="store_true", help="force libx265/libx264 (no NVENC)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the input file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-decode-check", action="store_true")
    ap.add_argument("--lock", default=os.path.join(os.path.expanduser("~"),
                                                   ".transcode.lock"))
    a = ap.parse_args()

    # --out is a DIRECTORY; create it up front so the free-space probe and the encode
    # both have a real path to work with (os.path.dirname of a not-yet-made dir is None,
    # and shutil.disk_usage raises WinError 3 on a path that does not exist).
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    if not a.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(a.lock)) or ".", exist_ok=True)

    files = collect(a.paths, a.recursive)
    if not files:
        raise SystemExit("nothing to do: no video/audio files found in %s" % a.paths)

    use_nvenc = (not a.cpu) and has_nvenc()
    log("=== transcode: %d file(s)  nvenc=%s  codec=%s  dry=%s"
        % (len(files), use_nvenc, a.codec, a.dry_run))

    done = {"ok": 0, "skipped": 0, "failed": 0}
    plans = []
    for src in files:
        info = probe(src)
        if not info:
            log("SKIP %s: unreadable" % os.path.basename(src)); done["skipped"] += 1; continue
        vmode, amode, reason = plan_for(info, src, a.codec)
        if vmode is None:
            log("SKIP %s: %s" % (os.path.basename(src), reason)); done["skipped"] += 1; continue
        plans.append((src, info, vmode, amode, reason))

    if a.dry_run:
        for src, info, vmode, amode, reason in plans:
            log("PLAN %s -> %s | video=%s audio=%s | %s"
                % (os.path.basename(src), os.path.basename(out_path(src, a)),
                   vmode, amode, reason))
        log("=== dry-run: %d to convert, %d already fine" % (len(plans), done["skipped"]))
        return

    with Lock(a.lock):
        for i, (src, info, vmode, amode, reason) in enumerate(plans, 1):
            dst = out_path(src, a)
            probe_dir = os.path.dirname(os.path.abspath(dst)) or "."
            os.makedirs(probe_dir, exist_ok=True)
            free = shutil.disk_usage(probe_dir).free / 2 ** 30
            if free < MIN_FREE_GB:
                log("!!! ABORT: %.1f GB free, need %d GB - %d item(s) not attempted"
                    % (free, MIN_FREE_GB, len(plans) - i + 1))
                break
            log("--- %d/%d %s | video=%s audio=%s | %s"
                % (i, len(plans), os.path.basename(src), vmode, amode, reason))
            if os.path.exists(dst) and not a.overwrite:
                log("SKIP: %s exists (use --overwrite)" % os.path.basename(dst))
                done["skipped"] += 1; continue

            part = dst + ".part.mp4"
            cmd = build_cmd(src, part, info, vmode, amode, a, use_nvenc)
            t0 = time.time()
            r = run(cmd)
            if r.returncode != 0:
                log("!! ffmpeg failed rc=%s: %s" % (r.returncode, (r.stderr or "").strip()[-300:]))
                if os.path.exists(part):
                    os.remove(part)
                done["failed"] += 1; continue
            el = time.time() - t0

            ok, msg = verify(info, part, vmode, amode, check_decode=not a.no_decode_check)
            if not ok:
                log("!! verify failed: %s" % msg)
                os.remove(part)
                done["failed"] += 1; continue

            os.replace(part, dst)
            back = probe(dst)
            if not back or abs(back["dur"] - info["dur"]) > max(1.0, info["dur"] * 0.01):
                log("!! READ-BACK MISMATCH after writing %s - input left untouched" % dst)
                done["failed"] += 1; continue

            if not a.keep and os.path.abspath(src) != os.path.abspath(dst):
                try:
                    os.remove(src)
                except OSError:
                    pass
            sz = os.path.getsize(dst)
            log("   ok %s  %.0f MB (%.0f%% of source) in %.0fs"
                % (os.path.basename(dst), sz / 1e6, 100.0 * sz / max(info["size"], 1), el))
            done["ok"] += 1

    log("=== done: ok=%d skipped=%d failed=%d" % (done["ok"], done["skipped"], done["failed"]))
    if done["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
