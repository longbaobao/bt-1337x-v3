"""
1337x 多关键词并发抓取 wrapper。

从 data/keys.txt 读每个 key,subprocess 调用 crawl_1337x_by_key.py 处理,
成功的 key 追加到 data/keys-done.txt(线程锁保护)。
已 done 的 key 自动跳过,失败的 key 不写 done(下次重试可捡起)。

重试策略: 子脚本 crawl_1337x_by_key.py 内置 run_with_retry() 共享一个
subprocess,内部最多尝试 MAX_ATTEMPTS 次(每次自启独立 Chrome,避免卡死
page 状态污染),断点落盘到 data/checkpoints/。wrapper 这里只管并发调度
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
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

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


def run_one(key: str) -> tuple[str, int, str]:
    """subprocess 跑单个 key。返回 (key, returncode, stderr_tail)。

    重试逻辑已在子脚本 crawl_1337x_by_key.py 内实现(共享 Chrome 自重启重试,
    最多 MAX_ATTEMPTS 次)。wrapper 这里只负责:每个 keyword 启一个 subprocess,
    用 WORKER_TIMEOUT 做硬性兜底(防止子进程失控卡死)。

    stdout 透传到父进程(实时看到子脚本的中文进度),stderr 截留备用(失败时 dump 尾部)。
    """
    args = [sys.executable, SCRIPT, key]
    logger.info(
        f"[开始] {key} pid={os.getpid()} "
        f"(DrissionPage 子脚本内自启 Chrome + 子脚本内重试 {MAX_ATTEMPTS} 次)"
    )
    try:
        # encoding 显式 utf-8:Windows 中文系统默认 GBK 会让中文 logging 崩
        # stdout 不 capture,实时看到子脚本进度;stderr 截留,失败时 dump
        proc = subprocess.run(
            args,
            stdout=None,           # 透传到父进程 stdout
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=WORKER_TIMEOUT,
        )
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-10:])
        return key, proc.returncode, stderr_tail
    except subprocess.TimeoutExpired:
        return key, 124, f"timeout after {WORKER_TIMEOUT}s(子进程可能被强制终止,断点已保存)"
    except Exception as e:
        return key, 1, f"wrapper exception: {type(e).__name__}: {e}"


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

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        worker_started: dict[concurrent.futures.Future, float] = {}
        for key in pending:
            logger.info(f"[入队] {key}")
            fut = pool.submit(run_one, key)
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