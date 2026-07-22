"""
1337x 搜索结果抓取：单个关键词全量翻页，落 MongoDB。

连接指定 CDP URL 的 Chrome（不新开进程），复用现有 context。
keyword 与 cdp_url 通过命令行参数传入，方便被 crawl_1337x_by_keys.py 并发调用。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import os
import re
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
BASE = "https://1337x.to"
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "bt_13337x_spider_db"
COLL_NAME = "bt_info_list"

# 全局并发设置:与 wrapper 共享同一环境变量名;本脚本是单 key 单进程,
# 不实际使用此值,仅在启动日志中 echo 以保持 API 一致
ENV_CONCURRENCY = "CRAWL_1337X_CONCURRENCY"

# 单页之间间隔（秒），礼貌爬取
PAGE_SLEEP = 1.0

# 1337x 时间格式: "Oct. 21st '22" / "2am Jul. 13th" / "Jul. 29th '24"
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_1337x_time(s: str) -> str:
    """1337x 列表里的时间文本 → 'yyyy-mm-dd hh:mm:ss'。

    支持的形式:
        'Oct. 21st 22'  → '2022-10-21 00:00:00'
        '2am Jul. 13th' → '<当前年>-07-13 02:00:00'
        'Jul. 29th 24'  → '2024-07-29 00:00:00'
    无法解析时返回空串。
    """
    if not s:
        return ""
    # 去掉前导时间，如 "2am "、"10pm "
    s = re.sub(r"^\d{1,2}(?:am|pm)\s+", "", s.strip(), flags=re.IGNORECASE)
    # 形如 "Oct. 21st '22" 或 "Jul. 29th 24"
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*'?(?:(\d{2,4}))?", s)
    if not m:
        return ""
    month_num = MONTH_MAP.get(m.group(1).lower()[:3])
    if not month_num:
        return ""
    day = int(m.group(2))
    yy_raw = m.group(3)
    if yy_raw:
        year = int(yy_raw)
        if year < 100:
            year += 2000
    else:
        year = datetime.now().year
    return f"{year:04d}-{month_num:02d}-{day:02d} 00:00:00"


def detect_last_page(html: str) -> int:
    """从分页栏提取最后一页页码。1337x 的分页 DOM：<div class="pagination">...<a href="/search/House/N/">N</a>...</div>"""
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.select("div.pagination a[href]"):
        m = re.search(r"/search/[^/]+/(\d+)/?", a["href"])
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 1


def parse_listing(html: str, keyword: str) -> list[dict]:
    """解析搜索结果表格的每一行。

    1337x 真实列结构（注意 time 用的是 coll-date，不是 coll-4）：
        coll-1     name (含 HD 图标)
        coll-2     seeds
        coll-3     leeches
        coll-date  time
        coll-4     size (mobile 视图拼了 seeds 在末尾，形如 "1.6 GB 7545")
        coll-5     uploader
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tr in soup.select("table.table-list tbody tr"):
        try:
            # coll-1 里通常有两个 <a>：第一个是图标（HD 标志），第二个是名字
            anchors = tr.select("td.coll-1 a")
            name_anchor = anchors[1] if len(anchors) >= 2 else (anchors[0] if anchors else None)
            if not name_anchor:
                continue
            name = name_anchor.get_text(strip=True)
            detail_url = urljoin(BASE, name_anchor.get("href", ""))

            seeds_txt = tr.select_one("td.coll-2").get_text(strip=True) if tr.select_one("td.coll-2") else "0"
            leech_txt = tr.select_one("td.coll-3").get_text(strip=True) if tr.select_one("td.coll-3") else "0"

            # 时间列 class 是 coll-date，不是 coll-4
            time_txt = tr.select_one("td.coll-date").get_text(strip=True) if tr.select_one("td.coll-date") else ""

            # size 单元格 mobile 视图拼了 seeds 在末尾，形如 "1.6 GB 7545"，只取前两段
            size_cell = tr.select_one("td.coll-4")
            size = " ".join(size_cell.get_text(" ", strip=True).split()[:2]) if size_cell else ""

            uploader = tr.select_one("td.coll-5").get_text(strip=True) if tr.select_one("td.coll-5") else ""

            item = {
                "_id": hashlib.md5(detail_url.encode()).hexdigest(),
                "name": name,
                "detail_url": detail_url,
                "seeds": int(seeds_txt) if seeds_txt.isdigit() else 0,
                "leechers": int(leech_txt) if leech_txt.isdigit() else 0,
                "size": size,
                "list_time": parse_1337x_time(time_txt),
                "uploader": uploader,
                "keyword": keyword,
                "source": "1337x",
                "c_time": datetime.now(),
            }
            items.append(item)
        except Exception as e:
            logger.warning(f"解析行失败: {e}")
    return items


