# STATE — 樊登读书会媒体库转码（2026-10-07/08）

给下一个会话的交接。**权威运行器：`scripts/library_run.py`**（不要再恢复旧的 `hevc_lib.py`）。

## 现在在跑什么

```bash
# 两个档并行（各自独立的锁：run-x265.lock / run-nvenc.lock），GPU 与 CPU 互不争抢
cd /d/dev/universal-video-transcode
python -u scripts/library_run.py --tier x265  --origin-root "Z:\Class\#snapshot\GMT+08-2026.09.25-09.50.20\樊登读书\樊登读书会（每周更新）" --reverse >> /d/tmp/xcode/run_x265.log 2>&1
python -u scripts/library_run.py --tier nvenc --origin-root "Z:\Class\#snapshot\GMT+08-2026.09.25-09.50.20\樊登读书\樊登读书会（每周更新）" --reverse >> /d/tmp/xcode/run_nvenc.log 2>&1
```

- **copy 档已整轮完成**：`=== done: ok=193 skipped=389 failed=0`（视频逐位不变 + 音频转 AAC）。无需再跑。
- 预期：x265 ~3.5 天、nvenc ~3 天跑完全库（582 文件 / ~324 GB）。
- 断点续跑天然幂等：已转好的文件（HEVC 且 ≤ 原档大小）自动跳过；失败项文件未动，下一轮自动重做。

## 目录与日志

| 用途 | 路径 |
|---|---|
| 运行日志 | `/d/tmp/xcode/run_x265.log`、`run_nvenc.log`、`run_copy.log` |
| 单文件日志 | `/d/tmp/xcode/run_single.log` |
| 暂存目录（编码/闸门 raw） | `C:\AI\transcode-run\stage`（C: 盘，要留 ≥8 GB） |
| 台账（每项一行 JSON） | `C:\AI\transcode-run\ledger.jsonl` |
| 锁（每档一个 + 心跳） | `C:\AI\transcode-run\run-<tier>.lock` |

## 本轮修掉的 bug（按时间顺序，全部已提交推送）

1. **闸门判据太粗糙** → 改"缺陷形状"判：`mean < 0.98` 或 `>1% 帧 <0.85` 才判错，并**剔除近平坦帧**（SSIM 在黑场/淡入淡出上无意义；实测：好编码 0/1500、误判文件 0.33%、真缺陷 2.9%）。
2. **闸门用输入 seek 解码** → 改**两侧顺序解码**（open GOP 源从中间 seek 会解出错误画面：同一文件与自身比对 min 0.25）。
3. **闸门 raw 文件固定名** → 三个档并行时互踩（实测报出"100% 帧不符"的假缺陷）→ 改 **PID + tag** 命名。
4. **安装只用 ffprobe 元数据复核** → RaiDrive 返回**缓存属性**，rename 没落地也说成功（实测"ok 1085→478 MB"而线上仍是原档）→ 改**安装后回读哈希**。
5. **源拷贝只校验字节数** → 加 **SHA-256 比对共享盘副本**（长度对内容错是真风险）。
6. **probe 单次失败即判文件失败** → 加**重试 3 次**。
7. **看门狗用 stderr 管道**（本轮最贵的 bug）→ ffmpeg `-stats` 每 0.5 s 一行，64 KB 管道几分钟塞满 → ffmpeg 阻塞在 write() → 输出不增长 → 看门狗**误杀好编码**（整夜 ~270 杀、只转成 24 个、GPU 0%）。修：**stderr 写文件**，阈值 5→15 分钟。自检脚本 `D:\dev\hoelee-tubesync-management\cache\watchdog_selfcheck.py`。
8. 看门狗**保留**（它要防的是真实场景：用户更新 NVIDIA 驱动会把在跑的 NVENC 冻死 1.5 小时且不报错）。

## 未完成 / 待办

- [ ] 两档跑完 → **全库落地审计**：`D:\dev\hoelee-tubesync-management\cache\install_audit.py`（核对线上文件真实大小 + 编码器；曾一次抓到"报告成功没落地"）+ `copy_land_check.py`（copy 档用音轨 aac/mp3 判定）。
- [ ] 失败项：无需清单，新一轮自动重做（失败时文件原样）。
- [ ] `2022年/0723 《走出强迫症》/走出强迫症.mp4` 用新下载（`D:\Download\0723走出强迫症.mp4`，618,696,954 B）已转好装好（450,502,893 B，HEVC+AAC，SSIM min 0.997，时长 3399.81 s）✓。**快照里那份仍是坏的**（618,697,011 B，moov 缺失）→ 待定：是否用干净副本替换快照里的坏档，以免将来"从原档重转"踩坑。
- [ ] 目标文件夹里曾出现一个 0 字节的 `0723走出强迫症.mp4`（现已不在）—— 若是用户自建请告知命名约定。
- [ ] 全库唯一真损坏：`走出强迫症.mp4` 的原档（上面已处理）；其余 573 个可读文件截断审计 0 可疑。

## 用法速查

```bash
# 单文件（任意位置）转好并装回库，走同一套分档/闸门/回读
python scripts/library_run.py --path "D:/Download/x.mp4" --dest "Z:/.../x.mp4" --keep-source

# 只审计不落地（本地编码+闸门，输出丢弃）
python scripts/library_run.py --tier x265 --limit 3 --keep-local

# 分档预览
python scripts/library_run.py --dry-run --limit 20
```

## 红线（用户明确要求过）

- **绝不删除/覆盖快照原档**（`#snapshot` 是唯一的干净档案）。
- 失败时**不动 live 文件**；只有"结构 + 全解码 + 图片闸门 + 上传回读 + 安装回读"全过才替换。
- 杀进程**只按 PID**（绝不用进程名批量杀）；一档一个锁；同一档不要跑两个实例。
- 动 GPU（更新驱动/跑 ComfyUI/加载大模型）前先暂停 nvenc 档。
