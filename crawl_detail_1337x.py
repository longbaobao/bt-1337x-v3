"""
1337x 详情页爬虫：从 bt_info_list 取 detail_url，抓 HTML 落本地，解析入库。

DrissionPage 自启 headless Chrome（auto_port 强制独立进程），不接管外部 9222 实例。
- 并发模型：ThreadPoolExecutor(max_workers=concurrency)，每个 worker 一个独立 tab
- 单 tab timeout 走 future.result(timeout=RUN_ONE_BUDGET)
- 浏览器死亡快检：批开始前先 new_tab() 一次，失败则整批跳过
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import re
import calendar
import hashlib
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from threading import Semaphore
from bs4 import BeautifulSoup
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.errors import ElementNotFoundError, PageDisconnectedError

# 复用 crawl_1337x 共享常量（兼容旧名 / 新名 crawl_1337x_by_key）
from crawl_1337x_by_key import MONGO_URI, DB_NAME, fetch_with_cf_bypass
COLL_LIST = "bt_info_list"
COLL_DETAIL = "bt_info_detail"

HTML_DIR = Path("data/html")

BATCH = 100
MAX_RETRIES = 3
RETRY_BACKOFF = (2, 4, 8)  # 秒
RUN_ONE_BUDGET = 60  # 秒
INTER_REQUEST_SLEEP = 0.5  # 秒，每次 fetch 成功后的间隔
CONCURRENCY = 1  # 默认并行 page 数


def now_str() -> str:
    """当前时间 → 'yyyy-mm-dd hh:mm:ss'。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def html_cache_path(detail_url: str) -> Path:
    """data/html/<md5_hex>.html"""
    return HTML_DIR / (hashlib.md5(detail_url.encode()).hexdigest() + ".html")


