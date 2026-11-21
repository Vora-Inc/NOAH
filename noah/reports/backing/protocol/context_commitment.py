"""Explicit, opt-in report commitment context; historical hashes stay unchanged."""
from dataclasses import dataclass
import hashlib
import json
import struct

from .preimage import REPORT_POLICY, ReportSubmission, decode

FORMAT = 'context-v1'
DOMAIN = b'noah-rpt-c1'
HEADER = struct.Struct('<11sHH32s')
ENVELOPE = struct.Struct('<11sHH32s32s')
assert HEADER.size == 47 and ENVELOPE.size == 79


def enabled(contract):
    value = contract.get('commitment_format')
    if 'commitment_format' in contract and value != FORMAT:
        raise ValueError('unsupported commitment format')
    return value == FORMAT


def round_id(contract):
    from ...verification import validate_round
    validate_round(contract)
    if not enabled(contract) or contract['eligibility_policy_version'] != REPORT_POLICY:
        raise ValueError('context commitment requires an explicit report-only round')
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).digest()


@dataclass(frozen=True)
class ContextCommitment:
    mining_netuid: int
    reported_netuid: int
    round_id: bytes
    report_hash: bytes

    def header(self):
        return HEADER.pack(DOMAIN, self.mining_netuid, self.reported_netuid, self.round_id)

    def encode(self):
        return self.header() + self.report_hash


def decode_commitment(raw):
    if not isinstance(raw, bytes) or len(raw) != ENVELOPE.size:
        raise ValueError('context commitment length')
    domain, mining, reported, identity, report_hash = ENVELOPE.unpack(raw)
    if domain != DOMAIN or not mining or not reported:
        raise ValueError('context commitment domain or subnet')
    return ContextCommitment(mining, reported, identity, report_hash)


def matches_context(raw, contract):
    value = decode_commitment(raw)
    return (value.mining_netuid == contract['mining_netuid']
            and value.reported_netuid == contract['reported_netuid']
            and value.round_id == round_id(contract))


def matches_reveal(raw, preimage):
    """Hash binding only; payload context and accuracy remain verifier duties."""
    value = decode_commitment(raw)
    return hashlib.sha256(value.header() + preimage).digest() == value.report_hash


def build_commitment(contract, preimage):
    payload = decode(preimage)
    if not isinstance(payload, ReportSubmission):
        raise ValueError('context commitments require report-only preimages')
    if (payload.genesis_hash.hex() != contract['genesis_hash']
            or payload.mining_netuid != contract['mining_netuid']
            or payload.netuid != contract['reported_netuid']
            or payload.block_number != contract['block_number']
            or payload.block_hash.hex() != contract['block_hash']
            or payload.round_yyyymmdd != int(contract['round_date'].replace('-', ''))
            or payload.release_prefix.hex() != contract['release_prefix']):
        raise ValueError('report preimage differs from commitment round')
    value = ContextCommitment(contract['mining_netuid'], contract['reported_netuid'], round_id(contract), b'')
    return value.header() + hashlib.sha256(value.header() + preimage).digest()
