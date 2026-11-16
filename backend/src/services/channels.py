"""渠道主档 / 按账日生效别名 / 逐笔归属与合并裁决。

设计约束（与业务说明一一对应）：
- 接账只保留原名并解析归属，绝不改写源账原名；
- 别名按账日区间生效；同一账日名称歧义时条目标记为待认领，不自动归属；
- 合并主档不直接相加金额：同一账日的账逐笔选用，保留弃用依据；
- 接账与合并并发时，通过注册版本号 + 行级锁使用同一有效版本裁决；
- 未裁决（待认领）条目不供分析；
- 撤销合并按来源还原；合并后新增账无法确定原属时阻止撤销并列出待认领；
- 操作单带幂等键与状态机，中断后恢复原操作，重试不重复生成主档和裁决。
"""
import hashlib
import json
import time
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from src.models import (
    ChannelMaster, ChannelAlias, ChannelOperation, SourceLedgerEntry,
    SystemState, Dataset,
)

# 条目归属状态
ST_AUTO = 'auto'          # 唯一别名自动归属
ST_CLAIMED = 'claimed'    # 人工认领
ST_ADJUDICATED = 'adjudicated'  # 合并逐笔裁决
ST_PENDING = 'pending'    # 歧义待认领
RESOLVED_STATUSES = {ST_AUTO, ST_CLAIMED, ST_ADJUDICATED}

# 主档状态
M_ACTIVE = 'active'
M_DEPRECATED = 'deprecated'

# 操作单状态
OP_RUNNING = 'running'
OP_COMPLETED = 'completed'
OP_FAILED = 'failed'
OP_UNDONE = 'undone'


# ---------- 基础工具 ----------

def _now():
    return time.time()


def _iso_day(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, f'账日格式无效：{value!r}，须为 YYYY-MM-DD')


def entry_fingerprint(dataset_id, row_index, day, original_name):
    raw = json.dumps([dataset_id, row_index, day, original_name], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def get_registry_version(db):
    state = db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, registry_version=0, updated_at=_now())
        db.add(state)
        db.flush()
    return state.registry_version


def bump_registry_version(db):
    state = db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, registry_version=0, updated_at=_now())
        db.add(state)
        db.flush()
    state.registry_version += 1
    state.updated_at = _now()
    db.flush()
    return state.registry_version


def _lock_registry(db):
    """串行化所有改变主档/别名/裁决的操作，保证接账与合并看到同一有效版本。"""
    db.query(SystemState).filter(SystemState.id == 1).with_for_update().first()
    return get_registry_version(db)


# ---------- 别名解析 ----------

def _active_aliases(db, name, day):
    """返回某名称在某账日生效的别名（不同主档）。"""
    rows = db.scalars(
        select(ChannelAlias).where(
            ChannelAlias.alias_name == name,
            ChannelAlias.effective_from <= day,
            (ChannelAlias.effective_to.is_(None)) | (ChannelAlias.effective_to >= day),
        )
    ).all()
    # 只保留仍有效主档上的别名（弃用主档的别名不参与自动解析，留给认领/裁决）
    masters = {m.id: m for m in db.scalars(select(ChannelMaster)).all()}
    return [a for a in rows if masters.get(a.master_id) and masters[a.master_id].status == M_ACTIVE]


def resolve_name(db, name, day):
    """返回 (master_id, 歧义候选列表)。唯一命中自动归属；零个或多个均为待认领。"""
    aliases = _active_aliases(db, name, day)
    master_ids = list(dict.fromkeys(a.master_id for a in aliases))
    if len(master_ids) == 1:
        return master_ids[0], []
    return None, master_ids


def _ensure_master(db, name, actor, version):
    master = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == name))
    if master:
        return master, False
    master = ChannelMaster(
        canonical_name=name, status=M_ACTIVE, created_by=actor,
        created_at=_now(), updated_at=_now(),
    )
    db.add(master)
    db.flush()
    return master, True


