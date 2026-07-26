"""测试 now_str：返回 yyyy-mm-dd hh:mm:ss 格式。"""
import re
import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
from crawl_detail_1337x import now_str


def test_format():
    s = now_str()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s), f"格式不符: {s!r}"