"""Bounded avatar downloads, validated disk cache, and circular thumbnails."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import http.client
from io import BytesIO
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageOps

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 4096 * 4096


@dataclass
class AvatarStats:
    unique_urls: int = 0
    downloaded: int = 0
    cache_hits: int = 0
    failed_urls: int = 0
    fallback_comments: int = 0


def cache_path(directory: Path, url: str) -> Path:
    return directory / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".png")


def decode(data: bytes) -> Image.Image:
    if len(data) > MAX_BYTES:
        raise ValueError("头像文件超过 5 MiB")
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("头像像素过大")
        image.load()
        return ImageOps.exif_transpose(image).convert("RGBA")


def download(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("头像链接必须使用 HTTP 或 HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "live-recorder/0.1"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("头像文件超过 5 MiB")
    return data


def fetch(url: str, directory: Path) -> tuple[Image.Image | None, str]:
    path = cache_path(directory, url)
    try:
        if path.is_file():
            with path.open("rb") as file:
                return decode(file.read(MAX_BYTES + 1)), "cache_hits"
    except (OSError, ValueError, Image.DecompressionBombError):
        pass  # Corrupt cache is refetched; a failure still produces a placeholder.
    try:
        image = decode(download(url))
        # Cache a bounded, normalized thumbnail rather than an arbitrary original.
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".png", delete=False) as file:
            temp = Path(file.name)
        try:
            image.save(temp, format="PNG")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return image, "downloaded"
    except (OSError, ValueError, http.client.HTTPException, Image.DecompressionBombError):
        return None, "failed_urls"


def placeholder() -> Image.Image:
    image = Image.new("RGBA", (128, 128), (82, 96, 116, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((44, 22, 84, 62), fill=(225, 231, 240, 255))
    draw.ellipse((25, 70, 103, 146), fill=(225, 231, 240, 255))
    return image


def circle(image: Image.Image, size: int, opacity: float) -> Image.Image:
    scale = 4
    side = size * scale
    result = ImageOps.fit(image, (side, side), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (side, side))
    ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
    result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
    result = result.resize((size, size), Image.Resampling.LANCZOS)
    result.putalpha(result.getchannel("A").point(lambda value: round(value * opacity)))
    return result


def prepare(events: list, directory: Path) -> tuple[dict[str | None, Image.Image], AvatarStats]:
    directory.mkdir(parents=True, exist_ok=True)
    urls = sorted({e.comment.avatar_url for e in events if e.comment.avatar_url})
    stats = AvatarStats(unique_urls=len(urls))
    images = {None: placeholder()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda url: fetch(url, directory), urls)
        for url, (image, status) in zip(urls, results):
            setattr(stats, status, getattr(stats, status) + 1)
            if image is not None:
                images[url] = image
    stats.fallback_comments = sum(e.comment.avatar_url not in images or
                                  e.comment.avatar_url is None for e in events)
    return images, stats
