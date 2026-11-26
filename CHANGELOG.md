# 更新日志

版本号规则：`v0.x.y` 递增，统一用 `git tag` 打标签；exe 只在明确要求时才上传到 GitHub Releases。

## 未发布（待确认后再定版本号/tag）

- **虎牙改回 HLS 引擎 + 分片先落临时目录、合并后才进保存目录**（用户要求「按虎牙原始几秒一片，用文本记录分片，分片先存临时文件，合并之后再放进保存路径」）：
  - `liverec/huya.py::_candidates()` 排序改回 HLS 优先、FLV 直连兜底；`resolve()` 的 CDN 轮转仍只在同一格式内进行。
  - 开「分段录制」时 ffmpeg 用 `-f segment -segment_time 4` 把 HLS 的几秒一片写进临时目录 `<保存路径>\merge-tmp\<场次名>\`（`.ts` 分片，TS 直接 concat 最稳），续段时用 `-segment_start_number` 接着编号、前缀跨续段稳定。
  - 录制循环每 5 秒 `_flush_ready_shards()`：攒够「单段时长」就把一批分片合并成 `<名字>_partNNN`，先在临时目录出 TS，再按用户选的格式转一次封装，最后 `os.replace` 挪进保存目录并删掉已合并的分片；整场结束 `_finalize_shards()` 把剩下的并成最后一个文件、清空并删除临时目录。
  - 保存目录留下两份清单：`<名字>.index.txt`（成品清单：序号 / 文件名 / 分片数 / 时长）与 `<名字>.shards.txt`（分片清单：文件名 / 大小，合并前就记进内存表，分片删掉后也能写全）。
  - 顺带修：分片模式下不记「待拼接分段」（`_remember_part` 早退，不再出现「这一段没有生成文件」）；体积/8 秒无写入判定把临时目录也算进去（不再因为成品没落盘而误报）；续段不再把 `segments` 清零；合并加 `threading.Lock`，避免录制循环的定期 flush 和收尾线程同时合并同一批分片（以前会并出两个同名 part、文件被覆盖）。
- 实测（房 660000，`split_seconds=20` / 15）：临时目录分片数持续增长、成品 `part000..part005` 依次落盘（每片 20 秒左右）、清单两份齐全、整场结束后临时目录被清空、录制不中断。
- 旧版：「虎牙分段录制改为一片一个成品视频」（ffmpeg segment 直出、不断线）与「虎牙录制改用 FLV 长连接」已被上面的 HLS+临时目录方案取代，`repair_file()` / `inpoint` 去重等能力保留给没开分段录制时的收尾合并路径。
- **虎牙分段录制改为「一片一个成品视频」**：开「分段录制」时不再由程序到点 terminate 切段、也不在收尾合并/再切 part，而是让 ffmpeg 的 segment 在一条不断线的 FLV 连接上直接写 `<名字>_000.flv`、`_001.flv`…，每片就是设置里的单段时长、可直接播放；同时用 `-segment_list … -segment_list_type flat -segment_list_flags +live` 落一个分片清单文本（<名字>_<时间>.index.txt）。续段时用 `-segment_start_number` 接着编号，不会覆盖已录分片；前缀在一场内保持稳定（`self._session_prefix`）。
- 收尾新增 `liverec/concat.py::repair_file()`：停止/断流时正在写的那一片是 `Duration: N/A`（播放器拖不动进度条），重封装一遍补上时长，日志「末尾被掐断，已修成可播放的 X 秒」；0 字节空片直接删掉。
- 修三个由此暴露的 bug：①`_shard_files()` 用 `os.path.getsize()` 会读到 Windows 懒更新的旧大小（末片被当成 0 字节漏掉）→ 改 `os.stat().st_size` + `min_size` 参数；②`_finalize_shards()` 的修复逻辑原来写在 `if converts:` 里，目标格式是 flv 时压根不跑 → 改为总是跑；③`stop()` 无条件 `_remember_part()`，会把分片误记成待拼接分段 → 分片模式跳过。
- 实测（房 660000，分片 15/20 秒）：分片 `_000.._011` 连续落盘、录制不中断、续段后序号接着往下、清单文本实时增长、收尾末片 5.1 秒可播、`_merged` 无残留、卡片「分段」计数跟着涨。
- **虎牙录制改用 FLV 长连接**（对齐 stream-rec 的 `flvFormatStreams()` 优先策略）：`liverec/huya.py::_candidates()` 排序改成 FLV 优先、HLS 兜底；`resolve()` 的 CDN 轮转只在**同一格式**内进行（以前混着轮转，下一段会从 FLV 跳回 HLS，接缝又变成重播 2 个分片）。HLS 仍保留本地中转 + inpoint 去重，作为没有 FLV 线路时的兜底。
- 续段接缝改用「媒体时长 − 实际录制墙钟时长」估算重播量（FLV 重连时 CDN 从直播边缘往后落后几秒起推），比只看墙钟时间更准；超过 0.3 秒就记「与上一段重叠 X 秒，拼接时自动跳过」并在拼接时用 `inpoint` 精确跳过。
- 实测（房 660000，`split_seconds=20` 强制切段）：4 段共 66.6 秒媒体 / 70 秒墙钟，无重复播放；连续录制 5 分钟 153.88 MB、卡片时长连续不归零。

- 虎牙录制按 stream-rec 的做法整体重写（liverec/huya.py）：mp.huya.com 取房间信息（iPhone 微信 UA）、CDN × {flv,hls} × 码率线路矩阵、antiCode 本地重算（codec=264、u 旋转、ss）、WUP liveup.getCdnTokenInfo 兜底、udblgn Cookie 校验；录制改走本地 HLS 中转（每次刷新重签、分片过期即续段、收尾自动拼接）。
- DISABLED_PLATFORMS 清空，虎牙重新可用；界面/README 里「虎牙已下线」的文案恢复。
- liverec/hlsproxy.py：拉流转 erify=False（虎牙 CDN 证书链不完整）、非 URL 行与坏 URI 不再让整篇播放列表 500。

## v0.2.0（2026-11-26）

本版是「七项整改 + 虎牙下线 + 自定义 ffmpeg + 抖音修复」的汇总发布。

**抖音链接形态与弹幕握手修复。**

- 链接解析：`_douyin_web_rid()` 增加两条规则——`www.douyin.com/…/live/<rid>`（关注页/根页跳转过来的直播间地址，
  `?anchor_id=…` 之类参数自动忽略）与 `?live_web_rid=<rid>`（直播频道页把房间号放在 query 里，路径是空的）。
  弹幕客户端 `danmaku/douyin.py` 抽出同规则的 `_web_rid()`，不再只认 `live.douyin.com/<数字>`（以前这类链接
  会报「抖音弹幕：未能从直播间网页解析出 room_id」）。
- 弹幕握手：抖音 WSS 端点对**不带 Cookie** 的握手直接回 `HTTP 200` 拒绝（`InvalidStatus: server rejected WebSocket
  connection: HTTP 200`），表现为「状态 live、条数一直 0」。现在 `DouyinDanmaku._cookie_header()` 始终把从页面拿到的
  `ttwid` 并进 Cookie（用户填了抖音 Cookie 时保留原 cookie 再补 ttwid），`_consume()` 也改用同一套头；`_prepare()`
  在直播间页没下发 ttwid 时回退到 `https://live.douyin.com/` 再取一次，仍拿不到就明确报错而不是静默 0 条。

