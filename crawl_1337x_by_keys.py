"""
1337x 多关键词并发抓取 wrapper。

从 data/keys.txt 读每个 key,subprocess 调用 crawl_1337x_by_key.py 处理,
成功的 key 追加到 data/keys-done.txt(线程锁保护)。
已 done 的 key 自动跳过,失败的 key 不写 done(下次重试可捡起)。

重试策略: 子脚本 crawl_1337x_by_key.py 内置 run_with_retry() 共享一个
subprocess + 共享同一 Chrome 实例,内部最多尝试 4 次(MAX_ATTEMPTS 在
子脚本里定义),断点落盘到 data/checkpoints/。wrapper 这里只管并发调度
+ 单 key 硬性超时兜底(WORKER_TIMEOUT 秒,防止子进程失控)。
失败重跑 wrapper 即可从中断页续爬,跨多次运行最终爬完大 key。

并发模型:
    -c N   ThreadPoolExecutor(N) 调 N 个 worker subprocess,每个 worker
           由 DrissionPage 子脚本内自启独立 headless Chrome(独立
           user-data-dir、独立端口),wrapper 不再管 Chrome 生命周期。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import concurrent.futures
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

KEYS_FILE = Path("data/keys.txt")
DONE_FILE = Path("data/keys-done.txt")
SCRIPT = "crawl_1337x_by_key.py"
# 单 key 子进程的硬性兜底超时(含子脚本内全部重试时间,并非每次重试的独立超时)。
# 子脚本重试时退出码非 0(超时/CF 拦截/解析失败)会被 wrapper 标记为不写 done、
# 断点保留,下次重跑 wrapper 自动从中断页续爬。
WORKER_TIMEOUT = 600

# 重试策略已移入 crawl_1337x_by_key.py 的 run_with_retry():
# 共享一个 subprocess,内部最多重试 4 次(每次自启独立 Chrome,避免卡死 page 状态污染),
# 失败时断点落盘,下次再跑 wrapper 从中断页继续。

# 全局并发设置:环境变量优先,默认 1(纯串行,向后兼容)
# 范围 [1, 16];CLI --concurrency 可临时覆盖
ENV_CONCURRENCY = "CRAWL_1337X_CONCURRENCY"
DEFAULT_CONCURRENCY = 1
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 16


def resolve_concurrency() -> int:
    """从环境变量读默认值(若非法回退到 1),CLI --concurrency 会在 argparse 后覆盖。"""
    raw = os.environ.get(ENV_CONCURRENCY)
    if raw is None or raw.strip() == "":
        return DEFAULT_CONCURRENCY
    try:
        v = int(raw)
    except ValueError:
        logger.warning(f"环境变量 {ENV_CONCURRENCY}={raw!r} 不是合法整数,回退默认 {DEFAULT_CONCURRENCY}")
        return DEFAULT_CONCURRENCY
    if not (MIN_CONCURRENCY <= v <= MAX_CONCURRENCY):
        logger.warning(
            f"环境变量 {ENV_CONCURRENCY}={v} 超出范围 [{MIN_CONCURRENCY}, {MAX_CONCURRENCY}],回退默认 {DEFAULT_CONCURRENCY}"
        )
        return DEFAULT_CONCURRENCY
    return v


def load_keys() -> list[str]:
    """读 keys.txt,trim,跳过空行与 # 注释,set 去重保序。"""
    if not KEYS_FILE.exists():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in KEYS_FILE.read_text(encoding="utf-8").splitlines():
        k = raw.strip()
        if not k or k.startswith("#"):
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def load_done() -> set[str]:
    if not DONE_FILE.exists():
        return set()
    return {
        line.strip()
        for line in DONE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def append_done(key: str, lock: threading.Lock) -> None:
    """线程安全地追加一行到 done.txt 并 flush。"""
    with lock:
        with DONE_FILE.open("a", encoding="utf-8") as f:
            f.write(key + "\n")
            f.flush()


# Chrome 实例注册表: 每个活跃 Chrome 子进程对应一个 {pid}.json。
# per-pid 文件设计免去跨进程锁,只靠 psutil.pid_exists 过滤僵尸条目。
# 主要用于跨多次 wrapper 运行共享同一个 Chrome 实例上限(防止手动多开 wrapper
# 累加出几十个 Chrome 把机器卡死)。
CHROME_INSTANCES_DIR = Path("data/chrome_instances")
CHROME_INSTANCE_KILL_TIMEOUT = 5.0  # kill 后等进程消失的最长秒数


def _chrome_instance_path(pid: int) -> Path:
    return CHROME_INSTANCES_DIR / f"{pid}.json"


def register_chrome_instance(pid: int, port: int = 0,
                             chrome_pid: int | None = None,
                             _started_at: float | None = None) -> None:
    """注册一个 Chrome 子进程条目。_started_at 仅测试用。

    chrome_pid 是 DrissionPage 拉起的 Chrome 浏览器进程 ID(≠ wrapper 子进程 PID)。
    Windows 上 psutil.Process(wrapper_pid).terminate() 只杀父进程,Chrome 子进程
    成为孤儿继续运行 —— 所以 LRU 必须直接杀 chrome_pid 才能真正关掉浏览器窗口。
    """
    CHROME_INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "port": port,
        "chrome_pid": chrome_pid,
        "started_at": _started_at if _started_at is not None else time.time(),
        "registered": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _chrome_instance_path(pid).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def unregister_chrome_instance(pid: int) -> None:
    """删除一个 Chrome 子进程条目(静默,文件不存在不抛)。"""
    try:
        _chrome_instance_path(pid).unlink(missing_ok=True)
    except Exception as e:
        logger.debug(f"unregister_chrome_instance({pid}) 失败: {e}")


def list_alive_chrome_instances() -> list[dict]:
    """枚举当前真正存活的 Chrome 实例,自动清理僵尸条目。

    psutil.pid_exists() 跨平台检查 PID 是否还活着。返回按 started_at 升序
    (最老的在前),便于 LRU 淘汰。
    """
    if not CHROME_INSTANCES_DIR.exists():
        return []
    alive: list[dict] = []
    for f in CHROME_INSTANCES_DIR.iterdir():
        if not f.name.endswith(".json"):
            continue
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            # 损坏文件 → 当僵尸清理
            try:
                f.unlink()
            except OSError:
                pass
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int):
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if not psutil.pid_exists(pid):
            # 进程已死但条目没被清理(可能 SIGKILL) → 清理僵尸文件
            try:
                f.unlink()
            except OSError:
                pass
            continue
        alive.append(entry)
    alive.sort(key=lambda e: e.get("started_at", 0))
    return alive


