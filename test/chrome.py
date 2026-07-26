import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_keys.py Chrome 实例生命周期测试。

覆盖:
- register/unregister: 文件读写 + 僵尸条目清理
- list_alive: psutil.pid_exists 过滤 + 自动清理
- ensure_chrome_capacity: LRU 淘汰到 < cap
- chrome_pid 字段: 子脚本注册时记录,确保 LRU 真杀 Chrome 不留孤儿
- _kill_proc_tree: 进程树杀法(children recursive + parent)

直接跑:python test/chrome.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
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


# ─── 纯文件操作: register / unregister / list_alive ──────────────────────────

def test_file_ops():
    print("[file_ops]")
    with tempfile.TemporaryDirectory() as tmpdir:
        w.CHROME_INSTANCES_DIR = Path(tmpdir) / "chrome_instances"
        w.CHROME_INSTANCES_DIR.mkdir()

        # 空目录
        check(w.list_alive_chrome_instances() == [], "空目录 → 0 个活跃实例")

        # 注册 + 读回
        real_pid = __import__("os").getpid()
        w.register_chrome_instance(real_pid, port=12345)
        alive = w.list_alive_chrome_instances()
        check(len(alive) == 1, f"注册 1 个 → list 返 1 (实际 {len(alive)})")
        check(alive[0]["pid"] == real_pid, "条目 pid 正确")
        check(alive[0]["port"] == 12345, "条目 port 正确")
        check("started_at" in alive[0], "条目有 started_at 时间戳")

        # 僵尸条目自动清理
        w.register_chrome_instance(99999999, port=22222)
        check((w.CHROME_INSTANCES_DIR / "99999999.json").exists(),
              "僵尸条目文件先创建")
        alive = w.list_alive_chrome_instances()
        check(len(alive) == 1, f"僵尸被过滤(实际 {len(alive)} 个活跃)")
        check(not (w.CHROME_INSTANCES_DIR / "99999999.json").exists(),
              "僵尸条目文件被自动清理")

        # unregister
        w.register_chrome_instance(real_pid, port=33333)
        w.unregister_chrome_instance(real_pid)
        check(not (w.CHROME_INSTANCES_DIR / f"{real_pid}.json").exists(),
              "unregister 后文件删除")

        # unregister 不存在的 PID
        try:
            w.unregister_chrome_instance(88888888)
            check(True, "unregister 不存在的 PID 不抛")
        except Exception as e:
            check(False, f"应静默却抛了: {e}")

        # 常量 + _started_at 参数
        check(hasattr(w, "CHROME_INSTANCES_DIR"), "CHROME_INSTANCES_DIR 已定义")
        w.register_chrome_instance(55555, port=999, _started_at=1234567890.0)
        entry = json.loads((w.CHROME_INSTANCES_DIR / "55555.json").read_text())
        check(entry["started_at"] == 1234567890.0, "_started_at 参数生效")

        # chrome_pid 字段支持
        w.register_chrome_instance(pid=12345, chrome_pid=99999)
        entry = json.loads((w.CHROME_INSTANCES_DIR / "12345.json").read_text())
        check(entry["pid"] == 12345, "chrome_pid 模式: pid 正确")
        check(entry["chrome_pid"] == 99999, "chrome_pid 模式: chrome_pid 正确")

        w.register_chrome_instance(pid=22222)  # 不传 chrome_pid
        entry = json.loads((w.CHROME_INSTANCES_DIR / "22222.json").read_text())
        check("chrome_pid" in entry, "chrome_pid 字段始终存在")
        check(entry["chrome_pid"] is None, "未传 chrome_pid → None")


# ─── ensure_chrome_capacity: LRU + 进程树杀法 ─────────────────────────────

def _make_fake_process_factory(state):
    """返回 fake_process(pid) 函数 + 共享 state 容器。"""
    import psutil as _ps
    processes_created = []
    killed_pids = []
    terminated_pids = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self._running = True
            processes_created.append(self)
            # 12345 是 subprocess parent,它有 1 个 child (Chrome 99999)
            self._children = [FakeProcess(99999)] if pid == 12345 else []
        def children(self, recursive=True):
            return self._children
        def is_running(self):
            return self._running
        def terminate(self):
            self._running = False
            terminated_pids.append(self.pid)
            state["killed"].add(self.pid)
        def wait(self, timeout=None):
            return 0 if not self._running else None
        def kill(self):
            self._running = False
            killed_pids.append(self.pid)
            state["killed"].add(self.pid)

    def factory(pid):
        if pid in state["killed"]:
            raise _ps.NoSuchProcess(pid)
        for fp in processes_created:
            if fp.pid == pid:
                return fp
        return FakeProcess(pid)

    return factory, terminated_pids, killed_pids


