# Todo: 1337x 多关键词批量抓取(支持并发)

> Status legend: ⬜ pending · 🟡 in progress · ✅ completed · ❌ blocked · 🚫 cancelled

## Phase 1: 单 key 脚本重构(子脚本)

- ⬜ **Task 1** — 重命名 `crawl_1337x.py` → `crawl_1337x_by_key.py` 并 KEYWORD/CDP_URL 参数化
  - ⬜ 删除模块级 `KEYWORD = "House"` 常量(`CDP_URL` 可保留作默认值)
  - ⬜ `main()` 签名改为 `main(keyword: str, cdp_url: str = CDP_URL)`
  - ⬜ `main()` 内部用 `keyword` 构造 `SEARCH_URL` 与 MongoDB `keyword` 字段
  - ⬜ `main()` 内部用传入 `cdp_url` 调 `connect_over_cdp`
  - ⬜ `__main__` 用 argparse:位置参数 `keyword` 必填 + `--cdp-url` 可选
  - ⬜ 验证 `python crawl_1337x_by_key.py House` 与原行为一致
  - ⬜ 验证 `python crawl_1337x_by_key.py` 无参报错退出
  - ⬜ 验证 `--help` 与 `--cdp-url` 参数生效

- ⬜ **Task 2** — 创建 `data/keys.txt` 与 `data/keys-done.txt`
  - ⬜ `data/keys.txt` 含至少 2 个真实 key
  - ⬜ `data/keys-done.txt` 空白存在
  - ⬜ 均为 UTF-8 LF(无 BOM)

## Phase 2: 批处理 wrapper(并发)

- ⬜ **Task 3** — 新建 `crawl_1337x_by_keys.py`(支持 `--concurrency`)
  - ⬜ argparse: `--concurrency` / `-c`(int, [1, 16], **默认 1**)+ `--help` 友好
  - ⬜ 读 `data/keys.txt`:trim、空行跳过、`#` 注释跳过、set 去重
  - ⬜ 读 `data/keys-done.txt` 建立 done 集合,过滤已 done
  - ⬜ 空 keys / 全 done → 退出码 0 + 日志提示
  - ⬜ **`-c 1`(默认)**:不启 Chrome,沿用现有 9222
  - ⬜ **`-c N>1`**:自动启 N 个 Chrome 在 9222..9221+N,等 CDP ready
  - ⬜ Chrome 启动参数:独立 `--user-data-dir=~/.chrome_debug_profile/pool_{port}` + `--headless=new`
  - ⬜ 端口冲突检测:启动前探测,占用则报错退出
  - ⬜ CDP ready 检测:`urllib` 探测 `/json/version`,超时 30s
  - ⬜ `ThreadPoolExecutor(max_workers=concurrency)` 跑 `subprocess.run([python, crawl_1337x_by_key.py, key, --cdp-url, port_url])`
  - ⬜ Worker 端口分配:round-robin
  - ⬜ 成功 → `threading.Lock` + `flush` append 到 `data/keys-done.txt`
  - ⬜ 失败 → catch stderr(最后 10 行)+ 记 traceback + 加入失败列表 + **不**写 done
  - ⬜ `atexit` + signal handler 注册 Chrome terminate
  - ⬜ 退出码:全部成功 0,有失败 1
  - ⬜ 汇总日志:成功数 / 失败数 / 跳过数 / 总耗时
  - ⬜ 验证:空 keys、`-c 1` 单成功、`-c 1` 失败不写 done、二次幂等、`-c 4` 4 个并发、`#` 注释跳过、`--concurrency 0/17` 报错

## Phase 3: 端到端验证

- ⬜ **Checkpoint** — 全链路验证
  - ⬜ 首次 `python crawl_1337x_by_keys.py -c 1` 跑完所有 keys
  - ⬜ MongoDB `db.bt_info_list.distinct("keyword")` 含 keys.txt 里所有 key
  - ⬜ 二次跑 wrapper 日志 "no new keys to process" 退出码 0
  - ⬜ `data/keys-done.txt` 行数 = 首次成功数
  - ⬜ 二次跑后无 Chrome 进程残留
  - ⬜ `-c 4` 跑 4 个并发 keys 验证 Chrome 池正常启停

## Backlog (本期不做,记录备查)

- ⬜ wrapper `--dry-run` 选项(只打印不爬)
- ⬜ wrapper `--key` CLI 单 key 覆盖 keys.txt
- ⬜ keys.txt 支持 1337x 分类/排序/过滤参数
- ⬜ `--share-profile` 让多个 Chrome 共享登录态(user-data-dir)
- ⬜ 进度条(tqdm)展示