"""
ETL:从 bt_13337x_spider_db.bt_info_list 提取所有 name 字段,
英文分词 + 去重,保存到 key.txt(用作后续 batch 抓取的 keyword 候选)。

跑法:.venv/Scripts/python.exe etl/extract_keys.py

分词规则:
    分隔符     [._\-\s()[\]{}/\\|:,'"]+
    过滤       len < 3 / 纯数字 / 停用词 / S01E10 / 1080x720 这类
    归一化     strip + lower
    去重       set
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import logging
import re
from pathlib import Path

from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "bt_13337x_spider_db"
COLL_NAME = "bt_info_list"
OUT_FILE = Path("../data/keys.txt")

# 分词规则
SEPARATORS = re.compile(
    r"[._\-\s()[\]{}/\\|:,\'\"<>=!?*&^%$#@~`"
    r"‘’“”"
    r"、。《》「」『』【】"
    r"]+",
    re.UNICODE,
)
STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that",
    "of", "in", "an", "to", "is", "it", "its", "by", "on", "at",
    "as", "be", "or", "not", "but", "all",
}
MIN_LEN = 3
# 字符白名单:Latin + 数字 + 日韩 + CJK
# 允许:house / webrip / 1080p / x264 / mp3 / 神宮寺
# 拒绝:#1s / ⭐no / 100% / +++ / 【vamp / 12"
ALLOWED_CHARS_RE = re.compile(
    r"^[a-z0-9"
    r"぀-ゟ"      # 平假名
    r"゠-ヿ"      # 片假名
    r"一-鿿"      # CJK 统一汉字
    r"가-힯"      # 韩文
    r"]+$",
    re.UNICODE,
)
SCAN_LOG_EVERY = 500


def is_valid_keyword(tl: str) -> bool:
    """后处理:判断 lowercase token 是否合法 keyword。

    拒绝:
        - 长度 < MIN_LEN
        - 在停用词表
        - 字符不在白名单(含标点 / emoji / 特殊符号)
    """
    if len(tl) < MIN_LEN:
        return False
    if tl in STOPWORDS:
        return False
    if not ALLOWED_CHARS_RE.match(tl):
        return False
    return True


def tokenize_name(name: str) -> set[str]:
    """对单条 name 做分词:split → lowercase → 后处理过滤 → 去重。"""
    raw = SEPARATORS.split(name)
    out: set[str] = set()
    for t in raw:
        if not t:
            continue
        tl = t.strip().lower()
        if not is_valid_keyword(tl):
            continue
        out.add(tl)
    return out


def main():
    logger.info(f"=== ETL 启动:从 {DB_NAME}.{COLL_NAME} 提取 name token ===")
    coll = MongoClient(MONGO_URI)[DB_NAME][COLL_NAME]
    total = coll.count_documents({})
    logger.info(f"MongoDB 连接成功,共 {total} 条记录待扫描")

    all_tokens: set[str] = set()
    scanned = 0
    empty_names = 0
    for doc in coll.find({}, {"name": 1, "_id": 0}):
        scanned += 1
        if scanned % SCAN_LOG_EVERY == 0:
            logger.info(f"扫描进度 {scanned}/{total},当前唯一 token 数 {len(all_tokens)}")
        name = doc.get("name") or ""
        if not name:
            empty_names += 1
            continue
        all_tokens |= tokenize_name(name)

    logger.info(
        f"扫描完毕:共处理 {scanned} 条(空 name {empty_names} 条),"
        f"提取 {len(all_tokens)} 个唯一 token"
    )

    sorted_tokens = sorted(all_tokens)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(sorted_tokens) + "\n", encoding="utf-8")
    logger.info(f"已写入 {OUT_FILE}({OUT_FILE.stat().st_size} bytes,{len(sorted_tokens)} 行)")

    logger.info("前 20 个 token(预览):")
    for t in sorted_tokens[:20]:
        logger.info(f"  {t}")
    logger.info("后 5 个 token:")
    for t in sorted_tokens[-5:]:
        logger.info(f"  {t}")


if __name__ == "__main__":
    main()