import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
"""crawl_1337x_by_key.py CF Turnstile 检测 v2 测试。

针对 bug: 上一版 detect_turnstile 只查 'challenges.cloudflare.com' 字面量,
CF 页面渲染早期 iframe src 还没注入 → 漏判为 shield → 永远 sleep+refetch
永远过不去。

改进: 多 marker 检测 (cf-turnstile class / cf-chl-widget / data-sitekey /
turnstile / challenges.cloudflare.com),任一命中即视为 Turnstile。

直接跑:python test_turnstile_v2.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# 早期 CF 页面: 只渲染了"请稍候" shell,iframe src 还没注入
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

# 标准 Turnstile 复选框页面 (iframe src 已有)
TURNSTILE_IFRAME = """
<html><body>
<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/.../..."></iframe>
</body></html>
"""

# 纯文本包含"turnstile"(JS 变量名之类)
TURNSTILE_TEXT = "<div>turnstile widget rendered</div>"

# 普通 5秒盾(没 turnstile 标记)
PLAIN_SHIELD = """
<html><body>
<div class="cf-chl_opt">Just a moment...</div>
</body></html>
"""

# 正常搜索结果页
NORMAL = "<table class='table-list'><tbody><tr><td>x</td></tr></tbody></table>"


def main():
    # 1. 早期 shell (cf-turnstile class) → True
    check(ck.detect_turnstile(EARLY_CF_SHELL) is True,
          "早期 shell 含 cf-turnstile class → True")

    # 2. 早期 shell (cf-chl-widget) → True
    check(ck.detect_turnstile('<div class="cf-chl-widget">x</div>') is True,
          "cf-chl-widget → True")

    # 3. data-sitekey 属性 → True
    check(ck.detect_turnstile('<div data-sitekey="0x4AAA"></div>') is True,
          "data-sitekey → True")

    # 4. iframe src 含 challenges.cloudflare.com → True
    check(ck.detect_turnstile(TURNSTILE_IFRAME) is True,
          "iframe src 含 challenges.cloudflare.com → True")

    # 5. 文本含"turnstile" → True
    check(ck.detect_turnstile(TURNSTILE_TEXT) is True,
          "文本含 'turnstile' → True")

    # 6. 纯 5秒盾 → False
    check(ck.detect_turnstile(PLAIN_SHIELD) is False,
          "纯 5秒盾无 turnstile marker → False")

    # 7. 正常页面 → False
    check(ck.detect_turnstile(NORMAL) is False,
          "正常页面 → False")

    # 8. classify_cf_challenge v2 把早期 shell 正确分类为 turnstile
    check(ck.classify_cf_challenge(EARLY_CF_SHELL) == "turnstile",
          "早期 shell → 'turnstile' (不再漏判为 shield)")

    # 9. STEALTH_INIT_JS 常量存在(用于覆盖 navigator.webdriver)
    check(hasattr(ck, "STEALTH_INIT_JS"),
          "STEALTH_INIT_JS 已定义")

    # 10. STEALTH_INIT_JS 包含 navigator.webdriver 覆盖
    js = getattr(ck, "STEALTH_INIT_JS", "")
    check("navigator.webdriver" in js,
          "STEALTH_INIT_JS 含 navigator.webdriver 覆盖")
    check("false" in js,
          "STEALTH_INIT_JS 把它设为 false")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())