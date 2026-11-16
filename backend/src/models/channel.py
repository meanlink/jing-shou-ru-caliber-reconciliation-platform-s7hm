from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class ChannelMaster(Base):
    """渠道主档：渠道的稳定身份。改名只产生新别名，不产生新渠道。"""
    __tablename__ = 'channel_masters'
    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='active')  # active / deprecated
    deprecated_reason: Mapped[str] = mapped_column(String(300), default='')
    # 若因合并被弃用，记录并入的目标主档；撤销合并时按此还原
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey('channel_masters.id'), nullable=True)
    created_by: Mapped[str] = mapped_column(String(150), default='system')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class ChannelAlias(Base):
    """渠道别名：同一主档在某账日区间内使用过的名称（按账日生效）。

    同账日同名别名允许指向不同主档（业务歧义），由服务端区间校验与注册锁
    保证一致性；歧义账日的新账进入待认领，不做静默归属。
    """
    __tablename__ = 'channel_aliases'
    __table_args__ = (
        Index('ix_alias_name_from', 'alias_name', 'effective_from'),
        Index('ix_alias_effective', 'alias_name', 'effective_from', 'effective_to'),
        Index('ix_alias_master', 'master_id'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey('channel_masters.id'))
    alias_name: Mapped[str] = mapped_column(String(160))
    effective_from: Mapped[str] = mapped_column(String(10))  # ISO 日期，含
    effective_to: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO 日期，含；None 表示至今
    # manual 人工维护 / intake 接账自动登记 / merge 合并继承
    origin: Mapped[str] = mapped_column(String(20), default='manual')
    created_by: Mapped[str] = mapped_column(String(150), default='system')
    created_at: Mapped[float] = mapped_column(Double)


class ChannelOperation(Base):
    """合并/撤销操作单：幂等键防重复生成主档与裁决，状态机支持中断恢复。"""
    __tablename__ = 'channel_operations'
    id: Mapped[int] = mapped_column(primary_key=True)
    op_type: Mapped[str] = mapped_column(String(20))  # merge / undo
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='running')  # running / completed / failed / undone
    # 合并时记录参与主档与逐笔裁决；撤销时记录被撤销的合并操作
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # 合并成功后的注册版本；接账与裁决共用同一有效版本
    registry_version: Mapped[int] = mapped_column(Integer, default=0)
    related_op_id: Mapped[int | None] = mapped_column(ForeignKey('channel_operations.id'), nullable=True)
    actor: Mapped[str] = mapped_column(String(150))
    error: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class SourceLedgerEntry(Base):
    """源账逐笔条目：原名永久保留，主档归属与认领/裁决状态可追溯。"""
    __tablename__ = 'source_ledger_entries'
    __table_args__ = (
        # 自然键（账日+原名+批次+行号）；fingerprint 为跨库兼容的等值依据
        UniqueConstraint('dataset_id', 'row_index', name='uq_ledger_dataset_row'),
        Index('ix_ledger_master_day', 'master_id', 'day'),
        Index('ix_ledger_status', 'resolution_status'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    row_index: Mapped[int] = mapped_column(Integer)
    day: Mapped[str] = mapped_column(String(10))
    original_name: Mapped[str] = mapped_column(String(160))  # 接账原名，永不改写
    master_id: Mapped[int | None] = mapped_column(ForeignKey('channel_masters.id'), nullable=True)
    # auto 唯一别名自动归属 / claimed 人工认领 / adjudicated 合并逐笔裁决 / pending 歧义待认领
    resolution_status: Mapped[str] = mapped_column(String(20), default='pending')
    # 归属依据：alias 别名命中 / claim 认领 / merge 合并裁决 / undo 撤销还原
    resolution_basis: Mapped[str] = mapped_column(String(30), default='')
    # 决定该归属的操作单与注册版本，保证接账与合并裁决使用同一有效版本
    operation_id: Mapped[int | None] = mapped_column(ForeignKey('channel_operations.id'), nullable=True)
    registry_version: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # 金额等完整行数据
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
