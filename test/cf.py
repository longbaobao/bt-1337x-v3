import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py CF 挑战处理全链路测试。

覆盖:
- load_cf_cookies: 用户手导出 cookie 文件的加载(文件不存在/合法/缺字段/损坏/非 list)
- maybe_refresh_cf_cookies: 过 CF 后自动写回(过滤域/字段清洗/no-op/异常静默)
- detect_turnstile/detect_hcaptcha/classify_cf_challenge: CF 挑战分类
- 多 marker 检测(早期 shell 也能识别 Turnstile)
- STEALTH_INIT_JS(navigator.webdriver=false 等反检测)

直接跑:python test/cf.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json
import tempfile
from pathlib import Path

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# ─── load_cf_cookies: 文件加载边界 ─────────────────────────────────────────

def test_load_cf_cookies():
    print("[load_cf_cookies]")

    # 文件不存在
    ck.CF_COOKIES_FILE = Path("/nonexistent/path/cf_cookies.json")
    check(ck.load_cf_cookies() == [], "文件不存在 → 返回 []")

    # 合法文件
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        valid = [
            {"name": "cf_clearance", "value": "abc123", "domain": ".1337x.to", "path": "/", "expiry": 9999999999},
            {"name": "__cf_bm", "value": "xyz789", "domain": ".1337x.to", "path": "/", "expiry": 9999999999},
        ]
        json.dump(valid, f)
        tmp = Path(f.name)
    ck.CF_COOKIES_FILE = tmp
    loaded = ck.load_cf_cookies()
    check(len(loaded) == 2, f"合法文件 → 2 条 cookie (实际 {len(loaded)})")
    check(loaded[0]["name"] == "cf_clearance", "第一条 cookie 名字正确")
    check(loaded[0]["domain"] == ".1337x.to", "第一条 cookie domain 正确")

    # 缺 name 字段
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        bad = [
            {"value": "no_name", "domain": ".1337x.to"},
            {"name": "ok", "value": "v1", "domain": ".1337x.to"},
        ]
        json.dump(bad, f)
        tmp2 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp2
    loaded = ck.load_cf_cookies()
    check(len(loaded) == 1, f"缺 name 字段被跳过 → 剩 1 条 (实际 {len(loaded)})")
    check(loaded[0]["name"] == "ok", "剩的那条是合法的")

    # 缺 value
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        bad = [{"name": "novalue", "domain": ".1337x.to"}]
        json.dump(bad, f)
        tmp3 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp3
    check(ck.load_cf_cookies() == [], "缺 value → 跳过 → 空")

    # JSON 损坏
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write("{not json{[")
        tmp4 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp4
    check(ck.load_cf_cookies() == [], "JSON 损坏 → 返回 []")

    # 顶层不是 list
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"name": "cf_clearance", "value": "x"}, f)
        tmp5 = Path(f.name)
    ck.CF_COOKIES_FILE = tmp5
    check(ck.load_cf_cookies() == [], "顶层不是 list → 返回 []")

    check(hasattr(ck, "CF_COOKIES_FILE"), "CF_COOKIES_FILE 常量已定义")


# ─── maybe_refresh_cf_cookies: 过 CF 后自动落盘 ───────────────────────────

def fake_cookies_page(cookies):
    """返回一个能被 maybe_refresh_cf_cookies 当 page 用的对象,只暴露 .cookies() 方法。"""
    class _FakePage:
        def cookies(self, all_domains=False, all_info=False):
            return cookies
    return _FakePage()


SAMPLE_COOKIES_PASSED = [
    {"name": "cf_clearance", "value": "fresh_token_abc", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0, "httpOnly": True, "secure": True},
    {"name": "__cf_bm", "value": "bm_token_xyz", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0, "httpOnly": True, "secure": True},
    # 别的域 cookie 应被过滤
    {"name": "_ga", "value": "GA1.2.xxx", "domain": ".google.com",
     "path": "/", "expires": 1735689600.0},
]

SAMPLE_COOKIES_NO_CF = [
    {"name": "_ga", "value": "GA1.2.xxx", "domain": ".1337x.to",
     "path": "/", "expires": 1735689600.0},
]


