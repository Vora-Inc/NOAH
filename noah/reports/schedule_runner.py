"""One miner scheduler tick, sharing the existing immutable report sender."""
from pathlib import Path

from .common import ReportError, immutable_json, read_json, digest
from .schedule import validate_schedule, day_for_time, resolve_day, verify_resolution
from .miner import prepare_report, submit_tick


def scheduled_tick(config, schedule, *, authorized=False, allow_mainnet=False,
                   chain=None, resolver=None, prepare_fn=None, submit_fn=None):
    from .backing.chain_session import SubstrateChain
    from .backing.keys import ss58_decode
    from .backing.submit import _refuse_wrong_chain
    identity = validate_schedule(schedule)
    if (not config.genesis_hash or config.genesis_hash.removeprefix('0x').lower() != schedule['genesis_hash']
            or config.netuid != schedule['mining_netuid']
            or ss58_decode(config.ark_coldkey).hex() != schedule['ark_coldkey']
            or ss58_decode(config.validator_hotkey).hex() != schedule['ark_validator_hotkey']):
        raise ReportError('miner configuration and report schedule differ')
    owned = chain is None
    chain = chain or SubstrateChain(config.endpoint)
    try:
        _refuse_wrong_chain(chain.genesis(), {'genesis': schedule['genesis_hash']}, allow_mainnet=allow_mainnet)
        now = chain.finalized_time_ms()
        day = day_for_time(schedule, now)
        state = Path(config.state_dir)
        immutable_json(state / 'report-schedules' / identity / 'schedule.json', schedule)
        # Resolve uncertain old sends before considering a new daily round. This
        # path cannot authorize a missing phase or reopen signing material.
        prior_root = state / 'reports' / schedule['genesis_hash'] / str(config.netuid) / ss58_decode(config.miner_hotkey).hex()
        sender = submit_fn or submit_tick
        for journal in sorted(prior_root.glob('*/*.journal')):
            saved = read_json(journal)
            if saved.get('state') in ('finalized', 'failed'):
                continue
            contract = read_json(journal.parent / 'round.json')
            outcome = sender(config, contract, authorized=False, allow_mainnet=allow_mainnet, chain=chain)
            return {'action': 'reconciled', 'schedule_sha256': identity, 'sent': False, 'report': outcome}
        effective_day = max(day, schedule['effective_day'])
        slot = (schedule['day_anchor_ts'] + effective_day * 86400 + schedule['snapshot_offset_seconds']) * 1000
        closed = slot + (schedule['prepare_lead_seconds'] + schedule['commit_seconds'] + schedule['reveal_seconds']) * 1000
        if now < slot:
            return {'action': 'waiting', 'sent': False, 'schedule_sha256': identity, 'next_at_ms': slot}
        if now >= closed:
            return {'action': 'waiting', 'sent': False, 'schedule_sha256': identity, 'next_at_ms': slot + 86400000,
                    'notice': 'today closed; saved rounds and journals are preserved'}
        resolution_path = state / 'report-schedules' / identity / f'day-{day}.json'
        if resolution_path.exists():
            resolution = verify_resolution(schedule, day, read_json(resolution_path), require_pair=False)
        else:
            resolution = (resolver or resolve_day)(schedule, [config.endpoint], day, require_pair=False)
        contract = resolution['round']
        if resolution['schedule_sha256'] != identity:
            raise ReportError('resolved schedule identity differs')
        immutable_json(resolution_path, resolution)
        (prepare_fn or prepare_report)(config, contract)
        outcome = sender(config, contract, authorized=authorized, allow_mainnet=allow_mainnet, chain=chain)
        return {**outcome, 'schedule_sha256': identity, 'round_sha256': digest(contract)}
    finally:
        if owned:
            chain.substrate.close()


def assignment_identity(config):
    """Operator-owned trust; never take these values from the downloaded file."""
    import re
    from .backing.keys import ss58_decode
    key = getattr(config, 'assignment_signer_public_key', None)
    if type(key) is not str or not re.fullmatch('[0-9a-f]{64}', key):
        raise ReportError('weekly assignments require a configured assignment signer public key')
    genesis = (config.genesis_hash or '').removeprefix('0x').lower()
    if not re.fullmatch('[0-9a-f]{64}', genesis):
        raise ReportError('weekly assignments require a configured genesis hash')
    return key, dict(genesis_hash=genesis, mining_netuid=config.netuid,
                     ark_coldkey=ss58_decode(config.ark_coldkey).hex(),
                     ark_validator_hotkey=ss58_decode(config.validator_hotkey).hex())


def validate_assignment_document(config, document, now_seconds):
    from .assignments import verify_week, week_start_date
    key, identity = assignment_identity(config)
    document = verify_week(document, trusted_public_key=key)
    if any(document.get(field) != value for field, value in identity.items()):
        raise ReportError('miner configuration and weekly assignment identity differ')
    if document['week_start'] != week_start_date(now_seconds).isoformat():
        raise ReportError('weekly assignment file is for a different week')
    return document


