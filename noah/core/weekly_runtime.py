"""Weekly authority and immutable no-bonus evidence. No chain or wallet access."""
from pathlib import Path

from .calculation_record import artifact, durable_write, encoded, retain, verify
from .report_publication import require, semantic_digest

TEMPLATE = 'noah.weekly-assignment-template/2'
AUTHORITY = 'report-state/weekly-template.json'
NO_BONUS = 'noah-weekly-no-bonus/1'
MARKER = 'noah-weekly-no-bonus-marker/1'


def bind_template(directory, configuration, *, persist=False):
    from ..reports.assignments import validate_template
    from .report_reward_transition import read_document
    validate_template(configuration)
    require(configuration['schema'] == TEMPLATE, 'new weekly use requires trusted template/2')
    path = Path(directory) / AUTHORITY
    if path.exists() or path.is_symlink():
        require(read_document(path) == configuration, 'retained weekly authority differs')
    else:
        weeks = path.parent / 'weekly-assignments'
        require(not weeks.exists() and not weeks.is_symlink()
                and not any(path.parent.glob('day-*.no-bonus.json')),
                'weekly evidence lacks retained authority')
        if persist:
            durable_write(path, encoded(configuration))
    return configuration


def authority(directory):
    from .report_reward_transition import read_document
    configuration = read_document(Path(directory) / AUTHORITY)
    return bind_template(directory, configuration)


def verify_week_for(configuration, document, day, anchor):
    from ..reports.assignments import verify_week, template_from_document, week_start_date
    result = verify_week(document, trusted_public_key=configuration['assignment_signer_public_key'])
    require(template_from_document(result) == template_from_document(configuration),
            'weekly template differs from signed file')
    require(configuration['day_anchor_ts'] == anchor
            and result['week_start'] == week_start_date(anchor + day * 86400).isoformat(),
            'signed assignment week differs from scoring day')
    return result


def signing_pair(path, trusted):
    """Use only the dedicated assignment seed, never the transaction signer."""
    from ..reports.assignments import load_assignment_key
    return load_assignment_key(path, trusted_public_key=trusted)


def marker_path(directory, day):
    return Path(directory) / 'report-state' / f'day-{day}.no-bonus.json'


def _proof_context(directory, source, configuration, reason, week, transition):
    context = artifact(directory, source, 'context')
    require(context.get('scoring_mode') == 'points-204040-v5'
            and not any(k in context for k in ('report_gate', 'report_no_bonus', 'starter_overlay', 'burn_allocation')),
            'weekly no-bonus source must be original V2 calculation')
    day, anchor = source['day'], configuration['day_anchor_ts']
    from ..chain.ss58 import public_key_of
    require(source['genesis_hash'].removeprefix('0x') == configuration['genesis_hash']
            and source['netuid'] == configuration['mining_netuid']
            and day >= configuration['effective_day']
            and context['day_start_utc_unix'] == anchor + day * 86400
            and public_key_of(context['ark_address']).hex() == configuration['ark_coldkey']
            and public_key_of(context['designated_validator_hotkey']).hex() == configuration['ark_validator_hotkey'],
            'weekly no-bonus source identity differs')
    require(reason in ('assignment_unavailable', 'no_assigned_targets'), 'invalid no-bonus reason')
    if reason == 'assignment_unavailable':
        require(week is None, 'unavailable assignments contain a week')
    else:
        verify_week_for(configuration, week, day, anchor)
        require(not any(row['targets'] for row in week['assignments']), 'no-target claim differs')
    from .report_reward_transition import read_document, load_transition, reward_for_day, reference_for
    from .report_runtime import validate_activation
    activation = validate_activation(read_document(Path(directory) / 'report-state/activation.json'))
    retained = load_transition(directory, activation)
    require(all(activation[k] == configuration[k] for k in ('genesis_hash', 'mining_netuid', 'effective_day'))
            and reward_for_day(activation, retained, day) == 'bonus-20'
            and transition == reference_for(retained, day), 'no-bonus activation or transition differs')
    return context


