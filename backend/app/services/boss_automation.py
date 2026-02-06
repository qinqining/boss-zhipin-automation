"""
Boss 直聘自动化核心服务
基于 Playwright 实现浏览器自动化
"""
import os
import asyncio
import logging
from typing import Optional, Dict, List
from datetime import datetime
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

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

        # 配置项
        self.base_url = "https://www.zhipin.com"
        # 如果指定了com_id，使用对应的auth文件；否则不加载任何认证文件（空cookies）
        self.auth_file = self.get_auth_file_path(com_id) if com_id else None

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

            # 启动 Playwright
            self.playwright = await async_playwright().start()

            # 启动浏览器
            logger.info(f"🖥️ 启动 Chromium 浏览器，headless={headless}，显示窗口={'否' if headless else '是'}")
            self.browser = await self.playwright.chromium.launch(
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-infobars',
                    '--start-maximized',  # 启动时最大化
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-popup-blocking',
                    '--disable-features=TranslateUI',
                ]
            )

            # 创建浏览器上下文
            # 不设置固定 viewport，让浏览器窗口自适应
            context_options = {
                'viewport': None,  # 不限制 viewport，跟随窗口大小
                'user_agent': AntiDetection.get_random_user_agent(),
                'locale': 'zh-CN',
                'timezone_id': 'Asia/Shanghai',
                'geolocation': {'latitude': 39.9042, 'longitude': 116.4074},  # 北京
                'permissions': ['geolocation'],
                'color_scheme': 'light',
                'device_scale_factor': 1,
                'is_mobile': False,
                'has_touch': False,
                'java_script_enabled': True,
                'bypass_csp': True,
                'ignore_https_errors': True,
            }

            # 如果指定了auth_file且文件存在，则加载已保存的登录状态
            if self.auth_file and os.path.exists(self.auth_file):
                logger.info(f"📂 加载已保存的登录状态: {self.auth_file}")
                context_options['storage_state'] = self.auth_file
            else:
                logger.info("🆕 使用空白状态初始化浏览器（无登录信息）")

            self.context = await self.browser.new_context(**context_options)

            # 创建新页面
            self.page = await self.context.new_page()

            # 注入反检测脚本
            await AntiDetection.inject_anti_detection_script(self.page)

            logger.info("✅ 浏览器初始化成功")

            if skip_auto_navigate:
                # 手动模式：先访问首页
                logger.info("📌 手动模式：启动浏览器")
                try:
                    await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=20000)
                    logger.info("✅ 已打开 Boss 直聘首页")
                except Exception as e:
                    logger.warning(f"⚠️ 访问首页失败: {str(e)}")

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

            # 获取当前URL
            current_url = self.page.url
            logger.info(f"📍 当前页面（准备前）: {current_url}")

            # 访问首页（带重试逻辑）
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.info(f"🌐 尝试访问首页 (尝试 {attempt + 1}/{max_retries})...")
                    await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=20000)
                    logger.info(f"✅ 首页加载成功")
                    break
                except Exception as e:
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
                await login_button.click()
                await self.page.wait_for_load_state('networkidle')
                await AntiDetection.random_sleep(1, 2)

                # 检查是否跳转到登录页面
                current_url = self.page.url
                logger.info(f"📍 当前页面: {current_url}")

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
                            await self.context.storage_state(path=self.auth_file)
                            logger.info("✅ 已登录状态验证成功")

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
                        await self.page.wait_for_selector(qrcode_switch_selector, timeout=5000)
                        await self.page.click(qrcode_switch_selector)
                        await AntiDetection.random_sleep(1, 2)
                        logger.info("✅ 已切换到二维码登录模式")
                    except Exception as e:
                        logger.warning(f"⚠️ 切换二维码登录失败（可能已经是二维码模式）: {str(e)}")

                    # 等待二维码加载
                    qrcode_img_selector = '#wrap > div > div.login-entry-page > div.login-register-content > div.scan-app-wrapper > div.qr-code-box > div.qr-img-box > img'
                    try:
                        logger.info("⏳ 等待二维码加载...")
                        await self.page.wait_for_selector(qrcode_img_selector, timeout=10000)
                        await AntiDetection.random_sleep(0.5, 1)
                        logger.info("✅ 二维码已加载到页面")

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
                        await self.context.storage_state(path=self.auth_file)

                        # 导航到推荐页面
                        await self.navigate_to_recommend_page()

                        return {
                            'success': True,
                            'already_logged_in': True,
                            'message': '已登录'
                        }
                    else:
                        # 登录已失效，导航到登录页面
                        logger.warning("⚠️ 登录已失效，导航到登录页面...")
                        self.is_logged_in = False

                        # 清除过期状态
                        if os.path.exists(self.auth_file):
                            os.remove(self.auth_file)
                        await self.context.clear_cookies()

                        # 直接导航到登录页面（带重试逻辑）
                        login_url = f"{self.base_url}/web/user/?ka=header-login"
                        for attempt in range(3):
                            try:
                                logger.info(f"🌐 尝试访问登录页面 (尝试 {attempt + 1}/3)...")
                                await self.page.goto(login_url, wait_until='domcontentloaded', timeout=20000)
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
                    return {
                        'success': False,
                        'message': f'验证登录失败: {str(e)}'
                    }

        except Exception as e:
            logger.error(f"❌ 准备登录页面失败: {str(e)}")
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

            # 如果不在登录页面，重新准备登录页面
            if 'zhipin.com/web/user/' not in current_url:
                logger.info("⚠️ 当前不在登录页面，重新准备登录页面...")
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
                    return prepare_result

                # 更新当前URL
                current_url = self.page.url
                logger.info(f"📍 准备后页面: {current_url}")

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
                    await self.context.storage_state(path=self.auth_file)
                    logger.info(f"💾 登录状态已保存: {self.auth_file}")

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

        if self.page:
            await self.page.close()
            self.page = None

        if self.context:
            await self.context.close()
            self.context = None

        if self.browser:
            await self.browser.close()
            self.browser = None

        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

        logger.info("✅ 资源清理完成")

    async def __aenter__(self):
        """上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        await self.cleanup()
