"""FFmpeg discovery, probing, and atomic MP4 rendering."""
import json
import math
import os
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def executable(name: str, explicit: str | None = None) -> str:
    candidate = explicit or shutil.which(name)
    if not candidate:
        local = Path(__file__).resolve().parent.parent / ".tools" / "bin" / name
        if local.is_file():
            candidate = str(local)
    if not candidate:
        raise ValueError(f"未找到 {name}，请安装 FFmpeg 或传入 --{name} 路径")
    found = shutil.which(candidate)
    if not found:
        raise ValueError(f"无法执行 {name}: {candidate}")
    return str(Path(found).resolve())


def run_checked(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"{Path(command[0]).name} 执行失败：\n{result.stderr[-3000:]}")
    return result.stdout


def check_ffmpeg(ffmpeg: str) -> None:
    filters = run_checked([ffmpeg, "-hide_banner", "-filters"])
    encoders = run_checked([ffmpeg, "-hide_banner", "-encoders"])
    if not any(line.split()[1:2] == ["ass"] for line in filters.splitlines()):
        raise ValueError("FFmpeg 缺少 ass/libass 滤镜，请安装支持 libass 的版本")
    for encoder in ("libx264", "aac"):
        if not any(line.split()[1:2] == [encoder] for line in encoders.splitlines()):
            raise ValueError(f"FFmpeg 缺少 {encoder} 编码器")


def probe(ffprobe: str, video: Path) -> dict:
    data = json.loads(run_checked([ffprobe, "-v", "error", "-show_format",
                                   "-show_streams", "-of", "json", str(video)]))
    stream = next((s for s in data.get("streams", []) if s["codec_type"] == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if stream is None:
        raise ValueError("输入文件没有视频轨道")
    width, height = int(stream["width"]), int(stream["height"])
    rotation = next((float(s["rotation"]) for s in stream.get("side_data_list", [])
                     if "rotation" in s), float(stream.get("tags", {}).get("rotate", 0)))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
        raise ValueError("无法确定有效的视频尺寸或时长")
    if width % 2 or height % 2:
        raise ValueError("H.264/yuv420p 输出要求视频宽高为偶数")
    fps = source_fps(stream)
    return {"width": width, "height": height, "duration": duration,
            "video_index": stream["index"], "fps": str(fps),
            "upsample": fps < 24, "output_fps": str(max(fps, Fraction(24)))}


def source_fps(stream: dict) -> Fraction:
    # Keep rational rates exact: 24000/1001 is below 24, 30000/1001 is above it.
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(stream.get(field, "0/0")))
            if rate > 0:
                return rate
        except (ValueError, ZeroDivisionError):
            pass
    raise ValueError("无法确定视频帧率，不能判断是否需要补帧")


def video_filter(info: dict) -> str:
    # Duplicate source frames BEFORE libass, so each new frame gets a freshly
    # evaluated animation position. Duplicating the composited frames would
    # retain the original low-rate, stuttering danmaku motion.
    prefix = "fps=24," if info["upsample"] else ""
    return prefix + "ass=comments.ass:fontsdir=fonts"


def find_font(explicit: str | None) -> Path:
    if explicit:
        font = Path(explicit).expanduser().resolve()
        if not font.is_file():
            raise ValueError(f"字体文件不存在：{font}")
        return font
    if shutil.which("fc-list"):
        # fc-match can silently return a Latin-only fallback for :lang=zh.
        # fc-list filters to fonts that actually advertise Chinese coverage.
        matches = run_checked(["fc-list", ":lang=zh", "-f", "%{file}\n"]).splitlines()
        for match in sorted(set(matches)):
            font = Path(match)
            if font.is_file():
                return font.resolve()
    for candidate in ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                      "C:/Windows/Fonts/msyh.ttc", "/System/Library/Fonts/PingFang.ttc"):
        if Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ValueError("未找到中文字体，请用 --font 指定中文 TTF/OTF/TTC 字体文件")


def publish(source: Path, target: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(source, target)
    else:
        # Same-filesystem hard link gives atomic create-without-overwriting.
        os.link(source, target)
        source.unlink()


def render(*, ffmpeg: str, video: Path, output: Path, ass: str, font: Path,
           info: dict, start: float, duration: float, crf: int, preset: str,
           overwrite: bool, events: list | None = None, avatars: dict | None = None,
           opacity: float = 1) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".danmaku-", dir=output.parent) as directory:
        work = Path(directory)
        (work / "comments.ass").write_text(ass, encoding="utf-8")
        (work / "fonts").mkdir()
        shutil.copyfile(font, work / "fonts" / ("danmaku" + font.suffix))
        command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
        # Seeking even to zero can fail on FLV files without a seek index and
        # advance past buffered opening frames. Full exports should read normally.
        if start > 0:
            command += ["-ss", str(start)]
        command += ["-i", str(video)]
        visible = [e for e in (events or []) if e.avatar_size and
                   e.start < start + duration and e.end > start]
        if visible:
            from .avatars import circle
            assert avatars is not None
            # Single-frame inputs are held by framesync; no looped image decoding
            # or extra frames are introduced into the main video's timeline.
            filters = [f"[0:{info['video_index']}]" +
                       ("fps=24" if info["upsample"] else "null") + "[base0]"]
            for index, event in enumerate(visible):
                image = avatars.get(event.comment.avatar_url, avatars[None])
                name = f"avatar{index}.png"
                circle(image, event.avatar_size, opacity).save(work / name)
                command += ["-threads", "1", "-i", name]
                speed = (info["width"] + event.width) / (event.end - event.start)
                begin, end = max(0, event.start - start), min(duration, event.end - start)
                x = f"{event.x_at(start, info['width']):.8f}-{speed:.8f}*t"
                filters.append(f"[base{index}][{index+1}:v]overlay=x='{x}':y={event.y}:"
                               f"enable='gte(t,{begin:.8f})*lt(t,{end:.8f})':"
                               f"eof_action=repeat:repeatlast=1:shortest=0[base{index+1}]")
            filters.append(f"[base{len(visible)}]ass=comments.ass:fontsdir=fonts[outv]")
            (work / "filters.txt").write_text(";\n".join(filters), encoding="utf-8")
            command += ["-filter_complex_threads", "1", "-filter_complex_script", "filters.txt",
                        "-map", "[outv]"]
        else:
            command += ["-map", f"0:{info['video_index']}", "-vf", video_filter(info)]
        command += ["-map", "0:a:0?", "-t", str(duration),
                   "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                   "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                   "-progress", "pipe:1", "-nostats", "render.mp4"]
        with (work / "ffmpeg.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=work, stdout=subprocess.PIPE,
                                       stderr=log, text=True)
            last_update = 0.0
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us" and value.lstrip("-").isdigit():
                        now = time.monotonic()
                        if now - last_update >= 5:
                            percent = max(0, min(100, int(value) / 1e6 / duration * 100))
                            print(f"渲染进度 {percent:.1f}%", file=sys.stderr, flush=True)
                            last_update = now
                if process.wait():
                    log.seek(0)
                    raise ValueError("FFmpeg 渲染失败：\n" + log.read()[-4000:])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdout:
                    process.stdout.close()
        publish(work / "render.mp4", output, overwrite)


def save_ass(path: Path, text: str, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ass-", dir=path.parent) as directory:
        temp = Path(directory) / "comments.ass"
        temp.write_text(text, encoding="utf-8")
        publish(temp, path, overwrite)
