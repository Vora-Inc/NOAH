"""One immutable prospective gate-to-bonus boundary; no accounting migration.

The original activation and every historical request/publication stay intact.
Announcement configuration is provenance, not a new schedule-rotation restriction.
"""
import hashlib
import os
import re
import stat
from pathlib import Path

from .calculation_record import encoded
from .report_publication import require

PATH = 'report-state/reward-transition.json'
ACTIVATION = 'report-state/activation.json'
SCHEMA = 'noah-report-reward-transition/1'
MAX_BYTES = 65536
SHA = re.compile(r'[0-9a-f]{64}')


def read_document(path):
    path = Path(path)
    require(not path.is_symlink() and not path.parent.is_symlink(), 'report boundary link refused')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_BYTES,
                'report boundary file refused')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(fd)
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(after)
                and not path.is_symlink() and not path.parent.is_symlink()
                and signature(after) == signature(path.stat(follow_symlinks=False)), 'report boundary changed while reading')
    finally:
        os.close(fd)
    from ..reports.adjudication_contract import strict_json
    value = strict_json(raw)
    require(encoded(value) == raw, 'report boundary bytes are not canonical')
    return value


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_transition(value, activation):
    from .report_runtime import validate_activation, validate_policy
    validate_activation(activation)
    fields = {'schema', 'activation_sha256', 'genesis_hash', 'mining_netuid',
              'day_anchor_ts', 'effective_day', 'from_reward', 'to_reward',
              'announcement', 'observed'}
    require(type(value) is dict and set(value) == fields and value['schema'] == SCHEMA,
            'invalid report reward transition')
    require(len(encoded(value)) <= MAX_BYTES, 'report reward transition exceeds byte bound')
    require(activation.get('reward', 'gate') == value['from_reward'] == 'gate'
            and value['to_reward'] == 'bonus-20'
            and value['activation_sha256'] == digest(activation),
            'report reward transition activation differs')
    require(all(value[k] == activation[k] for k in ('genesis_hash', 'mining_netuid')),
            'report reward transition network differs')
    require(type(value['day_anchor_ts']) is int and value['day_anchor_ts'] >= 0
            and value['day_anchor_ts'] % 86400 == 0
            and type(value['effective_day']) is int
            and activation['effective_day'] < value['effective_day'] < 2**32,
            'invalid report reward transition calendar')
    announcement = value['announcement']
    require(type(announcement) is dict and set(announcement) == {'configuration', 'sha256'},
            'invalid report reward announcement')
    configuration = validate_policy(announcement['configuration'])
    require(digest(configuration) == announcement['sha256']
            and all(configuration[k] == activation[k]
                    for k in ('genesis_hash', 'mining_netuid', 'effective_day')),
            'report reward announcement binding differs')
    if configuration['schema'] == 'noah.report-schedule/1':
        require(configuration['day_anchor_ts'] == value['day_anchor_ts'],
                'report reward announcement calendar differs')
    observed = value['observed']
    require(type(observed) is dict and set(observed) == {'block_number', 'block_hash', 'timestamp_ms'}
            and type(observed['block_number']) is int and observed['block_number'] > 0
            and type(observed['block_hash']) is str and SHA.fullmatch(observed['block_hash'])
            and type(observed['timestamp_ms']) is int and observed['timestamp_ms'] >= 0,
            'invalid report reward transition observation')
    require(observed['timestamp_ms'] < (value['day_anchor_ts'] + value['effective_day'] * 86400) * 1000,
            'report reward transition is not prospective')
    return value


def load_transition(directory, activation):
    path = Path(directory) / PATH
    require(not path.is_symlink() and not path.parent.is_symlink(), 'report boundary link refused')
    return validate_transition(read_document(path), activation) if path.exists() else None


def reward_for_day(activation, transition, day):
    require(type(day) is int and day >= activation['effective_day'], 'report day precedes activation')
    if transition is not None:
        validate_transition(transition, activation)
        if day >= transition['effective_day']:
            return 'bonus-20'
    return activation.get('reward', 'gate')


def configured_transition(directory, activation, reward):
    transition = load_transition(directory, activation)
    expected = 'bonus-20' if transition is not None else activation.get('reward', 'gate')
    require(reward == expected, 'report reward differs from retained activation or transition')
    return transition


def reference_for(transition, day):
    if transition is not None and day >= transition['effective_day']:
        return {'path': PATH, 'sha256': digest(transition)}
    return None


def verify_proof_reference(directory, proof):
    reference = proof.get('reward_transition')
    if reference is None:
        return
    require(type(reference) is dict and set(reference) == {'path', 'sha256'}
            and reference['path'] == PATH, 'invalid report reward transition reference')
    activation = read_document(Path(directory) / ACTIVATION)
    transition = load_transition(directory, activation)
    day = proof['request']['eval_day']
    require(transition is not None and reference == reference_for(transition, day)
            and proof.get('reward', 'gate') == reward_for_day(activation, transition, day)
            and all(proof['request'][k] == activation[k] for k in ('genesis_hash', 'mining_netuid'))
            and proof['request']['day_anchor_ts'] == transition['day_anchor_ts'],
            'report reward proof differs from retained transition')
