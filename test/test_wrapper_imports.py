import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_keys.py 模块加载 + 重试常量归属测试。

回归测试,防止重试常量再次错放在 wrapper 里。

重构历史:
- 重试从 wrapper 移到 crawl_1337x_by_key.py 的 run_with_retry()
- MAX_ATTEMPTS / RETRY_BACKOWN 现在只在 key 脚本里定义
- wrapper 不应再引用这两个名字(否则日志 f-string 会 NameError,
  导致每个 future 都报 future 异常,整个 batch 失败)

直接跑:python test_wrapper_imports.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import inspect
import re

import crawl_1337x_by_keys as w

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. wrapper 模块顶层 import 时无 NameError(隐式:到这一步没崩就是过了)
    check(True, "wrapper 模块 import 成功")

    # 2. 必需的常量存在
    check(hasattr(w, "WORKER_TIMEOUT") and w.WORKER_TIMEOUT == 600,
          f"WORKER_TIMEOUT = {w.WORKER_TIMEOUT} (=600)")
    check(hasattr(w, "KEYS_FILE"), "KEYS_FILE 已定义")
    check(hasattr(w, "DONE_FILE"), "DONE_FILE 已定义")

    # 3. 移走的常量在 wrapper 里**不应**存在
    check(not hasattr(w, "MAX_ATTEMPTS"), "wrapper 不再有 MAX_ATTEMPTS (移到 key 脚本)")
    check(not hasattr(w, "RETRY_BACKOFF"), "wrapper 不再有 RETRY_BACKOFF (移到 key 脚本)")

    # 4. 静态扫:wrapper 代码(剔除 docstring)里不应再出现这俩名字
    src = inspect.getsource(w)
    code_only = re.sub(r'"""[\s\S]*?"""', '', src)
    for name in ("MAX_ATTEMPTS", "RETRY_BACKOFF"):
        if re.search(r'\b' + name + r'\b', code_only):
            check(False, f"wrapper 代码里仍有 {name} 引用(f-string 会 NameError)")
        else:
            check(True, f"wrapper 代码里无 {name} 引用")

    # 5. 反向:key 脚本里这些常量应该在
    import crawl_1337x_by_key as k
    check(hasattr(k, "MAX_ATTEMPTS") and k.MAX_ATTEMPTS == 4,
          f"key 脚本 MAX_ATTEMPTS = {k.MAX_ATTEMPTS} (=4)")
    check(hasattr(k, "RETRY_BACKOFF") and k.RETRY_BACKOFF == 5,
          f"key 脚本 RETRY_BACKOFF = {k.RETRY_BACKOFF} (=5)")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())