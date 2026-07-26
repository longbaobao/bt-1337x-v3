import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py 页面 phase timing 助手测试。

加 observability: 每页打印 [N/M] fetch=Xs ... total=Xs,
让用户能看出时间花在哪里(CF 重试 / HTML 序列化 / BS4 / MongoDB / sleep)。

直接跑:python test/page_timing.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    # 1. PageTimer 类存在,有 start/stop/elapsed/dict 接口
    check(hasattr(ck, "PageTimer"), "PageTimer 已定义")

    # 2. 简单 timing 准确性
    t = ck.PageTimer()
    t.start("a")
    time.sleep(0.05)
    t.stop("a")
    t.start("b")
    time.sleep(0.02)
    t.stop("b")
    timings = t.to_dict()
    check("a" in timings and "b" in timings, "to_dict 包含所有阶段")
    check(0.04 < timings["a"] < 0.15, f"a 阶段时长合理 (~0.05s): {timings['a']:.3f}")
    check(0.01 < timings["b"] < 0.10, f"b 阶段时长合理 (~0.02s): {timings['b']:.3f}")

    # 3. 累加同一个阶段名(start 多次累加)
    t2 = ck.PageTimer()
    t2.start("loop")
    time.sleep(0.02)
    t2.stop("loop")
    t2.start("loop")
    time.sleep(0.02)
    t2.stop("loop")
    timings2 = t2.to_dict()
    check(0.03 < timings2["loop"] < 0.10,
          f"同名阶段累加 (~0.04s): {timings2['loop']:.3f}")

    # 4. 没 start 就 stop → 静默,不出错
    t3 = ck.PageTimer()
    try:
        t3.stop("never_started")
        check(True, "stop 未开始的阶段 → 静默")
    except Exception as e:
        check(False, f"应静默却抛: {e}")

    # 5. total_elapsed 返回从首次 start 到 now 的总时长
    t4 = ck.PageTimer()
    t4.start("work")
    time.sleep(0.05)
    total = t4.total_elapsed()
    check(0.04 < total < 0.5, f"total_elapsed 包含未 stop 的 in-flight 阶段: {total:.3f}")

    # 6. format_phase_log 输出类似 "[42/50] fetch=15.3s (cf=2x retry) parse=0.5s sleep=1.0s total=17.3s"
    t5 = ck.PageTimer()
    t5.start("fetch")
    time.sleep(0.01)
    t5.stop("fetch")
    t5.start("parse")
    time.sleep(0.01)
    t5.stop("parse")
    t5.start("sleep")
    time.sleep(0.01)
    t5.stop("sleep")
    log = ck.format_phase_log(page_num=42, total_pages=50, timer=t5,
                              cf_attempts=2, items_found=20)
    check("42/50" in log, f"log 含页码: {log}")
    check("fetch=" in log and "parse=" in log and "sleep=" in log and "total=" in log,
          f"log 含各阶段: {log}")
    check("cf=2x" in log, f"log 含 CF 重试次数: {log}")
    check("items=20" in log, f"log 含解析条数: {log}")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())