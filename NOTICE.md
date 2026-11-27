# 第三方组件与许可

本项目自身的代码按 MIT 协议使用。仓库只托管源码与文本，不托管任何二进制；但 **Release 里的免安装压缩包**（`LiveRec-vX.Y.Z-win64.zip`）把下列组件一并打进去了，分发或再分发这个压缩包时请注意各自的许可。

## 一、随发布压缩包分发的二进制

| 组件 | 用途 | 许可 | 说明 |
| --- | --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) 9.0.2（gyan.dev essentials build） | 录制、转封装、切段与合并（`_internal\bin\ffmpeg.exe`，约 100 MB） | **GPL v3**（该构建启用 `--enable-gpl --enable-version3`，含 libx264 / libx265 / libxvid 等 GPL 组件） | 许可文本：<https://ffmpeg.org/legal.html>；源码：<https://ffmpeg.org/download.html>（构建配置与 gyan.dev essentials 一致）。再分发时请保留本声明并附上 FFmpeg 的许可文本与源码获取方式 |
| Python 运行时（CPython 3.13） | 运行程序（`_internal\` 内的解释器与标准库） | PSF License 2.0 | 由 PyInstaller 打包进 `_internal\` |
| [PyInstaller](https://pyinstaller.org/) bootloader | 启动 `LiveRec.exe` | GPL v2 **with Bootloader Exception** | Bootloader 例外条款明确允许用它打包专有/其他许可的程序，不传染本项目代码 |
| [V8](https://v8.dev/)（经 [py-mini-racer](https://github.com/bpcreech/PyMiniRacer) 0.6.0 内嵌） | 执行抖音弹幕签名脚本 `liverec/danmaku/assets/sign.js` | V8: BSD-3-Clause；py-mini-racer: MIT | 仅本地执行，不联网 |

> 说明：本项目的代码（MIT）以**独立进程**方式调用 `ffmpeg.exe`（`subprocess`），属于「聚合分发」而非链接，因此本项目自身的许可不因 GPL 的 FFmpeg 而改变；FFmpeg 本身仍按 GPL v3 分发，需按 GPL 提供对应源码。

## 二、随包分发的纯 Python 依赖

这些以 `.pyc`/`.pyd` 形式进 `_internal\`，许可证均为宽松许可（MIT / BSD / Apache-2.0 / Unlicense），无需额外声明即可再分发：

- Web 服务：[fastapi](https://github.com/fastapi/fastapi)（MIT）、[uvicorn](https://github.com/encode/uvicorn)（BSD-3）、starlette（BSD-3）、pydantic（MIT）、python-multipart（Apache-2.0）
- 网络：[requests](https://github.com/psf/requests)（Apache-2.0）、[httpx](https://github.com/encode/httpx)（BSD-3）、[websockets](https://github.com/python-websockets/websockets)（BSD-3）、brotli（MIT）、protobuf（BSD-3）
- 解析：[yt-dlp](https://github.com/yt-dlp/yt-dlp)（Unlicense，仅虎牙兜底解析用）
- 日志：[loguru](https://github.com/Delgan/loguru)（MIT）

## 三、仓库内嵌的第三方脚本

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `liverec/danmaku/assets/sign.js` | [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) | 抖音 webmssdk 构建产物，用于生成弹幕 WSS 签名；来源已注明，许可以上游仓库为准 |

## 四、参考过的实现（未直接引用代码）

- [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)（MIT）：项目早期的平台解析思路参考。**当前版本已移除对其 `core/` 的依赖**，仓库与分发包中均不含它的代码。
- [stream-rec/stream-rec](https://github.com/stream-rec/stream-rec)：虎牙取流（FLV/HLS 线路矩阵 + antiCode 签名的算法顺序）与弹幕 TARS 协议的思路参考。
- [xfgryujk/blivedm](https://github.com/xfgryujk/blivedm)（MIT）：B 站弹幕协议（buvid3 + wbi 签名）参考。
- [wbt5/real-url](https://github.com/wbt5/real-url)（MIT）：虎牙/斗鱼取流思路参考。

## 五、仅开发期使用（不随分发包）

- [Pillow](https://python-pillow.org/)（MIT-CMU）：只用来生成 `bin/liverec.ico`。
- grpcio-tools（Apache-2.0）：只在改动 `douyin.proto` 后重新生成 `douyin_pb2.py` 时需要。

## 六、仓库里有没有二进制

- `bin\ffmpeg.exe`：**不在仓库里**（`.gitignore` 忽略 `bin/*.exe`）。需要时用 `tools\fetch_ffmpeg.ps1` 下载到 `bin\`，打包时会打进压缩包。
- `bin\liverec.ico`：在仓库里（程序图标，随 `LiveRec.spec` 打进 exe）。

## 免责声明

本工具仅用于个人学习与备份自己有权观看的直播内容。请遵守各直播平台的服务条款与当地法律，不要用于转载、二次分发或任何商业用途。录制内容的版权归原作者所有。
