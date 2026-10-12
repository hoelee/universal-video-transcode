# STATE — 樊登读书会媒体库转码（2026-10-09 11:40 更新）

给下一个会话的交接。**权威运行器：`scripts/library_run.py`**（不要再恢复旧的 `hevc_lib.py`）。

## 2026-10-11：画质策略落地（闸门对齐 / 门槛 / VMAF）—— 先读这一节

**结论：闸门以前把帧配错了对**，靠它判死过 14 个健康编码（9.6 h 里烧掉 12.4 h lane 时间、13 项被 park）。
完整细节、验收数字与决策台账（D-01…D-06）在队列仓 `docs/QUALITY-POLICY-PLAN.md`；本节只记引擎侧改了什么、怎么复现。

**改了什么**（`scripts/library_run.py`，commit `7d16fed`）

1. **偏移搜索**（`ssim_vs_source`）：原来是 `range(-100, 101, 10)` —— 步长 10，**永远试不到 ±1/±2**；而本库实测位移是恒定
   **−1 帧**（`-fps_mode cfr` 在丢帧处插重复帧；1499/1500、1495/1500 帧都是 −1）。现在三段：**细搜步长 1、±6 帧** →
   粗搜步长 10、±100（原样保留）→ **逐帧 ±2 最佳匹配**（`ALIGN_FINE` / `ALIGN_PERFRAME`）；命中即停，判定行写明走了哪一段。
2. **负偏移方向修好**：旧代码 `f.seek(max(0, off) * fs)` 只把参考往前挪，于是**所有负偏移与 off=0 同分** ——
   日志里那句 "verified at −100 frames" 其实什么都没对齐。
3. **临时文件带 PID**（两条 lane 共用暂存目录，固定名会互踩）；`pair_*.log` 解析完即删（否则全库一轮攒出 GB 级垃圾）。
4. **窗口解码重试一次 + 保留 ffmpeg stderr**（进 `stats["decode_error"]`，由 FAIL 信息带出）：一次偶发解码失败曾白扔一个
   已经跑完的 16.7 分钟编码，而旧代码把报错丢掉、只剩 `FAIL ssim: no frames compared`。闸门解码另用
   `GATE_STALL_S = 3600`（`trim` 到窗口前本来就不写任何字节，用编码器的 900 s 阈值会误杀）。
5. **两处 SSIM 滤镜加 900 s 超时** —— 卡死的 ffmpeg 不再能永久堵住一条 lane（今天确有一个空转烧 4500 s CPU 的例子）。
6. **门槛**：`SSIM_MEAN_MIN 0.98 → 0.995`、`SSIM_MEAN_MIN_REDO 0.975 → 0.990`、新增 `SSIM_MEAN_MIN_FALLBACK 0.990`
   （业主释放项走第二档，原先是让 mean **完全失效**）；per-frame floor 0.985 与坏帧占比 ≤1% 不变。
   触发对齐搜索的条件也改成按**实际门槛**（原硬编码 `mean < 0.985`，门槛一升就会出现"没触发搜索就判死"的新冤案）。
7. **对齐说明不再绕过判定**：它原来走第 4 个返回值（调用方当作 `irreg` = 时间轴不规则 → **跳过整条画面判定**），
   canary 与两个 x265 项就是这么通过的（mean 门槛从没生效）。现在只进 `stats["align_note"]` + 日志，门槛照常执行。
8. **VMAF observe-only**（`VMAF_OBSERVE`）：在同一对齐偏移上算，判定行带 `vmaf480 mean/min` —— **480p 尺度**；
   同一窗口原生读 95.85/93.76、480p 读 98.27/96.97（+2.4/+3.2），所以标了尺度；**从不参与判据**。

**怎么复现验收**（队列仓内，只用本地缓存产物，不碰库）

```bash
python cache/lean_verify.py           # sign 双向 / 旧值 0.9808 复现 / 细搜找到 −1 / 错画面簇仍被拒
python cache/vmaf_native.py --original <快照原档> --converted <线上档> --offset -1   # 原生分辨率 VMAF 标定
```