def _ensure_alias(db, master_id, name, day, actor, origin):
    """为某主档登记从 day 起生效的别名；同日重复登记幂等。"""
    existing = db.scalar(
        select(ChannelAlias).where(
            ChannelAlias.master_id == master_id,
            ChannelAlias.alias_name == name,
            ChannelAlias.effective_from == day,
        )
    )
    if existing:
        return existing
    alias = ChannelAlias(
        master_id=master_id, alias_name=name, effective_from=day,
        effective_to=None, origin=origin, created_by=actor, created_at=_now(),
    )
    db.add(alias)
    db.flush()
    return alias


# ---------- 接账（ingest）：保留原名，解析归属 ----------

def ingest_dataset_entries(db, dataset, actor='system'):
    """把一个不可变批次的记录登记为逐笔源账条目。

    全程在注册锁内执行，读取当前有效版本；同一批次重复接账幂等。
    返回本次新建条目数。
    """
    _lock_registry(db)
    version = get_registry_version(db)
    existing = db.scalar(
        select(func.count()).select_from(SourceLedgerEntry)
        .where(SourceLedgerEntry.dataset_id == dataset.id)
    )
    if existing:
        return 0
    created = 0
    for index, record in enumerate(dataset.records):
        day = _iso_day(record.get('day'))
        name = str(record.get('channel', '')).strip()
        if not name:
            raise HTTPException(422, f'第 {index + 1} 行渠道名称为空，无法接账')
        master_id, candidates = resolve_name(db, name, day)
        if master_id is None:
            # 未知名称：自动建立主档+别名（新渠道身份）；歧义名称保持待认领
            if not candidates:
                master, _ = _ensure_master(db, name, actor, version)
                _ensure_alias(db, master.id, name, day, actor, 'intake')
                master_id = master.id
                status, basis = ST_AUTO, 'alias'
            else:
                status, basis = ST_PENDING, ''
        else:
            status, basis = ST_AUTO, 'alias'
        entry = SourceLedgerEntry(
            dataset_id=dataset.id, row_index=index, day=day, original_name=name,
            master_id=master_id, resolution_status=status, resolution_basis=basis,
            registry_version=version, fingerprint=entry_fingerprint(dataset.id, index, day, name),
            payload=dict(record), created_at=_now(), updated_at=_now(),
        )
        db.add(entry)
        created += 1
    db.flush()
    return created


# ---------- 待认领与人工认领 ----------

def pending_entries(db):
    return list(db.scalars(
        select(SourceLedgerEntry)
        .where(SourceLedgerEntry.resolution_status == ST_PENDING)
        .order_by(SourceLedgerEntry.day, SourceLedgerEntry.original_name, SourceLedgerEntry.id)
    ).all())


def claim_entry(db, entry_id, master_id, actor, create_master_name=None):
    """认领歧义条目到指定主档；可顺带新建主档。认领同时补齐按账日生效别名。"""
    _lock_registry(db)
    version = get_registry_version(db)
    entry = db.get(SourceLedgerEntry, entry_id)
    if entry is None:
        raise HTTPException(404, '源账条目不存在')
    if entry.resolution_status != ST_PENDING:
        raise HTTPException(409, '该条目已完成归属，不能重复认领；如需调整请使用合并')
    if master_id is None and create_master_name:
        master, _ = _ensure_master(db, create_master_name.strip(), actor, version)
        master_id = master.id
    master = db.get(ChannelMaster, master_id) if master_id else None
    if master is None or master.status != M_ACTIVE:
        raise HTTPException(422, '认领目标主档不存在或已弃用')
    _ensure_alias(db, master.id, entry.original_name, entry.day, actor, 'manual')
    entry.master_id = master.id
    entry.resolution_status = ST_CLAIMED
    entry.resolution_basis = 'claim'
    entry.registry_version = version
    entry.updated_at = _now()
    db.flush()
    return entry


# ---------- 别名主档维护 ----------

def _overlaps(a_from, a_to, b_from, b_to):
    end_a = a_to or '9999-12-31'
    end_b = b_to or '9999-12-31'
    return a_from <= end_b and b_from <= end_a


