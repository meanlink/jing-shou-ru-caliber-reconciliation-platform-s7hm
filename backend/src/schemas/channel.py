from pydantic import BaseModel, Field


class AliasInput(BaseModel):
    master_id: int
    alias_name: str = Field(min_length=1, max_length=160)
    effective_from: str = Field(min_length=8, max_length=10)
    effective_to: str | None = Field(default=None, max_length=10)
    allow_ambiguous: bool = False


class ClaimInput(BaseModel):
    master_id: int | None = None
    create_master_name: str | None = Field(default=None, max_length=160)


class MergeInput(BaseModel):
    source_id: int
    target_id: int
    # 键为条目 ID（字符串或数字），值为 target（并入）或 source（保留原主档）
    decisions: dict[str, str]
    idempotency_key: str = Field(min_length=8, max_length=80)


class UndoInput(BaseModel):
    merge_operation_id: int
    idempotency_key: str = Field(min_length=8, max_length=80)


class TraceQuery(BaseModel):
    name: str = Field(min_length=1, max_length=160)