**实测锚点**：`危机领导力` 索引配对 0.9808 → 对齐 **0.9976**；`爆款` 0.9862 → **0.9974**；canary `上瘾` 0.9662 → **0.9968**
（装盘 + 安装后回读 SHA-256 一致）。
⚠ 比较 mean 必须同帧数：偏移搜索上限 1200 帧 —— 同一配对 1500 帧读 0.9808、1200 帧读 0.9832。
⚠ 原生 ffmpeg/ffprobe 读不了 CJK 路径（`Illegal byte sequence`），复核线上档要用 Python `subprocess` 传路径。

**现在由队列仓控制**（`D:\dev\hoelee-resource-transcode`：supervisor 看板 8790 + 三 lane）。2026-10-11 13:35 业主恢复转换，
cpu + gpu 两条 lane 在跑（`autostart_lanes=[cpu,gpu]`）。**本仓不要自己起编码** —— 引擎只作为 lane 的子进程被调用，
单档启动器（`run/relaunch_x265.sh` 等）只在看板不参与时应急用。

## 现在在跑什么

**只有 x265 一档在跑**（2026-10-09 20:51:41 起，单档启动器 `run/relaunch_x265.sh`）。
**nvenc 档故意不启动** —— GPU 被 TubeSync 的 58 个文件修复批次占着（20:36 起，与铁律一致：同一颗编码器不能两个任务）。

```bash
# 只起 x265（E 核，纯 CPU）—— 仓库源 run/relaunch_x265.sh 拷到 /d/tmp/xcode/ 再跑
powershell -NoProfile -Command 'Start-Process -FilePath "C:\Program Files\Git\bin\bash.exe" -ArgumentList "D:/tmp/xcode/relaunch_x265.sh" -WindowStyle Hidden'
# ⚠ 参数必须只有脚本路径；写成 -ArgumentList "-lc","bash <脚本>" 会起一个交互式 bash，什么都不跑
tail -f /d/tmp/xcode/run_x265.log
```

### 2026-10-09 16:51:58：两档是**整机重启**停的，不是 bug

（两档都不在 Hermes 的进程管理里 → `process_manage` 看不到，也没有退出通知：只能看日志，按 PID 杀。）
`LastBootUpTime = 2026-10-09 16:51:58`；x265 日志停在 16:50:42（404/589）、nvenc 停在 16:51:30
（308/589）。11:48 由 relauncher 重新拉起、跑到重启为止。锁文件里那两个 PID（23792 / 50128）已是死号 ——
`transcode.Lock` 是**过期感知**的（age > 300 s 且 pid 已死即接管），不需要手工清锁。

重启后内存正常：物理空闲 12.9–13 GB、提交 50.3/55.9 GB（11:19 那次 MemoryError 时是 96.5%）。
⚠ 但提交上限从 79.6 GB 缩到 55.9 GB：D: 已 92% 满，pagefile 长不上去。

### 为什么改成"脱管"启动 —— 10:37 那次全停的教训（第 9 个 bug）

上一轮的 x265 / nvenc 是 Hermes 后台进程。**用户停会话时它们被一起杀了**：两个都停在编码到一半
（stage 里 `*_out.mp4` 和 `.err` 停在 10:37，日志里没有 `PASS-DONE`），所以 `persist_on_release` 在这个
环境里**不可靠**。现在两档由 `powershell Start-Process` 拉起的独立 bash 启动，父进程立刻退出 →
进程树成为孤儿，**Hermes 的清理碰不到它们**。

代价与规矩：没有退出通知（看日志）；**停/杀只能按 PID**，绝不用进程名批量杀。

## 进度（2026-10-08 10:50，库内共 589 个文件）

