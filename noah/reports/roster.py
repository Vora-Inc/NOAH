"""Reconstruct the complete mining-subnet roster at a frozen round snapshot.

This reads raw storage through a receipt-recording source. It does not select
which members the report policy requires. A caller must compare independent
readers and retain/replay their separate receipt sessions before using it.
"""

from .adjudication_contract import natural, require
from .backing.keys import blake2_128_concat, netuid_le, storage_prefix
from .backing.reads import Reader
from .verification import validate_round


def _prefix(name, netuid=None):
    return storage_prefix('SubtensorModule', name) + (b'' if netuid is None else netuid_le(netuid))


def _raw(source, key, pin, size, *, default=None):
    value = source.call('state_getStorage', ['0x' + key.hex(), pin])
    if value is None and default is not None:
        return default
    require(type(value) is str and value.startswith('0x'), 'roster storage missing')
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError('roster storage not hex') from exc
    require(len(raw) == size and value == '0x' + raw.hex(), 'roster storage encoding')
    return raw


def read_scored_seat(source, seat, *, genesis_hash, mining_netuid):
    """Authenticate the current scoring incarnation at its frozen observation."""
    pin = '0x' + seat['observed_at']['block_hash']
    require(source.call('chain_getBlockHash', [0]) == '0x' + genesis_hash, 'seat genesis')
    reader = Reader(source, pin)
    reader.ensure_block(seat['observed_at']['block_number'])
    require(reader.qualified_runtime(reader.spec_version()), 'seat runtime unsupported')
    hotkey = bytes.fromhex(seat['hotkey'])
    uid = seat['uid']
    key = _prefix('Keys', mining_netuid) + uid.to_bytes(2, 'little')
    require(_raw(source, key, pin, 32) == hotkey, 'seat UID/hotkey differs')
    inverse = _prefix('Uids', mining_netuid) + blake2_128_concat(hotkey)
    require(int.from_bytes(_raw(source, inverse, pin, 2), 'little') == uid, 'seat inverse differs')
    owner = _raw(source, _prefix('Owner') + blake2_128_concat(hotkey), pin, 32)
    require(owner.hex() == seat['coldkey'], 'seat owner differs')
    key = _prefix('BlockAtRegistration', mining_netuid) + uid.to_bytes(2, 'little')
    registration = int.from_bytes(_raw(source, key, pin, 8, default=bytes(8)), 'little')
    require(registration == seat['registration_block'], 'seat registration incarnation differs')
    return dict(seat)


def read_snapshot_roster(source, contract, *, max_members):
    """Read all pages; refuse truncation, inconsistent inverse keys or identity.

    `max_members` is an explicit caller resource limit. Exceeding it refuses the
    whole roster, never returns a partial list. Runtime483 layout was checked
    against retained metadata; 490 against its pinned official source.
    """
    validate_round(contract)
    natural(max_members, 'roster member limit', 65535)
    require(max_members > 0, 'roster member limit must be positive')
    pin = '0x' + contract['block_hash']
    require(source.call('chain_getBlockHash', [0]) == '0x' + contract['genesis_hash'], 'roster genesis')
    reader = Reader(source, pin)
    number, timestamp = reader.ensure_block(contract['block_number'])
    require(timestamp == contract['block_timestamp_ms'], 'roster snapshot clock')
    spec = reader.spec_version()
    require(reader.qualified_runtime(spec), 'roster runtime unsupported')
    require(reader.subnet_state(contract['mining_netuid']) is not None, 'roster subnet absent')
    netuid = contract['mining_netuid']
    expected = int.from_bytes(_raw(source, _prefix('SubnetworkN', netuid), pin, 2, default=b'\0\0'), 'little')
    require(expected <= max_members, 'roster exceeds member limit')
    prefix = _prefix('Keys', netuid)
    prefix_hex = '0x' + prefix.hex()
    after = prefix_hex
    members = []
    hotkeys = set()
    while True:
        page = source.call('state_getKeysPaged', [prefix_hex, 128, after, pin])
        require(type(page) is list and len(page) <= 128, 'roster key page')
        if not page:
            break
        for key in page:
            require(type(key) is str and key == key.lower() and key.startswith(prefix_hex)
                    and len(key) == len(prefix_hex) + 4 and key > after, 'roster page key/order')
            try:
                raw_key = bytes.fromhex(key[2:])
            except ValueError as exc:
                raise ValueError('roster key not hex') from exc
            uid = int.from_bytes(raw_key[-2:], 'little')
            require(len(members) < max_members, 'roster exceeds member limit')
            hotkey = _raw(source, raw_key, pin, 32)
            require(hotkey != bytes(32) and hotkey not in hotkeys, 'roster duplicate/empty hotkey')
            hotkeys.add(hotkey)
            inverse = _prefix('Uids', netuid) + blake2_128_concat(hotkey)
            require(int.from_bytes(_raw(source, inverse, pin, 2), 'little') == uid, 'roster UID inverse mismatch')
            owner = _raw(source, _prefix('Owner') + blake2_128_concat(hotkey), pin, 32)
            require(owner != bytes(32), 'roster owner absent')
            registration_key = _prefix('BlockAtRegistration', netuid) + raw_key[-2:]
            registered = int.from_bytes(_raw(source, registration_key, pin, 8, default=bytes(8)), 'little')
            require(registered <= number, 'roster registration after snapshot')
            members.append({'uid': uid, 'hotkey': hotkey.hex(), 'coldkey': owner.hex(),
                            'registration_block': registered})
            after = key
    require(len(members) == expected, 'roster enumeration/count mismatch')
    return {'schema': 'noah.report-snapshot-roster/1',
            'genesis_hash': contract['genesis_hash'], 'mining_netuid': netuid,
            'block_number': number, 'block_hash': contract['block_hash'],
            'timestamp_ms': timestamp, 'spec_version': spec,
            'members': sorted(members, key=lambda member: member['uid'])}
