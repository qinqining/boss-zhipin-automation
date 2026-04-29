"""
Boss 直聘自动化核心服务
基于 Playwright 实现浏览器自动化
"""
import os
import asyncio
import logging
import sys
import time
import subprocess
from typing import Optional, Dict, List
from datetime import datetime
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

# playwright-stealth 版本在不同发行版本/命名空间里可能不提供 stealth_async。
# 这里做兼容封装：如果没有 stealth_async 就退化到 Stealth().use_async(page)。
try:
    from playwright_stealth import stealth_async  # type: ignore
except ImportError:  # pragma: no cover
    from playwright_stealth import Stealth  # type: ignore

    async def stealth_async(page: Page) -> None:
        # playwright-stealth v2.x: use_async() 返回 context manager，
        # 不能 await(page)；对 Page 应用脚本使用 apply_stealth_async(page)。
        await Stealth().apply_stealth_async(page)

from app.services.anti_detection import AntiDetection

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BossAutomation:
    """Boss 直聘自动化服务类"""

    def __init__(self, com_id: Optional[int] = None):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_logged_in: bool = False
        self.current_com_id: Optional[int] = com_id
        self.last_main_url: Optional[str] = None
        # Windows 手动模式是否使用 launch_persistent_context（清理方式不同）
        self._using_persistent_profile: bool = False
        self.manual_mode: bool = False
        self._prepare_login_failures: int = 0
        self._max_prepare_login_failures: int = 3
        self._step_seq: int = 0
        # 手动模式 about:blank 恢复
        self._manual_expected_login_url: Optional[str] = None
        self._manual_blank_recover_count: int = 0
        # 不再用固定上限硬停：改为节流式持续恢复（避免反复 blank 时“恢复次数用完”后彻底失控）
        self._manual_blank_recover_max: int = 3
        # 使用单调时钟，避免与 time.time() 混用导致恢复条件失效
        self._manual_login_started_ts: float = 0.0
        self._manual_last_recover_ts: float = 0.0
        self._manual_guard_task: Optional[asyncio.Task] = None
        self._manual_nav_lock_enabled: bool = False
        self._manual_guard_enabled: bool = False
        # 与 API 轮询共享的“浏览器会话锁”（用于避免并发 goto/evaluate 导致 about:blank）
        self._external_session_lock: Optional[asyncio.Lock] = None
        self._manual_attached_via_cdp: bool = False

        # 配置项
        self.base_url = "https://www.zhipin.com"
        # 如果指定了com_id，使用对应的auth文件；否则不加载任何认证文件（空cookies）
        self.auth_file = self.get_auth_file_path(com_id) if com_id else None

    def _log_step(self, tag: str, **kwargs):
        """统一步骤日志，便于定位流程卡点与跳转原因。"""
        self._step_seq += 1
        extras = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        if extras:
            logger.info(f"🔎 STEP[{self._step_seq:03d}] {tag} | {extras}")
        else:
            logger.info(f"🔎 STEP[{self._step_seq:03d}] {tag}")

    def set_session_lock(self, lock: asyncio.Lock) -> None:
        """注入外部会话锁（通常来自路由层的全局锁），用于串行化导航与 evaluate。"""
        self._external_session_lock = lock

    @staticmethod
    def _is_verification_or_risk_url(url: str) -> bool:
        """识别安全验证/风控链路页面（遇到这类页面不要强行拉回登录页）。"""
        u = (url or "").lower()
        if not u:
            return False
        keywords = (
            "verify-slider",
            "verify-phone",
            "safe/verify",
            "passport/zp/verify",
            "_security_check",
            "captcha",
        )
        return any(k in u for k in keywords)

    async def _goto_locked(self, url: str, **kwargs) -> None:
        """在外部锁保护下执行 goto，避免与 check-ready-state 并发打断。"""
        if self._external_session_lock is None:
            await self.page.goto(url, **kwargs)  # type: ignore[union-attr]
            return
        async with self._external_session_lock:
            await self.page.goto(url, **kwargs)  # type: ignore[union-attr]

    # ---------------------------
    # 手动模式：真实 Chrome + CDP 附加（更像 open_boss_chrome.bat，稳定优先）
    # ---------------------------
    def _get_real_chrome_exe(self) -> Optional[str]:
        """Windows：定位系统 Chrome 可执行文件。"""
        if os.name != "nt":
            return None
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None

    def _is_cdp_port_open(self, port: int) -> bool:
        """检查本机端口是否已被监听（用于判断是否已有 Chrome 调试实例在跑）。"""
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except Exception:
            return False

    def _launch_real_chrome_with_cdp(self, url: str, user_data_dir: str) -> bool:
        """启动真实 Chrome（独立 profile）并开启 remote debugging（随机端口）。

        使用 --remote-debugging-port=0 让 Chrome 随机选端口，然后从 DevToolsActivePort 文件读取，
        避免固定 9222 被站点安全脚本探测（你日志里已经看到它会尝试连 ws://127.0.0.1:9222 并 403）。
        """
        chrome = self._get_real_chrome_exe()
        if not chrome:
            return False

        try:
            os.makedirs(user_data_dir, exist_ok=True)
        except Exception:
            return False

        args = [
            chrome,
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            url,
        ]

        try:
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            return True
        except Exception:
            return False

    def _read_devtools_active_port(self, user_data_dir: str) -> Optional[int]:
        """读取 Chrome 随机调试端口：<profile>/DevToolsActivePort 第一行是端口号。"""
        try:
            path = os.path.join(user_data_dir, "DevToolsActivePort")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                first = (f.readline() or "").strip()
            return int(first) if first.isdigit() else None
        except Exception:
            return None

    async def _try_attach_to_real_chrome(self, url: str) -> bool:
        """手动模式：启动真实 Chrome 并通过 CDP 附加，避免 Playwright launch 导致 blank/乱跳。"""
        if os.name != "nt" or not self.playwright:
            return False

        profile_dir = os.path.abspath(os.path.join(os.getcwd(), ".boss_real_chrome_profile"))
        # 允许用户指定固定端口（不推荐）；默认走随机端口
        fixed_port_env = os.getenv("MANUAL_CDP_PORT", "").strip()
        fixed_port = int(fixed_port_env) if fixed_port_env.isdigit() else None

        port: Optional[int] = None
        if fixed_port is not None:
            port = fixed_port
            if not self._is_cdp_port_open(port):
                # 固定端口模式：仍然按固定端口启动（与历史兼容）
                started = self._launch_real_chrome_with_cdp(url=url, user_data_dir=profile_dir)
                if not started:
                    return False
                for _ in range(80):
                    if self._is_cdp_port_open(port):
                        break
                    await asyncio.sleep(0.1)
        else:
            # 随机端口模式：若已存在 DevToolsActivePort 且端口可用，优先复用；否则拉起新 Chrome
            port = self._read_devtools_active_port(profile_dir)
            if port is None or not self._is_cdp_port_open(port):
                started = self._launch_real_chrome_with_cdp(url=url, user_data_dir=profile_dir)
                if not started:
                    return False
                # 等待 DevToolsActivePort 写入 & 端口监听
                for _ in range(120):
                    port = self._read_devtools_active_port(profile_dir)
                    if port and self._is_cdp_port_open(port):
                        break
                    await asyncio.sleep(0.1)

        if not port or not self._is_cdp_port_open(port):
            return False

        try:
            browser = await self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception:
            return False

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()

        # CDP 模式尽量“像 open_boss_chrome.bat / no_refresh”：
        # - 不注入 stealth（它会改 navigator.webdriver，容易触发 “Cannot redefine property: webdriver”）
        # - 不注入 webdriver 相关 init_script
        # - 尽量复用现有 tab（真实浏览器更稳定），找不到再新开
        page = None
        try:
            for p in context.pages:
                try:
                    u = p.url or ""
                except Exception:
                    u = ""
                if "zhipin.com" in u and u != "about:blank":
                    page = p
                    break
        except Exception:
            page = None

        if page is None:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

        self.browser = browser
        self.context = context
        self.page = page
        # 依赖外部 profile/真实 Chrome，清理时不要强行 close browser
        self._using_persistent_profile = True
        self._log_step("initialize.cdp_attach.ok", port=port, url=self.page.url if self.page else None)
        self._manual_attached_via_cdp = True
        return True

    async def _cdp_reopen_page(self, url: str, reason: str) -> None:
        """CDP 附加模式：page 被关闭时，重新开一个新 page 并回到指定 URL。"""
        try:
            if not self.context:
                return
            self._log_step("cdp.page.reopen.begin", reason=reason, url=url)
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            self.page = page
            self._log_step("cdp.page.reopen.done", current_url=self.page.url if self.page else None)
        except Exception:
            return

    async def _trace_url_window(self, label: str, samples: int = 10, interval_s: float = 0.3):
        """短时间高频采样当前 URL，抓“瞬间跳转到 blank”窗口。"""
        for i in range(samples):
            try:
                url = self.page.url if self.page else None
                self._log_step("trace.url", label=label, idx=i + 1, url=url)
            except Exception as e:
                self._log_step("trace.url.error", label=label, idx=i + 1, error=str(e))
            await asyncio.sleep(interval_s)

    async def _apply_stealth(self, page: Optional[Page]):
        """创建/切换 page 后立即应用 stealth。"""
        if page:
            await stealth_async(page)

    async def _manual_login_guard_loop(self, expected_login_url: str) -> None:
        """手动模式守护：只要离开登录页/变 blank，就持续（节流）拉回登录页。"""
        try:
            # 守护 30 分钟，足够扫码/验证；超时后自动退出，避免长期占用资源
            started = time.monotonic()
            while time.monotonic() - started < 1800:
                await asyncio.sleep(1.0)
                if not self.manual_mode or not self.page:
                    return
                try:
                    current = self.page.url or ""
                except Exception:
                    current = ""

                # 安全验证/风控页面：不要强行拉回登录页，避免打断验证导致刷新循环
                if self._is_verification_or_risk_url(current):
                    continue

                # 只对“异常页”做恢复，避免把站点自身的临时跳转链路强行打断造成刷新抖动
                if (not current) or (current == "about:blank") or current.startswith("chrome-error://") or ("zhipin.com" not in current):
                    now = time.monotonic()
                    # 节流：避免疯狂重试触发风控
                    if now - float(self._manual_last_recover_ts or 0.0) < 2.0:
                        continue
                    self._manual_last_recover_ts = now
                    self._manual_blank_recover_count += 1
                    self._log_step(
                        "manual.guard.recover_to_login",
                        count=self._manual_blank_recover_count,
                        expected=expected_login_url,
                        current_url=current,
                    )
                    try:
                        if current == "about:blank" and (self._manual_blank_recover_count % 3 == 0):
                            await self._manual_replace_page_and_goto_login(expected_login_url, reason="guard_repeated_blank")
                        else:
                            await self._goto_locked(expected_login_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        self._log_step("manual.guard.goto_login.fail", error=str(e), current_url=current)
        except Exception:
            return

    async def _manual_replace_page_and_goto_login(self, expected_login_url: str, reason: str) -> None:
        """手动模式：当前 page 反复 blank 时，直接换新 page 再进登录页。"""
        try:
            if not self.context:
                return
            self._log_step("manual.replace_page.begin", reason=reason, expected=expected_login_url)
            old = self.page
            # 新建 page 也应串行化，避免与外部 evaluate/goto 并发导致状态错乱
            if self._external_session_lock is None:
                new_page = await self.context.new_page()
            else:
                async with self._external_session_lock:
                    new_page = await self.context.new_page()
            try:
                await stealth_async(new_page)
            except Exception:
                pass
            # 基础防护脚本（不依赖旧 page 的监听器）
            try:
                await new_page.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.close = () => {};
                    self.close = () => {};
                    try { window.location.reload = () => {}; } catch(e) {}
                    """
                )
                await new_page.add_init_script(
                    """
                    (() => {
                      const allow = (u) => {
                        try { return String(u || '').includes('/web/user/'); } catch(e) { return false; }
                      };
                      try {
                        const loc = window.location;
                        const _assign = loc.assign.bind(loc);
                        const _replace = loc.replace.bind(loc);
                        loc.assign = (u) => { if (!allow(u)) return; return _assign(u); };
                        loc.replace = (u) => { if (!allow(u)) return; return _replace(u); };
                      } catch (e) {}
                    })();
                    """
                )
            except Exception:
                pass

            self.page = new_page
            try:
                if self._external_session_lock is None:
                    await new_page.goto(expected_login_url, wait_until="domcontentloaded", timeout=60000)
                else:
                    async with self._external_session_lock:
                        await new_page.goto(expected_login_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                self._log_step("manual.replace_page.goto_login.fail", error=str(e))
            self._log_step("manual.replace_page.done", current_url=self.page.url if self.page else None)

            try:
                if old:
                    await old.close()
            except Exception:
                pass
        except Exception:
            return

    async def _manual_recover_from_page_closed(self, expected_login_url: str, reason: str) -> None:
        """手动模式：page 被异常关闭时，创建新 page 并回到登录页。"""
        try:
            if not self.manual_mode or not self.context:
                return
            target = self._get_manual_recover_target(expected_login_url)
            self._log_step("manual.page_closed.recover.begin", reason=reason, target=target, last_main_url=self.last_main_url)
            await self._manual_replace_page_and_goto_login(target, reason=reason)
            self._log_step("manual.page_closed.recover.done", current_url=self.page.url if self.page else None)
        except Exception:
            return

    def _get_manual_recover_target(self, fallback_login_url: str) -> str:
        """手动模式恢复目标：
        - 若最近一次主页面 URL 可用且不是登录/验证链路，优先恢复到该页面（避免“掉回扫码”）
        - 否则回到登录页
        """
        last = (self.last_main_url or "").strip()
        if (
            last
            and last != "about:blank"
            and "zhipin.com" in last
            and ("web/user/" not in last)  # 登录页
            and (not self._is_verification_or_risk_url(last))  # 验证/风控链路
        ):
            return last
        return fallback_login_url

    async def initialize(self, headless: bool = False, skip_auto_navigate: bool = False) -> bool:
        """
        初始化浏览器

        Args:
            headless: 是否无头模式
            skip_auto_navigate: 是否跳过自动导航（手动模式下只打开首页）

        Returns:
            是否初始化成功
        """
        try:
            logger.info(f"🚀 初始化 Playwright 浏览器... headless={headless}")
            is_manual_mode = bool(skip_auto_navigate)
            self.manual_mode = is_manual_mode
            manual_minimal = bool(is_manual_mode and os.getenv("MANUAL_MINIMAL", "1").strip() != "0")
            # 手动模式下守护逻辑（自动拉回登录页）在部分环境会导致“刷新一下就回扫码”的体验。
            # 默认关闭，仅在需要时手动开启：
            #   MANUAL_GUARD=1
            self._manual_guard_enabled = bool(is_manual_mode and os.getenv("MANUAL_GUARD", "").strip() == "1")
            self._prepare_login_failures = 0
            self._step_seq = 0
            logger.info(f"🐍 运行解释器: {sys.executable} | stealth_async_loaded={bool(stealth_async)}")
            self._log_step("initialize.begin", headless=headless, manual_mode=is_manual_mode, com_id=self.current_com_id)

            # 启动 Playwright
            self.playwright = await async_playwright().start()

            # 手动模式：优先采用“真实 Chrome + CDP 附加”（参考 open_boss_chrome.bat 的稳定性）
            # 默认开启；如需关闭，设置 MANUAL_CDP_ATTACH=0
            if is_manual_mode and os.name == "nt" and os.getenv("MANUAL_CDP_ATTACH", "").strip() != "0":
                login_url = f"{self.base_url}/web/user/?ka=header-login"
                self._manual_expected_login_url = login_url
                self._manual_login_started_ts = time.monotonic()
                self._manual_blank_recover_count = 0
                self._log_step("initialize.cdp_attach.try", url=login_url)
                attached = await self._try_attach_to_real_chrome(login_url)
                if attached:
                    logger.info("✅ 手动模式已通过 CDP 附加到真实 Chrome")
                else:
                    self._log_step("initialize.cdp_attach.fail_or_skip")

            # 手动最小模式：行为尽量贴近 open_boss_no_refresh
            # - 不注入 stealth / 反检测 / webdriver 改写（会触发风控/异常跳转）
            # - 不注册大量监听/守护
            # - 不做自动导航（只保证打开登录页）
            if manual_minimal and self.page and self.context and self.browser:
                try:
                    self.page.set_default_timeout(60000)
                    self.page.set_default_navigation_timeout(60000)
                except Exception:
                    pass
                # 但仍然需要一个最小“保命”脚本：
                # - 风控/跳转页有时会调用 window.close() 直接把页签关掉
                # - 登录后也可能出现反复“同 URL 刷新”（location.reload / history.go(0) / replace(location.href)）
                # 这里仅拦截 close 和“同 URL 刷新”，不碰 webdriver 等指纹字段，尽量不影响用户手动导航。
                minimal_guard_js = r"""
                (() => {
                  try { window.close = () => {}; } catch(e) {}
                  try { self.close = () => {}; } catch(e) {}

                  const isSameUrl = (u) => {
                    try {
                      if (!u) return true;
                      const a = new URL(String(u), window.location.href);
                      const b = new URL(String(window.location.href));
                      return a.href === b.href;
                    } catch (e) { return false; }
                  };

                  try {
                    const _reload = window.location.reload?.bind(window.location);
                    if (_reload) window.location.reload = () => {};
                  } catch(e) {}

                  try {
                    const _go = window.history.go?.bind(window.history);
                    if (_go) window.history.go = (delta) => {
                      try {
                        if (delta === 0) return;
                      } catch(e) {}
                      return _go(delta);
                    };
                  } catch(e) {}

                  try {
                    const _replace = window.location.replace?.bind(window.location);
                    if (_replace) window.location.replace = (u) => {
                      try { if (isSameUrl(u)) return; } catch(e) {}
                      return _replace(u);
                    };
                  } catch(e) {}

                  try {
                    const _assign = window.location.assign?.bind(window.location);
                    if (_assign) window.location.assign = (u) => {
                      try { if (isSameUrl(u)) return; } catch(e) {}
                      return _assign(u);
                    };
                  } catch(e) {}
                })();
                """
                try:
                    # 影响后续导航/新文档
                    await self.page.add_init_script(minimal_guard_js)
                except Exception:
                    pass
                try:
                    # 立刻打补丁到“当前已打开的页面”（CDP 复用 tab 时 add_init_script 不一定能立刻生效）
                    await self.page.evaluate(minimal_guard_js)
                except Exception:
                    pass
                # 如果当前不在登录页，轻量尝试打开登录页（不做任何额外动作）
                try:
                    cur = self.page.url or ""
                except Exception:
                    cur = ""
                if "zhipin.com/web/user/" not in cur:
                    try:
                        await self.page.goto(f"{self.base_url}/web/user/?ka=header-login", wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                self._log_step("initialize.manual_minimal.ready", url=self.page.url if self.page else None)
                return True

            # 启动浏览器
            logger.info(f"🖥️ 启动 Chromium 浏览器，headless={headless}，显示窗口={'否' if headless else '是'}")
            # 手动模式：尽量接近「用户自己点的 Chrome」，不要带 --no-sandbox（会触发黄条且易被风控）
            if is_manual_mode:
                launch_args = [
                    '--disable-blink-features=AutomationControlled',
                    '--start-maximized',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-popup-blocking',
                    # 手动模式用持久化 profile 时，系统/用户安装的扩展可能引发 chrome-extension://invalid
                    # 甚至触发异常跳转；这里显式禁用扩展，优先保证扫码登录页稳定。
                    '--disable-extensions',
                    '--disable-component-extensions-with-background-pages',
                ]
            else:
                launch_args = [
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-infobars',
                    '--start-maximized',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-popup-blocking',
                    '--disable-features=TranslateUI',
                ]

            launch_kwargs = dict(
                headless=headless,
                args=launch_args,
            )
            if is_manual_mode:
                # 手动模式：去掉自动化标记；Playwright 默认仍可能带 --no-sandbox（Chrome 黄条）
                launch_kwargs["ignore_default_args"] = [
                    "--enable-automation",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]

            # 创建浏览器上下文参数
            context_options = {
                'viewport': None,  # 不限制 viewport，跟随窗口大小
                'locale': 'zh-CN',
                'timezone_id': 'Asia/Shanghai',
                'color_scheme': 'light',
                'device_scale_factor': 1,
                'is_mobile': False,
                'has_touch': False,
                'java_script_enabled': True,
                'ignore_https_errors': True,
                'user_agent': AntiDetection.get_random_user_agent(),
            }
            if not is_manual_mode:
                context_options.update({
                    'geolocation': {'latitude': 39.9042, 'longitude': 116.4074},
                    'permissions': ['geolocation'],
                    'bypass_csp': True,
                })

            # 如果指定了auth_file且文件存在，则加载已保存的登录状态
            if self.auth_file and os.path.exists(self.auth_file):
                logger.info(f"📂 加载已保存的登录状态: {self.auth_file}")
                context_options['storage_state'] = self.auth_file
                self._log_step("initialize.storage_state.loaded", auth_file=self.auth_file)
            else:
                logger.info("🆕 使用空白状态初始化浏览器（无登录信息）")
                self._log_step("initialize.storage_state.empty", auth_file=self.auth_file)

            # Windows 手动模式优先使用持久化 profile（更像日常 Chrome）；失败则回退普通启动，避免无窗口
            # 若已通过 CDP 附加到真实 Chrome，则跳过后续 launch/new_context 流程
            if self.browser is None:
                self._using_persistent_profile = False
            # 但持久化 profile 很容易被“用户扩展/企业安全组件/异常配置”污染，出现 chrome-extension://invalid、
            # 甚至导致主 frame 反复跳 about:blank / page 被关闭。
            # 因此默认关闭持久化 profile，仅在显式开启时使用：
            #   MANUAL_USE_PERSISTENT_PROFILE=1
            use_persistent_profile = (
                is_manual_mode
                and os.name == "nt"
                and os.getenv("MANUAL_USE_PERSISTENT_PROFILE", "").strip() == "1"
            )
            if self.browser is None and use_persistent_profile:
                user_data_dir = os.path.abspath(os.path.join(os.getcwd(), ".boss_chrome_profile"))
                os.makedirs(user_data_dir, exist_ok=True)
                logger.info(f"🗂️ 手动模式尝试持久化浏览器目录: {user_data_dir}")
                self._log_step("initialize.persistent_context.try", user_data_dir=user_data_dir)
                try:
                    persistent_kwargs = dict(launch_kwargs)
                    persistent_kwargs.pop("headless", None)
                    persistent_kwargs["headless"] = False
                    persistent_kwargs.update(context_options)
                    persistent_kwargs["channel"] = "chrome"

                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        **persistent_kwargs
                    )
                    self.browser = self.context.browser
                    self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                    await stealth_async(self.page)
                    self._using_persistent_profile = True
                    logger.info("✅ 持久化 Chrome 上下文已启动")
                    self._log_step("initialize.persistent_context.ok", page_url=self.page.url if self.page else None)
                except Exception as e:
                    logger.warning(f"⚠️ 持久化启动失败，回退普通启动: {str(e)}", exc_info=True)
                    self._log_step("initialize.persistent_context.fail", error=str(e))
                    self._using_persistent_profile = False
                    self.context = None
                    self.browser = None
                    self.page = None
                    try:
                        if os.name == "nt":
                            self.browser = await self.playwright.chromium.launch(channel="chrome", **launch_kwargs)
                        else:
                            self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                    except Exception:
                        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                    self.context = await self.browser.new_context(**context_options)
                    self.page = await self.context.new_page()
                    await stealth_async(self.page)
            elif self.browser is None:
                # Windows：过去优先用系统 Chrome（channel="chrome"）更像真人浏览器，
                # 但在部分环境会被企业策略/安全软件注入影响，出现反复 about:blank / ERR_BLOCKED_BY_CLIENT。
                # 因此手动模式下默认优先使用 Playwright 自带 Chromium（更“干净”），
                # 只有显式开启时才使用系统 Chrome：
                #   USE_SYSTEM_CHROME=1
                try:
                    use_system_chrome = (os.name == "nt" and os.getenv("USE_SYSTEM_CHROME", "").strip() == "1")
                    if use_system_chrome:
                        self.browser = await self.playwright.chromium.launch(channel="chrome", **launch_kwargs)
                    else:
                        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                except Exception:
                    # 回退到 Playwright 自带 Chromium
                    self.browser = await self.playwright.chromium.launch(**launch_kwargs)

                self.context = await self.browser.new_context(**context_options)
                # 创建新页面
                self.page = await self.context.new_page()
                await stealth_async(self.page)

            # 关键生命周期日志：定位页面为何变成 about:blank
            def _on_page_close():
                logger.warning("🧨 Playwright page 已关闭")
                # 手动模式下：页面被异常关闭会导致前端看到“空白+一直刷新”。
                # 这里自动拉起一个新 page 回到登录页，尽量让浏览器保持可操作状态。
                try:
                    if self.manual_mode and self._manual_expected_login_url and self.context:
                        # CDP 模式：无论是否开启 guard，只要 page 被关就需要重开一个，否则前端会一直掉线
                        if self._manual_attached_via_cdp:
                            asyncio.get_running_loop().create_task(
                                self._cdp_reopen_page(self._manual_expected_login_url, reason="page_closed")
                            )
                        elif self._manual_guard_enabled:
                            asyncio.get_running_loop().create_task(
                                self._manual_recover_from_page_closed(self._manual_expected_login_url, reason="page_closed")
                            )
                except Exception:
                    pass

            def _on_page_crash():
                logger.error("💥 Playwright page 崩溃")

            def _on_frame_navigated(frame):
                try:
                    if self.page and frame == self.page.main_frame:
                        logger.info(f"🧭 主页面跳转: {frame.url}")
                        if frame.url and frame.url != "about:blank":
                            self.last_main_url = frame.url
                        # 如果已经进入招聘端/聊天端等非登录页面，说明用户可能已登录；
                        # 此时不应再把页面强行拉回登录页，否则会表现为“刷新后回到扫码”。
                        if self.manual_mode and frame.url and any(p in frame.url for p in ("/web/boss/", "/web/chat/", "/web/geek/", "chat/recommend", "geek/recommend")):
                            return
                        # 手动守护未开启时，不做任何自动恢复（交给用户手动处理）
                        if self.manual_mode and not self._manual_guard_enabled:
                            return
                        # 手动模式下：被踢到 about:blank / 非登录页，持续守护恢复到期望登录页
                        if (
                            self.manual_mode
                            # 只在真正“异常页”时恢复，避免打断站点自身的临时跳转链路造成刷新抖动
                            and (
                                frame.url == "about:blank"
                                or (frame.url and frame.url.startswith("chrome-error://"))
                                or (frame.url and "zhipin.com" not in frame.url)
                            )
                            and self._manual_expected_login_url
                        ):
                            # 若处于安全验证/风控页面，不做拉回，避免刷新循环
                            if self._is_verification_or_risk_url(frame.url or ""):
                                return
                            # 节流：避免每次 frame 变更都触发 goto 导致风控/死循环
                            now = time.monotonic()
                            if now - float(self._manual_last_recover_ts or 0.0) < 1.5:
                                return
                            self._manual_last_recover_ts = now

                            # 只要处于手动模式，就允许持续恢复（时间窗放宽），直到用户扫码完成
                            if 0 < self._manual_login_started_ts and (now - self._manual_login_started_ts) < 1800:
                                self._manual_blank_recover_count += 1
                                expected = self._get_manual_recover_target(self._manual_expected_login_url)
                                self._log_step(
                                    "manual.recover_to_login",
                                    count=self._manual_blank_recover_count,
                                    expected=expected,
                                    from_url=frame.url,
                                    last_main_url=self.last_main_url,
                                )
                                try:
                                    # 连续 blank 说明当前 tab 可能被污染，换新 tab 更稳
                                    if frame.url == "about:blank" and (self._manual_blank_recover_count % 3 == 0):
                                        asyncio.get_running_loop().create_task(
                                            self._manual_replace_page_and_goto_login(expected, reason="repeated_blank")
                                        )
                                    else:
                                        asyncio.get_running_loop().create_task(
                                            self._goto_locked(expected, wait_until="domcontentloaded", timeout=60000)
                                        )
                                except Exception:
                                    pass
                except Exception:
                    pass

            def _on_request_failed(request):
                try:
                    # 只记录 Boss 相关失败请求，避免日志过载
                    if "zhipin.com" in request.url:
                        failure = request.failure
                        err_text = failure.get("errorText") if isinstance(failure, dict) else str(failure)
                        is_main_doc = request.is_navigation_request()
                        logger.warning(
                            f"🌐 请求失败: {request.method} {request.url} -> {err_text} | main_doc={is_main_doc}"
                        )
                except Exception:
                    pass

            def _on_response(response):
                try:
                    url = response.url or ""
                    if "zhipin.com" not in url:
                        return
                    status = response.status
                    req = response.request
                    is_main_doc = (req.resource_type == "document") or req.is_navigation_request()
                    if req.resource_type == "document":
                        logger.info(
                            f"📄 文档响应: {status} {req.method} {url} | from={req.headers.get('referer', '')}"
                        )
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        logger.warning(f"↪️ 重定向响应: {status} {url} -> {location} | main_doc={is_main_doc}")
                        if is_main_doc and status == 302:
                            logger.warning(f"🧾 302响应头(main_doc): {dict(response.headers)}")
                    elif status >= 400:
                        logger.warning(f"🚨 异常响应: {status} {response.request.method} {url}")
                except Exception:
                    pass

            def _on_console(message):
                try:
                    msg_type = (message.type or "").lower()
                    text = (message.text or "").strip()
                    location = getattr(message, "location", None)
                    loc_text = ""
                    if isinstance(location, dict):
                        loc_text = f" @ {location.get('url', '')}:{location.get('lineNumber', '')}:{location.get('columnNumber', '')}"
                    if msg_type == "error":
                        logger.error(f"🧪 控制台error: {text[:1000]}{loc_text}")
                    elif msg_type == "warning":
                        logger.warning(f"🧪 控制台warning: {text[:500]}{loc_text}")
                except Exception:
                    pass

            def _on_page_error(error):
                try:
                    logger.error(f"💣 页面脚本异常: {error}")
                except Exception:
                    pass

            self.page.on("close", lambda: _on_page_close())
            self.page.on("crash", lambda: _on_page_crash())
            self.page.on("framenavigated", _on_frame_navigated)
            self.page.on("requestfailed", _on_request_failed)
            self.page.on("response", _on_response)
            self.page.on("console", _on_console)
            self.page.on("pageerror", _on_page_error)

            # 手动模式：可选的“文档级跳转拦截”。
            # 默认关闭（MANUAL_LOCK_NAV!=1），因为硬拦截可能导致 net::ERR_FAILED/ERR_ABORTED，
            # 进而表现为“拉回了又一直刷新/抖动”。
            if is_manual_mode and self.context and os.getenv("MANUAL_LOCK_NAV", "").strip() == "1":
                expected_login_prefix = f"{self.base_url}/web/user/"

                async def _route_lock_login(route, request):
                    try:
                        if not self.manual_mode:
                            return await route.continue_()
                        # 仅当“登录页已打开”后才启用锁定，避免初始化阶段访问首页被误伤
                        if not getattr(self, "_manual_nav_lock_enabled", False):
                            return await route.continue_()
                        if request.resource_type != "document" and not request.is_navigation_request():
                            return await route.continue_()
                        url = request.url or ""
                        # 允许登录页及其子路由；其余文档跳转一律拦截
                        if url.startswith(expected_login_prefix):
                            return await route.continue_()
                        # 少数情况下 about:blank 或 chrome-error 会触发导航；直接拦截后由 guard 拉回
                        self._log_step("manual.route.block_nav", url=url)
                        return await route.abort()
                    except Exception:
                        try:
                            return await route.continue_()
                        except Exception:
                            return

                try:
                    await self.context.route("**/*", _route_lock_login)
                except Exception:
                    pass

            # 显式隐藏 webdriver 指纹
            await self.page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                // 防止页面脚本调用 window.close / self.close 导致窗口瞬间消失（进而表现为 about:blank）
                window.close = () => {};
                self.close = () => {};
                """
            )

            if is_manual_mode:
                # 手动模式下“导航强拦截”很容易误伤登录/安全验证链路（例如跳到其它子域、风控页），
                # 从而表现为页面反复重试/看起来一直刷新。
                # 因此默认关闭，仅在明确需要时通过环境变量开启：
                #   MANUAL_BLOCK_NON_LOGIN_NAV=1
                if os.getenv("MANUAL_BLOCK_NON_LOGIN_NAV", "").strip() == "1":
                    await self.page.add_init_script(
                        """
                        (() => {
                          const allow = (u) => {
                            try {
                              const s = String(u || '');
                              return s.includes('/web/user/');
                            } catch (e) { return false; }
                          };
                          try {
                            // 避免页面脚本强制刷新导致“看起来一直刷新”
                            const _reload = window.location.reload.bind(window.location);
                            window.location.reload = () => {};
                          } catch (e) {}
                          try {
                            const loc = window.location;
                            const _assign = loc.assign.bind(loc);
                            const _replace = loc.replace.bind(loc);
                            loc.assign = (u) => { if (!allow(u)) return; return _assign(u); };
                            loc.replace = (u) => { if (!allow(u)) return; return _replace(u); };
                          } catch (e) {}
                          try {
                            const _push = history.pushState.bind(history);
                            const _rep = history.replaceState.bind(history);
                            history.pushState = (st, t, u) => { if (u && !allow(u)) return; return _push(st, t, u); };
                            history.replaceState = (st, t, u) => { if (u && !allow(u)) return; return _rep(st, t, u); };
                          } catch (e) {}
                        })();
                        """
                    )

            # 手动模式优先稳定性，不注入反检测脚本（减少登录页异常跳转风险）
            if not is_manual_mode:
                await AntiDetection.inject_anti_detection_script(self.page)

            # 统一超时配置，避免首页导航失败后停留在 about:blank
            self.page.set_default_timeout(60000)
            self.page.set_default_navigation_timeout(60000)

            logger.info("✅ 浏览器初始化成功")
            self._log_step("initialize.ready", page_url=self.page.url if self.page else None)

            if skip_auto_navigate:
                # 手动模式：先访问首页
                logger.info("📌 手动模式：启动浏览器")
                homepage_opened = False
                last_error: Optional[Exception] = None

                # 注意：守护任务不要在初始化早期就启动。
                # 否则它会在页面还处于 about:blank / 首页加载阶段就开始“拉回登录页”，
                # 反而更容易造成跳转抖动，甚至触发 page 被关闭。

                # 如果已经通过 CDP 附加到真实 Chrome，则“不要再做任何自动导航”。
                # 否则会像你日志里那样，在 attach 成功后仍然执行 goto_home/goto_login，造成跳转/刷新。
                if self._manual_attached_via_cdp:
                    self._log_step("manual.cdp_attached.skip_auto_nav", current_url=self.page.url if self.page else None)
                    logger.info("✅ CDP 已附加：跳过手动模式自动导航（保持页面稳定）")
                    return True

                # 用多策略 + 重试尽量确保不是 about:blank
                for attempt in range(3):
                    for wait_until in ("domcontentloaded", "load", "networkidle"):
                        try:
                            self._log_step("manual.goto_home.try", attempt=attempt + 1, wait_until=wait_until)
                            logger.info(f"🌐 访问首页: {self.base_url} (attempt={attempt + 1}/3, wait_until={wait_until})")
                            await self._goto_locked(self.base_url, wait_until=wait_until, timeout=60000)
                            await AntiDetection.random_sleep(0.5, 1.2)
                            if self.page.url and self.page.url != "about:blank":
                                homepage_opened = True
                                self._log_step("manual.goto_home.ok", url=self.page.url)
                                break
                        except Exception as e:
                            last_error = e
                            self._log_step("manual.goto_home.fail", attempt=attempt + 1, wait_until=wait_until, error=str(e))
                    if homepage_opened:
                        break

                if homepage_opened:
                    logger.info(f"✅ 已打开 Boss 直聘首页: {self.page.url}")
                else:
                    logger.warning(f"⚠️ 访问首页失败，当前仍为: {self.page.url}")
                    if last_error:
                        logger.warning(f"⚠️ 最后一次错误: {str(last_error)}")
                    # 手动模式：首页失败不致命，继续尝试直达登录页（很多时候首页会被风控/网络影响）

                # 直接打开登录页，减少首页点击“登录”触发异常跳转的概率
                try:
                    login_url = f"{self.base_url}/web/user/?ka=header-login"
                    self._log_step("manual.goto_login.try", login_url=login_url)
                    logger.info(f"🔐 手动模式：直达登录页 {login_url}")
                    self._manual_expected_login_url = login_url
                    self._manual_login_started_ts = time.monotonic()
                    self._manual_blank_recover_count = 0
                    await self._trace_url_window("before-goto-login", samples=4, interval_s=0.2)
                    await stealth_async(self.page)
                    await self._goto_locked(login_url, wait_until='domcontentloaded', timeout=60000)
                    # 一旦进入登录页，开启“导航锁定”：禁止被踢回首页/其它页面
                    self._manual_nav_lock_enabled = True
                    # 进入登录页后，如启用守护再启动（默认不启用，避免“刷新后回扫码”）
                    if self._manual_guard_enabled:
                        try:
                            if self._manual_guard_task is None or self._manual_guard_task.done():
                                self._manual_guard_task = asyncio.create_task(
                                    self._manual_login_guard_loop(self._manual_expected_login_url)
                                )
                        except Exception:
                            pass
                    # 默认不阻塞在 Inspector：否则后端接口会卡住，前端看起来“没进展”。
                    # 如需强制暂停（排查/手动操作），设置环境变量 PLAYWRIGHT_PAUSE=1。
                    if os.getenv("PLAYWRIGHT_PAUSE", "").strip() == "1":
                        await self.page.pause()
                    await self._trace_url_window("after-goto-login", samples=12, interval_s=0.25)
                    await AntiDetection.random_sleep(0.5, 1.2)
                    logger.info(f"✅ 当前页面 URL: {self.page.url}")
                    self._log_step("manual.goto_login.done", current_url=self.page.url)
                except Exception as e:
                    logger.warning(f"⚠️ 直达登录页失败，保持当前页: {str(e)}")
                    self._log_step("manual.goto_login.fail", error=str(e), current_url=self.page.url if self.page else None)

                # 如果加载了 cookies，验证是否有效，有效则直接导航到推荐牛人页面
                if self.auth_file and os.path.exists(self.auth_file):
                    logger.info("🔍 检测已加载 cookies，验证登录状态...")
                    try:
                        api_url = "https://www.zhipin.com/wapi/zpboss/h5/user/info"
                        response = await self.page.evaluate(f'''
                            async () => {{
                                try {{
                                    const resp = await fetch("{api_url}");
                                    return await resp.json();
                                }} catch(e) {{
                                    return {{ code: -1 }};
                                }}
                            }}
                        ''')
                        if response.get('code') == 0:
                            zp_data = response.get('zpData', {})
                            base_info = zp_data.get('baseInfo', {})
                            if base_info.get('comId'):
                                self.is_logged_in = True
                                logger.info(f"✅ Cookies 有效，用户: {base_info.get('showName')}，自动导航到推荐牛人页面")
                                await self.navigate_to_recommend_page()
                            else:
                                logger.info("⚠️ Cookies 已过期，需要手动登录")
                        else:
                            logger.info("⚠️ Cookies 已过期，需要手动登录")
                    except Exception as e:
                        logger.warning(f"⚠️ 验证 cookies 失败: {str(e)}，需要手动登录")
            else:
                # 自动模式：准备登录页面（原有逻辑）
                await self.prepare_login_page()

            return True

        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {str(e)}")
            return False

    async def prepare_login_page(self) -> dict:
        """
        准备登录页面（在初始化浏览器后自动调用）

        功能：
        1. 访问Boss直聘首页
        2. 检查是否已登录
        3. 如果未登录，导航到登录页面并切换到二维码模式

        Returns:
            包含状态信息的字典
        """
        try:
            logger.info("🔍 准备登录页面...")
            self._log_step("prepare_login.begin", failures=self._prepare_login_failures, manual_mode=self.manual_mode)
            if self._prepare_login_failures >= self._max_prepare_login_failures:
                msg = f"连续失败 {self._prepare_login_failures} 次，已暂停自动跳转，请手动打开登录页后重试"
                logger.error(f"❌ {msg}")
                self._log_step("prepare_login.stop_by_retry", message=msg)
                return {
                    'success': False,
                    'already_logged_in': False,
                    'message': msg
                }

            # 获取当前URL
            current_url = self.page.url
            logger.info(f"📍 当前页面（准备前）: {current_url}")
            self._log_step("prepare_login.current_url", current_url=current_url)
            if self.manual_mode and (not current_url or current_url == 'about:blank'):
                self._prepare_login_failures += 1
                msg = "手动模式下检测到空白页，已停止自动跳转，请手动输入 https://www.zhipin.com/web/user/?ka=header-login"
                logger.warning(f"⚠️ {msg}")
                self._log_step("prepare_login.manual_blank_blocked", failures=self._prepare_login_failures)
                return {
                    'success': False,
                    'already_logged_in': False,
                    'message': msg
                }
            # 手动模式若已在登录页，不再做任何自动导航，直接停在原地给用户扫码
            if self.manual_mode and 'zhipin.com/web/user/' in current_url:
                logger.info("✋ 手动模式已在登录页，锁定当前页，不再自动跳转")
                qrcode_switch_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.btn-sign-switch.ewm-switch'
                try:
                    await self.page.wait_for_selector(qrcode_switch_selector, timeout=5000)
                    await self.page.click(qrcode_switch_selector)
                except Exception as e:
                    logger.warning(f"⚠️ 手动模式未找到二维码切换按钮，额外等待30秒供手动操作: {str(e)}")
                    await asyncio.sleep(30)
                return {
                    'success': True,
                    'already_logged_in': False,
                    'message': '手动模式已锁定登录页，请直接扫码'
                }

            # 访问首页（带重试逻辑）
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    self._log_step("prepare_login.goto_home.try", attempt=attempt + 1)
                    logger.info(f"🌐 尝试访问首页 (尝试 {attempt + 1}/{max_retries})...")
                    await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=20000)
                    logger.info(f"✅ 首页加载成功")
                    self._log_step("prepare_login.goto_home.ok", url=self.page.url)
                    break
                except Exception as e:
                    self._log_step("prepare_login.goto_home.fail", attempt=attempt + 1, error=str(e))
                    if attempt == max_retries - 1:
                        logger.error(f"❌ 访问首页失败（已尝试 {max_retries} 次）: {str(e)}")
                        raise
                    logger.warning(f"⚠️ 访问首页失败，{2 * (attempt + 1)} 秒后重试: {str(e)}")
                    await asyncio.sleep(2 * (attempt + 1))

            await AntiDetection.random_sleep(1, 2)

            # 检查登录按钮
            login_button_selector = '#header > div.inner.home-inner > div.user-nav > div > a'
            login_button = await self.page.query_selector(login_button_selector)

            if login_button:
                # 未登录，点击登录按钮
                logger.info("👆 点击登录按钮...")
                self._log_step("prepare_login.click_login.try")
                await login_button.click()
                await self.page.wait_for_load_state('networkidle')
                await AntiDetection.random_sleep(1, 2)

                # 检查是否跳转到登录页面
                current_url = self.page.url
                logger.info(f"📍 当前页面: {current_url}")
                self._log_step("prepare_login.after_click", current_url=current_url)

                # 如果跳转到了已登录页面
                if '/web/chat/' in current_url or '/web/boss/' in current_url:
                    logger.info("✅ 检测到已登录状态")

                    # 验证登录
                    try:
                        api_url = "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json"
                        response = await self.page.evaluate(f'''
                            async () => {{
                                const response = await fetch("{api_url}");
                                return await response.json();
                            }}
                        ''')

                        if response.get('code') == 0:
                            # 登录成功
                            self.is_logged_in = True
                            if self.auth_file:
                                await self.context.storage_state(path=self.auth_file)
                            logger.info("✅ 已登录状态验证成功")
                            self._prepare_login_failures = 0

                            # 导航到推荐页面
                            await self.navigate_to_recommend_page()

                            return {
                                'success': True,
                                'already_logged_in': True,
                                'message': '已登录'
                            }
                    except Exception as e:
                        logger.warning(f"⚠️ 验证登录失败: {str(e)}")

                # 如果在登录页面，切换到二维码模式
                if 'zhipin.com/web/user/' in current_url:
                    # 切换到二维码登录
                    qrcode_switch_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.btn-sign-switch.ewm-switch'
                    try:
                        logger.info("🔄 切换到二维码登录模式...")
                        self._log_step("prepare_login.switch_qrcode.try")
                        await self.page.wait_for_selector(qrcode_switch_selector, timeout=5000)
                        await self.page.click(qrcode_switch_selector)
                        await AntiDetection.random_sleep(1, 2)
                        logger.info("✅ 已切换到二维码登录模式")
                        self._log_step("prepare_login.switch_qrcode.ok")
                    except Exception as e:
                        logger.warning(f"⚠️ 切换二维码登录失败（可能已经是二维码模式）: {str(e)}")
                        self._log_step("prepare_login.switch_qrcode.fail", error=str(e))
                        logger.info("⏳ 额外等待30秒，便于手动点击“二维码登录”")
                        await asyncio.sleep(30)

                    # 等待二维码加载
                    qrcode_img_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > img'
                    try:
                        logger.info("⏳ 等待二维码加载...")
                        await self.page.wait_for_selector(qrcode_img_selector, timeout=10000)
                        await AntiDetection.random_sleep(0.5, 1)
                        logger.info("✅ 二维码已加载到页面")
                        self._log_step("prepare_login.qrcode.ready")

                        return {
                            'success': True,
                            'already_logged_in': False,
                            'message': '登录页面已准备好'
                        }
                    except Exception as e:
                        logger.error(f"❌ 等待二维码加载失败: {str(e)}")
                        return {
                            'success': False,
                            'message': f'二维码加载失败: {str(e)}'
                        }
                else:
                    logger.warning(f"⚠️ 未跳转到预期的页面: {current_url}")
                    self._prepare_login_failures += 1
                    self._log_step("prepare_login.unexpected_url", current_url=current_url, failures=self._prepare_login_failures)
                    return {
                        'success': False,
                        'message': '未跳转到登录页面'
                    }
            else:
                # 没有登录按钮，验证登录状态
                logger.info("🔍 未找到登录按钮，验证登录状态...")

                try:
                    api_url = "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json"
                    response = await self.page.evaluate(f'''
                        async () => {{
                            const response = await fetch("{api_url}");
                            return await response.json();
                        }}
                    ''')

                    if response.get('code') == 0:
                        # 确实已登录
                        logger.info("✅ 验证通过，用户已登录")
                        self.is_logged_in = True
                        if self.auth_file:
                            await self.context.storage_state(path=self.auth_file)
                        self._prepare_login_failures = 0

                        # 导航到推荐页面
                        await self.navigate_to_recommend_page()

                        return {
                            'success': True,
                            'already_logged_in': True,
                            'message': '已登录'
                        }
                    else:
                        # 手动模式下不要强制跳转，避免快速刷新/回 blank
                        if self.manual_mode:
                            logger.warning("⚠️ 登录已失效（手动模式），保持当前页面不自动跳转")
                            return {
                                'success': False,
                                'already_logged_in': False,
                                'message': '登录状态失效，请手动在当前页面完成登录'
                            }

                        # 非手动模式才执行自动跳转
                        logger.warning("⚠️ 登录已失效，导航到登录页面...")
                        self.is_logged_in = False

                        # 清除过期状态（auth_file 可能为空，需先判空）
                        if self.auth_file and os.path.exists(self.auth_file):
                            os.remove(self.auth_file)
                        await self.context.clear_cookies()

                        # 直接导航到登录页面（带重试逻辑）
                        login_url = f"{self.base_url}/web/user/?ka=header-login"
                        for attempt in range(3):
                            try:
                                logger.info(f"🌐 尝试访问登录页面 (尝试 {attempt + 1}/3)...")
                                await stealth_async(self.page)
                                await self.page.goto(login_url, wait_until='domcontentloaded', timeout=20000)
                                await self.page.pause()
                                logger.info(f"✅ 登录页面加载成功")
                                break
                            except Exception as e:
                                if attempt == 2:
                                    logger.error(f"❌ 访问登录页面失败（已尝试 3 次）: {str(e)}")
                                    raise
                                logger.warning(f"⚠️ 访问登录页面失败，{2 * (attempt + 1)} 秒后重试: {str(e)}")
                                await asyncio.sleep(2 * (attempt + 1))

                        await AntiDetection.random_sleep(1, 2)

                        # 切换到二维码登录
                        qrcode_switch_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.btn-sign-switch.ewm-switch'
                        try:
                            logger.info("🔄 切换到二维码登录模式...")
                            await self.page.wait_for_selector(qrcode_switch_selector, timeout=5000)
                            await self.page.click(qrcode_switch_selector)
                            await AntiDetection.random_sleep(1, 2)
                            logger.info("✅ 已切换到二维码登录模式")
                        except Exception as e:
                            logger.warning(f"⚠️ 切换二维码登录失败: {str(e)}")
                            logger.info("⏳ 额外等待30秒，便于手动点击“二维码登录”")
                            await asyncio.sleep(30)

                        # 等待二维码加载
                        qrcode_img_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > img'
                        await self.page.wait_for_selector(qrcode_img_selector, timeout=10000)
                        await AntiDetection.random_sleep(0.5, 1)
                        logger.info("✅ 二维码已加载到页面")

                        return {
                            'success': True,
                            'already_logged_in': False,
                            'message': '登录页面已准备好（session已过期）'
                        }

                except Exception as e:
                    logger.error(f"❌ 验证登录状态失败: {str(e)}")
                    self._prepare_login_failures += 1
                    self._log_step("prepare_login.verify_failed", error=str(e), failures=self._prepare_login_failures)
                    return {
                        'success': False,
                        'message': f'验证登录失败: {str(e)}'
                    }

        except Exception as e:
            logger.error(f"❌ 准备登录页面失败: {str(e)}")
            self._prepare_login_failures += 1
            self._log_step("prepare_login.exception", error=str(e), failures=self._prepare_login_failures)
            return {
                'success': False,
                'message': f'准备登录页面失败: {str(e)}'
            }

    async def get_qrcode(self) -> dict:
        """
        获取登录二维码

        如果页面不在登录状态，会自动调用 prepare_login_page() 准备页面

        Returns:
            包含二维码数据或登录信息的字典
        """
        try:
            logger.info("📸 获取二维码...")
            self._log_step("get_qrcode.begin", manual_mode=self.manual_mode)

            # 检查浏览器是否初始化
            if not self.page:
                logger.error("❌ 浏览器未初始化")
                return {
                    'success': False,
                    'qrcode': '',
                    'message': '浏览器未初始化，请先初始化浏览器'
                }

            # 获取当前页面URL
            current_url = self.page.url
            logger.info(f"📍 当前页面: {current_url}")
            self._log_step("get_qrcode.current_url", current_url=current_url)

            # 如果不在登录页面，重新准备登录页面
            if 'zhipin.com/web/user/' not in current_url:
                if self.manual_mode and (not current_url or current_url == 'about:blank'):
                    self._log_step("get_qrcode.manual_blank_blocked")
                    return {
                        'success': False,
                        'qrcode': '',
                        'message': '手动模式页面为空白，请手动访问登录页后再获取二维码'
                    }
                if self.manual_mode:
                    self._log_step("get_qrcode.manual_non_login_blocked", current_url=current_url)
                    return {
                        'success': False,
                        'qrcode': '',
                        'message': '手动模式下请在浏览器中手动打开登录页后再获取二维码'
                    }
                logger.info("⚠️ 当前不在登录页面，重新准备登录页面...")
                self._log_step("get_qrcode.prepare_login.call")
                prepare_result = await self.prepare_login_page()

                # 如果准备过程中发现已登录，直接返回
                if prepare_result.get('already_logged_in'):
                    logger.info("✅ 检测到已登录")
                    return {
                        'success': True,
                        'already_logged_in': True,
                        'qrcode': '',
                        'message': '已登录'
                    }

                # 准备失败
                if not prepare_result.get('success'):
                    self._log_step("get_qrcode.prepare_login.failed", message=prepare_result.get('message'))
                    return prepare_result

                # 更新当前URL
                current_url = self.page.url
                logger.info(f"📍 准备后页面: {current_url}")
                self._log_step("get_qrcode.after_prepare", current_url=current_url)

            # 检查是否已登录（推荐页面或聊天页面）
            if '/web/chat/' in current_url or '/web/boss/' in current_url or 'geek/recommend' in current_url:
                logger.info("✅ 检测到已登录状态")

                # 验证登录并返回用户信息
                try:
                    api_url = "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json"
                    response = await self.page.evaluate(f'''
                        async () => {{
                            const response = await fetch("{api_url}");
                            return await response.json();
                        }}
                    ''')

                    if response.get('code') == 0:
                        zp_data = response.get('zpData', {})
                        user_info = {
                            'userId': zp_data.get('userId'),
                            'name': zp_data.get('name'),
                            'showName': zp_data.get('showName'),
                            'avatar': zp_data.get('largeAvatar'),
                            'email': zp_data.get('email'),
                            'brandName': zp_data.get('brandName'),
                        }

                        return {
                            'success': True,
                            'already_logged_in': True,
                            'user_info': user_info,
                            'qrcode': '',
                            'message': '已登录'
                        }
                except Exception as e:
                    logger.warning(f"⚠️ 验证登录失败: {str(e)}")

            # 如果在登录页面，读取二维码
            if 'zhipin.com/web/user/' in current_url:
                logger.info("📋 当前在登录页面，读取二维码...")
                self._log_step("get_qrcode.read_qr.try")

                # 先检查二维码是否过期，如果过期则自动刷新
                logger.info("🔍 检查二维码是否需要刷新...")
                refresh_result = await self.check_and_refresh_qrcode()

                if refresh_result.get('need_refresh') and refresh_result.get('qrcode'):
                    # 二维码已刷新，直接返回新的二维码
                    logger.info("✅ 二维码已自动刷新")
                    return {
                        'success': True,
                        'qrcode': refresh_result.get('qrcode'),
                        'message': '二维码已刷新'
                    }

                # 等待二维码元素
                qrcode_img_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > img'

                try:
                    # 查找二维码元素
                    qrcode_element = await self.page.wait_for_selector(qrcode_img_selector, timeout=5000)

                    if qrcode_element:
                        qrcode_src = await qrcode_element.get_attribute('src')
                        logger.info(f"✅ 成功读取二维码")
                        self._log_step("get_qrcode.read_qr.ok", has_src=bool(qrcode_src))

                        # 转换为完整URL
                        if qrcode_src and not qrcode_src.startswith('data:') and not qrcode_src.startswith('http'):
                            qrcode_src = f"{self.base_url}{qrcode_src}"

                        return {
                            'success': True,
                            'qrcode': qrcode_src,
                            'message': '二维码获取成功'
                        }
                    else:
                        logger.warning("⚠️ 未找到二维码元素")
                        return {
                            'success': False,
                            'qrcode': '',
                            'message': '未找到二维码元素'
                        }

                except Exception as e:
                    logger.error(f"❌ 读取二维码失败: {str(e)}")
                    return {
                        'success': False,
                        'qrcode': '',
                        'message': f'读取二维码失败: {str(e)}'
                    }
            else:
                # 不在预期页面
                logger.warning(f"⚠️ 当前不在登录页面: {current_url}")
                self._log_step("get_qrcode.not_on_login_page", current_url=current_url)
                return {
                    'success': False,
                    'qrcode': '',
                    'message': f'当前不在登录页面，请先初始化浏览器'
                }

        except Exception as e:
            logger.error(f"❌ 获取二维码失败: {str(e)}")
            return {
                'success': False,
                'qrcode': '',
                'message': f'获取二维码失败: {str(e)}'
            }

    async def check_and_refresh_qrcode(self) -> dict:
        """
        检查二维码是否需要刷新，如果需要则自动刷新

        Returns:
            包含结果的字典 {'need_refresh': bool, 'qrcode': str, 'message': str}
        """
        try:
            # 检查是否在登录页面
            current_url = self.page.url
            if 'zhipin.com/web/user/' not in current_url:
                return {
                    'need_refresh': False,
                    'qrcode': '',
                    'message': '不在登录页面'
                }

            # 检查刷新按钮
            refresh_button_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > div > button'
            refresh_button = await self.page.query_selector(refresh_button_selector)

            if refresh_button:
                # 需要刷新二维码
                logger.info("🔄 检测到二维码过期，自动刷新...")

                try:
                    # 点击刷新按钮
                    await refresh_button.click()
                    await AntiDetection.random_sleep(1, 2)

                    # 等待新二维码加载
                    qrcode_img_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > img'
                    await self.page.wait_for_selector(qrcode_img_selector, timeout=10000)
                    await AntiDetection.random_sleep(0.5, 1)

                    # 获取新的二维码
                    qrcode_element = await self.page.query_selector(qrcode_img_selector)
                    if qrcode_element:
                        qrcode_src = await qrcode_element.get_attribute('src')
                        logger.info(f"✅ 二维码已刷新")

                        # 如果是相对路径，转换为完整 URL
                        if qrcode_src and not qrcode_src.startswith('data:') and not qrcode_src.startswith('http'):
                            qrcode_src = f"{self.base_url}{qrcode_src}"

                        return {
                            'need_refresh': True,
                            'qrcode': qrcode_src,
                            'message': '二维码已刷新'
                        }
                    else:
                        return {
                            'need_refresh': True,
                            'qrcode': '',
                            'message': '刷新后未找到二维码'
                        }

                except Exception as e:
                    logger.error(f"❌ 刷新二维码失败: {str(e)}")
                    return {
                        'need_refresh': True,
                        'qrcode': '',
                        'message': f'刷新失败: {str(e)}'
                    }
            else:
                # 不需要刷新
                return {
                    'need_refresh': False,
                    'qrcode': '',
                    'message': '二维码有效'
                }

        except Exception as e:
            logger.error(f"❌ 检查二维码失败: {str(e)}")
            return {
                'need_refresh': False,
                'qrcode': '',
                'message': f'检查失败: {str(e)}'
            }

    async def check_login_status(self) -> dict:
        """
        检查是否已登录，并获取用户信息

        Returns:
            包含登录状态和用户信息的字典
        """
        try:
            current_url = self.page.url

            # 如果页面是空白或未访问Boss直聘,先访问官网首页
            if self.manual_mode and (not current_url or current_url == 'about:blank' or 'zhipin.com' not in current_url):
                return {
                    'logged_in': False,
                    'user_info': None,
                    'message': '手动模式下保持当前页面，请手动打开登录页并扫码'
                }
            if not current_url or current_url == 'about:blank' or 'zhipin.com' not in current_url:
                logger.info("📍 页面未访问Boss直聘,先访问首页...")
                await self.page.goto(self.base_url, wait_until='networkidle', timeout=30000)
                await AntiDetection.random_sleep(1, 2)
                current_url = self.page.url
                logger.info(f"📍 当前页面: {current_url}")

            # 检查是否二维码消失（页面跳转）
            if 'zhipin.com/web/user/' not in current_url:
                logger.info(f"📍 页面已跳转: {current_url}")

                # 使用 API 验证登录状态
                try:
                    # 调用 h5/user/info API 获取完整用户信息
                    api_url = "https://www.zhipin.com/wapi/zpboss/h5/user/info"
                    response = await self.page.evaluate(f'''
                        async () => {{
                            const response = await fetch("{api_url}");
                            return await response.json();
                        }}
                    ''')

                    logger.info(f"📡 API 响应: {response}")

                    if response.get('code') == 0:
                        # 登录成功
                        logger.info("✅ 登录成功！")

                        # 提取用户信息
                        zp_data = response.get('zpData', {})
                        base_info = zp_data.get('baseInfo', {})
                        com_id = base_info.get('comId')
                        user_info = {
                            'comId': com_id,
                            'name': base_info.get('name'),
                            'showName': base_info.get('showName'),
                            'avatar': base_info.get('avatar'),
                            'title': base_info.get('title'),
                        }

                        # 如果是新登录（auth_file为None），根据com_id生成新的auth文件
                        if not self.auth_file and com_id:
                            self.current_com_id = com_id
                            self.auth_file = self.get_auth_file_path(com_id)
                            logger.info(f"🆕 检测到新账号登录，com_id: {com_id}")

                        # 保存登录状态
                        if self.auth_file:
                            await self.context.storage_state(path=self.auth_file)
                            logger.info(f"💾 登录状态已保存: {self.auth_file}")
                        else:
                            logger.warning("⚠️ 无法保存登录状态：未获取到com_id")

                        self.is_logged_in = True

                        # 保存账号信息到数据库
                        try:
                            await self._save_account_info(response)
                            logger.info("💾 用户账号信息已保存到数据库")
                        except Exception as e:
                            logger.warning(f"⚠️ 保存账号信息失败: {str(e)}")

                        # 自动导航到推荐牛人页面
                        navigate_result = await self.navigate_to_recommend_page()
                        logger.info(f"📍 导航结果: {navigate_result.get('message')}")

                        return {
                            'logged_in': True,
                            'user_info': user_info,
                            'message': '登录成功',
                            'navigate_result': navigate_result
                        }
                    else:
                        # 登录失败
                        message = response.get('message', '登录验证失败')
                        logger.warning(f"⚠️ 登录验证失败: {message}")
                        return {
                            'logged_in': False,
                            'user_info': None,
                            'message': message
                        }

                except Exception as e:
                    logger.error(f"❌ API 调用失败: {str(e)}")
                    return {
                        'logged_in': False,
                        'user_info': None,
                        'message': f'API 调用失败: {str(e)}'
                    }
            else:
                # 还在登录页面
                return {
                    'logged_in': False,
                    'user_info': None,
                    'message': '等待扫码'
                }

        except Exception as e:
            logger.error(f"❌ 检查登录状态失败: {str(e)}")
            return {
                'logged_in': False,
                'user_info': None,
                'message': f'检查失败: {str(e)}'
            }

    async def check_and_login(self) -> bool:
        """
        检查登录状态，如果未登录则引导用户登录

        Returns:
            是否已登录
        """
        try:
            logger.info("🔍 检查登录状态...")

            # 访问首页
            await self.page.goto(self.base_url, wait_until='networkidle', timeout=30000)
            await AntiDetection.random_sleep(1, 2)

            # 检查是否存在登录按钮
            login_button_selector = '#header > div.inner.home-inner > div.user-nav > div > a'
            login_button = await self.page.query_selector(login_button_selector)

            if login_button:
                # 存在登录按钮，说明未登录
                button_text = await login_button.inner_text()
                logger.info(f"❌ 未登录，发现登录按钮: {button_text}")

                # 点击登录按钮
                logger.info("👆 点击登录按钮...")
                await login_button.click()
                await self.page.wait_for_load_state('networkidle')

                # 检查当前 URL
                current_url = self.page.url
                logger.info(f"📍 当前页面: {current_url}")

                # 等待用户手动登录
                logger.info("⏳ 请在浏览器中完成登录操作...")
                logger.info("   - 可以选择手机验证码登录")
                logger.info("   - 或使用 APP/微信扫码登录")
                logger.info("   - 注意：请选择「我要招聘」选项卡")

                # 等待跳转到招聘端首页或其他已登录页面
                # Boss 直聘登录后会跳转到 /web/boss/ 开头的页面
                try:
                    await self.page.wait_for_url('**/web/boss/**', timeout=120000)
                    logger.info("✅ 登录成功！")

                    # 保存登录状态
                    if self.auth_file:
                        await self.context.storage_state(path=self.auth_file)
                        logger.info(f"💾 登录状态已保存: {self.auth_file}")
                    else:
                        logger.warning("⚠️ 登录成功但 auth_file 为空，跳过持久化存储")

                    self.is_logged_in = True
                    return True

                except Exception as e:
                    logger.warning(f"⚠️ 等待登录超时或被取消: {str(e)}")
                    return False

            else:
                # 不存在登录按钮，检查是否已在招聘端
                if '/web/boss/' in self.page.url:
                    logger.info("✅ 已登录招聘端")
                    self.is_logged_in = True
                    return True
                else:
                    # 可能在其他页面，尝试访问招聘端首页
                    logger.info("🔄 尝试访问招聘端首页...")
                    await self.page.goto(f"{self.base_url}/web/boss/", wait_until='networkidle')

                    if '/web/boss/' in self.page.url:
                        logger.info("✅ 已登录招聘端")
                        self.is_logged_in = True
                        return True
                    else:
                        logger.error("❌ 登录状态异常")
                        return False

        except Exception as e:
            logger.error(f"❌ 检查登录状态失败: {str(e)}")
            return False

    async def search_candidates(
        self,
        keywords: str,
        city: Optional[str] = None,
        experience: Optional[str] = None,
        degree: Optional[str] = None,
        max_results: int = 50
    ) -> List[Dict]:
        """
        搜索求职者

        Args:
            keywords: 搜索关键词
            city: 城市代码
            experience: 工作经验要求
            degree: 学历要求
            max_results: 最大结果数

        Returns:
            求职者列表
        """
        if not self.is_logged_in:
            logger.error("❌ 未登录，无法搜索求职者")
            return []

        try:
            logger.info(f"🔍 搜索求职者: {keywords}")

            # 构建搜索 URL
            search_url = f"{self.base_url}/web/geek/search?query={keywords}"

            # 添加筛选参数
            if city:
                search_url += f"&city={city}"
            if experience:
                search_url += f"&experience={experience}"
            if degree:
                search_url += f"&degree={degree}"

            # 访问搜索页面
            await self.page.goto(search_url, wait_until='networkidle')
            await AntiDetection.random_sleep(2, 3)

            # 等待求职者列表加载
            await self.page.wait_for_selector('.geek-list', timeout=10000)

            # 使用智能滚动加载候选人
            logger.info("📋 使用智能滚动提取求职者信息...")
            candidates = await self._get_candidates_from_dom(max_results)

            logger.info(f"✅ 成功提取 {len(candidates)} 个求职者信息")
            return candidates

        except Exception as e:
            logger.error(f"❌ 搜索求职者失败: {str(e)}")
            return []

    async def _extract_candidate_info(self, item_element) -> Optional[Dict]:
        """
        从求职者卡片元素中提取信息

        Args:
            item_element: 求职者卡片元素

        Returns:
            求职者信息字典
        """
        try:
            # 提取 Boss ID
            boss_id = await item_element.get_attribute('data-geek-id')

            # 提取姓名
            name_element = await item_element.query_selector('.geek-name')
            name = await name_element.inner_text() if name_element else "Unknown"

            # 提取职位
            position_element = await item_element.query_selector('.geek-job')
            position = await position_element.inner_text() if position_element else "N/A"

            # 提取公司
            company_element = await item_element.query_selector('.geek-company')
            company = await company_element.inner_text() if company_element else None

            # 提取活跃时间
            active_element = await item_element.query_selector('.geek-active-time')
            active_time_str = await active_element.inner_text() if active_element else None

            # 提取个人主页链接
            link_element = await item_element.query_selector('a')
            profile_url = await link_element.get_attribute('href') if link_element else None
            if profile_url and not profile_url.startswith('http'):
                profile_url = f"{self.base_url}{profile_url}"

            return {
                'boss_id': boss_id,
                'name': name,
                'position': position,
                'company': company,
                'active_time': active_time_str,
                'profile_url': profile_url,
            }

        except Exception as e:
            logger.warning(f"提取求职者信息失败: {str(e)}")
            return None

    def _parse_api_candidate(self, api_data: dict) -> Dict:
        """
        解析 API 返回的候选人数据

        Args:
            api_data: Boss 直聘 API 返回的候选人数据

        Returns:
            标准化的候选人信息字典
        """
        try:
            # 提取工作经验
            works = api_data.get('works', [])
            work_desc = works[0].get('workDesc', '') if works else ''

            # 提取教育信息
            edu = api_data.get('edu', {})
            education = edu.get('degreeName', '')

            return {
                'boss_id': api_data.get('encryptGeekId'),
                'name': api_data.get('geekName'),
                'position': api_data.get('expectPositionName'),
                'company': works[0].get('company', '') if works else None,
                'active_time': api_data.get('activeTimeDesc'),
                'profile_url': f"{self.base_url}/web/geek/chat?geekId={api_data.get('encryptGeekId')}",
                'avatar': api_data.get('geekAvatar'),
                'work_experience': work_desc,
                'education': education,
                'salary': api_data.get('salary'),
                'location': api_data.get('expectLocationName'),
            }
        except Exception as e:
            logger.warning(f"解析 API 候选人数据失败: {str(e)}")
            return None

    async def _get_candidates_from_dom(self, max_results: int = 50) -> List[Dict]:
        """
        从 DOM 获取候选人列表（带智能滚动）

        Args:
            max_results: 最大候选人数量

        Returns:
            候选人信息列表
        """
        candidates = []
        previous_count = 0
        max_scrolls = 20

        logger.info(f"🔄 开始智能滚动加载候选人列表...")

        try:
            for scroll_count in range(max_scrolls):
                # 获取当前所有候选人元素
                items = await self.page.query_selector_all('.geek-item')
                current_count = len(items)

                logger.info(f"📊 滚动第 {scroll_count + 1} 次: 发现 {current_count} 个候选人")

                # 提取新候选人信息
                for item in items[previous_count:]:
                    if len(candidates) >= max_results:
                        break

                    candidate = await self._extract_candidate_info(item)
                    if candidate:
                        candidates.append(candidate)

                # 检查是否已达到目标数量
                if len(candidates) >= max_results:
                    logger.info(f"✅ 已达到目标数量 {max_results}，停止滚动")
                    break

                # 检查候选人数量是否不再增加
                if current_count == previous_count:
                    logger.info(f"✅ 候选人数量不再增加，停止滚动")
                    break

                previous_count = current_count

                # 滚动到最后一个元素
                if items:
                    try:
                        last_item = items[-1]
                        await last_item.scroll_into_view_if_needed()
                        await AntiDetection.random_sleep(0.5, 1)
                    except Exception as e:
                        logger.warning(f"滚动到元素失败: {str(e)}")
                        # 回退到普通滚动
                        await AntiDetection.simulate_scroll(self.page)
                        await AntiDetection.random_sleep(1, 2)

            logger.info(f"✅ 智能滚动完成，共加载 {len(candidates)} 个候选人")
            return candidates

        except Exception as e:
            logger.error(f"❌ 从 DOM 获取候选人失败: {str(e)}")
            return candidates

    async def get_recommended_candidates(self, max_results: int = 50) -> List[Dict]:
        """
        获取推荐牛人列表（优先使用 API，失败时回退到 DOM）

        Args:
            max_results: 最大候选人数量（默认 50）

        Returns:
            候选人信息列表
        """
        if not self.is_logged_in:
            logger.error("❌ 未登录，无法获取推荐候选人")
            return []

        try:
            logger.info("🎯 获取推荐候选人列表...")

            # 先导航到推荐页面
            await self.navigate_to_recommend_page()
            await AntiDetection.random_sleep(2, 3)

            # 优先尝试使用 API 获取
            logger.info("📡 尝试通过 API 获取推荐列表...")
            try:
                api_url = "https://www.zhipin.com/wapi/zpchat/geek/recommend"
                response = await self.page.evaluate(f'''
                    async () => {{
                        const response = await fetch("{api_url}", {{
                            method: 'GET',
                            headers: {{
                                'accept': 'application/json',
                                'x-requested-with': 'XMLHttpRequest'
                            }},
                            credentials: 'include'
                        }});
                        return await response.json();
                    }}
                ''')

                if response.get('code') == 0:
                    zp_data = response.get('zpData', {})
                    geek_list = zp_data.get('geekList', [])

                    logger.info(f"✅ API 返回 {len(geek_list)} 个推荐候选人")

                    candidates = []
                    for geek in geek_list[:max_results]:
                        candidate = self._parse_api_candidate(geek)
                        if candidate:
                            candidates.append(candidate)

                    logger.info(f"✅ 成功解析 {len(candidates)} 个候选人")
                    return candidates
                else:
                    logger.warning(f"⚠️ API 返回错误: code={response.get('code')}, message={response.get('message')}")

            except Exception as api_error:
                logger.warning(f"⚠️ API 获取失败: {str(api_error)}")

            # API 失败，回退到 DOM 解析
            logger.info("🔄 回退到 DOM 解析方式...")
            return await self._get_candidates_from_dom(max_results)

        except Exception as e:
            logger.error(f"❌ 获取推荐候选人失败: {str(e)}")
            return []

    async def send_greeting(
        self,
        candidate_boss_id: str,
        message: str,
        use_random_delay: bool = True
    ) -> bool:
        """
        向求职者发送打招呼消息（改进版：确保元素可见）

        Args:
            candidate_boss_id: 求职者 Boss ID
            message: 消息内容
            use_random_delay: 是否使用随机延迟

        Returns:
            是否发送成功
        """
        if not self.is_logged_in:
            logger.error("❌ 未登录，无法发送消息")
            return False

        try:
            logger.info(f"💬 向求职者 {candidate_boss_id} 发送消息...")

            # 随机延迟
            if use_random_delay:
                await AntiDetection.random_sleep(2, 5)

            # 1. 先找到候选人卡片
            card_selector = f'[data-geek-id="{candidate_boss_id}"]'
            try:
                card = await self.page.wait_for_selector(card_selector, timeout=10000)
            except Exception as e:
                logger.warning(f"⚠️ 未找到候选人卡片: {candidate_boss_id}, {str(e)}")
                return False

            if not card:
                logger.warning(f"⚠️ 候选人卡片不存在: {candidate_boss_id}")
                return False

            # 2. 滚动到候选人卡片（关键改进！）
            logger.info(f"📜 滚动到候选人卡片...")
            try:
                await card.scroll_into_view_if_needed()
                await AntiDetection.random_sleep(0.5, 1)
            except Exception as e:
                logger.warning(f"⚠️ 滚动失败，尝试继续: {str(e)}")

            # 3. 查找并点击沟通按钮
            chat_button = await card.query_selector('.start-chat-btn')

            if not chat_button:
                logger.warning(f"⚠️ 未找到沟通按钮: {candidate_boss_id}")
                return False

            # 4. 点击沟通按钮
            await chat_button.click()
            await AntiDetection.random_sleep(1, 2)

            # 等待聊天窗口打开
            await self.page.wait_for_selector('.chat-input', state='visible', timeout=5000)

            # 输入消息
            await self.page.fill('.chat-input', message)
            await AntiDetection.random_sleep(0.5, 1)

            # 点击发送按钮
            send_button = await self.page.query_selector('.send-btn')
            if send_button:
                await send_button.click()
                await AntiDetection.random_sleep(0.5, 1)

                logger.info(f"✅ 消息发送成功")
                return True
            else:
                logger.warning(f"⚠️ 未找到发送按钮")
                return False

        except Exception as e:
            logger.error(f"❌ 发送消息失败: {str(e)}")
            return False

    async def check_for_issues(self) -> Optional[str]:
        """
        检查是否出现验证码或账号限制

        Returns:
            如果有问题，返回问题描述；否则返回 None
        """
        # 检查验证码
        has_captcha = await AntiDetection.check_for_captcha(self.page)
        if has_captcha:
            return "检测到验证码"

        # 检查账号限制
        limit_reason = await AntiDetection.check_account_limit(self.page)
        if limit_reason:
            return f"账号被限制: {limit_reason}"

        return None

    async def get_chatted_jobs(self) -> dict:
        """
        获取已沟通的职位列表

        Returns:
            包含职位列表的字典
        """
        try:
            logger.info("📋 获取已沟通职位列表...")

            # 调用职位列表 API
            api_url = "https://www.zhipin.com/wapi/zpjob/job/chatted/jobList"

            response = await self.page.evaluate(f'''
                async () => {{
                    const response = await fetch("{api_url}", {{
                        method: 'GET',
                        headers: {{
                            'accept': 'application/json, text/plain, */*',
                            'x-requested-with': 'XMLHttpRequest'
                        }}
                    }});
                    return await response.json();
                }}
            ''')

            logger.info(f"📡 API 响应: {response}")

            if response.get('code') == 0:
                jobs = response.get('zpData', [])
                logger.info(f"✅ 成功获取 {len(jobs)} 个职位")

                return {
                    'success': True,
                    'jobs': jobs,
                    'total': len(jobs),
                    'message': '获取职位列表成功'
                }
            else:
                message = response.get('message', '获取职位列表失败')
                logger.warning(f"⚠️ API 返回错误: {message}")

                return {
                    'success': False,
                    'jobs': [],
                    'total': 0,
                    'message': message
                }

        except Exception as e:
            logger.error(f"❌ 获取职位列表失败: {str(e)}")
            return {
                'success': False,
                'jobs': [],
                'total': 0,
                'message': f'获取失败: {str(e)}'
            }

    async def select_job_position(self, job_value: str) -> dict:
        """
        在推荐牛人页面选择指定的招聘职位

        职位选择器在 iframe (name="recommendFrame") 中

        Args:
            job_value: 职位的 value 属性值

        Returns:
            包含选择结果的字典
        """
        try:
            logger.info(f"🎯 选择职位: {job_value}")

            # 确保在推荐页面
            current_url = self.page.url
            if 'chat/recommend' not in current_url:
                logger.warning("⚠️ 当前不在推荐页面，导航到推荐页面...")
                await self.navigate_to_recommend_page()
                await AntiDetection.random_sleep(1, 2)

            # 等待页面加载
            logger.info("⏳ 等待页面加载完成...")
            await AntiDetection.random_sleep(2, 3)

            # 查找 recommendFrame iframe
            logger.info("🔍 查找 recommendFrame iframe...")
            recommend_frame = None

            for frame in self.page.frames:
                if frame.name == 'recommendFrame':
                    recommend_frame = frame
                    logger.info(f"✅ 找到 recommendFrame: {frame.url}")
                    break

            if not recommend_frame:
                logger.error("❌ 未找到 recommendFrame iframe")
                return {
                    'success': False,
                    'message': '未找到职位选择器iframe'
                }

            # 在 iframe 中查找职位选择器
            logger.info("🔍 在 iframe 中查找职位选择器...")
            trigger_selector = ".ui-dropmenu-label"

            try:
                trigger_element = await recommend_frame.wait_for_selector(trigger_selector, timeout=10000)
                logger.info("✅ 找到职位选择器触发器")
            except Exception as e:
                logger.error(f"❌ 未找到职位选择器触发器: {e}")
                return {
                    'success': False,
                    'message': '未找到职位选择器'
                }

            # 点击触发器打开下拉菜单
            logger.info("👆 点击职位选择器...")
            await trigger_element.click()
            await AntiDetection.random_sleep(1, 2)

            # 等待下拉菜单出现
            logger.info("🔍 等待下拉菜单出现...")
            try:
                await recommend_frame.wait_for_selector("ul li", timeout=5000)
                logger.info("✅ 下拉菜单已出现")
            except Exception as e:
                logger.error(f"❌ 下拉菜单未出现: {e}")
                return {
                    'success': False,
                    'message': '下拉菜单未出现'
                }

            # 获取所有 li 元素
            li_elements = await recommend_frame.query_selector_all("ul li")
            logger.info(f"📋 找到 {len(li_elements)} 个 li 元素")

            # 查找匹配的职位
            target_job = None
            available_jobs = []

            for li in li_elements:
                try:
                    value = await li.get_attribute("value")
                    if value:
                        text = await li.text_content()
                        label_text = text.strip() if text else ""

                        available_jobs.append({
                            'value': value,
                            'label': label_text
                        })

                        if value == job_value:
                            target_job = li
                            logger.info(f"✅ 找到目标职位: {label_text[:60]}...")
                except Exception as e:
                    logger.warning(f"⚠️ 处理 li 元素失败: {e}")
                    continue

            if not target_job:
                logger.error(f"❌ 未找到匹配的职位: {job_value}")
                logger.info(f"可用职位列表:")
                for job in available_jobs:
                    logger.info(f"  {job['value']} - {job['label'][:60]}...")

                # 关闭下拉菜单
                try:
                    await trigger_element.click()
                except:
                    pass

                return {
                    'success': False,
                    'message': f'未找到匹配的职位: {job_value}',
                    'available_jobs': available_jobs
                }

            # 点击选中职位
            logger.info("👆 点击选择职位...")
            await target_job.click()
            await AntiDetection.random_sleep(2, 3)

            logger.info("✅ 职位选择成功")
            return {
                'success': True,
                'message': '职位选择成功',
                'selected_job': job_value
            }

        except Exception as e:
            logger.error(f"❌ 选择职位失败: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f'选择失败: {str(e)}'
            }

    async def get_available_jobs(self) -> dict:
        """
        获取当前可用的招聘职位列表

        职位选择器在 iframe (name="recommendFrame") 中

        Returns:
            包含职位列表的字典
        """
        try:
            logger.info("📋 获取可用职位列表...")

            # 确保在推荐页面
            current_url = self.page.url
            logger.info(f"📍 当前URL: {current_url}")

            if 'chat/recommend' not in current_url:
                logger.info("⚠️ 当前不在推荐页面，导航到推荐页面...")
                await self.navigate_to_recommend_page()
                await AntiDetection.random_sleep(1, 2)
                current_url = self.page.url
                logger.info(f"📍 导航后URL: {current_url}")

            # 等待页面加载
            logger.info("⏳ 等待页面加载完成...")
            await AntiDetection.random_sleep(3, 5)

            # 查找 recommendFrame iframe
            logger.info("🔍 查找 recommendFrame iframe...")
            recommend_frame = None

            for frame in self.page.frames:
                if frame.name == 'recommendFrame':
                    recommend_frame = frame
                    logger.info(f"✅ 找到 recommendFrame: {frame.url}")
                    break

            if not recommend_frame:
                logger.error("❌ 未找到 recommendFrame iframe")
                return {
                    'success': False,
                    'jobs': [],
                    'message': '未找到职位选择器iframe，请刷新页面重试'
                }

            # 在 iframe 中查找职位选择器
            logger.info("🔍 在 iframe 中查找职位选择器...")
            trigger_selector = ".ui-dropmenu-label"

            try:
                trigger_element = await recommend_frame.wait_for_selector(trigger_selector, timeout=10000)
                logger.info("✅ 找到职位选择器触发器")
            except Exception as e:
                logger.error(f"❌ 未找到职位选择器触发器: {e}")
                return {
                    'success': False,
                    'jobs': [],
                    'message': '未找到职位选择器，请确保已创建招聘职位'
                }

            # 点击触发器打开下拉菜单
            logger.info("👆 点击触发器打开下拉菜单...")
            await trigger_element.click()
            await AntiDetection.random_sleep(1, 2)

            # 查找下拉列表中的所有 li 元素
            logger.info("🔍 查找下拉列表...")
            try:
                # 等待下拉列表出现
                await recommend_frame.wait_for_selector("ul li", timeout=5000)

                # 获取所有 li 元素
                li_elements = await recommend_frame.query_selector_all("ul li")
                logger.info(f"📋 找到 {len(li_elements)} 个 li 元素")

                jobs = []
                for idx, li in enumerate(li_elements):
                    try:
                        # 获取 value 属性
                        value = await li.get_attribute("value")

                        # 只处理有 value 的元素（过滤掉"推荐"、"新牛人"等选项）
                        if value:
                            # 获取文本内容
                            text = await li.text_content()
                            label_text = text.strip() if text else f"职位 {idx + 1}"

                            logger.info(f"  {len(jobs) + 1}. value={value}, label={label_text[:60]}...")

                            jobs.append({
                                'value': value,
                                'label': label_text
                            })
                    except Exception as e:
                        logger.warning(f"⚠️ 处理第 {idx + 1} 个 li 元素时出错: {str(e)}")
                        continue

                # 关闭下拉菜单（再次点击触发器）
                logger.info("👆 关闭下拉菜单...")
                try:
                    await trigger_element.click()
                    await AntiDetection.random_sleep(0.3, 0.5)
                except:
                    logger.warning("⚠️ 关闭下拉菜单失败，继续执行")

                logger.info(f"✅ 成功获取 {len(jobs)} 个职位")
                return {
                    'success': True,
                    'jobs': jobs,
                    'total': len(jobs),
                    'message': f'获取职位列表成功，共 {len(jobs)} 个职位'
                }

            except Exception as e:
                logger.error(f"❌ 获取下拉列表失败: {str(e)}")
                return {
                    'success': False,
                    'jobs': [],
                    'message': f'获取职位列表失败: {str(e)}'
                }

        except Exception as e:
            logger.error(f"❌ 获取职位列表失败: {str(e)}", exc_info=True)
            return {
                'success': False,
                'jobs': [],
                'message': f'获取失败: {str(e)}'
            }

    async def navigate_to_recommend_page(self) -> dict:
        """
        导航到推荐牛人列表页面

        Returns:
            包含导航结果的字典
        """
        try:
            logger.info("🔍 导航到推荐牛人页面...")

            # 首先尝试查找并点击推荐牛人菜单元素
            menu_selector = '#wrap > div.side-wrap.side-wrap-v2 > div > dl.menu-recommend'

            try:
                logger.info(f"⏳ 等待推荐菜单元素（最多5秒）...")
                # 等待元素出现，最多5秒
                menu_element = await self.page.wait_for_selector(menu_selector, timeout=5000)

                if menu_element:
                    logger.info("✅ 找到推荐菜单元素，点击进入...")
                    await menu_element.click()
                    await AntiDetection.random_sleep(1, 2)

                    # 等待页面加载
                    await self.page.wait_for_load_state('networkidle')

                    current_url = self.page.url
                    logger.info(f"📍 当前页面: {current_url}")

                    return {
                        'success': True,
                        'method': 'click',
                        'url': current_url,
                        'message': '通过点击菜单进入推荐页面'
                    }

            except Exception as e:
                logger.warning(f"⚠️ 未找到推荐菜单元素或点击失败: {str(e)}")
                logger.info("🔄 尝试直接访问推荐页面URL...")

                # 如果找不到元素，直接访问URL
                recommend_url = "https://www.zhipin.com/web/chat/recommend"
                await self.page.goto(recommend_url, wait_until='networkidle', timeout=30000)
                await AntiDetection.random_sleep(1, 2)

                current_url = self.page.url
                logger.info(f"📍 当前页面: {current_url}")

                return {
                    'success': True,
                    'method': 'direct_url',
                    'url': current_url,
                    'message': '直接访问推荐页面URL'
                }

        except Exception as e:
            logger.error(f"❌ 导航到推荐页面失败: {str(e)}")
            return {
                'success': False,
                'method': 'error',
                'url': '',
                'message': f'导航失败: {str(e)}'
            }

    async def _save_account_info(self, api_response: dict):
        """
        保存账号信息到数据库

        Args:
            api_response: Boss直聘 h5/user/info API的响应数据
        """
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://localhost:27421/api/accounts/save-from-api",
                    json=api_response,
                    timeout=10.0
                )
                response.raise_for_status()
                result = response.json()
                logger.info(f"✅ 账号信息保存成功: {result.get('message')}")

                # 获取账号数据
                account = result.get('account', {})

                # 更新当前账号的com_id和auth_file_path
                com_id = account.get('com_id')
                if com_id:
                    self.current_com_id = com_id
                    self.auth_file = self.get_auth_file_path(self.current_com_id)
                    logger.info(f"📝 更新当前账号: com_id={com_id}, auth_file={self.auth_file}")

                    # 更新数据库中的auth_file_path
                    try:
                        async with httpx.AsyncClient() as update_client:
                            await update_client.put(
                                f"http://localhost:27421/api/accounts/{account['id']}",
                                json={"auth_file_path": self.auth_file},
                                timeout=5.0
                            )
                            logger.info(f"✅ 已更新数据库中的auth_file_path")
                    except Exception as e:
                        logger.warning(f"⚠️ 更新auth_file_path失败: {str(e)}")

                return result
        except Exception as e:
            logger.error(f"❌ 保存账号信息失败: {str(e)}")
            raise

    @staticmethod
    def get_auth_file_path(com_id: int) -> str:
        """
        根据com_id生成登录状态文件路径

        Args:
            com_id: 公司ID

        Returns:
            登录状态文件路径
        """
        return f"boss_auth_{com_id}.json"

    async def switch_account(self, com_id: int) -> dict:
        """
        切换到指定账号

        Args:
            com_id: 要切换到的账号的com_id

        Returns:
            包含切换结果的字典
        """
        try:
            logger.info(f"🔄 切换账号: com_id={com_id}")

            # 更新auth文件路径
            new_auth_file = self.get_auth_file_path(com_id)
            if not new_auth_file:
                return {
                    'success': False,
                    'message': '登录状态文件路径为空',
                    'needs_login': True
                }

            # 检查登录状态文件是否存在
            if not os.path.exists(new_auth_file):
                logger.warning(f"⚠️ 登录状态文件不存在: {new_auth_file}")
                return {
                    'success': False,
                    'message': '该账号未保存登录状态，请先登录',
                    'needs_login': True
                }

            # 关闭当前context和page
            if self.page:
                await self.page.close()
                self.page = None

            if self.context:
                await self.context.close()
                self.context = None

            # 更新当前账号信息
            self.current_com_id = com_id
            self.auth_file = new_auth_file

            # 创建新的context（加载新账号的登录状态）
            context_options = {
                'viewport': {'width': 1920, 'height': 1080},
                'user_agent': AntiDetection.get_random_user_agent(),
                'storage_state': self.auth_file
            }

            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()

            # 注入反检测脚本
            await AntiDetection.inject_anti_detection_script(self.page)

            # 验证登录状态
            logger.info("🔍 验证新账号登录状态...")
            await self.page.goto(self.base_url, wait_until='networkidle', timeout=30000)
            await AntiDetection.random_sleep(1, 2)

            try:
                api_url = "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json"
                response = await self.page.evaluate(f'''
                    async () => {{
                        const response = await fetch("{api_url}");
                        return await response.json();
                    }}
                ''')

                if response.get('code') == 0:
                    zp_data = response.get('zpData', {})
                    user_info = {
                        'comId': zp_data.get('baseInfo', {}).get('comId'),
                        'name': zp_data.get('baseInfo', {}).get('name'),
                        'showName': zp_data.get('baseInfo', {}).get('showName'),
                    }

                    # 验证comId是否匹配
                    if user_info.get('comId') == com_id:
                        self.is_logged_in = True
                        logger.info(f"✅ 账号切换成功: {user_info.get('showName')}")
                        return {
                            'success': True,
                            'message': '账号切换成功',
                            'user_info': user_info
                        }
                    else:
                        logger.error(f"❌ 账号不匹配: 期望 {com_id}, 实际 {user_info.get('comId')}")
                        return {
                            'success': False,
                            'message': '账号信息不匹配',
                            'needs_login': True
                        }
                else:
                    logger.warning("⚠️ 登录状态已失效")
                    return {
                        'success': False,
                        'message': '登录状态已失效，请重新登录',
                        'needs_login': True
                    }
            except Exception as e:
                logger.error(f"❌ 验证登录状态失败: {str(e)}")
                return {
                    'success': False,
                    'message': f'验证失败: {str(e)}',
                    'needs_login': True
                }

        except Exception as e:
            logger.error(f"❌ 切换账号失败: {str(e)}")
            return {
                'success': False,
                'message': f'切换失败: {str(e)}'
            }

    async def cleanup(self):
        """清理资源，关闭浏览器"""
        logger.info("🔚 清理资源...")

        try:
            # CDP 附加到“真实 Chrome”时：只断开引用，不要关闭真实浏览器。
            # 否则前端看起来像“浏览器自己关了”（实际是后端 cleanup 把会话关掉）。
            if self._manual_attached_via_cdp:
                self.page = None
                self.context = None
                self.browser = None
                return

            # launch_persistent_context：只关 context 即可，避免先关 page 再关 browser 导致异常/锁残留
            if self._using_persistent_profile and self.context:
                await self.context.close()
                self.context = None
                self.page = None
                self.browser = None
            else:
                if self.page:
                    try:
                        await self.page.close()
                    except Exception:
                        pass
                    self.page = None
                if self.context:
                    try:
                        await self.context.close()
                    except Exception:
                        pass
                    self.context = None
                if self.browser:
                    try:
                        await self.browser.close()
                    except Exception:
                        pass
                    self.browser = None
        finally:
            self._using_persistent_profile = False
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None

        logger.info("✅ 资源清理完成")

    async def __aenter__(self):
        """上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        await self.cleanup()