def add_alias(db, master_id, alias_name, effective_from, actor, effective_to=None,
              allow_ambiguous=False):
    _lock_registry(db)
    master = db.get(ChannelMaster, master_id)
    if master is None or master.status != M_ACTIVE:
        raise HTTPException(422, '主档不存在或已弃用，不能登记别名')
    day = _iso_day(effective_from)
    to_day = _iso_day(effective_to) if effective_to else None
    if to_day and to_day < day:
        raise HTTPException(422, '别名失效账日不能早于生效账日')
    alias_name = alias_name.strip()
    if not alias_name:
        raise HTTPException(422, '别名不能为空')
    # 同一名称在重叠账日若已指向别的有效主档，则构成歧义：
    # 默认拒绝静默覆盖；显式 allow_ambiguous 时登记，之后该账日接账进入待认领
    conflicts = [
        a for a in _active_aliases(db, alias_name, day)
        if a.master_id != master_id and _overlaps(day, to_day, a.effective_from, a.effective_to)
    ]
    if conflicts and not allow_ambiguous:
        raise HTTPException(409, '该名称在此账日区间已指向其他主档，登记会产生歧义；'
                                '如确需并存请确认歧义登记，相关新账将进入待认领')
    alias = ChannelAlias(
        master_id=master_id, alias_name=alias_name, effective_from=day,
        effective_to=to_day, origin='manual', created_by=actor, created_at=_now(),
    )
    db.add(alias)
    db.flush()
    bump_registry_version(db)
    return alias


# ---------- 合并主档：同日账逐笔选用，禁止直接相加 ----------

def _get_or_resume_operation(db, idem_key, op_type):
    """按幂等键取操作单；若存在未完成的同键操作，返回它以便恢复（不重复建单）。

    MySQL 下行锁让并发事务串行、后来者直接读到已存在的操作单；
    SQLite 等无行锁库在唯一索引上抛 IntegrityError，回退查询同一操作单，
    保证两种存储下并发同键请求都不会重复生成主档与裁决。
    """
    op = db.scalar(select(ChannelOperation).where(ChannelOperation.idempotency_key == idem_key))
    if op is not None:
        return op, True
    op = ChannelOperation(
        op_type=op_type, idempotency_key=idem_key, status=OP_RUNNING,
        payload={}, registry_version=0, actor='', created_at=_now(),
    )
    db.add(op)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ChannelOperation).where(
            ChannelOperation.idempotency_key == idem_key))
        if existing is None:
            raise
        return existing, True
    return op, False


