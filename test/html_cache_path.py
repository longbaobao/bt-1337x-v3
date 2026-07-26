"""测试 html_cache_path：data/html/<md5>.html。"""
import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
from pathlib import Path
from crawl_detail_1337x import html_cache_path


def test_returns_path_with_md5_and_html():
    url = "https://1337x.to/torrent/5006555/The-Night-House/"
    p = html_cache_path(url)
    assert isinstance(p, Path)
    assert p.parent.name == "html"
    assert p.suffix == ".html"
    # md5 hex is 32 chars
    assert len(p.stem) == 32


def test_deterministic():
    """同一 URL 多次调用返回相同路径。"""
    url = "https://1337x.to/torrent/123/"
    assert html_cache_path(url) == html_cache_path(url)


def test_different_urls_different_paths():
    assert html_cache_path("https://a/") != html_cache_path("https://b/")