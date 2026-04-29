"""
自动化任务 API 路由
"""
import asyncio
import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from datetime import datetime

from app.database import get_session
from app.models.automation_task import (
    AutomationTask,
    AutomationTaskCreate,
    AutomationTaskUpdate,
    TaskStatus
)
from app.models.greeting_template import GreetingTemplate
from app.models.filters import FilterOptions
from app.models.system_config import SystemConfig
from app.models.user_account import UserAccount
from app.services.boss_automation import BossAutomation
from app.services.logging_service import LoggingService
from app.models.log_entry import LogAction, LogLevel
from app.utils.filters_applier import FiltersApplier
from app.services.anti_detection import AntiDetection

router = APIRouter(prefix="/api/automation", tags=["automation"])

# 全局自动化服务实例（单例）
_automation_service: Optional[BossAutomation] = None
_current_task_id: Optional[int] = None
_headless: bool = True  # 默认隐藏浏览器
_last_home_recover_ts: float = 0.0  # 防止轮询导致反复刷新
_manual_mode: bool = False  # 向导手动模式下避免自动跳转/刷新
_last_blank_recover_ts: float = 0.0  # 验证链路掉到 blank 时的温和恢复节流

# Playwright 同一 Page 不能并发导航/evaluate；与 init 交错会导致 about:blank 等异常
_browser_session_lock = asyncio.Lock()
_init_in_progress: bool = False


async def _probe_page_alive(page) -> bool:
    """更可靠的 Page 探活：仅靠 is_closed 在 CDP/异常退出时可能滞后。"""
    try:
        if page is None:
            return False
        if page.is_closed():
            return False
    except Exception:
        return False
    try:
        # 用最小 evaluate 做一次握手；超时或 TargetClosed 都视为已死
        await asyncio.wait_for(page.evaluate("() => 1"), timeout=1.8)
        return True
    except Exception:
        return False


async def get_automation_service(headless: Optional[bool] = None) -> BossAutomation:
    """获取或创建自动化服务实例

    Args:
        headless: 是否无头模式，None 则使用全局设置
    """
    global _automation_service, _headless

    # 如果指定了 headless 参数，更新全局设置
    if headless is not None:
        _headless = headless

    if _automation_service is None:
        # 注意：initialize() 内部会触发导航/守护任务；如果在持有 _browser_session_lock 时再注入同一把锁，
        # 会导致 asyncio.Lock 不可重入而死锁（表现为页面一直 about:blank）。
        # 因此：仅在“初始化完成后”再注入锁；初始化期间通过“未初始化”状态阻止轮询接口介入。
        async with _browser_session_lock:
            if _automation_service is None:
                svc = BossAutomation()
                # 先不注入锁，避免 init 自身重入死锁
                _automation_service = svc
        # 在锁外初始化浏览器
        await _automation_service.initialize(headless=_headless)
        # 初始化完成后再注入锁，用于后续轮询/守护与 API 调用串行化
        try:
            _automation_service.set_session_lock(_browser_session_lock)
        except Exception:
            pass
    else:
        # 确保已有实例也绑定了锁（兼容老状态或热重载）
        try:
            _automation_service.set_session_lock(_browser_session_lock)
        except Exception:
            pass
    return _automation_service


