# 更新日志

版本号规则：`v0.1.x` 递增，统一用 `git tag` 打标签；exe 只在明确要求时才上传到 GitHub Releases。

## 未发布（待确认后再定版本号/tag）

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

## v0.2.0（2026-11-25 20:30，未打 tag，等确认）

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