def _kill_proc_tree(parent_pid: int) -> bool:
    """杀 parent_pid + 它所有 children(递归)。Windows 上 parent.terminate()
    不会自动杀 children(变成孤儿),必须显式遍历 children kill。

    Returns True if parent was killed (or was already dead).
    """
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return True  # 已死,算成功
    # 1. 收集所有 descendants
    try:
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, Exception):
        children = []
    # 2. 先 terminate 所有 children(给它们时间优雅退出)
    for child in children:
        try:
            child.terminate()
        except (psutil.NoSuchProcess, Exception):
            pass
    # 3. 等 children(最多 3s)
    if children:
        gone, alive = psutil.wait_procs(children, timeout=3.0)
        for c in alive:
            try:
                c.kill()
            except (psutil.NoSuchProcess, Exception):
                pass
    # 4. 最后 terminate parent
    try:
        parent.terminate()
        try:
            parent.wait(timeout=CHROME_INSTANCE_KILL_TIMEOUT)
        except psutil.TimeoutExpired:
            logger.warning(f"PID={parent_pid} {CHROME_INSTANCE_KILL_TIMEOUT}s 内未退出,强 kill")
            parent.kill()
            try:
                parent.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                pass
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as e:
        logger.warning(f"杀 PID={parent_pid} 失败: {type(e).__name__}: {e}")
        return False


