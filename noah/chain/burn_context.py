"""Narrow proof of reviewed effective Burn, including a genuinely absent key.

The exact V15 digest authenticates the reviewed storage declaration, Identity
u16 key, Default 0x00 and Burn=0/Recycle=1 layout. The exact deployed code hash
binds that layout to run_coinbase's Burn-or-absent fallback at upstream commit
e536e6bf4bfa58f1ec4506dff0147e1f9b1c1900 (495),
a68c6e8c65411a842fbf69269dc385aa13041391 (494),
8b407e40988ea7a251abb6dbc07a9ab7d602b740 (493),
541a50d38764acdce23578bd8ba54f80156ffb2a (492) and
db3fdeb98bc82c38b238bfac9f6a3303a3361ee4 (483). RPC errors are never runtime absence.
No SDK defaults, parent-runtime codec or spec-only cache are used here.
"""
from hashlib import sha256
from types import SimpleNamespace
import re

from . import token_context as token
from .runtime_compatibility import RuntimeApprovalRequired, lookup
from .sources import Unverified, validated_hash_digits

REVIEWED = {
    483: ('0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2', 'eb3df3b6d0f2677d96e515d667259ec046cedd6059a4bdfc6848d44f5fd1b15a'),
    492: ('0x22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e', 'a397a753ad67ba335fb02cd5c54ccb0c1149fbf981f3ed6d9df44d49ca5bf71d'),
    493: ('0x51adea39b16a2b9d60b58d430246d49312f1111b74607edc1166e47a73de4591', '89f3e67a94db5f9a155678122b5a5daff5b2ddbb1f179ac669df996bd3332a98'),
    494: (token.CODES[494], '80df15954eca22bf5349a687563ccc9ec03ef101c9916779a4e8782143aa5c1c'),
    495: (token.CODES[495], 'c4fbaf6d9365096d42eecc1e279f16002d5a62bf95407b9e348df2693ca77659'),
}
SCHEMA = 'noah-effective-burn/1'


def require(value, message='effective Burn evidence malformed'):
    if not value:
        raise Unverified(message)


def storage_key(netuid):
    require(type(netuid) is int and 0 <= netuid <= 65535)
    # Reviewed Twox128("SubtensorModule") || Twox128("RecycleOrBurn").
    # The admitted full metadata digests bind this fixed prefix and Identity
    # u16 layout; no hashing SDK dependency is needed to append the subnet.
    prefix = '658faa385070e074c85bf6b568cf055530823bc1353bfe6cb262413a5bd7a023'
    return '0x' + prefix + netuid.to_bytes(2, 'little').hex()


def metadata_for(genesis, spec, transaction, code):
    if type(spec) is int and type(transaction) is int and spec in REVIEWED:
        bundled_code, digest = REVIEWED[spec]
        if code == bundled_code and transaction == token.transaction_version(spec):
            return digest
    approved = lookup(genesis, spec, transaction, code, 'burn')
    if approved is None:
        raise RuntimeApprovalRequired('effective Burn')
    return approved.metadata


def validate(evidence):
    """Validate a retained proof's shape; live authorization still requires read()."""
    require(type(evidence) is dict and set(evidence) == {
        'schema', 'block', 'block_hash', 'genesis', 'netuid', 'spec_version',
        'transaction_version', 'code_hash', 'metadata_sha256', 'key', 'raw',
        'effective', 'origin',
    })
    require(evidence['schema'] == SCHEMA)
    require(type(evidence['block']) is int and evidence['block'] > 0)
    require(all(isinstance(evidence[k], str) and evidence[k].startswith('0x')
                and validated_hash_digits(evidence[k]) and int(evidence[k][2:], 16)
                for k in ('block_hash', 'genesis')))
    require(evidence['metadata_sha256'] == metadata_for(
        evidence['genesis'], evidence['spec_version'], evidence['transaction_version'], evidence['code_hash']))
    require(evidence['key'] == storage_key(evidence['netuid']))
    require(evidence['raw'] is None or evidence['raw'] == '0x00', 'effective Burn requires Burn or proven absence')
    require(evidence['effective'] == 'Burn')
    require(evidence['origin'] == ('runtime-default' if evidence['raw'] is None else 'explicit'))
    return evidence


def read(rpc, block, block_hash, netuid):
    """Read one finalized pin; rpc returns bare results and raises on errors.

    Each independent reader performs its own reads. This intentionally does not
    cache state or metadata: burn checks occur only for nonzero burn publication.
    """
    key = storage_key(netuid)
    require(type(block) is int and block > 0)
    require(isinstance(block_hash, str) and block_hash.startswith('0x') and validated_hash_digits(block_hash) and int(block_hash[2:], 16))
    head = rpc('chain_getFinalizedHead', [])
    require(isinstance(head, str) and head.startswith('0x') and validated_hash_digits(head) and int(head[2:], 16))
    header = rpc('chain_getHeader', [head])
    require(type(header) is dict and isinstance(header.get('number'), str)
            and re.fullmatch(r'0x[0-9a-fA-F]{1,16}', header['number']))
    require(int(header['number'], 16) >= block, 'effective Burn pin is not finalized')
    require(rpc('chain_getBlockHash', [block]) == block_hash, 'effective Burn block hash differs')
    genesis = rpc('chain_getBlockHash', [0])
    runtime = rpc('state_getRuntimeVersion', [block_hash])
    require(type(runtime) is dict)
    code = rpc('state_getStorageHash', ['0x3a636f6465', block_hash])
    metadata_sha = metadata_for(genesis, runtime.get('specVersion'), runtime.get('transactionVersion'), code)
    # Use the existing strict Option<Vec<V15>> framing parser, adapting only
    # its synchronous response envelope. No SDK query/default path is involved.
    connection = SimpleNamespace(rpc_request=lambda method, params: {'result': rpc(method, params)})
    metadata = token.explicit_metadata(connection, block_hash)
    require(sha256(metadata).hexdigest() == metadata_sha,
            'effective Burn metadata declaration/default is not reviewed')
    def raw_value():
        result = rpc('state_queryStorageAt', [[key], block_hash])
        require(type(result) is list and len(result) == 1 and type(result[0]) is dict)
        require(result[0].get('block') == block_hash)
        changes = result[0].get('changes')
        require(type(changes) is list and len(changes) == 1
                and isinstance(changes[0], (list, tuple)) and len(changes[0]) == 2
                and changes[0][0] == key)
        return changes[0][1]
    raw = raw_value()
    require(raw is None or raw == '0x00', 'effective Burn requires Burn or proven absence')
    require(raw_value() == raw, 'effective Burn storage changed at finalized pin')
    require(rpc('chain_getBlockHash', [block]) == block_hash, 'effective Burn block hash changed')
    return validate(dict(schema=SCHEMA, block=block, block_hash=block_hash, genesis=genesis,
                         netuid=netuid, spec_version=runtime['specVersion'],
                         transaction_version=runtime['transactionVersion'],
                         code_hash=code, metadata_sha256=metadata_sha, key=key, raw=raw,
                         effective='Burn', origin='runtime-default' if raw is None else 'explicit'))
