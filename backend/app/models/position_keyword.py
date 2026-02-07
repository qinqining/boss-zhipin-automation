"""
期望职位关键词数据模型
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class PositionKeyword(SQLModel, table=True):
    """期望职位关键词数据库模型"""
    __tablename__ = "position_keywords"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, description="关键词名称")
    usage_count: int = Field(default=0, description="使用次数")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
