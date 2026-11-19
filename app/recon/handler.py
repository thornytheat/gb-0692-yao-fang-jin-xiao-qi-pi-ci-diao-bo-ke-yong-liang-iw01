"""收发对账 HTTP 接口。"""
from flask import Blueprint, jsonify, render_template, request

from app.errors import DomainError
from app.recon import service

bp = Blueprint('recon', __name__)

DEMO_BATCH = 'RDEMO0000000001'


@bp.route('/api/recon/batches', methods=['GET'])
def batches():
    return jsonify(service.batches())


@bp.route('/api/recon/batches/<batch_id>', methods=['GET'])
def get_batch(batch_id):
    return jsonify(service.current(batch_id))


@bp.route('/api/recon/batches/<batch_id>/ledger', methods=['GET'])
def get_ledger(batch_id):
    return jsonify(service.ledger(batch_id))


@bp.route('/api/recon/events', methods=['POST'])
def post_events():
    body = request.get_json(silent=True) or {}
    events = body.get('events')
    batch_id = body.get('batch_id')
    if batch_id is not None and (not isinstance(batch_id, str) or not batch_id.strip()):
        raise DomainError('RECON_ID', 'batch_id 无效')
    return jsonify(service.submit(events, batch_id=batch_id.strip() if batch_id else None))


@bp.route('/api/recon/demo', methods=['POST'])
def demo():
    from app.recon.seed import demo_events
    return jsonify(service.reset_demo(DEMO_BATCH, demo_events()))


@bp.route('/recon')
def board():
    batch_id = request.args.get('batch_id') or DEMO_BATCH
    try:
        view = service.current(batch_id)
    except DomainError as e:
        if e.status != 404:
            raise
        view = None
    return render_template('recon/board.html', view=view, demo_batch=DEMO_BATCH,
                           active_batch=batch_id)
