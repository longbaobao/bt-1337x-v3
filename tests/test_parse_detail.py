"""测试 parse_detail：HTML → 结构化字典。"""
import pytest
import sys
sys.path.insert(0, ".")
from crawl_detail_1337x import parse_detail, ParseError
from conftest import fixture


NIGHT_HOUSE_URL = "https://1337x.to/torrent/5006555/The-Night-House-2021-720p-AMZN-WEBRip-800MB-x264-GalaxyRG-TGx/"


def test_full_page_basic_fields():
    """完整页面：title, genre, magnet, infohash, imdb 都有"""
    html = fixture("detail_night_house.html")
    d = parse_detail(html, NIGHT_HOUSE_URL)
    assert d["detail_url"] == NIGHT_HOUSE_URL
    assert d["title"] == "THE NIGHT HOUSE"
    assert d["genre"] == "HORROR THRILLER"
    assert d["category"] == "Movies"
    assert d["language"] == "English"
    assert isinstance(d["seeders"], int) and d["seeders"] > 0
    assert isinstance(d["leechers"], int) and d["leechers"] >= 0
    assert d["info_hash"] and len(d["info_hash"]) >= 32
    assert d["imdb_id"] is not None
    assert d["imdb_id"].startswith("tt")


def test_full_page_resource_links():
    """完整页面：resource_links 是嵌套 dict，至少 magnet 字段"""
    html = fixture("detail_night_house.html")
    d = parse_detail(html, NIGHT_HOUSE_URL)
    assert "magnet" in d["resource_links"]
    assert d["resource_links"]["magnet"].startswith("magnet:")


def test_full_page_tags_array():
    """tags 是字符串数组"""
    html = fixture("detail_night_house.html")
    d = parse_detail(html, NIGHT_HOUSE_URL)
    assert isinstance(d["tags"], list)
    assert len(d["tags"]) > 0
    assert all(isinstance(t, str) for t in d["tags"])


def test_full_page_date_format():
    """date_uploaded / last_checked 是 yyyy-mm-dd hh:mm:ss 字符串"""
    html = fixture("detail_night_house.html")
    d = parse_detail(html, NIGHT_HOUSE_URL)
    import re
    fmt = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
    if d["date_uploaded"]:
        assert re.match(fmt, d["date_uploaded"]), f"date_uploaded: {d['date_uploaded']!r}"
    if d["last_checked"]:
        assert re.match(fmt, d["last_checked"]), f"last_checked: {d['last_checked']!r}"


def test_full_page_c_time_present():
    html = fixture("detail_night_house.html")
    d = parse_detail(html, NIGHT_HOUSE_URL)
    assert "c_time" in d
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", d["c_time"])


def test_no_imdb():
    """无 IMDB 时 imdb_url 和 imdb_id 都是 None"""
    html = fixture("detail_no_imdb.html")
    d = parse_detail(html, "https://1337x.to/torrent/xxx/")
    assert d["imdb_url"] is None
    assert d["imdb_id"] is None


def test_minimal_page_no_crash():
    """最小字段页面也不崩（title / magnet 至少要有）"""
    html = fixture("detail_minimal.html")
    d = parse_detail(html, "https://1337x.to/torrent/xxx/")
    assert d["title"]  # 至少有 title
    assert d["detail_url"]


def test_broken_page_raises():
    """完全损坏的页面抛 ParseError"""
    html = fixture("detail_broken.html")
    with pytest.raises(ParseError):
        parse_detail(html, "https://1337x.to/torrent/xxx/")
