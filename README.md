# 1337x 爬虫系统

基于 DrissionPage + MongoDB 的 1337x 种子站爬虫,自动处理 Cloudflare 盾、断点续爬、Chrome 实例 LRU。

## 项目概述

1337x.to 是一个 BT 种子索引站,搜索结果按页组织,详情页含 magnet/torrent 链接。本项目:

- **列表爬取** — 关键词搜索,自动翻页,落 MongoDB
- **详情爬取** — 从列表拿 magnet/hash/IMDB 等
- **Cloudflare 绕过** — cookie 预热 + stealth + Turnstile 自动点击
- **弹性** — 断点续爬 / 失败重试 / Chrome LRU 上限 / 子进程超时

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│  crawl_1337x_by_keys.py (wrapper, 多 keyword 并发)              │
│  - 读 data/keys.txt, 过滤已 done 的                             │
│  - N 并发 spawn 子进程 (ThreadPoolExecutor)                     │
│  - 共享 Chrome LRU (data/chrome_instances/)                    │
│  - WORKER_TIMEOUT=600s 兜底                                     │
└──────────────┬──────────────────────────────────────────────────┘
               │ spawn 1 个子进程 per keyword
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  crawl_1337x_by_key.py (单 keyword 翻页)                       │
│  - run_with_retry() 共享 Chrome 1 实例                          │
│  - 4 次 attempt, fetch_with_cf_bypass 内部轮询                  │
│  - 失败重试: CF 盾 / 5秒盾 / Turnstile 自动点                  │
│  - 断点续爬: data/checkpoints/{key}-{md5}.json                  │
│  - CF cookie 注入: data/cf_cookies.json (去重后)                │
└──────────────┬──────────────────────────────────────────────────┘
               │ parse 每一页 table.table-list tbody tr
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  MongoDB bt_13337x_spider_db.bt_info_list                       │
│  - _id: md5(detail_url)                                         │
│  - 字段: name / detail_url / seeds / leechers / size / ...       │
└─────────────────────────────────────────────────────────────────┘
```

## 安装

```bash
# Python 3.11
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt      # Linux/Mac

# 启动 Chrome 一次让它自更新
.venv/Scripts/python -c "from DrissionPage import ChromiumPage; ChromiumPage().quit()"
```

**依赖**(见 `requirements.txt`):DrissionPage 4.1.1.4、pymongo、psutil、filelock、lxml 等。

## 使用

### 1. 准备关键词

```bash
# data/keys.txt 每行一个关键词
cat data/keys.txt
# 2014
# 005
# 063
# ...
```

### 2. (可选) 注入 CF cookie

```bash
# 首次跑会频繁弹 CF。建议先手动过:
#   1. 浏览器打开 1337x.to,手动过 CF
#   2. DevTools → Application → Cookies → 选 1337x.to
#   3. 复制 cf_clearance / __cf_bm 的 Name/Value/Domain/Path/Expires
#   4. 填到 data/cf_cookies.json(参考 .example 模板)
```

### 3. 跑 wrapper(多 keyword 并发)

```bash
# 默认串行(concurrency=1)
.venv/Scripts/python crawl_1337x_by_keys.py

# 4 并发
.venv/Scripts/python crawl_1337x_by_keys.py -c 4

# 自定义并发
CRAWL_1337X_CONCURRENCY=8 .venv/Scripts/python crawl_1337x_by_keys.py

# Chrome 实例上限(默认跟 -c)
MAX_CHROME_INSTANCES=2 .venv/Scripts/python crawl_1337x_by_keys.py -c 4
```

### 4. 单 keyword(调试用)

```bash
.venv/Scripts/python crawl_1337x_by_key.py 2014
```

### 5. 详情页爬取

```bash
# 默认从 bt_info_list 拿 detail_status=pending 的逐条抓
.venv/Scripts/python crawl_detail_1337x.py

