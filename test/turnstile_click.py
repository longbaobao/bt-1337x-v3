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


class FakeCdpTab:
    def __init__(self):
        self.calls = []

    def _run_cdp(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "DOM.performSearch":
            return {"resultCount": 1, "searchId": "search-1"}
        if method == "DOM.getSearchResults":
            return {"nodeIds": [42]}
        if method == "DOM.resolveNode":
            if kwargs.get("nodeId") != 42:
                raise AssertionError("DOM.resolveNode must receive nodeId=42")
            return {"object": {"objectId": "object-42"}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 20, 110, 20, 110, 70, 10, 70]}}
        if method == "DOM.discardSearchResults":
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")


class FakeShadowCdpTab(FakeCdpTab):
    def __init__(self, checkbox_after=None):
        super().__init__()
        self.checkbox_after = checkbox_after or {"found": True, "checked": True}
        self.runtime_evals = 0
        self.clicks = []

    def _run_cdp(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "DOM.performSearch":
            return {"resultCount": 1, "searchId": "search-1"}
        if method == "DOM.getSearchResults":
            return {"nodeIds": [42]}
        if method == "DOM.resolveNode":
            if kwargs.get("nodeId") != 42:
                raise AssertionError("DOM.resolveNode must receive nodeId=42")
            return {"object": {"objectId": "iframe-object"}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [100, 200, 400, 200, 400, 265, 100, 265]}}
        if method == "DOM.describeNode":
            if kwargs.get("objectId") != "iframe-object":
                raise AssertionError("DOM.describeNode must receive iframe objectId")
            return {"node": {"frameId": "frame-1"}}
        if method == "Page.createIsolatedWorld":
            if kwargs.get("frameId") != "frame-1":
                raise AssertionError("Page.createIsolatedWorld must target the iframe frame")
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            if kwargs.get("contextId") != 7:
                raise AssertionError("Runtime.evaluate must run in the iframe context")
            self.runtime_evals += 1
            value = ({"found": True, "checked": False, "x": 28, "y": 32, "w": 24, "h": 24}
                     if self.runtime_evals == 1 else self.checkbox_after)
            return {"result": {"value": value}}
        if method == "Input.dispatchMouseEvent":
            if kwargs.get("type") in ("mousePressed", "mouseReleased"):
                self.clicks.append((kwargs["x"], kwargs["y"], kwargs["type"]))
            return {}
        if method == "DOM.discardSearchResults":
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")


class FakeBroadIframeSearchTab(FakeCdpTab):
    def _run_cdp(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "DOM.performSearch":
            if kwargs.get("query") == "iframe":
                return {"resultCount": 1, "searchId": "search-iframes"}
            return {"resultCount": 0, "searchId": "search-specific"}
        if method == "DOM.getSearchResults":
            return {"nodeIds": [42]}
        if method == "DOM.describeNode":
            return {"node": {
                "attributes": [
                    "src", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/f/x",
                    "id", "cf-chl-widget-x",
                    "title", "Cloudflare security challenge",
                ],
                "frameId": "frame-1",
            }}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "object-42"}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 20, 110, 20, 110, 70, 10, 70]}}
        if method == "DOM.discardSearchResults":
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")


def check_find_iframe_uses_node_id():
    fake = FakeCdpTab()
    found = ck._find_turnstile_iframe(fake)
    check(found == ("object-42", (10, 20, 110, 70)),
          "_find_turnstile_iframe correctly resolves DOM search nodeIds")
    resolve_calls = [kwargs for method, kwargs in fake.calls if method == "DOM.resolveNode"]
    check(resolve_calls == [{"nodeId": 42}],
          "_find_turnstile_iframe passes nodeId to DOM.resolveNode")


def check_find_iframe_filters_broad_iframe_search():
    fake = FakeBroadIframeSearchTab()
    found = ck._find_turnstile_iframe(fake)
    queries = [kwargs.get("query") for method, kwargs in fake.calls if method == "DOM.performSearch"]
    check(found == ("object-42", (10, 20, 110, 70)),
          "_find_turnstile_iframe finds Cloudflare iframe via broad iframe search")
    check("iframe" in queries,
          "_find_turnstile_iframe searches all iframes before filtering attributes")


def check_shadow_checkbox_coordinates_are_used():
    fake = FakeShadowCdpTab()
    found = ck._find_turnstile_iframe(fake)
    clicked = ck._try_click_turnstile_checkbox(fake)
    press_points = [(x, y) for x, y, t in fake.clicks if t == "mousePressed"]
    check(found == ("iframe-object", (100, 200, 400, 265)),
          "_find_turnstile_iframe returns iframe object and viewport box")
    check(clicked is True,
          "_try_click_turnstile_checkbox accepts checked shadow-DOM checkbox after click")
    check((128, 232) in press_points,
          "_try_click_turnstile_checkbox clicks real checkbox center from iframe context")
    check(any(method == "Page.createIsolatedWorld" for method, _ in fake.calls),
          "_find_turnstile_checkbox_in_frame creates an iframe execution context")
    check(any(method == "Runtime.evaluate" for method, _ in fake.calls),
          "_find_turnstile_checkbox_in_frame evaluates checkbox finder inside iframe")


def check_fake_shadow_chrome_arg_is_not_used():
    full_src = open(inspect.getfile(ck), encoding="utf-8").read()
    check("--enable-blink-features=FakeShadowRoot" not in full_src,
          "Chrome launch does not use unsupported FakeShadowRoot flag")
    check(".set_argument(FAKE_SHADOW_ARG)" not in full_src,
          "run_with_retry does not pass FakeShadowRoot argument to Chrome")


def main():
    src = inspect.getsource(ck._try_click_turnstile_checkbox)
    check_find_iframe_uses_node_id()
    check_find_iframe_filters_broad_iframe_search()
    check_shadow_checkbox_coordinates_are_used()
    check_fake_shadow_chrome_arg_is_not_used()

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
