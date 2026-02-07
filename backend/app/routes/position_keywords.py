"""
期望职位关键词管理路由
"""
from typing import List
from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_session
from app.models.position_keyword import PositionKeyword

router = APIRouter(prefix="/api/position-keywords", tags=["position-keywords"])


class PositionKeywordCreate(BaseModel):
    name: str


@router.get("", response_model=List[PositionKeyword])
async def list_keywords(
    q: str = "",
    session: AsyncSession = Depends(get_session)
):
    """获取关键词列表，支持模糊搜索，按使用次数降序"""
    statement = select(PositionKeyword)
    if q:
        statement = statement.where(PositionKeyword.name.contains(q))
    statement = statement.order_by(PositionKeyword.usage_count.desc()).limit(20)
    result = await session.execute(statement)
    return result.scalars().all()


@router.post("", response_model=PositionKeyword)
async def create_keyword(
    data: PositionKeywordCreate,
    session: AsyncSession = Depends(get_session)
):
    """创建关键词（如已存在则返回已有记录）"""
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="关键词不能为空")

    statement = select(PositionKeyword).where(PositionKeyword.name == name)
    result = await session.execute(statement)
    existing = result.scalars().first()
    if existing:
        return existing

    keyword = PositionKeyword(name=name)
    session.add(keyword)
    await session.commit()
    await session.refresh(keyword)
    return keyword


@router.delete("/{keyword_id}")
async def delete_keyword(
    keyword_id: int,
    session: AsyncSession = Depends(get_session)
):
    """删除关键词"""
    keyword = await session.get(PositionKeyword, keyword_id)
    if not keyword:
        raise HTTPException(status_code=404, detail="关键词不存在")

    await session.delete(keyword)
    await session.commit()
    return {"success": True, "message": "已删除"}
