"""crawl_1337x_by_key.py Turnstile 自动过 clicker 测试。

bug 修复回归: 上一版 _try_click_turnstile_checkbox 用 tab.ele("iframe[...], timeout=3")
找 iframe,但 tab.ele 内部 _find_elements 触发 self.wait.doc_loaded()
(chromium_base.py:454),CF 盾页面 _is_loading 永远 True → 等满 3s 超时
返回 None。iframe 找不到 → 根本没走到点击那一步。

修复: 直接 CDP DOM.querySelectorAll + DOM.resolveNode + DOM.getBoxModel
找 iframe 并拿坐标,绕开 tab.ele 的 wait.doc_loaded 阻塞。
点击也改走 CDP Input.dispatchMouseEvent(isTrusted=true,CF Turnstile 接受)。

直接跑:python test/turnstile_click.py
"""
import _path  # noqa: F401  — 让 import crawl_xxx 找到项目根
import sys
sys.stdout.reconfigure(encoding="utf-8")
import inspect

import crawl_1337x_by_key as ck

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def main():
    src = inspect.getsource(ck._try_click_turnstile_checkbox)

    # 1. 不再用 tab.ele 找 iframe (绕开 wait.doc_loaded)
    check('tab.ele("iframe' not in src,
          "_try_click_turnstile_checkbox 不再调 tab.ele 找 iframe (绕开 wait.doc_loaded)")
    check('tab.ele(\'iframe' not in src,
          "同上(单引号检查)")

    # 2. 用 _find_turnstile_iframe 替代
    check(hasattr(ck, "_find_turnstile_iframe"),
          "_find_turnstile_iframe 函数已定义")
    helper_src = inspect.getsource(ck._find_turnstile_iframe)
    check('DOM.querySelectorAll' in helper_src,
          "_find_turnstile_iframe 走 DOM.querySelectorAll (直接 CDP)")
    check('"challenges.cloudflare.com"' in helper_src or "challenges.cloudflare.com" in helper_src,
          "_find_turnstile_iframe selector 含 challenges.cloudflare.com")
    check('DOM.getBoxModel' in helper_src,
          "_find_turnstile_iframe 走 DOM.getBoxModel 拿坐标")

    # 3. 点击走 _cdp_click → Input.dispatchMouseEvent (真实事件)
    check(hasattr(ck, "_cdp_click"), "_cdp_click 函数已定义")
    click_src = inspect.getsource(ck._cdp_click)
    check('Input.dispatchMouseEvent' in click_src,
          "_cdp_click 走 Input.dispatchMouseEvent (真实鼠标事件)")
    check('"mouseMoved"' in click_src or "'mouseMoved'" in click_src,
          "_cdp_click 发 mousemove 轨迹")
    check('"mousePressed"' in click_src and '"mouseReleased"' in click_src,
          "_cdp_click 发 mousedown + mouseup")

    # 4. click 坐标用 _TURNSTILE_BOX_FRACTION 比例(0.25, 0.5)左中,不是固定 offset
    check("_TURNSTILE_BOX_FRACTION" in src,
          "_try_click_turnstile_checkbox 用 _TURNSTILE_BOX_FRACTION 算点击坐标")
    constants_src = inspect.getsource(ck) if False else ""  # placeholder
    # 验证 _TURNSTILE_BOX_FRACTION = (0.25, 0.5) 在源代码
    full_src = open(inspect.getfile(ck), encoding="utf-8").read()
    check("_TURNSTILE_BOX_FRACTION = (0.25, 0.5)" in full_src,
          "_TURNSTILE_BOX_FRACTION = (0.25, 0.5) 左中位置")

    # 5. 旧 _TURNSTILE_CLICK_OFFSETS 不再被使用(避免误导)
    #    检查源码里 _TURNSTILE_CLICK_OFFSETS 不再出现(已删除)
    check("_TURNSTILE_CLICK_OFFSETS" not in full_src,
          "旧 _TURNSTILE_CLICK_OFFSETS 已删除")

    print()
    if failures:
        print(f"❌ {len(failures)} 个断言失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())