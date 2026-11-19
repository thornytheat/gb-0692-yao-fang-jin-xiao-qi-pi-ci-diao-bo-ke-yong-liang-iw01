"""调拨收发对账领域引擎。

设计原则（与业务约定一一对应）：
1. 同一调拨单守恒：批准量 = 已收 + 在途 + 短少 + 可再分配；未收清部分不进门店库存，也不许给别家。
2. 短少补发接回原单：补发是原单的新发货批次，不新开单、不重复冲减。
3. 晚到收货只回补原批次在途，已认定短少不被重复扣减；超出未补量的部分转入可再分配。
4. 纯函数 + 事件溯源：状态完全由有序事件重算，同批重跑结果一致、不新增加扣减。
"""
from datetime import datetime
from app.errors import DomainError

EVENT_TYPES = {
    'order_approved',      # 调拨单获批（整单成立，占用但不释放）
    'shipment_sent',       # 仓库整批/补发发货
    'receipt_recorded',    # 门店收货回执
    'shortage_claimed',    # 短少认定（补发责任落回原单）
    'order_closed',        # 单结清
}


def _int(value, field):
    if isinstance(value, bool):
        raise DomainError('RECON_QTY', f'{field}必须是非负整数')
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise DomainError('RECON_QTY', f'{field}必须是非负整数')
    if n < 0 or str(n) != str(value).strip():
        raise DomainError('RECON_QTY', f'{field}必须是非负整数')
    return n


def _require(cond, code, message):
    if not cond:
        raise DomainError(code, message)


def _time(value):
    if not value:
        raise DomainError('RECON_TIME', 'occurred_at 不能为空')
    try:
        datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise DomainError('RECON_TIME', 'occurred_at 时间格式无效')
    return str(value)


def validate_event(raw, seen_ids, orders):
    """校验单条事件；返回规范化事件。任何异常都不修改既有状态。"""
    _require(isinstance(raw, dict), 'RECON_EVENT', '事件必须是对象')
    etype = raw.get('type')
    _require(etype in EVENT_TYPES, 'RECON_TYPE', f'事件类型无效: {etype}')
    eid = str(raw.get('event_id') or '').strip()
    _require(eid, 'RECON_ID', 'event_id 不能为空')
    _require(eid not in seen_ids, 'RECON_DUP', f'事件重复提交: {eid}')
    occurred_at = _time(raw.get('occurred_at'))
    order_no = str(raw.get('order_no') or '').strip()
    _require(order_no, 'RECON_ORDER', 'order_no 不能为空')

    evt = {'event_id': eid, 'type': etype, 'occurred_at': occurred_at,
           'order_no': order_no, 'payload': {}}

    if etype == 'order_approved':
        sku = str(raw.get('sku') or '').strip()
        store = str(raw.get('store') or '').strip()
        lot = str(raw.get('lot') or '').strip()
        qty = _int(raw.get('qty'), 'qty')
        _require(sku and store, 'RECON_FIELD', 'sku 与 store 不能为空')
        _require(qty > 0, 'RECON_QTY', '批准数量必须大于0')
        _require(order_no not in orders, 'RECON_STATE', f'调拨单已存在: {order_no}')
        evt['payload'] = {'sku': sku, 'store': store, 'lot': lot, 'qty': qty}
        return evt

    order = orders.get(order_no)
    _require(order is not None, 'RECON_STATE', f'调拨单不存在或未获批: {order_no}')
    _require(not order['closed'], 'RECON_STATE', f'调拨单已结清，不可再处理: {order_no}')

    if etype == 'order_closed':
        evt['payload'] = {}
        return evt

    if etype == 'shipment_sent':
        ship_no = str(raw.get('shipment_no') or '').strip()
        qty = _int(raw.get('qty'), 'qty')
        _require(ship_no, 'RECON_FIELD', 'shipment_no 不能为空')
        _require(qty > 0, 'RECON_QTY', '发货数量必须大于0')
        _require(ship_no not in order['shipments'], 'RECON_DUP', f'发货批次重复: {ship_no}')
        kind = raw.get('kind', 'original')
        _require(kind in ('original', 'reship'), 'RECON_FIELD', '发货类别必须是 original 或 reship')
        if kind == 'original':
            _require(not order['has_original'], 'RECON_STATE', '原单已整批发货，不能重复原发')
            _require(qty == order['approved_qty'], 'RECON_QTY',
                     f'整批发货数量须等于批准量 {order["approved_qty"]}')
        else:
            outstanding_claim = order['claimed_short'] - order['reship_sent']
            _require(outstanding_claim > 0, 'RECON_STATE', '没有待补发的短少，不能发起补发')
            _require(qty <= outstanding_claim, 'RECON_QTY',
                     f'补发量 {qty} 超过待补短少 {outstanding_claim}')
        evt['payload'] = {'shipment_no': ship_no, 'qty': qty, 'kind': kind}
        return evt

    ship_no = str(raw.get('shipment_no') or '').strip()
    _require(ship_no, 'RECON_FIELD', 'shipment_no 不能为空')
    ship = order['shipments'].get(ship_no)
    _require(ship is not None, 'RECON_STATE', f'发货批次不存在: {ship_no}')

    if etype == 'receipt_recorded':
        qty = _int(raw.get('qty'), 'qty')
        _require(qty > 0, 'RECON_QTY', '收货数量必须大于0')
        # 允许晚到超过发货量：超出在途/短少额度的部分转可再分配，但累计须受控
        _require(ship['received'] + qty <= ship['qty'] + order['approved_qty'],
                 'RECON_QTY', '累计收货超出合理上限')
        receipt_no = str(raw.get('receipt_no') or '').strip()
        _require(receipt_no, 'RECON_FIELD', 'receipt_no 不能为空')
        dup = any(r['receipt_no'] == receipt_no for r in ship['receipts'])
        _require(not dup, 'RECON_DUP', f'收货回执重复: {receipt_no}')
        evt['payload'] = {'shipment_no': ship_no, 'qty': qty, 'receipt_no': receipt_no}
        return evt

    if etype == 'shortage_claimed':
        qty = _int(raw.get('qty'), 'qty')
        _require(qty > 0, 'RECON_QTY', '短少数量必须大于0')
        in_transit = ship['qty'] - ship['received'] - ship['claimed']
        _require(in_transit > 0, 'RECON_STATE', '该批次已无在途量，无法认定短少')
        _require(qty <= in_transit, 'RECON_QTY', f'短少量 {qty} 超过在途 {in_transit}')
        reason = str(raw.get('reason') or '').strip() or '门店短收'
        evt['payload'] = {'shipment_no': ship_no, 'qty': qty, 'reason': reason}
        return evt

    raise DomainError('RECON_TYPE', '事件类型无效')


