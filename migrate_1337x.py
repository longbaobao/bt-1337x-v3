"""一次性迁移：把 ResToDoItem1337x 中 cTime 改名为 c_time，并把旧 list_time 文本重新解析为 yyyy-mm-dd hh:mm:ss。

幂等：重复执行不报错。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import re
from datetime import datetime
from pymongo import MongoClient

sys.path.insert(0, ".")
from crawl_1337x import parse_1337x_time, DB_NAME, COLL_NAME, MONGO_URI


def main():
    # 兼容旧命名：pan_spider_db.ResToDoItem1337x
    src_client = MongoClient(MONGO_URI)
    src = src_client["pan_spider_db"]["ResToDoItem1337x"]
    dst = src_client[DB_NAME][COLL_NAME]

    copied = 0
    for d in src.find({}):
        dst.replace_one({"_id": d["_id"]}, d, upsert=True)
        copied += 1
    print(f"迁移 {copied} 条: pan_spider_db.ResToDoItem1337x → {DB_NAME}.{COLL_NAME}")

    coll = dst

    # 1) cTime → c_time
    renamed = 0
    for d in coll.find({"cTime": {"$exists": True}}, {"_id": 1, "cTime": 1}):
        coll.update_one({"_id": d["_id"]}, {"$set": {"c_time": d["cTime"]}, "$unset": {"cTime": ""}})
        renamed += 1
    print(f"cTime → c_time: {renamed} 条")

    # 2) list_time 文本 → yyyy-mm-dd hh:mm:ss
    # 已格式化的（yyyy-mm-dd hh:mm:ss）跳过；空值也跳过
    re_formatted = 0
    for d in coll.find(
        {"list_time": {"$exists": True, "$nin": ["", None], "$not": re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")}},
        {"_id": 1, "list_time": 1},
    ):
        new_val = parse_1337x_time(d["list_time"])
        if new_val and new_val != d["list_time"]:
            coll.update_one({"_id": d["_id"]}, {"$set": {"list_time": new_val}})
            re_formatted += 1
    print(f"list_time 重新格式化: {re_formatted} 条")

    # 3) 统计
    total = coll.count_documents({})
    empty_up = coll.count_documents({"uploader": ""})
    print(f"总计 {total} 条；uploader 为空 {empty_up} 条（将由 crawl_1337x.py 重跑补齐）")


if __name__ == "__main__":
    main()