"""XML parsing, collision-free scheduling, and ASS generation."""
from collections import defaultdict, deque
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Comment:
    time: float
    text: str
    color: int
    avatar_url: str | None = None


@dataclass
class Stats:
    total: int = 0
    invalid: int = 0
    out_of_range: int = 0
    crowded: int = 0
    scheduled: int = 0
    visible: int = 0


@dataclass(frozen=True)
class Event:
    start: float
    end: float
    width: int
    y: int
    comment: Comment
    avatar_size: int = 0
    text_offset: int = 0

    def x_at(self, time: float, screen: int) -> float:
        return screen - (screen + self.width) * (time - self.start) / (self.end - self.start)


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def read_xml(path: Path) -> tuple[list[Comment], Stats]:
    comments, stats = [], Stats()
    avatars = defaultdict(deque)
    keys = []
    # <d> is the sole source of events/timing. <o> only enriches those events.
    for _, node in ET.iterparse(path, events=("end",)):
        if node.tag == "o":
            try:
                body = json.loads(json.loads(node.text).get("body", "{}"))
                if body.get("cmd", "").startswith("DANMU_MSG"):
                    info = body["info"]
                    url = None
                    try:
                        url = info[0][15]["user"]["base"].get("face")
                    except (IndexError, KeyError, TypeError, AttributeError):
                        pass
                    key = (node.get("timestamp"), normalize_text(info[1]))
                    avatars[key].append(url if isinstance(url, str) and url else None)
            except (ValueError, TypeError, AttributeError, IndexError, KeyError):
                pass
        if node.tag == "d":
            stats.total += 1
            try:
                fields = node.attrib["p"].split(",")
                time, color = float(fields[0]), int(fields[3])
                text = normalize_text("".join(node.itertext()))
                if not math.isfinite(time) or not 0 <= color <= 0xFFFFFF or not text:
                    raise ValueError("Invalid comment")
                comments.append(Comment(time, text, color))
                keys.append((node.get("timestamp") or (fields[4] if len(fields) > 4 else None), text))
            except (KeyError, IndexError, ValueError):
                stats.invalid += 1
        node.clear()
    # Resolve after parsing: some exporters put raw metadata AFTER <d>.
    comments = [replace(c, avatar_url=avatars[k].popleft() if avatars[k] else None)
                for c, k in zip(comments, keys)]
    return sorted(comments, key=lambda c: c.time), stats


def can_follow(previous: Event, start: float, width: int, screen: int,
               duration: float, gap: int) -> bool:
    if start >= previous.end:
        return True
    old_speed = (screen + previous.width) / (previous.end - previous.start)
    new_speed = (screen + width) / duration
    # Distance between the previous tail and new head is linear in time.
    # Check both ends of their common lifetime to prevent initial overlap AND catch-up.
    initial_gap = old_speed * (start - previous.start) - previous.width
    final_gap = screen - new_speed * (previous.end - start)
    return min(initial_gap, final_gap) >= gap


def schedule(comments: list[Comment], stats: Stats, *, width: int, height: int,
             video_duration: float, font, font_size: int, duration: float,
             offset: float = 0, style: str = "text") -> list[Event]:
    margin = max(4, round(height * .01))
    row_height = math.ceil(font_size * 1.5) + 4
    avatar_size = font_size if style == "avatar" else 0
    text_offset = avatar_size + max(4, round(font_size * .2)) if avatar_size else 0
    count = (height - 2 * margin) // row_height
    if count < 1:
        raise ValueError("字号过大，画面无法容纳一行弹幕")
    lanes: list[Event | None] = [None] * count
    events = []
    cursor = 0
    for comment in comments:
        start = round(comment.time + offset, 2)
        if start < 0 or start >= video_duration:
            stats.out_of_range += 1
            continue
        # Conservative width includes shaping/outline allowance. Font used here is
        # also supplied to libass, avoiding measurement against an unrelated font.
        display = safe_text(comment.text)
        text_width = math.ceil(max(font.getlength(display),
                                   font.getbbox(display)[2]) * 1.2) + 8 + text_offset
        for step in range(count):
            lane = (cursor + step) % count
            previous = lanes[lane]
            if previous is None or can_follow(previous, start, text_width, width,
                                               duration, gap=max(8, font_size // 2)):
                event = Event(start, start + duration, text_width,
                              margin + lane * row_height, comment, avatar_size, text_offset)
                lanes[lane] = event
                events.append(event)
                cursor = (lane + 1) % count
                break
        else:
            stats.crowded += 1
    stats.scheduled = len(events)
    return events


def ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{cs:02}"


def safe_text(text: str) -> str:
    # ASS has no reliable literal-backslash escape across renderers. Fullwidth
    # variants preserve legibility without allowing user text to inject commands.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ").replace("\r", " ")


def ass_color(rgb: int) -> str:
    return f"&H{rgb & 255:02X}{(rgb >> 8) & 255:02X}{(rgb >> 16) & 255:02X}&"


def make_ass(events: list[Event], stats: Stats, *, width: int, height: int,
             font_name: str, font_size: int, opacity: float,
             clip_start: float, clip_duration: float) -> str:
    alpha = round((1 - opacity) * 255)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    stats.visible = 0
    for event in events:
        begin = max(event.start, clip_start)
        end = min(event.end, clip_start + clip_duration)
        if end <= begin:
            continue
        x1 = event.x_at(begin, width) + event.text_offset
        x2 = event.x_at(end, width) + event.text_offset
        tags = (f"{{\\an7\\q2\\move({x1:.2f},{event.y},{x2:.2f},{event.y})"
                f"\\c{ass_color(event.comment.color)}\\alpha&H{alpha:02X}&}}")
        lines.append(f"Dialogue: 0,{ass_time(begin-clip_start)},{ass_time(end-clip_start)},"
                     f"Danmaku,,0,0,0,,{tags}{safe_text(event.comment.text)}\n")
        stats.visible += 1
    return "".join(lines)