| 档 | 已转好 | 待转 |
|---|---|---|
| copy | 160 | 4（见下） |
| x265 | 13 | 169 |
| nvenc | 25 | 174 |
| 无需处理（已达播发标准） | 44 | — |
| **合计** | **242** | **347** |

- 预期：x265 ~3.5 天、nvenc ~3 天跑完各自那档（这一轮 10:43 才重新起来，10:37 被上面那次全停打断）。
- 台账：`C:\AI\transcode-run\ledger.jsonl`（每项一行：分类/结果/大小/SSIM）。
- 进度只认台账与 state，**不看 `.mp4` 扩展名**（TubeSync 重命名会把它改回 `.mkv`）。

## 2026-10-08 新增的 4 集"每周更新"（copy 档）

`1004 反内卷` `0912 感官觉醒` `0905 如何成为情绪稳定的父母` `0822 超赞的一代`（都在 `樊登读书2026年（更新中）`）：
H.264 + **mp3** 音轨，其中 `0822 超赞的一代` 其实是 **MPEG-TS** 只是挂着 .mp4 名 → iPad 播不了，
需要 copy 档（视频逐位不变 + 音轨转 AAC）。

⚠ **不要跑整轮 `--tier copy`**：copy 档唯一的跳过判据是"已经是 HEVC"，而 copy 的产物是 H.264 ——
整轮会把 193 个已转好的 copy 文件全部重做一遍。改用单文件通道：

```bash
bash /d/tmp/xcode/convert_new4.sh     # 逐个 --tier copy --path <file> --keep-source，共用 run-copy.lock
```

- `1004 反内卷` 10:48:53 完成 ✓（535 MB → 535 MB，视频 100% 逐位不变，音频转 AAC，149 s，rc=0）

## 闸门策略文件（业主决定 2026-10-10）：`C:\AI\transcode-run\gate_policy.json`

8 个项在 nvenc 档里反复失败、**每轮白烧 ~3.3 GPU 小时**（初始一次 + quality+2 一次），而且永远过不了：它们的
mean 差 0.2–1.7 个千分点（0.9626–0.9778），但**帧占比只有 0.20–0.93%** —— 按本仓库自己的注释，抓真缺陷的是
**帧占比 >1%** 那条，mean 那条只是在切一条连续分布。

```json
{
 "accept": [ "7 个项（帧占比 ≤1%）的库内相对路径" ],
 "park":   { "8 个项": 1 }
}
```

- `accept`：名单内的项以**帧占比测试**为准，mean 低于门槛只记录不拦截。**帧占比 >1% 的项即使列在名单里也照样拒**
  （《2020年\0912 洞见》1.33% 故意不在名单里）。
- `park`：每项**失败轮数**；达到 `PARK_AFTER = 2` 就 **SKIP**（不再重编码），要重试就把它的条目删掉。
  没有这条，同样的 8 个项每趟都重演一次、永远转不出来。
- 生成器：`scripts/build_gate_policy.py`（键**逐字取自日志**的 `--- i/n <相对路径>` 行，不手抄）。
- 停/起的单档启动器：`run/relaunch_nvenc.sh`（`relaunch_tiers.sh` 会连带拉起 x265，别用它单独起 GPU 档）。

## 图片闸门：re-do 用 0.975（业主决定 2026-10-08）

第一轮实测：一趟里 11 次编码**只差在 mean**（0.9655–0.9795）就被拒，而被接受的那些帧最低分反而更差
（0.33–0.90）—— 阈值在切一条连续的分布，不是在分开缺陷（`0718 马斯克原理`：min 0.887、**0.00%** 帧
<0.85，照样被拒）。被拒的文件本来就已能播（HEVC hvc1 + AAC），re-do 的唯一目的是清掉旧设定的膨胀；
而要够到 mean 0.98 得花掉超过源片自身的码率，会被 size ceiling 拦住 —— 结果是文件连同膨胀被永久搁置，
而且每一趟还白烧一次编码。

