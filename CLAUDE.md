# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

基于 Selenium + Chrome 的网盘资源爬虫。通过夸克搜索结果抓取各类网盘分享链接（阿里云盘、夸克、百度、迅雷、189、118、蓝奏、115），解析后写入 MongoDB。配套定时任务按每天 4 个时段运行。

## 运行

无打包、无构建系统、无测试、无 linter。直接以脚本方式执行：

- `python main_01.py` — 抓取主入口。`__main__` 已写死调用 5 个关键词搜索（aliyundrive / alipan / pan.quark / pan.baidu / pan.xunlei / 123pan）。
- `python schedule_job.py` — 启动 7:00 / 12:00 / 16:00 / 21:00 的定时调度器（依赖 `schedule` 库）。
- `python parse_index.py` — 离线解析 `data/index.html`（注意：脚本里写死路径 `D:/workspace/project/awesome-spider-python/pan-spider-quark-search/data/index.html`，需要修改后再跑）。
- `test_02.py` — 空文件。

依赖见 `requirements.txt`（`uv pip install -r requirements.txt`）。ChromeDriver 路径硬编码在 `main_01.py:394`。

## 架构与数据流

```
main_01.search_by_keyword(keyword)
    │
    ├─ start_chrome_with_debugging()        # 启动 Chrome 调试模式 (port 9222)
    ├─ scroll_page_slowly()                  # 缓慢滚动加载搜索结果
    ├─ parse_index_html(html)                # BeautifulSoup 解析搜索结果列表
    │     → 每条结果: 标题/URL/描述/来源/日期
    │
    ├─ 对每条结果:
    │     ├─ RegexUtils.parse_txt_multi(desc)        # 从描述中提网盘链接
    │     └─ spider_inner_html(driver, result_url)   # 跳进原文再抓一次
    │           ├─ data/processed_urls.txt 去重
    │           └─ RegexUtils.parse_html_multi(html) # 从原文 HTML 中提
    │
    └─ save_url_objects(url_objects, client="quark_search")
          └─ 写入 MongoDB: pan_spider_db.ResToDoItem
```

`RegexUtils.py` 是核心工具集，每个网盘厂商独立一个解析类（`AliYunPanRegexUtils` / `QuarkRegexUtils` / `DupanRegexUtils` / `XunLeiPanRegexUtils` / `LanZouPanRegexUtils` / `Pan118RegexUtils` / `Pan189RegexUtils` / `Pan115RegexUtils`），每个类提供 `has_valid_url` / `parse_txt_url_multi` / `parse_txt_url_and_pwd_multi`（参数形式 `?pwd=`）/ `parse_txt_url_and_pwd_split_multi`（"密码：" 分隔形式）/ `parse_txt_multi`（综合，去重并合并密码）。

`RegexUtils.parse_txt_multi` 是统一入口，依次调用 8 个厂商的 `parse_txt_multi` 并合并结果。`RegexUtils.parse_html_multi` 在此基础上再解析 HTML `<title>` / `<meta keywords>` / `<meta description>` 填到 `orgT` / `orgK` / `orgD`。

`RegexUtils.get_pan_url_by_pname(pname, surl)` 是反向操作：根据 `pname` 拼接完整 URL。新增厂商必须同时扩展这里。

## 关键约定

- **网盘厂商代码 (`pname`)**：严格使用以下常量 — `aliyp` / `quark` / `dupan` / `xunlei` / `189` / `118` / `lanzou` / `115`。出现在 Regex 工具类、`get_share_valid_status` 分支判断、`save_url_objects` 主流程中，**任何新增或修改必须三处一致**。
- **资源指纹**：`save_url_objects` 用 `md5("123" + url)` 作为 `_id`，`md5(url)` 作为 `md5` 字段，加前缀 `"123"` 是历史遗留（避免和别的集合冲突），不要去掉。
- **去重**：跨进程去重走 `data/processed_urls.txt`（每行一个 URL），`spider_inner_html` 进入页面前先查再追加。
- **忽略 `pwd == "dany"`**：在 `save_url_objects` 里 hardcode 跳过，不要去掉。
- **HTML 解析的搜索结果选择器**：`section.sc.sc_structure_template_normal`（夸克 DOM 结构），改版会失效。
- **搜索 URL 时间过滤 (`tbs`)**：`d`=天 / `w`=周 / `m`=月 / `y`=年，对应 `{"time":"4" / "3" / "2" / "1"}`。
- **MongoDB**：`mongodb://localhost:27017/` → DB `pan_spider_db` → collection `ResToDoItem`。`ResToDoItem` 字段约定：`_id` / `md5` / `url` / `pwd` / `pName` / `client` / `cTime` / `orgUrl` / `orgT` / `orgK` / `orgD` / `remark`。
- **Chrome 调试端口**：9222，用户数据目录 `~/.chrome_debug_profile`，Chrome 路径硬编码 `C:/Program Files/Google/Chrome/Application/chrome.exe`，ChromeDriver 路径硬编码 `H:\services\chromedriver\132.0.6834.83\chromedriver.exe`。本机配置差异需直接改 `main_01.py`。

## 已知 TODO / 占位实现

- `get_share_valid_status`（`main_01.py:245`）所有分支都直接返回 `True`，未接真实校验 API。注释里标了 `await AliYunPanApi.get_share_valid_status(...)` 等接口形态但未实现。
- `data/parsed_results.csv` 只有表头（`序号,标题,URL,描述,来源,日期`），主流程没回写 CSV，只有 `parse_index.py` 的 `save_to_csv` 会写但写的是 `D:/workspace/project/...` 硬编码路径。

## 修改时的注意

- 修改 `RegexUtils.py` 的正则：要跑 `parse_index.py`（改路径后）对照 `data/index.html` 看实际匹配数变化。
- 新增网盘厂商：需要 (1) 新建 `XxxRegexUtils` 类并提供同样的 5 个方法（has_valid_url / parse_txt_url_multi / parse_txt_url_and_pwd_multi / parse_txt_url_and_pwd_split_multi / parse_txt_multi）；(2) 在 `RegexUtils.parse_txt_multi` 入口加一行调用；(3) 在 `RegexUtils.get_pan_url_by_pname` 加分支；(4) 在 `main_01.get_share_valid_status` 加分支；(5) 在 `main_01.save_url_objects` 的 `pwd == "dany"` 守卫之前不需要改。
- 项目无锁文件（`uv pip freeze > requirements.txt` 是当前约定的快照方式，不区分直接依赖与传递依赖）；依赖变更请同步刷新 `requirements.txt`。