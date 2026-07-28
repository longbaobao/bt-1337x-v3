"""Live probe for 1337x Cloudflare Turnstile handling.

This script intentionally opens a real DrissionPage Chrome instance and hits
the target URL. It is not part of the default automated test suite.
"""
import sys
import time

import _path  # noqa: F401
from DrissionPage import ChromiumOptions, ChromiumPage

import crawl_1337x_by_key as ck


def cdp_count(page, query: str) -> str:
    try:
        result = page._run_cdp(
            "DOM.performSearch",
            query=query,
            includeUserAgentShadowDOM=True,
        )
        search_id = result.get("searchId")
        count = result.get("resultCount", 0)
        if search_id:
            page._run_cdp("DOM.discardSearchResults", searchId=search_id)
        return str(count)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def js_iframe_summary(page) -> str:
    script = r"""(() => {
        const out = [];
        function walk(root) {
            if (!root) return;
            if (root.querySelectorAll) {
                for (const f of root.querySelectorAll('iframe')) {
                    const r = f.getBoundingClientRect();
                    out.push({src: f.src, title: f.title, x: r.x, y: r.y, w: r.width, h: r.height});
                }
                for (const el of root.querySelectorAll('*')) {
                    const sr = el.fakeShadowRoot || el.shadowRoot;
                    if (sr) walk(sr);
                }
            }
        }
        walk(document);
        return out.slice(0, 10);
    })()"""
    try:
        result = page._run_cdp("Runtime.evaluate", expression=script, returnByValue=True)
        return repr(((result or {}).get("result") or {}).get("value"))
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def cdp_iframe_nodes(page) -> str:
    search_id = None
    try:
        result = page._run_cdp("DOM.performSearch", query="iframe", includeUserAgentShadowDOM=True)
        search_id = result.get("searchId")
        count = result.get("resultCount", 0)
        if not search_id or count <= 0:
            return "[]"
        ids = page._run_cdp("DOM.getSearchResults", searchId=search_id, fromIndex=0, toIndex=count)
        out = []
        for node_id in ids.get("nodeIds", [])[:10]:
            desc = page._run_cdp("DOM.describeNode", nodeId=node_id, depth=1, pierce=True)
            node = desc.get("node", {})
            attrs = node.get("attributes", [])
            attr_map = {attrs[i]: attrs[i + 1] for i in range(0, len(attrs) - 1, 2)}
            out.append({
                "nodeId": node_id,
                "attrs": attr_map,
                "frameId": node.get("frameId"),
                "contentFrameId": node.get("contentFrameId"),
            })
        return repr(out)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if search_id:
            try:
                page._run_cdp("DOM.discardSearchResults", searchId=search_id)
            except Exception:
                pass


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://1337x.to/search/aguilera/1/"
    options = ChromiumOptions().auto_port(True)
    page = ChromiumPage(options)
    try:
        page.add_init_js(ck.STEALTH_INIT_JS)
        ck.inject_cf_cookies(page)
        print(f"LIVE start url={url}")
        page.get(url)
        time.sleep(8)

        html = ck._get_outer_html(page)
        kind = ck.classify_cf_challenge(html)
        print(f"LIVE kind_before={kind} html_len={len(html)} url={page.url}")
        print("LIVE cdp_iframe_count=", cdp_count(page, "iframe"))
        print("LIVE cdp_cf_iframe_count=", cdp_count(page, 'iframe[src*="challenges.cloudflare.com"]'))
        print("LIVE cdp_sitekey_count=", cdp_count(page, "[data-sitekey]"))
        print("LIVE js_iframes=", js_iframe_summary(page))
        print("LIVE cdp_iframe_nodes=", cdp_iframe_nodes(page))

        if kind == "turnstile":
            clicked = ck._try_click_turnstile_checkbox(page)
            print(f"LIVE clicked={clicked}")
            time.sleep(10)
            html = ck._get_outer_html(page)
            kind = ck.classify_cf_challenge(html)

        has_rows = ck._selector_exists(page, "table.table-list tbody tr", timeout=8)
        refreshed = ck.maybe_refresh_cf_cookies(page)
        print(f"LIVE kind_after={kind} has_rows={has_rows} refreshed_cookies={refreshed} url={page.url}")
        return 0 if has_rows or kind == "none" else 2
    finally:
        page.quit()


if __name__ == "__main__":
    raise SystemExit(main())