新规则（`library_run.SSIM_MEAN_MIN_REDO = 0.975`）：**只有**"重做一个已经在播的文件"才用 0.975，条件为
live = HEVC hvc1 **且**音轨是 AAC（真能播）**且**本次是从快照原档重做；首次转换仍用 0.98。
帧占比判据（>1% 帧 <0.85）两档都不变 —— 它才是抓真缺陷的那条：`失控`（0.9655）仍会被拒，
`0808 陌生人效应`（0.9739）因为 live 是 H.264、根本不是 re-do，也仍走 0.98。
先在一个文件上验证（`0321 随他们去`，上一轮 mean 0.9789 被拒）→ 见 `/d/tmp/xcode/run_pilot.log`。

## 目录与日志

| 用途 | 路径 |
|---|---|
| 运行日志 | `/d/tmp/xcode/run_x265.log`、`run_nvenc.log`、`run_copy.log`、`run_new4.log` |
| 脱管启动器 / 新集转换 | `/d/tmp/xcode/relaunch_tiers.sh`、`convert_new4.sh`（+ `relauncher.out`）；**源在仓库 `run/`**（改仓库那份再拷出来） |
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
7. **看门狗用 stderr 管道**（本轮最贵的 bug）→ ffmpeg `-stats` 每 0.5 s 一行，64 KB 管道几分钟塞满 → ffmpeg 阻塞在 write() → 输出不增长 → 看门狗**误杀好编码**（整夜 ~270 杀、只转成 24 个、GPU 0%）。修：**stderr 写文件**，阈值 5→15 分钟。自检脚本 `D:\dev\hoelee-resource-transcode\cache\watchdog_selfcheck.py`。
8. 看门狗**保留**（它要防的是真实场景：用户更新 NVIDIA 驱动会把在跑的 NVENC 冻死 1.5 小时且不报错）。
9. **Hermes 后台进程会随会话一起被杀** —— 两个档在 10:37 同时死在编码中，日志里没有 `PASS-DONE`；改脱管启动（见上）。

## 未完成 / 待办

- [ ] 两档跑完 → **全库落地审计**：`D:\dev\hoelee-resource-transcode\cache\install_audit.py`（核对线上文件真实大小 + 编码器；曾一次抓到"报告成功没落地"）+ `copy_land_check.py`（copy 档用音轨 aac/mp3 判定）。
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

## 第 10 个 bug：MemoryError 会在共享盘上留孤儿（2026-10-09）

现象：11:19:31 与 11:19:37 连续两次 `!! FAIL MemoryError:`（消息空白），两次相隔 6 秒 ——
一次是《崇祯传》**装盘**（写 493 MB 到 RaiDrive），一次是下一项**读源**。

根因是**整机内存/提交额度耗尽**，不是文件问题：

```
物理可用 0.0 GB / 48 GB         已提交 76.8 GB / 上限 79.6 GB（96.5 %）
pagefile d: 初始 8 GB / 上限 32 GB   —— 但 D: 只剩 19 GB（97 % 满），长不上去
大户：Chrome ≈4 GB、Hermes ≈3.7 GB（3 个进程）、upscayl 1.17 GB、whisper-server 0.83 GB、Discord 0.76 GB
```

`copy_*` / `_sha_remote` 当时只 `except OSError`，**而 `MemoryError` 不是 OSError** → 直接冒到
per-item 兜底；删半截 `remote_tmp` 的清理写在 `except OSError` 里 → **半截/完整的 `.new.mp4` 留在共享盘上**。

已修（本轮，`scripts/library_run.py`）：
1. `_sha_remote` / `copy_remote_to_local` / `copy_local_to_remote` 的 `except OSError` → `except (OSError, MemoryError)`（瞬时抖动变重试，不是 FAIL）；
2. `copy_local_to_remote` 内的清理、读回不符时的清理同样放宽；
3. **per-item 兜底加了无条件删除 `<dest>.new.mp4`**（用 `os.remove` + 忽略 `FileNotFoundError`，**不用** `os.path.exists`，因为该共享盘的元数据可能是陈旧的）。

