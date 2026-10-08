# run/ — the operational launchers for the 樊登读书会 library

These are the **source of truth**; the live copies actually driving the job are in `/d/tmp/xcode/`
(same content). Edit here, then copy out to `/d/tmp/xcode/` and relaunch.

| script | what it does |
|---|---|
| `relaunch_tiers.sh` | Starts the two HEVC tiers (`x265` = CPU/E-cores, `nvenc` = GPU) **detached from Hermes** so ending a session cannot kill them, with up to 8 retries each. Idempotent: converted files are skipped, failed items are retried with the live file untouched. |
| `convert_new4.sh` | The newest weekly episodes that the copy pass never saw (H.264 + **mp3** audio, one actually MPEG-TS). Runs them through the single-file path (`--tier copy --path <file> --keep-source`) because a full `--tier copy` pass would redo all 193 already-converted copy-tier files. |

Why detached: on 2026-10-08 10:37 both tiers — launched as Hermes background processes with
`persist_on_release` — were killed mid-encode when the user's session was closed (no `PASS-DONE`
in either log, `*_out.mp4` and `.err` frozen in the stage dir). `powershell Start-Process` gives the
job an orphaned process tree that Hermes' cleanup cannot reach. Consequence: no exit notifications
(watch the logs) and **stop it by PID only** — `taskkill /IM ffmpeg.exe` would also kill an unrelated
encode.

```bash
bash /d/tmp/xcode/relaunch_tiers.sh      # relaunch / resume both tiers
tail -f /d/tmp/xcode/run_x265.log /d/tmp/xcode/run_nvenc.log
```
