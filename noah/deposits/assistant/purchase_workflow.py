"""Durable fixed purchase then transfers; never retry an uncertain transaction."""
import json
import re
import copy

from ...chain.sources import Refusal
from .mixed import NotSubmitted, refuse
from .mixed_workflow import UNRESOLVED, scope
from .purchase import plan, test_gate
from .signer import Journal, digest
from .proxy_signing import same
from .purchase_signer import canonical_address, bindings, binding
from types import SimpleNamespace


class PurchaseJournal(Journal):
    def __init__(self,directory):
        super().__init__(directory)
        self.db.execute('CREATE TABLE IF NOT EXISTS purchase_workflows (id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def workflows(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM purchase_workflows')]

    def save(self,row):
        self.db.execute('INSERT OR REPLACE INTO purchase_workflows VALUES (?,?)',
                        (row['id'],json.dumps(row,sort_keys=True)))


def actions(row):
    return ('purchase', *(['deposit-noah'] if row['plan']['noah_deposit_rao'] else []), 'deposit-foreign')


def public_result(row):
    complete=all(row['steps'].get(k,{}).get('status')=='finalized' for k in actions(row))
    return dict(intent_id=row['id'],label=row['label'],plan=row['plan'],steps=row['steps'],
                closed_failed=row.get('closed_failed',False),failure_closure=row.get('failure_closure'),
                delivery_complete=complete,settlement_verified=False,credit_verified=False,production_authorized=False)


def reconcile(row,adapter,journal):
    for step in row['steps'].values():
        if step['status'] not in UNRESOLVED:continue
        reserved=bool(step.get('journal_id') and step.get('reservation_token'))
        if step.get('extrinsic_hash') or step.get('proxy_request') or reserved:
            outcome=adapter.reconcile(step)
            if outcome.get('status')=='refused':outcome=dict(outcome,status='uncertain')
            if outcome.get('status')=='not_submitted':
                if (not step.get('reservation_token') or outcome.get('journal_id')!=step.get('journal_id')
                        or outcome.get('reservation_token')!=step['reservation_token']):
                    refuse('signer cannot establish continuity for never-submitted reservation')
                step['submission_started']=False
            if outcome.get('status') not in ('not_submitted','finalized','failed','uncertain','submitted'):
                refuse('unsupported purchase reconciliation result')
            step.update(outcome)
        elif (step.get('submission_protocol')=='proxy-reservation-v1'
                and step.get('phase') in ('preparing','reserving')
                and step.get('submission_started') is False):
            # This protocol cannot invoke SUBMIT before saving its reservation.
            # Legacy hashless prepared rows have no such evidence and stay held.
            step['status']='not_submitted'
        else:step['status']='uncertain'
        journal.save(row)


def refreshable_quote(row):
    """Prove every attempt stopped before the proxy PREPARE RPC boundary.

    A missing token alone proves nothing: PREPARE may have committed a signer
    reservation before its response was lost. Inspect retained attempts too,
    because a later local preparation failure can overwrite the current phase.
    Legacy, malformed and later-phase records keep their original quote.
    """
    if set(row['steps']) != {'purchase'}:
        return False
    history = row.get('attempt_history', {})
    if not isinstance(history, dict) or set(history) - {'purchase'}:
        return False
    attempts = history.get('purchase', [])
    if not isinstance(attempts, list):
        return False
    pre_prepare_fields = {'action', 'amount_rao', 'status', 'phase', 'submission_started',
                          'submission_protocol', 'error_code'}
    for step in [*attempts, row['steps']['purchase']]:
        if (not isinstance(step, dict) or step.get('action') != 'purchase'
                or set(step) - pre_prepare_fields
                or step.get('amount_rao') != row['plan']['max_tao_rao']
                or step.get('status') != 'not_submitted' or step.get('phase') != 'preparing'
                or step.get('submission_protocol') != 'proxy-reservation-v1'
                or step.get('submission_started') is not False
                or {'extrinsic_hash', 'reservation_token', 'journal_id', 'proxy_request'} & step.keys()):
            return False
    return True


