# Live Recorder Danmaku

将本地视频与 XML 弹幕合成为带滚动弹幕的 MP4。适合直播录像回看、分享和存档。

- 默认显示圆形头像和正文，不显示用户名，也支持纯文字模式。
- 按弹幕时间同步，全屏滚动，自动分配轨道以减少重叠。
- 保留视频分辨率；低于 24 fps 时补至 24 fps，否则保持原帧率。
- 支持短片预览、时间偏移、字体和透明度调整，头像下载后可离线复用。

## 安装

需要 **Python 3.10+**、**FFmpeg 5+**（包含 ffprobe、libass、libx264 和 AAC 编码器）及中文字体。将 FFmpeg 和 ffprobe 加入 `PATH`。

下载或克隆本仓库后，在项目目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。若未自动找到中文字体，可通过 `--font` 指定 TTF、OTF 或 TTC 字体文件。

## 使用

将 `video.flv` 和 `danmaku.xml` 替换为自己的文件路径：

```bash
live-recorder --video video.flv --danmaku danmaku.xml --output output/result.mp4
```

先预览原视频第 60 秒开始的 20 秒片段：

```bash
live-recorder --video video.flv --danmaku danmaku.xml \
  --output output/preview.mp4 --preview-start 60 --preview-duration 20
```

输出为 H.264/AAC MP4，并附带同名 ASS 字幕。ASS 仅包含正文，头像效果保存在视频中。输出已存在时需加 `--overwrite`。也可使用 `python -m live_recorder` 调用。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--style avatar\|text` | 头像＋正文（默认）或纯正文 |
| `--offset 1.5` | 弹幕延后 1.5 秒；负数表示提前 |
| `--font /path/to/font.ttf` | 指定中文字体 |
| `--font-size 36` | 字号，默认按视频高度缩放 |
| `--opacity 0.8` | 不透明度，范围 0–1，默认 1 |
| `--duration 8` | 每条弹幕滚动时长，默认 8 秒 |
| `--avatar-cache .cache/avatars` | 头像缓存目录 |
| `--crf 20` | 编码质量，越小越清晰，默认 20 |
| `--overwrite` | 覆盖已有输出视频和 ASS |

完整参数见 `live-recorder --help`。

## 弹幕格式与限制

支持包含 `<d p="...">正文</d>` 的 XML 弹幕，例如：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="1.5,1,25,16777215,0,0,0,0">你好，世界！</d>
</i>
```

`p` 的第一项为相对视频起点的秒数，第四项为十进制 RGB 颜色。所有文字统一按从右向左滚动呈现；不显示礼物、系统消息或用户名。

头像可从同一 XML 的 `<o>` 原始 `DANMU_MSG` 消息中提取，通过时间戳和正文匹配。首次下载需要网络；缺少头像信息或下载失败时使用默认头像，不影响导出。只有 `<d>` 的 XML 也可正常使用，建议选择 `--style text`。

低帧率补帧采用重复画面，使弹幕以 24 fps 更新，不会增加原视频的动作细节。轨道拥挤时跳过无法容纳的弹幕，并在终端报告数量。烧录弹幕需要重新编码，较长视频的导出会耗费一定时间。

## 开发

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

测试使用合成视频和本地图片，不依赖私人录像或网络。FFmpeg 集成测试需要上述系统依赖；如本机第三方 pytest 插件冲突，可设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。

## 许可证

采用 [BSD Zero Clause（0BSD）](LICENSE)，允许自由使用、修改和再分发，包括商业用途，不要求署名。
