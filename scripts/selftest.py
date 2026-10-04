#!/usr/bin/env python3
"""selftest - prove the frame-timing detector actually works, on this machine.

The whole value of this repo rests on one claim: a file can drop ~23% of its frames in a
browser while `r_frame_rate == avg_frame_rate` EXACTLY, and the defect is decidable from
the header alone. So the detector is tested against files built here, with both defect
shapes and two must-not-false-positive cases:

  src.mp4            healthy MP4, B-frames, proper ctts        -> must NOT be flagged
  src.mkv            same stream in Matroska (no ctts by design) -> must NOT be flagged
  flat_noctts.mp4    MP4 video track with NO ctts box           -> MUST be flagged
  flat_ctts1.mp4     MP4 with a single constant ctts entry      -> MUST be flagged

`flat_ctts1.mp4` is produced by binary-patching the real ctts box down to one entry and
padding the difference with a `free` box, so the moov keeps its exact size and no chunk
offsets shift. That is the shape YouTube/yt-dlp actually produced in the field.

Exit code 0 = all assertions held. Non-zero = the detector is wrong, and the tool cannot
be trusted to gate conversions.

Usage:  python scripts/selftest.py
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcode as T   # noqa: E402

FFMPEG = T.FFMPEG
FFPROBE = T.FFPROBE

NESTED = (b"moov", b"trak", b"mdia", b"minf", b"stbl")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def find_box(buf, typ):
    """Walk the (nested) box tree, return (offset, size) of the first `typ`, else None."""
    def walk(start, end):
        p = start
        while p + 8 <= end:
            size = struct.unpack(">I", buf[p:p + 4])[0]
            name = buf[p + 4:p + 8]
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", buf[p + 8:p + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - p
            if size < 8 or p + size > end:
                return None
            if name == typ:
                return (p, size)
            if name in NESTED:
                got = walk(p + hdr, p + size)
                if got:
                    return got
            p += size
        return None
    return walk(0, len(buf))


def make_flat_ctts1(src, dst):
    """Rewrite the video track's ctts to ONE constant entry, same box size.

    A ctts entry is (sample_count, sample_offset); one entry covering every sample with a
    constant offset is exactly the defect: PTS == DTS + const for the whole stream.
    The byte difference is absorbed by a `free` box so the moov size is unchanged and the
    chunk offset tables stay valid.
    """
    buf = bytearray(open(src, "rb").read())
    hit = find_box(bytes(buf), b"ctts")
    if not hit:
        raise SystemExit("selftest: no ctts in %s - cannot build the flat_ctts1 fixture" % src)
    off, size = hit
    ver = buf[off + 8]
    n = struct.unpack(">I", buf[off + 12:off + 16])[0]
    total, first_off = 0, 0
    for i in range(n):
        c, o = struct.unpack(">Ii" if ver == 1 else ">II", buf[off + 16 + 8 * i:off + 24 + 8 * i])
        if i == 0:
            first_off = o
        total += c
    new = struct.pack(">I", 24) + b"ctts" + bytes([ver, 0, 0, 0]) + \
        struct.pack(">I", 1) + struct.pack(">Ii" if ver == 1 else ">II", total, first_off)
    pad = size - len(new)
    if pad < 8:
        raise SystemExit("selftest: ctts too small to patch in place (%d bytes)" % size)
    filler = struct.pack(">I", pad) + b"free" + b"\x00" * (pad - 8)
    buf[off:off + size] = new + filler
    open(dst, "wb").write(bytes(buf))
    return total


def main():
    if not (shutil.which(FFMPEG) or os.path.exists(FFMPEG)):
        raise SystemExit("ffmpeg not found - set TRANSCODE_FFMPEG or put ffmpeg on PATH")
    d = tempfile.mkdtemp(prefix="tctest-")
    print("work dir: %s\n" % d)
    try:
        src = os.path.join(d, "src.mp4")
        # B-frames are required: without reordering there is nothing to get wrong, and the
        # detector is correct to stay silent.
        r = run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc=size=320x240:rate=25", "-t", "3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-bf", "3",
                 "-pix_fmt", "yuv420p", src])
        if r.returncode != 0:
            raise SystemExit("ffmpeg could not build the source: %s" % r.stderr)

        hbf = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=has_b_frames", "-of", "csv=p=0", src]).stdout.strip()
        if hbf in ("", "0"):
            raise SystemExit("fixture has no B-frames (has_b_frames=%r) - test is meaningless" % hbf)

        raw = os.path.join(d, "raw.h264")
        flat_no = os.path.join(d, "flat_noctts.mp4")
        run([FFMPEG, "-v", "error", "-y", "-i", src, "-c", "copy",
             "-bsf:v", "h264_mp4toannexb", "-f", "h264", raw])
        run([FFMPEG, "-v", "error", "-y", "-fflags", "+genpts", "-r", "25",
             "-i", raw, "-c", "copy", flat_no])

        flat_c1 = os.path.join(d, "flat_ctts1.mp4")
        total = make_flat_ctts1(src, flat_c1)

        mkv = os.path.join(d, "src.mkv")
        run([FFMPEG, "-v", "error", "-y", "-i", src, "-c", "copy", mkv])

        cases = [
            (src,     False, "healthy MP4, proper ctts"),
            (mkv,     False, "Matroska - no ctts by design, must NOT be flagged"),
            (flat_no, True,  "MP4 with NO ctts box (PTS == DTS)"),
            (flat_c1, True,  "MP4 with a single constant ctts entry (PTS == DTS + const)"),
        ]

        print("ctts table: %d samples folded into one constant entry\n" % total)
        print("%-34s %-8s %-8s %s" % ("fixture", "expect", "got", "verdict"))
        bad = 0
        for path, expect, note in cases:
            info = T.probe(path)
            got = T.flat_timing(info, path)
            ok = (got == expect)
            bad += 0 if ok else 1
            print("%-34s %-8s %-8s %-5s %s"
                  % (os.path.basename(path), expect, got, "ok" if ok else "FAIL", note))

        # The detector must also drive the right DECISION, not just a boolean: a flat file
        # cannot be fixed by -c copy, so plan_for has to choose a real re-encode.
        print()
        for path, want_encode, note in ((src, False, "healthy -> copy is safe"),
                                        (flat_c1, True, "flat -> must re-encode")):
            vmode, _, why = T.plan_for(T.probe(path), path, "auto")
            must_reencode = vmode in ("x264", "hevc")
            ok = (must_reencode == want_encode)
            bad += 0 if ok else 1
            print("%-34s plan=%-6s %-5s %s"
                  % (os.path.basename(path), vmode, "ok" if ok else "FAIL", note))
            print("%-34s   reason: %s" % ("", why))

        print("\n%s" % ("ALL ASSERTIONS HELD" if not bad else "%d ASSERTION(S) FAILED" % bad))
        return 1 if bad else 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
