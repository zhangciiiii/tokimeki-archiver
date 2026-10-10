import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from .danmaku import make_ass, read_xml, schedule
from .media import check_ffmpeg, executable, find_font, probe, render, save_ass


def finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("必须是有限数值")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="将 XML 弹幕烧录为全屏滚动弹幕 MP4")
    p.add_argument("--video", required=True, type=Path, help="输入视频")
    p.add_argument("--danmaku", required=True, type=Path, help="对应 XML 弹幕")
    p.add_argument("--output", required=True, type=Path, help="输出 MP4；同时生成同名 ASS")
    p.add_argument("--style", choices=["avatar", "text"], default="avatar",
                   help="avatar：圆形头像＋正文（默认）；text：仅正文；均不显示用户名")
    p.add_argument("--avatar-cache", type=Path, default=Path(".cache/avatars"),
                   help="头像缓存目录，默认 .cache/avatars")
    p.add_argument("--font", help="中文 TTF/OTF/TTC 字体文件")
    p.add_argument("--font-size", type=int, help="字号（像素），默认视频高度的 3.5%%")
    p.add_argument("--opacity", type=finite, default=1, help="不透明度 0–1，默认 1")
    p.add_argument("--duration", type=finite, default=8, help="单条弹幕滚动秒数，默认 8")
    p.add_argument("--offset", type=finite, default=0, help="弹幕时间偏移秒数，正数延后")
    p.add_argument("--preview-start", type=finite, default=0, help="原视频截取起点（秒）")
    p.add_argument("--preview-duration", type=finite, help="截取长度（秒），默认至视频结束")
    p.add_argument("--crf", type=int, default=20, help="H.264 质量 0–51，越小越清晰，默认 20")
    p.add_argument("--preset", default="veryfast", choices=["ultrafast", "superfast", "veryfast",
                   "faster", "fast", "medium", "slow", "slower", "veryslow"])
    p.add_argument("--ffmpeg", help="FFmpeg 可执行文件路径")
    p.add_argument("--ffprobe", help="ffprobe 可执行文件路径")
    p.add_argument("--overwrite", action="store_true", help="允许替换已有输出 MP4 和 ASS")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        video, xml, output = (p.expanduser().resolve() for p in
                              (args.video, args.danmaku, args.output))
        if not video.is_file() or not xml.is_file():
            raise ValueError("视频或 XML 文件不存在")
        if output.suffix.lower() != ".mp4":
            raise ValueError("--output 必须使用 .mp4 扩展名")
        sidecar = output.with_suffix(".ass")
        for target in (output, sidecar):
            if target in (video, xml) or (target.exists() and
                    any(target.samefile(source) for source in (video, xml))):
                raise ValueError("输出路径不能覆盖输入文件")
            if target.exists() and not args.overwrite:
                raise ValueError(f"输出已存在：{target}；使用 --overwrite 才允许覆盖")
        if not 0 <= args.opacity <= 1 or args.duration < .01:
            raise ValueError("opacity 必须为 0–1，duration 必须至少为 0.01 秒")
        if not 0 <= args.crf <= 51 or (args.font_size is not None and args.font_size <= 0):
            raise ValueError("crf 必须为 0–51，字号必须大于 0")
        if args.preview_start < 0 or (args.preview_duration is not None and args.preview_duration < .01):
            raise ValueError("预览起点不能为负数，预览长度必须至少为 0.01 秒")
        ffmpeg, ffprobe = executable("ffmpeg", args.ffmpeg), executable("ffprobe", args.ffprobe)
        check_ffmpeg(ffmpeg)
        info = probe(ffprobe, video)
        if args.preview_start >= info["duration"]:
            raise ValueError("预览起点超出视频时长")
        clip_duration = min(args.preview_duration or info["duration"],
                            info["duration"] - args.preview_start)
        try:
            from PIL import ImageFont
        except ImportError as exc:
            raise ValueError("缺少 Pillow，请运行 python -m pip install -e .") from exc
        font_path = find_font(args.font)
        font_size = args.font_size or max(12, round(info["height"] * .035))
        font = ImageFont.truetype(str(font_path), font_size)
        font_name = font.getname()[0]
        if any(char in font_name for char in ",\n\r"):
            raise ValueError("字体名称包含 ASS 不支持的字符")
        comments, stats = read_xml(xml)
        events = schedule(comments, stats, width=info["width"], height=info["height"],
                          video_duration=info["duration"], font=font, font_size=font_size,
                          duration=round(args.duration, 2), offset=args.offset, style=args.style)
        ass = make_ass(events, stats, width=info["width"], height=info["height"],
                       font_name=font_name, font_size=font_size, opacity=args.opacity,
                       clip_start=args.preview_start, clip_duration=clip_duration)
        print(f"视频 {info['width']}×{info['height']}，原帧率 {info['fps']} fps → "
              f"输出 {info['output_fps']} fps，"
              f"时长 {info['duration']:.3f}s；字体 {font_name}", flush=True)
        print("弹幕统计：" + json.dumps(asdict(stats), ensure_ascii=False), flush=True)
        avatars = None
        if args.style == "avatar":
            from .avatars import prepare
            visible = [e for e in events if e.start < args.preview_start + clip_duration and
                       e.end > args.preview_start]
            avatars, avatar_stats = prepare(visible, args.avatar_cache.expanduser().resolve())
            print("头像统计：" + json.dumps(asdict(avatar_stats), ensure_ascii=False), flush=True)
            if avatar_stats.fallback_comments:
                print(f"警告：{avatar_stats.fallback_comments} 条弹幕使用默认头像"
                      "（头像缺失或下载失败）", file=sys.stderr)
        if stats.invalid or stats.out_of_range or stats.crowded:
            print(f"警告：跳过异常 {stats.invalid} 条、越界 {stats.out_of_range} 条、"
                  f"轨道拥挤 {stats.crowded} 条", file=sys.stderr)
        save_ass(sidecar, ass, args.overwrite)
        render(ffmpeg=ffmpeg, video=video, output=output, ass=ass, font=font_path,
               info=info, start=args.preview_start, duration=clip_duration,
               crf=args.crf, preset=args.preset, overwrite=args.overwrite,
               events=events, avatars=avatars, opacity=args.opacity)
        print(f"完成：{output}\n弹幕字幕：{sidecar}")
        return 0
    except KeyboardInterrupt:
        print("已取消，未完成的视频已清理；ASS 文件保留。", file=sys.stderr)
        return 130
    except (ValueError, OSError, ET.ParseError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
