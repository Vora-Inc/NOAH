"""Permanent closure of a proven pre-PREPARE proxy plan; no chain or key access."""
import json
import re
import uuid

from .proxy_signing import reject, uint, same
from .signer import digest


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def tombstone(signer):
    """Called under the shared signer lock; missing initialized storage fails closed."""
    from .purchase_signer import ident
    db = signer.journal.db
    signer.journal.incarnation()
    marker = db.execute('SELECT token FROM journal_identity WHERE id=2').fetchone()
    exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_cancellations'").fetchone()
    if marker:
        if marker[0] != 'purchase-cancellations-v1' or not exists:
            reject('purchase cancellation journal is missing or incompatible')
    else:
        if exists:
            reject('purchase cancellation journal identity is absent')
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('CREATE TABLE purchase_cancellations (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute("INSERT INTO journal_identity VALUES (2,'purchase-cancellations-v1')")
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    saved = db.execute('SELECT body FROM purchase_cancellations WHERE id=?', (ident(signer,'purchase'),)).fetchone()
    if not saved:
        return None
    try:
        proof = json.loads(saved[0])
        valid = (set(proof) == {'status','plan_id','amount_rao','config_binding','policy_fingerprint',
                               'journal_id','cancellation_token','closure_id'}
                 and proof['status'] == 'cancelled'
                 and proof['plan_id'] == signer.purchase.plan_id
                 and type(proof['amount_rao']) is int and proof['amount_rao'] > 0
                 and all(_hex(proof[k],32) for k in ('journal_id','cancellation_token','closure_id'))
                 and all(_hex(proof[k],64) for k in ('config_binding','policy_fingerprint')))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        reject('purchase cancellation record is malformed')
    return proof


def cancel(signer, request, rows, saved, incarnation):
    from .purchase_signer import binding, ident
    # Exact immutable public settings, plus trusted policy and journal continuity.
    if request['action'] != 'purchase' or uint(request['amount_rao'],'amount',1) != signer.purchase.max_tao_rao:
        reject('cancellation must name the exact fixed purchase amount')
    if request['config_binding'] != binding(signer):
        reject('cancellation settings differ from signer policy')
    if any(r['genesis']==signer.config.genesis_hash
           and same(r['owner'],signer.config.coldkey) and same(r['miner'],signer.config.miner_hotkey)
           and r['request'].get('intent_id')==signer.purchase.plan_id for r in rows):
        reject('signer already holds an intent for this plan; cancellation forbidden')
    fingerprint = digest(signer.policy)
    expected = dict(plan_id=signer.purchase.plan_id, amount_rao=signer.purchase.max_tao_rao,
                    config_binding=binding(signer), policy_fingerprint=fingerprint, journal_id=incarnation)
    if saved and any(saved.get(k) != v for k,v in expected.items()):
        reject('cancelled plan policy or journal identity changed')
    if request['operation'] == 'cancel-check':
        if saved:
            reject('plan is already cancelled; recover using its original closure request')
        return dict(expected,status='cancel_ready')
    if (request['journal_id'] != incarnation or request['policy_fingerprint'] != fingerprint
            or not _hex(request['cancellation_token'],32)):
        reject('cancellation journal or trusted policy changed')
    if saved:
        if saved['cancellation_token'] != request['cancellation_token']:
            reject('cancelled plan belongs to a different closure request')
        return saved
    proof = dict(expected,status='cancelled',cancellation_token=request['cancellation_token'],
                 closure_id=uuid.uuid4().hex)
    signer.journal.db.execute('INSERT INTO purchase_cancellations VALUES (?,?)',
        (ident(signer,'purchase'),json.dumps(proof,sort_keys=True)))
    return proof


def close_client(row, adapter, journal, ledger, config_binding):
    """Caller holds canonical owner + purchase journal locks. RPCs cannot sign."""
    expected = dict(plan_id=row['plan']['plan_id'],amount_rao=row['plan']['max_tao_rao'],
                    config_binding=config_binding)
    pending = row.get('cancellation')
    if pending is None:
        ready = adapter.cancel_proxy('cancel-check',dict(config_binding=config_binding))
        if (not isinstance(ready,dict) or ready.get('status') != 'cancel_ready'
                or any(ready.get(k) != v for k,v in expected.items())
                or not _hex(ready.get('journal_id'),32) or not _hex(ready.get('policy_fingerprint'),64)):
            reject('signer did not establish cancellation policy and journal identity')
        pending = dict(ready,cancellation_token=uuid.uuid4().hex)
        row['cancellation'] = pending
        journal.save(row)  # Must precede any request capable of closing the plan.
    if (pending.get('status') != 'cancel_ready' or any(pending.get(k) != v for k,v in expected.items())
            or not _hex(pending.get('journal_id'),32) or not _hex(pending.get('policy_fingerprint'),64)
            or not _hex(pending.get('cancellation_token'),32)):
        reject('saved cancellation request is malformed or bound to different settings')
    proof = adapter.cancel_proxy('cancel',pending)
    if (not isinstance(proof,dict) or proof.get('status') != 'cancelled'
            or any(proof.get(k) != v for k,v in pending.items() if k != 'status')
            or not _hex(proof.get('closure_id'),32)):
        reject('signer did not establish durable cancellation')
    row.update(closed_failed=True,failure_closure=proof)
    journal.save(row)  # A crash here is repaired by the next command, without RPC.
    ledger.close_failed_mixed(row['id'])


def validate_closed(row, config_binding):
    pending, proof = row.get('cancellation'), row.get('failure_closure')
    if (not isinstance(pending,dict) or not isinstance(proof,dict)
            or pending.get('status')!='cancel_ready' or proof.get('status')!='cancelled'
            or pending.get('config_binding')!=config_binding
            or pending.get('plan_id')!=row['plan']['plan_id']
            or pending.get('amount_rao')!=row['plan']['max_tao_rao']
            or any(proof.get(k)!=v for k,v in pending.items() if k!='status')
            or not all(_hex(proof.get(k),32) for k in ('journal_id','cancellation_token','closure_id'))
            or not _hex(proof.get('policy_fingerprint'),64)):
        reject('purchase closure proof is missing or malformed')
