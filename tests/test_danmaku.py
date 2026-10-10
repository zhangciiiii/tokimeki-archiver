import pytest
from PIL import ImageFont

from live_recorder.danmaku import (Comment, Event, Stats, ass_color, ass_time,
                                   can_follow, make_ass, read_xml, safe_text, schedule)
from live_recorder.media import find_font


def test_parse_uses_relative_seconds_and_only_normalized_messages(tmp_path):
    path = tmp_path / "comments.xml"
    path.write_text('''<i><o timestamp="123">duplicate raw event</o>
        <d p="170.075,1,25,16777215,999999,0,0,0">你好 &amp; 欢迎</d>
        <d p="2,1,25,16711680">second</d><s>gift</s>
        <d p="nan,1,25,0">bad</d><d p="1,1,25,16777216">bad</d>
        <d p="broken">bad</d><d p="3,1,25,0">  </d></i>''', encoding="utf-8")
    comments, stats = read_xml(path)
    assert [c.time for c in comments] == [2, 170.075]
    assert comments[1].text == "你好 & 欢迎"
    assert stats.total == 6 and stats.invalid == 4


def test_ass_format_and_text_cannot_inject_tags():
    assert ass_color(0x112233) == "&H332211&"
    assert ass_time(3661.25) == "1:01:01.25"
    assert ass_time(59.999) == "0:01:00.00"
    assert safe_text(r"{\pos(0,0)}\N") == "｛＼pos(0,0)｝＼N"


def test_fast_long_comment_cannot_catch_previous():
    previous = Event(0, 8, 100, 0, Comment(0, "short", 0))
    # At t=2 the tail has cleared the entrance; a much faster long message
    # would still catch it near the left edge, so this must be rejected.
    assert not can_follow(previous, 2, 600, 720, 8, 8)
    assert can_follow(previous, 2, 100, 720, 8, 8)
    assert can_follow(previous, 8, 600, 720, 8, 8)


def test_offset_overflow_and_crowding_are_counted_without_delay():
    font = ImageFont.truetype(str(find_font(None)), 20)
    comments = [Comment(0, "early", 0), Comment(2, "你好", 0),
                Comment(2, "同一时间", 0), Comment(20, "late", 0)]
    stats = Stats(total=4)
    events = schedule(comments, stats, width=720, height=45, video_duration=10,
                      font=font, font_size=20, duration=8, offset=-1)
    assert len(events) == 1 and events[0].start == 1
    assert stats.out_of_range == 2 and stats.crowded == 1


def test_preview_clips_motion_instead_of_restarting_it():
    event = Event(10, 18, 80, 10, Comment(10, "你好", 0xFF0000))
    stats = Stats()
    ass = make_ass([event], stats, width=720, height=1280, font_name="test", font_size=30,
                   opacity=.5, clip_start=14, clip_duration=2)
    assert r"\move(320.00,10,120.00,10)" in ass
    assert "0:00:00.00,0:00:02.00" in ass
    assert r"\c&H0000FF&\alpha&H80&" in ass
    assert stats.visible == 1


def test_malformed_xml_fails(tmp_path):
    import xml.etree.ElementTree as ET
    path = tmp_path / "broken.xml"
    path.write_text("<i><d>")
    with pytest.raises(ET.ParseError):
        read_xml(path)