# 参数
.venv/Scripts/python crawl_detail_1337x.py -c 4 -b 200 --retry-failed
```

## 数据模型

### `bt_info_list` (列表)
```json
{
  "_id": "md5(detail_url)",
  "name": "Movie.Name.2024.1080p.BluRay.x264",
  "detail_url": "/torrent/12345/Movie-Name/",
  "seeds": 1500, "leechers": 200,
  "size": "1.5 GB",
  "uploader": "userX",
  "list_time": "2024-10-21 00:00:00",
  "keyword": "2014",
  "c_time": "2024-10-21 12:34:56",
  "detail_status": "pending"  // pending → processing → done / failed
}
```

### `bt_info_detail` (详情)
```json
{
  "_id": "md5(detail_url)",
  "detail_url": "...",
  "name": "...", "title": "...",
  "category": "Movies", "type": "BluRay", "language": "English",
  "total_size": "1.5 GB",
  "uploaded_by": "userX",
  "downloads": 1500,
  "date_uploaded": "2024-09-15 00:00:00",
  "last_checked": "2024-10-20 00:00:00",
  "seeders": 1500, "leechers": 200,
  "resource_links": {
    "magnet": "magnet:?xt=urn:btih:...",
    "itorrents": "https://itorrents.org/...",
    "torrage": "https://torrage.info/...",
    "btcache": "https://btcache.me/...",
    "stream": "https://..."
  },
  "info_hash": "ABCDEF1234567890...",
  "imdb_url": "https://www.imdb.com/title/tt1234567/",
  "imdb_id": "tt1234567",
  "description": "...",
  "cover_url": "https://...",
  "tags": ["Action", "Drama"],
  "c_time": "2024-10-21 12:34:56"
}
```

## Cloudflare 处理

CF 是这个项目最难的部分,3 层防御:

### 1. Cookie 预热(主防线)
- 启动时从 `data/cf_cookies.json` 读 cf_clearance / __cf_bm
- 通过 raw CDP `Network.setCookie` 一条条注入(避免同名覆盖)
- **去重**:文件多条同名 cookie 时只留 expires 最大的
- 通过 CF 后 `maybe_refresh_cf_cookies` 自动写回

### 2. Stealth(辅助)
- 启动前注入 init JS,覆盖 `navigator.webdriver = false`
- 补 `window.chrome` 对象、修复 `permissions.query` 等

### 3. Turnstile 自动过(兜底)
CF 偶尔仍会弹 Turnstile。`_try_click_turnstile_checkbox`:
- 找 iframe(直接 CDP `DOM.performSearch`,绕开 `tab.ele` 内部 `wait.doc_loaded` 阻塞)
- 拿 iframe bounding box
- **多位置尝试**:`(0.08, 0.5)` 主 / `(0.15, 0.5)` / `(0.5, 0.5)` 备
- 真实鼠标轨迹:从远处 -200px 起步,12 步平滑 mousemove
- mousedown / mouseup 用 bitmask `buttons=1/0`(CDP spec 整数,不是字符串)
- 点击后 1.5s 验证 Turnstile iframe 是否消失(确认命中)

## 数据流状态文件

| 文件 | 谁写 | 谁读 | 内容 |
|---|---|---|---|
| `data/keys.txt` | 人工 | wrapper | 待抓 keyword 列表 |
| `data/keys-done.txt` | wrapper(append 锁) | wrapper(load) | 已成功 key |
| `data/checkpoints/{key}-{md5}.json` | key 爬虫(原子写) | key 爬虫(load) | `{done_page, last_page, ...}` |
| `data/cf_cookies.json` | key 爬虫(自动) | key 爬虫(load) | cf_clearance / __cf_bm(去重) |
| `data/chrome_instances/{pid}.json` | wrapper(自动) | wrapper(LRU) | 当前 Chrome 实例 PID+端口 |
| `data/html/{md5}.html` | 详情爬虫 | 详情爬虫 | 详情页 HTML 缓存 |

## 运行日志:phase timing

每页会输出一行 phase 日志,清晰看到时间花在哪:

```
[42/50] fetch=12.83s (cf=1) [g=4.49 h=0.33 e=8.01] parse=0.13s save=0.00s sleep=1.00s items=20 total=13.96s
```

| 字段 | 含义 |
|---|---|
| `g` | `tab.get(url)` HTTP 请求耗时 |
| `h` | DOM outerHTML 序列化(直接 CDP,绕开 `tab.html` 隐式 `wait.doc_loaded`) |
| `e` | 等 selector(`_selector_exists`,直接 `DOM.performSearch` 轮询) |
| `cf=N` | CF 盾重试次数 |

## 测试

```bash
# 单跑(7 套)
.venv/Scripts/python test/parsing.py
.venv/Scripts/python test/cf.py
.venv/Scripts/python test/chrome.py
.venv/Scripts/python test/modules.py
.venv/Scripts/python test/page_timing.py
.venv/Scripts/python test/fetch_subphase.py
.venv/Scripts/python test/turnstile_click.py

