"""crawl_1337x_by_key.py 空结果页判定的测试。

针对 bug: 空的 table.table-list(0 行)被当成"有效且爬完",写入 done.txt。
修复: has_result_rows() 判定页面是否含真实结果行,main() 据此决定成败。

直接跑:python test_empty_page.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# Cloudflare 软墙 / 未渲染:表格骨架在,但 tbody 无行,也无分页
EMPTY_HTML = """
<html><body>
<table class="table-list">
  <thead><tr><th class="coll-1 name">Name</th></tr></thead>
  <tbody></tbody>
</table>
</body></html>
"""

# 正常有结果:1 行 + 分页到第 3 页
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


def main():
    # 1. 空页面:无结果行
    check(ck.has_result_rows(EMPTY_HTML) is False, "空表格 → has_result_rows False")
    check(ck.parse_listing(EMPTY_HTML, "005") == [], "空表格 → parse_listing 0 条")
    check(ck.detect_last_page(EMPTY_HTML) == 1, "空表格无分页 → last_page 1")

    # 2. 正常页面:有结果行
    check(ck.has_result_rows(POPULATED_HTML) is True, "有行 → has_result_rows True")
    items = ck.parse_listing(POPULATED_HTML, "005")
    check(len(items) == 1, "有行 → parse_listing 1 条")
    check(items and items[0]["name"] == "Foo Movie", "解析出正确 name")
    check(ck.detect_last_page(POPULATED_HTML) == 3, "分页 → last_page 3")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
