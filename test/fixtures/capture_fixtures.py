"""从 bt_info_list 选取多个有差异的详情页，访问后保存到 tests/fixtures/。

这是一次性采集脚本。DrissionPage 自启 headless Chrome(独立 user-data-dir)，
不依赖外部 9222 实例。采集后可根据页面中 IMDB、INFOHASH 和 magnet 字段
的实际情况手工挑选 canonical fixtures。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本既可从项目根目录运行，也可直接从 tests/fixtures/ 运行。
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from DrissionPage import ChromiumPage, ChromiumOptions
from pymongo import MongoClient

from crawl_detail_1337x import (
    MONGO_URI,
    DB_NAME,
    COLL_LIST,
    html_cache_path,
)

FIX_DIR = Path(__file__).parent
NIGHT_HOUSE_PATTERN = "Night.House"
DOC_PROJECTION = {"_id": 1, "name": 1, "detail_url": 1}


def _select_targets(coll):
    """返回 Night House（如存在）及若干不同样本。"""
    targets = []
    seen_urls = set()

    night_house = coll.find_one(
        {"name": {"$regex": NIGHT_HOUSE_PATTERN, "$options": "i"}},
        DOC_PROJECTION,
    )
    if night_house:
        targets.append(("detail_night_house.html", night_house))
        seen_urls.add(night_house["detail_url"])
    else:
        print("WARN: 找不到 Night.House，将从库内样本中选择完整 fixture")

    # 多抓一些候选，便于之后人工按字段完整性挑选 canonical fixtures。
    for doc in coll.find(
        {
            "detail_url": {"$exists": True},
            "name": {"$not": {"$regex": NIGHT_HOUSE_PATTERN, "$options": "i"}},
        },
        DOC_PROJECTION,
    ).limit(12):
        url = doc.get("detail_url")
        if not url or url in seen_urls:
            continue
        targets.append((f"detail_sample_{str(doc['_id'])[:8]}.html", doc))
        seen_urls.add(url)

    return targets


def main() -> None:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    coll = MongoClient(MONGO_URI)[DB_NAME][COLL_LIST]
    targets = _select_targets(coll)
    if not targets:
        raise RuntimeError(f"{DB_NAME}.{COLL_LIST} 中没有可用 detail_url")

    # DrissionPage 自启 headless Chrome,与 crawl_1337x_by_key.py 共享同一模式。
    # auto_port(True) 强制自启独立 Chrome(不 attach 用户 9222)。
    options = ChromiumOptions().auto_port(True)
    page = ChromiumPage(options)
    try:
        for filename, doc in targets:
            url = doc["detail_url"]
            output = FIX_DIR / filename
            print(f"抓取 {url} -> {filename}")
            print(f"  cache key: {html_cache_path(url)}")
            try:
                page.get(url)
                page.wait.load_start()
                page.ele(
                    "div.torrent-detail, div.box-info-heading, div.box-info",
                    timeout=30,
                )
                html = page.html
            except Exception as exc:
                print(f"  WARN: 页面加载失败，跳过: {exc}")
                continue
            output.write_text(html, encoding="utf-8")
            print(f"  保存 {len(html)} 字节 ({doc.get('name', '')})")
    finally:
        page.quit()


if __name__ == "__main__":
    main()
