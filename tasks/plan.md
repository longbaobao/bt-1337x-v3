# Implementation Plan: 1337x 多关键词批量抓取(支持并发)

## Overview

把当前单关键词(`House`)硬编码的 `crawl_1337x.py` 重构为支持命令行传 key 与可配置 CDP URL 的 `crawl_1337x_by_key.py`,并新建 `crawl_1337x_by_keys.py` 作为批处理 wrapper:从 `data/keys.txt` 读取多个 key、**按 `--concurrency` 配置多进程并发处理**,处理完的 key 追加写入 `data/keys-done.txt`(跨进程幂等 + 崩溃可恢复)。

## 现有接口摸底

- `crawl_1337x.py:28` — `KEYWORD = "House"` 是模块级常量
- `crawl_1337x.py:154` — `main()` 内部直接引用模块级 `KEYWORD`,无参数
- `crawl_1337x.py:130` — 写入 MongoDB 时把 `keyword: KEYWORD` 作为固定字段
- `crawl_1337x.py:26` — `CDP_URL = "http://127.0.0.1:9222"` 也是模块级常量
- `__main__` 入口没读 sys.argv
- `data/` 目录目前只有 `html/`,`keys.txt` / `keys-done.txt` 都不存在
- Chrome 路径硬编码 `C:/Program Files/Google/Chrome/Application/chrome.exe`(CLAUDE.md)

## 架构决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| KEYWORD 传参方式 | 重构 `main(keyword: str, cdp_url: str = CDP_URL)` + argparse 入口 | 不引入第三方 CLI 库,保持脚本风格;argparse 只用内置 |
| CDP_URL 传参 | 子脚本新增 `--cdp-url` argparse 参数(默认 9222) | 让 wrapper 能把不同 worker 分配到不同 CDP 端口 |
| wrapper 调用方式 | **`subprocess` 跑子脚本,不用 import** | Playwright sync API 多线程不安全(共享事件循环),多进程隔离是唯一真并发方案 |
| 并发执行模型 | **`ThreadPoolExecutor(max_workers=N)` 跑 `subprocess.Popen`** | wrapper 主进程只跑线程+subprocess,不开 Playwright;线程池调度多个隔离 worker 进程 |
| 并发粒度 | **每个 key 一个 worker subprocess**(不切分单 key 的翻页) | 用户语义"多关键词批量"对应 key 级并发;单 key 内部翻页保持串行(Playwright sync 不支持并发翻页) |
| **`--concurrency` 默认值** | **1**(纯串行,完全向后兼容) | 用户偏好"默认只串行";并发按需启用(`-c 4` / `-c 8` 等),避免无意识抢占资源 |
| Chrome 端口分配 | `--concurrency N>1` 时,wrapper 自动启 N 个 Chrome 在 9222..9221+N 端口;**`N=1` 不启**(用现有 9222) | 既有 9222 Chrome 单端口时向后兼容零侵入;并发场景自动管理生命周期 |
| Chrome 启动参数 | `--headless=new` + 独立 `--user-data-dir=~/.chrome_debug_profile/pool_{port}` | 避免多实例共享 user data 冲突,headless 节省资源 |
| done.txt 并发安全 | `threading.Lock`(只在 wrapper 主进程内) + `flush()` | wrapper 进程是唯一写者,跨 worker 不需要 OS 级文件锁 |
| 已 done key 处理 | 启动时读 `keys-done.txt` 建立集合,过滤掉 | 避免重复爬、幂等可重入 |
| 单 key 失败语义 | subprocess 非零退出 → 记日志 → **不**写入 done → 计入失败列表 | 留给下次重试可捡起,符合"处理完才写入"语义 |
| `keys.txt` 格式 | 一行一个 key,空行与 `#` 开头跳过,trim 空白 | 简单、人类可编辑;无 JSON/YAML 解析负担 |
| 重复 key | `set()` 去重 | 防止 keys.txt 误重复行导致重复爬 |
| 端口冲突保护 | wrapper 启动 Chrome 前 `try connect` 检测端口空闲;占用则报错退出 | 避免静默挂掉 |

## Task List

### Phase 1: 单 key 脚本重构(子脚本)

#### Task 1: 重命名 + KEYWORD 参数化 + --cdp-url 支持

**Description:** 把 `crawl_1337x.py` 改名为 `crawl_1337x_by_key.py`,删除模块级 `KEYWORD` 常量,`main()` 改为接受 `keyword: str` 与可选 `cdp_url: str` 参数,内部用 `keyword` 构造 `SEARCH_URL` 与 MongoDB `keyword` 字段,用 `cdp_url` 调 `connect_over_cdp`。`__main__` 入口用 argparse 解析 `--cdp-url`,位置参数 `keyword` 必须有。

**Acceptance criteria:**
- [ ] 文件已重命名(Windows `shutil.move` 或等价)
- [ ] 模块顶部不再有 `KEYWORD = "..."` 常量;`CDP_URL` 可保留作为 `--cdp-url` 的默认值
- [ ] `main(keyword: str, cdp_url: str = CDP_URL)` 签名接受外部传参
- [ ] 搜索 URL、MongoDB `keyword` 字段用传入值,不再硬编码 `"House"`
- [ ] `__main__` 用 argparse,无 keyword 时退出码非 0 + 友好错误
- [ ] 不引入新依赖(只用 `argparse` + `logging` 内置)