# pytest 跑全部(包含 5 个从旧 tests/ 移过来的)
.venv/Scripts/python -m pytest test/ -v
```

**测试统计**:
- 7 套自写脚本测试(plain `python test/xxx.py`)
- 5 套 pytest 测试(extract_imdb_id / html_cache_path / now_str / parse_detail / parse_relative_time)
- 共 ~200 条断言,覆盖 checkpoint / CF cookie 去重 / Chrome LRU / fetch sub-phase / Turnstile click / 模块签名 / 详情页解析

## 文件结构

```
bt-1337x-v3/
├── README.md                         # 本文件
├── CLAUDE.md                         # 项目 Claude 指令
├── requirements.txt                  # Python 依赖
│
├── crawl_1337x_by_key.py            # 单 keyword 翻页(主入口)
├── crawl_1337x_by_keys.py           # 多 keyword wrapper + Chrome LRU
├── crawl_detail_1337x.py            # 详情页爬取
├── migrate_1337x.py                 # 历史迁移脚本
├── migrate_detail_status.py         # 详情 status 迁移
├── tes_playwright_attach.py        # (废弃) Playwright 探针
│
├── data/
│   ├── keys.txt                      # 待抓关键词
│   ├── keys-done.txt                 # 已完成
│   ├── cf_cookies.json.example      # CF cookie 模板
│   ├── checkpoints/                 # 断点续爬 JSON
│   ├── chrome_instances/             # LRU 注册表
│   ├── html/                         # 详情页 HTML 缓存 + _progress.log
│   └── parse_cache/                  # 解析缓存
│
└── test/                             # 14 个测试 + conftest + 共享 _path
    ├── _path.py                      # sys.path 共享设置
    ├── conftest.py                   # pytest fixture 辅助
    ├── fixtures/                     # 详情页 HTML fixture
    ├── parsing.py / cf.py / chrome.py / modules.py
    ├── page_timing.py / fetch_subphase.py / turnstile_click.py
    └── extract_imdb_id.py / html_cache_path.py / now_str.py
    └── parse_detail.py / parse_relative_time.py / verify_parse_vs_html.py
```

## 已知限制

1. **Turnstile 自动过在 CF ML 拒绝 CDP 事件时失效** — 大部分页面 cookie 预热后不弹盾;若偶发弹盾,需手动在浏览器过 CF 后导出 cookies
2. **CF cookie 15-30 天过期** — 自动检测/刷新,但用户需在浏览器手动过首次
3. **`fetch_with_cf_bypass` 单页 45s 超时** — 50 页 keyword 完整跑约 1-3 分钟(无盾时)
4. **详情页爬取按列表分批,默认 100/批** — 详情页多时跑得久

## 已知开发约束(项目根 CLAUDE.md)

- 单页之间 `PAGE_SLEEP = 8.0`(礼貌爬取 + 降低被 CF 风险评分)
- 无 lock 文件,`uv pip freeze > requirements.txt` 是当前约定的快照方式
- 新增网盘厂商不适用(本项目只针对 1337x 单一来源)
- Chrome 默认 auto_port 模式,每个 wrapper subprocess 启独立 Chrome
- 见 CLAUDE.md 完整约定

## 跑出错的常见原因

| 现象 | 排查 |
|---|---|
| `已注入 0 条 CF cookie` | `data/cf_cookies.json` 不存在或全失效;需手动过 CF 导出 |
| `Turnstile iframe 找到但点不到,等 5s 后重试` | 3 次都失败说明 CF 拒 CDP 事件;靠 cookie 预热绕 |
| `已自动刷新 data\cf_cookies.json (2 条 cookie, 下次启动直接注入省一遍 CF)` | 自反馈闭环工作 ✓ |
| `Chrome 实例数 N >= cap N, LRU 杀掉最老...` | LRU 触发,杀掉最老 Chrome 给新 keyword 腾位置 ✓ |
| `[X/Y] fetch=N1s (cf=N2x) [g=... h=... e=...]` | phase 日志,看时间花在哪 |

## License

Private.
