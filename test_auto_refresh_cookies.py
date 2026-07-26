"""crawl_1337x_by_key.py CF cookie 自动刷新测试。

行为:
- fetch_with_cf_bypass 通过 CF 后,maybe_refresh_cf_cookies(page) 把
  当前 .1337x.to 的 cookie 写回 data/cf_cookies.json(原子写)。
- 没拿到 cf_clearance → 不写(避免空覆盖)。
- 写入内容与现有文件完全相同 → 跳过(no-op)。
- 没变化的 cookie 不重复写盘。

直接跑:python test_auto_refresh_cookies.py
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


# 模拟 page.cookies(all_info=True) 的返回值(含跨域 cookie + 过期字段)
def fake_cookies_page(cookies):
    """返回一个能被 maybe_refresh_cf_cookies 当 page 用的对象,只暴露 .cookies() 方法。"""
    class _FakePage:
        def cookies(self, all_domains=False, all_info=False):
            return cookies
    return _FakePage()


SAMPLE_COOKIES_PASSED = [
    # CF 通过后会拿到这俩(关键)
    {"name": "cf_clearance", "value": "fresh_token_abc", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0, "httpOnly": True, "secure": True},
    {"name": "__cf_bm", "value": "bm_token_xyz", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0, "httpOnly": True, "secure": True},
    # 别的域的 cookie 应被过滤掉
    {"name": "_ga", "value": "GA1.2.xxx", "domain": ".google.com",
     "path": "/", "expires": 1735689600.0},
]

SAMPLE_COOKIES_NO_CF = [
    # 没 cf_clearance → 视为没真正过 CF,不写
    {"name": "_ga", "value": "GA1.2.xxx", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0},
]


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "cf_cookies.json"
        ck.CF_COOKIES_FILE = tmp

        # 1. 通过 CF + 文件原本不存在 → 写入
        ck.maybe_refresh_cf_cookies(fake_cookies_page(SAMPLE_COOKIES_PASSED))
        check(tmp.exists(), "通过 CF → 文件已创建")
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(len(loaded) == 2, f"只写 1337x.to 的 cookie(2 条)实际 {len(loaded)}")
        names = {c["name"] for c in loaded}
        check(names == {"cf_clearance", "__cf_bm"}, f"含 cf_clearance + __cf_bm: {names}")
        # 字段清洗:不应包含 httpOnly / secure 等非标准字段
        sample = loaded[0]
        check(set(sample.keys()) <= {"name", "value", "domain", "path", "expires"},
              f"字段清洗:实际 keys={sorted(sample.keys())}")

        # 2. 没拿到 cf_clearance → 不写(避免空覆盖)
        tmp.write_text(json.dumps([{"name": "cf_clearance", "value": "OLD_VALUE",
                                     "domain": ".1337x.to", "path": "/",
                                     "expires": 1700000000.0}]),
                        encoding="utf-8")
        old_mtime = tmp.stat().st_mtime_ns
        import time as _t
        _t.sleep(0.05)  # 确保 mtime 差异可观察
        ck.maybe_refresh_cf_cookies(fake_cookies_page(SAMPLE_COOKIES_NO_CF))
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(loaded[0]["value"] == "OLD_VALUE",
              "无 cf_clearance → 文件未被覆盖(保留 OLD_VALUE)")

        # 3. 内容相同 → no-op (mtime 不变)
        tmp.write_text(json.dumps([
            {"name": "cf_clearance", "value": "same_token",
             "domain": ".1337x.to", "path": "/", "expires": 1735689600.0},
            {"name": "__cf_bm", "value": "same_bm",
             "domain": ".1337x.to", "path": "/", "expires": 1735689600.0},
        ]), encoding="utf-8")
        old_mtime = tmp.stat().st_mtime_ns
        _t.sleep(0.05)
        same_cookies = [
            {"name": "cf_clearance", "value": "same_token", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0, "httpOnly": True},
            {"name": "__cf_bm", "value": "same_bm", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0, "httpOnly": True},
        ]
        result = ck.maybe_refresh_cf_cookies(fake_cookies_page(same_cookies))
        check(result is False, "内容相同 → 返回 False (no-op)")
        check(tmp.stat().st_mtime_ns == old_mtime, "内容相同 → mtime 未变")

        # 4. 内容不同 → 写
        new_cookies = [
            {"name": "cf_clearance", "value": "NEW_TOKEN", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0},
            {"name": "__cf_bm", "value": "new_bm", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0},
        ]
        result = ck.maybe_refresh_cf_cookies(fake_cookies_page(new_cookies))
        check(result is True, "新内容 → 返回 True")
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(loaded[0]["value"] == "NEW_TOKEN", "新内容已写入")

        # 5. maybe_refresh_cf_cookies 函数存在
        check(hasattr(ck, "maybe_refresh_cf_cookies"),
              "maybe_refresh_cf_cookies 已定义")

        # 6. page.cookies 失败 → 静默,不抛
        class _BrokenPage:
            def cookies(self, **kw):
                raise RuntimeError("CDP 断线")
        try:
            result = ck.maybe_refresh_cf_cookies(_BrokenPage())
            check(True, "page.cookies 抛异常 → 静默返回")
        except Exception as e:
            check(False, f"应静默却抛了: {e}")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())