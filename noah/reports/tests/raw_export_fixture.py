"""Entirely synthetic RPC facts; Finney is only the fixture's protocol domain.

The unchanged runtime490 metadata supplies SCALE types, not chain observations.
No real receipt, account state, block, approval or signature is reused. Zero
signature bytes merely encode a signed-extrinsic shape; no signing occurs.
"""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

from substrateinterface import Keypair, SubstrateInterface

from noah.reports.backing.chain_session import commitment_info
from noah.reports.backing.constants import PERQUINTILL_ONE
from noah.reports.backing.engine import compute
from noah.reports.backing.keys import blake2_128_concat, hex_encode, timestamp_now_key
from noah.reports.backing.protocol.preimage import ReportSubmission, REPORT_POLICY
from noah.reports.backing.scale import Availability, SimSwap
from noah.reports.common import digest
from noah.reports.public_export import GENESIS
from noah.reports.roster import _prefix
from noah.reports.tests.fakes import FakeChain, alpha_storage_key
from noah.reports.verification import CALCULATION

T0 = 1794700800000
HOT = bytes.fromhex('55' * 32)
COLD = bytes.fromhex('66' * 32)
ARK = bytes.fromhex('77' * 32)


def block_hash(number):
    return '0x' + (GENESIS if number == 0 else hashlib.sha256(f'synthetic-report-block-{number}'.encode()).hexdigest())


class RawSocket:
    def __init__(self, rpc, budget=None):
        self.rpc, self.budget, self.pending = rpc, budget, None
        self.timeout = 30

    def send(self, raw):
        request = json.loads(raw)
        if self.budget:
            self.budget.request(request)
        self.pending = json.dumps({'jsonrpc': '2.0', 'id': request['id'],
            'result': self.rpc.call(request['method'], request.get('params') or [])})

    def recv(self):
        raw, self.pending = self.pending, None
        return self.budget.response(raw) if self.budget else raw

    def close(self):
        pass


