"""
筛选条件应用工具
用于在浏览器中应用用户配置的筛选条件
"""

import asyncio
import logging
from typing import Optional
from app.models.filters import FilterOptions
from app.utils.age_filter import set_age_filter_via_vue

logger = logging.getLogger(__name__)


class FiltersApplier:
    """筛选条件应用器"""

    def __init__(self, frame, page):
        """
        初始化

        Args:
            frame: Playwright的iframe对象（recommendFrame）
            page: Playwright的page对象
        """
        self.frame = frame
        self.page = page

    async def open_filter_panel(self) -> bool:
        """
        打开筛选面板

        Returns:
            是否成功打开
        """
        try:
            logger.info("📂 打开筛选面板...")
            filter_btn = await self.frame.wait_for_selector(
                ".recommend-filter",
                timeout=10000
            )
            await filter_btn.click()
            await asyncio.sleep(2)
            logger.info("✅ 筛选面板已打开")
            return True
        except Exception as e:
            logger.error(f"❌ 打开筛选面板失败: {e}")
            return False

    async def apply_age_filter(self, age_filter: dict) -> bool:
        """应用年龄筛选"""
        try:
            min_age = age_filter.get('min', 16)
            max_age = age_filter.get('max')

            logger.info(f"设置年龄: {min_age} - {max_age if max_age else '不限'}")

            result = await set_age_filter_via_vue(self.frame, min_age, max_age)

            if result['success']:
                logger.info(f"✅ 年龄设置成功: {result['final_values']}")
                return True
            else:
                logger.warning(f"⚠️  年龄设置失败: {result.get('error')}")
                return False

        except Exception as e:
            logger.error(f"❌ 应用年龄筛选失败: {e}")
            return False

    async def apply_single_select_filter(self, field: str, value: str, label: str) -> bool:
        """
        应用单选筛选条件

        Args:
            field: 字段名（用于定位筛选区域）
            value: 选项值
            label: 显示标签（用于日志）

        Returns:
            是否成功
        """
        try:
            if value == '不限':
                logger.info(f"{label}: 跳过（不限）")
                return True

            logger.info(f"设置{label}: {value}")

            # 使用text定位器查找并点击对应的文本
            button = await self.frame.query_selector(f"text={value}")

            if button:
                await button.click()
                await asyncio.sleep(0.5)
                logger.info(f"✅ {label}设置成功")
                return True
            else:
                logger.warning(f"⚠️  未找到{label}选项: {value}")
                return False

        except Exception as e:
            logger.error(f"❌ 应用{label}筛选失败: {e}")
            return False

    async def apply_multi_select_filter(self, field: str, values: list, label: str) -> bool:
        """
        应用多选筛选条件

        Args:
            field: 字段名
            values: 选项值列表
            label: 显示标签

        Returns:
            是否成功
        """
        try:
            if not values or len(values) == 0:
                logger.info(f"{label}: 跳过（不限）")
                return True

            logger.info(f"设置{label}: {', '.join(values)}")

            success_count = 0
            for value in values:
                button = await self.frame.query_selector(f"text={value}")
                if button:
                    await button.click()
                    await asyncio.sleep(0.3)
                    success_count += 1

            logger.info(f"✅ {label}设置成功: {success_count}/{len(values)}")
            return success_count > 0

        except Exception as e:
            logger.error(f"❌ 应用{label}筛选失败: {e}")
            return False

    async def apply_keywords(self, keywords: list) -> bool:
        """
        应用牛人关键词

        Args:
            keywords: 关键词列表

        Returns:
            是否成功
        """
        try:
            if not keywords or len(keywords) == 0:
                logger.info("牛人关键词: 跳过（无关键词）")
                return True

            logger.info(f"设置牛人关键词: {', '.join(keywords)}")

            # 关键词通常直接点击标签按钮
            success_count = 0
            for keyword in keywords:
                button = await self.frame.query_selector(f"text={keyword}")
                if button:
                    await button.click()
                    await asyncio.sleep(0.3)
                    success_count += 1

            logger.info(f"✅ 关键词设置成功: {success_count}/{len(keywords)}")
            return success_count > 0

        except Exception as e:
            logger.error(f"❌ 应用关键词筛选失败: {e}")
            return False

    async def confirm_filters(self) -> bool:
        """
        点击确定按钮应用筛选

        Returns:
            是否成功
        """
        try:
            logger.info("📌 应用筛选条件...")

            confirm_btn = await self.frame.query_selector("text=确定")
            if not confirm_btn:
                # 尝试其他可能的选择器
                confirm_btn = await self.frame.query_selector(".confirm-btn")

            if confirm_btn:
                await confirm_btn.click()
                await asyncio.sleep(2)
                logger.info("✅ 筛选条件已应用")
                return True
            else:
                logger.warning("⚠️  未找到确定按钮")
                return False

        except Exception as e:
            logger.error(f"❌ 确认筛选失败: {e}")
            return False

    async def apply_all_filters(self, filters: FilterOptions) -> dict:
        """
        应用所有筛选条件

        Args:
            filters: 筛选条件对象

        Returns:
            应用结果
        """
        results = {
            "success": True,
            "applied_filters": [],
            "failed_filters": [],
        }

        try:
            logger.info("="*60)
            logger.info("🎯 开始应用筛选条件")
            logger.info("="*60)

            # 1. 年龄
            if filters.age:
                if await self.apply_age_filter(filters.age.dict()):
                    results["applied_filters"].append("年龄")
                else:
                    results["failed_filters"].append("年龄")

            # 2. 专业
            if filters.major:
                if await self.apply_multi_select_filter("major", filters.major, "专业"):
                    results["applied_filters"].append("专业")
                else:
                    results["failed_filters"].append("专业")

            # 3. 活跃度
            if filters.activity and filters.activity != '不限':
                if await self.apply_single_select_filter("activity", filters.activity, "活跃度"):
                    results["applied_filters"].append("活跃度")
                else:
                    results["failed_filters"].append("活跃度")

            # 4. 性别（多选）
            if filters.gender:
                if await self.apply_multi_select_filter("gender", filters.gender, "性别"):
                    results["applied_filters"].append("性别")
                else:
                    results["failed_filters"].append("性别")

            # 5. 近期没有看过（多选）
            if filters.not_recently_viewed:
                if await self.apply_multi_select_filter(
                    "notRecentlyViewed",
                    filters.not_recently_viewed,
                    "近期没有看过"
                ):
                    results["applied_filters"].append("近期没有看过")
                else:
                    results["failed_filters"].append("近期没有看过")

            # 6. 是否与同事交换简历（多选）
            if filters.resume_exchange:
                if await self.apply_multi_select_filter(
                    "resumeExchange",
                    filters.resume_exchange,
                    "是否与同事交换简历"
                ):
                    results["applied_filters"].append("是否与同事交换简历")
                else:
                    results["failed_filters"].append("是否与同事交换简历")

            # 7. 院校
            if filters.school:
                if await self.apply_multi_select_filter("school", filters.school, "院校"):
                    results["applied_filters"].append("院校")
                else:
                    results["failed_filters"].append("院校")

            # 8. 跳槽频率
            if filters.job_hopping_frequency and filters.job_hopping_frequency != '不限':
                if await self.apply_single_select_filter(
                    "jobHopping",
                    filters.job_hopping_frequency,
                    "跳槽频率"
                ):
                    results["applied_filters"].append("跳槽频率")
                else:
                    results["failed_filters"].append("跳槽频率")

            # 9. 牛人关键词
            if filters.keywords:
                if await self.apply_keywords(filters.keywords):
                    results["applied_filters"].append("牛人关键词")
                else:
                    results["failed_filters"].append("牛人关键词")

            # 10. 经验要求（多选）
            if filters.experience:
                if await self.apply_multi_select_filter("experience", filters.experience, "经验要求"):
                    results["applied_filters"].append("经验要求")
                else:
                    results["failed_filters"].append("经验要求")

            # 11. 学历要求（多选）
            if filters.education:
                if await self.apply_multi_select_filter("education", filters.education, "学历要求"):
                    results["applied_filters"].append("学历要求")
                else:
                    results["failed_filters"].append("学历要求")

            # 12. 薪资待遇
            if filters.salary and filters.salary != '不限':
                if await self.apply_single_select_filter("salary", filters.salary, "薪资待遇"):
                    results["applied_filters"].append("薪资待遇")
                else:
                    results["failed_filters"].append("薪资待遇")

            # 13. 求职意向（多选）
            if filters.job_intention:
                if await self.apply_multi_select_filter(
                    "jobIntention",
                    filters.job_intention,
                    "求职意向"
                ):
                    results["applied_filters"].append("求职意向")
                else:
                    results["failed_filters"].append("求职意向")

            # 确认应用筛选
            if await self.confirm_filters():
                results["confirmed"] = True
            else:
                results["confirmed"] = False
                results["success"] = False

            # 总结
            logger.info("="*60)
            logger.info("📊 筛选条件应用结果")
            logger.info("="*60)
            logger.info(f"成功应用: {len(results['applied_filters'])} 项")
            logger.info(f"  - {', '.join(results['applied_filters'])}")

            if results['failed_filters']:
                logger.warning(f"失败: {len(results['failed_filters'])} 项")
                logger.warning(f"  - {', '.join(results['failed_filters'])}")

            logger.info(f"已确认: {'是' if results['confirmed'] else '否'}")
            logger.info("="*60)

        except Exception as e:
            logger.exception(f"❌ 应用筛选条件出错: {e}")
            results["success"] = False
            results["error"] = str(e)

        return results
