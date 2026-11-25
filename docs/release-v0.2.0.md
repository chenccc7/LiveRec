# LiveRec 控制台 v0.2.0

抖音 / B站 / 斗鱼 多房间直播录制 + 弹幕采集（虎牙录制暂时下线），单文件 exe，内置 ffmpeg。

## 这次更新了什么

### 抖音
- 支持更多直播链接形态：`live.douyin.com/531854687617`、`live.douyin.com/share/…`、`www.douyin.com/follow/live/<rid>?anchor_id=…`、
  `www.douyin.com/root/live/<rid>`、`live.douyin.com/?live_web_rid=<rid>&…`（直播频道页）、`v.douyin.com/...` 短链。
- 修复「弹幕状态 live 但条数一直是 0」：抖音弹幕 WSS 端点对**不带 Cookie** 的握手直接回 `HTTP 200` 拒绝，
  现在始终把页面下发的 `ttwid` 并进 Cookie（填了抖音 Cookie 时保留原 cookie 再补 ttwid）。

### 录制与房间
- 只保留一条线路：线路不出数据就直接回到「等待开播」并按间隔重试，不再挨个试多条候选线路。
- 虎牙录制暂时下线（流地址一次性失效 + CDN 频繁掐断）：链接会明确提示，已存在的虎牙房间置为待机；实现保留在 `liverec/huya.py`。
- 卡片画质跟着实际解析结果走（例如选了「原画」但平台只给到「超清」，卡片会显示「超清」+ 橙色「降级」标记）。

### 界面
- 每张卡片两个独立开关：**自动**（无人值守，下次启动是否自动开始）、**弹幕**（这场是否采集弹幕）。
- 卡片去掉「分段」统计；设置/编辑弹窗只能右上角 ✕ 关闭。
- 启动画面（分阶段进度条）与窗口/任务栏图标统一为 exe 图标。
- 启动时自动校验四个平台的 Cookie，并把结论打进运行日志。

### 设置
- 新增「弹幕保存路径」（留空 = 跟随每场录像目录下的 `danmaku/`）。
- 新增「ffmpeg 路径」（留空 = 用内置 `bin/ffmpeg.exe`）；保存时会校验文件是否存在，填错直接报错不写入。

## 已知限制

- 虎牙录制已下线，恢复时清空 `liverec/resolver.py` 里的 `DISABLED_PLATFORMS` 即可。
- B站未登录时最高只能拿到「超清」，填了 B站 Cookie 才会出现「原画 / 蓝光」。
- 抖音弹幕需要程序能访问 `live.douyin.com` 并拿到 `ttwid`；网络受限时会提示而不是静默 0 条。
- 斗鱼多档位依赖 `getH5PlayV1` 的 rate（原画/蓝光/超清/流畅），房间只提供一档时下拉也只有一档。

## 校验

- 文件：`LiveRec.exe`（约 78 MB，单文件，首次启动会解压 5–8 秒）
- SHA256 见 Release 附件说明或下方命令：

```
certutil -hashfile LiveRec.exe SHA256
```
