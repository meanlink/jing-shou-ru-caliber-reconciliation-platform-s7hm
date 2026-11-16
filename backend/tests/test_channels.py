"""渠道主档/别名/认领/合并/撤销的端到端测试。

用 SQLite 内存库与 FastAPI TestClient，不依赖 MySQL/Redis；
worker 分析拦截与中断恢复直接调用服务层验证。
"""
import os
import time
import uuid

import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', '/tmp/channel-test-artifacts')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as db_config  # noqa: E402

# 内存 SQLite 跨连接会丢表，用 StaticPool 让所有会话共享同一连接
db_config.engine = create_engine(
    'sqlite://', connect_args={'check_same_thread': False},
    poolclass=StaticPool, future=True,
)
db_config.Session.configure(bind=db_config.engine)
from src.config.database import Session, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models import (  # noqa: E402
    User, Dataset, ChannelMaster, ChannelAlias, ChannelOperation, SourceLedgerEntry, Job,
)
from src.services import channels  # noqa: E402
from src.services.business import analyze  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.utils.serialization import checksum  # noqa: E402


def _row(day, channel, gross=100.0):
    return {'day': day, 'channel': channel, 'currency': 'CNY', 'gross': gross,
            'refunds': 1.0, 'discount': 2.0, 'recognized_net': gross - 4.0, 'visits': 10}


@pytest.fixture()
def client():
    # 每个测试重建内存表
    from src.config.database import Base
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=f'{role}@demo.local', name=role, role=role,
                        password=hash_password('Demo123!')))
        db.commit()
    with TestClient(app) as c:
        yield c


def _token(client, role):
    r = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return r.json()['access_token']


def _auth(client, role):
    return {'Authorization': 'Bearer ' + _token(client, role)}


def _make_dataset(name, rows):
    with Session() as db:
        d = Dataset(name=name, source=name + '.csv', records=rows,
                    checksum=checksum(rows), created_at=time.time())
        db.add(d)
        db.flush()
        channels.ingest_dataset_entries(db, d)
        db.commit()
        return d.id


# ---------- 接账与原名保留 ----------

def test_ingest_keeps_original_name_and_auto_masters(client):
    ds = _make_dataset('批次甲', [_row('2026-09-01', '甲渠道'), _row('2026-09-02', '甲渠道')])
    r = client.get('/channels', headers=_auth(client, 'viewer'))
    body = r.json()
    assert r.status_code == 200
    master = next(m for m in body['masters'] if m['canonical_name'] == '甲渠道')
    assert master['entry_count'] == 2 and master['pending_count'] == 0
    with Session() as db:
        entries = db.scalars(select(SourceLedgerEntry).where(SourceLedgerEntry.dataset_id == ds)).all()
        assert all(e.original_name == '甲渠道' for e in entries)
        assert all(e.resolution_status == 'auto' for e in entries)


def test_renamed_channel_resolves_via_dated_alias(client):
    _make_dataset('旧名批', [_row('2026-09-01', '星河渠道')])
    with Session() as db:
        master = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '星河渠道'))
        master_id = master.id
    # 登记 09-10 起生效的新别名
    r = client.post('/channels/aliases', headers=_auth(client, 'analyst'),
                    json={'master_id': master_id, 'alias_name': '星河Pro',
                          'effective_from': '2026-09-10'})
    assert r.status_code == 201
    ds2 = _make_dataset('新名批', [_row('2026-09-10', '星河Pro'), _row('2026-09-11', '星河Pro')])
    with Session() as db:
        entries = db.scalars(select(SourceLedgerEntry).where(SourceLedgerEntry.dataset_id == ds2)).all()
        assert all(e.master_id == master_id for e in entries)
        assert all(e.original_name == '星河Pro' for e in entries)  # 原名未被改写


# ---------- 歧义与待认领 ----------

