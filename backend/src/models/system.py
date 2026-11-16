from sqlalchemy import String, Integer, Double
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SystemState(Base):
    """单行系统状态：渠道注册版本号。每次合并/撤销生效后单调递增，
    接账归属与合并冲突裁决必须读取同一有效版本。"""
    __tablename__ = 'system_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    registry_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)