def merge_masters(db, source_id, target_id, decisions, actor, idem_key):
    """把 source 主档并入 target。

    decisions: {entry_id: 'target' | 'source'}，同一账日逐笔选用：
      - target：该笔归并入主档（裁决）；
      - source：保留在原主档（该主档因此不能弃用，合并范围缩小）。
    金额永不自动相加；每笔裁决都记录弃用/保留依据。
    """
    if source_id == target_id:
        raise HTTPException(422, '不能将主档并入自身')
    _lock_registry(db)
    op, resumed = _get_or_resume_operation(db, idem_key, 'merge')
    if resumed and op.status == OP_COMPLETED:
        return op  # 幂等重试：直接返回已完成结果，不重复生成主档/裁决
    op.actor = actor or op.actor
    source = db.get(ChannelMaster, source_id)
    target = db.get(ChannelMaster, target_id)
    if source is None or target is None:
        raise HTTPException(404, '参与合并的主档不存在')
    # 恢复一个 running 残留单时，来源可能已在崩溃前被弃用，需允许续算；
    # 全新合并则要求双方均为有效主档。
    if not resumed and (source.status != M_ACTIVE or target.status != M_ACTIVE):
        raise HTTPException(409, '仅两个有效主档可以合并；已弃用主档请先撤销其合并')
    if target.status != M_ACTIVE:
        raise HTTPException(409, '并入目标主档已弃用，无法合并')

    if resumed:
        # 恢复：来源条目 = 本操作单已处理的笔（可能已迁到目标）+ 当前仍挂来源的笔，
        # 避免崩溃后已迁走的笔在续算时被漏算或重复裁决。
        processed = list(db.scalars(
            select(SourceLedgerEntry).where(SourceLedgerEntry.operation_id == op.id)
        ).all())
        still_on_source = list(db.scalars(
            select(SourceLedgerEntry).where(
                SourceLedgerEntry.master_id == source_id,
                SourceLedgerEntry.resolution_status.in_(tuple(RESOLVED_STATUSES)))
        ).all())
        by_id = {e.id: e for e in processed + still_on_source}
        entries = [by_id[i] for i in sorted(by_id)]
    else:
        entries = list(db.scalars(
            select(SourceLedgerEntry)
            .where(SourceLedgerEntry.master_id == source_id,
                   SourceLedgerEntry.resolution_status.in_(tuple(RESOLVED_STATUSES)))
            .order_by(SourceLedgerEntry.day, SourceLedgerEntry.id)
        ).all())
    pending = list(db.scalars(
        select(SourceLedgerEntry)
        .where(SourceLedgerEntry.master_id == source_id,
               SourceLedgerEntry.resolution_status == ST_PENDING)
    ).all())
    if pending:
        raise HTTPException(409, f'来源主档尚有 {len(pending)} 笔待认领账，认领完成后才能合并')

    allowed = {str(e.id) for e in entries}
    bad = set(decisions) - allowed
    if bad:
        raise HTTPException(422, f'裁决包含不属于来源主档的条目：{sorted(bad)}')
    missing = allowed - set(decisions)
    if missing and not resumed:
        raise HTTPException(422, f'还有 {len(missing)} 笔同日账未逐笔选用，合并不允许默认相加')

    version = get_registry_version(db)
    moved, kept = [], []
    for e in entries:
        choice = decisions.get(str(e.id), decisions.get(e.id))
        if resumed:
            # 恢复时以已落库的裁决为准，未处理笔再按本次决策补齐
            if e.operation_id == op.id:
                (moved if e.master_id == target_id else kept).append(e.id)
                continue
        if choice == 'target':
            e.master_id = target_id
            e.resolution_status = ST_ADJUDICATED
            e.resolution_basis = 'merge'
            e.operation_id = op.id
            e.registry_version = version
            e.updated_at = _now()
            moved.append(e.id)
        elif choice == 'source':
            e.resolution_status = ST_ADJUDICATED
            e.resolution_basis = 'merge-keep'
            e.operation_id = op.id
            e.registry_version = version
            e.updated_at = _now()
            kept.append(e.id)
        else:
            raise HTTPException(422, f'条目 {e.id} 的裁决取值无效，须为 target 或 source')

    # 来源主档上仍保留的账 => 主档不能弃用；全部迁走 => 弃用并记录依据
    remaining = db.scalar(
        select(func.count()).select_from(SourceLedgerEntry)
        .where(SourceLedgerEntry.master_id == source_id,
               SourceLedgerEntry.resolution_status.in_(tuple(RESOLVED_STATUSES)))
    )
    deprecated = remaining == 0
    inherited_alias_ids = []
    source_name_set = {source.canonical_name}
    if deprecated:
        source.status = M_DEPRECATED
        source.merged_into_id = target_id
        source.deprecated_reason = f'合并并入主档#{target_id}（{target.canonical_name}），操作单#{op.id}'
        source.updated_at = _now()
        # 来源主档的别名由目标主档继承（按账日生效，保留原名称追溯链）
        for a in db.scalars(select(ChannelAlias).where(ChannelAlias.master_id == source_id)).all():
            source_name_set.add(a.alias_name)
            inherited = _ensure_alias(db, target_id, a.alias_name, a.effective_from, actor, 'merge')
            inherited_alias_ids.append(inherited.id)

    new_version = bump_registry_version(db)
    op.status = OP_COMPLETED
    op.registry_version = new_version
    op.payload = {
        'source_id': source_id, 'target_id': target_id,
        'moved_entry_ids': moved, 'kept_entry_ids': kept,
        'source_deprecated': deprecated, 'decisions': decisions,
        'inherited_alias_ids': inherited_alias_ids,
        'source_name_set': sorted(source_name_set),
    }
    op.finished_at = _now()
    db.flush()
    return op


# ---------- 撤销合并：按来源还原 ----------