async def run_automation_task(task_id: int, session: AsyncSession):
    """
    在后台运行自动化任务

    Args:
        task_id: 任务 ID
        session: 数据库会话
    """
    global _current_task_id

    try:
        # 获取任务
        result = await session.execute(
            select(AutomationTask).where(AutomationTask.id == task_id)
        )
        task = result.scalar_one_or_none()

        if not task:
            return

        # 更新任务状态
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        session.add(task)
        await session.commit()

        _current_task_id = task_id

        # 获取自动化服务
        automation = await get_automation_service()

        # 检查并登录
        if not automation.is_logged_in:
            is_logged_in = await automation.check_and_login()
            if not is_logged_in:
                task.status = TaskStatus.FAILED
                task.error_message = "登录失败"
                session.add(task)
                await session.commit()
                return

        # 获取问候模板（如果指定）
        template = None
        if task.greeting_template_id is not None:
            template_result = await session.execute(
                select(GreetingTemplate).where(
                    GreetingTemplate.id == task.greeting_template_id
                )
            )
            template = template_result.scalar_one_or_none()

            if not template:
                task.status = TaskStatus.FAILED
                task.error_message = "问候模板不存在"
                session.add(task)
                await session.commit()
                return

        # 解析筛选条件
        import json
        filters = json.loads(task.filters) if task.filters else {}

        # 搜索候选人
        candidates = await automation.search_candidates(
            keywords=task.search_keywords,
            city=filters.get('city'),
            experience=filters.get('experience'),
            degree=filters.get('degree'),
            max_results=task.max_contacts
        )

        task.total_found = len(candidates)
        session.add(task)
        await session.commit()

        # 向候选人发送问候
        from app.models.candidate import Candidate, CandidateStatus
        from app.models.greeting import GreetingRecord

        success_count = 0
        failed_count = 0

        for idx, candidate_data in enumerate(candidates):
            # 检查任务是否被暂停或取消
            await session.refresh(task)
            if task.status in [TaskStatus.PAUSED, TaskStatus.CANCELLED]:
                break

            # 检查是否已存在该候选人
            result = await session.execute(
                select(Candidate).where(Candidate.boss_id == candidate_data['boss_id'])
            )
            existing_candidate = result.scalar_one_or_none()

            if existing_candidate:
                # 检查是否已经联系过
                greeting_result = await session.execute(
                    select(GreetingRecord).where(
                        GreetingRecord.candidate_id == existing_candidate.id
                    )
                )
                if greeting_result.scalar_one_or_none():
                    continue  # 跳过已联系的候选人
                candidate = existing_candidate
            else:
                # 创建新候选人记录
                candidate = Candidate(
                    boss_id=candidate_data['boss_id'],
                    name=candidate_data['name'],
                    position=candidate_data['position'],
                    company=candidate_data.get('company'),
                    status=CandidateStatus.NEW,
                    profile_url=candidate_data.get('profile_url'),
                    active_time=candidate_data.get('active_time')
                )
                session.add(candidate)
                await session.commit()
                await session.refresh(candidate)

            # 生成个性化消息
            if template:
                message = template.content
                message = message.replace('{name}', candidate.name)
                message = message.replace('{position}', candidate.position)
                if candidate.company:
                    message = message.replace('{company}', candidate.company)
            else:
                # 使用默认消息
                message = f"你好，我对你的简历很感兴趣，期待与你进一步沟通。"

            # 发送问候
            send_success = await automation.send_greeting(
                candidate_boss_id=candidate.boss_id,
                message=message,
                use_random_delay=True
            )

            # 记录问候结果
            greeting_record = GreetingRecord(
                candidate_id=candidate.id,
                task_id=task.id,
                template_id=template.id if template else None,
                message=message,
                success=send_success,
                sent_at=datetime.now(),
                error_message=None if send_success else "发送失败"
            )
            session.add(greeting_record)

            # 更新候选人状态
            if send_success:
                candidate.status = CandidateStatus.CONTACTED
                success_count += 1
            else:
                failed_count += 1

            session.add(candidate)

            # 更新任务进度
            task.progress = int((idx + 1) / len(candidates) * 100)
            task.total_contacted = idx + 1
            task.total_success = success_count
            task.total_failed = failed_count
            session.add(task)

            await session.commit()

            # 检查是否出现问题
            issue = await automation.check_for_issues()
            if issue:
                task.status = TaskStatus.FAILED
                task.error_message = issue
                session.add(task)
                await session.commit()
                break

        # 任务完成
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            session.add(task)
            await session.commit()

    except Exception as e:
        # 更新任务为失败状态
        result = await session.execute(
            select(AutomationTask).where(AutomationTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            session.add(task)
            await session.commit()

    finally:
        _current_task_id = None


@router.post("/tasks", response_model=AutomationTask)
async def create_task(
    task_data: AutomationTaskCreate,
    session: AsyncSession = Depends(get_session)
):
    """创建新的自动化任务"""
    # 如果指定了模板ID，验证模板是否存在
    if task_data.greeting_template_id is not None:
        result = await session.execute(
            select(GreetingTemplate).where(
                GreetingTemplate.id == task_data.greeting_template_id
            )
        )
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(status_code=404, detail="问候模板不存在")

    # 创建任务
    task = AutomationTask(
        **task_data.model_dump(),
        status=TaskStatus.PENDING,
        progress=0,
        total_found=0,
        total_contacted=0,
        total_success=0,
        total_failed=0,
        created_at=datetime.now()
    )

    session.add(task)
    await session.commit()
    await session.refresh(task)

    # 记录日志
    logging_service = LoggingService(session)
    await logging_service.log(
        action=LogAction.TASK_CREATE,
        message=f"创建任务: {task.name}",
        level=LogLevel.INFO,
        task_id=task.id,
        task_name=task.name,
        details={
            "search_keywords": task.search_keywords,
            "max_contacts": task.max_contacts,
            "template_id": task.greeting_template_id,
        }
    )

    return task


@router.get("/tasks", response_model=List[AutomationTask])
async def get_tasks(
    status: Optional[TaskStatus] = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session)
):
    """获取任务列表"""
    query = select(AutomationTask)

    if status:
        query = query.where(AutomationTask.status == status)

    query = query.order_by(AutomationTask.created_at.desc()).offset(offset).limit(limit)

    result = await session.execute(query)
    tasks = result.scalars().all()

    return tasks


@router.get("/tasks/{task_id}", response_model=AutomationTask)
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_session)
):
    """获取任务详情"""
    result = await session.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return task


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """启动任务"""
    global _current_task_id

    # 检查是否有其他任务正在运行
    if _current_task_id is not None:
        raise HTTPException(status_code=400, detail="已有任务正在运行")

    result = await session.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in [TaskStatus.PENDING, TaskStatus.PAUSED]:
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {task.status}，无法启动"
        )

    # 记录日志
    logging_service = LoggingService(session)
    await logging_service.log(
        action=LogAction.TASK_START,
        message=f"启动任务: {task.name}",
        level=LogLevel.INFO,
        task_id=task.id,
        task_name=task.name,
    )

    # 在后台运行任务
    background_tasks.add_task(run_automation_task, task_id, session)

    return {"message": "任务已启动", "task_id": task_id}


