"""测试 parse_relative_time：将 '4 years ago' 等相对时间转为 yyyy-mm-dd hh:mm:ss。"""
from datetime import datetime
import sys
sys.path.insert(0, ".")
from crawl_detail_1337x import parse_relative_time


REF = datetime(2026, 7, 21, 19, 0, 0)


def test_years_ago():
    assert parse_relative_time("4 years ago", REF) == "2022-07-21 00:00:00"


def test_hours_ago():
    assert parse_relative_time("11 hours ago", REF) == "2026-07-21 08:00:00"


def test_minutes_ago():
    assert parse_relative_time("30 minutes ago", REF) == "2026-07-21 18:30:00"


def test_days_ago():
    assert parse_relative_time("3 days ago", REF) == "2026-07-18 00:00:00"


def test_empty():
    assert parse_relative_time("", REF) == ""


def test_unparseable_returns_empty():
    assert parse_relative_time("yesterday", REF) == ""
    assert parse_relative_time("foo bar baz", REF) == ""


def test_singular_year():
    """'1 year ago' 单数也应解析"""
    assert parse_relative_time("1 year ago", REF) == "2025-07-21 00:00:00"


def test_unparseable_with_trailing_text():
    """trailing garbage 在 re.fullmatch 下应返回空串（旧 re.match 会误接受）。"""
    assert parse_relative_time("3 days ago extra", REF) == ""
    assert parse_relative_time("4 years ago unexpected", REF) == ""
    assert parse_relative_time("11 hours ago something else", REF) == ""
    assert parse_relative_time("3 days ago ", REF) == "2026-07-18 00:00:00"  # trailing space 被 strip 后仍是合法


def test_year_boundary_leap_day():
    """闰年 Feb 29 减 1 年应 clamp 到 Feb 28（2023 年无 29 日）。"""
    ref = datetime(2024, 2, 29, 10, 30, 0)
    assert parse_relative_time("1 year ago", ref) == "2023-02-28 00:00:00"


def test_month_boundary_31st():
    """3 月 31 日减 1 月应 clamp 到 2 月最后一天（2024 是闰年 → Feb 29）。"""
    ref = datetime(2024, 3, 31, 8, 15, 0)
    assert parse_relative_time("1 month ago", ref) == "2024-02-29 00:00:00"