"""An SDK Default is not an explicit on-chain burn selection."""
import asyncio
from types import SimpleNamespace

import pytest

from noah.core import reader as module
from noah.core.reader import ChainReader, ChainUnreadable

PIN = '0x' + 'ab' * 32
KEY = bytes.fromhex('12' * 32 + '5200')


def reader(stored, *, decoded=None, fail=None, pin=PIN, missing_entry=None, param_types=('u16',)):
    calls = []
    entry = SimpleNamespace(value_type='RecycleOrBurn', param_types=param_types)
    class Codec:
        def storage_entry(self, pallet, item):
            assert (pallet, item) == ('SubtensorModule', 'RecycleOrBurn')
            if missing_entry is not None:
                raise missing_entry
            return entry
        def storage_key(self, actual, params):
            assert actual is entry and params == [82]
            return KEY
        def decode(self, value_type, data):
            calls.append(('decode', value_type, data))
            assert value_type == entry.value_type
            return decoded if decoded is not None else {b'\x00': 'Burn', b'\x01': 'Recycle'}[data]
    class Raw:
        async def rpc_request(self, method, params):
            calls.append((method, params))
            if method == 'chain_getFinalizedHead':
                assert params == []
                return pin
            assert (method, params) == ('state_getStorageAt', ['0x' + KEY.hex(), PIN])
            if fail:
                raise fail
            return stored
        async def codec_at(self, pin):
            calls.append(('codec_at', pin))
            assert pin == PIN
            return Codec()
    raw = Raw()
    raw._runtimes = raw
    client = SimpleNamespace(_client=SimpleNamespace(_substrate=SimpleNamespace(raw=raw)),
                             _call=asyncio.run,
                             query=lambda *a, **k: pytest.fail('default-substituting SDK query used'))
    return ChainReader(client), calls


def test_absent_raw_storage_never_becomes_default_burn():
    r, calls = reader(None, decoded='Burn')
    assert r.recycle_or_burn(82) is None
    assert not any(c[0] == 'decode' for c in calls)
    assert calls == [('chain_getFinalizedHead', []), ('codec_at', PIN),
                     ('state_getStorageAt', ['0x' + KEY.hex(), PIN])]


@pytest.mark.parametrize('raw,expected', [('0x00', 'Burn'), ('0x01', 'Recycle')])
def test_explicit_enum_decodes_only_actual_bytes_at_finalized_pin(raw, expected):
    r, calls = reader(raw)
    assert r.recycle_or_burn(82) == expected
    assert calls[-1] == ('decode', 'RecycleOrBurn', bytes.fromhex(raw[2:]))


@pytest.mark.parametrize('raw', ['0x02', '0x0000', '', False, {}, 0])
def test_malformed_present_storage_refuses(raw):
    r, calls = reader(raw)
    with pytest.raises(ChainUnreadable, match='canonical enum'):
        r.recycle_or_burn(82)
    assert not any(c[0] == 'decode' for c in calls)


def test_decoder_disagreement_cannot_authorize_burn():
    r, _ = reader('0x01', decoded='Burn')
    with pytest.raises(ChainUnreadable, match='raw and decoded'):
        r.recycle_or_burn(82)


def test_failed_storage_read_is_sanitized_and_never_default(monkeypatch):
    monkeypatch.setattr(module, '_transport_failure_types', lambda: (ConnectionError,))
    r, _ = reader(None, fail=ConnectionError('private endpoint sentinel'))
    with pytest.raises(ChainUnreadable) as error:
        r.recycle_or_burn(82)
    assert 'private endpoint sentinel' not in str(error.value)


def test_real_raw_transport_exception_is_sanitized():
    from bittensor._transport.errors import SubstrateRequestException

    r, calls = reader(None, fail=SubstrateRequestException({
        'error': {'code': -32000, 'message': 'private endpoint sentinel'},
    }))
    with pytest.raises(ChainUnreadable, match='could not read explicit RecycleOrBurn') as error:
        r.recycle_or_burn(82)
    assert 'private endpoint sentinel' not in str(error.value)
    assert not any(c[0] == 'decode' for c in calls)


@pytest.mark.parametrize('pin', [None, [], '0xab', '0x' + 'xy' * 32])
def test_invalid_finalized_pin_refuses_before_codec_or_storage(pin):
    r, calls = reader('0x00', pin=pin)
    with pytest.raises(ChainUnreadable, match='finalized block is invalid'):
        r.recycle_or_burn(82)
    assert calls == [('chain_getFinalizedHead', [])]


@pytest.mark.parametrize('netuid', [False, '82', -1, 65536])
def test_invalid_subnet_refuses_before_rpc(netuid):
    r, calls = reader('0x00')
    with pytest.raises(ChainUnreadable, match='subnet is not a u16'):
        r.recycle_or_burn(netuid)
    assert calls == []


def test_actual_missing_metadata_entry_is_not_explicit_burn():
    from bittensor._transport.errors import StorageFunctionNotFound

    r, calls = reader('0x00', missing_entry=StorageFunctionNotFound('missing storage entry'))
    assert r.recycle_or_burn(82) is None
    assert calls == [('chain_getFinalizedHead', []), ('codec_at', PIN)]


@pytest.mark.parametrize('raw,decoded', [('0x00', {'Burn': None}), ('0x01', {'Recycle': None})])
def test_variant_dict_decoder_preserves_explicit_value(raw, decoded):
    r, calls = reader(raw, decoded=decoded)
    assert r.recycle_or_burn(82) == next(iter(decoded))
    assert calls[-1] == ('decode', 'RecycleOrBurn', bytes.fromhex(raw[2:]))

@pytest.mark.parametrize('param_types', [(), ('u16', 'u16')])
def test_unexpected_metadata_arity_refuses_before_partial_key_read(param_types):
    r, calls = reader('0x00', param_types=param_types)
    with pytest.raises(ChainUnreadable, match='exactly one subnet key'):
        r.recycle_or_burn(82)
    assert calls == [('chain_getFinalizedHead', []), ('codec_at', PIN)]