def test_maybe_refresh_cf_cookies():
    print("[maybe_refresh_cf_cookies]")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "cf_cookies.json"
        ck.CF_COOKIES_FILE = tmp

        # 1. 通过 CF + 文件原本不存在 → 写入
        ck.maybe_refresh_cf_cookies(fake_cookies_page(SAMPLE_COOKIES_PASSED))
        check(tmp.exists(), "通过 CF → 文件已创建")
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(len(loaded) == 2, f"只写 1337x.to 的 cookie(2 条)实际 {len(loaded)}")
        names = {c["name"] for c in loaded}
        check(names == {"cf_clearance", "__cf_bm"}, f"含 cf_clearance + __cf_bm: {names}")
        sample = loaded[0]
        check(set(sample.keys()) <= {"name", "value", "domain", "path", "expires"},
              f"字段清洗:实际 keys={sorted(sample.keys())}")

        # 2. 没拿到 cf_clearance → 不写
        tmp.write_text(json.dumps([{"name": "cf_clearance", "value": "OLD_VALUE",
                                     "domain": ".1337x.to", "path": "/",
                                     "expires": 1700000000.0}]),
                        encoding="utf-8")
        old_mtime = tmp.stat().st_mtime_ns
        import time as _t
        _t.sleep(0.05)
        ck.maybe_refresh_cf_cookies(fake_cookies_page(SAMPLE_COOKIES_NO_CF))
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(loaded[0]["value"] == "OLD_VALUE",
              "无 cf_clearance → 文件未被覆盖(保留 OLD_VALUE)")

        # 3. 内容相同 → no-op
        tmp.write_text(json.dumps([
            {"name": "cf_clearance", "value": "same_token",
             "domain": ".1337x.to", "path": "/", "expires": 1735689600.0},
            {"name": "__cf_bm", "value": "same_bm",
             "domain": ".1337x.to", "path": "/", "expires": 1735689600.0},
        ]), encoding="utf-8")
        old_mtime = tmp.stat().st_mtime_ns
        _t.sleep(0.05)
        same_cookies = [
            {"name": "cf_clearance", "value": "same_token", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0, "httpOnly": True},
            {"name": "__cf_bm", "value": "same_bm", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0, "httpOnly": True},
        ]
        result = ck.maybe_refresh_cf_cookies(fake_cookies_page(same_cookies))
        check(result is False, "内容相同 → 返回 False (no-op)")
        check(tmp.stat().st_mtime_ns == old_mtime, "内容相同 → mtime 未变")

        # 4. 内容不同 → 写
        new_cookies = [
            {"name": "cf_clearance", "value": "NEW_TOKEN", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0},
            {"name": "__cf_bm", "value": "new_bm", "domain": ".1337x.to",
             "path": "/", "expires": 1735689600.0},
        ]
        result = ck.maybe_refresh_cf_cookies(fake_cookies_page(new_cookies))
        check(result is True, "新内容 → 返回 True")
        loaded = json.loads(tmp.read_text(encoding="utf-8"))
        check(loaded[0]["value"] == "NEW_TOKEN", "新内容已写入")

        # 5. maybe_refresh_cf_cookies 函数存在
        check(hasattr(ck, "maybe_refresh_cf_cookies"),
              "maybe_refresh_cf_cookies 已定义")

        # 6. page.cookies 失败 → 静默
        class _BrokenPage:
            def cookies(self, **kw):
                raise RuntimeError("CDP 断线")
        try:
            result = ck.maybe_refresh_cf_cookies(_BrokenPage())
            check(True, "page.cookies 抛异常 → 静默返回")
        except Exception as e:
            check(False, f"应静默却抛了: {e}")


# ─── CF 挑战分类: 多 marker + hCaptcha 区分 ─────────────────────────────

TURNSTILE_HTML = """
<html><body>
<div id="cf-challenge-running">
  <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/.../..."></iframe>
  请稍候...正在验证您是真人
</div>
</body></html>
"""

HCAPTCHA_HTML = """
<html><body>
<div class="h-captcha">
  <iframe src="https://hcaptcha.com/...?sitekey=..."></iframe>
  select images with buses
</div>
</body></html>
"""

SHIELD_HTML = """
<html><body>
<div class="cf-chl_opt">Just a moment...</div>
</body></html>
"""

