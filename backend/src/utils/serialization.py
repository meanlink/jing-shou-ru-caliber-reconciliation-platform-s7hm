import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
def entry_dict(e):
    return {'id':e.id,'dataset_id':e.dataset_id,'row_index':e.row_index,'day':e.day,
            'original_name':e.original_name,'master_id':e.master_id,
            'resolution_status':e.resolution_status,'resolution_basis':e.resolution_basis,
            'operation_id':e.operation_id,'registry_version':e.registry_version,
            'fingerprint':e.fingerprint,'payload':e.payload}
def operation_dict(o):
    return {'id':o.id,'op_type':o.op_type,'idempotency_key':o.idempotency_key,
            'status':o.status,'payload':o.payload,'registry_version':o.registry_version,
            'related_op_id':o.related_op_id,'actor':o.actor,'error':o.error,
            'created_at':o.created_at,'finished_at':o.finished_at}
