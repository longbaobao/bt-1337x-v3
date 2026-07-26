import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_keys.py Chrome 实例 LRU 注册表测试。

针对诉求: 最多 -c 个 ChromiumPage 实例同时存活,开新前 LRU 关掉最老的。
- 用 data/chrome_instances/{pid}.json 记录每个实例(per-pid 文件,免锁)
- list_alive_chrome_instances() 用 psutil.pid_exists 过滤掉僵尸条目
- ensure_chrome_capacity(cap) 在 count >= cap 时 kill oldest

直接跑:python test_chrome_lru.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import crawl_1337x_by_keys as w

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        w.CHROME_INSTANCES_DIR = Path(tmpdir) / "chrome_instances"
        w.CHROME_INSTANCES_DIR.mkdir()

        # ─── 纯文件操作测试(不需要 mock)─────────────────────────────

        # 1. 空目录 → list 返回 []
        check(w.list_alive_chrome_instances() == [], "空目录 → 0 个活跃实例")

        # 2. 注册一个真实存活 PID(用 os.getpid())
        real_pid = __import__("os").getpid()
        w.register_chrome_instance(real_pid, port=12345)
        alive = w.list_alive_chrome_instances()
        check(len(alive) == 1, f"注册 1 个 → list 返 1 (实际 {len(alive)})")
        check(alive[0]["pid"] == real_pid, "条目 pid 正确")
        check(alive[0]["port"] == 12345, "条目 port 正确")
        check("started_at" in alive[0], "条目有 started_at 时间戳")

        # 3. 注册一个不存在的 PID(僵尸) → list 过滤掉,文件被清理
        w.register_chrome_instance(99999999, port=22222)
        check((w.CHROME_INSTANCES_DIR / "99999999.json").exists(),
              "僵尸条目文件先创建")
        alive = w.list_alive_chrome_instances()
        check(len(alive) == 1, f"僵尸被过滤(实际 {len(alive)} 个活跃)")
        check(not (w.CHROME_INSTANCES_DIR / "99999999.json").exists(),
              "僵尸条目文件被自动清理")

        # 4. unregister → 文件删除
        w.register_chrome_instance(real_pid, port=33333)
        w.unregister_chrome_instance(real_pid)
        check(not (w.CHROME_INSTANCES_DIR / f"{real_pid}.json").exists(),
              "unregister 后文件删除")

        # 5. unregister 不存在的 PID 不抛
        try:
            w.unregister_chrome_instance(88888888)
            check(True, "unregister 不存在的 PID 不抛")
        except Exception as e:
            check(False, f"应静默却抛了: {e}")

        # 6. CHROME_INSTANCES_DIR 常量存在
        check(hasattr(w, "CHROME_INSTANCES_DIR"), "CHROME_INSTANCES_DIR 已定义")

        # 7. register 有 _started_at 参数支持
        w.register_chrome_instance(55555, port=999, _started_at=1234567890.0)
        import json as _json
        entry = _json.loads((w.CHROME_INSTANCES_DIR / "55555.json").read_text())
        check(entry["started_at"] == 1234567890.0, "_started_at 参数生效")

        # ─── ensure_chrome_capacity 测试(mock list_alive 让循环可控)────

        # 8. count < cap → no-op
        # 模拟 list_alive 返 2 个,cap=5 → 不杀
        with patch.object(w, "list_alive_chrome_instances",
                          return_value=[{"pid": 1, "started_at": 0},
                                        {"pid": 2, "started_at": 1}]), \
             patch.object(w.psutil, "Process") as proc_cls:
            killed = w.ensure_chrome_capacity(cap=5)
            check(killed is False, "count(2) < cap(5) → 不杀,返 False")
            check(proc_cls.call_count == 0, "psutil.Process 未被调用")

        # 9. count >= cap → 杀 oldest,直到 count < cap
        #    关键: mock list_alive 必须 reflect "kill 后此 PID 不再出现在列表",
        #    否则 loop 永远不收敛(类似 bug 现场重现)。
        state = {"killed": set()}
        def fake_list_alive():
            return [{"pid": i, "started_at": float(i)}
                    for i in range(5) if i not in state["killed"]]
        def fake_process(pid):
            state["killed"].add(pid)  # 模拟 "kill 后下次 list_alive 过滤掉"
            return MagicMock(
                wait=MagicMock(return_value=None),
                terminate=MagicMock(),
                kill=MagicMock(),
            )
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive), \
             patch.object(w.psutil, "Process", side_effect=fake_process):
            killed = w.ensure_chrome_capacity(cap=3)
            check(killed is True, "count(5) >= cap(3) → 杀了,返 True")
            check(state["killed"] == {0, 1, 2},
                  f"杀到 < cap 应杀 3 个 oldest (实际 {sorted(state['killed'])})")

        # 10. count == cap 边界(count=3,cap=3)→ 仍应杀(直到 < cap)
        state = {"killed": set()}
        def fake_list_alive_eq():
            return [{"pid": i, "started_at": float(i)}
                    for i in range(3) if i not in state["killed"]]
        def fake_process_eq(pid):
            state["killed"].add(pid)
            return MagicMock(
                wait=MagicMock(return_value=None),
                terminate=MagicMock(),
                kill=MagicMock(),
            )
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_eq), \
             patch.object(w.psutil, "Process", side_effect=fake_process_eq):
            killed = w.ensure_chrome_capacity(cap=3)
            check(killed is True, "count(3) == cap(3) → 杀 1 个,返 True")
            check(state["killed"] == {0},
                  f"应杀 1 个 oldest (实际 {sorted(state['killed'])})")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())