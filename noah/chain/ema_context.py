"""Exact runtime EMA/spot observations, without economic valuation or fallback.

Slow emission EMA is capped at a one-TAO input by runtimes483/488. Fast EMA is
separate and unclamped, but is not accepted in this release. The policy selects
the slow field explicitly; raw absence is kept.
"""
from . import token_context as token
from .sources import Unverified

SOURCES = {
    'SubnetMovingPrice': ('I96F32', 2**32, True, 'Default'),
}
EMA_CODES = {version: token.CODES[version] for version in (483, 488)}


def source(value):
    if value not in SOURCES:
        raise Unverified('unreviewed EMA source; no alternate field is selected')
    return SOURCES[value]


def fixed(raw, signed):
    data = token.hex_bytes(raw, 16)
    token.require(len(data) == 16, 'EMA storage width differs')
    return int.from_bytes(data, 'little', signed=signed)


def batch(connection, at, keys):
    result = token.rpc(connection, 'state_queryStorageAt', [keys, at])
    token.require(isinstance(result, list) and len(result) == 1 and result[0]['block'] == at)
    values = {}
    for row in result[0]['changes']:
        token.require(isinstance(row, (list, tuple)) and len(row) == 2)
        key, value = row
        token.require(key in keys and key not in values)
        if value is not None:
            token.hex_bytes(value, 16)
        values[key] = value
    token.require(set(values) == set(keys))
    return values


def read(connection, block, block_hash, assets, cache, ema_source):
    encoding, denominator, signed, modifier = source(ema_source)
    context = token.read(connection, block, block_hash, assets, cache)
    if context['runtime']['spec_version'] not in EMA_CODES:
        raise Unverified('EMA runtime semantics have not been reviewed')
    runtime = connection.init_runtime(block_hash=block_hash)
    # At an upgrade boundary the SDK may use parent metadata. Never silently
    # decode the chosen field with a different target schema.
    if token.metadata(runtime.metadata.data.to_hex()) != cache[context['runtime']['code_hash']]:
        raise Unverified('EMA SDK metadata does not match the exact reference runtime')
    pallet = runtime.metadata.get_metadata_pallet('SubtensorModule')
    item = pallet.get_storage_function(ema_source)
    if item is None:
        raise Unverified('selected EMA field is not declared')
    token.require(item.value['modifier'] == modifier and len(item.get_params_type_string()) == 1)
    default = item.value_object['default'].value_object
    default = default if isinstance(default, str) else '0x' + bytes(default).hex()
    token.require(default == ('0x' + '00'*16 if signed else '0x00'))
    keys = connection.create_storage_keys('SubtensorModule', ema_source, [[a] for a in assets], block_hash=block_hash)
    token.require(len(keys) == len(assets))
    import xxhash
    def twox(value):
        return b''.join(xxhash.xxh64(value, seed=i).intdigest().to_bytes(8, 'little') for i in (0, 1))
    for asset, key in zip(assets, keys, strict=True):
        expected = twox(b'SubtensorModule') + twox(ema_source.encode()) + asset.to_bytes(2, 'little')
        token.require(key.to_hex().lower() == '0x' + expected.hex(), 'EMA SDK storage key differs')
    hexes = [k.to_hex() for k in keys]
    token.require(len(set(hexes)) == len(hexes))
    raw = batch(connection, block_hash, hexes)
    answers = connection.query_multi(keys, block_hash=block_hash)
    values = {}
    for key, value in answers:
        k = key.to_hex()
        token.require(k in raw and k not in values)
        values[k] = token.plain(value)
    token.require(set(values) == set(raw) and batch(connection, block_hash, hexes) == raw)
    prices = {}
    for asset, key in zip(assets, hexes, strict=True):
        observed = raw[key]
        decoded = values[key]
        if observed is None:
            token.require(isinstance(decoded, dict) and set(decoded) == {'bits'} and
                          type(decoded['bits']) is int and decoded['bits'] == 0,
                          'EMA absence/default contradiction')
            bits = None  # no inference that an absent raw field is a stored zero
        else:
            bits = fixed(observed, signed)
            token.require(isinstance(decoded, dict) and set(decoded) == {'bits'} and type(decoded['bits']) is int and decoded['bits'] == bits)
        spot_raw = token.rpc(connection, 'state_call', ['SwapRuntimeApi_current_alpha_price', '0x'+asset.to_bytes(2, 'little').hex(), block_hash])
        data = token.hex_bytes(spot_raw, 8)
        token.require(len(data) == 8, 'spot runtime result is not u64')
        spot = int.from_bytes(data, 'little')
        sdk_spot = token.plain(connection.runtime_call('SwapRuntimeApi', 'current_alpha_price', [(asset,)], block_hash=block_hash))
        token.require(type(sdk_spot) is int and sdk_spot == spot, 'spot runtime/SDK contradiction')
        prices[str(asset)] = {
            'ema': {'field': ema_source, 'encoding': encoding, 'raw': observed,
                    'bits': bits, 'numerator': bits, 'denominator': denominator,
                    'key': key, 'default': default},
            'spot': {'raw': spot_raw, 'rao': spot, 'numerator': spot,
                     'denominator': 10**9},
        }
    return {**context, 'ema_source': ema_source, 'prices': prices}
