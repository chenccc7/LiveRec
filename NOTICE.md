# 第三方组件与许可

本项目自身代码可按 MIT 协议使用。它依赖/内嵌了下列第三方组件，分发时请一并遵守：

| 组件 | 用途 | 许可 | 说明 |
| --- | --- | --- | --- |
| [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | 平台解析兜底（`core/`） | MIT | 不随仓库托管，用 `tools\fetch_core.ps1` 拉取 |
| [FFmpeg](https://ffmpeg.org/)（gyan.dev essentials build） | 录制与转封装（`bin\ffmpeg.exe`） | LGPL/GPL | 二进制不随仓库托管，用 `tools\fetch_ffmpeg.ps1` 拉取；对外分发 exe 时请附 FFmpeg 许可文本 |
| [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) | 抖音弹幕签名脚本 `liverec/danmaku/assets/sign.js` | 见上游仓库 | 该文件为 webmssdk 构建产物，随本项目内嵌，来源已注明 |
| [wbt5/real-url](https://github.com/wbt5/real-url) | 虎牙/斗鱼的取流思路参考 | MIT | 仅参考实现，未直接引用代码 |
| [xfgryujk/blivedm](https://github.com/xfgryujk/blivedm) | B站弹幕协议参考（buvid3 + wbi 签名） | MIT | 仅参考实现 |

## 免责声明

本工具仅用于个人学习与备份自己有权观看的直播内容。请遵守各直播平台的服务条款与当地法律，不要用于转载、二次分发或任何商业用途。录制内容的版权归原作者所有。
