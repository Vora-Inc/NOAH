"""Durable conversion, NOAH deposit, and foreign deposit orchestration.

Adapters may sign only these three typed steps. Every signed transaction hash
must reach durable storage before broadcast; an uncertain step is never resent.
"""
from contextlib import nullcontext
from dataclasses import asdict
import json
import re

from ...chain.sources import Refusal
from .mixed import NotSubmitted, allocation, delivered_amounts, refuse, test_gate
from .signer import Journal, digest


STEPS = ('convert', 'deposit-noah', 'deposit-foreign')
UNRESOLVED = ('prepared', 'signed', 'submitted', 'uncertain')


class MixedJournal(Journal):
    def __init__(self, directory):
        super().__init__(directory)
        self.db.execute('CREATE TABLE IF NOT EXISTS mixed_workflows (id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def workflows(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM mixed_workflows')]

    def save(self, row):
        self.db.execute('INSERT OR REPLACE INTO mixed_workflows VALUES (?, ?)',
                        (row['id'], json.dumps(row, sort_keys=True)))


def scope(config):
    return {'genesis': config.genesis_hash, 'owner': config.coldkey,
            'miner': config.miner_hotkey, 'netuid': config.netuid}


def binding(config, settings):
    # Persist a hash, not endpoint credentials or wallet paths.
    return digest({'config': asdict(config), 'mixed': asdict(settings)})


def public_result(row):
    steps = row['steps']
    complete = all(steps.get(name, {}).get('status') == 'finalized' for name in STEPS)
    return {'intent_id': row['id'], 'label': row['label'], 'plan': row['plan'],
            'delivery_complete': complete, 'settlement_verified': False,
            'credit_verified': False, 'production_authorized': False,
            'steps': steps, 'closed_failed': row.get('closed_failed', False),
            'failure_closure': row.get('failure_closure')}


def reconcile(row, adapter, journal):
    for step in row['steps'].values():
        if step['status'] not in UNRESOLVED:
            continue
        if not step.get('extrinsic_hash'):
            step['status'] = 'uncertain'
        else:
            result = adapter.reconcile(step)
            if result.get('status') not in ('finalized', 'failed', 'submitted', 'uncertain'):
                refuse('adapter returned an unsupported reconciliation result')
            if result['status'] != step['status']:
                step.setdefault('reconciliation_history', []).append({
                    'previous_status': step['status'], 'status': result['status'],
                    'extrinsic_hash': step['extrinsic_hash'],
                    'block_hash': result.get('block_hash'),
                    'extrinsic_id': result.get('extrinsic_id')})
            step.update(result)
        journal.save(row)


def run(config, settings, adapter, journal, *, label, preview=False, status=False,
        ask=input, say=print, close_failed=False):
    if sum(bool(v) for v in (preview, status, close_failed)) > 1:
        refuse('select only one of preview, status, or close-failed')
    test_gate(config, settings)
    ledger = getattr(adapter, 'ledger', None)
    # Production SDK adapters always supply the canonical ledger. Pure state
    # machine test adapters may omit it to exercise journal behavior in isolation.
    with ledger.execution_lock() if ledger is not None else nullcontext():
        result = _run_locked(config, settings, adapter, journal, label=label,
                             preview=preview, status=status, ask=ask, say=say, close_failed=close_failed)
        if ledger is not None and result.get('delivery_complete'):
            ledger.complete_mixed(result['intent_id'])
        if ledger is not None and result.get('closed_failed'):
            ledger.close_failed_mixed(result['intent_id'])
        return result