def apply_event(order, evt):
    """把一条已校验事件落到聚合上（就地更新）。"""
    p = evt['payload']
    if evt['type'] == 'order_approved':
        order.update({
            'order_no': evt['order_no'], 'sku': p['sku'], 'store': p['store'],
            'lot': p['lot'], 'approved_qty': p['qty'],
            'shipments': {}, 'has_original': False,
            'claimed_short': 0, 'reship_sent': 0, 'late_offset_claim': 0,
            'late_recovered': 0, 'redistributable': 0, 'closed': False,
            'events': [],
        })
        order['events'].append(evt['event_id'])
        return

    order['events'].append(evt['event_id'])

    if evt['type'] == 'order_closed':
        order['closed'] = True
        order['closed_at'] = evt['occurred_at']
        return

    if evt['type'] == 'shipment_sent':
        order['shipments'][p['shipment_no']] = {
            'shipment_no': p['shipment_no'], 'kind': p['kind'],
            'qty': p['qty'], 'received': 0, 'claimed': 0,
            'late_excess': 0, 'late_offset': 0, 'receipts': [],
            'sent_event': evt['event_id'], 'sent_at': evt['occurred_at'],
        }
        if p['kind'] == 'original':
            order['has_original'] = True
        else:
            order['reship_sent'] += p['qty']
        order.setdefault('late_offset_claim', 0)
        return

    ship = order['shipments'][p['shipment_no']]

    if evt['type'] == 'receipt_recorded':
        qty = p['qty']
        remaining_in_transit = ship['qty'] - ship['received'] - ship['claimed']
        # 先回补在途；若在途已被短少认定占满，晚到部分不再冲减短少，转可再分配。
        normal = min(qty, max(remaining_in_transit, 0))
        excess = qty - normal
        ship['received'] += normal
        if excess:
            ship['late_excess'] += excess
            order['late_recovered'] += excess
            order['redistributable'] += excess
            # 晚到件补齐了此前认定的缺口：冲减净短少（不重复扣减），货转可再分配。
            # 仅原发批次的短少属于本单缺口；可冲减上限为该批尚未被晚到冲过的短少量。
            if ship['kind'] == 'original':
                offset = min(excess, ship['claimed'] - ship['late_offset'])
                ship['late_offset'] += offset
                order['late_offset_claim'] += offset
        ship['receipts'].append({
            'receipt_no': p['receipt_no'], 'qty': qty,
            'normal_qty': normal, 'excess_qty': excess,
            'event_id': evt['event_id'], 'at': evt['occurred_at'],
        })
        return

    if evt['type'] == 'shortage_claimed':
        ship['claimed'] += p['qty']
        order['claimed_short'] += p['qty']
        ship.setdefault('claims', []).append({
            'qty': p['qty'], 'reason': p['reason'],
            'event_id': evt['event_id'], 'at': evt['occurred_at'],
        })
        return