class RawChain:
    def __init__(self, *, context=False, unassigned=False, assigned_missing=False):
        self.context = context
        self.unassigned = unassigned
        self.assigned_missing = assigned_missing
        fixture = Path(__file__).parent / 'fixtures/synthetic/runtime490-metadata.hex.gz'
        self.metadata = gzip.decompress(fixture.read_bytes()).decode()
        assert hashlib.sha256(bytes.fromhex(self.metadata[2:])).hexdigest() == 'a76cbe520927d0e232533b84bef160e029b0cd8acf782de458d2d2008500bebc'
        self.calls = []
        self.headers = {block_hash(n): {'number': hex(n), 'parentHash': block_hash(n - 1) if n else '0x'+'00'*32,
            'stateRoot': '0x'+'00'*32, 'extrinsicsRoot': '0x'+'00'*32, 'digest': {'logs': []}} for n in range(7)}
        self.blocks = {key: {'block': {'header': header, 'extrinsics': []}, 'justifications': None}
                       for key, header in self.headers.items()}
        self.events = {}
        self.storage = {}
        self.engine = FakeChain(block_hash(2))
        self.engine.number = 2
        self.engine.timestamp = T0 + 24000
        self.engine.netuid = 82
        self.engine.set_pool(82, alpha_in=50000000000000, tao=50000000000000,
            alpha_out=2000000000000, quote=PERQUINTILL_ONE // 2)
        holder = bytes.fromhex('88' * 32)
        self.engine.alpha_keys = [hex_encode(alpha_storage_key(b'\x99'*32, holder, 82))]
        self.engine.availability[holder] = Availability(2000000000000, 0, 2000000000000)
        self.engine.sim = lambda amount: SimSwap(amount // 10, amount, 0, 0, 0, 0)
        for name, suffix, raw in (
            ('Keys', (0).to_bytes(2,'little'), HOT),
            ('Uids', blake2_128_concat(HOT), (0).to_bytes(2,'little')),
            ('BlockAtRegistration', (0).to_bytes(2,'little'), (1).to_bytes(8,'little')),
            ('SubnetworkN', b'', (1).to_bytes(2,'little')),
        ):
            self.storage[hex_encode(_prefix(name, 82) + suffix)] = hex_encode(raw)
        self.storage[hex_encode(_prefix('Owner') + blake2_128_concat(HOT))] = hex_encode(COLD)
        self.contract = dict(schema='noah.backing.round.v1', genesis_hash=GENESIS,
            mining_netuid=82, reported_netuid=82, round_date='2026-11-15',
            block_number=2, block_hash=block_hash(2)[2:], block_timestamp_ms=T0+24000,
            commit_open_ms=T0+24000, commit_close_ms=T0+48000,
            reveal_open_ms=T0+48000, reveal_close_ms=T0+72000,
            eligibility_policy_version=REPORT_POLICY, scoring_release='points-v20',
            release_prefix='ab'*16, calculation_version=CALCULATION,
            ark_coldkey=ARK.hex(), ark_validator_hotkey='aa'*32)
        if context:
            self.contract['commitment_format'] = 'context-v1'
        if unassigned:
            # This incarnation joined after the weekly roster but before B.
            extra_hot, extra_cold = bytes.fromhex('44' * 32), bytes.fromhex('33' * 32)
            for name, suffix, raw in (
                ('Keys', (1).to_bytes(2, 'little'), extra_hot),
                ('Uids', blake2_128_concat(extra_hot), (1).to_bytes(2, 'little')),
                ('BlockAtRegistration', (1).to_bytes(2, 'little'), (2).to_bytes(8, 'little')),
                ('SubnetworkN', b'', (2).to_bytes(2, 'little')),
            ):
                self.storage[hex_encode(_prefix(name, 82) + suffix)] = hex_encode(raw)
            self.storage[hex_encode(_prefix('Owner') + blake2_128_concat(extra_hot))] = hex_encode(extra_cold)
        self.expected_engine, _ = compute(self, 82, block_hash(2), 2, ark=ARK)
        assert self.expected_engine['status'] == 'OK'
        self._encode_blocks()

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method in ('chain_getHead', 'chain_getFinalizedHead'):
            return block_hash(6)
        if method == 'chain_getBlockHash':
            value = params[0] if params else 6
            return block_hash(int(value, 16) if isinstance(value, str) else value)
        if method == 'chain_getHeader':
            return deepcopy(self.headers[params[0] if params else block_hash(6)])
        if method == 'chain_getBlock':
            return deepcopy(self.blocks[params[0]])
        if method == 'state_getRuntimeVersion':
            return {'specName': 'node-subtensor', 'implName': 'synthetic-fixture', 'specVersion': 490,
                'transactionVersion': 1, 'authoringVersion': 1, 'implVersion': 1, 'apis': []}
        if method == 'state_getMetadata':
            return self.metadata
        if method == 'rpc_methods':
            return {'methods': ['state_getStorage', 'state_getStorageAt', 'state_getRuntimeVersion', 'state_getMetadata'], 'version': 1}
        if method == 'system_chain':
            return 'synthetic-report-fixture'
        if method == 'system_properties':
            return {'ss58Format': 42, 'tokenDecimals': 9, 'tokenSymbol': 'TEST'}
        if method in ('state_getStorage', 'state_getStorageAt'):
            key, pin = params
            if key == hex_encode(timestamp_now_key()):
                n = int(self.headers[pin]['number'], 16)
                return hex_encode((T0+n*12000).to_bytes(8,'little'))
            if key in self.events:
                return self.events[key].get(pin, '0x00')
            return self.storage[key] if key in self.storage else self.engine.storage.get(key)
        if method == 'state_getKeysPaged':
            prefix, count, after, _ = params
            keys = sorted([key for key in self.storage if key.startswith(prefix)] +
                          [key for key in self.engine.alpha_keys if key.startswith(prefix)])
            return [key for key in keys if after is None or key > after][:count]
        if method == 'state_call':
            return self.engine._state_call(params[0], params[1])
        raise AssertionError(f'unimplemented synthetic RPC: {method} {params}')

    def _encode_blocks(self):
        self.sdk = SubstrateInterface(websocket=RawSocket(self), auto_discover=False)
        self.sdk.init_runtime()
        for n, body in enumerate(self.blocks.values()):
            call = self.sdk.compose_call('Timestamp','set',{'now': T0+n*12000})
            body['block']['extrinsics'].append(str(self.sdk.create_unsigned_extrinsic(call).data))
        values = self.expected_engine
        report = ReportSubmission(bytes.fromhex(GENESIS), HOT, COLD, 82, 82, 20261115,
            2, bytes.fromhex(block_hash(2)[2:]), values['output1']['drop_ppm'],
            values['output2']['alpha_required_rao'], values['output3']['alpha_required_rao'],
            bytes.fromhex(self.contract['release_prefix']), b's'*16).encode()
        variant, commitment = 'Raw32', hashlib.sha256(report).digest()
        if self.context:
            from noah.reports.backing.protocol.context_commitment import build_commitment
            variant, commitment = 'BigRaw', build_commitment(self.contract, report)
        for number, variant, payload in ((3, variant, commitment), (5, 'BigRaw', report)):
            if self.assigned_missing and number == 5:
                continue
            call = self.sdk.compose_call('Commitments', 'set_commitment', {'netuid':82,'info':commitment_info(variant,payload)})
            ext = self.sdk.create_signed_extrinsic(call, Keypair(public_key=HOT, crypto_type=1, ss58_format=42), nonce=0, signature=bytes(64))
            self.blocks[block_hash(number)]['block']['extrinsics'].append(str(ext.data))
        key = self.sdk.create_storage_key('System', 'Events', [])
        self.event_key = key.to_hex()
        value_type = key.value_scale_type
        self.events[self.event_key] = {}
        for n in range(7):
            events = []
            if n == 3 or (n == 5 and not self.assigned_missing):
                events = [{'phase': {'ApplyExtrinsic':1}, 'event': {'System': {'ExtrinsicSuccess': {
                    'dispatch_info': {'weight': {'ref_time': 0, 'proof_size': 0}, 'class':'Normal','pays_fee':'Yes'}}}}, 'topics':[]}]
            obj = self.sdk.runtime_config.create_scale_object(value_type, metadata=self.sdk.metadata)
            self.events[self.event_key][block_hash(n)] = str(obj.encode(events))

    def request(self):
        day = T0 // 86400000
        policy = {'schema':'noah.report-policy/1','genesis_hash':GENESIS,'mining_netuid':82,
            'effective_day':day,'eligibility_policy_version':REPORT_POLICY,'required_rounds':[self.contract]}
        return {'schema':'noah.report-adjudication-request/1','policy':policy,'policy_sha256':digest(policy),
            'genesis_hash':GENESIS,'mining_netuid':82,'eval_day':day,'day_anchor_ts':0,
            'evaluation':{'block_number':6,'block_hash':block_hash(6)[2:],'timestamp_ms':T0+72000},
            'calculation':{'reference':{'path':'evidence/'+'1'*64+'.json','sha256':'1'*64,'day':day,
                'genesis_hash':GENESIS,'candidate_sha256':None},'fixture_sha256':'2'*64},
            'scored_seats':[{'uid':0,'hotkey':HOT.hex(),'coldkey':COLD.hex(),'registration_block':1,
                'registration_day':day,'observed_at':{'block_number':6,'block_hash':block_hash(6)[2:]}}]}