def undo_merge(db, merge_op_id, actor, idem_key):
    _lock_registry(db)
    # 幂等优先：同键撤销单已完成则直接返回，不重复还原
    existing = db.scalar(select(ChannelOperation).where(
        ChannelOperation.idempotency_key == idem_key,
        ChannelOperation.op_type == 'undo'))
    if existing is not None and existing.status == OP_COMPLETED:
        return existing
    merge_op = db.get(ChannelOperation, merge_op_id)
    if merge_op is None or merge_op.op_type != 'merge' or merge_op.status != OP_COMPLETED:
        raise HTTPException(404, '待撤销的合并操作不存在、未完成或已撤销')

    op, resumed = _get_or_resume_operation(db, idem_key, 'undo')
    if resumed and op.status == OP_COMPLETED:
        return op
    op.actor = actor or op.actor
    op.related_op_id = merge_op.id

    source_id = merge_op.payload['source_id']
    target_id = merge_op.payload['target_id']
    moved_ids = set(merge_op.payload.get('moved_entry_ids', ()))
    source_name_set = set(merge_op.payload.get('source_name_set', ()))
    merge_finished = merge_op.finished_at or merge_op.created_at

    # 阻断条件：合并之后新增的账，原名来自来源主档名称集且当前挂在目标主档，
    # 系统无法判断它本属来源还是目标 -> 阻止撤销，列出待认领。
    # 目标主档自身名称的账、合并前已存在的账不阻断。
    target_entries = db.scalars(
        select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == target_id)
    ).all()
    blockers = [
        e for e in target_entries
        if e.id not in moved_ids
        and e.created_at >= merge_finished - 1e-6
        and e.original_name in source_name_set
    ]
    if blockers:
        op.status = OP_FAILED
        op.error = '合并后存在无法确定原属的新增账'
        op.finished_at = _now()
        db.flush()
        raise HTTPException(409, {
            'message': '合并后新增的账无法确定原属，撤销已阻止；请先认领以下条目',
            'pending_claim': [_entry_brief(e) for e in blockers],
        })

    version = get_registry_version(db)
    restored = []
    for eid in moved_ids:
        e = db.get(SourceLedgerEntry, eid)
        if e is None or e.master_id != target_id:
            continue  # 已被后续操作处理的笔不强行还原
        e.master_id = source_id
        e.resolution_status = ST_ADJUDICATED
        e.resolution_basis = 'undo'
        e.operation_id = op.id
        e.registry_version = version
        e.updated_at = _now()
        restored.append(eid)

    source = db.get(ChannelMaster, source_id)
    if source and source.merged_into_id == target_id:
        source.status = M_ACTIVE
        source.merged_into_id = None
        source.deprecated_reason = ''
        source.updated_at = _now()

    # 清理合并时继承到目标主档的别名（仅删除继承来源，人工后加的不动）
    for alias_id in merge_op.payload.get('inherited_alias_ids', ()):
        inherited = db.get(ChannelAlias, alias_id)
        if inherited and inherited.master_id == target_id and inherited.origin == 'merge':
            db.delete(inherited)

    new_version = bump_registry_version(db)
    merge_op.status = OP_UNDONE
    op.status = OP_COMPLETED
    op.registry_version = new_version
    op.payload = {'merge_op_id': merge_op_id, 'restored_entry_ids': restored}
    op.finished_at = _now()
    db.flush()
    return op


def _entry_brief(e):
    return {
        'entry_id': e.id, 'dataset_id': e.dataset_id, 'day': e.day,
        'original_name': e.original_name, 'resolution_status': e.resolution_status,
    }


# ---------- 查询与原名追查 ----------

def master_detail(db, master_id):
    master = db.get(ChannelMaster, master_id)
    if master is None:
        raise HTTPException(404, '渠道主档不存在')
    aliases = db.scalars(
        select(ChannelAlias).where(ChannelAlias.master_id == master_id)
        .order_by(ChannelAlias.effective_from.desc(), ChannelAlias.id)
    ).all()
    entries = db.scalars(
        select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == master_id)
        .order_by(SourceLedgerEntry.day, SourceLedgerEntry.id)
    ).all()
    return {
        'id': master.id, 'canonical_name': master.canonical_name, 'status': master.status,
        'deprecated_reason': master.deprecated_reason, 'merged_into_id': master.merged_into_id,
        'registry_version': get_registry_version(db),
        'aliases': [{'id': a.id, 'alias_name': a.alias_name, 'effective_from': a.effective_from,
                     'effective_to': a.effective_to, 'origin': a.origin} for a in aliases],
        'entries': [_entry_brief(e) | {'master_id': e.master_id,
                                       'resolution_basis': e.resolution_basis,
                                       'registry_version': e.registry_version} for e in entries],
        'counts': {
            'total': len(entries),
            'pending': sum(e.resolution_status == ST_PENDING for e in entries),
            'resolved': sum(e.resolution_status in RESOLVED_STATUSES for e in entries),
        },
    }


