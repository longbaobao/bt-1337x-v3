"""通过 CDP 连接本地 9222 调试端口的 Chrome，打开百度。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"

with sync_playwright() as p:
    # connect_over_cdp 接入已运行的 Chrome，不会新开浏览器
    browser = p.chromium.connect_over_cdp(CDP_URL)
    print(f"已连接，现有 contexts 数: {len(browser.contexts)}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()

    page = context.new_page()
    page.goto("https://www.baidu.com")
    print(f"标题: {page.title()}")
    print(f"URL: {page.url}")

    # 不关 browser —— 这是用户共享的 Chrome 实例
    page.close()