def _seed_ambiguous_dataset(client):
    _make_dataset('批1', [_row('2026-09-01', '渠道X')])
    _make_dataset('批2', [_row('2026-09-01', '渠道Y')])
    with Session() as db:
        x = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '渠道X')).id
        y = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '渠道Y')).id
    # 默认拒绝静默歧义
    r = client.post('/channels/aliases', headers=_auth(client, 'admin'),
                    json={'master_id': y, 'alias_name': '渠道X', 'effective_from': '2026-09-05'})
    assert r.status_code == 409
    # 显式确认歧义
    r = client.post('/channels/aliases', headers=_auth(client, 'admin'),
                    json={'master_id': y, 'alias_name': '渠道X', 'effective_from': '2026-09-05',
                          'allow_ambiguous': True})
    assert r.status_code == 201
    ds3 = _make_dataset('歧义批', [_row('2026-09-05', '渠道X')])
    pending = client.get('/channels/pending', headers=_auth(client, 'viewer')).json()
    assert len(pending) == 1 and pending[0]['resolution_status'] == 'pending'
    # 未裁决账不供分析
    with Session() as db:
        with pytest.raises(Exception) as ei:
            channels.resolved_records_for_dataset(db, ds3)
        assert '待认领' in str(ei.value)
    return ds3, x, pending[0]['id']


def test_ambiguous_name_becomes_pending_and_blocks_analysis(client):
    ds3, x_id, entry_id = _seed_ambiguous_dataset(client)
    assert ds3 and x_id and entry_id


def test_claim_resolves_pending_and_analysis_uses_canonical_name(client):
    ds3, x_id, entry_id = _seed_ambiguous_dataset(client)
    r = client.post(f'/channels/entries/{entry_id}/claim', headers=_auth(client, 'analyst'),
                    json={'master_id': x_id})
    assert r.status_code == 200 and r.json()['resolution_status'] == 'claimed'
    with Session() as db:
        records = channels.resolved_records_for_dataset(db, ds3)
        assert records[0]['channel'] == '渠道X'
        assert records[0]['_original_channel'] == '渠道X'
        result = analyze(records, {'expression': 'sum(gross)-sum(refunds)-sum(discount)',
                                   'group_by': 'channel', 'zero_policy': 'reject'})
        assert result['table']['rows'][0]['dimension'] == '渠道X'
    # 重复认领被拒
    r = client.post(f'/channels/entries/{entry_id}/claim', headers=_auth(client, 'analyst'),
                    json={'master_id': x_id})
    assert r.status_code == 409


def test_viewer_cannot_write(client):
    r = client.post('/channels/aliases', headers=_auth(client, 'viewer'),
                    json={'master_id': 1, 'alias_name': 'Z', 'effective_from': '2026-09-01'})
    assert r.status_code == 403


# ---------- 合并：逐笔裁决、禁止相加、弃用依据 ----------

def test_merge_requires_per_entry_decisions_and_deprecates_when_fully_moved(client):
    _make_dataset('A批', [_row('2026-09-01', '主A', 100.0), _row('2026-09-02', '主A', 110.0)])
    _make_dataset('B批', [_row('2026-09-01', '主B', 200.0), _row('2026-09-02', '主B', 220.0)])
    with Session() as db:
        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主A')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主B')).id
        b_entries = db.scalars(select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b)
                               .order_by(SourceLedgerEntry.id)).all()
        b_ids = [e.id for e in b_entries]
    # 缺一笔裁决 -> 拒绝（不允许默认相加）
    r = client.post('/channels/merge', headers=_auth(client, 'admin'),
                    json={'source_id': b, 'target_id': a,
                          'decisions': {str(b_ids[0]): 'target'},
                          'idempotency_key': 'k' + uuid.uuid4().hex})
    assert r.status_code == 422 and '逐笔选用' in r.json()['detail']
    # analyst 不能合并
    r = client.post('/channels/merge', headers=_auth(client, 'analyst'),
                    json={'source_id': b, 'target_id': a,
                          'decisions': {str(i): 'target' for i in b_ids},
                          'idempotency_key': 'k' + uuid.uuid4().hex})
    assert r.status_code == 403
    # 完整逐笔裁决
    key = 'merge-' + uuid.uuid4().hex
    r = client.post('/channels/merge', headers=_auth(client, 'admin'),
                    json={'source_id': b, 'target_id': a,
                          'decisions': {str(i): 'target' for i in b_ids},
                          'idempotency_key': key})
    assert r.status_code == 201
    op = r.json()
    assert op['status'] == 'completed' and sorted(op['payload']['moved_entry_ids']) == sorted(b_ids)
    with Session() as db:
        b_master = db.get(ChannelMaster, b)
        assert b_master.status == 'deprecated'
        assert b_master.merged_into_id == a and b_master.deprecated_reason
        # 金额未被相加：逐笔 master_id 已改，数值字段原样
        for eid in b_ids:
            e = db.get(SourceLedgerEntry, eid)
            assert e.master_id == a and e.resolution_status == 'adjudicated'
            assert e.payload['gross'] in (200.0, 220.0)
        # 别名继承
        assert db.scalar(select(ChannelAlias).where(
            ChannelAlias.master_id == a, ChannelAlias.alias_name == '主B',
            ChannelAlias.origin == 'merge')) is not None
    # 同键重试不重复
    r2 = client.post('/channels/merge', headers=_auth(client, 'admin'),
                     json={'source_id': b, 'target_id': a,
                           'decisions': {str(i): 'target' for i in b_ids},
                           'idempotency_key': key})
    assert r2.json()['id'] == op['id']