def retain_no_bonus(directory, source_reference, configuration, *, reason, week=None, transition=None):
    bind_template(directory, configuration, persist=True)
    source = verify(directory, source_reference)
    context = _proof_context(directory, source, configuration, reason, week, transition)
    request = Path(directory) / 'report-state' / f'day-{source["day"]}.request.json'
    require(not request.exists() and not request.is_symlink(),
            'frozen report request cannot become no-bonus fallback')
    proof = dict(schema=NO_BONUS, source=source_reference, configuration=configuration,
                 configuration_sha256=semantic_digest(configuration), reason=reason,
                 week=week, reward_transition=transition)
    ref = retain(directory, fixture=artifact(directory, source, 'fixture'),
                 agreed_text=encoded(artifact(directory, source, 'agreed_score')).decode(),
                 vector={int(k): v for k, v in source['publishable_weights_u16'].items()},
                 context={**context, 'report_no_bonus': proof}, canonical_day=False)
    verify_no_bonus(directory, ref)
    marker = dict(schema=MARKER, day=source['day'], source=source_reference, reference=ref)
    durable_write(marker_path(directory, source['day']), encoded(marker))
    return ref


def verify_no_bonus(directory, reference):
    manifest = verify(directory, reference)
    context = artifact(directory, manifest, 'context')
    proof = context.get('report_no_bonus')
    if proof is None:
        return None
    require(type(proof) is dict and set(proof) == {'schema', 'source', 'configuration',
            'configuration_sha256', 'reason', 'week', 'reward_transition'} and proof['schema'] == NO_BONUS,
            'invalid no-bonus evidence')
    configuration = authority(directory)
    require(proof['configuration'] == configuration
            and proof['configuration_sha256'] == semantic_digest(configuration), 'no-bonus authority differs')
    source = verify(directory, proof['source'])
    raw = _proof_context(directory, source, configuration, proof['reason'], proof['week'], proof['reward_transition'])
    require(context == {**raw, 'report_no_bonus': proof}
            and {k: v for k, v in manifest.items() if k != 'artifacts'} == {k: v for k, v in source.items() if k != 'artifacts'}
            and all(manifest['artifacts'][k] == source['artifacts'][k] for k in ('fixture', 'agreed_score')),
            'no-bonus calculation changed raw financial result')
    return proof


def validate_no_bonus_marker(directory, marker):
    require(type(marker) is dict and set(marker) == {'schema', 'day', 'source', 'reference'}
            and marker['schema'] == MARKER and type(marker['day']) is int and marker['day'] >= 0,
            'invalid no-bonus marker')
    proof = verify_no_bonus(directory, marker['reference'])
    request = Path(directory) / 'report-state' / f'day-{marker["day"]}.request.json'
    require(proof is not None and proof['source'] == marker['source']
            and verify(directory, marker['reference'])['day'] == marker['day']
            and not request.exists() and not request.is_symlink(),
            'no-bonus marker source or day differs')
    return proof


def expand_resolution(configuration, week, day, anchor, base):
    """Reconstruct target rounds from one paired boundary, never a saved verdict.

    Target schedules differ only in reported_netuid. Each derived resolution is
    passed through the original raw-receipt verifier; this does not duplicate
    the schedule's timing or round-ID rules.
    """
    from ..reports.assignments import schedule_for_target, template_from_document
    from ..reports.schedule import validate_schedule, verify_resolution
    week = verify_week_for(configuration, week, day, anchor)
    needed = sorted({netuid for row in week['assignments'] for netuid in row['targets']})
    require(needed, 'weekly file has no assigned subnet')
    template = template_from_document(configuration)
    first = schedule_for_target(template, needed[0])
    require(first['day_anchor_ts'] == anchor, 'weekly calendar differs')
    verify_resolution(first, day, base, require_pair=True)
    resolutions = []
    for netuid in needed:
        schedule = schedule_for_target(template, netuid)
        require({k: v for k, v in schedule.items() if k != 'reported_netuid'} ==
                {k: v for k, v in first.items() if k != 'reported_netuid'},
                'weekly targets do not share one schedule boundary')
        identity = validate_schedule(schedule)
        contract = {**base['round'], 'reported_netuid': netuid, 'release_prefix': identity[:32]}
        item = {'round': contract, 'policy': {**base['policy'], 'required_rounds': [contract]},
                'schedule_sha256': identity, 'boundary_proof': base['boundary_proof']}
        verify_resolution(schedule, day, item, require_pair=True)
        resolutions.append(item)
    return resolutions
