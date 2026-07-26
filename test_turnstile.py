"""crawl_1337x_by_key.py CF 挑战分类测试。

复选框 Turnstile 和 hCaptcha 图像题需要不同处理:
- Turnstile(可自动点): 含 challenges.cloudflare.com iframe
- hCaptcha 图像题(无法自动过): 含 hcaptcha.com + 图像选择提示
- 普通 5秒盾(已能过): Just a moment / cf_chl_opt

直接跑:python test_turnstile.py
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


# 场景 A: Turnstile 复选框(可自动点)
TURNSTILE_HTML = """
<html><body>
<div id="cf-challenge-running">
  <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/.../..."></iframe>
  请稍候...正在验证您是真人
</div>
</body></html>
"""

# 场景 B: hCaptcha 图像题(无法自动)
HCAPTCHA_HTML = """
<html><body>
<div class="h-captcha">
  <iframe src="https://hcaptcha.com/...?sitekey=..."></iframe>
  select images with buses
</div>
</body></html>
"""

# 场景 C: 普通 5秒盾(已能自动过)
SHIELD_HTML = """
<html><body>
<div class="cf-chl_opt">Just a moment...</div>
</body></html>
"""

# 场景 D: 正常页面(无 CF)
NORMAL_HTML = """
<html><body>
<table class="table-list"><tbody><tr><td>1</td></tr></tbody></table>
</body></html>
"""


def main():
    # 1. 检测 Turnstile iframe 出现 → True
    check(ck.detect_turnstile(TURNSTILE_HTML) is True,
          "Turnstile 复选框页面 → detect_turnstile True")

    # 2. 检测 hCaptcha 出现 → True
    check(ck.detect_hcaptcha(HCAPTCHA_HTML) is True,
          "hCaptcha 图像题 → detect_hcaptcha True")

    # 3. 普通 5秒盾 → 两个都 False(走 sleep+refetch 老路径)
    check(ck.detect_turnstile(SHIELD_HTML) is False,
          "5秒盾 → detect_turnstile False")
    check(ck.detect_hcaptcha(SHIELD_HTML) is False,
          "5秒盾 → detect_hcaptcha False")

    # 4. 正常页面 → 两个都 False
    check(ck.detect_turnstile(NORMAL_HTML) is False,
          "正常页面 → detect_turnstile False")
    check(ck.detect_hcaptcha(NORMAL_HTML) is False,
          "正常页面 → detect_hcaptcha False")

    # 5. classify_cf_challenge 三态返回
    check(ck.classify_cf_challenge(TURNSTILE_HTML) == "turnstile",
          "Turnstile → classify 'turnstile'")
    check(ck.classify_cf_challenge(HCAPTCHA_HTML) == "hcaptcha",
          "hCaptcha → classify 'hcaptcha'")
    check(ck.classify_cf_challenge(SHIELD_HTML) == "shield",
          "5秒盾 → classify 'shield'")
    check(ck.classify_cf_challenge(NORMAL_HTML) == "none",
          "正常 → classify 'none'")

    # 6. CFChallengeEscalated 异常类存在
    check(hasattr(ck, "CFChallengeEscalated"),
          "CFChallengeEscalated 异常类已定义")

    # 7. CF 标记常量存在
    check(hasattr(ck, "_TURNSTILE_MARKERS"),
          "_TURNSTILE_MARKERS 已定义(v2: 多 marker)")
    check(hasattr(ck, "_HCAPTCHA_MARKERS"),
          "_HCAPTCHA_MARKERS 已定义")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())