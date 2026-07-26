import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""3 个模块的结构 + 签名测试(防止重构错位)。

覆盖:
- crawl_1337x_by_key.main / run_with_retry 签名 + Chrome 归属
- crawl_1337x_by_keys 常量 + 不应再含 MAX_ATTEMPTS/RETRY_BACKOFF(已移走)
- crawl_detail_1337x 关键符号从 key 脚本导入 + 调用链静态扫

直接跑:python test/modules.py
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


# ─── crawl_1337x_by_key.py 签名 + Chrome 归属 ───────────────────────────

def test_key_signature():
    print("[key_signature]")
    import crawl_1337x_by_key as ck

    sig = inspect.signature(ck.main)
    params = list(sig.parameters.keys())
    check(params == ["keyword", "page", "coll", "started_at"],
          f"main() 4 参签名: {params}")

    sig = inspect.signature(ck.run_with_retry)
    params = list(sig.parameters.keys())
    check(params == ["keyword"], f"run_with_retry() 1 参签名: {params}")

    check(hasattr(ck, "MAX_ATTEMPTS") and ck.MAX_ATTEMPTS == 4,
          f"MAX_ATTEMPTS={ck.MAX_ATTEMPTS} (=4)")
    check(hasattr(ck, "RETRY_BACKOFF") and ck.RETRY_BACKOFF == 5,
          f"RETRY_BACKOFF={ck.RETRY_BACKOFF} (=5)")

    # main() 不再自己创建 Chrome
    src = inspect.getsource(ck.main)
    check("auto_port" not in src, "main() 不调用 auto_port (Chrome 归 run_with_retry 管)")
    check("ChromiumPage(" not in src, "main() 不创建 ChromiumPage")
    check("page.quit()" not in src, "main() 不再调 page.quit()")

    # run_with_retry() 创建并管理 Chrome —— 只创建 1 次,只 quit 1 次
    src = inspect.getsource(ck.run_with_retry)
    check("auto_port" in src, "run_with_retry() 调 auto_port")
    n_create = src.count("ChromiumPage(")
    check(n_create == 1, f"run_with_retry() 只创建 1 次 ChromiumPage (实际 {n_create})")
    n_quit = src.count("page.quit()")
    check(n_quit == 1, f"page.quit() 只调 1 次 (实际 {n_quit})")
    loop_body_match = re.search(r"for attempt in.*?time\.sleep\(RETRY_BACKOFF\)", src, re.DOTALL)
    if loop_body_match:
        loop_body = loop_body_match.group(0)
        check("page.quit()" not in loop_body, "retry 循环体内不含 page.quit()")
    else:
        check(False, "未匹配到 retry 循环体,请人工检查")

    # main() 用 None 当 page 优雅降级
    rc = ck.main("nosuchkey", None, None, 0.0)
    check(rc == 2, f"main() page=None → rc=2 优雅返回(实际 rc={rc})")


# ─── crawl_1337x_by_keys.py 常量 + 不应再含迁移走的常量 ───────────────

def test_wrapper_imports():
    print("[wrapper_imports]")
    import crawl_1337x_by_keys as w

    check(True, "wrapper 模块 import 成功")
    check(hasattr(w, "WORKER_TIMEOUT") and w.WORKER_TIMEOUT == 600,
          f"WORKER_TIMEOUT = {w.WORKER_TIMEOUT} (=600)")
    check(hasattr(w, "KEYS_FILE"), "KEYS_FILE 已定义")
    check(hasattr(w, "DONE_FILE"), "DONE_FILE 已定义")

    # 移走的常量不应再出现
    check(not hasattr(w, "MAX_ATTEMPTS"),
          "wrapper 不再有 MAX_ATTEMPTS (移到 key 脚本)")
    check(not hasattr(w, "RETRY_BACKOFF"),
          "wrapper 不再有 RETRY_BACKOFF (移到 key 脚本)")

    # 静态扫源码:docstring 之外不能引用这俩
    src = inspect.getsource(w)
    code_only = re.sub(r'"""[\s\S]*?"""', '', src)
    for name in ("MAX_ATTEMPTS", "RETRY_BACKOFF"):
        if re.search(r'\b' + name + r'\b', code_only):
            check(False, f"wrapper 代码里仍有 {name} 引用(f-string 会 NameError)")
        else:
            check(True, f"wrapper 代码里无 {name} 引用")

    # 反向:key 脚本应该有
    import crawl_1337x_by_key as k
    check(hasattr(k, "MAX_ATTEMPTS") and k.MAX_ATTEMPTS == 4,
          f"key 脚本 MAX_ATTEMPTS = {k.MAX_ATTEMPTS} (=4)")
    check(hasattr(k, "RETRY_BACKOFF") and k.RETRY_BACKOFF == 5,
          f"key 脚本 RETRY_BACKOFF = {k.RETRY_BACKOFF} (=5)")


# ─── crawl_detail_1337x.py 集成 CF 处理 的烟雾测试 ──────────────────────

def test_detail_imports():
    print("[detail_imports]")
    try:
        import crawl_detail_1337x as d
    except Exception as e:
        check(False, f"import 失败: {type(e).__name__}: {e}")
        return
    check(True, "crawl_detail_1337x 模块 import 成功")

    for sym in ("inject_cf_cookies", "STEALTH_INIT_JS", "maybe_refresh_cf_cookies"):
        check(hasattr(d, sym), f"{sym} 已从 crawl_1337x_by_key 导入")
    check(hasattr(d, "fetch_with_cf_bypass"),
          "fetch_with_cf_bypass 已导入(验证 Turnstile 自动点等新逻辑继承)")

    src = inspect.getsource(d)
    check("inject_cf_cookies(browser)" in src,
          "main() 里 browser 创建后调 inject_cf_cookies")
    check("browser.add_init_js(STEALTH_INIT_JS)" in src,
          "main() 里 browser 创建后调 add_init_js(STEALTH_INIT_JS)")
    check("maybe_refresh_cf_cookies(tab)" in src,
          "fetch_one 调 maybe_refresh_cf_cookies(tab)")

    n_newtab = src.count("browser.new_tab()")
    n_init = src.count("add_init_js(STEALTH_INIT_JS)")
    check(n_newtab > 0, f"有 {n_newtab} 处 browser.new_tab() 调用")
    check(n_init >= n_newtab,
          f"add_init_js 调用({n_init}) >= new_tab 调用({n_newtab})")

    fetch_one_src = inspect.getsource(d.fetch_one)
    check("fetch_with_cf_bypass" in fetch_one_src,
          "fetch_one 调 fetch_with_cf_bypass")
    check("maybe_refresh_cf_cookies" in fetch_one_src,
          "fetch_one 调 maybe_refresh_cf_cookies(自反馈 cookie)")


def main():
    test_key_signature()
    test_wrapper_imports()
    test_detail_imports()
    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())