def ensure_chrome_capacity(cap: int) -> bool:
    """若活跃 Chrome 数 >= cap,杀最老的(直到 < cap)。返回是否真杀了。

    cap 语义:
      cap > 0: 杀到活跃数 < cap(常见用法)
      cap <= 0: 视为 "全部杀掉"(0 个允许)

    杀法策略:
      1. 如果条目有 chrome_pid,先 terminate Chrome 进程(直接关浏览器窗口)
      2. 然后用 psutil 杀 parent 进程树(parent + 所有 children)
         Windows 上单独 terminate parent 不会杀 children,会变孤儿
      3. 等 list_alive 下一轮过滤掉(进程死了 → 文件被清理)

    跨进程边界场景:
      - 用户手动开了多个 wrapper 同时跑 → 它们都会查这个共享目录
      - 总数超过 cap 时,后来的 wrapper 会杀先来的进程(用户的预期)
    """
    if cap < 0:
        cap = 0  # 负数视为"全部杀掉"
    target = cap
    killed = False
    while True:
        alive = list_alive_chrome_instances()
        # 当 alive < target 时已腾够位置;alive >= target 都要继续杀
        # (cap=N 含义:最多允许 N 个,所以 >= N 就要杀到 < N)
        if len(alive) < target:
            return killed
        oldest = alive[0]
        sub_pid = oldest["pid"]
        chrome_pid = oldest.get("chrome_pid")
        logger.warning(
            f"Chrome 实例数 {len(alive)} >= cap {target},"
            f"LRU 杀掉最老 sub_pid={sub_pid} chrome_pid={chrome_pid} "
            f"(started_at={oldest.get('started_at')})"
        )
        # 1. 直接杀 Chrome(若知道其 PID),绕过 psutil children 扫描的延迟
        if chrome_pid:
            try:
                cproc = psutil.Process(chrome_pid)
                cproc.terminate()
                try:
                    cproc.wait(timeout=2.0)
                except psutil.TimeoutExpired:
                    cproc.kill()
                    try:
                        cproc.wait(timeout=1.0)
                    except psutil.TimeoutExpired:
                        pass
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                logger.debug(f"直接杀 Chrome pid={chrome_pid} 失败(继续走进程树杀法): {e}")
        # 2. 进程树杀法(兜底,处理 Chrome 子进程散落到 grandchildren 等场景)
        if _kill_proc_tree(sub_pid):
            killed = True


