import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py 纯解析助手测试(无网络/Chrome/MongoDB 依赖)。

覆盖:
- checkpoint 断点续爬(load/save/clear + 文件名安全化 + 多 key 隔离)
- 空结果页判定(has_result_rows + parse_listing + detect_last_page)

直接跑:python test/parsing.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import tempfile
from pathlib import Path

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# ─── checkpoint 断点续爬 ────────────────────────────────────────────────────

def test_checkpoint():
    print("[checkpoint]")
    ck.CHECKPOINT_DIR = Path(tempfile.mkdtemp(prefix="cp_test_"))

    check(ck.load_checkpoint("005") == (0, 0), "无 checkpoint 返回 (0,0)")

    ck.save_checkpoint("005", 7, 50)
    check(ck.load_checkpoint("005") == (7, 50), "save/load 往返 (7,50)")

    ck.save_checkpoint("005", 8, 50)
    check(ck.load_checkpoint("005") == (8, 50), "覆盖写推进到 (8,50)")

    ck.save_checkpoint("006", 3, 20)
    check(ck.load_checkpoint("005") == (8, 50), "005 不受 006 影响")
    check(ck.load_checkpoint("006") == (3, 20), "006 独立 (3,20)")

    p = ck._checkpoint_path("pan.quark/foo bar")
    check("/" not in p.name and "\\" not in p.name and " " not in p.name,
          f"非法字符被安全化: {p.name}")

    check(ck._checkpoint_path("005") != ck._checkpoint_path("006"),
          "不同 key checkpoint 路径不同")

    ck.clear_checkpoint("005")
    check(ck.load_checkpoint("005") == (0, 0), "clear 后回到 (0,0)")

    ck.clear_checkpoint("nonexistent")
    check(True, "clear 不存在的 key 不抛异常")


# ─── 空结果页判定 ─────────────────────────────────────────────────────────

EMPTY_HTML = """
<html><body>
<table class="table-list">
  <thead><tr><th class="coll-1 name">Name</th></tr></thead>
  <tbody></tbody>
</table>
</body></html>
"""

POPULATED_HTML = """
<html><body>
<table class="table-list">
  <thead><tr><th class="coll-1 name">Name</th></tr></thead>
  <tbody>
    <tr>
      <td class="coll-1 name"><a href="/x/1/">icon</a><a href="/torrent/123/foo/">Foo Movie</a></td>
      <td class="coll-2 seeds">42</td>
      <td class="coll-3 leeches">7</td>
      <td class="coll-date">Oct. 21st '22</td>
      <td class="coll-4 size">1.6 GB</td>
      <td class="coll-5">uploaderX</td>
    </tr>
  </tbody>
</table>
<div class="pagination">
  <a href="/search/foo/2/">2</a>
  <a href="/search/foo/3/">3</a>
</div>
</body></html>
"""


def test_empty_page():
    print("[empty_page]")
    check(ck.has_result_rows(EMPTY_HTML) is False, "空表格 → has_result_rows False")
    check(ck.parse_listing(EMPTY_HTML, "005") == [], "空表格 → parse_listing 0 条")
    check(ck.detect_last_page(EMPTY_HTML) == 1, "空表格无分页 → last_page 1")

    check(ck.has_result_rows(POPULATED_HTML) is True, "有行 → has_result_rows True")
    items = ck.parse_listing(POPULATED_HTML, "005")
    check(len(items) == 1, "有行 → parse_listing 1 条")
    check(items and items[0]["name"] == "Foo Movie", "解析出正确 name")
    check(ck.detect_last_page(POPULATED_HTML) == 3, "分页 → last_page 3")


def main():
    test_checkpoint()
    test_empty_page()
    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())