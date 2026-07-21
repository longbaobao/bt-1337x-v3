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