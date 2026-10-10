import json
from pathlib import Path
import subprocess

import pytest

from live_recorder.cli import main
from live_recorder.media import executable, publish, run_checked, source_fps, video_filter


def test_atomic_publish_does_not_overwrite(tmp_path):
    source, target = tmp_path / "partial", tmp_path / "final"
    source.write_text("new")
    target.write_text("old")
    with pytest.raises(FileExistsError):
        publish(source, target, False)
    assert target.read_text() == "old"
    publish(source, target, True)
    assert target.read_text() == "new" and not source.exists()


@pytest.fixture
def binaries():
    try:
        return executable("ffmpeg"), executable("ffprobe")
    except ValueError as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("audio", [True, False])
@pytest.mark.parametrize("style", ["avatar", "text"])
def test_render_timed_chinese_text_audio_and_unsafe_paths(tmp_path, binaries, audio, style):
    ffmpeg, ffprobe = binaries
    folder = tmp_path / "space ' comma, 冒号: [test]"
    folder.mkdir()
    source, xml, output = folder / "in.mp4", folder / "in.xml", folder / "out.mp4"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=4"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
    run_checked(command)
    xml.write_text('<i><d p="1,1,25,16777215">中文弹幕测试</d></i>', encoding="utf-8")
    args = ["--video", str(source), "--danmaku", str(xml), "--output", str(output),
            "--duration", "2", "--font-size", "24", "--ffmpeg", ffmpeg, "--ffprobe", ffprobe,
            "--style", style, "--avatar-cache", str(tmp_path / "cache")]
    assert main(args) == 0
    data = json.loads(run_checked([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                   "-of", "json", str(output)]))
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["avg_frame_rate"]) == (320, 240, "24/1")
    assert any(s["codec_type"] == "audio" for s in data["streams"]) == audio
    assert abs(float(data["format"]["duration"]) - 4) < .2
    def pixels(at):
        return subprocess.run([ffmpeg, "-v", "error", "-ss", str(at), "-i", str(output),
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                              check=True, capture_output=True).stdout
    assert max(pixels(.5)) < 10
    assert max(pixels(2)) > 200
    # Consecutive 24 Hz frames must have distinct text positions even though
    # the source is only 10 Hz. This detects fps being applied AFTER ass.
    assert pixels(2) != pixels(2 + 1/24)
    assert max(pixels(3.5)) < 10
    old = output.read_bytes()
    assert main(args) == 1 and output.read_bytes() == old


def test_render_failure_cleans_partial_video(tmp_path, binaries, monkeypatch):
    from live_recorder import media
    from live_recorder.media import find_font
    ffmpeg, _ = binaries
    source = tmp_path / "bad.mp4"
    source.write_bytes(b"not a video")
    output = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="渲染失败"):
        media.render(ffmpeg=ffmpeg, video=source, output=output, ass="", font=find_font(None),
                     info={"video_index": 0, "upsample": False}, start=0, duration=1, crf=20,
                     preset="veryfast", overwrite=False)
    assert not output.exists() and not list(tmp_path.glob(".danmaku-*"))


@pytest.mark.parametrize("rate,upsample", [("5/1", True), ("24000/1001", True),
                         ("24/1", False), ("25/1", False), ("30000/1001", False), ("60/1", False)])
def test_fps_threshold_and_filter_order(rate, upsample):
    fps = source_fps({"avg_frame_rate": rate})
    assert (fps < 24) is upsample
    filters = video_filter({"upsample": upsample})
    assert filters.startswith("fps=24,ass=" if upsample else "ass=")


def test_fps_fallback_and_unknown():
    assert source_fps({"avg_frame_rate": "0/0", "r_frame_rate": "30/1"}) == 30
    with pytest.raises(ValueError, match="无法确定视频帧率"):
        source_fps({"avg_frame_rate": "0/0", "r_frame_rate": "0/0"})


@pytest.mark.parametrize("rate", ["24", "30000/1001", "60"])
def test_high_rate_video_preserves_original_rate(tmp_path, binaries, rate):
    ffmpeg, ffprobe = binaries
    source, xml, output = tmp_path / "source.mp4", tmp_path / "source.xml", tmp_path / "out.mp4"
    run_checked([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                 f"color=c=black:s=160x120:r={rate}:d=1", "-c:v", "libx264", str(source)])
    xml.write_text('<i><d p="0,1,25,16777215">你好</d></i>', encoding="utf-8")
    assert main(["--video", str(source), "--danmaku", str(xml), "--output", str(output),
                 "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]) == 0
    def timestamps(path):
        data = json.loads(run_checked([ffprobe, "-v", "error", "-select_streams", "v:0",
                         "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                         "-of", "json", str(path)]))
        return [f["best_effort_timestamp_time"] for f in data["frames"]]
    assert timestamps(output) == timestamps(source)


def test_full_flv_export_preserves_opening_frames(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source, xml, output = tmp_path / "source.flv", tmp_path / "source.xml", tmp_path / "out.mp4"
    run_checked([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=160x120:r=5:d=3", "-c:v", "libx264",
                 "-output_ts_offset", "0.014", "-f", "flv", str(source)])
    xml.write_text('<i><d p="0,1,25,16777215">开头</d></i>', encoding="utf-8")
    assert main(["--video", str(source), "--danmaku", str(xml), "--output", str(output),
                 "--style", "text", "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]) == 0
    data = json.loads(run_checked([ffprobe, "-v", "error", "-select_streams", "v:0",
                                  "-show_streams", "-of", "json", str(output)]))
    stream = data["streams"][0]
    assert float(stream["start_time"]) < .25
    assert float(stream["duration"]) >= 2.8