实测（注入 MemoryError）：上传路径不再抛异常、半截文件被删；读源路径重试后抛干净的 `IOError`；
`_sha_remote` 重试 2 次后抛出。

**内存仍是硬前提**：物理可用 0 GB / 提交 96.5 % 时重启，第一项照样会 FAIL（现在至少不留残留）。
重启前建议：关掉 upscayl / 多余 Chrome 标签 / Discord 腾出 5-6 GB；pagefile 上限别放在 97 % 满的 D:。

## 8 个 `.new.mp4` 残留（2026-10-09 11:35 逐个探过）

原档都在，没丢东西。7 个是**半截前缀**（内容早停 → 没用，删掉让下一轮重做）；
**只有一个是完整的、可以救回来**：

| 文件 | 暂存 | 判定 |
|---|---|---|
| 2022年/0115《崇祯传》 | 493 MB | ✅ **完整**（3616.8 s 与源一致、内容到 3609 s）→ 校验后可改名安装 |

### 已处理（2026-10-09 12:10）

- **《崇祯传》已安装**：先过全部门才改名 —— 暂存档拷回本地并回读比对哈希（493,366,137 B，与共享盘副本一致）
  → 结构闸门 True → **全解码 True**（唯一能证明没截断的检查）→ 图片闸门 **min 0.9980 / mean 0.9988（1500 帧，
  0 帧 < 0.85，远高于 0.98 的杆）** → `os.replace` 改名 → 安装回读 `sha256 5a903ab8a3d1`（493,366,137 B == 本地）
  → 替换后闸门 hevc/aac、tag=hvc1、dur 3616.8 ✓。**省下一次 3616 秒的重编码**（822,288,651 B h264 → 493,366,137 B HEVC）。
- **7 个半截残留已删**（8 MB–165 MB，都是内容早停的前缀）：0511原生家庭、20190601被管教的勇气、0622人生海海、
  0807东京传、0527 洪业、» 0819 李白、1230 不安的哲学。**它们对应的 live 原档一个没动**，下一轮自动重做。
- 共享盘上 `*.new.mp4` 现在 **0 个**。
| 0622人生海海 / » 0819 李白 / 0527 洪业 / 0807东京传 / 20190601被管教的勇气 / 1230 不安的哲学 / 0511原生家庭 | 8 MB–165 MB | ❌ 半截，删 |

## 第 12 个 bug：解除 park 被"正在跑的引擎"改回去（2026-10-11）

现象：13:47 解除了 `0705机械宇宙` 的 park 条目（业主"降参数"决定），13:59 这一项还是被 **SKIP parked: 2 rounds**，
而且 `0808 陌生人效应` 的条目莫名出现。

根因：`save_policy()` 把**内存里的整份快照**写回文件。13:35 起跑的那个引擎进程在 13:35 读的 policy 里
`机械宇宙` 还是 2；它自己的项在 13:5x 失败时就把快照整份写回 → 外部刚删掉的条目复活（并顺手写入自己的 1）。

修（`scripts/library_run.py`）：`save_policy` 改成**读-改-写**——先重新读盘，再只覆盖本进程真正 +1 的计数
（`POLICY_DIRTY`），`accept` 一律以盘上为准。回归：`scripts/test_policy_merge.py`（4 项：放行项保持放行 / 外部新增
accept 保住 / 计数落盘 / 旧行为确实会把它复活）。

⚠ 顺序仍然重要：**解除 park 前先 `pause-after` 把 lane 停到 item 边界**，别指望"改得够快"。

## 第 11 个 bug：pythonw 下每个 ffmpeg/ffprobe 子进程弹一个终端窗口（2026-10-11）

现象：转换一跑，屏幕时不时冒出一个命令行/终端窗口，一项里出现多次。

