"""Read-only commitment quota at one finalized state; no wallet or submission.

483/490/491/492/493/494/495/497 source rule: get_epoch_index returns SubnetEpochIndex, not block/tempo
arithmetic. set_commitment resets UsedSpaceOf on *inequality* with that counter.
"""
import re

from ..common import ReportError
from ...chain.runtime_compatibility import lookup
from ...chain.sources import Refusal
from .constants import (
    RUNTIME_CODE_PINS, SUBTENSOR_PIN, SUBTENSOR_491_PIN, SUBTENSOR_492_PIN,
    SUBTENSOR_493_PIN, SUBTENSOR_494_PIN, SUBTENSOR_495_PIN, SUBTENSOR_497_PIN,
)
from .keys import ss58_decode, ss58_encode

U64_MAX = 2**64 - 1
SOURCE_PINS = {
    483: 'db3fdeb98bc82c38b238bfac9f6a3303a3361ee4',
    490: SUBTENSOR_PIN,
    491: SUBTENSOR_491_PIN,
    492: SUBTENSOR_492_PIN,
    493: SUBTENSOR_493_PIN,
    494: SUBTENSOR_494_PIN,
    495: SUBTENSOR_495_PIN,
    497: SUBTENSOR_497_PIN,
}


class QuotaRulesUnreviewed(ReportError):
    """The finalized runtime has no reviewed local quota arithmetic."""


def _require(condition, message):
    if not condition:
        raise ReportError('commitment quota: ' + message)


def _uint(value, bits=64):
    _require(type(value) is int and 0 <= value < 2**bits, 'invalid unsigned integer')
    return value


def _hash(value):
    _require(type(value) is str and re.fullmatch(r'0x[0-9a-f]{64}', value), 'invalid block or code hash')
    return value


def assess_quota(*, epoch_index, stored_usage, max_space, required_space):
    """Exact native dispatch arithmetic, including default and saturation rules."""
    epoch_index = _uint(epoch_index)
    max_space = _uint(max_space, 32)
    required_space = _uint(required_space)
    _require(required_space >= 100, 'required space is below the dispatch minimum')
    if stored_usage is None:
        last_epoch, used_space = 0, 0
    else:
        _require(type(stored_usage) is dict and set(stored_usage) == {'last_epoch', 'used_space'},
                 'invalid UsedSpaceOf fields')
        last_epoch = _uint(stored_usage['last_epoch'])
        used_space = _uint(stored_usage['used_space'])
    effective = used_space if last_epoch == epoch_index else 0
    total = min(U64_MAX, effective + required_space)
    return {'epoch_index': epoch_index, 'stored_usage': stored_usage,
            'effective_used_space': effective, 'max_space': max_space,
            'required_space': required_space, 'used_space_after': total,
            'allowed': total <= max_space}


def required_space_for_payload(payload):
    _require(type(payload) is bytes and len(payload) in (32, 79, 203, 207),
             'unsupported report phase payload')
    return max(100, len(payload))


def read_commitment_quota(substrate, *, mining_netuid, hotkey, required_space, at=None):
    """Return a public receipt; required_space=909 checks the whole new batch.

    All state queries use the same finalized hash. Runtime qualification retains
    the existing adapter policy: exact 483/491/492/493/494/495/497 code, or the supported 490 spec.
    The receipt records the observed code for each runtime.
    A positive result is an observation, not an atomic reservation.
    """
    _require(1 <= _uint(mining_netuid, 16) <= 65535, 'invalid mining subnet')
    _require(_uint(required_space) >= 100, 'required space is below the dispatch minimum')
    try:
        normalized_hotkey = ss58_encode(ss58_decode(hotkey))
        def rpc(method, params):
            response = substrate.rpc_request(method, params)
            _require(type(response) is dict and 'result' in response and 'error' not in response,
                     'RPC response unavailable')
            return response['result']
        finalized = _hash(rpc('chain_getFinalizedHead', []))
        pin = finalized if at is None else _hash(at)
        header = rpc('chain_getHeader', [pin])
        _require(type(header) is dict, 'finalized header unavailable')
        number = header.get('number')
        if type(number) is str:
            _require(re.fullmatch(r'0x[0-9a-f]+', number), 'invalid finalized height')
            number = int(number, 16)
        number = _uint(number)
        _require(_hash(rpc('chain_getBlockHash', [number])) == pin, 'finalized hash is not canonical')
        if at is not None:
            tip = rpc('chain_getHeader', [finalized])
            _require(type(tip) is dict, 'finalized height unavailable')
            tip_number = tip['number']
            if isinstance(tip_number, str):
                _require(re.fullmatch(r'0x[0-9a-f]{1,16}', tip_number), 'invalid finalized height')
                tip_number = int(tip_number, 16)
            _require(number <= _uint(tip_number), 'quota block is not finalized')
        genesis = _hash(rpc('chain_getBlockHash', [0]))
        version = rpc('state_getRuntimeVersion', [pin])
        _require(type(version) is dict, 'runtime unavailable')
        spec = _uint(version.get('specVersion'), 32)
        transaction = _uint(version.get('transactionVersion'), 32)
        code = _hash(rpc('state_getStorageHash', ['0x3a636f6465', pin]))
        bundled = (spec in SOURCE_PINS and (spec not in RUNTIME_CODE_PINS or code == RUNTIME_CODE_PINS[spec])
                   and (spec not in (494, 495, 497) or transaction == 2))
        approved = None if bundled else lookup(genesis, spec, transaction, code, 'reports')
        if not bundled and approved is None:
            raise QuotaRulesUnreviewed('commitment quota: unsupported finalized runtime')
        if approved is not None:
            from hashlib import sha256
            from ...chain.token_context import explicit_metadata

            _require(sha256(explicit_metadata(substrate, pin)).hexdigest() == approved.metadata,
                     'runtime metadata differs from approval')
        source = approved.source if approved else SOURCE_PINS[spec]
        qualification = 'spec-and-code' if approved or spec in RUNTIME_CODE_PINS else 'supported-spec'

        def query(module, name, params):
            result = substrate.query(module, name, params, block_hash=pin)
            _require(hasattr(result, 'value'), 'undecoded quota response')
            return result.value
        epoch = query('SubtensorModule', 'SubnetEpochIndex', [mining_netuid])
        usage = query('Commitments', 'UsedSpaceOf', [mining_netuid, normalized_hotkey])
        cap = query('Commitments', 'MaxSpace', [])
        timestamp = _uint(query('Timestamp', 'Now', []))
        assessment = assess_quota(epoch_index=epoch, stored_usage=usage, max_space=cap,
                                  required_space=required_space)
        return {'schema': 'noah.commitment-quota/1', 'genesis_hash': genesis,
                'finalized_block_number': number, 'finalized_block_hash': pin,
                'finalized_timestamp_ms': timestamp, 'mining_netuid': mining_netuid,
                'hotkey': normalized_hotkey, 'runtime': {
                    'spec_version': spec, 'transaction_version': transaction, 'code_hash': code,
                    'reviewed_source_commit': source,
                    'qualification': qualification},
                **assessment}
    except ReportError:
        raise
    except (Exception, Refusal):
        # Never turn a failed or malformed query into zero usage; do not leak URLs.
        raise ReportError('commitment quota: finalized state could not be read') from None
