# 实测证据与坑（universal-video-transcode）

每条都是在一个真实媒体库上量出来的，正文只留必须每次记住的铁律。

- **A RaiDrive/Dokan write can return with NO error while the file is a fraction of what it should be.** Measured: a 403 MB upload left 12.5 MB on the share, and RaiDrive's own "the operation didn't complete within specific time limit" toast arrived *after* the write had already returned success. Never trust a write to a Dokan-backed share: hash the local file, hash the share copy back, compare size AND SHA-256, and only then rename. On a mismatch, retry the upload (it usually works on the second attempt) rather than failing the item. Uploads should use small chunks (4 MB) with a periodic `flush`+`fsync`; a partial `*.new.mp4`/`*.part.mp4` must be deleted, never resumed from `os.path.getsize()` on the share (RaiDrive reports stale sizes, so a resume offset taken from it corrupts the file).
- **Those same toasts are an overload signal, not a permission problem.** Three jobs reading/writing one share provoked both the truncated write above and `[Errno 9] Bad file descriptor` on a 1 GB staged read; writing into the library folder itself was perfectly fine (verified by creating and deleting a file there). Cap the concurrency at two jobs (one CPU tier + one GPU tier), give each tier its own lock file, and make the lock take over a stale one (pid gone).
- **Compare pictures by TIMESTAMP, never by frame index after an input seek.** An input `-ss` snaps to a keyframe, and it snaps to a *different* keyframe for the source and for the encode - measured: two 1500-frame decodes whose start times differed by ~10 s, so every frame met the wrong one and a perfectly good encode scored min SSIM 0.25. Cut the window with the `trim` filter on absolute PTS (`-copyts -ss <before window> -i in -vf "trim=start=X:end=Y,setpts=PTS-STARTPTS,..."`). Validate the method by comparing a file against ITSELF: it must read lo=1.0/mean=1.0, otherwise the harness is broken and every verdict it produces is noise.
- **If the frame counts in the sampled window differ by more than ~1%, the picture gate is NOT APPLICABLE - do not fail the file.** A source with holes in its timeline (measured: 1459 frames where 1500 belong, a 1.7 s hole) is *supposed* to gain duplicated frames from `-fps_mode cfr`; after the hole every frame pairs with the wrong one and SSIM collapses. Report the condition and let the structural/decode/size gates carry the verdict.
- **File paths with a space before the extension are NOT the problem they look like** - `1130 维特根斯坦十讲 .mp4` opened and streamed fine. Suspect the share, not the name.
- **Never kill encoders by process name.** `Get-Process ffmpeg | Stop-Process -Force` also killed an unrelated pilot encode that was running legitimately (rc=-1 mid-encode). Kill the specific background PID, or the specific `ffmpeg` PID you started.

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

- **A `MemoryError` out of a share write is not an edge case - it orphans the staged file.**
  Measured 2026-10-09 11:19: the host hit **0 GB physical free and 76.8 GB of a 79.6 GB commit
  limit** while both tiers ran (Chrome ~4 GB, Hermes ~3.7 GB, upscayl 1.2 GB, whisper-server
  0.8 GB, Discord 0.8 GB; the pagefile is capped at 32 GB on a **97 %-full D:** so it cannot grow).
  The Dokan write of the batch's largest output raised `MemoryError` - and `except OSError` does
  not catch that, so the item was logged `!! FAIL MemoryError:` with an **empty message** and the
  *complete* 493 MB `<live>.new.mp4` stayed on the share. Eight such orphans had piled up (7 of
  them truncated prefixes from earlier aborted uploads).
  Rule: treat `MemoryError` as a sibling of `OSError` in **every** share helper, and have the
  per-item handler delete `<dest>.new.mp4` **unconditionally** - never gate that delete on
  `os.path.exists()`, whose answer on this share can be stale.
