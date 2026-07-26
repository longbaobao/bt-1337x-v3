import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py 重构后的签名测试。

重构后:
- main(keyword, page, coll, started_at) 单次尝试,4 参
- run_with_retry(keyword) 共享 Chrome 自重启重试
- __main__ 调 run_with_retry(),不再调 main()

直接跑:python test_signature.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import inspect

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. main() 签名: 4 个参数 (keyword, page, coll, started_at)
    sig = inspect.signature(ck.main)
    params = list(sig.parameters.keys())
    check(params == ["keyword", "page", "coll", "started_at"],
          f"main() 4 参签名: {params}")

    # 2. run_with_retry() 签名: 1 个参数 (keyword)
    sig = inspect.signature(ck.run_with_retry)
    params = list(sig.parameters.keys())
    check(params == ["keyword"],
          f"run_with_retry() 1 参签名: {params}")

    # 3. 重试常量都在
    check(hasattr(ck, "MAX_ATTEMPTS") and ck.MAX_ATTEMPTS == 4,
          f"MAX_ATTEMPTS={ck.MAX_ATTEMPTS} (=4)")
    check(hasattr(ck, "RETRY_BACKOFF") and ck.RETRY_BACKOFF == 5,
          f"RETRY_BACKOFF={ck.RETRY_BACKOFF} (=5)")

    # 4. main() 不再自己创建 Chrome (没有 auto_port / set_argument 调用)
    src = inspect.getsource(ck.main)
    check("auto_port" not in src, "main() 不调用 auto_port (Chrome 归 run_with_retry 管)")
    check("ChromiumPage(" not in src, "main() 不创建 ChromiumPage")

    # 5. run_with_retry() 创建并管理 Chrome —— **只创建一次**,循环内不复建
    src = inspect.getsource(ck.run_with_retry)
    check("auto_port" in src, "run_with_retry() 调 auto_port")
    n_create = src.count("ChromiumPage(")
    check(n_create == 1, f"run_with_retry() 只创建 1 次 ChromiumPage (实际 {n_create})")
    check("page.quit()" in src, "run_with_retry() 负责 quit()")
    # 关键:quit() 应在外层 finally(只调 1 次),不在循环内的 finally
    n_quit = src.count("page.quit()")
    check(n_quit == 1, f"page.quit() 只调 1 次 (实际 {n_quit})")
    # 循环体里不能有 quit,否则每次 attempt 都关 Chrome
    # 简单粗暴验证:retry loop 内不应出现 page.quit
    import re
    loop_body_match = re.search(r"for attempt in.*?time\.sleep\(RETRY_BACKOFF\)", src, re.DOTALL)
    if loop_body_match:
        loop_body = loop_body_match.group(0)
        check("page.quit()" not in loop_body, "retry 循环体内不含 page.quit() (Chrome 不跨 attempt 关)")
    else:
        check(False, "未匹配到 retry 循环体,请人工检查")

    # 6. main() 不再含 page.quit (Chrome 不归 main 管)
    src = inspect.getsource(ck.main)
    check("page.quit()" not in src, "main() 不再调 page.quit()")

    # 7. main() 用 None 当 page 会优雅降级(fetch_with_cf_bypass 内部超时后返回 2),
    #    不抛异常给 run_with_retry 增加处理负担
    rc = ck.main("nosuchkey", None, None, 0.0)
    check(rc == 2, f"main() page=None → rc=2 优雅返回(不抛异常): 实际 rc={rc}")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())