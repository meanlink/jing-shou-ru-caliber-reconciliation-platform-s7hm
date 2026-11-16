from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import ChannelOperation
from src.schemas.channel import AliasInput, ClaimInput, MergeInput, UndoInput
from src.services import channels
from src.services.audit import record
from src.utils.serialization import entry_dict, operation_dict

router = APIRouter(prefix='/channels')


@router.get('')
def masters(db=Depends(get_db), u=Depends(current_user)):
    return channels.list_masters(db)


@router.get('/pending')
def pending(db=Depends(get_db), u=Depends(current_user)):
    return [entry_dict(e) for e in channels.pending_entries(db)]


@router.get('/operations')
def operations(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(ChannelOperation).order_by(ChannelOperation.id.desc()).limit(100)).all()
    return [operation_dict(o) for o in rows]


@router.get('/trace')
def trace(name: str, db=Depends(get_db), u=Depends(current_user)):
    return channels.trace_original_name(db, name.strip())


@router.get('/{master_id}')
def detail(master_id: int, db=Depends(get_db), u=Depends(current_user)):
    return channels.master_detail(db, master_id)


@router.post('/aliases', status_code=201)
def add_alias(data: AliasInput, db=Depends(get_db), u=Depends(can_write)):
    a = channels.add_alias(db, data.master_id, data.alias_name, data.effective_from,
                           u.email, data.effective_to, data.allow_ambiguous)
    record(db, u.email, 'channel.alias.add',
           {'master_id': a.master_id, 'alias': a.alias_name, 'from': a.effective_from})
    db.commit()
    return channels.master_detail(db, a.master_id)


@router.post('/entries/{entry_id}/claim')
def claim(entry_id: int, data: ClaimInput, db=Depends(get_db), u=Depends(can_write)):
    if data.master_id is None and not data.create_master_name:
        raise HTTPException(422, '认领须指定已有主档或新主档名称')
    e = channels.claim_entry(db, entry_id, data.master_id, u.email, data.create_master_name)
    record(db, u.email, 'channel.entry.claim',
           {'entry_id': e.id, 'master_id': e.master_id, 'original_name': e.original_name})
    db.commit()
    return entry_dict(e)


@router.post('/merge', status_code=201)
def merge(data: MergeInput, db=Depends(get_db), u=Depends(admin_only)):
    op = channels.merge_masters(db, data.source_id, data.target_id, data.decisions,
                                u.email, data.idempotency_key)
    record(db, u.email, 'channel.merge',
           {'op_id': op.id, 'source': data.source_id, 'target': data.target_id,
            'status': op.status, 'version': op.registry_version})
    db.commit()
    return operation_dict(op)


@router.post('/undo', status_code=201)
def undo(data: UndoInput, db=Depends(get_db), u=Depends(admin_only)):
    op = channels.undo_merge(db, data.merge_operation_id, u.email, data.idempotency_key)
    record(db, u.email, 'channel.undo',
           {'op_id': op.id, 'merge_op': data.merge_operation_id, 'version': op.registry_version})
    db.commit()
    return operation_dict(op)
