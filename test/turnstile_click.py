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
import re
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
    # 主路径用 DOM.performSearch (不需 nodeId)
    check('DOM.performSearch' in helper_src,
          "_find_turnstile_iframe 主路径用 DOM.performSearch (无需 nodeId)")
    check('DOM.getSearchResults' in helper_src,
          "_find_turnstile_iframe 配合 DOM.getSearchResults 拿 nodeId 列表")
    # 兜底用 DOM.querySelectorAll (必须传 nodeId)
    check('DOM.querySelectorAll' in helper_src,
          "_find_turnstile_iframe 兜底用 DOM.querySelectorAll")
    check('DOM.getDocument' in helper_src,
          "_find_turnstile_iframe 兜底前先 DOM.getDocument 拿 root nodeId")
    check('root_node_id' in helper_src or "root_nodeId" in helper_src,
          "querySelectorAll 必须传 nodeId 参数(否则 Invalid parameters)")
    check('"challenges.cloudflare.com"' in helper_src or "challenges.cloudflare.com" in helper_src,
          "_find_turnstile_iframe selector 含 challenges.cloudflare.com")
    check('DOM.getBoxModel' in helper_src,
          "_find_turnstile_iframe 走 DOM.getBoxModel 拿坐标")
    # 释放 search handle
    check('discardSearchResults' in helper_src,
          "_find_turnstile_iframe 释放 performSearch handle(防泄漏)")

    # 3. 点击走 _cdp_click → Input.dispatchMouseEvent (真实事件)
    check(hasattr(ck, "_cdp_click"), "_cdp_click 函数已定义")
    click_src = inspect.getsource(ck._cdp_click)
    check('Input.dispatchMouseEvent' in click_src,
          "_cdp_click 走 Input.dispatchMouseEvent (真实鼠标事件)")
    check('"mouseMoved"' in click_src or "'mouseMoved'" in click_src,
          "_cdp_click 发 mousemove 轨迹")
    check('mousePressed' in click_src and 'mouseReleased' in click_src,
          "_cdp_click 发 mousedown + mouseup")
    # P1 bug 防御:buttons 必须是 INTEGER bitmask (1=left/2=right/4=middle),
    # 上一版传 buttons="left" 字符串是无效参数,Chrome 静默丢弃事件
    # 排除 docstring(里面示例提到旧的错误写法)
    click_code = re.sub(r'"""[\s\S]*?"""', '', click_src)
    check('buttons="left"' not in click_code,
          '_cdp_click 代码不再传 buttons="left" 字符串(CDP 需整数)')
    check('buttons="right"' not in click_src,
          '_cdp_click 不再传 buttons="right" 字符串')
    check('buttons=1' in click_src,
          '_cdp_click 传 buttons=1 (mousePressed 时 left held bitmask)')
    check('buttons=0' in click_src,
          '_cdp_click 传 buttons=0 (mouseReleased/mouseMoved 时无按钮)')
    # 真实鼠标轨迹:从远处 (-200,-200 偏移) 平滑移动到目标
    check('start_x = max(0, x - 200)' in click_src or 'x - 200' in click_src,
          "_cdp_click 鼠标轨迹从远处起步(避免 teleport,反 bot 检测)")

    # 4. click 坐标用 _TURNSTILE_CLICK_POSITIONS 多位置(checkbox 在最左 8%)
    check("_TURNSTILE_CLICK_POSITIONS" in src,
          "_try_click_turnstile_checkbox 用 _TURNSTILE_CLICK_POSITIONS 多位置尝试")
    full_src = open(inspect.getfile(ck), encoding="utf-8").read()
    check("(0.08, 0.5)" in full_src,
          "_TURNSTILE_CLICK_POSITIONS 主位置 (0.08, 0.5) 最左 8% (checkbox 实际位置)")
    check("(0.15, 0.5)" in full_src,
          "_TURNSTILE_CLICK_POSITIONS 备位置 1 (0.15, 0.5)")
    check("(0.50, 0.5)" in full_src,
          "_TURNSTILE_CLICK_POSITIONS 备位置 2 (0.5, 0.5) 中心")
    # 旧 _TURNSTILE_BOX_FRACTION (单一 0.25, 0.5) 已替换
    check("_TURNSTILE_BOX_FRACTION = (0.25, 0.5)" not in full_src,
          "旧 _TURNSTILE_BOX_FRACTION = (0.25, 0.5) 单位置 已替换")
    # 多位置 + 验证逻辑
    check("for frac_x, frac_y in _TURNSTILE_CLICK_POSITIONS" in src,
          "_try_click_turnstile_checkbox 循环尝试每个位置")
    check("_turnstile_iframe_still_present" in src,
          "_try_click_turnstile_checkbox 点击后验证 iframe 是否消失(确认命中)")
    check("iframe 已消失" in src,
          "成功时日志说 'iframe 已消失'")

    # 5. 旧 _TURNSTILE_CLICK_OFFSETS 不再被使用(避免误导)
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