def test_merge_kept_entries_keep_master_active(client):
    _make_dataset('A批', [_row('2026-09-01', '主A')])
    _make_dataset('B批', [_row('2026-09-01', '主B'), _row('2026-09-02', '主B')])
    with Session() as db:
        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主A')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主B')).id
        b_entries = db.scalars(select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b)
                               .order_by(SourceLedgerEntry.id)).all()
    decisions = {str(b_entries[0].id): 'target', str(b_entries[1].id): 'source'}
    r = client.post('/channels/merge', headers=_auth(client, 'admin'),
                    json={'source_id': b, 'target_id': a, 'decisions': decisions,
                          'idempotency_key': 'merge-' + uuid.uuid4().hex})
    assert r.status_code == 201
    with Session() as db:
        assert db.get(ChannelMaster, b).status == 'active'  # 有保留账，不弃用
        kept = db.get(SourceLedgerEntry, b_entries[1].id)
        moved = db.get(SourceLedgerEntry, b_entries[0].id)
        assert kept.master_id == b and kept.resolution_basis == 'merge-keep'
        assert moved.master_id == a and moved.resolution_basis == 'merge'


# ---------- 撤销：阻断、按来源还原 ----------

def test_undo_blocked_by_undeterminable_new_entries_then_restores(client):
    _make_dataset('A批', [_row('2026-09-01', '主A')])
    _make_dataset('B批', [_row('2026-09-01', '主B'), _row('2026-09-02', '主B')])
    with Session() as db:
        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主A')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '主B')).id
        b_ids = [e.id for e in db.scalars(
            select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b))]
    key = 'merge-' + uuid.uuid4().hex
    r = client.post('/channels/merge', headers=_auth(client, 'admin'),
                    json={'source_id': b, 'target_id': a,
                          'decisions': {str(i): 'target' for i in b_ids},
                          'idempotency_key': key})
    merge_op_id = r.json()['id']
    # 合并后新增一笔来源名称的账（经继承别名自动挂到 A）
    time.sleep(0.01)
    new_ds = _make_dataset('合并后新批', [_row('2026-09-03', '主B', 999.0)])
    r = client.post('/channels/undo', headers=_auth(client, 'admin'),
                    json={'merge_operation_id': merge_op_id,
                          'idempotency_key': 'undo-' + uuid.uuid4().hex})
    assert r.status_code == 409
    detail = r.json()['detail']
    assert isinstance(detail, dict) and detail['pending_claim']
    assert detail['pending_claim'][0]['original_name'] == '主B'
    # 干净撤销：删除阻断账后按来源还原
    with Session() as db:
        db.query(SourceLedgerEntry).filter_by(dataset_id=new_ds).delete()
        db.commit()
    r = client.post('/channels/undo', headers=_auth(client, 'admin'),
                    json={'merge_operation_id': merge_op_id,
                          'idempotency_key': 'undo-' + uuid.uuid4().hex})
    assert r.status_code == 201
    with Session() as db:
        assert db.get(ChannelMaster, b).status == 'active'
        assert db.get(ChannelOperation, merge_op_id).status == 'undone'
        for eid in b_ids:
            assert db.get(SourceLedgerEntry, eid).master_id == b
        # 继承别名已清理
        assert db.scalar(select(ChannelAlias).where(
            ChannelAlias.master_id == a, ChannelAlias.alias_name == '主B',
            ChannelAlias.origin == 'merge')) is None


