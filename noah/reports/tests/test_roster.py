"""Snapshot completeness and identity on explicitly synthetic storage."""
import pytest
from noah.reports.backing.keys import blake2_128_concat, hex_encode, timestamp_now_key
from noah.reports.common import ReportError
from noah.reports.roster import _prefix, read_snapshot_roster
from noah.reports.tests.sample import fixture
from noah.reports.tests.test_engine_arithmetic import _chain, BLOCK


def case():
    contract = fixture()['round']
    contract['block_hash'] = BLOCK[2:]
    source = _chain()
    source.storage[hex_encode(timestamp_now_key())] = hex_encode(contract['block_timestamp_ms'].to_bytes(8, 'little'))
    keys = []
    for uid, key in [(1, '22'), (256, '33')]:
        hotkey, owner = bytes.fromhex(key * 32), bytes.fromhex('55' * 32)
        storage = _prefix('Keys', 82) + uid.to_bytes(2, 'little')
        keys.append(hex_encode(storage))
        source.storage[hex_encode(storage)] = hex_encode(hotkey)
        source.storage[hex_encode(_prefix('Uids', 82) + blake2_128_concat(hotkey))] = hex_encode(uid.to_bytes(2, 'little'))
        source.storage[hex_encode(_prefix('Owner') + blake2_128_concat(hotkey))] = hex_encode(owner)
        source.storage[hex_encode(_prefix('BlockAtRegistration', 82) + uid.to_bytes(2, 'little'))] = hex_encode((50).to_bytes(8, 'little'))
    source.storage[hex_encode(_prefix('SubnetworkN', 82))] = '0x0200'
    real = source.call
    def call(method, params):
        if method == 'chain_getBlockHash' and params == [0]:
            source.calls.append((method, params))
            return '0x' + contract['genesis_hash']
        if method == 'state_getKeysPaged':
            source.calls.append((method, params))
            assert params[3] == BLOCK
            return sorted(k for k in keys if k > params[2])[:params[1]]
        return real(method, params)
    source.call = call
    return source, contract, keys


def test_all_pages_and_registration_incarnations_are_retained():
    source, contract, _ = case()
    roster = read_snapshot_roster(source, contract, max_members=2)
    assert [row['uid'] for row in roster['members']] == [1, 256]
    assert all(row['registration_block'] == 50 for row in roster['members'])
    assert len([args for method, args in source.calls if method == 'state_getKeysPaged']) == 2
    assert roster['block_hash'] == contract['block_hash']
    assert roster['genesis_hash'] == contract['genesis_hash']


def test_truncated_enumeration_cannot_be_complete():
    source, contract, keys = case()
    keys.pop()
    with pytest.raises(ReportError, match='count mismatch'):
        read_snapshot_roster(source, contract, max_members=2)


def test_limit_refuses_instead_of_silently_truncating():
    source, contract, _ = case()
    with pytest.raises(ReportError, match='member limit'):
        read_snapshot_roster(source, contract, max_members=1)
    assert not any(method == 'state_getKeysPaged' for method, _ in source.calls)


@pytest.mark.parametrize('problem', ['inverse', 'owner', 'future_registration', 'duplicate_hotkey'])
def test_bad_identity_never_becomes_required_roster(problem):
    source, contract, keys = case()
    hotkey = bytes.fromhex('22' * 32)
    if problem == 'inverse':
        source.storage[hex_encode(_prefix('Uids', 82) + blake2_128_concat(hotkey))] = '0x0200'
    elif problem == 'owner':
        source.storage[hex_encode(_prefix('Owner') + blake2_128_concat(hotkey))] = None
    elif problem == 'future_registration':
        source.storage[hex_encode(_prefix('BlockAtRegistration', 82) + (1).to_bytes(2, 'little'))] = hex_encode((101).to_bytes(8, 'little'))
    else:
        source.storage[keys[1]] = source.storage[keys[0]]
    with pytest.raises(ReportError):
        read_snapshot_roster(source, contract, max_members=2)


@pytest.mark.parametrize('problem', ['repeat', 'foreign_prefix', 'malformed_key'])
def test_pagination_corruption_refuses(problem):
    source, contract, keys = case()
    real = source.call
    def call(method, params):
        if method == 'state_getKeysPaged':
            if problem == 'repeat':
                return [sorted(keys)[0], sorted(keys)[0]]
            if problem == 'foreign_prefix':
                return ['0x' + 'aa' * 36]
            return [keys[0][:-1] + 'z']
        return real(method, params)
    source.call = call
    with pytest.raises((ReportError, ValueError)):
        read_snapshot_roster(source, contract, max_members=2)


def test_wrong_genesis_stops_before_storage():
    source, contract, _ = case()
    real = source.call
    source.call = lambda method, params: '0x' + '99' * 32 if method == 'chain_getBlockHash' and params == [0] else real(method, params)
    with pytest.raises(ReportError, match='genesis'):
        read_snapshot_roster(source, contract, max_members=2)
    assert source.calls == []


def test_zero_registration_uses_qualified_runtime_default():
    source, contract, _ = case()
    source.storage[hex_encode(_prefix('BlockAtRegistration', 82) + (1).to_bytes(2, 'little'))] = None
    roster = read_snapshot_roster(source, contract, max_members=2)
    assert roster['members'][0]['registration_block'] == 0


def test_roster_reconstructs_from_raw_receipts_without_live_source():
    from noah.reports.backing.rpc import result_sha256
    from noah.reports.backing.source import ReplaySource
    source, contract, _ = case()
    real, receipts = source.call, []
    def capture(method, params):
        result = real(method, params)
        digest, size = result_sha256(result)
        receipts.append({'method': method, 'params': params, 'result': result,
                         'sha256': digest, 'byte_length': size})
        return result
    source.call = capture
    original = read_snapshot_roster(source, contract, max_members=2)
    replayed = read_snapshot_roster(ReplaySource(receipts, BLOCK), contract, max_members=2)
    assert replayed == original


@pytest.mark.parametrize('known_code', [False, True])
def test_483_roster_requires_pinned_code(known_code):
    from noah.reports.backing.constants import RUNTIME_483_CODE_HASH
    from noah.reports.backing.keys import networks_added_key
    source, contract, _ = case()
    source.spec = 483
    source.storage[hex_encode(networks_added_key(82))] = '0x01'
    real = source.call
    source.call = lambda method, params: (RUNTIME_483_CODE_HASH if known_code else '0x' + '00' * 32) if method == 'state_getStorageHash' else real(method, params)
    if known_code:
        assert len(read_snapshot_roster(source, contract, max_members=2)['members']) == 2
    else:
        with pytest.raises(ReportError, match='runtime unsupported'):
            read_snapshot_roster(source, contract, max_members=2)
        assert not any(method == 'state_getKeysPaged' for method, _ in source.calls)
