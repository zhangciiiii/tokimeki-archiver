from io import BytesIO
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest
from PIL import Image, ImageFont

from live_recorder import avatars
from live_recorder.cli import main
from live_recorder.danmaku import Comment, Event, Stats, read_xml, schedule
from live_recorder.media import executable, find_font, run_checked


def raw(root, text, timestamp, url):
    info = [[0] * 16, text, [0, "PRIVATE_NAME"]]
    info[0][15] = {"user": {"base": {"name": "PRIVATE_NAME", "face": url}}}
    node = ET.SubElement(root, "o", timestamp=str(timestamp))
    node.text = json.dumps({"body": json.dumps({"cmd": "DANMU_MSG", "info": info})})


def test_matching_duplicates_and_late_raw_metadata(tmp_path):
    root = ET.Element("i")
    for time, text in [(1, "重复"), (2, "重复"), (3, "无头像")]:
        ET.SubElement(root, "d", p=f"{time},1,25,16777215,100", uid="0",
                      user="PRIVATE_NAME").text = text
    raw(root, "重复", 100, "https://example.test/1.png")
    raw(root, "重复", 100, "https://example.test/2.png")
    path = tmp_path / "input.xml"
    ET.ElementTree(root).write(path, encoding="utf-8")
    comments, stats = read_xml(path)
    assert [c.avatar_url for c in comments] == ["https://example.test/1.png",
                                               "https://example.test/2.png", None]
    assert [c.time for c in comments] == [1, 2, 3]
    assert stats.total == 3
    assert all("PRIVATE_NAME" not in c.text for c in comments)


def test_cache_download_hit_corrupt_refetch_and_failed_fallback(tmp_path, monkeypatch):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "red").save(buffer, format="PNG")
    calls = []
    def download(url):
        calls.append(url)
        if url.endswith("broken"):
            return b"not an image"
        return buffer.getvalue()
    monkeypatch.setattr(avatars, "download", download)
    url = "https://example.test/avatar"
    comments = [Comment(0, "text", 0, url), Comment(1, "text", 0, url),
                Comment(2, "text", 0, None), Comment(3, "text", 0, url + "broken")]
    events = [Event(c.time, c.time + 8, 100, 0, c) for c in comments]
    images, stats = avatars.prepare(events, tmp_path)
    assert stats.downloaded == 1 and stats.failed_urls == 1 and stats.fallback_comments == 2
    assert calls.count(url) == 1 and url in images
    assert avatars.fetch(url, tmp_path)[1] == "cache_hits"
    avatars.cache_path(tmp_path, url).write_bytes(b"broken cache")
    assert avatars.fetch(url, tmp_path)[1] == "downloaded"
    monkeypatch.setattr(avatars, "download", lambda _: (_ for _ in ()).throw(OSError("offline")))
    assert avatars.fetch(url + "offline", tmp_path) == (None, "failed_urls")


def test_download_rejects_non_http_and_oversized_images(monkeypatch):
    with pytest.raises(ValueError):
        avatars.download("file:///tmp/private.png")
    with pytest.raises(ValueError):
        avatars.decode(b"x" * (avatars.MAX_BYTES + 1))
    buffer = BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    monkeypatch.setattr(avatars, "MAX_PIXELS", 10)
    with pytest.raises(ValueError, match="像素"):
        avatars.decode(buffer.getvalue())


def test_circular_alpha_and_opacity():
    image = avatars.circle(Image.new("RGBA", (32, 20), "red"), 24, .5)
    assert image.size == (24, 24)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((12, 12))[3] == 128


def test_avatar_width_participates_in_collision_detection():
    font = ImageFont.truetype(str(find_font(None)), 20)
    comments = [Comment(0, "字", 0), Comment(.45, "字", 0)]
    options = dict(width=720, height=45, video_duration=10, font=font, font_size=20, duration=8)
    text = schedule(comments, Stats(), style="text", **options)
    stats = Stats()
    rich = schedule(comments, stats, style="avatar", **options)
    assert len(text) == 2 and len(rich) == 1 and stats.crowded == 1
    assert rich[0].width == text[0].width + rich[0].text_offset


def test_cached_real_avatar_sync_preview_and_no_username(tmp_path):
    try:
        ffmpeg, ffprobe = executable("ffmpeg"), executable("ffprobe")
    except ValueError as exc:
        pytest.skip(str(exc))
    source, xml = tmp_path / "source.mp4", tmp_path / "source.xml"
    cache = tmp_path / "cache ' 中文"
    cache.mkdir()
    url = "https://example.test/red.png"
    Image.new("RGB", (64, 64), "red").save(avatars.cache_path(cache, url))
    root = ET.Element("i")
    ET.SubElement(root, "d", p="1,1,25,16777215,100", timestamp="100",
                  user="PRIVATE_NAME").text = "中文正文"
    raw(root, "中文正文", 100, url)
    ET.ElementTree(root).write(xml, encoding="utf-8")
    run_checked([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=320x240:r=5:d=4", "-c:v", "libx264", str(source)])
    args = ["--video", str(source), "--danmaku", str(xml), "--avatar-cache", str(cache),
            "--font-size", "24", "--duration", "2", "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]
    output, preview = tmp_path / "full.mp4", tmp_path / "preview.mp4"
    assert main(args + ["--output", str(output)]) == 0
    assert main(args + ["--output", str(preview), "--preview-start", "2", "--preview-duration", "1"]) == 0
    assert "PRIVATE_NAME" not in output.with_suffix(".ass").read_text()
    def red_bounds(path, at):
        data = subprocess.run([ffmpeg, "-v", "error", "-ss", str(at), "-i", str(path),
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              check=True, capture_output=True).stdout
        xs = [(i // 3) % 320 for i in range(0, len(data), 3)
              if data[i] > 150 and data[i+1] < 70 and data[i+2] < 70]
        return (min(xs), max(xs)) if xs else None
    assert red_bounds(output, .5) is None and red_bounds(output, 3.5) is None
    first, second = red_bounds(output, 2), red_bounds(output, 2 + 1/24)
    assert first and second and second[0] < first[0]
    assert abs(first[0] - red_bounds(preview, 0)[0]) <= 2
    comments, stats = read_xml(xml)
    event = schedule(comments, stats, width=320, height=240, video_duration=4,
                     font=ImageFont.truetype(str(find_font(None)), 24), font_size=24,
                     duration=2, style="avatar")[0]
    assert abs(first[0] - event.x_at(2, 320)) <= 3
