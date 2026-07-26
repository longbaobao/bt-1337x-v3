"""
1337x 搜索结果抓取：单个关键词全量翻页，落 MongoDB。

DrissionPage 拉自己的 Chrome,每个子脚本独立管理浏览器生命周期。
keyword 通过命令行参数传入，方便被 crawl_1337x_by_keys.py 并发调用。

Headless 模式：DrissionPage 4.1.1.4 的 .headless(True) 在 Windows 上有 bug
(传 --headless=new,Chrome 不监听 ws endpoint,DrissionPage 连不上报 404)。
变通方案:用 set_argument('--headless')(老式 flag),Chrome 会监听 ws,能正常
启 headless 无窗口。.set_headless() 旧 API 在 4.1.1.4 不存在。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import json
import os
import re
import time
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from DrissionPage import ChromiumPage, ChromiumOptions
from bs4 import BeautifulSoup
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE = "https://1337x.to"
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "bt_13337x_spider_db"
COLL_NAME = "bt_info_list"

# 旧 Playwright 时代用的 CDP 端口常量,留作向后兼容(给 crawl_detail_1337x.py 复用),
# 本脚本已不再使用(DrissionPage 用 auto_port 自启)。如果 detail crawler 也迁走,即可删除。
CDP_URL = "http://127.0.0.1:9222"

# 全局并发设置:与 wrapper 共享同一环境变量名;本脚本是单 key 单进程,
# 不实际使用此值,仅在启动日志中 echo 以保持 API 一致
ENV_CONCURRENCY = "CRAWL_1337X_CONCURRENCY"

# 单页之间间隔（秒），礼貌爬取
PAGE_SLEEP = 1.0

# 子脚本内部重试:失败(超时/CF 拦截/未渲染等)自动再跑,Chrome 每次重新创建
# (前次失败时的卡死 page 状态不应跨 attempt 保留)。
# 1 次初始 + 最多 3 次重试 = 最多 4 次尝试。
MAX_ATTEMPTS = 4
RETRY_BACKOFF = 5  # 每次尝试前 sleep 秒数

# 断点续爬 checkpoint 目录:每个 keyword 一个 JSON,记录已爬到的页码。
# 子进程被 wrapper 超时 kill 后,重试可从中断页继续,而不是重头爬(避免大 key 永远超时无进展)。
CHECKPOINT_DIR = Path("data/checkpoints")


def _checkpoint_path(keyword: str) -> Path:
    """keyword → checkpoint 文件路径。文件名做安全化 + md5 后缀防冲突/防非法字符。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", keyword)[:40]
    h = hashlib.md5(keyword.encode()).hexdigest()[:8]
    return CHECKPOINT_DIR / f"{safe}-{h}.json"


def load_checkpoint(keyword: str) -> tuple[int, int]:
    """读取 (done_page, last_page)。无 checkpoint 或读取失败返回 (0, 0)。"""
    p = _checkpoint_path(keyword)
    if not p.exists():
        return 0, 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data.get("done_page", 0)), int(data.get("last_page", 0))
    except Exception as e:
        logger.warning(f"读取 checkpoint 失败({p.name}): {e}，当作无 checkpoint 从头开始")
        return 0, 0


def save_checkpoint(keyword: str, done_page: int, last_page: int) -> None:
    """原子写 checkpoint(先写 .tmp 再 replace),防止子进程被 kill 时留下半截损坏文件。"""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    p = _checkpoint_path(keyword)
    tmp = p.parent / (p.name + ".tmp")
    payload = {
        "keyword": keyword,
        "done_page": done_page,
        "last_page": last_page,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)  # 同盘原子替换


def clear_checkpoint(keyword: str) -> None:
    """全部爬完后删除 checkpoint。"""
    p = _checkpoint_path(keyword)
    try:
        p.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"删除 checkpoint 失败({p.name}): {e}")

# 1337x 时间格式: "Oct. 21st '22" / "2am Jul. 13th" / "Jul. 29th '24"
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_1337x_time(s: str) -> str:
    """1337x 列表里的时间文本 → 'yyyy-mm-dd hh:mm:ss'。

    支持的形式:
        'Oct. 21st 22'  → '2022-10-21 00:00:00'
        '2am Jul. 13th' → '<当前年>-07-13 02:00:00'
        'Jul. 29th 24'  → '2024-07-29 00:00:00'
    无法解析时返回空串。
    """
    if not s:
        return ""
    # 去掉前导时间，如 "2am "、"10pm "
    s = re.sub(r"^\d{1,2}(?:am|pm)\s+", "", s.strip(), flags=re.IGNORECASE)
    # 形如 "Oct. 21st '22" 或 "Jul. 29th 24"
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*'?(?:(\d{2,4}))?", s)
    if not m:
        return ""
    month_num = MONTH_MAP.get(m.group(1).lower()[:3])
    if not month_num:
        return ""
    day = int(m.group(2))
    yy_raw = m.group(3)
    if yy_raw:
        year = int(yy_raw)
        if year < 100:
            year += 2000
    else:
        year = datetime.now().year
    return f"{year:04d}-{month_num:02d}-{day:02d} 00:00:00"