**Verification:**
- [ ] `python -c "import ast; ast.parse(open('crawl_1337x_by_key.py', encoding='utf-8').read())"`
- [ ] import 检查 `main.__code__.co_varnames[:3]` 含 `keyword`, `cdp_url`
- [ ] `--help` 输出正确,含 `keyword` 位置参数与 `--cdp-url` 选项
- [ ] 无参执行 → 退出码非 0
- [ ] 跑 `python crawl_1337x_by_key.py House` 行为与原 `crawl_1337x.py` 一致(MongoDB `keyword="House"` 记录数 ≥ 之前状态)
- [ ] 跑 `python crawl_1337x_by_key.py House --cdp-url http://127.0.0.1:9222` 与默认行为等价

**Dependencies:** None

**Files likely touched:**
- `crawl_1337x.py` → 删除
- `crawl_1337x_by_key.py` → 新建(原内容 + 参数化改造 + argparse 入口)

**Estimated scope:** S (1-2 文件,纯重构)

---

#### Task 2: 创建 keys.txt 与 keys-done.txt

**Description:** 在 `data/` 下创建输入清单 `keys.txt`(示例填 2-3 个真实关键词)与空白的 `keys-done.txt`(0 字节,文件存在即可)。

**Acceptance criteria:**
- [ ] `data/keys.txt` 存在且至少含 2 个 key(行)
- [ ] `data/keys-done.txt` 存在且 0 字节
- [ ] UTF-8 无 BOM,LF 换行

**Verification:**
- [ ] `wc -l data/keys.txt` ≥ 2
- [ ] `wc -c data/keys-done.txt` == 0
- [ ] `python -c "open('data/keys.txt','rb').read()[:3] != b'\xef\xbb\xbf'"` → True(无 BOM)

**Dependencies:** None

**Files likely touched:**
- `data/keys.txt` → 新建
- `data/keys-done.txt` → 新建

**Estimated scope:** XS (2 文件)

---

### Phase 2: 批处理 wrapper(并发)

#### Task 3: 新建 crawl_1337x_by_keys.py — 支持并发配置

**Description:** 新建 `crawl_1337x_by_keys.py`,职责:
1. argparse 解析 `--concurrency` / `-c`(默认 4,范围 [1, 16])
2. 读 `data/keys.txt` → trim → 跳过空行与 `#` 注释 → set 去重
3. 读 `data/keys-done.txt` 建立 done 集合 → 过滤掉已 done
4. **当 `concurrency > 1`**:自动启 `concurrency` 个 Chrome 实例在端口 9222..9221+N(独立 user-data-dir,`--headless=new`);**当 `concurrency == 1`**:不启 Chrome,沿用现有 9222
5. 用 `ThreadPoolExecutor(max_workers=concurrency)` 跑 worker,每个 worker `subprocess.run(['python', 'crawl_1337x_by_key.py', key, '--cdp-url', f'http://127.0.0.1:{port}'])`(端口轮询分配)
6. 每个 worker 返回后:成功 → `threading.Lock` 保护下 append key 到 `data/keys-done.txt` + `flush`;失败 → 记日志 traceback,加入失败列表,**不**写 done
7. 退出前:关闭 wrapper 自动启动的所有 Chrome 进程(`proc.terminate()` + 等待 5s + `proc.kill()`)
8. 汇总日志:成功数 / 失败数 / 跳过数 / 总耗时

**Acceptance criteria:**
- [ ] argparse 接受 `--concurrency` / `-c`(int, [1, 16], **默认 1**)
- [ ] 读 `data/keys.txt` 规则:trim、空行跳过、`#` 注释跳过、set 去重
- [ ] 读 `data/keys-done.txt` 建立 done 集合,过滤已 done
- [ ] 空 keys / 全部已 done → 日志提示 + 退出码 0
- [ ] `concurrency == 1`:不启 Chrome,直接 subprocess 走 9222
- [ ] `concurrency > 1`:wrapper 自动启 N 个 Chrome 在 9222..9221+N,等 CDP ready 后再分配 worker
- [ ] 端口冲突检测:启动前 `socket.connect(('127.0.0.1', port))` 探测,占用则报错退出(不让静默挂)
- [ ] Chrome 启动参数:独立 `--user-data-dir=~/.chrome_debug_profile/pool_{port}` + `--headless=new`
- [ ] CDP ready 检测:`urllib.request.urlopen('http://127.0.0.1:{port}/json/version', timeout=1)`,超时 30s
- [ ] Worker 端口分配:轮询(round-robin),每个 key 取一个 port
- [ ] 每个 key 跑完后 append `data/keys-done.txt`(`threading.Lock` + `flush`)
- [ ] 单 key 失败:catch stderr,记录日志(traceback),加入 `failed` 列表,**不**写入 done
- [ ] 所有 worker 完成 / 异常后:terminate 所有 wrapper 启动的 Chrome(`atexit` 注册)
- [ ] 退出码:全部成功 0,有失败 1(便于 CI/调度器判断)
- [ ] 不引入新第三方依赖(只用 `subprocess` / `threading` / `urllib` / `argparse` / `pathlib` 内置)

