"""收发对账持久化、版本化与 HTTP 接口测试。"""
import pytest

from app.errors import DomainError
from app.recon.seed import demo_events
from app.recon.handler import DEMO_BATCH
from app.recon import service, store


@pytest.fixture
def recon_db(tmp_path, monkeypatch):
    monkeypatch.setenv('RECON_DB', str(tmp_path / 'recon.sqlite3'))
    yield


def _basic_events(no='T1'):
    return [
        {'event_id': no + '-a', 'type': 'order_approved', 'occurred_at': '2026-11-10T08:00:00Z',
         'order_no': no, 'sku': '0001', 'store': '门店01', 'lot': 'L1', 'qty': 100},
        {'event_id': no + '-s', 'type': 'shipment_sent', 'occurred_at': '2026-11-11T09:00:00Z',
         'order_no': no, 'shipment_no': no + '-SH1', 'kind': 'original', 'qty': 100},
        {'event_id': no + '-r', 'type': 'receipt_recorded', 'occurred_at': '2026-11-12T10:00:00Z',
         'order_no': no, 'shipment_no': no + '-SH1', 'receipt_no': no + '-RC1', 'qty': 70},
    ]


# ---------- 服务/持久化 ----------

def test_submit_creates_versioned_conclusion(recon_db):
    out = service.submit(_basic_events(), batch_id='RB1')
    assert out['reused'] is False
    assert out['version'] == 1
    assert out['conclusion']['totals']['all_balanced']
    again = service.submit(_basic_events(), batch_id='RB1')
    # 同批重跑：复用，不新增加扣减（事件数不变），仅登记一条 reused 版本记录
    assert again['reused'] is True
    assert again['conclusion'] == out['conclusion']
    ledger = service.ledger('RB1')['events']
    assert len(ledger) == 3
    versions = store.list_versions('RB1')
    assert versions[-1]['reused'] == 1


def test_rerun_same_batch_adds_no_deductions(recon_db):
    first = service.submit(demo_events(), batch_id='RDEMO')
    service.submit(demo_events(), batch_id='RDEMO')
    service.submit(demo_events(), batch_id='RDEMO')
    ledger = service.ledger('RDEMO')['events']
    # 事件只增表中每事件仅一条，重跑不重复写入、不产生新扣减
    assert len(ledger) == len(demo_events())
    current = service.current('RDEMO')
    assert current['conclusion'] == first['conclusion']


def test_failed_batch_rolls_back_and_keeps_previous_version(recon_db):
    service.submit(_basic_events(), batch_id='RB2')
    before = service.current('RB2')
    # 追加一条非法事件：引用不存在的发货批次
    bad = _basic_events() + [
        {'event_id': 'T1-bad', 'type': 'receipt_recorded', 'occurred_at': '2026-11-13T10:00:00Z',
         'order_no': 'T1', 'shipment_no': 'GHOST', 'receipt_no': 'RCG', 'qty': 5}]
    with pytest.raises(DomainError) as e:
        service.submit(bad, batch_id='RB2')
    assert e.value.code == 'RECON_STATE'
    after = service.current('RB2')
    # 上一版结论原样保留，事件未被污染
    assert after['version'] == before['version']
    assert after['conclusion'] == before['conclusion']
    assert len(service.ledger('RB2')['events']) == 3


def test_incremental_valid_events_produce_new_version(recon_db):
    service.submit(_basic_events(), batch_id='RB3')
    more = _basic_events() + [
        {'event_id': 'T1-c', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
         'order_no': 'T1', 'shipment_no': 'T1-SH1', 'qty': 30, 'reason': '短少30'}]
    out = service.submit(more, batch_id='RB3')
    assert out['reused'] is False
    assert out['version'] == 2
    r = next(x for x in out['conclusion']['rows'] if x['order_no'] == 'T1')
    assert r['shortage_qty'] == 30
    # 历史版本仍可追溯：初始版 + 新增量版
    assert [v['version'] for v in store.list_versions('RB3')] == [1, 2]


def test_duplicate_event_id_in_batch_rejected(recon_db):
    events = _basic_events()
    events.append(dict(events[-1]))  # 同 event_id
    with pytest.raises(DomainError) as e:
        service.submit(events, batch_id='RB4')
    assert e.value.code == 'RECON_ID'


