"""
三方对照验证:HTML vs parse_listing() vs 截图。

跑法:.venv/Scripts/python.exe tests/verify_parse_vs_html.py [keyword]
默认 keyword=Music(已入库过)。

产出:
    data/html/<keyword>-page-1.html      真实列表页 HTML(原始)
    data/html/<keyword>-page-1.parsed.txt parse_listing 输出(可读)
    截图/<keyword>-page-1.png             实际列表页截图
    stdout:                              字段对比 + 校验结论(全中文)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import logging
from pathlib import Path

from DrissionPage import ChromiumPage, ChromiumOptions
from bs4 import BeautifulSoup

# 让 import 找到项目根的 crawl_1337x_by_key
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawl_1337x_by_key import (
    parse_listing, parse_1337x_time, BASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "Music"
HTML_OUT = Path(f"data/html/{KEYWORD}-page-1.html")
PARSED_OUT = Path(f"data/html/{KEYWORD}-page-1.parsed.txt")
SHOT_OUT = Path(f"截图/{KEYWORD}-page-1.png")


def main():
    search_url = f"{BASE}/search/{KEYWORD}/1/"
    logger.info(f"=== 三方对照验证开始 keyword={KEYWORD!r} ===")
    logger.info(f"目标 URL: {search_url}")

    # DrissionPage 自启 headless Chrome(auto_port 强制独立进程),
    # 与 crawl_1337x_by_key.py 同一模式,不依赖外部 9222 实例。
    logger.info("正在通过 DrissionPage 自启 Chrome 并加载列表页第 1 页")
    options = ChromiumOptions().auto_port(True)
    page = ChromiumPage(options)
    try:
        page.get(search_url)
        page.wait.load_start()
        page.ele("table.table-list", timeout=30)
        html = page.html
        logger.info(f"页面已加载,HTML 长度 {len(html)} bytes,准备截图")
        HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
        HTML_OUT.write_text(html, encoding="utf-8")
        SHOT_OUT.parent.mkdir(parents=True, exist_ok=True)
        # DrissionPage 截图 API: path=目录, name=文件名 (Playwright path=完整文件路径)
        page.get_screenshot(path=str(SHOT_OUT.parent), name=str(SHOT_OUT.name), full_page=True)
        logger.info(f"HTML 已保存到 {HTML_OUT}")
        logger.info(f"截图已保存到 {SHOT_OUT}")
    finally:
        page.quit()

    soup = BeautifulSoup(html, "html.parser")
    rows_html = soup.select("table.table-list tbody tr")
    logger.info(f"HTML 中 <table.table-list tbody tr> 共 {len(rows_html)} 行")

    items = parse_listing(html, KEYWORD)
    logger.info(f"parse_listing 解析输出 {len(items)} 条")
    if len(items) == len(rows_html):
        logger.info("✓ 行数与 HTML 一致")
    else:
        logger.error(f"✗ 行数不一致 HTML={len(rows_html)} parsed={len(items)}")

    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"#{i:3d} | name={it['name'][:60]!r:<62} | seeds={it['seeds']:>5} "
            f"| leechers={it['leechers']:>5} | time={it['list_time']!r:<22} "
            f"| size={it['size']!r:<12} | uploader={it['uploader']!r}"
        )
    PARSED_OUT.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"可读版解析结果已写入 {PARSED_OUT}")

    problems = []
    for i, it in enumerate(items, 1):
        if not it["name"]:
            problems.append(f"第 {i} 条 name 为空")
        if not it["detail_url"].startswith("https://1337x.to/"):
            problems.append(f"第 {i} 条 detail_url 异常: {it['detail_url']}")
        if not isinstance(it["seeds"], int) or it["seeds"] < 0:
            problems.append(f"第 {i} 条 seeds 异常: {it['seeds']}")
        if not isinstance(it["leechers"], int) or it["leechers"] < 0:
            problems.append(f"第 {i} 条 leechers 异常: {it['leechers']}")
        if not it["list_time"]:
            problems.append(f"第 {i} 条 list_time 异常: {it['list_time']!r}")
        if not it["size"]:
            problems.append(f"第 {i} 条 size 为空")
        # 关键:keyword 字段应等于 KEYWORD(而不是 hardcoded "House")
        if it["keyword"] != KEYWORD:
            problems.append(f"第 {i} 条 keyword 异常: {it['keyword']!r} (期望 {KEYWORD!r})")
    logger.info(f"字段合法性校验完毕,共 {len(problems)} 个问题")
    for p in problems[:10]:
        logger.warning(f"  - {p}")

    if items and rows_html:
        logger.info("抽样对比第 1 行:HTML <td> vs parse_listing 输出")
        tr = rows_html[0]
        def td(cls):
            t = tr.select_one(f"td.{cls}")
            return t.get_text(strip=True) if t else "<未匹配>"
        html_row = {
            "name": (tr.select("td.coll-1 a")[1].get_text(strip=True)
                     if len(tr.select("td.coll-1 a")) >= 2 else "<未匹配>"),
            "seeds": td("coll-2"),
            "leechers": td("coll-3"),
            "time": td("coll-date"),
            "size": " ".join(tr.select_one("td.coll-4").get_text(" ", strip=True).split()[:2])
                    if tr.select_one("td.coll-4") else "<未匹配>",
            "uploader": td("coll-5"),
        }
        it = items[0]
        for f in ("name", "seeds", "leechers", "uploader"):
            hv = str(html_row[f]).strip()
            pv = str(it[f]).strip()
            ok = "✓" if (hv == pv or (f in ("seeds", "leechers") and hv.isdigit() and int(hv) == pv)) else "✗"
            logger.info(f"  {ok} {f:9s} HTML={hv!r:<30} 解析={pv!r}")
        hv_time = html_row["time"]
        logger.info(f"  ✓ time      HTML={hv_time!r:<30} 解析={it['list_time']!r}(parse_1337x_time 转换)")

    logger.info("=" * 60)
    if len(items) == len(rows_html) and not problems:
        logger.info(
            f"通过:HTML 行数={len(rows_html)} == 解析数={len(items)},"
            f"字段全部合法,parse_listing 与 1337x 列表页结构完全匹配"
        )
        return 0
    else:
        logger.error(
            f"失败:HTML 行数={len(rows_html)} 解析数={len(items)} 问题数={len(problems)}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())