"""crawl_1337x_by_key.py 断点续爬 checkpoint 助手的最小测试。

直接跑:python test_checkpoint.py
不依赖网络/Chrome/MongoDB,只测纯文件逻辑。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
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
    # 把 checkpoint 目录指到临时目录,不污染 data/
    ck.CHECKPOINT_DIR = Path(tempfile.mkdtemp(prefix="cp_test_"))

    # 1. 无 checkpoint → (0, 0)
    check(ck.load_checkpoint("005") == (0, 0), "无 checkpoint 返回 (0,0)")

    # 2. 写入后能读回
    ck.save_checkpoint("005", 7, 50)
    check(ck.load_checkpoint("005") == (7, 50), "save/load 往返 (7,50)")

    # 3. 覆盖写(模拟每页推进)
    ck.save_checkpoint("005", 8, 50)
    check(ck.load_checkpoint("005") == (8, 50), "覆盖写推进到 (8,50)")

    # 4. 不同 key 互不干扰
    ck.save_checkpoint("006", 3, 20)
    check(ck.load_checkpoint("005") == (8, 50), "005 不受 006 影响")
    check(ck.load_checkpoint("006") == (3, 20), "006 独立 (3,20)")

    # 5. 文件名安全化:含 / . 等字符不产生非法路径
    p = ck._checkpoint_path("pan.quark/foo bar")
    check("/" not in p.name and "\\" not in p.name and " " not in p.name,
          f"非法字符被安全化: {p.name}")

    # 6. 不同 key 文件名不冲突(md5 后缀)
    check(ck._checkpoint_path("005") != ck._checkpoint_path("006"),
          "不同 key checkpoint 路径不同")

    # 7. clear 后回到 (0,0)
    ck.clear_checkpoint("005")
    check(ck.load_checkpoint("005") == (0, 0), "clear 后回到 (0,0)")

    # 8. clear 不存在的 key 不报错
    ck.clear_checkpoint("nonexistent")
    check(True, "clear 不存在的 key 不抛异常")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