@router.post("/tasks/{task_id}/pause")
async def pause_task(
    task_id: int,
    session: AsyncSession = Depends(get_session)
):
    """暂停任务"""
    result = await session.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != TaskStatus.RUNNING:
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {task.status}，无法暂停"
        )

    task.status = TaskStatus.PAUSED
    session.add(task)
    await session.commit()

    return {"message": "任务已暂停", "task_id": task_id}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_session)
):
    """删除任务"""
    result = await session.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == TaskStatus.RUNNING:
        raise HTTPException(status_code=400, detail="无法删除正在运行的任务")

    await session.delete(task)
    await session.commit()

    return {"message": "任务已删除", "task_id": task_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    session: AsyncSession = Depends(get_session)
):
    """取消任务"""
    result = await session.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED]:
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {task.status}，无法取消"
        )

    task.status = TaskStatus.CANCELLED
    session.add(task)
    await session.commit()

    return {"message": "任务已取消", "task_id": task_id}


@router.get("/status")
async def get_automation_status():
    """获取自动化服务状态"""
    global _automation_service, _current_task_id, _headless

    return {
        "service_initialized": _automation_service is not None,
        "is_logged_in": _automation_service.is_logged_in if _automation_service else False,
        "current_task_id": _current_task_id,
        "headless": _headless
    }


_CHECK_READY_NOT_INITIALIZED = {
    "ready": False,
    "logged_in": False,
    "on_recommend_page": False,
    "has_frame": False,
    "user_info": None,
    "current_url": None,
    "browser_status": "未初始化",
    "api_error": None,
    "message": "浏览器未初始化",
}


