"""
打招呼自动化服务
管理打招呼任务的执行、状态和日志
"""
import asyncio
import logging
import random
import os
import json
from typing import Optional, Dict, List
from datetime import datetime
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# 日志目录
LOGS_DIR = Path(__file__).parent.parent.parent / "logs" / "greeting"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> float:
    """生成随机延迟时间（秒）

    Args:
        min_seconds: 最小延迟秒数
        max_seconds: 最大延迟秒数

    Returns:
        随机延迟时间
    """
    return random.uniform(min_seconds, max_seconds)


class GreetingTaskManager:
    """打招呼任务管理器（单例）"""

    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.status: str = "idle"  # idle, running, completed, error, limit_reached
        self.target_count: int = 0
        self.current_index: int = 0
        self.success_count: int = 0
        self.failed_count: int = 0
        self.skipped_count: int = 0
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.logs: deque = deque(maxlen=500)  # 增加到500条日志
        self.error_message: Optional[str] = None
        self.limit_reached: bool = False  # 是否触发打招呼限制

        # 打招呼自动化对象
        self.automation = None

        # 期望职位列表（用于职位匹配筛选）
        self.expected_positions: List[str] = []

        # 日志文件路径（每次任务创建新文件）
        self.log_file_path: Optional[Path] = None

    def add_log(self, level: str, message: str):
        """添加日志（同时保存到内存和文件）"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        }
        self.logs.append(log_entry)

        # 同时输出到标准日志
        if level == "INFO":
            logger.info(message)
        elif level == "WARNING":
            logger.warning(message)
        elif level == "ERROR":
            logger.error(message)

        # 写入日志文件（持久化）
        if self.log_file_path:
            try:
                with open(self.log_file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"写入日志文件失败: {e}")

    def get_status(self) -> Dict:
        """获取当前状态"""
        elapsed_time = None
        if self.start_time:
            end = self.end_time or datetime.now()
            elapsed_time = (end - self.start_time).total_seconds()

        return {
            "status": self.status,
            "target_count": self.target_count,
            "current_index": self.current_index,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "progress": min((self.success_count / self.target_count * 100) if self.target_count > 0 else 0, 100),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "elapsed_time": elapsed_time,
            "error_message": self.error_message
        }

    def get_logs(self, last_n: int = 50) -> List[Dict]:
        """获取最近的日志"""
        return list(self.logs)[-last_n:]

    async def start_greeting_task(self, target_count: int, automation_service=None, expected_positions: List[str] = None):
        """启动打招呼任务

        Args:
            target_count: 目标打招呼数量
            automation_service: 已初始化的BossAutomation实例（复用已打开的浏览器）
            expected_positions: 期望职位关键词列表（包含匹配）
        """
        if self.status == "running":
            raise RuntimeError("任务已在运行中")

        # 重置状态
        self.reset()
        self.status = "running"
        self.target_count = target_count
        self.start_time = datetime.now()

        # 创建日志文件（按时间戳命名）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file_path = LOGS_DIR / f"greeting_{timestamp}.log"
        logger.info(f"📝 日志文件: {self.log_file_path}")

        # 保存期望职位列表
        if expected_positions:
            self.expected_positions = expected_positions
            self.add_log("INFO", f"🎯 启用职位匹配筛选，关键词: {', '.join(expected_positions)}")

        self.add_log("INFO", f"🚀 开始打招呼任务，目标数量: {target_count}")

        # 保存自动化服务引用（复用已有浏览器）
        self.automation = automation_service

        # 创建后台任务
        self.task = asyncio.create_task(self._run_greeting_task(target_count))

    async def _run_greeting_task(self, target_count: int):
        """执行打招呼任务（后台运行）"""
        try:
            if not self.automation:
                raise RuntimeError("自动化服务未初始化，请先在向导中初始化浏览器")

            if not self.automation.page:
                raise RuntimeError("浏览器页面不可用")

            self.add_log("INFO", f"✅ 使用已打开的浏览器")
            self.add_log("INFO", f"目标：成功打招呼 {target_count} 个候选人")

            # 获取当前页面的iframe
            recommend_frame = None
            for frame in self.automation.page.frames:
                if frame.name == 'recommendFrame':
                    recommend_frame = frame
                    break

            if not recommend_frame:
                raise RuntimeError("未找到recommendFrame，请确保在推荐页面")

            self.add_log("INFO", "✅ 找到推荐页面iframe")

            # 逐个处理候选人，直到成功打招呼达到目标数量
            # 使用已处理集合追踪（解决虚拟滚动问题）
            processed_ids = set()  # 已处理候选人ID集合（名字+期望职位）
            no_new_candidate_count = 0  # 连续无新候选人计数
            # 动态设置最大尝试次数：目标数量的3倍，最少100，最多1000
            max_attempts = min(max(target_count * 3, 100), 1000)
            self.add_log("INFO", f"📊 目标成功数: {target_count}, 最多尝试: {max_attempts} 个候选人")

            while self.success_count < target_count and len(processed_ids) < max_attempts:
                try:
                    # 获取当前可见的所有候选人卡片
                    cards = await recommend_frame.locator('ul.card-list > li').all()

                    # 找第一个未处理的候选人
                    card = None
                    candidate_name = None
                    candidate_id = None
                    for c in cards:
                        try:
                            name_el = c.locator('.name').first
                            if await name_el.count() > 0:
                                name = await name_el.inner_text()
                                # 提取期望职位用于组合ID（避免重名冲突）
                                expected_pos = await self._extract_expected_position(c)
                                cid = f"{name}|{expected_pos or ''}"
                                if cid not in processed_ids:
                                    card = c
                                    candidate_name = name
                                    candidate_id = cid
                                    break
                        except:
                            continue

                    # 如果没找到未处理的候选人，滚动加载更多
                    if card is None:
                        no_new_candidate_count += 1
                        if no_new_candidate_count >= 3:
                            self.add_log("WARNING", "⚠️ 连续3次滚动未找到新候选人，可能已到达列表末尾")
                            self.add_log("INFO", "🔄 刷新页面后继续执行任务...")

                            # 刷新页面
                            await self.automation.page.reload()
                            await asyncio.sleep(3)  # 等待页面加载

                            # 重新获取 recommendFrame
                            recommend_frame = None
                            for frame in self.automation.page.frames:
                                if frame.name == 'recommendFrame':
                                    recommend_frame = frame
                                    break

                            if not recommend_frame:
                                self.add_log("ERROR", "❌ 刷新后未找到recommendFrame，停止任务")
                                break

                            self.add_log("INFO", "✅ 页面刷新成功，继续执行任务")
                            no_new_candidate_count = 0  # 重置计数
                            await asyncio.sleep(2)  # 额外等待确保页面稳定
                            continue

                        self.add_log("INFO", "📜 滚动加载更多候选人...")
                        await recommend_frame.evaluate("""
                            window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'smooth' });
                        """)
                        await asyncio.sleep(2)
                        continue

                    no_new_candidate_count = 0  # 重置计数
                    processed_ids.add(candidate_id)  # 标记为已处理
                    self.current_index = len(processed_ids)

                    self.add_log("INFO", f"📍 处理候选人 #{len(processed_ids)} {candidate_name} (已成功: {self.success_count}/{target_count})")

                    # 职位匹配筛选（如果启用）
                    if self.expected_positions:
                        # 从候选人ID中提取期望职位（已在查找时提取）
                        expected_pos = candidate_id.split('|')[1] if '|' in candidate_id else None

                        if not expected_pos:
                            # 候选人没有期望职位信息，跳过
                            self.skipped_count += 1
                            self.add_log("WARNING", f"⏭️  {candidate_name}: 无期望职位信息，已跳过")
                            continue

                        # 检查期望职位是否匹配
                        display_pos = expected_pos.replace('|', ' / ')
                        if not self._match_position(expected_pos, self.expected_positions):
                            # 职位不匹配，跳过
                            self.skipped_count += 1
                            self.add_log("INFO", f"⏭️  {candidate_name}: 期望职位不匹配({display_pos})，已跳过")
                            continue

                        # 职位匹配，记录日志
                        self.add_log("INFO", f"✅ {candidate_name}: 期望职位匹配({display_pos})")

                    self.add_log("INFO", f"🖱️  准备点击候选人: {candidate_name}")

                    # 确保没有对话框阻挡
                    if await self._ensure_no_blocking_dialogs(recommend_frame):
                        self.add_log("INFO", "已清理阻挡的对话框")
                        await asyncio.sleep(0.5)  # 额外延迟确保DOM稳定

                    # 点击候选人卡片，带重试机制
                    click_success = False
                    try:
                        await card.click()
                        self.add_log("INFO", f"✅ 已点击候选人: {candidate_name}")
                        click_success = True
                    except Exception as e:
                        error_str = str(e)
                        self.add_log("ERROR", f"❌ 点击候选人失败: {error_str}")

                        # 检测是否是对话框拦截错误
                        if 'intercept' in error_str.lower() or 'covering' in error_str.lower() or 'pointer-events' in error_str.lower():
                            self.add_log("WARNING", "检测到对话框阻挡，尝试清理并重试...")
                            # 再次清理对话框
                            await self._ensure_no_blocking_dialogs(recommend_frame)
                            await asyncio.sleep(1.0)

                            # 重试一次
                            try:
                                await card.click()
                                self.add_log("INFO", "✅ 重试点击成功")
                                click_success = True
                            except Exception as retry_error:
                                self.add_log("ERROR", f"❌ 重试失败: {str(retry_error)}")

                    # 如果点击失败，跳过此候选人
                    if not click_success:
                        self.failed_count += 1
                        self.add_log("ERROR", f"❌ 跳过候选人 {card_index}（点击失败）")
                        continue

                    # 随机延迟：模拟人类点击后的等待（1-2秒）
                    delay = random_delay(1.0, 2.0)
                    await asyncio.sleep(delay)

                    # 等待简历面板加载
                    await recommend_frame.wait_for_selector('.dialog-lib-resume', timeout=10000)
                    self.add_log("INFO", "✅ 简历面板已加载")

                    # 随机延迟：模拟人类阅读简历的时间（2-4秒）
                    delay = random_delay(2.0, 4.0)
                    self.add_log("INFO", f"📖 阅读简历... ({delay:.1f}秒)")
                    await asyncio.sleep(delay)

                    # 查找并点击打招呼按钮
                    button_selectors = [
                        '.dialog-lib-resume .button-list-wrap button',
                        '.dialog-lib-resume .communication button',
                        '.resume-right-side .communication button',
                    ]

                    button_found = False
                    already_contacted = False
                    for selector in button_selectors:
                        try:
                            button = recommend_frame.locator(selector).first
                            if await button.count() > 0 and await button.is_visible():
                                text = await button.inner_text()
                                self.add_log("INFO", f"找到按钮: '{text}'")

                                # 检查是否为"继续沟通"，如果是则跳过
                                if '继续沟通' in text:
                                    self.add_log("INFO", f"⏭️  {candidate_name}: 已打过招呼（按钮显示: {text}），跳过")
                                    already_contacted = True
                                    button_found = False
                                    break

                                # 随机延迟：模拟人类决策时间（0.5-1.5秒）
                                delay = random_delay(0.5, 1.5)
                                await asyncio.sleep(delay)

                                await button.click()
                                self.add_log("INFO", f"✅ 已点击【{text}】按钮")
                                button_found = True
                                break
                        except:
                            continue

                    if not button_found and not already_contacted:
                        self.add_log("WARNING", "⚠️ 未找到打招呼按钮，可能已经打过招呼")

                    # 随机延迟：等待按钮状态变化和服务器响应
                    if already_contacted:
                        # 已打过招呼，快速关闭（0.5-1秒）
                        delay = random_delay(0.5, 1.0)
                    else:
                        # 正常情况，等待服务器响应（2-3秒）
                        delay = random_delay(2.0, 3.0)
                    await asyncio.sleep(delay)

                    # 检测是否出现打招呼限制弹窗
                    if button_found and await self._check_limit_dialog():
                        self.add_log("WARNING", "⚠️ 检测到打招呼限制弹窗，任务停止")
                        self.limit_reached = True
                        self.status = "limit_reached"
                        break  # 跳出循环，结束任务

                    # 点击关闭按钮
                    close_selectors = [
                        '.dialog-lib-resume .close-icon',
                        '.dialog-lib-resume .boss-popup__close',
                        'button.boss-popup__close',
                    ]

                    close_success = False
                    for selector in close_selectors:
                        try:
                            close_btn = recommend_frame.locator(selector).first
                            if await close_btn.count() > 0 and await close_btn.is_visible():
                                # 随机延迟：模拟人类找关闭按钮的时间（0.3-0.8秒）
                                delay = random_delay(0.3, 0.8)
                                await asyncio.sleep(delay)

                                await close_btn.click()
                                self.add_log("INFO", "✅ 已点击关闭按钮")

                                # 等待对话框完全消失
                                try:
                                    await recommend_frame.locator('.dialog-lib-resume').wait_for(
                                        state='hidden',
                                        timeout=2000
                                    )
                                    self.add_log("INFO", "✅ 简历面板已完全关闭")
                                    close_success = True
                                except:
                                    # 超时，但认为关闭成功
                                    self.add_log("INFO", "⏱️ 对话框关闭超时，继续执行")
                                    close_success = True

                                break
                        except Exception as e:
                            logger.warning(f"关闭对话框失败: {e}")
                            continue

                    if not close_success:
                        self.add_log("WARNING", "⚠️ 未能关闭简历面板")
                        # 强制等待，给对话框时间关闭
                        await asyncio.sleep(2.0)

                    # 随机延迟：模拟人类返回列表后的思考时间（1-2秒）
                    delay = random_delay(1.0, 2.0)
                    await asyncio.sleep(delay)

                    if button_found:
                        self.success_count += 1
                        self.add_log("INFO", f"✅ 候选人 {self.current_index} 处理成功")
                    elif already_contacted:
                        self.skipped_count += 1
                        self.add_log("INFO", f"⏭️  候选人 {self.current_index} 已跳过（已打过招呼）")
                    else:
                        self.failed_count += 1
                        self.add_log("WARNING", f"⚠️ 候选人 {self.current_index} 处理失败")

                except Exception as e:
                    self.failed_count += 1
                    self.add_log("ERROR", f"❌ 候选人 {self.current_index} 出错: {str(e)}")
                    logger.error(f"处理候选人 {self.current_index} 时出错", exc_info=True)

            # 任务完成
            if not self.limit_reached:
                self.status = "completed"
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time).total_seconds()

            total_processed = len(processed_ids)
            if self.limit_reached:
                self.add_log("INFO", f"⚠️ 任务已停止（触发打招呼限制）")
            else:
                self.add_log("INFO", f"🎉 任务完成！")
            self.add_log("INFO", f"✅ 成功: {self.success_count} 个 (目标: {target_count})")
            self.add_log("INFO", f"❌ 失败: {self.failed_count} 个")
            if self.skipped_count > 0:
                self.add_log("INFO", f"⏭️  跳过: {self.skipped_count} 个")
            self.add_log("INFO", f"📊 共处理: {total_processed} 个候选人")
            self.add_log("INFO", f"⏱️  耗时: {elapsed:.1f}秒")

            # 发送钉钉通知
            await self._send_notification(total_processed, elapsed)

            # 保存任务摘要到日志文件
            self._save_task_summary(total_processed, elapsed)

        except Exception as e:
            self.status = "error"
            self.error_message = str(e)
            self.end_time = datetime.now()
            self.add_log("ERROR", f"❌ 任务失败: {str(e)}")
            logger.error(f"打招呼任务失败: {e}", exc_info=True)

        finally:
            # 确保状态被清理，防止僵尸任务
            if self.status == "running":
                self.status = "error"
                self.error_message = "任务异常中断"
                self.end_time = datetime.now()
                logger.warning("⚠️ 任务在finally块中被清理，可能发生了未捕获的异常")

            # 不再自动重置，保留任务状态和日志供用户查看
            # 用户可以直接点击"开始打招呼"按钮开始新任务
            self.add_log("INFO", "💡 任务已结束，可直接点击「开始打招呼」按钮开始新任务")

            # 不要关闭浏览器，因为是复用的全局实例

    async def _extract_expected_position(self, card) -> Optional[str]:
        """
        从候选人卡片提取期望职位（求职期望行的全部文本）
        直接获取 .expect-wrap 行的完整文本内容，用于关键词匹配

        Args:
            card: Playwright locator对象，候选人卡片

        Returns:
            期望职位完整文本，如果提取失败则返回None
        """
        try:
            result = await card.evaluate("""
                (el) => {
                    // 获取 expect-wrap 行元素
                    const expectRow = el.querySelector('.row-flex.expect-wrap');
                    if (expectRow) {
                        // 获取该行内所有 span.content 元素的文本（可能有多个同级 span.content）
                        const contentSpans = expectRow.querySelectorAll('span.content');
                        if (contentSpans.length > 0) {
                            const texts = [];
                            for (const span of contentSpans) {
                                const text = span.textContent.trim();
                                if (text) texts.push(text);
                            }
                            if (texts.length > 0) return texts.join('|');
                        }
                        // 回退：直接取整行文本
                        const text = expectRow.textContent.trim();
                        if (text) return text;
                    }

                    // 旧版DOM回退: .join-text-wrap
                    const joinWrap = el.querySelector('.row-flex .content .join-text-wrap');
                    if (joinWrap) {
                        const text = joinWrap.textContent.trim();
                        if (text) return text;
                    }

                    return null;
                }
            """)

            return result if result else None

        except Exception as e:
            logger.warning(f"提取期望职位失败: {str(e)}")

        return None

    def _match_position(self, candidate_pos: str, expected_list: List[str]) -> bool:
        """
        包含匹配：候选人期望职位文本包含任一配置关键词即匹配
        candidate_pos 可能是 | 分隔的多段文本（城市|薪资|职位等）

        Args:
            candidate_pos: 候选人的期望职位文本（可能含 | 分隔符）
            expected_list: 期望职位关键词列表

        Returns:
            是否匹配
        """
        if not candidate_pos or not expected_list:
            return False

        # 将 | 分隔的文本拆开逐段匹配
        segments = candidate_pos.split('|')
        for expected in expected_list:
            expected_lower = expected.lower()
            for segment in segments:
                if expected_lower in segment.lower():
                    return True

        return False

    async def _ensure_no_blocking_dialogs(self, frame) -> bool:
        """
        确保没有对话框阻挡操作
        检测并关闭所有可能阻挡点击的对话框（简历面板、限制弹窗等）

        Args:
            frame: Playwright frame对象，通常是recommendFrame

        Returns:
            如果检测到并关闭了对话框返回 True，否则返回 False
        """
        try:
            # 检测所有可能的对话框类型
            # 格式：(对话框选择器, 关闭按钮选择器)
            dialog_selectors = [
                ('.dialog-lib-resume', '.close-icon, .boss-popup__close'),  # 简历对话框
                ('.business-block-dialog', '.boss-popup__close'),           # 限制弹窗
                ('[data-type="boss-dialog"]', '.close-icon'),               # 通用Boss对话框
                ('.dialog-wrap.active', '.close-icon'),                     # 活动对话框
            ]

            for dialog_sel, close_sel in dialog_selectors:
                dialog = frame.locator(dialog_sel).first
                if await dialog.count() > 0 and await dialog.is_visible():
                    logger.info(f"检测到对话框: {dialog_sel}")

                    # 尝试关闭
                    close_btn = dialog.locator(close_sel).first
                    if await close_btn.count() > 0 and await close_btn.is_visible():
                        await close_btn.click()
                        logger.info("已点击关闭按钮，等待对话框消失...")

                        # 等待对话框消失
                        try:
                            await dialog.wait_for(state='hidden', timeout=2000)
                            logger.info("✅ 对话框已完全关闭")
                            return True
                        except:
                            # 超时，增加额外延迟
                            await asyncio.sleep(1.0)
                            logger.warning("⏱️ 等待对话框关闭超时，继续执行")
                            return True

            return False

        except Exception as e:
            logger.warning(f"检测对话框时出错: {e}")
            return False

    async def _check_limit_dialog(self) -> bool:
        """
        检测是否出现打招呼限制弹窗

        弹窗出现在主页面（非iframe内），包含特定的class和文本
        使用多策略检测以提高可靠性：
        1. class选择器检测 (.business-block-dialog 等)
        2. 关键词文本匹配验证

        Returns:
            是否检测到限制弹窗
        """
        try:
            if not self.automation or not self.automation.page:
                return False

            # 策略1: 使用类选择器检测限制弹窗（最可靠）
            # 注意：dialog ID是动态生成的，不能依赖具体ID
            selectors = [
                '.business-block-dialog',
                '.business-block-wrap',
                '[class*="business-block"]'
            ]

            for selector in selectors:
                try:
                    dialog = self.automation.page.locator(selector).first
                    if await dialog.count() > 0:
                        is_visible = await dialog.is_visible()
                        if is_visible:
                            # 验证文本内容以确保是限制弹窗
                            text = await dialog.inner_text()
                            if '主动沟通' in text and ('上限' in text or '限制' in text):
                                logger.info(f"✅ 使用选择器 '{selector}' 检测到限制弹窗")
                                return True
                except Exception:
                    continue

            # 策略2: 关键词搜索（备用方案）
            result = await self.automation.page.evaluate("""
                () => {
                    const keywords = ['主动沟通', '上限', '达上限', '需付费'];
                    const allElements = document.querySelectorAll('[class*="dialog"], [class*="popup"]');

                    for (const el of allElements) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') {
                            continue;
                        }

                        const text = el.textContent || '';
                        let matchCount = 0;
                        for (const keyword of keywords) {
                            if (text.includes(keyword)) {
                                matchCount++;
                            }
                        }

                        // 如果匹配到至少2个关键词，认为是限制弹窗
                        if (matchCount >= 2) {
                            return true;
                        }
                    }
                    return false;
                }
            """)

            if result:
                logger.info("✅ 使用关键词搜索检测到限制弹窗")
                return True

            return False

        except Exception as e:
            logger.error(f"检测限制弹窗时出错: {e}")
            return False

    def _save_task_summary(self, total_processed: int, elapsed_time: float):
        """保存任务摘要到日志文件"""
        if not self.log_file_path:
            return

        try:
            summary = {
                "type": "SUMMARY",
                "timestamp": datetime.now().isoformat(),
                "status": self.status,
                "target_count": self.target_count,
                "success_count": self.success_count,
                "failed_count": self.failed_count,
                "skipped_count": self.skipped_count,
                "total_processed": total_processed,
                "elapsed_time": elapsed_time,
                "limit_reached": self.limit_reached,
                "expected_positions": self.expected_positions,
            }
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 50 + "\n")
                f.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            logger.info(f"📝 任务摘要已保存到: {self.log_file_path}")
        except Exception as e:
            logger.error(f"保存任务摘要失败: {e}")

    async def _send_notification(self, total_processed: int, elapsed_time: float):
        """
        发送钉钉通知

        Args:
            total_processed: 总处理数
            elapsed_time: 耗时（秒）
        """
        try:
            from app.database import async_session_maker
            from app.models.notification_config import NotificationConfig
            from app.services.notification_service import NotificationService
            from sqlmodel import select

            # 获取通知配置
            async with async_session_maker() as session:
                result = await session.execute(
                    select(NotificationConfig).limit(1)
                )
                config = result.scalar_one_or_none()

                if not config or not config.dingtalk_enabled:
                    return

                # 创建通知服务
                notification_service = NotificationService(config)

                # 根据任务状态发送不同的通知
                if self.limit_reached:
                    await notification_service.send_limit_reached_notification(
                        success_count=self.success_count,
                        failed_count=self.failed_count,
                        skipped_count=self.skipped_count,
                        total_processed=total_processed
                    )
                elif self.status == "completed":
                    await notification_service.send_task_completion_notification(
                        success_count=self.success_count,
                        failed_count=self.failed_count,
                        skipped_count=self.skipped_count,
                        total_processed=total_processed,
                        elapsed_time=elapsed_time
                    )
                elif self.status == "error" and self.error_message:
                    await notification_service.send_error_notification(
                        error_message=self.error_message
                    )

        except Exception as e:
            logger.error(f"发送钉钉通知失败: {e}")

    async def stop_task(self):
        """停止任务"""
        if self.task and not self.task.done():
            self.task.cancel()
            self.status = "cancelled"
            self.end_time = datetime.now()
            self.add_log("WARNING", "⚠️ 任务已被用户停止")

            # 不要关闭浏览器，因为是复用的全局实例

    def is_stale(self, timeout_minutes: int = 30) -> bool:
        """
        检查任务是否超时未完成

        Args:
            timeout_minutes: 超时分钟数，默认30分钟

        Returns:
            如果任务运行超过指定时间返回True
        """
        if self.status == "running" and self.start_time:
            elapsed_minutes = (datetime.now() - self.start_time).total_seconds() / 60
            return elapsed_minutes > timeout_minutes
        return False

    def reset(self):
        """重置任务状态"""
        logger.warning("🔄 正在重置任务状态...")

        # 取消正在运行的任务
        if self.task and not self.task.done():
            self.task.cancel()
            logger.info("已取消正在运行的任务")

        # 重置所有状态
        self.status = "idle"
        self.target_count = 0
        self.current_index = 0
        self.success_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.start_time = None
        self.end_time = None
        self.logs.clear()
        self.error_message = None
        self.expected_positions = []
        self.limit_reached = False
        self.log_file_path = None
        self.task = None
        if self.automation:
            self.automation = None

        logger.info("✅ 任务状态已重置")


# 全局单例
greeting_manager = GreetingTaskManager()