def test_incremental_reship_flow_status_transitions(recon_db):
    # 第一批：获批 + 整批发货 + 部分收货
    service.submit(_basic_events('T9'), batch_id='RB9')
    r1 = next(x for x in service.current('RB9')['conclusion']['rows'] if x['order_no'] == 'T9')
    assert r1['status'] == '部分收货在途' and r1['in_transit_qty'] == 30
    # 第二批：认定短少30 -> 短少待补发
    claim = _basic_events('T9') + [
        {'event_id': 'T9-c', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
         'order_no': 'T9', 'shipment_no': 'T9-SH1', 'qty': 30, 'reason': '短少30'}]
    service.submit(claim, batch_id='RB9')
    r2 = next(x for x in service.current('RB9')['conclusion']['rows'] if x['order_no'] == 'T9')
    assert r2['status'] == '短少待补发' and r2['shortage_qty'] == 30
    # 第三批：补发30接原单 -> 补发在途
    reship = claim + [
        {'event_id': 'T9-rs', 'type': 'shipment_sent', 'occurred_at': '2026-11-14T09:00:00Z',
         'order_no': 'T9', 'shipment_no': 'T9-SH2', 'kind': 'reship', 'qty': 30}]
    service.submit(reship, batch_id='RB9')
    r3 = next(x for x in service.current('RB9')['conclusion']['rows'] if x['order_no'] == 'T9')
    assert r3['status'] == '补发在途' and r3['shortage_qty'] == 0 and r3['in_transit_qty'] == 30
    # 第四批：补发收齐 -> 可结清，全程守恒
    done = reship + [
        {'event_id': 'T9-rr', 'type': 'receipt_recorded', 'occurred_at': '2026-11-15T10:00:00Z',
         'order_no': 'T9', 'shipment_no': 'T9-SH2', 'receipt_no': 'T9-RC2', 'qty': 30}]
    out = service.submit(done, batch_id='RB9')
    r4 = next(x for x in out['conclusion']['rows'] if x['order_no'] == 'T9')
    assert r4['status'] == '可结清' and r4['received_qty'] == 100 and r4['balanced']
    assert [v['version'] for v in store.list_versions('RB9')] == [1, 2, 3, 4]
    # 事件只增：共6条原始事件
    assert len(service.ledger('RB9')['events']) == 6


def test_unknown_batch_404(recon_db):
    with pytest.raises(DomainError) as e:
        service.current('NOPE')
    assert e.value.status == 404


def test_ledger_retains_raw_orders_and_receipts(recon_db):
    service.submit(demo_events(), batch_id='RDEMO2')
    ledger = service.ledger('RDEMO2')['events']
    types = {e['type'] for e in ledger}
    assert {'order_approved', 'shipment_sent', 'receipt_recorded',
            'shortage_claimed', 'order_closed'} <= types
    # 原始报文留存
    assert all('raw' in e and e['raw'] for e in ledger)
    receipts = [e for e in ledger if e['type'] == 'receipt_recorded']
    assert any('RC1002-2' in e['raw'] for e in receipts)


# ---------- HTTP ----------

@pytest.fixture
def client(recon_db):
    from app.main import app
    app.config['TESTING'] = True
    return app.test_client()


def test_demo_endpoint_and_board_render(client):
    resp = client.post('/api/recon/demo', json={})
    assert resp.status_code == 200
    bid = resp.get_json()['batch_id']
    page = client.get('/recon?batch_id=' + bid).data.decode()
    assert '调拨收发对账看板' in page
    assert 'T1002' in page and '守恒相符' in page
    # 看板展示卡步与能否继续发放
    assert '可继续发放' in page and '不可继续发放' in page


def test_events_api_idempotent_rerun(client):
    body = {'batch_id': 'RBAPI', 'events': _basic_events()}
    first = client.post('/api/recon/events', json=body).get_json()
    second = client.post('/api/recon/events', json=body).get_json()
    assert first['version'] == 1 and second['reused'] is True
    assert second['conclusion'] == first['conclusion']


def test_events_api_failure_keeps_old_version(client):
    client.post('/api/recon/events', json={'batch_id': 'RBF', 'events': _basic_events()})
    bad_events = _basic_events() + [
        {'event_id': 'bad', 'type': 'shortage_claimed', 'occurred_at': '2026-11-13T09:00:00Z',
         'order_no': 'T1', 'shipment_no': 'GHOST', 'qty': 1}]
    resp = client.post('/api/recon/events', json={'batch_id': 'RBF', 'events': bad_events})
    assert resp.status_code == 422
    kept = client.get('/api/recon/batches/RBF').get_json()
    assert kept['version'] == 1


def test_ledger_api(client):
    client.post('/api/recon/demo', json={})
    resp = client.get('/api/recon/batches/' + DEMO_BATCH + '/ledger')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data['events']) == len(demo_events())
    assert data['versions']


def test_batches_listing(client):
    client.post('/api/recon/demo', json={})
    rows = client.get('/api/recon/batches').get_json()
    assert any(r['batch_id'] == DEMO_BATCH for r in rows)


def test_non_object_json_rejected(client):
    resp = client.post('/api/recon/events', json=[1, 2])
    assert resp.status_code == 400
    assert resp.get_json()['code'] == 'REQUEST'


def test_m6_page_links_to_recon_board(client):
    page = client.get('/work/6').data.decode()
    assert '/recon' in page
