"""crawl_detail_1337x.py 集成最新 CF 处理 的烟雾测试。

验证:
1. 模块 import 不崩
2. 关键符号从 crawl_1337x_by_key 正确导入并暴露给详情爬虫
3. fetch_one / run_one / main 调用链静态扫源码确认:
   - browser 创建后调 add_init_js(STEALTH_INIT_JS)
   - browser 创建后调 inject_cf_cookies(browser)
   - 每个 new_tab() 后调 tab.add_init_js(STEALTH_INIT_JS)
   - fetch_one 调 maybe_refresh_cf_cookies(tab)

直接跑:python test_detail_imports.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import inspect
import re

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. 模块 import 成功
    try:
        import crawl_detail_1337x as d
    except Exception as e:
        check(False, f"import 失败: {type(e).__name__}: {e}")
        return 1
    check(True, "crawl_detail_1337x 模块 import 成功")

    # 2. 关键符号导入并暴露
    for sym in ("inject_cf_cookies", "STEALTH_INIT_JS", "maybe_refresh_cf_cookies"):
        check(hasattr(d, sym), f"{sym} 已从 crawl_1337x_by_key 导入")
    check(hasattr(d, "fetch_with_cf_bypass"), "fetch_with_cf_bypass 已导入(验证 Turnstile 自动点等新逻辑继承)")

    # 3. 静态扫源码确认调用点
    src = inspect.getsource(d)

    # browser 创建后调 inject_cf_cookies
    check("inject_cf_cookies(browser)" in src,
          "main() 里 browser 创建后调 inject_cf_cookies")

    # browser 创建后调 add_init_js(STEALTH_INIT_JS)
    check("browser.add_init_js(STEALTH_INIT_JS)" in src,
          "main() 里 browser 创建后调 add_init_js(STEALTH_INIT_JS)")

    # fetch_one 调 maybe_refresh_cf_cookies
    check("maybe_refresh_cf_cookies(tab)" in src,
          "fetch_one 调 maybe_refresh_cf_cookies(tab)")

    # 每个 new_tab() 后有 add_init_js 兜底(防 CDP per-target 失效)
    n_newtab = src.count("browser.new_tab()")
    n_init = src.count("add_init_js(STEALTH_INIT_JS)")
    check(n_newtab > 0, f"有 {n_newtab} 处 browser.new_tab() 调用")
    check(n_init >= n_newtab,
          f"add_init_js 调用({n_init}) >= new_tab 调用({n_newtab})")

    # 4. fetch_one 签名/逻辑检查
    fetch_one_src = inspect.getsource(d.fetch_one)
    check("fetch_with_cf_bypass" in fetch_one_src,
          "fetch_one 调 fetch_with_cf_bypass")
    check("maybe_refresh_cf_cookies" in fetch_one_src,
          "fetch_one 调 maybe_refresh_cf_cookies(自反馈 cookie)")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())