async def _check_ready_state_locked_body(logger) -> dict:
    """在已持有 _browser_session_lock 时执行；与 init 并发会导致 Playwright 页面异常（about:blank）。"""
    import time
    global _automation_service, _last_home_recover_ts, _manual_mode, _last_blank_recover_ts

    if _automation_service is None or _automation_service.page is None:
        return dict(_CHECK_READY_NOT_INITIALIZED)

    page = _automation_service.page
    page_closed = page.is_closed() if page else True
    logged_in = False
    on_recommend_page = False
    has_frame = False
    user_info = None
    browser_status = "就绪"
    api_error = None

    try:
        if page_closed:
            return {
                "ready": False,
                "logged_in": False,
                "on_recommend_page": False,
                "has_frame": False,
                "needs_verification": False,
                "user_info": None,
                "current_url": None,
                "browser_status": "页面已关闭",
                "api_error": "page_closed",
                "message": "浏览器页面已关闭，请重新初始化浏览器"
            }

        current_url = page.url
        logger.info(f"📍 当前URL: {current_url}")

        # 有些情况下浏览器刚启动/页面崩溃会短暂停留在 about:blank，
        # 这里主动拉起到 Boss 首页，避免前端看到“空白页一直不动”
        # 但前端会每 3 秒轮询一次，如果每次都 goto 会导致页面反复刷新、影响登录。
        # 因此加冷却时间：30 秒内最多恢复一次。
        need_recover = (not current_url) or (current_url == "about:blank") or ("zhipin.com" not in current_url)
        cooldown_ok = (time.time() - _last_home_recover_ts) > 30

        # 手动模式下完全禁止自动导航，避免打断登录/验证导致刷新循环
        if _manual_mode and need_recover:
            logger.info("✋ 手动模式下检测到空白页/非Boss页面，不做自动跳转")
        elif need_recover and cooldown_ok:
            try:
                logger.info("🔄 当前不在 Boss 页面，尝试访问首页以恢复...")
                await page.goto("https://www.zhipin.com", wait_until="domcontentloaded", timeout=60000)
                _last_home_recover_ts = time.time()
                current_url = page.url
                logger.info(f"📍 恢复后URL: {current_url}")
            except Exception as e:
                logger.warning(f"⚠️ 恢复访问首页失败: {str(e)}")
        elif need_recover and not cooldown_ok:
            logger.info("⏳ 页面需要恢复但处于冷却期，避免重复刷新")

        # 检测是否在推荐牛人页面
        on_recommend_page = 'web/geek/recommend' in current_url or 'chat/recommend' in current_url

        # 检测 recommendFrame iframe
        for frame in page.frames:
            if frame.name == 'recommendFrame':
                has_frame = True
                break

        # 检测页面是否在验证/登录拦截页（即使 API cookies 有效也不算真正可用）
        is_on_blocked_page = any(kw in current_url for kw in [
            'verify-slider', 'verify-phone', 'safe/verify',
            'web/user/', 'login', 'captcha',
            'passport/zp/verify', '_security_check'
        ])
        if is_on_blocked_page:
            logger.info(f"⚠️ 当前在验证/登录拦截页: {current_url}，需要手动处理")
            browser_status = "需要验证"

        # 手动模式 + about:blank：直接返回等待手动恢复，避免在空白上下文中执行 fetch
        if _manual_mode and current_url == "about:blank":
            return {
                "ready": False,
                "logged_in": False,
                "on_recommend_page": False,
                "has_frame": False,
                "needs_verification": True,
                "user_info": user_info,
                "current_url": current_url,
                "browser_status": "等待手动恢复",
                "api_error": None,
                "message": "页面进入空白，请在浏览器地址栏手动访问 https://www.zhipin.com 后继续验证"
            }

        # 手动最小模式：不做任何 page.evaluate(fetch...)，避免触发站点安全脚本/跳转。
        # 仅通过 URL + iframe 是否存在来判断状态（更稳、更接近 open_boss_no_refresh）。
        if _manual_mode and (os.getenv("MANUAL_MINIMAL", "1").strip() != "0"):
            u = (current_url or "")
            is_login_page = ("/web/user/" in u) or ("login" in u)
            # 手动最小模式下不做 API 校验：只要离开登录页且仍在 zhipin.com 域名，就视为“可能已登录”
            # 真正就绪仍以“推荐页 + recommendFrame iframe”为准，避免误判。
            logged_in = (
                (on_recommend_page and has_frame)
                or any(p in u for p in ("/web/boss/", "/web/chat/", "/web/geek/", "chat/recommend", "geek/recommend"))
                or (("zhipin.com" in u) and (not is_login_page) and (u != "about:blank"))
            )
            ready = on_recommend_page and has_frame
            return {
                "ready": ready,
                "logged_in": logged_in,
                "on_recommend_page": on_recommend_page,
                "has_frame": has_frame,
                "needs_verification": is_on_blocked_page,
                "user_info": None,
                "current_url": current_url,
                "browser_status": "手动最小模式",
                "api_error": None,
                "message": (
                    "手动最小模式：检测到你已离开登录页，请在浏览器进入“推荐牛人”页后继续"
                    if (logged_in and (not on_recommend_page))
                    else "手动最小模式：请在浏览器中完成登录/进入推荐页，系统仅做被动检测"
                ),
            }

        # 检测登录状态（通过 API）
        try:
            # 在验证拦截页上接口经常返回 HTML（非 JSON），这里跳过 API 检查避免噪音和误判
            if is_on_blocked_page:
                return {
                    "ready": False,
                    "logged_in": False,
                    "on_recommend_page": on_recommend_page,
                    "has_frame": has_frame,
                    "needs_verification": True,
                    "user_info": user_info,
                    "current_url": current_url,
                    "browser_status": browser_status,
                    "api_error": None,
                    "message": "⚠️ 当前处于安全验证流程，请先在浏览器完成验证"
                }

            api_url = "https://www.zhipin.com/wapi/zpboss/h5/user/info"
            response = await page.evaluate(f'''
                async () => {{
                    try {{
                        const response = await fetch("{api_url}", {{
                            method: 'GET',
                            credentials: 'include',
                            headers: {{
                                'Content-Type': 'application/json'
                            }}
                        }});
                        if (!response.ok) {{
                            return {{ code: -2, error: 'HTTP ' + response.status }};
                        }}
                        return await response.json();
                    }} catch(e) {{
                        return {{ code: -1, error: e.message }};
                    }}
                }}
            ''')

            error_code = response.get('code', -1)
            logger.info(f"🔍 check-ready-state API code={error_code}, on_blocked={is_on_blocked_page}")
            
            if error_code != 0:
                api_error = response.get('error', '未知错误')
                logger.warning(f"⚠️ API调用返回错误: code={error_code}, error={api_error}")

            if error_code == 0:
                zp_data = response.get('zpData', {})
                base_info = zp_data.get('baseInfo', {})
                com_id = base_info.get('comId')

                # 必须有 comId + 不在拦截页面 才算真正登录可用
                if com_id and not is_on_blocked_page:
                    logged_in = True
                    _automation_service.is_logged_in = True
                    user_info = {
                        'comId': com_id,
                        'name': base_info.get('name'),
                        'showName': base_info.get('showName'),
                        'avatar': base_info.get('avatar'),
                        'title': base_info.get('title'),
                    }
                    logger.info(f"✅ 已登录: {base_info.get('showName')} (comId={com_id})")
                    browser_status = "已登录"

                    # 保存认证状态
                    if not _automation_service.auth_file:
                        _automation_service.current_com_id = com_id
                        _automation_service.auth_file = _automation_service.get_auth_file_path(com_id)

                    if _automation_service.auth_file:
                        try:
                            await _automation_service.context.storage_state(path=_automation_service.auth_file)
                        except Exception:
                            pass

                    # 保存账号信息到数据库
                    try:
                        await _automation_service._save_account_info(response)
                    except Exception:
                        pass
                elif com_id and is_on_blocked_page:
                    # API 有效但页面被拦截，提取用户信息但不标记为已登录
                    user_info = {
                        'comId': com_id,
                        'name': base_info.get('name'),
                        'showName': base_info.get('showName'),
                        'avatar': base_info.get('avatar'),
                        'title': base_info.get('title'),
                    }
                    logger.info(f"⚠️ API 有效但在拦截页，需先完成验证: {base_info.get('showName')}")
                    browser_status = "已登录-需要验证"
                else:
                    logger.info("⚠️ API code=0 但无 comId，未登录")
                    browser_status = "未登录"
        except Exception as e:
            api_error = str(e)
            logger.warning(f"⚠️ 检查登录状态异常: {api_error}")
            browser_status = "API检查失败"

        ready = logged_in and on_recommend_page and has_frame

        # 生成友好的消息提示
        if ready:
            message = "✅ 所有条件已满足，可以开始打招呼"
        elif is_on_blocked_page:
            message = "⚠️ 需要在浏览器中完成安全验证，完成后自动继续"
        elif not logged_in:
            message = "📱 请在浏览器中扫码或登录"
        elif not on_recommend_page:
            message = "🔍 请在浏览器中导航到推荐牛人页面"
        elif not has_frame:
            message = "⏳ 页面加载中，请稍候..."
        else:
            message = "⏳ 等待中..."

        return {
            "ready": ready,
            "logged_in": logged_in,
            "on_recommend_page": on_recommend_page,
            "has_frame": has_frame,
            "needs_verification": is_on_blocked_page,
            "user_info": user_info,
            "current_url": current_url,
            "browser_status": browser_status,
            "api_error": api_error,
            "message": message
        }

    except Exception as e:
        logger.error(f"❌ 检查就绪状态异常: {str(e)}", exc_info=True)
        return {
            "ready": False,
            "logged_in": False,
            "on_recommend_page": False,
            "has_frame": False,
            "user_info": None,
            "current_url": None,
            "browser_status": "异常",
            "api_error": str(e),
            "message": f"检查失败: {str(e)}"
        }