**Verification:**
- [ ] `--help` 输出正确
- [ ] 空 keys.txt → 退出码 0,日志 "no new keys to process"
- [ ] `-c 1` 单 key 成功 → done.txt 增 1 行;MongoDB 该 keyword 记录数 > 0
- [ ] `-c 1` 单 key 失败(如 keys.txt 写非法字符)→ 不写入 done,日志记 stderr 摘要
- [ ] `-c 1` 重复跑第二次 → 日志 "skip <key> (already done)",done.txt 不再增加
- [ ] `-c 4` 跑 4 个 key → 日志时间戳交错(真并发),done.txt 增 4 行,无重复,无丢失
- [ ] `-c 4` 中途 wrapper `Ctrl+C` → done.txt 已完成的 key 仍写入,Chrome 进程被 terminate
- [ ] 端口 9222 已占用时 `-c 1` → 不冲突(用现有 Chrome);`-c 4` → 报端口冲突错
- [ ] keys.txt 含 `# comment` 与空行 → 被跳过
- [ ] `--concurrency 0` 或 `17` → argparse 报错退出
- [ ] `python -c "import ast; ast.parse(...)"` 通过

**Dependencies:** Task 1, Task 2

**Files likely touched:**
- `crawl_1337x_by_keys.py` → 新建

**Estimated scope:** M (1 新文件,核心 wrapper 逻辑 + Chrome pool 管理 + 并发调度 + IO + 错误处理)

---

### Phase 3: 端到端验证

#### Checkpoint: 全链路验证

**Description:** 所有 Task 完成后,实际跑通验证整个流程,确认 MongoDB 多 keyword 数据落库正确 + 幂等可重入 + 并发安全。

**Verification:**
- [ ] 首次 `python crawl_1337x_by_keys.py -c 4` → 跑完所有 keys.txt 中的 key,每个都写入 done.txt
- [ ] MongoDB:`db.bt_info_list.distinct("keyword")` 至少包含 keys.txt 里所有 key
- [ ] 二次 `python crawl_1337x_by_keys.py -c 4` → 日志 "no new keys to process",退出码 0
- [ ] `wc -l data/keys-done.txt` 等于首次成功数
- [ ] 二次跑后**没有** Chrome 进程残留(`tasklist | grep chrome` 检查)
- [ ] `-c 1` 跑 1 个新 key → 走现有 9222,不启新 Chrome,正常完成

**Dependencies:** Task 1, Task 2, Task 3

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Playwright sync API 多线程共享 context 冲突 | High | wrapper **不开** Playwright,只跑 subprocess;每个 worker 子进程独立 Playwright runtime |
| 用户原有 9222 Chrome 与 wrapper 自动启的 Chrome 冲突 | Med | `concurrency == 1` 时不启 Chrome(沿用现有);`concurrency > 1` 时端口从 9222 开始 wrapper 接管,需告知用户先关外部 Chrome |
| Chrome 启动慢 / 内存占用 | Med | `--headless=new`;并发数上限 16;`urllib` 探测 CDP ready 超时 30s 强制 fail-fast |
| `keys-done.txt` 并发追加交错半行 | Low | `threading.Lock` 保护下 append + flush;OS 文件 append < PIPE_BUF(4096)在 Windows 上原子;按行追加不超过 1KB 完全安全 |
| 旧 MongoDB 记录 `keyword=House` 与新 wrapper 重复 upsert | Low | `_id = md5(detail_url)`,upsert 到相同 `_id` 预期行为,不影响去重 |
| subprocess worker 把 stderr 混到 wrapper 日志难定位 | Low | wrapper 用 `subprocess.Popen(..., stderr=subprocess.PIPE)`,失败时把 stderr 最后 10 行记日志 |
| 跨 Windows 平台路径分隔符问题 | Low | 全部用 `pathlib.Path` + `as_uri()` 或 `f"http://127.0.0.1:{port}"` 拼接,不用字符串 `\` |
| `atexit` 注册 Chrome 清理在 `Ctrl+C` 时不可靠 | Med | wrapper 主循环包 `try/finally` + signal handler(SIGINT/SIGTERM 都注册 terminate);`atexit` 作兜底 |

## Open Questions

- (低优先)wrapper 是否需要支持 `--dry-run` 只打印不爬?—— 用户未要求,本期不做
- (低优先)wrapper 是否需要支持 `--key` 单 key CLI 覆盖 keys.txt?—— 用户未要求,本期不做
- (中优先)`keys.txt` 是否要支持搜索参数(分类、排序、过滤)?—— 用户未要求,本期只用 URL 的 `/search/{keyword}/{page}/` 默认形态
- (中优先)Chrome 自动启动的 user-data-dir 是否需要复用 `~/.chrome_debug_profile` 还是独立目录?—— 计划用独立目录(`pool_{port}`),避免 cookie / 登录态冲突。如果用户希望共享登录态可改 `--share-profile` flag(本期不做)