**七项交互与录制逻辑整改。**

- 单线路：`recorder.py` 不再把解析出的多条候选线路挨个试（`candidates = [stream_url]`）。这条线路一个字节都没出
  （下播 / 风控 / 地址失效）就置 `_no_data`、把卡片改回「等待开播」并按检测间隔重试；日志里不再有「线路 N/M 未出数据，尝试下一条」。
- 虎牙独立：`liverec/huya.py`（新文件，约 620 行）承接全部虎牙逻辑——`resolve / qualities / qualities_from_data /
  next_stream / drop_cache / resign / fresh_line / target_bitrate`；`resolver.py` 只留转发薄壳，`recorder.py` 通过
  `huya_mod` 取新签名。虎牙签名「一次一签」、多 CDN 可用性差异、静态分片等问题都收敛在这个模块里。
- 画质一致：`manager.snapshot()` 以前用 `meta.quality` 覆盖运行时快照的画质，导致卡片显示的是「你当初选的档位」而不是
  实际在录的档位。现在以运行中的任务为准并回写 meta；`recorder` 也把 `adjust_quality` 的结果无条件写回 `self.quality`，
  所以自动/无人值守拉起时卡片会直接显示真实档位（如「原画（蓝光30M）」）。
- 启动 Cookie 自检：`server._cookie_audit()` 在启动 1.2 秒后把四平台各查一遍写日志（每平台一条），设置里点「检测」、
  保存配置也不再各记两条（前端那两条 `pushLocalLog` 已删）。