def run_one(key: str, max_chrome: int) -> tuple[str, int, str]:
    """subprocess 跑单个 key。返回 (key, returncode, stderr_tail)。

    重试逻辑已在子脚本 crawl_1337x_by_key.py 内实现(run_with_retry 共享同一
    Chrome 实例,最多 4 次 attempt)。wrapper 这里只负责:每个 keyword 启一个
    subprocess,用 WORKER_TIMEOUT 做硬性兜底(防止子进程失控卡死)。

    Chrome 实例 LRU 上限:
      - spawn 前 ensure_chrome_capacity(max_chrome),到上限杀最老 PID
      - Popen 后 register,future 完成(unregister)删除条目
      - max_chrome 通常 = --concurrency(也支持独立环境变量覆盖)

    stdout 透传到父进程(实时看到子脚本的中文进度),stderr 截留备用(失败时 dump 尾部)。
    """
    args = [sys.executable, SCRIPT, key]
    logger.info(
        f"[开始] {key} pid={os.getpid()} "
        f"(DrissionPage 子脚本内自启 Chrome,重试策略在子脚本内,Chrome 上限={max_chrome})"
    )
    # LRU 检查:若活跃 Chrome >= cap,杀最老的腾位置
    ensure_chrome_capacity(max_chrome)
    proc = None
    try:
        # encoding 显式 utf-8:Windows 中文系统默认 GBK 会让中文 logging 崩
        # stdout 不 capture,实时看到子脚本进度;stderr 截留,失败时 dump
        proc = subprocess.Popen(
            args,
            stdout=None,           # 透传到父进程 stdout
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # 注意: 注册 chrome_pid 由子脚本内部完成(它知道 DrissionPage 拉的 Chrome 进程 ID),
        # wrapper 这里不再重复注册,避免冲突。
        try:
            stderr_bytes, _ = proc.communicate(timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 子进程超时:用进程树杀法(terminate 不杀子进程,Chrome 会成孤儿)
            _kill_proc_tree(proc.pid)
            try:
                proc.communicate(timeout=2.0)
            except Exception:
                pass
            return key, 124, f"timeout after {WORKER_TIMEOUT}s(子进程被强制终止,断点已保存)"
        stderr_tail = "\n".join((stderr_bytes or "").splitlines()[-10:])
        return key, proc.returncode, stderr_tail
    except Exception as e:
        return key, 1, f"wrapper exception: {type(e).__name__}: {e}"
    finally:
        # 子脚本的 finally 已经 unregister 了它自己的条目
        # (子脚本退出时会删 data/chrome_instances/<sub_pid>.json)
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="1337x 多关键词并发抓取 wrapper")
    parser.add_argument(
        "-c", "--concurrency", type=int, default=resolve_concurrency(), choices=range(MIN_CONCURRENCY, MAX_CONCURRENCY + 1), metavar="N",
        help=(
            f"并发 worker 数(范围 [{MIN_CONCURRENCY}, {MAX_CONCURRENCY}],默认读环境变量"
            f" {ENV_CONCURRENCY}={DEFAULT_CONCURRENCY};每个 worker 由 DrissionPage 子脚本自启独立 Chrome)"
        ),
    )
    args = parser.parse_args()
    concurrency: int = args.concurrency
    env_val = os.environ.get(ENV_CONCURRENCY)
    if env_val and env_val.strip():
        logger.info(f"全局并发设置:环境变量 {ENV_CONCURRENCY}={env_val}(本次实际并发={concurrency})")

    keys = load_keys()
    done = load_done()
    pending = [k for k in keys if k not in done]
    logger.info(
        f"=== 启动批量抓取 === keys 文件={KEYS_FILE} done 文件={DONE_FILE} "
        f"并发数={concurrency} keys.txt 共 {len(keys)} 个 key,已完成 {len(done)} 个,待处理 {len(pending)} 个"
    )
    if not pending:
        logger.info("无新 key 待处理,退出")
        return 0

    done_lock = threading.Lock()
    failed: list[tuple[str, str]] = []
    started_at = time.time()

    # Chrome 实例上限: 默认 = --concurrency,也支持 MAX_CHROME_INSTANCES 独立覆盖
    # (例如想 -c 8 并发但只允许 3 个 Chrome 同时存活,设 MAX_CHROME_INSTANCES=3)
    raw_max = os.environ.get("MAX_CHROME_INSTANCES", str(concurrency)).strip()
    try:
        max_chrome = int(raw_max) if raw_max else concurrency
    except ValueError:
        logger.warning(f"MAX_CHROME_INSTANCES={raw_max!r} 不是整数,回退到 {concurrency}")
        max_chrome = concurrency
    if max_chrome != concurrency:
        logger.info(f"Chrome 实例上限与 --concurrency 不一致: cap={max_chrome}, concurrency={concurrency}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        worker_started: dict[concurrent.futures.Future, float] = {}
        for key in pending:
            logger.info(f"[入队] {key}")
            fut = pool.submit(run_one, key, max_chrome)
            futures[fut] = key
            worker_started[fut] = time.time()
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            worker_elapsed = time.time() - worker_started[fut]
            try:
                _, rc, stderr_tail = fut.result()
            except Exception as e:
                failed.append((key, f"future 异常: {type(e).__name__}: {e}"))
                logger.error(f"[失败] {key} 耗时 {worker_elapsed:.1f}s 异常: {e}")
                continue
            if rc == 0:
                append_done(key, done_lock)
                logger.info(f"[完成] {key} 耗时 {worker_elapsed:.1f}s → 已写入 done.txt")
            else:
                logger.error(
                    f"[失败] {key} 耗时 {worker_elapsed:.1f}s 退出码={rc} "
                    f"(子脚本内已自重试,断点已保留下次可续爬)\n{stderr_tail}"
                )
                failed.append((key, f"退出码={rc}"))

    elapsed = time.time() - started_at
    ok_count = len(pending) - len(failed)
    logger.info("=" * 60)
    logger.info(f"=== 批量抓取完成,总耗时 {elapsed:.1f}s ===")
    logger.info(f"成功数: {ok_count} | 失败数: {len(failed)} | 跳过数(已完成): {len(keys) - len(pending)}")
    if failed:
        logger.info("失败列表(下次重试):")
        for k, reason in failed:
            logger.info(f"  - {k}: {reason}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())