def load_page_with_retry(page, url: str, page_num: int, retries: int = 3) -> str | None:
    """带重试地加载页面，超出重试次数返回 None（让上层跳过）。"""
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("table.table-list", timeout=30000)
            return page.content()
        except (PWTimeout, Exception) as e:
            logger.warning(f"第 {page_num} 页第 {attempt}/{retries} 次加载失败: {type(e).__name__}")
            if attempt < retries:
                time.sleep(2 * attempt)  # 退避
    return None


def main(keyword: str, cdp_url: str = CDP_URL):
    search_url = f"{BASE}/search/{keyword}/{{page}}/"
    env_val = os.environ.get(ENV_CONCURRENCY, "").strip()
    logger.info(f"=== 开始抓取 keyword={keyword!r} cdp_url={cdp_url} ===")
    if env_val:
        logger.info(f"全局并发设置:环境变量 {ENV_CONCURRENCY}={env_val}(本脚本单 key 单进程,仅记录)")
    started_at = time.time()

    client = MongoClient(MONGO_URI)
    coll = client[DB_NAME][COLL_NAME]
    logger.info(f"MongoDB 已连接: {MONGO_URI}{DB_NAME}.{COLL_NAME}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        logger.info(f"Playwright 已连接 CDP 并创建新页面")

        # 先打开第 1 页，探测总页数
        first_html = load_page_with_retry(page, search_url.format(page=1), 1)
        if first_html is None:
            logger.error("第 1 页加载失败，无法启动")
            return
        last_page = detect_last_page(first_html)
        logger.info(f"搜索 {keyword} 共 {last_page} 页，开始全量翻页")

        # 第一页已加载，复用
        items = parse_listing(first_html, keyword)
        new_count = 0
        for it in items:
            if coll.update_one({"_id": it["_id"]}, {"$set": it}, upsert=True).upserted_id:
                new_count += 1
        logger.info(f"[1/{last_page}] 解析 {len(items)} 条，新写入 MongoDB {new_count} 条")

        # 翻 2..N
        for n in range(2, last_page + 1):
            url = search_url.format(page=n)
            html = load_page_with_retry(page, url, n)
            if html is None:
                logger.warning(f"第 {n} 页重试耗尽，跳过")
                continue

            items = parse_listing(html, keyword)
            new_count = 0
            for it in items:
                if coll.update_one({"_id": it["_id"]}, {"$set": it}, upsert=True).upserted_id:
                    new_count += 1
            logger.info(f"[{n}/{last_page}] 解析 {len(items)} 条，新写入 MongoDB {new_count} 条")
            time.sleep(PAGE_SLEEP)
            time.sleep(PAGE_SLEEP)

        total = coll.count_documents({"keyword": keyword})
        elapsed = time.time() - started_at
        logger.info(
            f"=== 完成 keyword={keyword} 耗时 {elapsed:.1f}s "
            f"库内 {DB_NAME}.{COLL_NAME} 中该 keyword 共 {total} 条 ==="
        )
        page.close()
        logger.info("Playwright 页面已关闭")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="1337x 单关键词全量抓取（落 MongoDB）",
    )
    parser.add_argument(
        "keyword",
        help="搜索关键词（会作为 MongoDB 文档 keyword 字段值）",
    )
    parser.add_argument(
        "--cdp-url",
        default=CDP_URL,
        help=f"Chrome DevTools Protocol URL（默认 {CDP_URL}）",
    )
    args = parser.parse_args()
    main(keyword=args.keyword, cdp_url=args.cdp_url)