"""
1337x 多关键词并发抓取 wrapper。

从 data/keys.txt 读每个 key,subprocess 调用 crawl_1337x_by_key.py 处理,
成功的 key 追加到 data/keys-done.txt(线程锁保护)。
已 done 的 key 自动跳过,失败的 key 不写 done(下次重试可捡起)。

并发模型:
    -c 1         串行,沿用现有 9222 Chrome(向后兼容,零侵入)
    -c N>1       自动启 N 个 Chrome 在 9222..9221+N(独立 user-data-dir)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import atexit
import concurrent.futures
import logging
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA_ROOT = Path.home() / ".chrome_debug_profile"
KEYS_FILE = Path("data/keys.txt")
DONE_FILE = Path("data/keys-done.txt")
SCRIPT = "crawl_1337x_by_key.py"
CDP_BASE_PORT = 9222
CDP_URL_PREFIX = "http://127.0.0.1:"
CDP_READY_TIMEOUT = 30  # 秒
WORKER_TIMEOUT = 600    # 单 key 上限

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


def port_in_use(port: int) -> bool:
    """探测端口是否已被占用(其他 Chrome / 服务)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_cdp_ready(port: int, timeout: int = CDP_READY_TIMEOUT) -> bool:
    """等 CDP /json/version 可访问,超时返回 False。"""
    url = f"{CDP_URL_PREFIX}{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def start_chrome(port: int) -> subprocess.Popen:
    """启一个独立 Chrome 实例,返回 Popen。"""
    user_data = USER_DATA_ROOT / f"pool_{port}"
    user_data.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    logger.info(f"启动 Chrome: port={port}, profile={user_data}")
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # 不弹黑窗
    return subprocess.Popen(args, **kwargs)


def append_done(key: str, lock: threading.Lock) -> None:
    """线程安全地追加一行到 done.txt 并 flush。"""
    with lock:
        with DONE_FILE.open("a", encoding="utf-8") as f:
            f.write(key + "\n")
            f.flush()


def run_one(key: str, cdp_url: str) -> tuple[str, int, str]:
    """subprocess 跑单个 key,返回 (key, returncode, stderr_tail)。

    stdout 透传到父进程(实时看到子脚本的中文进度),stderr 截留备用(失败时 dump 尾部)。
    """
    args = [sys.executable, SCRIPT, key, "--cdp-url", cdp_url]
    logger.info(f"[开始] {key} pid={os.getpid()} cdp_url={cdp_url}")
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
        return key, 124, f"timeout after {WORKER_TIMEOUT}s"
    except Exception as e:
        return key, 1, f"wrapper exception: {type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="1337x 多关键词并发抓取 wrapper")
    parser.add_argument(
        "-c", "--concurrency", type=int, default=resolve_concurrency(), choices=range(MIN_CONCURRENCY, MAX_CONCURRENCY + 1), metavar="N",
        help=(
            f"并发 worker 数(范围 [{MIN_CONCURRENCY}, {MAX_CONCURRENCY}],默认读环境变量"
            f" {ENV_CONCURRENCY}={DEFAULT_CONCURRENCY};>1 时 wrapper 自动启 N 个 Chrome)"
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

    chrome_procs: list[subprocess.Popen] = []
    cdp_urls: list[str] = []

    def cleanup_chrome() -> None:
        for p in chrome_procs:
            if p.poll() is None:
                logger.info(f"关闭 Chrome pid={p.pid}")
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    atexit.register(cleanup_chrome)

    def _signal_handler(signum, _frame):
        cleanup_chrome()
        sys.exit(128 + signum)

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        # Windows 下 signal 在子线程里注册会 ValueError,主线程 OK,这里兜底
        pass

    if concurrency == 1:
        cdp_urls = [f"{CDP_URL_PREFIX}{CDP_BASE_PORT}"]
        logger.info(f"并发数=1,沿用现有 Chrome {CDP_BASE_PORT} 端口")
    else:
        ports = list(range(CDP_BASE_PORT, CDP_BASE_PORT + concurrency))
        for port in ports:
            if port_in_use(port):
                logger.error(
                    f"端口 {port} 已被占用,并发模式需独占 {ports[0]}..{ports[-1]}。"
                    f"请先关闭占用的 Chrome 实例,或减小 -c 参数"
                )
                cleanup_chrome()
                return 1
        for port in ports:
            chrome_procs.append(start_chrome(port))
            cdp_urls.append(f"{CDP_URL_PREFIX}{port}")
        # 等所有 CDP 就绪
        ready_ok = True
        for port, proc in zip(ports, chrome_procs):
            if proc.poll() is not None:
                logger.error(f"Chrome 端口 {port} 启动后立即退出,退出码={proc.returncode}")
                ready_ok = False
                break
            if not wait_cdp_ready(port):
                logger.error(f"Chrome 端口 {port} CDP 未在 {CDP_READY_TIMEOUT}s 内就绪")
                ready_ok = False
                break
            logger.info(f"Chrome 端口 {port} CDP 就绪")
        if not ready_ok:
            cleanup_chrome()
            return 1

    done_lock = threading.Lock()
    failed: list[tuple[str, str]] = []
    started_at = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        worker_started: dict[concurrent.futures.Future, float] = {}
        for i, key in enumerate(pending):
            cdp_url = cdp_urls[i % concurrency]
            logger.info(f"[入队] {key} cdp_url={cdp_url}")
            fut = pool.submit(run_one, key, cdp_url)
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
                logger.error(f"[失败] {key} 耗时 {worker_elapsed:.1f}s 退出码={rc}\n{stderr_tail}")
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