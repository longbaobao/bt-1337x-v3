"""crawl_1337x_by_keys.py 进程树 kill 测试。

bug: register_chrome_instance 只跟踪 subprocess PID,kill 时 psutil.Process(sub_pid)
只杀父,Chrome 子进程成为孤儿继续跑(taskbar 图标不掉)。

修复方向:
1. subprocess 注册时把 chrome_pid 也带上
2. ensure_chrome_capacity 杀进程树(children recursive + parent),
   不仅杀 PID,还杀所有 child Chrome

直接跑:python test_kill_tree.py
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

        # 1. register 支持 chrome_pid 参数
        w.register_chrome_instance(pid=12345, chrome_pid=99999)
        import json as _json
        entry = _json.loads((w.CHROME_INSTANCES_DIR / "12345.json").read_text())
        check(entry["pid"] == 12345, "条目 pid 正确")
        check(entry["chrome_pid"] == 99999, "条目 chrome_pid 正确(新)")

        # 2. chrome_pid 可选(向后兼容,旧测试不传也能跑)
        w.register_chrome_instance(pid=22222)
        entry = _json.loads((w.CHROME_INSTANCES_DIR / "22222.json").read_text())
        check("chrome_pid" in entry, "chrome_pid 字段始终存在")
        check(entry["chrome_pid"] is None, "未传 chrome_pid → None")

        # 3. ensure_chrome_capacity 应该杀掉 Chrome child + parent
        #    (进程树杀法),而不仅杀 subprocess PID
        processes_created = []  # [(pid, was_killed, was_terminated)]
        killed_pids = []
        terminated_pids = []
        # 跟踪已"杀"的 PID,list_alive 用它模拟"杀后文件被清理"
        state = {"killed": set()}

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
                # 杀完后 wait 应立即返 0(成功退出);否则 psutil.wait_procs 会阻塞到 timeout
                return 0 if not self._running else None
            def kill(self):
                self._running = False
                killed_pids.append(self.pid)
                state["killed"].add(self.pid)

        def fake_list_alive():
            # 过滤已"杀"的 PID(模拟真实世界 list_alive 检测到进程已死)
            all_entries = [
                {"pid": 12345, "chrome_pid": 99999, "started_at": 0.0},
            ]
            return [e for e in all_entries
                    if e["pid"] not in state["killed"]
                    and e["chrome_pid"] not in state["killed"]]

        def fake_process_factory(pid):
            # 已经"杀"过的 PID → psutil.NoSuchProcess
            if pid in state["killed"]:
                import psutil as _ps
                raise _ps.NoSuchProcess(pid)
            # 已经在 processes_created 里 → 返同一个 FakeProcess (idempotent)
            for fp in processes_created:
                if fp.pid == pid:
                    return fp
            return FakeProcess(pid)

        with patch.object(w, "list_alive_chrome_instances", side_effect=fake_list_alive), \
             patch.object(w.psutil, "Process", side_effect=fake_process_factory):
            killed = w.ensure_chrome_capacity(cap=1)  # cap=1,1 alive → 必杀
            check(killed is True, "cap=1, count=1 → 杀了,返 True")
            # Chrome child 99999 应该被 terminate (ensure 直接杀 chrome_pid)
            check(99999 in terminated_pids or 99999 in killed_pids,
                  f"Chrome child (99999) 被 terminate 或 kill (实际 terminate={terminated_pids}, kill={killed_pids})")
            # Subprocess parent 12345 应该也被 terminate 或 kill
            check(12345 in terminated_pids or 12345 in killed_pids,
                  f"Subprocess parent (12345) 被 terminate 或 kill")

        # 4. 没 chrome_pid 字段的旧条目(向后兼容)→ 只杀 subprocess PID,不崩
        # 清空目录,注册一个无 chrome_pid 的条目
        for f in w.CHROME_INSTANCES_DIR.iterdir():
            f.unlink()
        w.register_chrome_instance(pid=33333)  # 不传 chrome_pid
        # list_alive 返这个,且过滤已杀的 PID(否则 loop 不收敛)
        def fake_list_alive_compat():
            return [e for e in [{"pid": 33333, "chrome_pid": None, "started_at": 0.0}]
                    if e["pid"] not in state["killed"]]
        with patch.object(w, "list_alive_chrome_instances",
                          side_effect=fake_list_alive_compat), \
             patch.object(w.psutil, "Process", side_effect=fake_process_factory):
            try:
                killed = w.ensure_chrome_capacity(cap=1)
                check(True, "旧条目(无 chrome_pid)兼容, 不崩")
                check(33333 in terminated_pids or 33333 in killed_pids,
                      "旧条目(无 chrome_pid)也能杀 sub_pid")
            except Exception as e:
                check(False, f"兼容失败: {type(e).__name__}: {e}")

        print()
        if failures:
            print(f"❌ {len(failures)} 个断言失败")
            return 1
        print("✅ 全部通过")
        return 0


if __name__ == "__main__":
    sys.exit(main())