def reconcile_assignments(config, *, chain, allow_mainnet=False, submit_fn=None):
    """Read back uncertain old sends even if this week's file is unavailable."""
    from .backing.submit import _refuse_wrong_chain
    _, identity = assignment_identity(config)
    _refuse_wrong_chain(chain.genesis(), {'genesis': identity['genesis_hash']}, allow_mainnet=allow_mainnet)
    return _reconcile(config, identity, submit_fn or submit_tick, allow_mainnet, chain)


def assignments_tick(config, document, *, authorized=False, allow_mainnet=False, chain=None,
                     registration_block=None, resolver=None, prepare_fn=None, submit_fn=None):
    """Advance at most one transaction, preserving independent target journals."""
    from .assignments import assignment_for, schedule_for_target, template_from_document
    from .backing.chain_session import SubstrateChain
    from .backing.keys import ss58_decode
    from .miner import status

    key, _ = assignment_identity(config)
    owned = chain is None
    chain = chain or SubstrateChain(config.endpoint)
    sender = submit_fn or submit_tick
    if prepare_fn is None:
        inventory = {}
        def preparer(config, contract):
            return prepare_report(config, contract, inventory=inventory)
    else:
        preparer = prepare_fn
    try:
        prior = reconcile_assignments(config, chain=chain, allow_mainnet=allow_mainnet, submit_fn=sender)
        if prior is not None:
            return {'sent': False, 'action': 'reconciled', 'report': prior}
        now = chain.finalized_time_ms()
        document = validate_assignment_document(config, document, now // 1000)
        hotkey = ss58_decode(config.miner_hotkey).hex()
        coldkey = ss58_decode(config.coldkey).hex()
        supplements = None
        if getattr(config, 'late_joiner_assignments', False) is True:
            from .assignments import load_supplement_prefix
            from datetime import datetime, timezone
            day_name = datetime.fromtimestamp(now // 1000, timezone.utc).date().isoformat()
            supplements = load_supplement_prefix(
                Path(config.state_dir) / 'weekly-assignments', document, day_name, trusted_public_key=key)
        if registration_block is None:
            registration_block = _registration_block(
                chain, document, config.miner_hotkey, use_head=supplements is not None)
        if supplements is not None:
            from .assignments import assignment_for_day
            from datetime import datetime, timezone
            day_name = datetime.fromtimestamp(now // 1000, timezone.utc).date().isoformat()
            row = assignment_for_day(
                document, supplements, day=day_name, hotkey=hotkey, coldkey=coldkey,
                registration_block=registration_block, trusted_public_key=key)
        else:
            row = assignment_for(document, hotkey=hotkey, coldkey=coldkey,
                                 registration_block=registration_block, trusted_public_key=key)
        targets = [] if row is None else list(row['targets'])
        base = {'sent': False, 'targets': targets, 'week_start': document['week_start']}
        if not targets:
            if supplements is not None:
                template = template_from_document(document)
                schedule = schedule_for_target(template, template['mining_netuid'])
                day = max(day_for_time(schedule, now), schedule['effective_day'])
                slot = (schedule['day_anchor_ts'] + day * 86400 + schedule['snapshot_offset_seconds']) * 1000
                commit_close = slot + (schedule['prepare_lead_seconds'] + schedule['commit_seconds']) * 1000
                if now < commit_close:
                    return {**base, 'action': 'waiting', 'next_at_ms': commit_close}
            return {**base, 'action': 'not_assigned'}
        template = template_from_document(document)
        schedule = schedule_for_target(template, targets[0])
        day = max(day_for_time(schedule, now), schedule['effective_day'])
        slot = (schedule['day_anchor_ts'] + day * 86400 + schedule['snapshot_offset_seconds']) * 1000
        if now < slot:
            return {**base, 'action': 'waiting', 'next_at_ms': slot}
        closed = slot + (schedule['prepare_lead_seconds'] + schedule['commit_seconds'] + schedule['reveal_seconds']) * 1000
        contracts = []
        for netuid in targets:
            schedule = schedule_for_target(template, netuid)
            identity = validate_schedule(schedule)
            path = Path(config.state_dir) / 'weekly-assignments' / identity / f'day-{day}.json'
            if path.exists():
                resolution = verify_resolution(schedule, day, read_json(path), require_pair=False)
            elif now >= closed:
                return {**base, 'action': 'expired'}
            else:
                resolution = (resolver or resolve_day)(schedule, [config.endpoint], day, require_pair=False)
                if resolution['schedule_sha256'] != identity:
                    raise ReportError('resolved assignment schedule identity differs')
                immutable_json(path, resolution)
            contracts.append(resolution['round'])
        states = [(contract, status(config, contract)) for contract in contracts]
        results = [{'reported_netuid': contract['reported_netuid'], 'commit': state['commit'],
                    'reveal': state['reveal']} for contract, state in states]
        base['reports'] = results
        if all(state['reveal'] == 'finalized' for _, state in states):
            return {**base, 'action': 'complete'}
        terminal = all(state['commit'] == 'failed' or state['reveal'] in ('finalized', 'failed')
                       for _, state in states)
        if terminal:
            return {**base, 'action': 'partial'}
        # Read-only prep starts at the snapshot, including the lead before commit opens.
        # One subnet's failure is recorded and the other prepared reports can still be signed.
        prep_errors = _prepare_assigned(config, states, now, preparer)
        states = [(contract, status(config, contract)) for contract in contracts]
        base['reports'] = [{'reported_netuid': contract['reported_netuid'], 'commit': state['commit'],
                            'reveal': state['reveal']} for contract, state in states]
        if prep_errors:
            base['prep_errors'] = prep_errors
        skip = {item['reported_netuid'] for item in prep_errors}
        for phase in ('commit', 'reveal'):
            outcome = _advance(config, states, phase, now, authorized, allow_mainnet, chain, preparer, sender,
                               skip_netuids=skip)
            if outcome is not None:
                return {**base, **outcome}
        if prep_errors and now < closed:
            return {**base, 'action': 'prep_failed', 'sent': False}
        return {**base, 'action': 'expired' if now >= closed else 'waiting'}
    finally:
        if owned:
            chain.substrate.close()


def _registration_block(chain, document, hotkey, *, use_head=False):
    substrate = chain.substrate
    block = '0x' + document['roster_block_hash']
    def registered_at(pin):
        uid = substrate.query('SubtensorModule', 'Uids', [document['mining_netuid'], hotkey], block_hash=pin).value
        if uid is None:
            return None
        registered = substrate.query('SubtensorModule', 'BlockAtRegistration',
                                     [document['mining_netuid'], uid], block_hash=pin).value
        if type(registered) is not int:
            raise ReportError('weekly assignments: registration block is unavailable')
        return registered
    week_registration = registered_at(block)
    if week_registration is None:
        if not use_head:
            return None
        head = substrate.rpc_request('chain_getFinalizedHead', [])
        if not isinstance(head, dict) or 'error' in head or not isinstance(head.get('result'), str):
            raise ReportError('weekly assignments: finalized registration is unavailable')
        return registered_at(head['result'])
    head = substrate.rpc_request('chain_getFinalizedHead', [])
    if not isinstance(head, dict) or 'error' in head or not isinstance(head.get('result'), str):
        raise ReportError('weekly assignments: finalized registration is unavailable')
    return week_registration if registered_at(head['result']) == week_registration else None


def _reconcile(config, document, sender, allow_mainnet, chain):
    from .backing.keys import ss58_decode
    root = (Path(config.state_dir) / 'reports' / document['genesis_hash']
            / str(document['mining_netuid']) / ss58_decode(config.miner_hotkey).hex())
    for journal in sorted(root.glob('*/*.journal')):
        saved = read_json(journal)
        if saved.get('state') in ('finalized', 'failed'):
            continue
        contract = read_json(journal.parent / 'round.json')
        return sender(config, contract, authorized=False, allow_mainnet=allow_mainnet, chain=chain)
    return None


def _prepare_assigned(config, states, now, preparer):
    """Prepare every open target before any signature. Keep going after one failure."""
    from .backing.prepare import PrepareError
    errors = []
    for contract, state in states:
        if state.get('prepared') or state.get('commit') in ('finalized', 'failed'):
            continue
        if now >= contract['commit_close_ms']:
            continue
        try:
            preparer(config, contract)
        except (ReportError, PrepareError) as exc:
            errors.append({'reported_netuid': contract['reported_netuid'], 'reason': _prep_reason(exc)})
    return errors


def _prep_reason(exc):
    text = str(exc)
    if "://" in text or "@" in text or len(text) > 160:
        return type(exc).__name__
    return text


def _advance(config, states, phase, now, authorized, allow_mainnet, chain, preparer, sender, skip_netuids=()):
    opened = []
    for contract, state in states:
        if contract['reported_netuid'] in skip_netuids:
            continue
        if phase == 'reveal' and state['commit'] != 'finalized':
            continue
        if state[phase] in ('finalized', 'failed'):
            continue
        start, close = contract[f'{phase}_open_ms'], contract[f'{phase}_close_ms']
        if now < start:
            opened.append(start)
            continue
        if now >= close:
            continue
        if phase == 'commit' and not state['prepared']:
            preparer(config, contract)
        outcome = sender(config, contract, authorized=authorized, allow_mainnet=allow_mainnet, chain=chain)
        return {**outcome, 'phase': phase, 'reported_netuid': contract['reported_netuid']}
    if opened:
        return {'action': 'waiting', 'sent': False, 'phase': phase, 'next_at_ms': min(opened)}
    return None