# ---------- 中断恢复与同一有效版本 ----------

def test_interrupted_operation_resumes_without_duplication():
    with Session() as db:
        for name in ('渠道一', '渠道二'):
            db.add(ChannelMaster(canonical_name=name, status='active',
                                 created_at=time.time(), updated_at=time.time()))
        db.commit()
        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '渠道一')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '渠道二')).id
    _make_dataset('一', [_row('2026-09-01', '渠道一')])
    _make_dataset('二', [_row('2026-09-01', '渠道二'), _row('2026-09-02', '渠道二')])
    key = 'resume-' + uuid.uuid4().hex
    with Session() as db:
        b_ids = [e.id for e in db.scalars(
            select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b))]
        # 模拟操作单已建但中途崩溃（running 残留）
        stale = ChannelOperation(op_type='merge', idempotency_key=key, status='running',
                                 payload={}, actor='tester', created_at=time.time())
        db.add(stale)
        db.commit()
        stale_id = stale.id
        # 同键重试：恢复原操作而非新建
        op = channels.merge_masters(
            db, b, a, {str(i): 'target' for i in b_ids}, 'tester', key)
        db.commit()
        assert op.id == stale_id and op.status == 'completed'
        # 裁决只生成一次
        adjudicated = db.scalar(select(ChannelOperation).where(ChannelOperation.id == stale_id))
        assert len(adjudicated.payload['moved_entry_ids']) == len(b_ids)
        total_ops = db.query(ChannelOperation).filter(ChannelOperation.idempotency_key == key).count()
        assert total_ops == 1


def test_partially_applied_merge_resumes_and_finishes():
    """模拟合并在部分笔已裁决后崩溃（running 残留），同键重试应续算而非重复。"""
    with Session() as db:
        for name in ('恢复甲', '恢复乙'):
            db.add(ChannelMaster(canonical_name=name, status='active',
                                 created_at=time.time(), updated_at=time.time()))
        db.commit()
        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '恢复甲')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '恢复乙')).id
    _make_dataset('恢复甲批', [_row('2026-09-01', '恢复甲')])
    _make_dataset('恢复乙批', [_row('2026-09-01', '恢复乙'), _row('2026-09-02', '恢复乙'),
                              _row('2026-09-03', '恢复乙')])
    key = 'partial-' + uuid.uuid4().hex
    with Session() as db:
        b_entries = db.scalars(select(SourceLedgerEntry).where(
            SourceLedgerEntry.master_id == b).order_by(SourceLedgerEntry.id)).all()
        all_b_ids = [e.id for e in b_entries]
        # 残留 running 操作单
        stale = ChannelOperation(op_type='merge', idempotency_key=key, status='running',
                                 payload={}, actor='tester', created_at=time.time())
        db.add(stale)
        db.flush()
        # 仅第一笔已落库裁决，随后崩溃
        first = b_entries[0]
        first.master_id = a
        first.resolution_status = 'adjudicated'
        first.resolution_basis = 'merge'
        first.operation_id = stale.id
        db.commit()
        stale_id = stale.id
        # 同键重试，补齐其余两笔（决策覆盖全部三笔，首笔已落库则续算跳过）
        decisions = {str(eid): 'target' for eid in all_b_ids}
        op = channels.merge_masters(db, b, a, decisions, 'tester', key)
        db.commit()
        assert op.id == stale_id and op.status == 'completed'
        assert len(op.payload['moved_entry_ids']) == 3 and op.payload['kept_entry_ids'] == []
        # 每笔只裁决一次，无重复
        for eid in all_b_ids:
            fresh = db.get(SourceLedgerEntry, eid)
            assert fresh.master_id == a and fresh.operation_id == stale_id
        assert db.get(ChannelMaster, b).status == 'deprecated'
        assert db.query(ChannelOperation).filter(ChannelOperation.idempotency_key == key).count() == 1