def cancellable_quote(row):
    """Require every saved pre-PREPARE attempt to have an expired quote."""
    if not refreshable_quote(row):
        return False
    attempts = row.get('attempt_history', {}).get('purchase', [])
    return all(step.get('error_code') == 'quote_expired'
               for step in [*attempts, row['steps']['purchase']])


def run(config,settings,adapter,journal,*,label,preview=False,status=False,cancel_unsubmitted=False,
        recover_pre_submit=False,ask=input,say=print):
    if sum((preview,status,cancel_unsubmitted,recover_pre_submit))>1:
        refuse('select one purchase operation')
    test_gate(config,settings)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}',label):refuse('invalid purchase label')
    # Mandatory canonical ledger: shared owner mounts serialize direct, mixed,
    # purchase and proxy requests and prevent a new journal bypassing uncertainty.
    ledger=adapter.ledger
    identity=scope(config)
    canonical=dict(identity,owner=canonical_address(config.coldkey),miner=canonical_address(config.miner_hotkey))
    ident=digest(['free_tao_purchase',canonical,settings.plan_id or label])
    bound=SimpleNamespace(config=config,purchase=settings)
    config_binding=binding(bound)
    with ledger.execution_lock(), journal.locked():
        rows=journal.workflows()
        matches=[r for r in rows if r['scope']['genesis']==identity['genesis']
                 and same(r['scope']['owner'],identity['owner']) and same(r['scope']['miner'],identity['miner'])
                 and r['scope']['netuid']==identity['netuid']
                 and (r['plan'].get('plan_id')==settings.plan_id if settings.plan_id else r['label']==label)]
        if len(matches)>1:refuse('multiple records bind this fixed plan; investigate before continuing')
        row=matches[0] if matches else None
        if row:ident=row['id']
        ledger.check_mixed_execution(ident,journal.path,row is not None)
        if row:
            if row['binding'] not in bindings(bound):refuse('purchase label is bound to different fixed settings')
            if row.get('closed_failed'):
                from .purchase_cancellation import validate_closed
                validate_closed(row,config_binding)
                if not preview:ledger.close_failed_mixed(ident)
                return public_result(row)
            if cancel_unsubmitted:
                if not getattr(adapter,'uses_proxy',False) or not cancellable_quote(row):
                    refuse('only proven pre-PREPARE quote-expired proxy attempts can be cancelled')
                from .purchase_cancellation import close_client
                close_client(row,adapter,journal,ledger,config_binding)
                return public_result(row)
            if recover_pre_submit:
                if (not getattr(adapter,'uses_proxy',False) or set(row['steps']) != {'purchase'}
                        or row['steps']['purchase'].get('status') != 'uncertain'
                        or row['steps']['purchase'].get('submission_protocol') != 'proxy-reservation-v1'
                        or not row['steps']['purchase'].get('proxy_request')
                        or not row['steps']['purchase'].get('journal_id')
                        or not row['steps']['purchase'].get('reservation_token')):
                    refuse('only the original unresolved pre-submit proxy purchase can be recovered')
                previous=row['steps']['purchase']
                if (previous.get('extrinsic_hash') is not None
                        and not (previous.get('failure_stage') in ('after_hash','signed_before_submit')
                                 and previous.get('failure_code')=='quote_expired'
                                 and previous.get('block_hash') is None)):
                    refuse('signed purchase outcome is not proven before submission')
                if any(s.get('block_hash') is not None or s.get('events') is not None
                       for attempts in row.get('attempt_history',{}).values() for s in attempts):
                    refuse('a retained purchase attempt may have reached submission')
                adapter.identity()
                quote=adapter.quote(recovery=True)
                step=dict(row['steps']['purchase'])
                step.update(params=quote['params'],quote_params=quote['params'],
                            quote_block=quote['quote_block'],amount_rao=settings.max_tao_rao)
                outcome=adapter.recover_proxy(step)
                if (outcome.get('status') != 'not_submitted'
                        or outcome.get('journal_id') != step['journal_id']
                        or outcome.get('reservation_token') != step['reservation_token']):
                    refuse('signer did not prove pre-submit recovery and reservation continuity')
                row.setdefault('quote_history',[]).append(row['quote'])
                row['quote']=quote
                row.setdefault('attempt_history',{}).setdefault('purchase',[]).append(copy.deepcopy(previous))
                row['steps']['purchase']=dict(action='purchase',amount_rao=settings.max_tao_rao,
                    status='not_submitted',phase='recovered_pre_submit',submission_started=False,
                    submission_protocol='proxy-reservation-v1',proxy_request=True,
                    journal_id=step['journal_id'],reservation_token=step['reservation_token'],
                    params=quote['params'],quote_params=quote['params'],quote_block=quote['quote_block'])
                journal.save(row)
                return dict(public_result(row),recovered_pre_submit=True,signed=False,broadcast=False)
            if row.get('cancellation'):
                if status or preview:return dict(public_result(row),cancellation_pending=True)
                refuse('cancellation is pending; retry cancel-unsubmitted with original settings')
            adapter.identity()
            # Preview must not reconcile/persist an existing attempt. Use status
            # or a normal retry to resolve its durable submission evidence.
            if not preview:reconcile(row,adapter,journal)
            result=public_result(row)
            if result['delivery_complete']:
                if not preview:ledger.complete_mixed(ident)
                return result
            if status:return result
            if any(s['status'] in (*UNRESOLVED,'failed') for s in row['steps'].values()):return result
        elif cancel_unsubmitted:refuse('no existing pre-PREPARE purchase attempt to cancel')
        elif status:return dict(label=label,status='not_started',credit_verified=False)
        else:
            for old in rows:
                if old.get('closed_failed'):
                    from .purchase_cancellation import validate_closed
                    validate_closed(old,old['binding'])
                if same(old['scope']['owner'],identity['owner']) and old['scope']['genesis']==identity['genesis'] and not public_result(old)['delivery_complete'] and not old.get('closed_failed'):
                    refuse('owner has an incomplete purchase; inspect its original label')
            adapter.identity()
            row=dict(id=ident,label=label,binding=config_binding,scope=identity,
                     plan=plan(config,settings),quote=adapter.quote(),steps={})
        # A new quote cannot alter an existing signer reservation. Only the
        # explicit pre-PREPARE protocol proof above permits repricing; preserve
        # the plan, binding, label and canonical ledger reservation throughout.
        refresh = bool(getattr(adapter,'uses_proxy',False) and refreshable_quote(row))
        quote = adapter.quote() if refresh else row['quote']
        if quote['plan'] != row['plan']:
            refuse('fresh quote differs from the immutable fixed purchase plan')
        if preview:return dict(public_result(row),preview=True,signed=False,broadcast=False,
                               quote=quote,quote_refreshed=refresh)
        from .session import _shown_disclosure
        from ..disclosures import REQUIRED_DISCLOSURES
        phrase=f"SEND PURCHASE {settings.max_tao_rao} TAO RAO"
        say(json.dumps(dict(plan=row['plan'],quote=quote,
            note='Separate irreversible transactions. A failed purchase stops deposits. Partial delivery remains possible; inspect the original label.'),indent=2))
        for topic in REQUIRED_DISCLOSURES:say(_shown_disclosure(topic))
        if ask(f'To approve this fixed test workflow, type exactly: {phrase}\n> ').strip()!=phrase:
            return dict(status='declined',signed=False,broadcast=False)
        if refresh:
            # No stored quote/history changes on preview, failed preflight or
            # declined approval. Retain every previously approved quote.
            row.setdefault('quote_history',[]).append(row['quote'])
            row['quote']=quote
        ledger.reserve_mixed(ident,journal.path)
        journal.save(row)
        for action in actions(row):
            previous=row['steps'].get(action)
            if previous:
                if previous['status']=='finalized':continue
                if previous['status']!='not_submitted' or previous.get('submission_started'):return public_result(row)
                row.setdefault('attempt_history',{}).setdefault(action,[]).append(previous)
            if action=='purchase':amount=settings.max_tao_rao
            elif action=='deposit-noah':amount=row['plan']['noah_deposit_rao']
            else:
                amount=row['steps']['purchase']['purchase_result']['foreign_received_rao']
            step=dict(action=action,amount_rao=amount,status='prepared',phase='preparing',submission_started=False)
            if getattr(adapter,'uses_proxy',False):
                step['submission_protocol']='proxy-reservation-v1'
            if previous and previous.get('reservation_token'):
                # Retain the request needed for signer STATUS at every save,
                # including a crash before the new read-only prepare completes.
                for key in ('journal_id','reservation_token','params','quote_block'):
                    step[key]=previous[key]
                step['proxy_request']=previous.get('proxy_request',False)
            row['steps'][action]=step; journal.save(row)
            try:
                try:prepared=adapter.prepare(action,amount,row['quote'])
                except NotSubmitted:raise
                except (Exception,Refusal):raise NotSubmitted('preparation_check_failed') from None
                step.update(prepared)
                step['phase']='reserving' if getattr(adapter,'uses_proxy',False) else 'signing'
                journal.save(row)
                if getattr(adapter,'uses_proxy',False):
                    if not step.get('reservation_token'):
                        # PREPARE cannot sign or broadcast. A lost connection at
                        # this stage is safely retryable; the next prepare finds
                        # the same durable reservation if its response was lost.
                        try:reservation=adapter.prepare_proxy(step)
                        except (Exception,Refusal):
                            step.update(status='not_submitted',error_code='signer_prepare_unavailable')
                            journal.save(row);return public_result(row)
                        if reservation.get('status') in ('finalized','failed','uncertain'):
                            step.update(reservation);journal.save(row)
                            if step['status']!='finalized':return public_result(row)
                            continue
                        if (reservation.get('status')!='not_submitted'
                                or not reservation.get('journal_id') or not reservation.get('reservation_token')):
                            refuse('signer did not provide a durable never-submitted reservation')
                        step.update(journal_id=reservation['journal_id'],reservation_token=reservation['reservation_token'])
                        journal.save(row)
                    step.update(proxy_request=True,submission_started=True,status='submitted',phase='proxy_request')
                    journal.save(row)
                    outcome=adapter.submit_proxy(step)
                    if outcome.get('status')=='refused':
                        proven=(outcome.get('journal_id')==step['journal_id']
                                and outcome.get('reservation_token')==step['reservation_token'])
                        outcome=dict(outcome,status='failed' if proven else 'uncertain')
                    if outcome.get('status') not in ('finalized','failed','uncertain'):
                        refuse('unsupported proxy purchase result')
                    step.update(outcome);journal.save(row)
                else:
                    signed,txhash=adapter.sign(step)
                    if not isinstance(txhash,str) or not re.fullmatch(r'0x[0-9a-f]{64}',txhash):refuse('signed transaction has no exact hash')
                    step.update(extrinsic_hash=txhash,status='signed',phase='pre_submission_checks');journal.save(row)
                    def before_submit():
                        step.update(submission_started=True,phase='submitting');journal.save(row)
                    adapter.broadcast(signed,before_submit=before_submit)
                    step.update(status='submitted',phase='reconciling');journal.save(row)
                    reconcile(row,adapter,journal)
            except NotSubmitted as error:
                step['status']='uncertain' if step['submission_started'] else 'not_submitted'
                step['error_code']=error.public_code;journal.save(row)
                return public_result(row)
            except (Exception,Refusal):
                step.update(status='uncertain',error_code='operation_failed_outcome_unconfirmed');journal.save(row)
                return public_result(row)
            if step['status']!='finalized':return public_result(row)
        result=public_result(row)
        if result['delivery_complete']:ledger.complete_mixed(ident)
        return result
