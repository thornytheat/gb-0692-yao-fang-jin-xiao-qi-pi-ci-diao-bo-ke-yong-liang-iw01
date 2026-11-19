"""演示用收发事件（确定性、可重复装载）。

覆盖场景：
- T1001 整批发货、门店分两次收齐 -> 已结清路径上的正常分批收货
- T1002 整批发货、短收 -> 短少认定 -> 补发接回原单 -> 补发收齐 -> 可结清
- T1003 短少认定后晚到：晚到不重复冲减短少，超出部分转可再分配
- T1004 仍在途、未收清：不算门店库存，也不许给别家
- T1005 已获批待发货：卡在仓库发货步骤
- T1006 补发在途：补发已发、门店尚未收到
"""
from copy import deepcopy

_EVENTS = [
    # ---- T1001 正常：整批100，分两次收齐 ----
    {'event_id': 'E1001-0', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
     'order_no': 'T1001', 'sku': '0001', 'store': '门店01', 'lot': 'B0002', 'qty': 100},
    {'event_id': 'E1001-1', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
     'order_no': 'T1001', 'shipment_no': 'S1001-A', 'kind': 'original', 'qty': 100},
    {'event_id': 'E1001-2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
     'order_no': 'T1001', 'shipment_no': 'S1001-A', 'receipt_no': 'RC1001-1', 'qty': 60},
    {'event_id': 'E1001-3', 'type': 'receipt_recorded', 'occurred_at': '2026-11-13T10:00:00Z',
     'order_no': 'T1001', 'shipment_no': 'S1001-A', 'receipt_no': 'RC1001-2', 'qty': 40},
    {'event_id': 'E1001-4', 'type': 'order_closed', 'occurred_at': '2026-11-13T15:00:00Z',
     'order_no': 'T1001'},

    # ---- T1002 短收 -> 短少认定 -> 补发接回原单 -> 收齐 ----
    {'event_id': 'E1002-0', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
     'order_no': 'T1002', 'sku': '0002', 'store': '门店02', 'lot': 'B0003', 'qty': 80},
    {'event_id': 'E1002-1', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
     'order_no': 'T1002', 'shipment_no': 'S1002-A', 'kind': 'original', 'qty': 80},
    {'event_id': 'E1002-2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
     'order_no': 'T1002', 'shipment_no': 'S1002-A', 'receipt_no': 'RC1002-1', 'qty': 70},
    {'event_id': 'E1002-3', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
     'order_no': 'T1002', 'shipment_no': 'S1002-A', 'qty': 10, 'reason': '运输短少10件'},
    {'event_id': 'E1002-4', 'type': 'shipment_sent', 'occurred_at': '2026-11-14T09:00:00Z',
     'order_no': 'T1002', 'shipment_no': 'S1002-B', 'kind': 'reship', 'qty': 10},
    {'event_id': 'E1002-5', 'type': 'receipt_recorded', 'occurred_at': '2026-11-15T10:00:00Z',
     'order_no': 'T1002', 'shipment_no': 'S1002-B', 'receipt_no': 'RC1002-2', 'qty': 10},

    # ---- T1003 短少认定后晚到：不重复冲减，晚到转可再分配 ----
    {'event_id': 'E1003-0', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
     'order_no': 'T1003', 'sku': '0001', 'store': '门店03', 'lot': 'B0004', 'qty': 50},
    {'event_id': 'E1003-1', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
     'order_no': 'T1003', 'shipment_no': 'S1003-A', 'kind': 'original', 'qty': 50},
    {'event_id': 'E1003-2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
     'order_no': 'T1003', 'shipment_no': 'S1003-A', 'receipt_no': 'RC1003-1', 'qty': 40},
    {'event_id': 'E1003-3', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
     'order_no': 'T1003', 'shipment_no': 'S1003-A', 'qty': 10, 'reason': '门店短收10件'},
    # 其中 6 件晚到：在途已被短少占满，6 件全部转可再分配，短少仍记 10
    {'event_id': 'E1003-4', 'type': 'receipt_recorded', 'occurred_at': '2026-11-16T14:00:00Z',
     'order_no': 'T1003', 'shipment_no': 'S1003-A', 'receipt_no': 'RC1003-2', 'qty': 6},

    # ---- T1004 在途未收清：不进门店库存、不许再分配 ----
    {'event_id': 'E1004-0', 'type': 'order_approved', 'occurred_at': '2026-11-12T08:00:00Z',
     'order_no': 'T1004', 'sku': '0002', 'store': '门店04', 'lot': 'B0005', 'qty': 60},
    {'event_id': 'E1004-1', 'type': 'shipment_sent', 'occurred_at': '2026-11-13T09:00:00Z',
     'order_no': 'T1004', 'shipment_no': 'S1004-A', 'kind': 'original', 'qty': 60},
    {'event_id': 'E1004-2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-14T10:00:00Z',
     'order_no': 'T1004', 'shipment_no': 'S1004-A', 'receipt_no': 'RC1004-1', 'qty': 25},

    # ---- T1005 已获批待发货 ----
    {'event_id': 'E1005-0', 'type': 'order_approved', 'occurred_at': '2026-11-15T08:00:00Z',
     'order_no': 'T1005', 'sku': '0001', 'store': '门店05', 'lot': 'B0006', 'qty': 40},

    # ---- T1006 补发在途 ----
    {'event_id': 'E1006-0', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
     'order_no': 'T1006', 'sku': '0002', 'store': '门店06', 'lot': 'B0007', 'qty': 90},
    {'event_id': 'E1006-1', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
     'order_no': 'T1006', 'shipment_no': 'S1006-A', 'kind': 'original', 'qty': 90},
    {'event_id': 'E1006-2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
     'order_no': 'T1006', 'shipment_no': 'S1006-A', 'receipt_no': 'RC1006-1', 'qty': 75},
    {'event_id': 'E1006-3', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
     'order_no': 'T1006', 'shipment_no': 'S1006-A', 'qty': 15, 'reason': '门店短收15件'},
    {'event_id': 'E1006-4', 'type': 'shipment_sent', 'occurred_at': '2026-11-14T09:00:00Z',
     'order_no': 'T1006', 'shipment_no': 'S1006-B', 'kind': 'reship', 'qty': 15},
]


def demo_events():
    return deepcopy(_EVENTS)
