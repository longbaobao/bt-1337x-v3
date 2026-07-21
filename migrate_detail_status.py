"""一次性迁移：bt_info_list 添加 detail_status 字段；c_time datetime → 字符串；
bt_info_detail 建唯一索引。幂等。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from pymongo import MongoClient

sys.path.insert(0, ".")
try:
    from crawl_1337x import DB_NAME, MONGO_URI
except ModuleNotFoundError as exc:
    if exc.name != "crawl_1337x":
        raise
    # 当前单关键词爬虫已重命名为 crawl_1337x_by_key.py。
    from crawl_1337x_by_key import DB_NAME, MONGO_URI

COLL_LIST = "bt_info_list"
COLL_DETAIL = "bt_info_detail"


def main():
    client = MongoClient(MONGO_URI)
    coll_list = client[DB_NAME][COLL_LIST]
    coll_detail = client[DB_NAME][COLL_DETAIL]

    # 1) 添加 detail_status 字段（已存在的不动）
    r1 = coll_list.update_many(
        {"detail_status": {"$exists": False}},
        {
            "$set": {"detail_status": "pending"},
            "$unset": {
                "detail_started_at": "",
                "detail_processed_at": "",
                "detail_error": "",
            },
        },
    )
    print(f"[1] detail_status 字段补充: {r1.modified_count} 条")

    # 2) c_time 字段 datetime → 字符串
    r2 = coll_list.update_many(
        {"c_time": {"$type": "date"}},
        [
            {
                "$set": {
                    "c_time": {
                        "$dateToString": {
                            "format": "%Y-%m-%d %H:%M:%S",
                            "date": "$c_time",
                        }
                    }
                }
            }
        ],
    )
    print(f"[2] c_time 转字符串: {r2.modified_count} 条")

    # 3) bt_info_detail 建唯一索引
    coll_detail.create_index("detail_url", unique=True)
    print("[3] bt_info_detail.detail_url 唯一索引已建")

    # 验证
    total = coll_list.count_documents({})
    pending = coll_list.count_documents({"detail_status": "pending"})
    sample = coll_list.find_one({}, {"detail_status": 1, "c_time": 1, "_id": 0})
    print(f"\n总: {total}, pending: {pending}")
    print(f"样本: {sample}")


if __name__ == "__main__":
    main()
