"""crawl_1337x_by_key.py cookie 预热助手测试。

针对 CF Turnstile 复选框场景的兜底方案:
用户在自己 Chrome 里手动过一次 CF,导出 cf_clearance cookie 存到
data/cf_cookies.json,脚本启动时注入到 headless Chrome,CF 不再弹验证。

直接跑:python test_cf_cookies.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import tempfile
from pathlib import Path

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. 文件不存在 → 返回空列表(不抛异常,这是预期行为)
    ck.CF_COOKIES_FILE = Path("/nonexistent/path/cf_cookies.json")
    check(ck.load_cf_cookies() == [], "文件不存在 → 返回 []")

    # 2. 文件存在 + 合法 JSON 数组 → 原样返回
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        valid = [
            {"name": "cf_clearance", "value": "abc123", "domain": ".1337x.to", "path": "/", "expiry": 9999999999},
            {"name": "__cf_bm", "value": "xyz789", "domain": ".1337x.to", "path": "/", "expiry": 9999999999},
        ]
        json.dump(valid, f)
        tmp = Path(f.name)
    ck.CF_COOKIES_FILE = tmp
    loaded = ck.load_cf_cookies()
    check(len(loaded) == 2, f"合法文件 → 2 条 cookie (实际 {len(loaded)})")
    check(loaded[0]["name"] == "cf_clearance", "第一条 cookie 名字正确")
    check(loaded[0]["domain"] == ".1337x.to", "第一条 cookie domain 正确")

    # 3. 缺 name 字段 → 跳过该条,不算整体失败
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        bad = [
            {"value": "no_name", "domain": ".1337x.to"},  # 缺 name
            {"name": "ok", "value": "v1", "domain": ".1337x.to"},
        ]
        json.dump(bad, f)
        tmp2 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp2
    loaded = ck.load_cf_cookies()
    check(len(loaded) == 1, f"缺 name 字段被跳过 → 剩 1 条 (实际 {len(loaded)})")
    check(loaded[0]["name"] == "ok", "剩的那条是合法的")

    # 4. 缺 value 字段 → 跳过
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        bad = [{"name": "novalue", "domain": ".1337x.to"}]
        json.dump(bad, f)
        tmp3 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp3
    check(ck.load_cf_cookies() == [], "缺 value → 跳过 → 空")

    # 5. JSON 损坏 → 返回空(不抛)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write("{not json{[")
        tmp4 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp4
    check(ck.load_cf_cookies() == [], "JSON 损坏 → 返回 []")

    # 6. 不是 list(比如 dict)→ 返回空
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"name": "cf_clearance", "value": "x"}, f)  # dict 而非 list
        tmp5 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp5
    check(ck.load_cf_cookies() == [], "顶层不是 list → 返回 []")

    # 7. CF_COOKIES_FILE 常量存在
    check(hasattr(ck, "CF_COOKIES_FILE"), "CF_COOKIES_FILE 常量已定义")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())