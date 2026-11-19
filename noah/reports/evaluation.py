"""Bounded raw-response proof that a frozen report evaluation block is finalized."""

import re

from .adjudication_contract import _block, hex32, natural, require
from .backing.reads import Reader


def _hash(value):
    require(type(value) is str and re.fullmatch(r'0x[0-9a-f]{64}', value), 'evaluation hash')
    return value


def _header(source, block_hash):
    value = source.call('chain_getHeader', [block_hash])
    require(type(value) is dict and type(value.get('number')) is str and
            re.fullmatch(r'0x[0-9a-f]+', value['number']), 'evaluation header')
    number = int(value['number'], 16)
    natural(number, 'evaluation block number', 2**32 - 1)
    return number, _hash(value.get('parentHash'))


def prove_evaluation(source, evaluation, *, genesis_hash, max_ancestor_distance):
    """Rebuild from recorded receipts using the same function, without network.

    Each reader uses a separate receipt session. This proves what that reader
    reported, not cryptographic consensus; production also compares independent
    readers and binds the exact boundary to the trusted request.
    """
    _block(evaluation, 'evaluation', timestamp=True)
    hex32(genesis_hash, 'evaluation genesis')
    natural(max_ancestor_distance, 'evaluation ancestry limit', 10000)
    require(source.call('chain_getBlockHash', [0]) == '0x' + genesis_hash, 'evaluation genesis mismatch')
    pin = '0x' + evaluation['block_hash']
    head = _hash(source.call('chain_getFinalizedHead', []))
    number, parent = _header(source, head)
    head_number = number
    distance = number - evaluation['block_number']
    require(0 <= distance <= max_ancestor_distance, 'evaluation outside finalized ancestry budget')
    ancestry = [head]
    cursor = head
    while number > evaluation['block_number']:
        cursor = parent
        next_number, parent = _header(source, cursor)
        require(next_number == number - 1, 'evaluation ancestry discontinuity')
        number = next_number
        ancestry.append(cursor)
    require(cursor == pin, 'evaluation finalized fork mismatch')
    require(source.call('chain_getBlockHash', [hex(number)]) == pin, 'evaluation canonical hash mismatch')
    reader = Reader(source, pin)
    spec = reader.spec_version()
    require(reader.qualified_runtime(spec), 'evaluation runtime unsupported')
    require(reader.timestamp_ms() == evaluation['timestamp_ms'], 'evaluation timestamp mismatch')
    return {'schema': 'noah.report-evaluation-proof/1', 'genesis_hash': genesis_hash,
            'evaluation': dict(evaluation), 'spec_version': spec,
            'finalized_head': {'block_hash': head[2:], 'block_number': head_number},
            'ancestry': [value[2:] for value in ancestry]}
