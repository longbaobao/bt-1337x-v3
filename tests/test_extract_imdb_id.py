"""测试 extract_imdb_id：从 imdb URL 提取 ttXXXXXXX。"""
import sys
sys.path.insert(0, ".")
from crawl_detail_1337x import extract_imdb_id


def test_normal():
    assert extract_imdb_id("https://www.imdb.com/title/tt9731534") == "tt9731534"


def test_with_query():
    assert extract_imdb_id("https://www.imdb.com/title/tt1234567/?ref_=fn") == "tt1234567"


def test_with_trailing_slash():
    assert extract_imdb_id("https://www.imdb.com/title/tt9999999/") == "tt9999999"


def test_none():
    assert extract_imdb_id(None) is None


def test_invalid():
    assert extract_imdb_id("not a url") is None
    assert extract_imdb_id("https://example.com/no/imdb/here") is None