def test_registry_version_monotonic_and_shared_by_entries():
    ds = _make_dataset('版本批', [_row('2026-09-01', '版本渠道')])
    with Session() as db:
        v0 = channels.get_registry_version(db)
        entries = db.scalars(select(SourceLedgerEntry).where(SourceLedgerEntry.dataset_id == ds)).all()
        assert all(e.registry_version == v0 for e in entries)  # 接账读取同一有效版本
        master = entries[0].master_id
        channels.add_alias(db, master, '版本渠道改名', '2026-10-01', 'tester')
        db.commit()
        assert channels.get_registry_version(db) == v0 + 1


def test_approved_results_and_archives_keep_original_values():
    """合并/撤销只改逐笔归属；已批准结果与归档是不可变快照，保留原值。"""
    import json
    import os
    from src.config.settings import ARTIFACT_DIR
    from src.services.reports import deliver
    from src.models import Report, Delivery

    ds_a = _make_dataset('归档A', [_row('2026-09-01', '归档甲', 100.0)])
    ds_b = _make_dataset('归档B', [_row('2026-09-01', '归档乙', 200.0)])
    with Session() as db:
        admin = db.scalar(select(User).where(User.role == 'admin'))
        params = {'expression': 'sum(gross)-sum(refunds)-sum(discount)',
                  'group_by': 'channel', 'zero_policy': 'reject'}
        # 合并前先完成并批准一个基于乙的结果
        job_b = Job(name='归档基线', dataset_id=ds_b, params=params,
                    status='completed', approved=True, created_by=admin.id,
                    created_at=time.time(), finished_at=time.time(),
                    result=analyze([_row('2026-09-01', '归档乙', 200.0)], params))
        db.add(job_b)
        db.flush()
        snapshot = json.loads(json.dumps(job_b.result, ensure_ascii=False))
        report = Report(name='归档简报', job_id=job_b.id, destination='本地收件箱',
                        interval_seconds=86400, next_run=time.time() + 86400, enabled=True)
        db.add(report)
        db.flush()
        delivery = deliver(db, report)
        db.commit()
        delivery_id = delivery.id
        artifact = os.path.join(ARTIFACT_DIR, delivery.filename + '.json')
        with open(artifact, encoding='utf-8') as f:
            archived = json.load(f)

        a = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '归档甲')).id
        b = db.scalar(select(ChannelMaster).where(ChannelMaster.canonical_name == '归档乙')).id
        b_ids = [e.id for e in db.scalars(
            select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b))]

    # 合并 乙->甲 再撤销
    key = 'arch-merge-' + uuid.uuid4().hex
    with Session() as db:
        channels.merge_masters(db, b, a, {str(i): 'target' for i in b_ids}, 'tester', key)
        db.commit()
    with Session() as db:
        merge_op = db.scalar(select(ChannelOperation).where(
            ChannelOperation.idempotency_key == key))
        channels.undo_merge(db, merge_op.id, 'tester', 'arch-undo-' + uuid.uuid4().hex)
        db.commit()

    with Session() as db:
        job_after = db.scalar(select(Job).where(Job.name == '归档基线'))
        # 已批准结果逐字段保持原值
        assert job_after.approved is True
        assert job_after.result == snapshot
        d_after = db.get(Delivery, delivery_id)
        assert d_after.checksum == delivery.checksum
        with open(os.path.join(ARTIFACT_DIR, d_after.filename + '.json'), encoding='utf-8') as f:
            assert json.load(f) == archived  # 归档文件原值未变