def _run_locked(config, settings, adapter, journal, *, label, preview, status, ask, say, close_failed):
    ledger = getattr(adapter, 'ledger', None)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}', label):
        refuse('mixed deposit label must contain 1–96 safe characters')
    adapter.identity()
    identity = scope(config)
    ident = digest([identity, label])
    config_hash = binding(config, settings)
    with journal.locked():
        rows = journal.workflows()
        row = next((r for r in rows if r['id'] == ident), None)
        if ledger is not None:
            ledger.check_mixed_execution(ident, journal.path, row is not None)
        if row and row['binding'] != config_hash:
            refuse('this label is already bound to different settings; inspect the original intent')
        if row and row.get('closed_failed'):
            return public_result(row)  # Original label is permanently non-sendable.
        if close_failed:
            if not row or set(row['steps']) != {'convert'}:
                refuse('failure closure requires only the original failed conversion and no deposit steps')
            step = row['steps']['convert']
            if step['status'] != 'failed':
                refuse('uncertain or successful conversion cannot be closed as failed')
            proof = adapter.certify_failed_conversion(step)
            say(json.dumps({'label': label, 'verified_failed_conversion': proof,
                            'note': 'This retains the failed transaction and fee; it never retries.'}, indent=2))
            phrase = f'CLOSE FAILED {label}'
            if ask(f'To close this failed test intent, type exactly: {phrase}\n> ').strip() != phrase:
                return dict(public_result(row), closure_declined=True)
            row['closed_failed'] = True
            row['failure_closure'] = proof
            journal.save(row)  # Canonical reservation remains held if a crash occurs here.
            return public_result(row)
        if row:
            reconcile(row, adapter, journal)
            if status or public_result(row)['delivery_complete']:
                return public_result(row)
            if any(s['status'] in UNRESOLVED or s['status'] == 'failed' for s in row['steps'].values()):
                return public_result(row)
        elif status:
            return {'label': label, 'status': 'not_started', 'credit_verified': False}
        else:
            # Other wallets sharing this owner's journal cannot bypass an
            # unresolved/partially completed workflow by selecting a new label.
            for old in rows:
                if (old['scope']['genesis'] == identity['genesis'] and
                        old['scope']['owner'] == identity['owner'] and
                        old['steps'] and not old.get('closed_failed')
                        and not public_result(old)['delivery_complete']):
                    refuse('owner has an incomplete mixed workflow; inspect its original label')
            total = adapter.contribution_budget()
            plan = allocation(total, settings)
            quote = adapter.quote(plan)
            row = {'id': ident, 'label': label, 'binding': config_hash,
                   'scope': identity, 'plan': plan, 'quote': quote, 'steps': {}}
        if preview:
            return dict(public_result(row), preview=True, signed=False, broadcast=False,
                        quote=row['quote'])
        from .amounts import format_noah
        from .session import _shown_disclosure
        from ..disclosures import REQUIRED_DISCLOSURES
        phrase = f"SEND MIXED {format_noah(row['plan']['contribution_budget_rao'])}"
        say(json.dumps({'plan': row['plan'], 'quote': row['quote'],
                        'completed_steps': [k for k,v in row['steps'].items() if v['status']=='finalized'],
                        'note': 'Foreign output is provisional until conversion finalizes. '
                                'Three separate transactions; partial completion is possible. '
                                'Transfers are permanent; this does not establish scoring credit.'}, indent=2))
        for topic in REQUIRED_DISCLOSURES:say(_shown_disclosure(topic))
        if ask(f'To approve this test workflow, type exactly: {phrase}\n> ').strip() != phrase:
            return {'status': 'declined', 'signed': False, 'broadcast': False}
        if ledger is not None:
            # Reserve first: a crash between stores leaves an orphan that refuses
            # execution, never a potentially sent workflow invisible to direct.
            ledger.reserve_mixed(ident, journal.path)
        journal.save(row)
        for name in STEPS:
            if name in row['steps']:
                previous = row['steps'][name]
                if previous['status'] == 'finalized':
                    continue
                if previous['status'] != 'not_submitted' or previous.get('submission_started'):
                    return public_result(row)
                # A later leg may be approved again only after a proven pre-RPC
                # refusal. Retain every attempt, including any signed hash.
                row.setdefault('attempt_history', {}).setdefault(name, []).append(previous)
            if name == 'convert':
                amount = row['plan']['swap_budget_rao']
            else:
                actual = delivered_amounts(row['plan']['contribution_budget_rao'],
                                           row['steps']['convert']['conversion_result'])
                amount = actual['noah_rao'] if name == 'deposit-noah' else actual['foreign_rao']
            # All read-only validation precedes preparing an irreversible step.
            step = dict(action=name, amount_rao=amount, status='prepared', phase='preparing',
                        submission_started=False)
            row['steps'][name] = step
            journal.save(row)
            try:
                try:
                    prepared = adapter.prepare(name, amount, row['quote'])
                except NotSubmitted:
                    raise
                except (Exception, Refusal):
                    # prepare is exclusively read-only; neither key access nor
                    # submission has been invoked at this boundary.
                    raise NotSubmitted('preparation_check_failed') from None
                step.update(prepared)
                step['phase'] = 'signing'
                journal.save(row)
                signed, tx_hash = adapter.sign(step)
                if not isinstance(tx_hash, str) or not re.fullmatch(r'0x[0-9a-f]{64}', tx_hash):
                    refuse('signed transaction did not supply a verifiable hash')
                step.update(extrinsic_hash=tx_hash, status='signed', phase='pre_submission_checks')
                journal.save(row)
                def before_submit():
                    step.update(submission_started=True, phase='submitting')
                    journal.save(row)
                adapter.broadcast(signed, before_submit=before_submit)
                step.update(status='submitted', phase='reconciling')
                journal.save(row)
                reconcile(row, adapter, journal)
            except NotSubmitted as error:
                if step['submission_started']:
                    # A transport/adapter must never turn an attempted send into
                    # a retryable refusal, even if it raises this exception type.
                    step['status'] = 'uncertain'
                else:
                    step['status'] = 'not_submitted'
                    if name == 'convert' and set(row['steps']) == {'convert'}:
                        row['closed_failed'] = True
                        row['failure_closure'] = dict(kind='not_submitted',
                            phase=step['phase'], code=error.public_code, retry_submitted=False)
                step['error_code'] = error.public_code
                journal.save(row)
                return public_result(row)
            except (Exception, Refusal):
                # Never persist wallet/provider exception text; never resubmit.
                step['status'] = 'uncertain'
                step['error_code'] = 'operation_failed_outcome_unconfirmed'
                journal.save(row)
                return public_result(row)
            if step['status'] != 'finalized':
                return public_result(row)
        return public_result(row)