- 弹幕：新增「弹幕保存路径」（`config.ini` 的 `弹幕保存路径(不填则跟随视频目录)`，默认跟随 `录像目录/danmaku/`）；
  每张卡片新增「弹幕 / 无弹幕」开关（`PATCH /api/rooms/{id}` 的 `danmaku`，切换会重启该房间任务）。
- 卡片：删掉「分段」这一格（保留「当前分段 x/y」进度条与文件名）；设置/编辑弹窗去掉「取消」与遮罩点击关闭，只留右上角 ✕。
- 无人值守默认关闭：`manager.unattended` 初始值改为 `False`，新装程序与新加房间都要手动点「自动」才会在下次启动时自动监测。
- **虎牙录制暂时下线**：新增 `resolver.DISABLED_PLATFORMS`（当前只含 `huya`）。`resolve()` / `fetch_qualities()` 遇到虎牙直接返回
  「虎牙录制已暂时下线：平台流地址一次性失效、CDN 频繁掐断，暂时录不稳」。`manager.add()` 对虎牙链接直接报错不建卡；`manager.load()`
  把已存在的虎牙房间置为待机（`enabled=False`、`unattended=False`），`resume_unattended()` 跳过并记一条「N 个房间所属平台已下线」；
  添加弹窗与设置页的提示文案同步改成「抖音 / B站 / 斗鱼」。实现仍完整保留在 `liverec/huya.py`，恢复时清空 `DISABLED_PLATFORMS` 即可。
- **设置里自定义 ffmpeg 路径**：`config.ini` 新增 `ffmpeg路径(不填则用内置)`（`K_FFMPEG`），`Config.ffmpeg_path()` 会调
  `paths.set_ffmpeg_path()` —— 传进来的文件不存在时**不采用**并回落到内置/PATH，避免一次手滑让全部录制崩掉。`paths.ffmpeg_exe()`
  现在优先返回这个 override；`recorder` / `concat` / `huya` 都是通过它取路径，所以保存设置后新开始的录制立刻用新 ffmpeg（已运行的不重启）。
  `POST /api/settings` 收到不存在的路径直接 400（提示「找不到这个 ffmpeg：…」），并打一行「ffmpeg 路径已切换 / 已改回内置」；
  `_apply_startup()` 启动时先应用一次，所以日志第一行 `已启动 · ffmpeg=…` 就是真正生效的那个。设置页新增该输入框 + 「当前生效：…」提示。

## 内部迭代（2026-11-25 20:30，未单独发布，已并入 v0.2.0）

**虎牙弹幕接入 + 斗鱼画质真正按档位取流。**

- `liverec/danmaku/huya.py`：按 biliup 的 Rust 实现移植 TARS 协议（`wss://cdnws.api.huya.com/`）——
  注册包 `WSUserInfo{lUid=页面 uid, lGroupId=uid, lGroupType=3, lTid=lSid=0}`、60 秒 `HEARTBEAT`、
  推送解析 `read_int32(0)==7 → read_bytes(1) → read_int64(1)==1400 → read_bytes(2)`，
  昵称取 `MessageNotice` tag0 的 sender struct（tag2 是 `lSid`，旧实现读错）。实测 30 秒 154 条。
- `liverec/resolver.py`：斗鱼改走 `getEncryption + getH5PlayV1`，按 `rate`（0 原画 / 3 蓝光 / 2 超清 / 1 流畅）取流；
  UI 的画质下拉固定给这四档（`betard` 的 `multirates` 很多房间只有一条，不能当档位表）。
  实测同一房间 6 秒分别 2.4MB / 1.0MB / 0.7MB，应用内「流畅 6.6 Mbps ↔ 原画 8.2 Mbps」可切。
