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

# Chrome 实例 LRU 注册(共享 wrapper 的 data/chrome_instances/ 注册表)
from crawl_1337x_by_keys import (
    register_chrome_instance, unregister_chrome_instance,
)

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

# CF 复选框/图像题分流标记(纯字符串检测用,不看 DOM 元素)
# Turnstile 有多个 markup 形式,任一命中即可识别:
#   - challenges.cloudflare.com  (iframe src,完整加载后才有)
#   - cf-turnstile               (class,早期 shell 就出现)
#   - cf-chl-widget              (class,容器)
#   - data-sitekey               (属性,JS 渲染前就有)
#   - turnstile                  (字面,JS 变量/注释里常见)
_TURNSTILE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-turnstile",
    "cf-chl-widget",
    "data-sitekey",
    "turnstile",
)
_HCAPTCHA_MARKERS = ("hcaptcha.com",)                   # hCaptcha 图像题

# 反检测: 在每个新文档加载前执行,屏蔽 CDP 指纹
# (navigator.webdriver = true 是 DrissionPage/Selenium 默认,CF 用它判 bot)
STEALTH_INIT_JS = """
// 1. navigator.webdriver 改成 false
Object.defineProperty(Navigator.prototype, 'webdriver', {
    get: () => false,
    configurable: true
});
// 2. 删 webdriver 痕迹
delete Navigator.prototype.__proto__;
// 3. window.chrome 缺失补上(部分 CF 检查)
if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
}
// 4. permissions API 修复
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
"""

# 用户手动过 CF 后导出的 cookie 文件(JSON list 格式,见 data/cf_cookies.json.example)
CF_COOKIES_FILE = Path("data/cf_cookies.json")


class CFChallengeEscalated(Exception):
    """CF 验证升级到图像题(hCaptcha),无法自动过,应快速失败而非继续重试。"""
    pass