def test_undo_not_blocked_by_targets_own_new_entries():
    """合并后新增的若是目标主档自身名称的账，归属明确，不应阻止撤销。"""
    _make_dataset('T-A', [_row('2026-09-01', '目标主档'), _row('2026-09-02', '目标主档')])
    _make_dataset('T-B', [_row('2026-09-01', '来源主档')])
    with Session() as db:
        a = db.scalar(select(ChannelMaster).where(
            ChannelMaster.canonical_name == '目标主档')).id
        b = db.scalar(select(ChannelMaster).where(
            ChannelMaster.canonical_name == '来源主档')).id
        b_ids = [e.id for e in db.scalars(
            select(SourceLedgerEntry).where(SourceLedgerEntry.master_id == b))]
    r_key = 'nb-merge-' + uuid.uuid4().hex
    with Session() as db:
        op = channels.merge_masters(db, b, a, {str(i): 'target' for i in b_ids},
                                    'tester', r_key)
        db.commit()
        merge_id = op.id
    # 合并后新增目标主档自身名称的账（归属明确，不阻断）
    time.sleep(0.01)
    _make_dataset('T-A-new', [_row('2026-09-03', '目标主档', 888.0)])
    with Session() as db:
        undo = channels.undo_merge(db, merge_id, 'tester', 'nb-undo-' + uuid.uuid4().hex)
        db.commit()
        assert undo.status == 'completed'
        assert db.get(ChannelMaster, b).status == 'active'


# ---------- 原名追查 ----------

def test_trace_original_name(client):
    _make_dataset('追查批', [_row('2026-09-01', '灯塔渠道')])
    r = client.get('/channels/trace', params={'name': '灯塔渠道'}, headers=_auth(client, 'viewer'))
    assert r.status_code == 200
    body = r.json()
    assert body['name'] == '灯塔渠道'
    assert len(body['aliases']) == 1 and len(body['entries']) == 1
    assert body['entries'][0]['resolution_status'] == 'auto'


# ---------- worker：未裁决账不供分析，已裁决按主档口径计算 ----------

def test_worker_blocks_pending_and_computes_resolved():
    from src.worker import execute
    # 干净批次：可正常执行
    ds_id = _make_dataset('工作批', [_row('2026-09-01', '工作渠道', 100.0),
                                    _row('2026-09-02', '工作渠道', 120.0)])
    with Session() as db:
        admin = db.scalar(select(User).where(User.role == 'admin'))
        job = Job(name='J1', dataset_id=ds_id,
                  params={'expression': 'sum(gross)-sum(refunds)-sum(discount)',
                          'group_by': 'channel', 'zero_policy': 'reject'},
                  status='queued', created_by=admin.id, created_at=time.time())
        db.add(job)
        db.commit()
        job_id = job.id
    execute(job_id)  # Redis 不可用不影响 SQL 兜底执行
    with Session() as db:
        job = db.get(Job, job_id)
        assert job.status == 'completed'
        assert job.result['table']['rows'][0]['dimension'] == '工作渠道'

    # 人为制造一笔待认领 -> worker 必须失败而不是出数
    with Session() as db:
        other = ChannelMaster(canonical_name='另一渠道', status='active',
                              created_at=time.time(), updated_at=time.time())
        db.add(other)
        db.flush()
        channels.add_alias(db, other.id, '工作渠道', '2026-09-01', 'tester',
                           allow_ambiguous=True)
        db.commit()
        ds2 = _make_dataset('工作批-歧义', [_row('2026-09-01', '工作渠道', 50.0)])
        admin = db.scalar(select(User).where(User.role == 'admin'))
        job2 = Job(name='J2', dataset_id=ds2,
                   params={'expression': 'sum(gross)-sum(refunds)-sum(discount)',
                           'group_by': 'channel', 'zero_policy': 'reject'},
                   status='queued', created_by=admin.id, created_at=time.time())
        db.add(job2)
        db.commit()
        job2_id = job2.id
    execute(job2_id)
    with Session() as db:
        job2 = db.get(Job, job2_id)
        assert job2.status == 'failed'
        assert '待认领' in job2.error