NORMAL_HTML = """
<html><body>
<table class="table-list"><tbody><tr><td>1</td></tr></tbody></table>
</body></html>
"""

# 早期 shell(只有 cf-turnstile class,iframe src 未注入)
EARLY_CF_SHELL = """
<html><body>
<h1>1337x.to</h1>
<p>正在进行安全验证</p>
<div class="cf-chl-widget">
  <div class="cf-turnstile" data-sitekey="0x4AAAAAAA"></div>
  <p>请稍候...</p>
</div>
</body></html>
"""

TURNSTILE_IFRAME = """
<html><body>
<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/.../..."></iframe>
</body></html>
"""


def test_classify_cf():
    print("[classify_cf]")

    # v1 检测(单 marker 'challenges.cloudflare.com')
    check(ck.detect_turnstile(TURNSTILE_HTML) is True,
          "Turnstile 复选框页面 → detect_turnstile True")
    check(ck.detect_hcaptcha(HCAPTCHA_HTML) is True,
          "hCaptcha 图像题 → detect_hcaptcha True")
    check(ck.detect_turnstile(SHIELD_HTML) is False,
          "5秒盾 → detect_turnstile False")
    check(ck.detect_hcaptcha(SHIELD_HTML) is False,
          "5秒盾 → detect_hcaptcha False")
    check(ck.detect_turnstile(NORMAL_HTML) is False,
          "正常页面 → detect_turnstile False")
    check(ck.detect_hcaptcha(NORMAL_HTML) is False,
          "正常页面 → detect_hcaptcha False")

    # v1 classify 三态
    check(ck.classify_cf_challenge(TURNSTILE_HTML) == "turnstile",
          "Turnstile → classify 'turnstile'")
    check(ck.classify_cf_challenge(HCAPTCHA_HTML) == "hcaptcha",
          "hCaptcha → classify 'hcaptcha'")
    check(ck.classify_cf_challenge(SHIELD_HTML) == "shield",
          "5秒盾 → classify 'shield'")
    check(ck.classify_cf_challenge(NORMAL_HTML) == "none",
          "正常 → classify 'none'")

    # 异常类
    check(hasattr(ck, "CFChallengeEscalated"),
          "CFChallengeEscalated 异常类已定义")

    # 常量
    check(hasattr(ck, "_TURNSTILE_MARKERS"),
          "_TURNSTILE_MARKERS 已定义(v2: 多 marker)")
    check(hasattr(ck, "_HCAPTCHA_MARKERS"),
          "_HCAPTCHA_MARKERS 已定义")

    # v2 多 marker: 早期 shell 也能识别
    check(ck.detect_turnstile(EARLY_CF_SHELL) is True,
          "早期 shell 含 cf-turnstile class → True")
    check(ck.detect_turnstile('<div class="cf-chl-widget">x</div>') is True,
          "cf-chl-widget → True")
    check(ck.detect_turnstile('<div data-sitekey="0x4AAA"></div>') is True,
          "data-sitekey → True")
    check(ck.detect_turnstile(TURNSTILE_IFRAME) is True,
          "iframe src 含 challenges.cloudflare.com → True")
    check(ck.detect_turnstile("<div>turnstile widget rendered</div>") is True,
          "文本含 'turnstile' → True")
    check(ck.detect_turnstile(SHIELD_HTML) is False,
          "纯 5秒盾无 turnstile marker → False")
    check(ck.detect_turnstile(NORMAL_HTML) is False,
          "正常页面 → False")
    check(ck.classify_cf_challenge(EARLY_CF_SHELL) == "turnstile",
          "早期 shell → 'turnstile' (不再漏判为 shield)")


# ─── STEALTH_INIT_JS: 反检测 init script ────────────────────────────────

def test_stealth():
    print("[stealth]")
    check(hasattr(ck, "STEALTH_INIT_JS"), "STEALTH_INIT_JS 已定义")
    js = getattr(ck, "STEALTH_INIT_JS", "")
    check("navigator.webdriver" in js,
          "STEALTH_INIT_JS 含 navigator.webdriver 覆盖")
    check("false" in js,
          "STEALTH_INIT_JS 把它设为 false")


def main():
    test_load_cf_cookies()
    test_maybe_refresh_cf_cookies()
    test_classify_cf()
    test_stealth()
    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())