def load_cf_cookies() -> list[dict]:
    """读 data/cf_cookies.json,返回合法 cookie 列表。

    合法格式:[{"name": "cf_clearance", "value": "...", "domain": ".1337x.to", ...}, ...]
    - 文件不存在 / JSON 损坏 / 顶层不是 list → 返回 []
    - 缺 name 或 value 的条目 → 跳过
    """
    if not CF_COOKIES_FILE.exists():
        return []
    try:
        data = json.loads(CF_COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读 {CF_COOKIES_FILE} 失败: {type(e).__name__}: {e} — 跳过 cookie 预热")
        return []
    if not isinstance(data, list):
        logger.warning(f"{CF_COOKIES_FILE} 顶层不是 list,跳过")
        return []
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        if not c.get("name") or c.get("value") is None:
            continue
        out.append(c)
    if not out and data:
        logger.warning(f"{CF_COOKIES_FILE} 里 {len(data)} 条全是无效 cookie(缺 name/value)")
    return out


def inject_cf_cookies(page) -> bool:
    """把 load_cf_cookies() 读到的 cookie 注入到当前 page。返回 True 表示注入了至少 1 条。"""
    cookies = load_cf_cookies()
    if not cookies:
        return False
    try:
        page.set.cookies(cookies)
        logger.info(f"已注入 {len(cookies)} 条 CF cookie(来自 {CF_COOKIES_FILE})")
        return True
    except Exception as e:
        logger.warning(f"注入 CF cookie 失败: {type(e).__name__}: {e}")
        return False


def _normalize_cookie(c: dict) -> dict | None:
    """把 page.cookies() 返回的 CDP cookie dict 清洗成 load_cf_cookies() 接受的子集。

    保留 name/value/domain/path/expires,丢弃 httpOnly/secure/size/session/sameSite/
    priority 等非持久化字段。注意 page.cookies() 用 'expires'(float 秒),
    load_cf_cookies() 接受 'expiry'(int 秒) —— 都保留也行,format_cookie 都认。
    """
    if not c.get("name") or c.get("value") is None:
        return None
    out = {"name": c["name"], "value": c["value"]}
    if c.get("domain"):
        out["domain"] = c["domain"]
    if c.get("path"):
        out["path"] = c["path"]
    if c.get("expires"):
        out["expires"] = c["expires"]
    return out


def maybe_refresh_cf_cookies(page) -> bool:
    """通过 CF 后,把当前 .1337x.to cookie 写回 CF_COOKIES_FILE(原子写)。

    触发场景: fetch_with_cf_bypass 拿到目标元素(刚刚穿过盾)。此时浏览器里
    的 cf_clearance / __cf_bm 是新鲜的,自动落盘,下次启动直接注入省一遍盾。

    返回:
        True  = 写盘了(文件内容变化了)
        False = 没写(无 cf_clearance / 内容相同 / 写盘失败)
    """
    try:
        raw = page.cookies(all_domains=False, all_info=True)
    except Exception as e:
        logger.debug(f"读 cookies 失败(可能是 CDP 抖动): {e}")
        return False
    # 过滤: 只留 .1337x.to(含子域),且必须有 cf_clearance 才算真正过 CF
    domain_cookies = []
    for c in raw:
        dom = c.get("domain", "")
        if not (dom == "1337x.to" or dom.endswith(".1337x.to")):
            continue
        n = _normalize_cookie(c)
        if n:
            domain_cookies.append(n)
    if not any(c["name"] == "cf_clearance" for c in domain_cookies):
        return False
    # 内容是否变了?
    existing = load_cf_cookies()
    if existing == domain_cookies:
        return False
    # 原子写
    try:
        CF_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CF_COOKIES_FILE.parent / (CF_COOKIES_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps(domain_cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(CF_COOKIES_FILE)
        logger.info(
            f"已自动刷新 {CF_COOKIES_FILE} ({len(domain_cookies)} 条 cookie, "
            f"下次启动直接注入省一遍 CF)"
        )
        return True
    except Exception as e:
        logger.warning(f"写 CF cookie 文件失败: {type(e).__name__}: {e}")
        return False


def detect_turnstile(html: str) -> bool:
    """页面是否含 Turnstile 复选框(任一 marker 命中即视为 Turnstile)。

    多 marker 检测原因: CF 早期 shell 只渲染"请稍候" + cf-turnstile class,
    iframe src 要等 JS 注入后才出现。只查 challenges.cloudflare.com 会漏判早期 shell
    → classifier 走 shield 路径只 sleep 5s → 永远过不去。
    """
    return any(m in html for m in _TURNSTILE_MARKERS)


def detect_hcaptcha(html: str) -> bool:
    """页面是否含 hCaptcha(图像题,无法自动过,应快速失败)。"""
    return any(m in html for m in _HCAPTCHA_MARKERS)


def classify_cf_challenge(html: str) -> str:
    """CF 挑战分类: 'turnstile' | 'hcaptcha' | 'shield' | 'none'。"""
    if detect_turnstile(html):
        return "turnstile"
    if detect_hcaptcha(html):
        return "hcaptcha"
    if any(m in html for m in _CF_CHALLENGE_MARKERS):  # type: ignore[name-defined]
        return "shield"
    return "none"


# Turnstile 复选框在沙箱跨域 iframe 里,DOM 访问被挡,只能从父页发鼠标事件。
# CF bot 检测看:
#   - navigator.webdriver (用 STEALTH_INIT_JS 覆盖)
#   - 鼠标移动轨迹是否自然 (CDP Input.dispatchMouseEvent 多步 mousemove)
#   - 点击位置是否"太正中" (偏左 25% 模拟真实用户点击 checkbox 位置)
# 关键:tab.ele() 内部会触发 wait.doc_loaded()(chromium_base.py:454),
# 等 Page.loadEventFired,但 CF 盾页面永远不发 → 等满 timeout 返回 None。
# 所以必须用直接 CDP DOM.querySelectorAll 找 iframe,绕开 wait.doc_loaded。
_TURNSTILE_BOX_FRACTION = (0.25, 0.5)  # 复选框在 iframe (左 25%, 中线 50%)


def _find_turnstile_iframe(tab):
    """直接 CDP 找 Turnstile iframe,返回 (objectId, content_box) 或 None。

    content_box = (x1, y1, x2, y2) 浏览器视口坐标(content,非 border)。
    """
    try:
        r = tab._run_cdp(
            "DOM.querySelectorAll",
            selector='iframe[src*="challenges.cloudflare.com"]',
        )
    except Exception as e:
        logger.info(f"querySelectorAll 找 iframe 失败: {e}")
        return None
    if not r or not r.get("nodeIds"):
        return None
    for backend_node_id in r["nodeIds"]:
        if backend_node_id == 0:
            continue
        try:
            obj = tab._run_cdp("DOM.resolveNode", backendNodeId=backend_node_id)
            object_id = obj["object"]["objectId"]
            box = tab._run_cdp("DOM.getBoxModel", objectId=object_id)
        except Exception:
            continue
        model = box.get("model", {})
        content = model.get("content")
        if not content or len(content) < 4:
            continue
        # content = [x1, y1, x2, y2, x3, y3, x4, y4](四边形)
        x1, y1, x2, y2 = content[0], content[1], content[4], content[5]
        return object_id, (x1, y1, x2, y2)
    return None


def _cdp_click(tab, x: float, y: float, steps: int = 6, step_sleep: float = 0.04):
    """直接走 CDP Input.dispatchMouseEvent 发真实鼠标事件(isTrusted=true)。

    steps 个 mousemove 中间点(step_sleep 秒间隔)→ mousedown → mouseup,
    模拟真实用户的鼠标轨迹。CF Turnstile 接受 isTrusted=true 的事件。
    """
    try:
        cur = tab._run_cdp("Input.dispatchMouseEvent",
                            type="mouseMoved", x=x, y=y, button="none")
        # cur 一般返回当前位置;如果浏览器已记录 cursor,新 mousemove 会接着走
        for i in range(1, steps + 1):
            tab._run_cdp("Input.dispatchMouseEvent",
                          type="mouseMoved",
                          x=x + (i * 0.5), y=y + (i * 0.5),  # 细微抖动
                          button="none")
            time.sleep(step_sleep)
    except Exception as e:
        logger.debug(f"mousemove 失败: {e}")
    # mousedown + mouseup (clickCount=1)
    try:
        tab._run_cdp("Input.dispatchMouseEvent", type="mousePressed",
                      x=x, y=y, button="left", buttons="left", clickCount=1)
        time.sleep(0.05)
        tab._run_cdp("Input.dispatchMouseEvent", type="mouseReleased",
                      x=x, y=y, button="left", clickCount=1)
    except Exception as e:
        logger.info(f"mousedown/up 失败: {e}")
        return False
    return True


def _try_click_turnstile_checkbox(tab) -> bool:
    """真实模拟鼠标过 Turnstile 复选框。

    返回 True = 已发点击事件(等下次循环 fetch 看是否过盾)。
    返回 False = 连 iframe 都找不到(可能早期 shell 还没加载完)。
    """
    # 1. 直接 CDP 找 iframe(绕开 tab.ele 的 wait.doc_loaded 阻塞)
    found = _find_turnstile_iframe(tab)
    if not found:
        # 早期 shell: iframe src 还没注入,等 1s 让 JS 注入完再试一次
        logger.info("Turnstile iframe 还没注入,等 1s 重试...")
        time.sleep(1)
        found = _find_turnstile_iframe(tab)
    if not found:
        return False
    object_id, (x1, y1, x2, y2) = found
    box_w = x2 - x1
    box_h = y2 - y1
    # 复选框大致在 iframe (左 25%, 中线 50%) — 1337x 用的 Turnstile widget
    click_x = x1 + box_w * _TURNSTILE_BOX_FRACTION[0]
    click_y = y1 + box_h * _TURNSTILE_BOX_FRACTION[1]
    logger.info(
        f"找到 Turnstile iframe ({box_w:.0f}x{box_h:.0f} @ {x1:.0f},{y1:.0f}),"
        f"点击 ({click_x:.0f},{click_y:.0f})"
    )
    return _cdp_click(tab, click_x, click_y)

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


class PageTimer:
    """每页 phase 计时器。多次 start/stop 同名阶段会累加。

    用法:
        t = PageTimer()
        t.start("fetch")
        ... 做工作 ...
        t.stop("fetch")
        t.start("parse")
        ... 做工作 ...
        t.stop("parse")
        log = format_phase_log(page_num, total_pages, t, cf_attempts=2)
    """
    def __init__(self) -> None:
        self._acc: dict[str, float] = {}
        self._inflight: dict[str, float] = {}
        self._first_start: float | None = None

    def start(self, phase: str) -> None:
        now = time.perf_counter()
        self._inflight[phase] = now
        if self._first_start is None:
            self._first_start = now

    def stop(self, phase: str) -> None:
        now = time.perf_counter()
        started = self._inflight.pop(phase, None)
        if started is None:
            return  # 未 start 过,静默
        self._acc[phase] = self._acc.get(phase, 0.0) + (now - started)

    def to_dict(self) -> dict[str, float]:
        """返回所有阶段累计时长(秒)。"""
        return dict(self._acc)

    def total_elapsed(self) -> float:
        """从首次 start 到现在的总时长(含未 stop 的 in-flight 阶段)。"""
        if self._first_start is None:
            return 0.0
        return time.perf_counter() - self._first_start


class _FetchStats:
    """fetch_with_cf_bypass 的观测计数器(class 属性,主循环读 last_attempts)。"""
    last_attempts: int = 0
    # 子阶段耗时累加(tab.get / wait.load_start / tab.html / tab.ele)
    # 每次 fetch 内部循环会 + 一次,main() 在 phase log 里打印最后一次的累计
    fetch_subphases: dict[str, float] = {}


def format_phase_log(
    page_num: int,
    total_pages: int,
    timer: PageTimer,
    cf_attempts: int = 0,
    items_found: int = 0,
) -> str:
    """格式化单页 phase log。

    示例输出:
        [42/50] fetch=15.3s (cf=2x)[g=.5 l=14.0 h=.3 e=.5] parse=0.5s save=0.05s sleep=1.0s items=20 total=17.3s

    fetch 后面可选的 [g=... l=... h=... e=...] 是 sub-phase 拆分
    (get / load_start / html / ele),只看 fetch 总耗时看不出在等啥时看这个。
    """
    t = timer.to_dict()
    parts = [f"[{page_num}/{total_pages}]"]
    for phase, key in (("fetch", "fetch"), ("parse", "parse"),
                       ("save", "save"), ("sleep", "sleep")):
        v = t.get(phase)
        if v is None:
            continue
        suffix = "s"
        sub_breakdown = ""
        if phase == "fetch":
            cf_suffix = f" (cf={cf_attempts}x)" if cf_attempts > 1 else f" (cf={cf_attempts})"
            suffix = "s" + cf_suffix
            subs = _FetchStats.fetch_subphases or {}
            if subs:
                # 只输出非零的 sub-phase,避免空泡
                nonzero = {k: round(v, 2) for k, v in subs.items() if v > 0}
                if nonzero:
                    sub_breakdown = "[" + " ".join(
                        f"{ {'tab.get':'g','tab.html':'h','tab.ele':'e'}[k] }={v}"
                        for k, v in nonzero.items()
                    ) + "]"
        parts.append(f"{key}={v:.2f}{suffix}{sub_breakdown}")
    parts.append(f"items={items_found}")
    parts.append(f"total={timer.total_elapsed():.2f}s")
    return " ".join(parts)


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
      tab.ele(sel, timeout=N)    — 等同 Playwright wait_for_selector(sel, timeout=N*1000)
      tab.html                   — 等同 page.content()(会触发 wait.doc_loaded 隐式等待,
                                          谨慎使用;直接调 DOM.getOuterHTML 可绕过)

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


def _get_outer_html(tab) -> str:
    """直接通过 CDP 拿 outerHTML,跳过 DrissionPage 的 tab.html 隐式 wait.doc_loaded()
    (那个默认等 30s 第三方脚本,完全没必要)。
    若 _root_id 还没设(从未调过 tab.ele),先 DOM.getDocument + DOM.resolveNode 拿一次。

    注意:DrissionPage 的 _run_cdp 已 unwrap CDP "result" 字段,
    直接返回 result dict(参见 chromium_base.py:145 '_run_cdp(...).root.backendNodeId')。
    所以不要写 ["result"]["root"] — 直接 ["root"]。
    """
    if getattr(tab, "_root_id", None) is None:
        doc = tab._run_cdp("DOM.getDocument")
        bid = doc["root"]["backendNodeId"]
        tab._root_id = tab._run_cdp(
            "DOM.resolveNode", backendNodeId=bid
        )["object"]["objectId"]
    return tab._run_cdp(
        "DOM.getOuterHTML", objectId=tab._root_id
    )["outerHTML"]


def _selector_exists(tab, selector: str, timeout: float = 8.0) -> bool:
    """直接 CDP DOM.performSearch 轮询 selector 是否存在,跳过 tab.ele() 内部 wait.doc_loaded()
    隐式等待(chromium_base.py:454 _find_elements 一进来就 self.wait.doc_loaded(),
    默认等 page_load timeout,被第三方脚本拖 8s+)。

    返回 True = 在 timeout 内找到;False = 超时未找到。

    轮询间隔 50ms:SSR 页面 tab.get() 完成后 selector 几百 ms 内就在 DOM 里。
    """
    deadline = time.perf_counter() + timeout
    last_search_id: str | None = None
    while time.perf_counter() < deadline:
        try:
            r = tab._run_cdp(
                "DOM.performSearch", query=selector, includeUserAgentShadowDOM=True
            )
            if r and r.get("resultCount", 0) > 0:
                # 释放 search handle 避免 Chrome 端累积
                if "searchId" in r:
                    try:
                        tab._run_cdp("DOM.discardSearchResults", searchId=r["searchId"])
                    except Exception:
                        pass
                return True
            # 用完旧的 searchId(可能在某次失败的循环里残留)
            if last_search_id:
                try:
                    tab._run_cdp("DOM.discardSearchResults", searchId=last_search_id)
                except Exception:
                    pass
            last_search_id = r.get("searchId") if r else None
        except Exception:
            # CDP 暂时断连(页面正在加载中),等下次轮询
            pass
        time.sleep(0.05)
    return False


def fetch_with_cf_bypass(tab, url: str, target_selector: str, max_wait: int = 45) -> str:
    """访问 URL, 自动处理 Cloudflare 5秒盾, 轮询直到目标元素出现或超时。

    最优策略:数据加载完就不等。
      1. tab.get(url) 触发导航(不等 page load 事件,会被第三方脚本拖 30s)
      2. tab.ele(target_selector, timeout=8) 等**我们自己**要的元素,
         内部按 ~10ms 轮询 DOM,找到立即返回(不依赖 page load 事件)
      3. 直接调 DOM.getOuterHTML 拿 HTML(不走 tab.html 隐式 wait.doc_loaded)
      4. 检测 HTML 是否含 Cloudflare 挑战页特征 (5秒盾)
         - 是: 等 5s 后重 fetch (challenge JS 通常 5s 后自动 redirect)
      5. 出现 → 返回 html
      6. max_wait 秒后仍未达成 → 抛 TimeoutError

    ⚠️ HEADLESS 限制: Cloudflare bot 检测对 headless Chrome 极度激进, 会持续返回
    盾页 (即使每次 fetch 都等 5s), 此 helper 无法绕过。要爬 1337x 必须用
    visible Chrome 模式 (不传 --headless), 或预热 Chrome profile 注入 cf_clearance
    cookie 后再 headless。详见 crawl_1337x_by_key.py 顶部 docstring。

    Raises: TimeoutError (目标元素未出现) / 原始 DrissionPage 异常。

    Observability: 每轮循环自增 _FetchStats.last_attempts,main loop 可读出
    CF 重试次数(用于诊断"为啥这一页这么慢")。
    """
    from DrissionPage.errors import ElementNotFoundError, PageDisconnectedError

    _FetchStats.last_attempts = 0
    _FetchStats.fetch_subphases = {
        "tab.get": 0.0, "tab.html": 0.0, "tab.ele": 0.0,
    }
    deadline = time.time() + max_wait
    attempts = 0
    last_stage = "init"
    while time.time() < deadline:
        attempts += 1
        _FetchStats.last_attempts = attempts  # 暴露给 main loop 做诊断
        try:
            # ─── 最优策略:数据加载完就不等 ──────────────────────────
            # 不等 Page.loadEventFired(可被第三方脚本拖 30s),
            # 也不让 tab.html 触发 wait.doc_loaded()(同上)。
            # 只等我们真正需要的元素 tab.ele(target_selector),它内部按 ~10ms
            # 轮询 DOM,找到就立即返回。然后直接调 DOM.getOuterHTML 拿 HTML。
            # 对 1337x 这种服务端渲染:tab.ele 几百 ms 就找到,
            # 总 fetch 时间从 20s+ 降到 1-2s。

            t0 = time.perf_counter()
            tab.get(url)
            _FetchStats.fetch_subphases["tab.get"] += time.perf_counter() - t0

            # 先尝试等目标元素(只等我们要的,不等 page load event)
            # 用 _selector_exists 直接 CDP DOM.performSearch 轮询,跳过
            # tab.ele() 内部 self.wait.doc_loaded() 的隐式 8s+ 等。
            t0 = time.perf_counter()
            selector_found = _selector_exists(tab, target_selector, timeout=8)
            _FetchStats.fetch_subphases["tab.ele"] += time.perf_counter() - t0

            # 不管 selector 找没找到,都拿 HTML(走直接 CDP 调,无 wait.doc_loaded)
            t0 = time.perf_counter()
            html = _get_outer_html(tab)
            _FetchStats.fetch_subphases["tab.html"] += time.perf_counter() - t0

            # 检测 Cloudflare 盾(可能在 selector 找到之前出现,但 HTML 拿到后就能判)
            if any(m in html for m in _CF_CHALLENGE_MARKERS):
                kind = classify_cf_challenge(html)
                if kind == "turnstile":
                    last_stage = "cf_turnstile"
                    clicked = _try_click_turnstile_checkbox(tab)
                    if clicked:
                        logger.info(
                            f"  fetch 第 {attempts} 次: Turnstile 复选框已自动点击,等 4s 后重试"
                        )
                        time.sleep(4)
                    else:
                        logger.info(
                            f"  fetch 第 {attempts} 次: Turnstile iframe 找到但点不到,等 5s 后重试"
                        )
                        time.sleep(5)
                    continue
                elif kind == "hcaptcha":
                    last_stage = "cf_hcaptcha"
                    logger.error(
                        f"  fetch 第 {attempts} 次: CF 升级到 hCaptcha 图像题,自动无法过,"
                        f"立即失败(已写 checkpoint 可下次续爬)"
                    )
                    raise CFChallengeEscalated("hcaptcha image challenge")
                else:
                    # 普通 5秒盾 JS challenge: 5s 后自动 redirect
                    last_stage = "cf_shield"
                    logger.info(
                        f"  fetch 第 {attempts} 次: 遇 Cloudflare 5秒盾,等 5s 后重试"
                    )
                    time.sleep(5)
                    continue

            if not selector_found:
                # 不是 CF 盾但 selector 仍未出现 → 真没加载完 → 等 3s 重试
                last_stage = "no_target"
                logger.info(
                    f"  fetch 第 {attempts} 次: 未找到 {target_selector},等 3s 后重试"
                )
                time.sleep(3)
                continue

            last_stage = "ok"
            # 刚穿过 CF:把浏览器里新鲜的 .1337x.to cookie 写回 cf_cookies.json,
            # 下次启动直接注入省一遍盾
            maybe_refresh_cf_cookies(tab)
            return html
        except PageDisconnectedError as e:
            last_stage = "page_disconnected"
            logger.warning(f"  fetch 第 {attempts} 次: 页面断开 {e}, 等 2s 后重试")
            time.sleep(2)
            continue
        except CFChallengeEscalated:
            # hCaptcha 图像题等不可自动过的情形 — 直接抛出,不重试不睡
            raise
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
            timer1 = PageTimer()
            timer1.start("fetch")
            first_html = load_page_with_retry(page, search_url.format(page=1), 1)
            timer1.stop("fetch")
            cf_attempts_1 = _FetchStats.last_attempts
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
            timer1.start("parse")
            items1 = parse_listing(first_html, keyword)
            for it in items1:
                coll.update_one({"_id": it["_id"]}, {"$set": it}, upsert=True)
            timer1.stop("parse")
            timer1.start("save")
            save_checkpoint(keyword, 1, last_page)
            timer1.stop("save")
            logger.info(f"[1/{last_page}] 解析 {len(items1)} 条(首次页)")
            logger.info(format_phase_log(
                page_num=1, total_pages=last_page, timer=timer1,
                cf_attempts=cf_attempts_1, items_found=len(items1),
            ))
            done_page = 1
            start_page = 2

        # 翻 start_page..N
        for n in range(start_page, last_page + 1):
            url = search_url.format(page=n)
            # 每页 phase 计时,看时间花在哪
            timer = PageTimer()

            timer.start("fetch")
            html = load_page_with_retry(page, url, n)
            timer.stop("fetch")
            cf_attempts = _FetchStats.last_attempts  # fetch_with_cf_bypass 内循环次数

            items_found = 0
            if html is None:
                logger.warning(f"第 {n} 页重试耗尽，跳过(标记已处理,避免卡住进度)")
            else:
                timer.start("parse")
                # 直接调 parse_listing 取条数(避免 _process 里又重复解析)
                items = parse_listing(html, keyword)
                items_found = len(items)
                new_count = 0
                for it in items:
                    if coll.update_one({"_id": it["_id"]}, {"$set": it}, upsert=True).upserted_id:
                        new_count += 1
                timer.stop("parse")
                logger.info(f"[{n}/{last_page}] 解析 {items_found} 条，新写入 MongoDB {new_count} 条")

            # 无论成功/跳过都推进 checkpoint,保证重试单调前进,不会永远卡在同一页
            done_page = n
            timer.start("save")
            save_checkpoint(keyword, done_page, last_page)
            timer.stop("save")

            timer.start("sleep")
            time.sleep(PAGE_SLEEP)
            timer.stop("sleep")

            # Phase log: 一眼看出这一页慢在哪
            logger.info(format_phase_log(
                page_num=n, total_pages=last_page, timer=timer,
                cf_attempts=cf_attempts, items_found=items_found,
            ))

    total = coll.count_documents({"keyword": keyword})
    elapsed = time.time() - started_at
    logger.info(
        f"=== 完成 keyword={keyword} 耗时 {elapsed:.1f}s "
        f"库内 {DB_NAME}.{COLL_NAME} 中该 keyword 共 {total} 条 ==="
    )
    clear_checkpoint(keyword)  # 全部爬完,清掉 checkpoint
    return 0


def run_with_retry(keyword: str) -> int:
    """**真正共享**一个 Chrome 实例,最多尝试 MAX_ATTEMPTS 次 main(keyword)。

    关键设计:ChromiumPage 在循环外**只创建一次**,所有 attempt 复用同一 page 对象
    —— 节省每次重启 Chrome 的 ~12s 启动时间 + CF 盾重新挑战的开销,
    且同一 Chrome 内 cf_clearance cookie / 浏览器状态跨 attempt 保留,
    第 2 次起基本秒过 CF。Chrome 在外层 finally 统一 quit()。

    如果某次 attempt 把 page 弄到崩溃态(main() 抛异常),后续 attempt 会复用这个
    坏的 page;此时 fetch_with_cf_bypass 内部的 PageDisconnectedError 重试仍
    会失败,最终该 attempt 整体失败被记入 rc,下次 attempt 仍复用同一 page
    (其实已不可用,会一直失败直到 MAX_ATTEMPTS 用尽)。这是有意识的简化:
    真要 page 复活应靠 wrapper 重跑(新 subprocess → 新 Chrome)。

    返回 0=全部爬完,非 0=MAX_ATTEMPTS 次都失败。
    """
    env_val = os.environ.get(ENV_CONCURRENCY, "").strip()
    logger.info(f"=== 开始抓取 keyword={keyword!r} (最多 {MAX_ATTEMPTS} 次,共享 Chrome) ===")
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

    page = ChromiumPage(options)
    logger.info(f"Chrome 已启动 (address={options.address})—— {MAX_ATTEMPTS} 次 attempt 共享此实例")

    # 注:不再设 page_load timeout(原先砍到 5s 的逻辑已不需要)。
    # fetch_with_cf_bypass 已不调 wait.load_start / 不让 tab.html 触发
    # wait.doc_loaded,直接 tab.ele + DOM.getOuterHTML,完全绕过 page load
    # 事件(防止第三方脚本拖延 30s)。

    # Chrome 实例 LRU 注册:让 wrapper 的 ensure_chrome_capacity 能正确杀掉
    # 本进程拉起的 Chrome(只杀 parent wrapper PID 会让 Chrome 成孤儿继续跑)
    try:
        chrome_pid = page.process_id  # DrissionPage 暴露的浏览器进程 ID
        register_chrome_instance(pid=os.getpid(), chrome_pid=chrome_pid)
        logger.info(f"已注册到 Chrome LRU 注册表 (sub_pid={os.getpid()}, chrome_pid={chrome_pid})")
    except Exception as e:
        logger.warning(f"注册 Chrome 实例失败: {type(e).__name__}: {e}")

    # 反检测: 在每个新文档加载前注入 stealth JS,
    # 把 navigator.webdriver 改成 false 等(DrissionPage 默认是 true,CF 一眼 bot)
    try:
        page.add_init_js(STEALTH_INIT_JS)
        logger.info("已注入 stealth init JS (navigator.webdriver=false)")
    except Exception as e:
        logger.warning(f"注入 stealth init JS 失败: {type(e).__name__}: {e}")

    # CF cookie 预热:用户在 data/cf_cookies.json 留了 cf_clearance 就注入,
    # 让 CF 不再弹复选框/5秒盾(若文件不存在或 cookie 已过期则静默跳过)
    inject_cf_cookies(page)

    rc = 1
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            logger.info(f"[尝试 {attempt}/{MAX_ATTEMPTS}] {keyword}")
            try:
                rc = main(keyword, page, coll, started_at)
                if rc == 0:
                    return 0
            except Exception as e:
                logger.error(f"[尝试 {attempt}] 异常: {type(e).__name__}: {e}")
                rc = 99

            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    f"[尝试 {attempt}] 失败 rc={rc},{RETRY_BACKOFF}s 后从中断页续爬"
                )
                time.sleep(RETRY_BACKOFF)
    finally:
        # 统一关闭 Chrome(只关一次,不管几次 attempt)
        try:
            page.quit()
            logger.info("Chrome 已关闭")
        except Exception as e:
            logger.warning(f"关闭 Chrome 异常: {type(e).__name__}: {e}")
        # 注销 LRU 注册表条目
        try:
            unregister_chrome_instance(os.getpid())
        except Exception as e:
            logger.debug(f"注销 Chrome 实例失败: {e}")

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