@router.get("/check-ready-state")
async def check_ready_state():
    """检查浏览器中用户操作的就绪状态（用于手动模式轮询）

    与 /init 共用锁，避免 Playwright 同一 Page 上并发 goto / evaluate 导致 about:blank。
    """
    import logging

    logger = logging.getLogger(__name__)
    global _automation_service

    if _automation_service is None or _automation_service.page is None:
        return dict(_CHECK_READY_NOT_INITIALIZED)

    async with _browser_session_lock:
        return await _check_ready_state_locked_body(logger)


@router.post("/init")
async def initialize_browser(
    headless: bool = True,
    com_id: Optional[int] = None,
    manual_mode: bool = False
):
    """初始化浏览器

    Args:
        headless: 是否无头模式（隐藏浏览器窗口）
        com_id: 可选的账号com_id，用于加载该账号的登录状态
        manual_mode: 手动模式，只启动浏览器访问首页，不自动执行登录/导航

    Returns:
        初始化结果
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"🔧 初始化浏览器 - headless={headless} (类型: {type(headless).__name__}), com_id={com_id}, manual_mode={manual_mode}")

    global _automation_service, _headless, _manual_mode
    global _init_in_progress

    # 同上：不要在持有 _browser_session_lock 的情况下调用 initialize() 并注入同一把锁，否则会死锁。
    # 这里分两段：
    # - 锁内：清理旧实例、设置全局状态、预先放入新实例（让轮询接口看到“初始化中/未就绪”而不介入）
    # - 锁外：执行 initialize（真实启动浏览器与导航）
    # - 再锁内：注入锁，完成绑定
    async with _browser_session_lock:
        if _init_in_progress:
            return {
                "success": False,
                "message": "浏览器正在初始化中，请稍候再试（避免重复启动导致页面抖动/关闭）",
                "headless": _headless,
                "service_initialized": _automation_service is not None,
                "com_id": com_id,
                "manual_mode": manual_mode,
            }

        # 幂等保护：手动模式下如果已经初始化过（尤其是 CDP 模式），重复点击“启动浏览器”
        # 不要重建/cleanup，否则会导致窗口/页签被替换，用户体感为“浏览器自己关了/跳回登录”。
        if (
            manual_mode
            and _automation_service is not None
            and _automation_service.page is not None
            and getattr(_automation_service, "manual_mode", False)
            and (_automation_service.current_com_id == com_id)
        ):
            page_closed = True
            current_url = None
            browser_connected = None
            try:
                page_closed = _automation_service.page.is_closed()
            except Exception:
                page_closed = True
            try:
                current_url = _automation_service.page.url
            except Exception:
                current_url = None
            try:
                if getattr(_automation_service, "browser", None) is not None:
                    browser_connected = _automation_service.browser.is_connected()
            except Exception:
                browser_connected = None

            # 如果页面其实已经被关了（你看到的“浏览器自己关了”），不要误报“已启动”。
            # CDP 模式下优先尝试重开一个新 page 复活会话；失败再走重建流程。
            page_alive = False
            try:
                page_alive = await _probe_page_alive(_automation_service.page)
            except Exception:
                page_alive = False

            if page_closed or (browser_connected is False) or (not page_alive):
                try:
                    if getattr(_automation_service, "_manual_attached_via_cdp", False):
                        expected_url = getattr(_automation_service, "_manual_expected_login_url", None) or "https://www.zhipin.com/web/user/?ka=header-login"
                        reopen = getattr(_automation_service, "_cdp_reopen_page", None)
                        if callable(reopen):
                            await reopen(expected_url, reason="init_idempotent_page_closed")
                            try:
                                current_url = _automation_service.page.url if _automation_service.page else None
                            except Exception:
                                current_url = None
                            return {
                                "success": True,
                                "message": "检测到页面已关闭，已尝试重开浏览器页签（CDP）",
                                "headless": _headless,
                                "service_initialized": True,
                                "com_id": com_id,
                                "manual_mode": True,
                                "current_url": current_url,
                            }
                except Exception:
                    # 继续走下面的 cleanup + 重新初始化
                    pass
            return {
                "success": True,
                "message": f"浏览器已初始化，无需重复启动{' [手动模式]' if manual_mode else ''}",
                "headless": _headless,
                "service_initialized": True,
                "com_id": com_id,
                "manual_mode": True,
                "current_url": current_url,
            }

        if _automation_service is not None:
            await _automation_service.cleanup()
            _automation_service = None

        _headless = headless
        logger.info(f"🔧 设置全局 _headless={_headless}")
        _manual_mode = manual_mode

        svc = BossAutomation(com_id=com_id)
        _automation_service = svc
        _init_in_progress = True

    try:
        initialized = await svc.initialize(headless=headless, skip_auto_navigate=manual_mode)
    finally:
        async with _browser_session_lock:
            _init_in_progress = False
    if not initialized:
        try:
            await svc.cleanup()
        except Exception:
            pass
        async with _browser_session_lock:
            if _automation_service is svc:
                _automation_service = None
        raise HTTPException(status_code=500, detail="浏览器启动成功，但无法打开 Boss 页面，请检查网络/IP风控后重试")

    async with _browser_session_lock:
        if _automation_service is svc:
            try:
                svc.set_session_lock(_browser_session_lock)
            except Exception:
                pass

    return {
        "success": True,
        "message": f"浏览器初始化成功{f'（使用账号 {com_id}）' if com_id else ''}{' [手动模式]' if manual_mode else ''}",
        "headless": headless,
        "service_initialized": True,
        "com_id": com_id,
        "manual_mode": manual_mode
    }


@router.post("/login")
async def trigger_login():
    """触发登录流程"""
    async with _browser_session_lock:
        automation = await get_automation_service()

        if automation.is_logged_in:
            return {"message": "已登录", "logged_in": True}

        # 启动登录流程
        is_logged_in = await automation.check_and_login()

    return {
        "message": "登录成功" if is_logged_in else "登录失败",
        "logged_in": is_logged_in
    }


@router.post("/recover-homepage")
async def recover_homepage():
    """当页面变成 about:blank 时手动恢复到 Boss 首页"""
    global _automation_service

    if _automation_service is None or _automation_service.page is None:
        raise HTTPException(status_code=400, detail="浏览器未初始化")

    async with _browser_session_lock:
        if _automation_service is None or _automation_service.page is None:
            raise HTTPException(status_code=400, detail="浏览器未初始化")
        page = _automation_service.page
        current_url = page.url or ""

        if "zhipin.com" in current_url and current_url != "about:blank":
            return {"success": True, "message": "当前页面正常，无需恢复", "current_url": current_url}

        try:
            await page.goto("https://www.zhipin.com", wait_until="domcontentloaded", timeout=60000)
            await AntiDetection.random_sleep(0.5, 1.2)
            return {"success": True, "message": "已恢复到 Boss 首页", "current_url": page.url}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"恢复首页失败: {str(e)}")


@router.get("/qrcode")
async def get_qrcode():
    """获取登录二维码"""
    async with _browser_session_lock:
        automation = await get_automation_service()

        # 直接调用 get_qrcode，让它自己判断是否需要二维码
        # 不在路由层面检查 is_logged_in，因为这个标志可能过期
        result = await automation.get_qrcode()
    return result


@router.get("/check-login")
async def check_login(session: AsyncSession = Depends(get_session)):
    """检查登录状态并获取用户信息"""
    async with _browser_session_lock:
        automation = await get_automation_service()
        # 检查登录状态
        result = await automation.check_login_status()

    # 如果登录成功，记录日志
    if result.get('logged_in'):
        user_info = result.get('user_info', {})
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.LOGIN_SUCCESS,
            message=f"用户登录成功: {user_info.get('showName', 'Unknown')}",
            level=LogLevel.INFO,
            user_id=str(user_info.get('userId', '')),
            user_name=user_info.get('showName'),
            details={
                "email": user_info.get('email'),
                "company": user_info.get('brandName'),
            }
        )

    return result


@router.get("/refresh-qrcode")
async def refresh_qrcode():
    """检查并刷新二维码"""
    async with _browser_session_lock:
        automation = await get_automation_service()
        # 检查并刷新二维码
        result = await automation.check_and_refresh_qrcode()
    return result


@router.get("/jobs")
async def get_chatted_jobs():
    """获取已沟通的职位列表"""
    automation = await get_automation_service()

    # 获取职位列表
    result = await automation.get_chatted_jobs()
    return result


@router.post("/cleanup")
async def cleanup_service():
    """清理自动化服务"""
    global _automation_service, _current_task_id

    if _current_task_id is not None:
        raise HTTPException(status_code=400, detail="有任务正在运行，无法清理")

    async with _browser_session_lock:
        if _automation_service:
            await _automation_service.cleanup()
            _automation_service = None

    return {"message": "服务已清理"}


@router.get("/recommend-candidates")
async def get_recommend_candidates(
    max_results: int = 50,
    session: AsyncSession = Depends(get_session)
):
    """获取推荐候选人列表

    Args:
        max_results: 最大候选人数量（默认 50）

    Returns:
        推荐候选人列表
    """
    automation = await get_automation_service()

    if not automation.is_logged_in:
        raise HTTPException(status_code=401, detail="未登录，无法获取推荐候选人")

    try:
        # 获取推荐候选人
        candidates = await automation.get_recommended_candidates(max_results=max_results)

        # 记录日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.SEARCH,
            message=f"获取推荐候选人列表，返回 {len(candidates)} 个结果",
            level=LogLevel.INFO,
            details={
                "max_results": max_results,
                "actual_results": len(candidates)
            }
        )

        return {
            "success": True,
            "count": len(candidates),
            "candidates": candidates
        }

    except Exception as e:
        # 记录错误日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.ERROR,
            message=f"获取推荐候选人失败: {str(e)}",
            level=LogLevel.ERROR,
            details={"error": str(e)}
        )

        raise HTTPException(
            status_code=500,
            detail=f"获取推荐候选人失败: {str(e)}"
        )


@router.get("/available-jobs")
async def get_available_jobs(session: AsyncSession = Depends(get_session)):
    """获取当前可用的招聘职位列表

    Returns:
        职位列表
    """
    automation = await get_automation_service()

    if not automation.is_logged_in:
        raise HTTPException(status_code=401, detail="未登录，无法获取职位列表")

    try:
        # 获取可用职位
        result = await automation.get_available_jobs()

        # 记录日志
        if result.get('success'):
            logging_service = LoggingService(session)
            await logging_service.log(
                action=LogAction.SEARCH,
                message=f"获取可用职位列表，返回 {result.get('total', 0)} 个职位",
                level=LogLevel.INFO,
                details={
                    "total_jobs": result.get('total', 0)
                }
            )

        return result

    except Exception as e:
        # 记录错误日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.ERROR,
            message=f"获取职位列表失败: {str(e)}",
            level=LogLevel.ERROR,
            details={"error": str(e)}
        )

        raise HTTPException(
            status_code=500,
            detail=f"获取职位列表失败: {str(e)}"
        )


@router.post("/select-job")
async def select_job(
    job_value: str,
    session: AsyncSession = Depends(get_session)
):
    """选择指定的招聘职位

    Args:
        job_value: 职位的 value 属性值

    Returns:
        选择结果
    """
    automation = await get_automation_service()

    if not automation.is_logged_in:
        raise HTTPException(status_code=401, detail="未登录，无法选择职位")

    try:
        # 选择职位
        result = await automation.select_job_position(job_value=job_value)

        # 记录日志
        if result.get('success'):
            logging_service = LoggingService(session)
            await logging_service.log(
                action=LogAction.SEARCH,
                message=f"选择职位成功: {job_value}",
                level=LogLevel.INFO,
                details={
                    "job_value": job_value
                }
            )
        else:
            logging_service = LoggingService(session)
            await logging_service.log(
                action=LogAction.ERROR,
                message=f"选择职位失败: {result.get('message')}",
                level=LogLevel.WARNING,
                details={
                    "job_value": job_value,
                    "error": result.get('message')
                }
            )

        return result

    except Exception as e:
        # 记录错误日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.ERROR,
            message=f"选择职位失败: {str(e)}",
            level=LogLevel.ERROR,
            details={
                "job_value": job_value,
                "error": str(e)
            }
        )

        raise HTTPException(
            status_code=500,
            detail=f"选择职位失败: {str(e)}"
        )


@router.post("/apply-filters")
async def apply_filters(
    filters: FilterOptions,
    session: AsyncSession = Depends(get_session)
):
    """应用筛选条件到推荐页面

    Args:
        filters: 筛选条件对象

    Returns:
        应用结果
    """
    automation = await get_automation_service()

    if not automation.is_logged_in:
        raise HTTPException(status_code=401, detail="未登录，无法应用筛选条件")

    try:
        import asyncio

        # 导航到推荐页面
        await automation.navigate_to_recommend_page()
        await asyncio.sleep(3)

        # 获取 iframe
        recommend_frame = None
        for frame in automation.page.frames:
            if frame.name == 'recommendFrame':
                recommend_frame = frame
                break

        if not recommend_frame:
            raise HTTPException(status_code=500, detail="未找到推荐页面 iframe")

        # 应用筛选条件
        applier = FiltersApplier(recommend_frame, automation.page)

        # 打开筛选面板
        if not await applier.open_filter_panel():
            raise HTTPException(status_code=500, detail="无法打开筛选面板")

        # 应用所有筛选条件
        filter_result = await applier.apply_all_filters(filters)

        if not filter_result['success']:
            raise HTTPException(
                status_code=500,
                detail=f"筛选条件应用失败: {filter_result.get('error', 'Unknown error')}"
            )

        # 记录日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.SEARCH,
            message=f"应用筛选条件成功: {len(filter_result['applied_filters'])} 项",
            level=LogLevel.INFO,
            details={
                "applied_filters": filter_result['applied_filters'],
                "failed_filters": filter_result['failed_filters']
            }
        )

        return {
            "success": True,
            "message": f"成功应用 {len(filter_result['applied_filters'])} 项筛选条件",
            "applied_count": len(filter_result['applied_filters']),
            "failed_count": len(filter_result['failed_filters']),
            "details": filter_result
        }

    except HTTPException:
        raise
    except Exception as e:
        # 记录错误日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.ERROR,
            message=f"应用筛选条件失败: {str(e)}",
            level=LogLevel.ERROR,
            details={"error": str(e)}
        )

        raise HTTPException(
            status_code=500,
            detail=f"应用筛选条件失败: {str(e)}"
        )


@router.post("/switch-account/{account_id}")
async def switch_automation_account(
    account_id: int,
    session: AsyncSession = Depends(get_session)
):
    """
    切换自动化服务使用的账号

    Args:
        account_id: 要切换到的账号ID
        session: 数据库会话

    Returns:
        切换结果
    """
    try:
        # 获取账号信息
        result = await session.execute(
            select(UserAccount).where(UserAccount.id == account_id)
        )
        account = result.scalar_one_or_none()

        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        # 获取自动化服务
        automation = await get_automation_service()

        if not automation:
            raise HTTPException(status_code=500, detail="自动化服务未初始化")

        # 执行账号切换
        switch_result = await automation.switch_account(account.com_id)

        if not switch_result.get('success'):
            # 切换失败
            return {
                "success": False,
                "message": switch_result.get('message', '切换失败'),
                "needs_login": switch_result.get('needs_login', False)
            }

        # 切换成功，更新系统配置中的当前账号ID
        config_result = await session.execute(select(SystemConfig))
        config = config_result.scalar_one_or_none()

        if not config:
            config = SystemConfig()
            session.add(config)

        config.current_account_id = account_id
        config.updated_at = datetime.now()
        await session.commit()

        # 记录日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.SYSTEM,
            message=f"切换账号成功: {account.show_name}",
            level=LogLevel.INFO,
            details={
                "account_id": account_id,
                "com_id": account.com_id,
                "show_name": account.show_name
            }
        )

        return {
            "success": True,
            "message": "账号切换成功",
            "account": {
                "id": account.id,
                "com_id": account.com_id,
                "show_name": account.show_name,
                "avatar": account.avatar,
                "company_name": account.company_short_name
            },
            "user_info": switch_result.get('user_info')
        }

    except HTTPException:
        raise
    except Exception as e:
        # 记录错误日志
        logging_service = LoggingService(session)
        await logging_service.log(
            action=LogAction.ERROR,
            message=f"切换账号失败: {str(e)}",
            level=LogLevel.ERROR,
            details={"error": str(e), "account_id": account_id}
        )

        raise HTTPException(
            status_code=500,
            detail=f"切换账号失败: {str(e)}"
        )