def _measures(order):
    """统一口径，页面、守恒、状态机都从这里取数，避免多处不一致。"""
    shipments = list(order['shipments'].values())
    original = [s for s in shipments if s['kind'] == 'original']
    reships = [s for s in shipments if s['kind'] == 'reship']
    sent = sum(s['qty'] for s in shipments)
    received = sum(s['received'] for s in shipments)               # 门店库存只认实收
    in_transit = sum(s['qty'] - s['received'] - s['claimed'] for s in shipments)
    original_in_transit = sum(
        s['qty'] - s['received'] - s['claimed'] for s in original)
    reship_in_transit = sum(
        s['qty'] - s['received'] - s['claimed'] for s in reships)
    claimed = sum(s['claimed'] for s in shipments)                 # 累计认定短少（毛额）
    reship_received = sum(s['received'] for s in reships)
    # 净短少：认定后已由补发收回、由晚到补齐、或补发已在途（缺口已转为货物）的部分都不再算短少
    net_shortage = (claimed - reship_received - reship_in_transit
                    - order.get('late_offset_claim', 0))
    redisp = order['redistributable']
    # 已获批但仓库尚未原发的数量（待发货态占用，既非门店库存也不许给别家）
    pending_issue = order['approved_qty'] - sum(s['qty'] for s in original)
    return {
        'shipments': shipments, 'sent': sent, 'received': received,
        'in_transit': in_transit, 'original_in_transit': original_in_transit,
        'reship_in_transit': reship_in_transit,
        'pending_issue': pending_issue,
        'claimed': claimed, 'reship_received': reship_received,
        'net_shortage': net_shortage, 'redistributable': redisp,
    }


def _order_status(order):
    """推导单据卡在哪一步、能否继续发放。"""
    if not order['has_original']:
        return '待发货', False, '调拨单已获批，仓库尚未整批发货'
    if order['closed']:
        return '已结清', False, '单据已关闭，不再受理收发'
    m = _measures(order)
    awaiting_reship = order['claimed_short'] - order['reship_sent']
    if awaiting_reship > 0:
        return '短少待补发', True, f'已认定短少 {awaiting_reship} 件，可接原单补发'
    if m['reship_in_transit'] > 0:
        return '补发在途', True, '补发批次在途，到货后可继续收货，未收量不进门店库存'
    if m['original_in_transit'] > 0:
        return '部分收货在途', True, '原批次仍有在途，等待门店回执或短少认定'
    if m['net_shortage'] > 0:
        return '待短少认定/结清', True, f'净短少 {m["net_shortage"]} 件待处理，可继续认定或结清'
    if m['received'] + m['redistributable'] >= order['approved_qty']:
        return '可结清', False, '批准量已收齐（晚到部分转可再分配），可结清'
    return '处理中', True, '等待下一收发动作'


def summarize_order(order):
    m = _measures(order)
    # 守恒：批准量 = 原发待发 + 已收 + 在途 + 净短少 + 可再分配
    balanced = (order['approved_qty']
                == m['pending_issue'] + m['received'] + m['in_transit']
                + m['net_shortage'] + m['redistributable'])
    status, can_issue, hint = _order_status(order)
    return {
        'order_no': order['order_no'], 'sku': order['sku'], 'store': order['store'],
        'lot': order['lot'], 'approved_qty': order['approved_qty'],
        'sent_qty': m['sent'], 'received_qty': m['received'],
        'pending_issue_qty': m['pending_issue'],
        'in_transit_qty': m['in_transit'],
        'claimed_shortage_qty': m['claimed'],
        'reship_received_qty': m['reship_received'],
        'shortage_qty': m['net_shortage'],
        'reship_sent_qty': order['reship_sent'],
        'late_excess_qty': order['late_recovered'],
        'redistributable_qty': m['redistributable'],
        'store_inventory_qty': m['received'],  # 未收清的数量不算门店库存
        'balanced': balanced, 'status': status,
        'can_continue_issue': can_issue, 'status_hint': hint, 'closed': order['closed'],
        'shipments': [
            {
                'shipment_no': s['shipment_no'], 'kind': s['kind'], 'qty': s['qty'],
                'received': s['received'], 'claimed': s['claimed'],
                'in_transit': s['qty'] - s['received'] - s['claimed'],
                'late_excess': s['late_excess'],
                'receipts': list(s['receipts']), 'claims': list(s.get('claims', [])),
            } for s in m['shipments']
        ],
        'event_ids': list(order['events']),
    }


def replay(events):
    """从空状态按序重算全部事件，返回订单聚合。纯函数，便于重跑校验。"""
    orders = {}
    seen = set()
    for evt in events:
        clean = validate_event(evt, seen, orders)
        if clean['type'] == 'order_approved':
            orders[clean['order_no']] = {}
            apply_event(orders[clean['order_no']], clean)
        else:
            apply_event(orders[clean['order_no']], clean)
        seen.add(clean['event_id'])
    return orders


def build_conclusion(events):
    """对外的可发布对账结论。"""
    orders = replay(events)
    rows = [summarize_order(o) for o in orders.values()]
    rows.sort(key=lambda r: r['order_no'])
    totals = {
        'orders': len(rows),
        'approved': sum(r['approved_qty'] for r in rows),
        'pending_issue': sum(r['pending_issue_qty'] for r in rows),
        'received': sum(r['received_qty'] for r in rows),
        'in_transit': sum(r['in_transit_qty'] for r in rows),
        'shortage': sum(r['shortage_qty'] for r in rows),
        'redistributable': sum(r['redistributable_qty'] for r in rows),
        'unbalanced': [r['order_no'] for r in rows if not r['balanced']],
    }
    totals['all_balanced'] = not totals['unbalanced']
    return {'rows': rows, 'totals': totals}