def parse_relative_time(s: str, ref_now: datetime) -> str:
    """'4 years ago' / '11 hours ago' / '30 minutes ago' / '3 days ago'
    → '<ref_now - delta>' 格式化为 'yyyy-mm-dd hh:mm:ss'。
    无法解析或空字符串返回空串。
    year/month/day 单位将时间部分归零（00:00:00）；hour/minute 保留时间计算。"""
    if not s:
        return ""
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s+(year|years|month|months|day|days|hour|hours|minute|minutes)\s+ago", s, re.IGNORECASE)
    if not m:
        return ""
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("year"):
        target_year = ref_now.year - n
        last_day = calendar.monthrange(target_year, ref_now.month)[1]
        target_day = min(ref_now.day, last_day)
        target = datetime(target_year, ref_now.month, target_day, 0, 0, 0)
    elif unit.startswith("month"):
        total = ref_now.year * 12 + (ref_now.month - 1) - n
        new_year, m_idx = divmod(total, 12)
        target_month = m_idx + 1
        last_day = calendar.monthrange(new_year, target_month)[1]
        target_day = min(ref_now.day, last_day)
        target = datetime(new_year, target_month, target_day, 0, 0, 0)
    elif unit.startswith("day"):
        target = (ref_now - timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif unit.startswith("hour"):
        target = ref_now - timedelta(hours=n)
    elif unit.startswith("minute"):
        target = ref_now - timedelta(minutes=n)
    else:
        return ""
    return target.strftime("%Y-%m-%d %H:%M:%S")


def extract_imdb_id(imdb_url: str | None) -> str | None:
    """从 'https://www.imdb.com/title/tt9731534' 提取 'tt9731534'。
    None 或无匹配返回 None。"""
    if not imdb_url:
        return None
    m = re.search(r"(tt\d+)", imdb_url)
    return m.group(1) if m else None


# ============================================================
# DB 层（Task 5）
# ============================================================
from pymongo import ReturnDocument


def claim_one(coll_list, doc_id: str) -> dict | None:
    """CAS: 把 pending 的 doc 抢占为 processing。返回 claimed 文档；被抢走返回 None。"""
    claimed = coll_list.find_one_and_update(
        {"_id": doc_id, "detail_status": "pending"},
        {"$set": {"detail_status": "processing", "detail_started_at": now_str()}},
        return_document=ReturnDocument.AFTER,
    )
    return claimed


def mark_done(coll_list, doc_id: str) -> None:
    """成功完成 → done。"""
    coll_list.update_one(
        {"_id": doc_id},
        {"$set": {"detail_status": "done", "detail_processed_at": now_str()},
         "$unset": {"detail_started_at": "", "detail_error": ""}},
    )


def mark_failed(coll_list, doc_id: str, error_msg: str) -> None:
    """失败 → failed。保留 error 信息供排查。"""
    coll_list.update_one(
        {"_id": doc_id},
        {"$set": {"detail_status": "failed",
                  "detail_processed_at": now_str(),
                  "detail_error": error_msg},
         "$unset": {"detail_started_at": ""}},
    )


def upsert_detail(coll_detail, doc: dict) -> None:
    """覆盖或插入详情文档。"""
    coll_detail.replace_one({"_id": doc["_id"]}, doc, upsert=True)


def rescue_orphaned_processing(coll_list) -> int:
    """启动时恢复：卡在 processing 的孤儿 → pending。返回恢复数量。"""
    r = coll_list.update_many(
        {"detail_status": "processing"},
        {"$set": {"detail_status": "pending"},
         "$unset": {"detail_started_at": ""}},
    )
    return r.modified_count


class ParseError(Exception):
    """详情页结构无法识别时抛出。被 run_one 捕获并标 failed。"""


def _text(element) -> str:
    """提取并规范化 BeautifulSoup 元素文本。"""
    return element.get_text(" ", strip=True) if element else ""



def _as_int(value: str) -> int:
    """将页面中的非负整数字符串转换为 int。"""
    normalized = value.replace(",", "").strip()
    return int(normalized) if normalized.isdigit() else 0


def parse_detail(html: str, detail_url: str) -> dict:
    """将 1337x 详情页 HTML 解析为 bt_info_detail 文档。"""
    soup = BeautifulSoup(html, "html.parser")

    if not soup.select_one("div.torrent-detail, div.box-info-heading"):
        raise ParseError(f"详情页结构无法识别: {detail_url}")

    heading = soup.select_one("div.box-info-heading h1, h1")
    name = _text(heading)
    title_element = soup.select_one("div.torrent-detail-info h3 a, div.torrent-detail-info h3")
    title = (_text(title_element) or name).upper()

    meta = {}
    for row in soup.select("ul.list li"):
        key_element = row.select_one("strong")
        value_element = row.select_one("span")
        if key_element and value_element:
            key = _text(key_element).rstrip(":").strip().lower()
            meta[key] = _text(value_element)

    ref_now = datetime.now()
    date_uploaded = parse_relative_time(meta.get("date uploaded", ""), ref_now)
    last_checked = parse_relative_time(meta.get("last checked", ""), ref_now)

    genre = " ".join(
        filter(None, (_text(element) for element in soup.select("div.torrent-category span")))
    ).upper()
    tags = list(
        filter(None, (_text(link) for link in soup.select("ul.category-name li a")))
    )

    infohash_box = soup.select_one("div.infohash-box")
    match = re.search(
        r"INFOHASH\s*:\s*([A-Fa-f0-9]{32,40})",
        _text(infohash_box),
        re.IGNORECASE,
    )
    info_hash = match.group(1) if match else ""

    resource_links = {}
    magnet_link = soup.select_one("a[href^='magnet:']")
    if magnet_link and magnet_link.get("href"):
        resource_links["magnet"] = magnet_link["href"]

    for link_name, host in (
        ("itorrents", "itorrents.org"),
        ("torrage", "torrage.info"),
        ("btcache", "btcache.me"),
    ):
        link = soup.select_one(f"a[href*='{host}']")
        if link and link.get("href"):
            resource_links[link_name] = link["href"]

    stream_link = soup.select_one(
        "div.torrent-detail-info a[href*='play'], "
        "div.torrent-detail-info a[href*='stream']"
    )
    if stream_link and stream_link.get("href"):
        resource_links["stream"] = stream_link["href"]

    imdb_link = soup.select_one("a[href*='imdb.com/title/']")
    imdb_href = imdb_link.get("href") if imdb_link else None
    imdb_url = imdb_href if isinstance(imdb_href, str) else None

    cover = soup.select_one("div.torrent-image img, img.poster")
    cover_url = cover["src"] if cover and cover.get("src") else None

    description_element = soup.select_one("div.torrent-detail-info .content-row p, div#description")
    description = _text(description_element)

    rating = None
    rating_element = soup.select_one("div.torrent-rating")
    if rating_element:
        try:
            rating = int(float(_text(rating_element)))
        except ValueError:
            pass

    related_sites = []
    for link in soup.select("div#description a[href^='http']"):
        href = link.get("href", "")
        if "imdb.com" in href or "1337x.to" in href:
            continue
        link_name = _text(link)
        if link_name and href:
            related_sites.append({"name": link_name, "url": href})

    return {
        "_id": hashlib.md5(detail_url.encode()).hexdigest(),
        "detail_url": detail_url,
        "name": name,
        "category": meta.get("category", ""),
        "type": meta.get("type", ""),
        "language": meta.get("language", ""),
        "total_size": meta.get("total size", ""),
        "uploaded_by": meta.get("uploaded by", ""),
        "downloads": _as_int(meta.get("downloads", "")),
        "last_checked": last_checked,
        "date_uploaded": date_uploaded,
        "seeders": _as_int(meta.get("seeders", "")),
        "leechers": _as_int(meta.get("leechers", "")),
        "resource_links": resource_links,
        "cover_url": cover_url,
        "title": title,
        "genre": genre,
        "description": description,
        "rating": rating,
        "tags": tags,
        "info_hash": info_hash,
        "imdb_url": imdb_url,
        "imdb_id": extract_imdb_id(imdb_url),
        "related_sites": related_sites,
        "c_time": now_str(),
        "source": "1337x",
    }


# ============================================================
# 浏览器层（Task 6）
# ============================================================


def fetch_one(tab, url: str) -> str:
    """访问详情页并返回 HTML 字符串。Cloudflare 5秒盾由 fetch_with_cf_bypass 自动等待。

    Raises: TimeoutError (目标元素始终未出现) / 原始 DrissionPage 异常。
    """
    return fetch_with_cf_bypass(
        tab, url, "div.torrent-detail, div.box-info-heading", max_wait=45
    )


def save_html_cache(detail_url: str, html: str) -> None:
    """写本地 HTML 缓存。HTML_DIR 不存在则建。"""
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = html_cache_path(detail_url)
    path.write_text(html, encoding="utf-8")


# ============================================================
# 编排层（Task 7）
# ============================================================
import argparse
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_one(tab, doc: dict, coll_list, coll_detail, dry_run: bool = False) -> str:
    """处理单条 URL → 持久化一条记录。返回 'done' 或 'failed'。

    单 URL 顺序：
        claim（main loop 里完成）
        → fetch HTML（或读缓存）
        → parse_detail
        → upsert_detail（非 dry_run）
        → mark_done（非 dry_run）
        → time.sleep(INTER_REQUEST_SLEEP)
        → 下一条

    缓存命中：直接读 HTML，跳过 fetch + retry。
    缓存未命中：retry loop 内 fetch + cache save + parse + save。
    - ElementNotFoundError / PageDisconnectedError → 重试 MAX_RETRIES 次（间或用 RETRY_BACKOFF）
    - ParseError → 不重试，直接 failed
    - 其他异常 → 重试 MAX_RETRIES 次
    """
    doc_id = doc["_id"]
    url = doc["detail_url"]
    short_id = doc_id[:8]
    last_err = None

    name = doc.get("name", "")
    logger.info(f"[{short_id}] 开始处理：{name}")

    # 缓存命中分支：直接读 HTML，跳过 fetch + retry
    cache_path = html_cache_path(url)
    if cache_path.exists():
        logger.info(f"[{short_id}] 缓存命中，跳过下载：{cache_path.name}")
        try:
            html = cache_path.read_text(encoding="utf-8")
        except OSError as e:
            last_err = f"CacheReadError: {e}"
            logger.error(f"[{short_id}] 读取缓存失败：{e}")
            if not dry_run:
                mark_failed(coll_list, doc_id, last_err)
            return "failed"
        try:
            parsed = parse_detail(html, url)
        except ParseError as e:
            last_err = f"ParseError: {e}"
            logger.error(f"[{short_id}] 解析失败（缓存）：{e}")
            if not dry_run:
                mark_failed(coll_list, doc_id, last_err)
            return "failed"
        if not dry_run:
            upsert_detail(coll_detail, parsed)
            mark_done(coll_list, doc_id)
        logger.info(f"[{short_id}] 处理完成（缓存命中）→ done | title={parsed.get('title', '')!r}")
        _time.sleep(INTER_REQUEST_SLEEP)
        return "done"

    # 缓存未命中：retry loop
    logger.info(f"[{short_id}] 缓存未命中，开始下载：{url}")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[{short_id}] 第 {attempt}/{MAX_RETRIES} 次尝试：fetch_one")
            html = fetch_one(tab, url)
            logger.info(f"[{short_id}] 下载完成，HTML {len(html)} 字节，保存到缓存")
            save_html_cache(url, html)
            logger.info(f"[{short_id}] 开始解析详情页")
            parsed = parse_detail(html, url)
            logger.info(f"[{short_id}] 解析完成：title={parsed.get('title', '')!r}")
            if not dry_run:
                upsert_detail(coll_detail, parsed)
                logger.info(f"[{short_id}] 已写入 bt_info_detail")
                mark_done(coll_list, doc_id)
                logger.info(f"[{short_id}] 状态已更新 → done")
            else:
                logger.info(f"[{short_id}] dry-run 模式，跳过 DB 写入")
            _time.sleep(INTER_REQUEST_SLEEP)
            return "done"
        except (ElementNotFoundError, PageDisconnectedError) as e:
            last_err = f"DrissionError: {type(e).__name__}: {e}"
            logger.warning(f"[{short_id}] 第 {attempt}/{MAX_RETRIES} 次浏览器错误：{last_err}")
            if attempt < MAX_RETRIES:
                _time.sleep(RETRY_BACKOFF[attempt - 1])
        except ParseError as e:
            last_err = f"ParseError: {e}"
            logger.error(f"[{short_id}] 解析失败（不可重试）：{e}")
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[{short_id}] 第 {attempt}/{MAX_RETRIES} 次未知错误：{last_err}")
            if attempt < MAX_RETRIES:
                _time.sleep(RETRY_BACKOFF[attempt - 1])

    logger.error(f"[{short_id}] 重试耗尽，标记 failed：{last_err}")
    if not dry_run:
        mark_failed(coll_list, doc_id, last_err or "unknown")
    return "failed"


def run_batch(browser: ChromiumPage, docs: list, coll_list, coll_detail, concurrency: int, dry_run: bool) -> tuple[int, int]:
    """开 N 个 tab 并行跑一批 docs，返回 (done, failed)。

    每个 task 用 future.result(timeout=RUN_ONE_BUDGET) 包 run_one，强制 60s 总预算。
    超时 → mark_failed + 返回 "failed"（并跳过 DB 写）。

    浏览器死亡快检：批开始前先试一次 new_tab，失败则整批跳过，
    避免 100 条 doc 全部走完 100 次 new_tab 失败才意识到浏览器死了。
    """
    sem = Semaphore(concurrency)

    # 浏览器健康检查：试开一 tab 立即关掉
    try:
        health_tab = browser.new_tab()
        health_tab.close()
    except Exception as e:
        err = f"browser dead, batch aborted: {type(e).__name__}: {e}"
        logger.error(f"浏览器已不可用，整批 {len(docs)} 条跳过：{e}")
        if not dry_run:
            for doc in docs:
                mark_failed(coll_list, doc["_id"], err)
        return 0, len(docs)

    def one(doc):
        with sem:
            try:
                tab = browser.new_tab()
            except Exception as e:
                # 浏览器上下文已关闭 / new_tab 失败 —— 单条 doc 标 failed
                err = f"new_tab failed: {type(e).__name__}: {e}"
                logger.error(f"[{doc['_id'][:8]}] {err}（浏览器可能已关闭）")
                if not dry_run:
                    mark_failed(coll_list, doc["_id"], err)
                return "failed"
            try:
                with ThreadPoolExecutor(max_workers=1) as inner:
                    fut = inner.submit(run_one, tab, doc, coll_list, coll_detail, dry_run=dry_run)
                    return fut.result(timeout=RUN_ONE_BUDGET)
            except FutTimeout:
                if not dry_run:
                    mark_failed(coll_list, doc["_id"], f"run_one exceeded {RUN_ONE_BUDGET}s budget")
                return "failed"
            except Exception as e:
                # run_one 内部已有 try/except，但外层兜底（tab 状态异常等）
                err = f"{type(e).__name__}: {e}"
                logger.error(f"[{doc['_id'][:8]}] run_one 抛出未捕获异常：{err}")
                if not dry_run:
                    mark_failed(coll_list, doc["_id"], err)
                return "failed"
            finally:
                try:
                    tab.close()
                except Exception:
                    pass  # tab 可能已 dead，忽略关闭错误

    with ThreadPoolExecutor(max_workers=concurrency) as outer:
        results = list(outer.map(one, docs))
    done = sum(1 for r in results if r == "done")
    failed = sum(1 for r in results if r == "failed")
    return done, failed


def parse_args():
    p = argparse.ArgumentParser(description="1337x 详情页爬虫")
    p.add_argument("-c", "--concurrency", type=int, default=CONCURRENCY, help="并行 page 数")
    p.add_argument("-b", "--batch", type=int, default=BATCH, help="每批从 MongoDB 取多少条")
    p.add_argument("-p", "--pace", type=float, default=1.0, help="批次间停顿秒数")
    p.add_argument("-l", "--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    p.add_argument("-k", "--keyword", type=str, default=None, help="只处理指定 keyword")
    p.add_argument("--force", action="store_true", help="无视 status 强制重跑")
    p.add_argument("--retry-failed", action="store_true", help="重置 failed → pending 后再跑")
    p.add_argument("--dry-run", action="store_true", help="只解析不写")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    from pymongo import MongoClient
    client = MongoClient(MONGO_URI)
    coll_list = client[DB_NAME][COLL_LIST]
    coll_detail = client[DB_NAME][COLL_DETAIL]

    # --force: 重置全部为 pending
    if args.force:
        r = coll_list.update_many(
            {},
            {"$set": {"detail_status": "pending"},
             "$unset": {"detail_started_at": "",
                        "detail_processed_at": "",
                        "detail_error": ""}},
        )
        logger.info(f"--force: 重置 {r.modified_count} 条 → pending")

    # --retry-failed: 仅重置 failed → pending，保留 done
    if args.retry_failed:
        r = coll_list.update_many(
            {"detail_status": "failed"},
            {"$set": {"detail_status": "pending"},
             "$unset": {"detail_started_at": "",
                        "detail_processed_at": "",
                        "detail_error": ""}},
        )
        logger.info(f"--retry-failed: 重置 {r.modified_count} 条 failed → pending")

    # 启动恢复孤儿
    rescued = rescue_orphaned_processing(coll_list)
    if rescued:
        logger.info(f"恢复 {rescued} 条卡在 processing 的孤儿")

    # 构造 query
    query: dict = {"detail_status": "pending"}
    if args.keyword:
        query["keyword"] = args.keyword

    total_done = 0
    total_failed = 0
    total_processed = 0

    # DrissionPage 自启 headless Chrome（auto_port 强制独立进程）
    # 一个 ChromiumPage 实例 = 一个 Chrome 进程；并发靠多 tab + ThreadPoolExecutor
    # set_argument('--headless') 用老式 flag (不是 --headless=new),
    # 绕过 DrissionPage 4.1.1.4 .headless(True) 在 Windows 上 ws 连接失败的 bug,
    # 实现真 headless 无窗口运行。详见 crawl_1337x_by_key.py 顶部 docstring。
    options = (ChromiumOptions()
               # .set_argument("--headless")
               .auto_port(True))
    browser = ChromiumPage(options)
    logger.info(f"DrissionPage 已启动独立 headless Chrome (address={options.address})")

    try:
        batch_idx = 0
        while True:
            # 分批取
            cursor = coll_list.find(query).sort("_id").limit(args.batch)
            batch = []
            for doc in cursor:
                if args.limit and (total_processed + len(batch)) >= args.limit:
                    break
                if args.dry_run:
                    # Dry-run: 不抢占 status，保留 pending 计数供后续 verification
                    batch.append(doc)
                else:
                    claimed = claim_one(coll_list, doc["_id"])
                    if claimed:
                        batch.append(claimed)

            if not batch:
                logger.info("没有更多 pending 记录，退出")
                break

            batch_idx += 1
            t0 = time.time()
            logger.info(f"[batch {batch_idx}] 拿到 {len(batch)} 条，开始处理")
            done, failed = run_batch(browser, batch, coll_list, coll_detail,
                                     args.concurrency, args.dry_run)
            elapsed = time.time() - t0
            total_done += done
            total_failed += failed
            total_processed += len(batch)

            # 进度日志
            log_line = f"[batch {batch_idx}] done={done} failed={failed} elapsed={elapsed:.1f}s"
            logger.info(log_line)
            (HTML_DIR / "_progress.log").open("a", encoding="utf-8").write(
                log_line + "\n"
            )

            if args.limit and total_processed >= args.limit:
                logger.info(f"达到 --limit={args.limit}，停止")
                break

            time.sleep(args.pace)
    finally:
        # 关闭整个 Chrome（DrissionPage 拥有自己的 Chrome，必须 quit）
        try:
            browser.quit()
            logger.info("DrissionPage Chrome 已关闭")
        except Exception as e:
            logger.warning(f"关闭 Chrome 时异常: {type(e).__name__}: {e}")

    logger.info(
        f"完成。done={total_done} failed={total_failed} "
        f"total={total_processed}"
    )


if __name__ == "__main__":
    main()
