"""收发对账领域引擎测试：守恒、补发接原单、晚到不重复冲减、状态机。"""
import pytest

from app.errors import DomainError
from app.recon.engine import build_conclusion, replay, validate_event
from app.recon.seed import demo_events


def _order(events, no):
    rows = build_conclusion(events)['rows']
    return next(r for r in rows if r['order_no'] == no)


def _approved(no='T1', qty=100):
    return {'event_id': no + '-a', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
            'order_no': no, 'sku': '0001', 'store': '门店X', 'lot': 'L1', 'qty': qty}


def test_demo_all_orders_balance():
    conclusion = build_conclusion(demo_events())
    assert conclusion['totals']['all_balanced']
    for r in conclusion['rows']:
        assert r['approved_qty'] == (r['pending_issue_qty'] + r['received_qty']
                                     + r['in_transit_qty'] + r['shortage_qty']
                                     + r['redistributable_qty'])


def test_split_receipt_keeps_unreceived_in_transit():
    # 整批100发出，分两次收60/30：未收10件在途，不是门店库存，也不可再分配
    events = [_approved(),
              {'event_id': 'T1-s', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'kind': 'original', 'qty': 100},
              {'event_id': 'T1-r1', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'receipt_no': 'RC1', 'qty': 60},
              {'event_id': 'T1-r2', 'type': 'receipt_recorded', 'occurred_at': '2026-11-13T10:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'receipt_no': 'RC2', 'qty': 30}]
    r = _order(events, 'T1')
    assert r['received_qty'] == 90
    assert r['store_inventory_qty'] == 90          # 未收清的10件不算门店库存
    assert r['in_transit_qty'] == 10
    assert r['redistributable_qty'] == 0           # 在途不另许给别家
    assert r['status'] == '部分收货在途'
    assert r['can_continue_issue'] is True
    assert r['balanced']


def test_reship_links_back_to_original_order():
    events = demo_events()
    r = _order(events, 'T1002')
    # 原发80，短少10，补发10接回同一单调拨单；收齐后净短少归零、守恒相符
    assert r['approved_qty'] == 80
    assert r['received_qty'] == 80
    assert r['claimed_shortage_qty'] == 10         # 毛认定保留留痕
    assert r['shortage_qty'] == 0                  # 补发收齐 -> 净短少0
    assert r['reship_sent_qty'] == 10
    kinds = {(s['shipment_no'], s['kind']) for s in r['shipments']}
    assert ('S1002-B', 'reship') in kinds
    assert r['status'] == '可结清'
    assert r['balanced']


def test_late_receipt_does_not_double_charge_shortage():
    r = _order(demo_events(), 'T1003')
    # 认定短少10后晚到6：6件冲净短少且转可再分配，不重复扣减；仍有4件净短少
    assert r['claimed_shortage_qty'] == 10
    assert r['late_excess_qty'] == 6
    assert r['redistributable_qty'] == 6
    assert r['shortage_qty'] == 4
    assert r['received_qty'] == 40                 # 晚到6不计门店库存
    assert r['balanced']


def test_reship_in_transit_is_not_shortage():
    r = _order(demo_events(), 'T1006')
    assert r['claimed_shortage_qty'] == 15
    assert r['in_transit_qty'] == 15               # 补发15在途
    assert r['shortage_qty'] == 0                  # 缺口已转为在途货物
    assert r['status'] == '补发在途'
    assert r['balanced']


def test_pending_shipment_is_pending_issue_not_inventory():
    r = _order(demo_events(), 'T1005')
    assert r['pending_issue_qty'] == 40
    assert r['received_qty'] == 0
    assert r['status'] == '待发货'
    assert r['can_continue_issue'] is False
    assert r['balanced']


def test_duplicate_receipt_is_rejected():
    events = [_approved(),
              {'event_id': 'T1-s', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'kind': 'original', 'qty': 100},
              {'event_id': 'T1-r1', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'receipt_no': 'RC1', 'qty': 60}]
    dup = {'event_id': 'T1-r1b', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T11:00:00Z',
           'order_no': 'T1', 'shipment_no': 'SH1', 'receipt_no': 'RC1', 'qty': 10}
    orders = replay(events)
    with pytest.raises(DomainError) as e:
        validate_event(dup, {'T1-a', 'T1-s', 'T1-r1'}, orders)
    assert e.value.code == 'RECON_DUP'


def test_reship_cannot_exceed_outstanding_claim():
    events = [_approved(),
              {'event_id': 'T1-s', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'kind': 'original', 'qty': 100},
              {'event_id': 'T1-r1', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'receipt_no': 'RC1', 'qty': 90},
              {'event_id': 'T1-c', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
               'order_no': 'T1', 'shipment_no': 'SH1', 'qty': 10, 'reason': '短少'}]
    bad = {'event_id': 'T1-rs', 'type': 'shipment_sent', 'occurred_at': '2026-11-14T09:00:00Z',
           'order_no': 'T1', 'shipment_no': 'SH2', 'kind': 'reship', 'qty': 11}
    orders = replay(events)
    with pytest.raises(DomainError) as e:
        validate_event(bad, set(), orders)
    assert e.value.code == 'RECON_QTY'


def test_original_shipment_must_equal_approved_qty():
    events = [_approved()]
    bad = {'event_id': 'T1-s', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
           'order_no': 'T1', 'shipment_no': 'SH1', 'kind': 'original', 'qty': 99}
    with pytest.raises(DomainError) as e:
        validate_event(bad, set(), replay(events))
    assert e.value.code == 'RECON_QTY'


def test_unknown_order_rejected():
    bad = {'event_id': 'X', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
           'order_no': 'NOPE', 'shipment_no': 'SH1', 'kind': 'original', 'qty': 10}
    with pytest.raises(DomainError) as e:
        validate_event(bad, set(), {})
    assert e.value.code == 'RECON_STATE'


def test_closed_order_rejects_further_events():
    events = demo_events()
    orders = replay(events)
    more = {'event_id': 'late', 'type': 'receipt_recorded', 'occurred_at': '2026-12-01T00:00:00Z',
            'order_no': 'T1001', 'shipment_no': 'S1001-A', 'receipt_no': 'RCX', 'qty': 1}
    with pytest.raises(DomainError) as e:
        validate_event(more, set(), orders)
    assert e.value.code == 'RECON_STATE'


def test_bad_quantity_rejected():
    with pytest.raises(DomainError):
        validate_event({**_approved('T2'), 'qty': -3}, set(), {})
    with pytest.raises(DomainError):
        validate_event({**_approved('T3'), 'qty': 1.5}, set(), {})


def test_replay_is_deterministic():
    a = build_conclusion(demo_events())
    b = build_conclusion(demo_events())
    assert a == b