def test_ensure_lru():
    print("[ensure_lru]")
    with tempfile.TemporaryDirectory() as tmpdir:
        w.CHROME_INSTANCES_DIR = Path(tmpdir) / "chrome_instances"
        w.CHROME_INSTANCES_DIR.mkdir()

        # count < cap → no-op(连 psutil.Process 都不调)
        with patch.object(w, "list_alive_chrome_instances",
                          return_value=[{"pid": 1, "started_at": 0},
                                        {"pid": 2, "started_at": 1}]), \
             patch.object(w.psutil, "Process") as proc_cls:
            killed = w.ensure_chrome_capacity(cap=5)
            check(killed is False, "count(2) < cap(5) → 不杀,返 False")
            check(proc_cls.call_count == 0, "psutil.Process 未被调用")

        # count >= cap → 杀 oldest 到 < cap(mock 必须 reflect 杀后过滤,否则 loop 不收敛)
        state = {"killed": set()}
        def fake_list_alive_lru():
            return [{"pid": i, "started_at": float(i)}
                    for i in range(5) if i not in state["killed"]]
        factory, _, _ = _make_fake_process_factory(state)
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_lru), \
             patch.object(w.psutil, "Process", side_effect=factory):
            killed = w.ensure_chrome_capacity(cap=3)
            check(killed is True, "count(5) >= cap(3) → 杀了,返 True")
            check(state["killed"] == {0, 1, 2},
                  f"杀到 < cap 应杀 3 个 oldest (实际 {sorted(state['killed'])})")

        # count == cap 边界(杀到 < cap,即杀 1)
        state = {"killed": set()}
        def fake_list_alive_eq():
            return [{"pid": i, "started_at": float(i)}
                    for i in range(3) if i not in state["killed"]]
        factory, _, _ = _make_fake_process_factory(state)
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_eq), \
             patch.object(w.psutil, "Process", side_effect=factory):
            killed = w.ensure_chrome_capacity(cap=3)
            check(killed is True, "count(3) == cap(3) → 杀 1 个,返 True")
            check(state["killed"] == {0},
                  f"应杀 1 个 oldest (实际 {sorted(state['killed'])})")


def test_ensure_kill_tree():
    print("[ensure_kill_tree]")
    """进程树杀法: chrome child + subprocess parent 都该被 terminate/kill。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        w.CHROME_INSTANCES_DIR = Path(tmpdir) / "chrome_instances"
        w.CHROME_INSTANCES_DIR.mkdir()

        state = {"killed": set()}
        def fake_list_alive_killtree():
            all_entries = [
                {"pid": 12345, "chrome_pid": 99999, "started_at": 0.0},
            ]
            return [e for e in all_entries
                    if e["pid"] not in state["killed"]
                    and e["chrome_pid"] not in state["killed"]]
        factory, terminated, killed = _make_fake_process_factory(state)
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_killtree), \
             patch.object(w.psutil, "Process", side_effect=factory):
            killed_flag = w.ensure_chrome_capacity(cap=1)
            check(killed_flag is True, "cap=1, count=1 → 杀了,返 True")
            check(99999 in terminated or 99999 in killed,
                  f"Chrome child (99999) 被 terminate 或 kill (terminate={terminated}, kill={killed})")
            check(12345 in terminated or 12345 in killed,
                  f"Subprocess parent (12345) 被 terminate 或 kill")

        # 旧条目(无 chrome_pid)兼容 — 不崩
        for f in w.CHROME_INSTANCES_DIR.iterdir():
            f.unlink()
        w.register_chrome_instance(pid=33333)  # 不传 chrome_pid
        def fake_list_alive_compat():
            return [e for e in [{"pid": 33333, "chrome_pid": None, "started_at": 0.0}]
                    if e["pid"] not in state["killed"]]
        factory, terminated, killed = _make_fake_process_factory(state)
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_compat), \
             patch.object(w.psutil, "Process", side_effect=factory):
            try:
                _ = w.ensure_chrome_capacity(cap=1)
                check(True, "旧条目(无 chrome_pid)兼容, 不崩")
                check(33333 in terminated or 33333 in killed,
                      "旧条目(无 chrome_pid)也能杀 sub_pid")
            except Exception as e:
                check(False, f"兼容失败: {type(e).__name__}: {e}")


def main():
    test_file_ops()
    test_ensure_lru()
    test_ensure_kill_tree()
    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())