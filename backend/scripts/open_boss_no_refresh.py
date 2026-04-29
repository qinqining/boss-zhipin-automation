"""
独立脚本：只打开 Boss 直聘页面，不做任何守护/轮询/自动刷新。

用途：
- 排除项目内“守护拉回/轮询检查/任务逻辑”对页面的影响
- 让你在一个干净的 Playwright 浏览器里手动扫码/登录/验证

运行（Windows）：
  backend/.venv/Scripts/python backend/scripts/open_boss_no_refresh.py
"""

import asyncio
import sys
from typing import Optional

from playwright.async_api import async_playwright, Page


LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
HOME_URL = "https://www.zhipin.com/"


async def safe_print_page_url(page: Optional[Page], label: str) -> None:
    try:
        url = page.url if page else None
    except Exception:
        url = None
    print(f"[{label}] url={url}")


async def main() -> None:
    print("== open_boss_no_refresh ==")
    print("提示：本脚本不会自动刷新/守护/轮询。")
    print(f"将打开：{LOGIN_URL}")

    async with async_playwright() as p:
        # 使用 Playwright 自带 Chromium，尽量隔离系统 Chrome / 扩展 / 策略干扰
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
            ],
        )

        context = await browser.new_context(
            viewport=None,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
        )

        page = await context.new_page()

        # 最小化注入：不要做任何“导航拦截/守护”
        try:
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
        except Exception:
            pass

        # 打开登录页；如果失败，回退打开首页，避免卡死
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] 打开登录页失败：{e!s}")
            print("[warn] 回退打开首页…")
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)

        await safe_print_page_url(page, "opened")

        print("浏览器已打开。你可以在窗口里手动扫码/登录/完成验证。")
        print("关闭浏览器窗口，或在此控制台按 Ctrl+C 结束。")

        # 保持进程不退出，让浏览器持续打开
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

