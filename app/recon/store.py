"""收发对账账本：独立 SQLite，事件只增 + 版本化快照。

关键保证：
- 事件表只追加、不修改不删除（原调拨单、收货回执永久留存）。
- 每批处理在一个事务内完成：任一条事件非法则整批回滚，上一版快照原样保留。
- 同 batch_id 重跑：全部事件已存在则直接返回当前版本，不新增加扣减、不产生新版本。
"""
import json
import os
import sqlite3
from pathlib import Path

from app.errors import DomainError
from app.recon.engine import build_conclusion, replay, validate_event

SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_events(
    event_id    TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    order_no    TEXT NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    raw         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_versions(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    batch_id    TEXT NOT NULL,
    conclusion  TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    accepted    INTEGER NOT NULL,
    reused      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_batches(
    batch_id    TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def connection():
    path = Path(os.environ.get('RECON_DB', 'data/recon.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    return db


def _load_events(db, batch_id):
    rows = db.execute(
        'SELECT payload,type,occurred_at,order_no,event_id FROM recon_events '
        'WHERE batch_id=? ORDER BY seq', (batch_id,)).fetchall()
    return [
        {'event_id': r['event_id'], 'type': r['type'], 'order_no': r['order_no'],
         'occurred_at': r['occurred_at'], **json.loads(r['payload'])}
        for r in rows
    ]


def _latest_version(db, batch_id):
    row = db.execute(
        'SELECT version,conclusion,event_count,accepted,created_at,reused '
        'FROM recon_versions WHERE batch_id=? ORDER BY version DESC LIMIT 1',
        (batch_id,)).fetchone()
    if row is None:
        return None
    return {'version': row['version'], 'batch_id': batch_id,
            'conclusion': json.loads(row['conclusion']),
            'event_count': row['event_count'], 'accepted': row['accepted'],
            'reused': bool(row['reused']), 'created_at': row['created_at']}


def get_current(batch_id):
    with connection() as db:
        record = _latest_version(db, batch_id)
    if record is None:
        raise DomainError('NOT_FOUND', '对账批次不存在', 404)
    return record


def list_batches():
    with connection() as db:
        rows = db.execute(
            'SELECT b.batch_id,b.fingerprint,b.created_at,'
            'COUNT(DISTINCT e.event_id) AS event_count,'
            'MAX(v.version) AS version '
            'FROM recon_batches b LEFT JOIN recon_events e ON e.batch_id=b.batch_id '
            'LEFT JOIN recon_versions v ON v.batch_id=b.batch_id '
            'GROUP BY b.batch_id ORDER BY b.created_at DESC').fetchall()
    return [dict(r) for r in rows]


def get_ledger(batch_id):
    """返回原始事件凭证（含原始报文），用于留存复核。"""
    with connection() as db:
        exists = db.execute('SELECT 1 FROM recon_batches WHERE batch_id=?',
                            (batch_id,)).fetchone()
        if not exists:
            raise DomainError('NOT_FOUND', '对账批次不存在', 404)
        rows = db.execute(
            'SELECT event_id,seq,order_no,type,occurred_at,payload,raw,created_at '
            'FROM recon_events WHERE batch_id=? ORDER BY seq', (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def _next_version(db, batch_id):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM recon_versions '
                     'WHERE batch_id=?', (batch_id,)).fetchone()
    return row['v']


def process_batch(batch_id, fingerprint, events, now):
    """处理一批事件，返回 (版本记录, 是否复用)。

    重跑同一批：事件指纹完全一致时复用，不写新事件、不新增版本、不新增加扣减。
    部分重叠（同一 batch_id 带来新 event_id）：增量追加，全部通过后产出新版本。
    """
    if not isinstance(events, list) or not events:
        raise DomainError('RECON_BATCH', 'events 必须是非空数组')

    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO recon_batches(batch_id,fingerprint,created_at) '
                   'VALUES(?,?,?)', (batch_id, fingerprint, now))

        known = {r['event_id'] for r in db.execute(
            'SELECT event_id FROM recon_events WHERE batch_id=?', (batch_id,))}
        incoming = [e for e in events if isinstance(e, dict) and e.get('event_id')]
        incoming_ids = {str(e['event_id']) for e in incoming}
        if len(incoming) != len(events) or len(incoming_ids) != len(incoming):
            raise DomainError('RECON_ID', '事件缺少 event_id 或批次内重复')

        # 纯重跑：全部是已知事件 -> 直接回当前版本，结果零变化
        if incoming_ids and incoming_ids <= known:
            record = _latest_version(db, batch_id)
            db.execute(
                'INSERT INTO recon_versions(version,batch_id,conclusion,event_count,accepted,reused,created_at) '
                'VALUES(?,?,?,?,?,1,?)',
                (_next_version(db, batch_id), batch_id,
                 json.dumps(record['conclusion'], ensure_ascii=False),
                 record['event_count'], 0, now))
            db.commit()
            return _latest_version(db, batch_id), True

        # 增量校验：在当前已知事件重放出的状态上，仅对新事件顺序校验，任一非法整体回滚
        existing_events = _load_events(db, batch_id)
        orders = replay(existing_events) if existing_events else {}
        seen = set(known)
        staged = []
        for evt in events:
            eid = str(evt.get('event_id')) if isinstance(evt, dict) else ''
            if eid in known:
                continue  # 重跑携带的旧事件：幂等跳过，不重复校验、不重复入账
            clean = validate_event(evt, seen, orders)
            if clean['type'] == 'order_approved':
                orders[clean['order_no']] = {}
            apply_safe(orders[clean['order_no']], clean)
            seen.add(clean['event_id'])
            staged.append(clean)

        seq = len(known)
        for clean in staged:
            if clean['event_id'] in known:
                continue
            seq += 1
            db.execute(
                'INSERT INTO recon_events(event_id,batch_id,seq,order_no,type,payload,'
                'occurred_at,raw,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (clean['event_id'], batch_id, seq, clean['order_no'], clean['type'],
                 json.dumps(clean['payload'], ensure_ascii=False), clean['occurred_at'],
                 json.dumps(clean, ensure_ascii=False), now))

        all_events = _load_events(db, batch_id)
        conclusion = build_conclusion(all_events)
        db.execute(
            'INSERT INTO recon_versions(version,batch_id,conclusion,event_count,accepted,reused,created_at) '
            'VALUES(?,?,?,?,?,0,?)',
            (_next_version(db, batch_id), batch_id,
             json.dumps(conclusion, ensure_ascii=False),
             len(all_events), len(staged), now))
        db.commit()
        return _latest_version(db, batch_id), False
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_safe(order, clean):
    from app.recon.engine import apply_event
    apply_event(order, clean)


def list_versions(batch_id):
    with connection() as db:
        exists = db.execute('SELECT 1 FROM recon_batches WHERE batch_id=?',
                            (batch_id,)).fetchone()
        if not exists:
            raise DomainError('NOT_FOUND', '对账批次不存在', 404)
        rows = db.execute(
            'SELECT version,event_count,accepted,reused,created_at '
            'FROM recon_versions WHERE batch_id=? ORDER BY version', (batch_id,)).fetchall()
    return [dict(r) for r in rows]