def detect_last_page(html: str) -> int:
    """从分页栏提取最后一页页码。1337x 的分页 DOM：<div class="pagination">...<a href="/search/House/N/">N</a>...</div>"""
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.select("div.pagination a[href]"):
        m = re.search(r"/search/[^/]+/(\d+)/?", a["href"])
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 1


def has_result_rows(html: str) -> bool:
    """页面是否含至少一行结果(table.table-list 里有 tbody tr)。

    用于区分「正常有结果」与「Cloudflare 软墙/未渲染返回的空表格骨架」。
    1337x 有结果时表格必有行;拿到 0 行几乎都是被挡或没加载完,不能当成爬完。
    """
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select("table.table-list tbody tr"))


def parse_listing(html: str, keyword: str) -> list[dict]:
    """解析搜索结果表格的每一行。

    1337x 真实列结构（注意 time 用的是 coll-date，不是 coll-4）：
        coll-1     name (含 HD 图标)
        coll-2     seeds
        coll-3     leeches
        coll-date  time
        coll-4     size (mobile 视图拼了 seeds 在末尾，形如 "1.6 GB 7545")
        coll-5     uploader
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tr in soup.select("table.table-list tbody tr"):
        try:
            # coll-1 里通常有两个 <a>：第一个是图标（HD 标志），第二个是名字
            anchors = tr.select("td.coll-1 a")
            name_anchor = anchors[1] if len(anchors) >= 2 else (anchors[0] if anchors else None)
            if not name_anchor:
                continue
            name = name_anchor.get_text(strip=True)
            detail_url = urljoin(BASE, name_anchor.get("href", ""))

            seeds_txt = tr.select_one("td.coll-2").get_text(strip=True) if tr.select_one("td.coll-2") else "0"
            leech_txt = tr.select_one("td.coll-3").get_text(strip=True) if tr.select_one("td.coll-3") else "0"

            # 时间列 class 是 coll-date，不是 coll-4
            time_txt = tr.select_one("td.coll-date").get_text(strip=True) if tr.select_one("td.coll-date") else ""

            # size 单元格 mobile 视图拼了 seeds 在末尾，形如 "1.6 GB 7545"，只取前两段
            size_cell = tr.select_one("td.coll-4")
            size = " ".join(size_cell.get_text(" ", strip=True).split()[:2]) if size_cell else ""

            uploader = tr.select_one("td.coll-5").get_text(strip=True) if tr.select_one("td.coll-5") else ""

            item = {
                "_id": hashlib.md5(detail_url.encode()).hexdigest(),
                "name": name,
                "detail_url": detail_url,
                "seeds": int(seeds_txt) if seeds_txt.isdigit() else 0,
                "leechers": int(leech_txt) if leech_txt.isdigit() else 0,
                "size": size,
                "list_time": parse_1337x_time(time_txt),
                "uploader": uploader,
                "keyword": keyword,
                "source": "1337x",
                "c_time": datetime.now(),
            }
            items.append(item)
        except Exception as e:
            logger.warning(f"解析行失败: {e}")
    return items


def load_page_with_retry(tab, url: str, page_num: int, retries: int = 3) -> str | None:
    """带重试地加载页面，超出重试次数返回 None（让上层跳过）。

    DrissionPage API(传入的 tab 实际就是 ChromiumPage,本身即一个 tab):
      tab.get(url)               — 导航
      tab.wait.load_start()      — 等同 Playwright wait_until="domcontentloaded"
      tab.ele(sel, timeout=N)    — 等同 Playwright wait_for_selector(sel, timeout=N*1000)
      tab.html                   — 等同 page.content()

    Cloudflare 5秒盾由 fetch_with_cf_bypass 内置处理 (见下), 无需手动 retry。

    注意 target_selector 用 "table.table-list tbody tr"(要求至少一行结果),
    而不是 "table.table-list"(只要表格骨架)。否则 CF 软墙/未渲染返回的空表格
    会被当成"加载成功",导致 detect_last_page=1、解析 0 条、误判爬完写入 done。
    """
    try:
        return fetch_with_cf_bypass(tab, url, "table.table-list tbody tr", max_wait=45)
    except Exception as e:
        logger.warning(f"第 {page_num} 页加载失败: {type(e).__name__}: {str(e)[:80]}")
        return None


# Cloudflare 5秒盾特征字符串（CDN 在中国镜像成中文 "请稍候…"）
_CF_CHALLENGE_MARKERS = (
    "请稍候",
    "Just a moment",
    "cf_chl_opt",
    "challenge-form",
    "Checking your browser",
)


def fetch_with_cf_bypass(tab, url: str, target_selector: str, max_wait: int = 45) -> str:
    """访问 URL, 自动处理 Cloudflare 5秒盾, 轮询直到目标元素出现或超时。

    策略:
      1. tab.get(url) 触发导航
      2. wait.load_start 等 DOMContentLoaded
      3. 检查 HTML 是否含 Cloudflare 挑战页特征 (5秒盾)
         - 是: 等 5s 后重 fetch (challenge JS 通常 5s 后自动 redirect)
      4. 检查目标元素是否出现
         - 否: 等 3s 后重 fetch (页面可能还在加载)
      5. 出现 → 返回 html
      6. max_wait 秒后仍未达成 → 抛 TimeoutError

    ⚠️ HEADLESS 限制: Cloudflare bot 检测对 headless Chrome 极度激进, 会持续返回
    盾页 (即使每次 fetch 都等 5s), 此 helper 无法绕过。要爬 1337x 必须用
    visible Chrome 模式 (不传 --headless), 或预热 Chrome profile 注入 cf_clearance
    cookie 后再 headless。详见 crawl_1337x_by_key.py 顶部 docstring。

    Raises: TimeoutError (目标元素未出现) / 原始 DrissionPage 异常。
    """
    from DrissionPage.errors import ElementNotFoundError, PageDisconnectedError

    deadline = time.time() + max_wait
    attempts = 0
    last_stage = "init"
    while time.time() < deadline:
        attempts += 1
        try:
            tab.get(url)
            tab.wait.load_start()
            html = tab.html
            # 检测 Cloudflare 5秒盾中间页
            if any(m in html for m in _CF_CHALLENGE_MARKERS):
                last_stage = "cf_shield"
                logger.info(
                    f"  fetch 第 {attempts} 次: 遇 Cloudflare 5秒盾,等 5s 后重试"
                )
                time.sleep(5)
                continue
            # 检测目标元素
            try:
                tab.ele(target_selector, timeout=8)
                last_stage = "ok"
                return html
            except ElementNotFoundError:
                last_stage = "no_target"
                logger.info(
                    f"  fetch 第 {attempts} 次: 未找到 {target_selector},等 3s 后重试"
                )
                time.sleep(3)
                continue
        except PageDisconnectedError as e:
            last_stage = "page_disconnected"
            logger.warning(f"  fetch 第 {attempts} 次: 页面断开 {e}, 等 2s 后重试")
            time.sleep(2)
            continue
        except Exception as e:
            last_stage = f"{type(e).__name__}"
            logger.warning(
                f"  fetch 第 {attempts} 次: {type(e).__name__}: {str(e)[:80]}, 等 3s 后重试"
            )
            time.sleep(3)
            continue

    raise TimeoutError(
        f"fetch 等 {max_wait}s 仍未拿到 {target_selector} (尝试 {attempts} 次, 最后阶段: {last_stage})"
    )


def main(keyword: str, page, coll, started_at: float) -> int:
    """抓取单个 keyword 全量翻页(单次尝试)。
    Chrome 由调用方 run_with_retry 创建/关闭;本函数只负责翻页+checkpoint+落库。
    返回 0=全部爬完,非 0=失败(交给外层重试)。
    """
    search_url = f"{BASE}/search/{keyword}/{{page}}/"

    # 断点续爬:读上次进度。done_page=已处理到的页,last_page=总页数。
    done_page, last_page = load_checkpoint(keyword)
    resuming = done_page > 0 and last_page > 0

    def _process(html: str, page_num: int) -> None:
        items = parse_listing(html, keyword)
        new_count = 0
        for it in items:
            if coll.update_one({"_id": it["_id"]}, {"$set": it}, upsert=True).upserted_id:
                new_count += 1
        logger.info(f"[{page_num}/{last_page}] 解析 {len(items)} 条，新写入 MongoDB {new_count} 条")

    if resuming and done_page >= last_page:
        logger.info(f"checkpoint 显示 {keyword} 已全部完成({done_page}/{last_page}),直接收尾")
    else:
        if resuming:
            start_page = done_page + 1
            logger.info(f"断点续爬 {keyword}: 已完成 {done_page}/{last_page} 页,从第 {start_page} 页继续")
        else:
            # 首次运行:打开第 1 页,探测总页数并处理
            first_html = load_page_with_retry(page, search_url.format(page=1), 1)
            if first_html is None:
                logger.error("第 1 页加载失败，无法启动")
                return 2
            # 兜底:即使拿到 html,若无结果行(CF 软墙/未渲染的空表格),
            # 判定失败,不清 checkpoint、不写 done,交给 wrapper 重试。
            if not has_result_rows(first_html):
                logger.error("第 1 页无结果行(疑似被 Cloudflare 拦截或未加载完),判定失败,交给上层重试")
                return 3
            last_page = detect_last_page(first_html)
            logger.info(f"搜索 {keyword} 共 {last_page} 页，开始全量翻页")
            _process(first_html, 1)
            done_page = 1
            save_checkpoint(keyword, done_page, last_page)
            start_page = 2

        # 翻 start_page..N
        for n in range(start_page, last_page + 1):
            url = search_url.format(page=n)
            html = load_page_with_retry(page, url, n)
            if html is None:
                logger.warning(f"第 {n} 页重试耗尽，跳过(标记已处理,避免卡住进度)")
            else:
                _process(html, n)
            # 无论成功/跳过都推进 checkpoint,保证重试单调前进,不会永远卡在同一页
            done_page = n
            save_checkpoint(keyword, done_page, last_page)
            time.sleep(PAGE_SLEEP)

    total = coll.count_documents({"keyword": keyword})
    elapsed = time.time() - started_at
    logger.info(
        f"=== 完成 keyword={keyword} 耗时 {elapsed:.1f}s "
        f"库内 {DB_NAME}.{COLL_NAME} 中该 keyword 共 {total} 条 ==="
    )
    clear_checkpoint(keyword)  # 全部爬完,清掉 checkpoint
    return 0


def run_with_retry(keyword: str) -> int:
    """共享一个 Chrome 实例,最多尝试 MAX_ATTEMPTS 次 main(keyword)。

    关键设计:Chrome 只在第 1 次尝试前启动,失败后退出当前 Chrome、重启新的;
    浏览器/CF cookie 状态不跨 attempts 保留(否则前次失败时的卡死状态可能带过来)。

    返回 0=全部爬完,非 0=MAX_ATTEMPTS 次都失败。
    """
    env_val = os.environ.get(ENV_CONCURRENCY, "").strip()
    logger.info(f"=== 开始抓取 keyword={keyword!r} (最多 {MAX_ATTEMPTS} 次,每次自启 Chrome) ===")
    if env_val:
        logger.info(f"全局并发设置:环境变量 {ENV_CONCURRENCY}={env_val}(本脚本单 key 单进程,仅记录)")
    started_at = time.time()

    client = MongoClient(MONGO_URI)
    coll = client[DB_NAME][COLL_NAME]
    logger.info(f"MongoDB 已连接: {MONGO_URI}{DB_NAME}.{COLL_NAME}")

    # DrissionPage 自拉 Chrome,完全独立,不接管外部 Chrome
    # ChromiumPage 本身即一个 tab,可直接当 tab 用,无需 new_tab()
    # auto_port(True) 强制自启独立 Chrome(不 attach 用户 9222)
    # set_argument('--headless') 用老式 flag (不是 --headless=new),
    # 绕过 DrissionPage 4.1.1.4 .headless(True) 在 Windows 上 ws 连接失败的 bug,
    # 实现真 headless 无窗口运行。
    options = (ChromiumOptions()
               # .set_argument("--headless")
               .auto_port(True))

    rc = 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        page = None
        try:
            page = ChromiumPage(options)
            logger.info(f"[尝试 {attempt}/{MAX_ATTEMPTS}] Chrome 已启动 (address={options.address})")
            rc = main(keyword, page, coll, started_at)
            if rc == 0:
                return 0
        except Exception as e:
            logger.error(f"[尝试 {attempt}] 异常: {type(e).__name__}: {e}")
            rc = 99
        finally:
            if page is not None:
                try:
                    page.quit()
                    logger.info(f"[尝试 {attempt}] Chrome 已关闭")
                except Exception as e:
                    logger.warning(f"[尝试 {attempt}] 关闭 Chrome 异常: {type(e).__name__}: {e}")

        if attempt < MAX_ATTEMPTS:
            logger.warning(
                f"[尝试 {attempt}] 失败 rc={rc},{RETRY_BACKOFF}s 后从中断页续爬"
            )
            time.sleep(RETRY_BACKOFF)

    logger.error(f"=== 失败 keyword={keyword} {MAX_ATTEMPTS} 次尝试均失败 ===")
    return rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="1337x 单关键词全量抓取（落 MongoDB）",
    )
    parser.add_argument(
        "keyword",
        help="搜索关键词（会作为 MongoDB 文档 keyword 字段值）",
    )
    args = parser.parse_args()
    sys.exit(run_with_retry(args.keyword))