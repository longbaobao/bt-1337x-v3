"""
1337x 详情页爬虫：从 bt_info_list 取 detail_url，抓 HTML 落本地，解析入库。

连接本地 9222 调试端口的 Chrome（不新开进程），复用现有 context。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import re
import calendar
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

CDP_URL = "http://127.0.0.1:9222"
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "bt_13337x_spider_db"
COLL_LIST = "bt_info_list"
COLL_DETAIL = "bt_info_detail"
HTML_DIR = Path("data/html")

BATCH = 200
MAX_RETRIES = 3
RETRY_BACKOFF = (2, 4, 8)  # 秒
RUN_ONE_BUDGET = 60  # 秒


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


async def fetch_one(page, url: str) -> str:
    """访问详情页并返回 HTML 字符串。超时抛 PWTimeout。"""
    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
    await page.wait_for_selector(
        "div.torrent-detail, div.box-info-heading", timeout=30000
    )
    return await page.content()


def save_html_cache(detail_url: str, html: str) -> None:
    """写本地 HTML 缓存。HTML_DIR 不存在则建。"""
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = html_cache_path(detail_url)
    path.write_text(html, encoding="utf-8")
