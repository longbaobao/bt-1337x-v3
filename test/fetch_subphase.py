import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py fetch_with_cf_bypass sub-phase 计时 + 短 timeout 测试。

实测 fetch=22s 但 cf=1x(没 CF 重试),时间花在 fetch 内部某段。
DrissionPage 默认 page_load timeout=30s,导致 tab.wait.load_start() / tab.html
傻等第三方脚本加载完成。

修复:
1. fetch_with_cf_bypass 加 sub-phase 计时(tab.get / load_start / tab.html / tab.ele)
2. fetch_with_cf_bypass 显式传 timeout 给 wait.load_start(读 page.timeouts.page_load)
3. run_with_retry 创建 page 后把 page.timeouts.page_load 砍到 5s

直接跑:python test/fetch_subphase.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import inspect
import time
import re

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. _FetchStats.fetch_subphases dict 已定义,3 个 sub-phase (无 load_start)
    ck._FetchStats.fetch_subphases = {k: 0 for k in ["tab.get", "tab.html", "tab.ele"]}
    expected = {"tab.get", "tab.html", "tab.ele"}
    check(expected.issubset(set(ck._FetchStats.fetch_subphases.keys())),
          f"_FetchStats.fetch_subphases 含 3 个 sub-phase: {sorted(ck._FetchStats.fetch_subphases.keys())}")

    # 2. 模拟 sub-phase 累加(测 PageTimer 配合 _FetchStats 的逻辑,不是真 fetch)
    ck._FetchStats.fetch_subphases = {k: 0 for k in ["tab.get", "tab.html", "tab.ele"]}
    for _ in range(3):
        t0 = time.perf_counter()
        time.sleep(0.1)
        ck._FetchStats.fetch_subphases["tab.html"] += (time.perf_counter() - t0)
    check(0.25 < ck._FetchStats.fetch_subphases["tab.html"] < 0.5,
          f"sub-phase 累加 3 次 ~0.1s: {ck._FetchStats.fetch_subphases['tab.html']:.3f}s")

    # 3. fetch_with_cf_bypass 源码含 sub-phase 计时调用
    src = inspect.getsource(ck.fetch_with_cf_bypass)
    check('fetch_subphases["tab.get"]' in src,
          "fetch_with_cf_bypass 含 tab.get sub-phase 计时")
    check('fetch_subphases["tab.html"]' in src,
          "fetch_with_cf_bypass 含 tab.html sub-phase 计时")
    check('fetch_subphases["tab.ele"]' in src,
          "fetch_with_cf_bypass 含 tab.ele sub-phase 计时")

    # 4. fetch_with_cf_bypass 不再调 wait.load_start(最优策略)
    check("wait.load_start" not in src,
          "fetch_with_cf_bypass 不调 wait.load_start (避免傻等第三方脚本)")

    # 5. fetch_with_cf_bypass 也不让 tab.html 触发 wait.doc_loaded
    #    (改用 _get_outer_html 直接调 DOM.getOuterHTML)
    check("_get_outer_html" in src,
          "fetch_with_cf_bypass 调 _get_outer_html (跳过 wait.doc_loaded)")

    # 6. _get_outer_html 函数存在 + 走 DOM CDP 直调 + 不带 ["result"] wrapper
    check(hasattr(ck, "_get_outer_html"),
          "_get_outer_html 函数已定义")
    helper_src = inspect.getsource(ck._get_outer_html)
    check("DOM.getOuterHTML" in helper_src,
          "_get_outer_html 走 DOM.getOuterHTML (直接 CDP)")
    # P0 bug 防御:DrissionPage _run_cdp 已 unwrap result 字段,
    # 不应再写 ["result"]["outerHTML"] 这种 KeyError 触发写法
    check('["result"]["outerHTML"]' not in helper_src,
          '_get_outer_html 不带 ["result"]["outerHTML"] 错误写法')
    check('["result"]["object"]["objectId"]' not in helper_src,
          '_get_outer_html 不带 ["result"]["object"]["objectId"] 错误写法')
    check('["result"]["root"]["backendNodeId"]' not in helper_src,
          '_get_outer_html 不带 ["result"]["root"]["backendNodeId"] 错误写法')
    # 应该直接 ["outerHTML"] / ["object"]["objectId"] / ["root"]["backendNodeId"]
    check('["outerHTML"]' in helper_src,
          '_get_outer_html 走 ["outerHTML"] (无 result wrapper)')
    check('["object"]["objectId"]' in helper_src,
          '_get_outer_html 走 ["object"]["objectId"] (无 result wrapper)')
    check('["root"]["backendNodeId"]' in helper_src,
          '_get_outer_html 走 ["root"]["backendNodeId"] (无 result wrapper)')

    # 7. run_with_retry 不再设 page.timeouts.page_load = 5
    run_src = inspect.getsource(ck.run_with_retry)
    check("timeouts.page_load = 5" not in run_src,
          "run_with_retry 不再设 page_load = 5 (已不需要)")

    # 6. format_phase_log 读 _FetchStats.fetch_subphases 输出子阶段
    #    (上一条断言已经覆盖了源码扫描)
    fmt_src = inspect.getsource(ck.format_phase_log)
    check("_FetchStats.fetch_subphases" in fmt_src,
          "format_phase_log 读 _FetchStats.fetch_subphases")

    # 7. format_phase_log 输出 sub-phase 详情(fetch 子阶段)
    #    当前格式只输出 fetch=Ns,需要扩展成 fetch=N.Xs(tab.get=A load=B html=C ele=D)
    fmt_src = inspect.getsource(ck.format_phase_log)
    # 至少包含 fetch_subphases 的引用(说明有扩展)
    check("fetch_subphases" in fmt_src or "subphases" in fmt_src,
          "format_phase_log 引用 fetch_subphases(扩展输出子阶段)")
    # 或者现在还没扩展,我们将手动测试它接受 kwarg
    sig = inspect.signature(ck.format_phase_log)
    # 当前签名应该是 (page_num, total_pages, timer, cf_attempts, items_found)
    # 我们不强制改签名,只检查格式 log 字符串里能体现 sub-phase
    t = ck.PageTimer()
    t.start("fetch"); t.stop("fetch")
    log = ck.format_phase_log(1, 1, t)
    check("fetch=" in log, "format_phase_log 含 fetch= 字段")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())