- `liverec/resolver.py`：虎牙优先挑「带 `#EXTINF` 的 media 播放列表」线路；`_huya_playlist_text` 只读前 64KB
  （修掉「解析卡死 3 分钟」——虎牙偶尔回一条不结束的响应）。
- `liverec/recorder.py`：虎牙不再走本地 HLS 中转（中转的刷新节奏会打乱 CDN 验签，直连反而更稳）；
  分片续录按 300KB 判定，弹幕会话跨段保持（修掉「弹幕条数一会大一会小」）；解析加 75 秒超时。
- 已知：虎牙 CDN 对同一出口 IP 有短时会话上限，长时间密集测试后单段只能录到 0–2MB，隔一段时间会恢复。


## v0.1.2（2026-11-25 19:00）

**修复虎牙录制中途断流后再也接不上**（`recording` 卡住、体积不涨、日志刷 403）。

- `liverec/hlsproxy.py`：播放列表是 master 时自己跟一跳取子列表（最多 2 层，子列表同样过 signer）；每次刷新最多签 3 次，全失败时回放上一次的播放列表，不再直接 502 把 ffmpeg 弄断流。
- `liverec/resolver.py`：`_huya_fresh_line` 改为「同 CDN + 同 ratio」精确匹配并绕过缓存（ttl 2.5s），每次刷新都拿新的 wsTime；线路按 CDN 稳定性排序（`hs.` 优先）；补 `import time`（缺它会让缓存层抛 `NameError`）。
- `liverec/recorder.py`：虎牙分片续录——签名到期被掐断时保留已录字节（`_base_bytes`）、立刻换新签名续录，卡片保持「录制中」；连续拿不到数据时按 1–5 秒退避，连续 7 段都失败才回「等待开播」，避免把 CDN 当刷量越试越 403。虎牙的断流判定阈值 15 秒（其它平台 60 秒）。
- `liverec/recorder.py`：新增 `_guarded_run()` 保护壳，录制循环内任何意外异常都只把卡片摆回「等待开播」并 15 秒后重试，不会把房间永久卡在「录制中」。
- 实测：虎牙 10188 连续录制 3 分钟、64 MB 不中断（2560×1440 / 60fps / 3.2 Mbps，`ffprobe` 校验可解码）；冻结版三平台同时录制正常。

## v0.1.1（2026-11-25 18:16）

**这一版是「回退版」**：撤销了 17:38–17:57 之间针对虎牙的 4 项改动（HLS 主列表跟跳、刷新重试与兜底、`_huya_fresh_line` 精确匹配 + 绕过缓存、虎牙分片续录），恢复到 17:18 的行为。

- 保留 `import time`（17:18 那版 `_huya_mp_data_cached` 少这一行，会抛 `NameError`，属必须保留的修复）。
- 保留虎牙短名解析（`www.huya.com/<短名>` → 真实房间号）、wsSecret 重签、录制前画质自动校正。
- 已知行为：虎牙连续录制约 1MB 后会断（签名 20 秒过期），卡片会停在 `recording` 但体积不涨；B站/抖音/斗鱼不受影响。

## v0.1.0（2026-11-25 03:30）

首个对外版本，包含：

- 多直播间并行录制（抖音 / B站 / 虎牙 / 斗鱼），每房间一张卡片，实时显示状态、体积、码率、分辨率、分段、落盘文件
- 无人值守：每张卡片独立开关「自动 / 手动」，关闭程序时记录状态，下次启动按状态恢复
- 弹幕采集：抖音（WSS + webmssdk 签名 + protobuf）、B站（buvid3 + wbi 签名）、斗鱼（裸 TCP STT）；虎牙弹幕未接入（需开放平台资质）
- 画质选择：按链接识别该直播间真实可选的档位，录制时按所选档位取流；实际档位降级时卡片显示「xx·降级」
- 系统托盘常驻、单实例、启动画面进度条、窗口图标统一
- 录制看门狗：连续 60 秒没有新数据 → 停止录制并回到等待开播，卡片写明原因
- HLS 中转：修部分 B站房间（HLS-fMP4 + 相对 URI 丢签名）录不到的问题
- Cookie 有效性检测（B站/抖音走登录态接口，虎牙/斗鱼做字段 + 令牌有效期校验）
- 配置：`data/config/config.ini`（跟程序目录走，含 Cookie，不要提交到 Git）
