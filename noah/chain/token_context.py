"""Paired-read inputs for prospective token-reference eligibility.

Known runtime code, explicit V15 metadata, raw presence and SDK decoding remain
separate evidence. No zero/absence is inferred from a failed read. The caller
uses AgreeingChain to compare this entire result from independently checked
readers. Local metadata memoization is keyed by exact deployed code hash.
"""
import hashlib
import re

from .sources import Refusal, SourceLied, Unverified

CODES = {
    483: '0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2',
    486: '0x49781ac4cfda0ac6cf6e53cfceea7c728394d9c28da7881f47e3fbfbc239800f',
    488: '0x3147daab804046184359d8a929cb333d75f490323b9e1327887a172c64eafa94',
}
FIELDS = (('NetworksAdded', 'bool'), ('NetworkRegisteredAt', 'u64'),
          ('RegisteredSubnetCounter', 'u64'), ('SubnetMechanism', 'u16'))


def require(value, message='token reference evidence malformed'):
    if not value:
        raise SourceLied(message)


def plain(value):
    return getattr(value, 'value', value)


def rpc(connection, method, params):
    result = connection.rpc_request(method, params)
    if isinstance(result, dict) and 'error' in result:
        from .live import ChainUnavailable
        raise ChainUnavailable('historical token reference RPC did not return data')
    require(isinstance(result, dict) and 'result' in result)
    return result['result']


def hex_bytes(raw, limit):
    require(isinstance(raw, str) and re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', raw) and len(raw) <= 2 + 2 * limit)
    return bytes.fromhex(raw[2:])


def metadata(raw):
    data = hex_bytes(raw, 16 * 1024**2)
    require(len(data) >= 5 and data[:5] == b'meta\x0f')
    return data


def explicit_metadata(connection, at):
    data = hex_bytes(rpc(connection, 'state_call', ['Metadata_metadata_at_version', '0x0f000000', at]), 16 * 1024**2 + 5)
    require(len(data) >= 2 and data[0] == 1 and data[1] & 3 != 3)
    mode = data[1] & 3
    width = (1, 2, 4)[mode]
    size = int.from_bytes(data[1:1 + width], 'little') >> 2
    require(size >= (0, 64, 16384)[mode] and len(data) == 1 + width + size)
    return metadata('0x' + data[1 + width:].hex())


def decoded(raw, kind):
    width = {'bool': 1, 'u16': 2, 'u64': 8}[kind]
    data = hex_bytes(raw, width)
    require(len(data) == width)
    value = int.from_bytes(data, 'little')
    if kind == 'bool':
        require(value in (0, 1))
        return bool(value)
    return value


def runtime_identity(connection, at):
    version = rpc(connection, 'state_getRuntimeVersion', [at])
    require(isinstance(version, dict) and type(version.get('specVersion')) is int)
    code = rpc(connection, 'state_getStorageHash', ['0x3a636f6465', at])
    if version['specVersion'] not in CODES or code != CODES[version['specVersion']] or version.get('transactionVersion') != 1:
        raise Unverified('token lifecycle runtime semantics have not been reviewed')
    return {'spec_version': version['specVersion'], 'code_hash': code, 'transaction_version': 1}


def raw_batch(connection, at, keys):
    groups = rpc(connection, 'state_queryStorageAt', [keys, at])
    require(isinstance(groups, list) and len(groups) == 1 and groups[0]['block'] == at)
    rows = groups[0]['changes']
    require(isinstance(rows, list) and len(rows) == len(keys))
    result = {}
    for row in rows:
        require(isinstance(row, (list, tuple)) and len(row) == 2)
        key, value = row
        require(key in keys and key not in result)
        if value is not None:
            hex_bytes(value, 8)
        result[key] = value
    require(set(result) == set(keys))
    return result


def read(connection, block, block_hash, assets, cache):
    require(type(block) is int and block > 0 and 0 < len(assets) <= 9)
    require(len(set(assets)) == len(assets) and all(type(a) is int and 0 < a < 65536 for a in assets))
    header = rpc(connection, 'chain_getHeader', [block_hash])
    require(int(header['number'], 16) == block)
    parent = header['parentHash']
    require(isinstance(parent, str) and re.fullmatch('0x[0-9a-f]{64}', parent))
    identity = runtime_identity(connection, block_hash)
    parent_identity = runtime_identity(connection, parent)
    code = identity['code_hash']
    if code not in cache:
        target = explicit_metadata(connection, block_hash)
        cache[code] = target
    parent_code = parent_identity["code_hash"]
    if parent_code not in cache:
        cache[parent_code] = explicit_metadata(connection, parent)
    runtime = connection.init_runtime(block_hash=block_hash)
    require(metadata(runtime.metadata.data.to_hex()) in (cache[code], cache[parent_code]))
    pallet = runtime.metadata.get_metadata_pallet('SubtensorModule')
    require(pallet is not None)
    keys, descriptors = [], []
    for name, kind in FIELDS:
        item = pallet.get_storage_function(name)
        if item is None:
            raise Unverified('required token lifecycle storage is not declared')
        require(item.value['modifier'] == 'Default' and len(item.get_params_type_string()) == 1)
        default = item.value_object['default'].value_object
        default = default if isinstance(default, str) else '0x' + bytes(default).hex()
        default_value = decoded(default, kind)
        require(default_value == (False if kind == "bool" else 0))
        generated = connection.create_storage_keys('SubtensorModule', name, [[a] for a in assets], block_hash=block_hash)
        require(len(generated) == len(assets))
        for asset, key in zip(assets, generated, strict=True):
            hex_bytes(key.to_hex(), 128)
            import xxhash
            def twox128(value):
                return b''.join(xxhash.xxh64(value, seed=seed).intdigest().to_bytes(8, 'little') for seed in (0, 1))
            expected = twox128(b'SubtensorModule') + twox128(name.encode()) + asset.to_bytes(2, 'little')
            require(key.to_hex().lower() == '0x' + expected.hex(), 'lifecycle SDK key differs from reviewed runtime storage')
            keys.append(key)
            descriptors.append((asset, name, kind, default, default_value, key.to_hex()))
    key_hexes = [k.to_hex() for k in keys]
    require(len(set(key_hexes)) == len(keys))
    first = raw_batch(connection, block_hash, key_hexes)
    answers = connection.query_multi(keys, block_hash=block_hash)
    require(len(answers) == len(keys))
    values = {}
    for key, value in answers:
        k = key.to_hex()
        require(k in first and k not in values)
        values[k] = plain(value)
    require(set(values) == set(first) and raw_batch(connection, block_hash, key_hexes) == first)
    rows = {str(a): {} for a in assets}
    for asset, name, kind, default, default_value, key in descriptors:
        raw = first[key]
        value = values[key]
        require(type(value) is (bool if kind == 'bool' else int))
        require(value == (default_value if raw is None else decoded(raw, kind)))
        if name == 'NetworkRegisteredAt' and value > block:
            raise SourceLied('token birth is later than sampled state')
        rows[str(asset)][name] = {'raw': raw, 'value': value, 'default': default, 'key': key}
    return dict(block=block, block_hash=block_hash, parent_hash=parent, runtime=identity, parent_runtime=parent_identity,
                metadata_sha256=hashlib.sha256(cache[code]).hexdigest(), assets=rows)