def list_masters(db):
    masters = db.scalars(select(ChannelMaster).order_by(ChannelMaster.id)).all()
    result = []
    for m in masters:
        entry_count = db.scalar(
            select(func.count()).select_from(SourceLedgerEntry)
            .where(SourceLedgerEntry.master_id == m.id)
        )
        pending_count = db.scalar(
            select(func.count()).select_from(SourceLedgerEntry)
            .where(SourceLedgerEntry.master_id == m.id,
                   SourceLedgerEntry.resolution_status == ST_PENDING)
        )
        result.append({
            'id': m.id, 'canonical_name': m.canonical_name, 'status': m.status,
            'deprecated_reason': m.deprecated_reason, 'merged_into_id': m.merged_into_id,
            'entry_count': entry_count, 'pending_count': pending_count,
        })
    return {'registry_version': get_registry_version(db), 'masters': result}


def trace_original_name(db, name):
    """追查一个原名：命中过哪些别名/主档、涉及哪些逐笔账。"""
    aliases = db.scalars(
        select(ChannelAlias).where(ChannelAlias.alias_name == name)
        .order_by(ChannelAlias.effective_from)
    ).all()
    entries = db.scalars(
        select(SourceLedgerEntry).where(SourceLedgerEntry.original_name == name)
        .order_by(SourceLedgerEntry.day, SourceLedgerEntry.dataset_id, SourceLedgerEntry.row_index)
    ).all()
    master_ids = {a.master_id for a in aliases} | {e.master_id for e in entries if e.master_id}
    masters = {m.id: m.canonical_name for m in db.scalars(select(ChannelMaster)).all()}
    return {
        'name': name,
        'aliases': [{'id': a.id, 'master_id': a.master_id,
                     'master_name': masters.get(a.master_id), 'effective_from': a.effective_from,
                     'effective_to': a.effective_to, 'origin': a.origin} for a in aliases],
        'entries': [_entry_brief(e) | {'master_id': e.master_id,
                                       'master_name': masters.get(e.master_id),
                                       'resolution_basis': e.resolution_basis,
                                       'registry_version': e.registry_version} for e in entries],
    }


# ---------- 分析供给：未裁决账不供分析 ----------

def resolved_records_for_dataset(db, dataset_id):
    """返回可分析记录；存在待认领条目时整体拒绝，避免口径被拆散后静默出数。"""
    pending = db.scalar(
        select(func.count()).select_from(SourceLedgerEntry)
        .where(SourceLedgerEntry.dataset_id == dataset_id,
               SourceLedgerEntry.resolution_status == ST_PENDING)
    )
    total = db.scalar(
        select(func.count()).select_from(SourceLedgerEntry)
        .where(SourceLedgerEntry.dataset_id == dataset_id)
    )
    if not total:
        return None  # 该批次尚未接账，调用方回退到原始记录
    if pending:
        raise HTTPException(409, f'该批次有 {pending} 笔歧义账待认领，未裁决账不供分析')
    entries = db.scalars(
        select(SourceLedgerEntry)
        .where(SourceLedgerEntry.dataset_id == dataset_id,
               SourceLedgerEntry.resolution_status.in_(tuple(RESOLVED_STATUSES)))
        .order_by(SourceLedgerEntry.row_index)
    ).all()
    masters = {m.id: m for m in db.scalars(select(ChannelMaster)).all()}
    records = []
    for e in entries:
        row = dict(e.payload)
        master = masters.get(e.master_id)
        # 分析口径使用主档规范名，原名保留在条目里可追查；已批准结果与归档保持原值不变
        row['channel'] = master.canonical_name if master else e.original_name
        row['_original_channel'] = e.original_name
        row['_master_id'] = e.master_id
        records.append(row)
    return records