根因：队列把引擎（`library_run.py`）以 **`pythonw.exe`** 启动 —— GUI 子系统，**完全没有控制台**。
Windows 的规则是：控制台子进程若父进程没有控制台，系统就**新建一个控制台并显示出来**。
所以引擎里每一次 `ffprobe`/`ffmpeg` 调用（probe、编码、闸门的 SSIM/pair/VMAF）都弹一个窗口；
Win11 默认终端是 Windows Terminal，所以看到的是"终端窗口"（类名 `CASCADIA_HOSTING_WINDOW_CLASS`），不是老式 cmd 窗口。

修（`library_run.py` + `transcode.py`）：每个 `subprocess` 调用都带 `creationflags=CREATE_NO_WINDOW`
（`transcode.run`、`library_run.run`、`run_watched`、以及三处直接调 ffmpeg 的闸门助手）。
队列仓的 `scripts/tsconv.py` / `scripts/tsfix.py` 同病同修（各加一个默认带 flag 的 `run()` 包装）。

验证（队列仓 `scripts/nowin_popup_launch.py` → `nowin_popup_test.py`）：必须用**没有控制台的父进程**
（从 shell 起 pythonw 会继承那个 shell 的控制台 → 假阴性），脚本先断言 `GetConsoleWindow()==0`，
再数可见的 `ConsoleWindowClass` + `CASCADIA_HOSTING_WINDOW_CLASS`：裸 `Popen` 会多出 1 个窗口，
带 flag 的（含全部 patched helper）保持 0。实测 2026-10-11：0→1 / 0 / 0 / 0 / 0 / 0。

## 第 13 个 bug：闸门 raw 没人收，暂存目录越堆越满（2026-10-12）

现象：`C:\AI\transcode-run\stage` 里积了 6 个 291 MB 的 `ssim_<pid>_<tag>_{ref,out}.yuv` + 123 个 `ssim_<tag>.log`。
正常收尾只删 `<tag>_src`/`_out.mp4`，**闸门的 raw / pair / vmaf / stats 日志没人删**；被暂停、重启、看门狗杀掉的项
留下的文件更是永远没人收。加上项目历史遗留的 45 GB 测量垃圾（`C:/AI/transcode-run/*` 与 `D:/tmp/*`），
work 盘只剩 3.9 GB → 队列守卫拒绝所有新项，两条 lane 空转 30+ 分钟。

修：
1. `process()` 的 `finally` 加 `purge_item_artifacts(tag)` —— 按 tag（stage hash）与 pid 删本项的 `ssim_/pair_/vmaf_` 产物，
   另一条 lane 的文件名字里没有这些 key，绝不会误删。
2. 新增 `sweep_stage()`，每次启动扫一遍 stage：只删老于 `STAGE_MAX_AGE_H`(6 h，`RT_STAGE_MAX_AGE_H` 可覆盖) 的残留，
   并且**整条 tag 跳过**（只要有新鲜的 `<tag>_out.mp4.err` = 那一项正在编码）。
3. 队列侧 `rt/janitor.py`（supervisor 每 30 分钟 + **守卫失败时立刻**清一次再复检）用同一套规则把 stage 和
   `cache/runs/*.log` 收干净；`ledger*`/`*.lock`/`tsconv_state*`/`queue.sqlite*` 永不碰。

回归：`scripts/test_stage_cleanup.py` 6 项（另一 lane 不动 / 死残留删 / 在编 tag 保全 / 台账不动 / 字节数正确）。

## 红线（用户明确要求过）

- **绝不删除/覆盖快照原档**（`#snapshot` 是唯一的干净档案）。
- 失败时**不动 live 文件**；只有"结构 + 全解码 + 图片闸门 + 上传回读 + 安装回读"全过才替换。
- 杀进程**只按 PID**（绝不用进程名批量杀，`taskkill /IM ffmpeg.exe` 会连带杀掉别人的编码）；一档一个锁；同一档不要跑两个实例。
- 动 GPU（更新驱动/跑 ComfyUI/加载大模型）前先暂停 nvenc 档。
