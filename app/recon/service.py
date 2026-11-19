"""收发对账服务层：批次身份、指纹与用例编排。"""
import hashlib
import json
from datetime import datetime, timezone

from app.recon import store

RULE_VERSION = 'recon-2026.11.19.1'


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _canonical(events):
    return json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(events):
    return hashlib.sha256(_canonical(events).encode('utf-8')).hexdigest()


def submit(events, batch_id=None):
    """提交一批收发事件。

    batch_id 可由调用方指定（同一业务批次重跑复用）；缺省由事件内容派生，
    保证同内容重跑落在同一批次，实现幂等。
    """
    if not isinstance(events, list) or not events:
        from app.errors import DomainError
        raise DomainError('RECON_BATCH', 'events 必须是非空数组')
    fp = fingerprint(events)
    if not batch_id:
        batch_id = 'R' + hashlib.sha256((RULE_VERSION + ':' + fp).encode('utf-8')).hexdigest()[:15]
    record, reused = store.process_batch(batch_id, fp, events, _now())
    return {'batch_id': batch_id, 'reused': reused,
            'rule_version': RULE_VERSION, 'fingerprint': fp, **record}


def current(batch_id):
    record = store.get_current(batch_id)
    return {'batch_id': batch_id, 'rule_version': RULE_VERSION, **record}


def batches():
    return store.list_batches()


def ledger(batch_id):
    return {'batch_id': batch_id, 'rule_version': RULE_VERSION,
            'events': store.get_ledger(batch_id),
            'versions': store.list_versions(batch_id)}


def reset_demo(batch_id, events):
    """演示数据装载：固定批次，重复装载走幂等重跑路径，不产生加扣减。"""
    return submit(events, batch_id=batch_id)
