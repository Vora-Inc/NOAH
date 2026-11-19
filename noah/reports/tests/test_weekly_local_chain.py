"""Disposable local chain. Real miner and validator commands, no live network.

The chain is an in-process JSON-RPC server using the retained runtime 490
metadata. It is not a subtensor binary and it does not spend real funds.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import os

import sr25519
import pytest
import threading

from bittensor_wallet import Wallet
from scalecodec.base import ScaleBytes
from substrateinterface import Keypair
from websockets.sync.server import serve

from noah.reports.backing.chain_session import commitment_info
from noah.reports.backing.constants import PERQUINTILL_ONE
from noah.reports.backing.keys import blake2_128_concat, hex_encode, timestamp_now_key
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.roster import _prefix
from noah.reports.tests.raw_export_fixture import RawChain, T0, block_hash

LOCAL_GENESIS = '12' * 32
from noah.reports.verification import CALCULATION

ROOT = Path(__file__).resolve().parents[3]
PYTHON = Path(sys.executable)
TARGETS = (7, 18, 64)


def _commitment_data(value):
    if isinstance(value, dict):
        for variant in ('Raw32', 'BigRaw'):
            raw = value.get(variant)
            if isinstance(raw, str) and raw.startswith('0x'):
                return variant, bytes.fromhex(raw[2:])
        for item in value.values():
            found = _commitment_data(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _commitment_data(item)
            if found is not None:
                return found
    return None


def _availability_for_requested_subnet(self, data):
    from noah.reports.backing.scale import Reader, encode_compact, encode_u16
    from noah.reports.backing.scale import Availability
    reader = Reader(data)
    coldkeys = [reader.account() for _ in range(reader.compact())]
    assert reader.take(1) == b'\x01'
    count = reader.compact()
    netuid = reader.u16()
    assert count == 1
    out = bytearray(encode_compact(len(coldkeys)))
    for cold in coldkeys:
        row = self.availability.get(cold, Availability(0, 0, 0))
        out += cold
        out += encode_compact(1)
        out += encode_u16(netuid)
        out += encode_compact(row.total) + encode_compact(row.locked) + encode_compact(row.available)
    return bytes(out)


class Chain(RawChain):
    def __init__(self, miner, owner, extras=()):
        self.reveal_time = None
        self.clock_ms = None
        self.adjudication_time = None
        self.commitment_prefix = ''
        self.commitment_scale = None
        self.nonces = {}
        self.success_indexes = {}
        super().__init__()
        self.commitment_prefix = self._commitment_prefix()
        self.commitment_scale = None
        self._install_miner(miner, owner, extras)
        self._install_quota(miner)
        # The mining subnet is registered but stable; only TARGETS are dynamic.
        from noah.reports.backing.keys import subnet_mechanism_key
        self.engine.storage[hex_encode(subnet_mechanism_key(82))] = '0x0000'
        holder = bytes.fromhex('88' * 32)
        from noah.reports.tests.fakes import alpha_storage_key
        for netuid in TARGETS:
            self.engine.set_pool(netuid, alpha_in=50000000000000, tao=50000000000000,
                                 alpha_out=2000000000000, quote=PERQUINTILL_ONE // 2)
            self.engine.alpha_keys.append(hex_encode(alpha_storage_key(b'\x99' * 32, holder, netuid)))
        self.engine._availability_bytes = _availability_for_requested_subnet.__get__(self.engine, type(self.engine))
        from noah.reports.backing.keys import total_networks_key
        self.storage[hex_encode(total_networks_key())] = hex_encode((64).to_bytes(2, 'little'))
        original = block_hash(0)
        self.headers['0x' + LOCAL_GENESIS] = self.headers[original]
        self.blocks['0x' + LOCAL_GENESIS] = self.blocks[original]
        reveal_hash = block_hash(7)
        self.headers[reveal_hash] = {'number': hex(7), 'parentHash': block_hash(6), 'stateRoot': '0x' + '00' * 32,
                                     'extrinsicsRoot': '0x' + '00' * 32, 'digest': {'logs': []}}
        self.blocks[reveal_hash] = {'block': {'header': self.headers[reveal_hash], 'extrinsics': []}, 'justifications': None}

    def _quota_storage(self, module, name, params, value):
        key = self.sdk.create_storage_key(module, name, params)
        encoded = self.sdk.runtime_config.create_scale_object(key.value_scale_type, metadata=self.sdk.metadata)
        self.storage[key.to_hex()] = '0x' + bytes(encoded.encode(value).data).hex()

    def _install_quota(self, miner):
        self.quota_hotkey = Keypair(public_key=miner, crypto_type=1, ss58_format=42).ss58_address
        self.quota_used = 0
        self._quota_storage('SubtensorModule', 'SubnetEpochIndex', [82], 42)
        self._quota_storage('Commitments', 'MaxSpace', [], 1000)
        self._quota_storage('Commitments', 'UsedSpaceOf', [82, self.quota_hotkey],
                            {'last_epoch': 42, 'used_space': 0})

    def _commitment_prefix(self):
        one = self.sdk.create_storage_key('Commitments', 'CommitmentOf', [82, '5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM'])
        two = self.sdk.create_storage_key('Commitments', 'CommitmentOf', [82, '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'])
        first, second = one.to_hex(), two.to_hex()
        size = 0
        while size < len(first) and first[size] == second[size]:
            size += 1
        return first[:size]

    def _install_miner(self, miner, owner, extras=()):
        stale = [key for key in self.storage if key.startswith('0x' + _prefix('Keys', 82).hex())
                 or key.startswith('0x' + _prefix('Uids', 82).hex())
                 or key.startswith('0x' + _prefix('Owner').hex())
                 or key.startswith('0x' + _prefix('BlockAtRegistration', 82).hex())]
        for key in stale:
            self.storage.pop(key, None)
        members = [(miner, owner), *extras]
        for uid, (hot, cold) in enumerate(members):
            self.storage[hex_encode(_prefix('Keys', 82) + uid.to_bytes(2, 'little'))] = hex_encode(hot)
            self.storage[hex_encode(_prefix('Uids', 82) + blake2_128_concat(hot))] = hex_encode(uid.to_bytes(2, 'little'))
            self.storage[hex_encode(_prefix('Owner') + blake2_128_concat(hot))] = hex_encode(cold)
            self.storage[hex_encode(_prefix('BlockAtRegistration', 82) + uid.to_bytes(2, 'little'))] = hex_encode((0).to_bytes(8, 'little'))
        self.storage[hex_encode(_prefix('SubnetworkN', 82))] = hex_encode(len(members).to_bytes(2, 'little'))

    def open_adjudication(self, timestamp):
        pin = block_hash(8)
        self.headers[pin] = {'number': hex(8), 'parentHash': block_hash(7), 'stateRoot': '0x' + '00' * 32,
                             'extrinsicsRoot': '0x' + '00' * 32, 'digest': {'logs': []}}
        self.blocks[pin] = {'block': {'header': self.headers[pin], 'extrinsics': []}, 'justifications': None}
        self.adjudication_time = timestamp
        call = self.sdk.compose_call('Timestamp', 'set', {'now': timestamp})
        self.blocks[pin]['block']['extrinsics'].append(str(self.sdk.create_unsigned_extrinsic(call).data))
        event_type = self.sdk.create_storage_key('System', 'Events', []).value_scale_type
        encoded = self.sdk.runtime_config.create_scale_object(event_type, metadata=self.sdk.metadata)
        self.events[self.event_key][pin] = '0x' + bytes(encoded.encode([]).data).hex()

    def call(self, method, params):
        if method == 'chain_getFinalizedHead' and self.adjudication_time is not None:
            return block_hash(8)
        if method == 'chain_getFinalizedHead' and self.reveal_time is not None:
            return block_hash(7)
        if method == 'state_getStorageHash' and params[0] == '0x3a636f6465':
            assert params[1] in self.headers
            # Synthetic observed code identity; supported490 qualification is unchanged.
            return '0x' + 'ab' * 32
        if method == 'state_getKeysPaged':
            prefix, count, start, _pin = params
            keys = sorted(set(self.storage) | set(self.engine.storage) | set(self.engine.alpha_keys))
            return [key for key in keys if key.startswith(prefix) and (start is None or key > start)][:count]
        if method == 'author_unwatchExtrinsic':
            return None
        if method == 'system_accountNextIndex':
            account = params[0]
            nonce = self.nonces.get(account, 0)
            self.nonces[account] = nonce + 1
            return nonce
        if method == 'chain_getBlockHash':
            value = params[0] if params else 6
            number = int(value, 16) if isinstance(value, str) else int(value)
            if number == 0:
                return '0x' + LOCAL_GENESIS
        prefix = getattr(self, 'commitment_prefix', '')
        if prefix and method in ('state_getStorage', 'state_getStorageAt') and params and str(params[0]).startswith(prefix):
            return self.commitment_scale
        if method in ('state_getStorage', 'state_getStorageAt') and params and params[0] == hex_encode(timestamp_now_key()):
            pin = params[1] if len(params) > 1 else None
            if pin in self.headers and int(self.headers[pin]['number'], 16) == 0:
                return hex_encode((T0 - 12000).to_bytes(8, 'little'))
            if self.adjudication_time is not None and (pin is None or pin == block_hash(8)):
                return hex_encode(self.adjudication_time.to_bytes(8, 'little'))
            if self.reveal_time is not None and (pin is None or pin == block_hash(7)):
                return hex_encode(self.reveal_time.to_bytes(8, 'little'))
            if self.clock_ms is not None:
                return hex_encode(self.clock_ms.to_bytes(8, 'little'))
        if method == 'author_submitAndWatchExtrinsic':
            return self.include(params[0])
        return super().call(method, params)

    def include(self, extrinsic):
        if self.reveal_time is not None:
            self._ensure_reveal_timestamp()
        head = block_hash(7) if self.reveal_time is not None else block_hash(6)
        text = extrinsic if str(extrinsic).startswith('0x') else '0x' + extrinsic
        body = self.blocks[head]['block']['extrinsics']
        body.append(text)
        index = len(body) - 1
        self._remember_commitment(text)
        self.success_indexes.setdefault(head, []).append(index)
        event_type = self.sdk.create_storage_key('System', 'Events', []).value_scale_type
        decoded = [{'phase': {'ApplyExtrinsic': item}, 'event': {'System': {'ExtrinsicSuccess': {
            'dispatch_info': {'weight': {'ref_time': 0, 'proof_size': 0}, 'class': 'Normal', 'pays_fee': 'Yes'}}}},
            'topics': []} for item in self.success_indexes.get(head, [])]
        encoded = self.sdk.runtime_config.create_scale_object(event_type, metadata=self.sdk.metadata)
        blob = encoded.encode(decoded)
        self.events[self.event_key][head] = '0x' + bytes(blob.data).hex()
        return head

    def _ensure_reveal_timestamp(self):
        body = self.blocks[block_hash(7)]['block']['extrinsics']
        if body:
            return
        call = self.sdk.compose_call('Timestamp', 'set', {'now': self.reveal_time})
        body.append(str(self.sdk.create_unsigned_extrinsic(call).data))

    def _remember_commitment(self, extrinsic):
        try:
            obj = self.sdk.runtime_config.create_scale_object('Extrinsic', metadata=self.sdk.metadata,
                                                              data=ScaleBytes(bytes.fromhex(extrinsic[2:])))
            obj.decode()
            call = obj.value['call']
            if call.get('call_function') != 'set_commitment':
                return
            stored = _commitment_data(call)
            if stored is None:
                self.commitment_scale = None
                return
            value_type = self.sdk.create_storage_key('Commitments', 'CommitmentOf',
                [82, '5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM']).value_scale_type
            registration = self.sdk.runtime_config.create_scale_object(value_type, metadata=self.sdk.metadata)
            encoded = registration.encode({'deposit': 0, 'block': 6, 'info': commitment_info(*stored)})
            self.commitment_scale = '0x' + bytes(encoded.data).hex()
            self.quota_used += max(100, len(stored[1]))
            self._quota_storage('Commitments', 'UsedSpaceOf', [82, self.quota_hotkey],
                                {'last_epoch': 42, 'used_space': self.quota_used})
        except Exception:
            self.commitment_scale = None


class Socket:
    def __init__(self, chain):
        self.chain = chain

    def handler(self, websocket):
        for raw in websocket:
            message = json.loads(raw)
            method = message.get('method')
            if method == 'author_submitAndWatchExtrinsic':
                block = self.chain.include(message['params'][0])
                websocket.send(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': 'local-sub'}))
                websocket.send(json.dumps({'jsonrpc': '2.0', 'method': 'author_extrinsicUpdate',
                                           'params': {'subscription': 'local-sub', 'result': {'inBlock': block}}}))
            else:
                try:
                    result = self.chain.call(method, message.get('params') or [])
                except Exception as exc:
                    websocket.send(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                               'error': {'code': -32000, 'message': str(exc)}}))
                else:
                    websocket.send(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}))


def _serve(chain):
    server = serve(Socket(chain).handler, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    return server, f'ws://127.0.0.1:{port}'


def _http(directory):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = directory / self.path.lstrip('/')
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}'


def _run(args):
    completed = subprocess.run([str(PYTHON), '-I', '-B', '-m', 'noah.reports.cli', *args], cwd=ROOT,
                               text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


def _assignment_key(template_path):
    path = template_path.parent / 'assignment.seed'
    if not path.exists():
        path.write_bytes(b'A' * 32)
        path.chmod(0o600)
    return path


def _schedule(template_path, endpoint, chain, state_root):
    """Normal validator tick with separate assignment custody and actual RPC clock."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from noah.core.report_runtime import publish_weekly
    def timestamp(height):
        raw = chain.call('state_getStorage', [hex_encode(timestamp_now_key()), block_hash(height)])
        return int.from_bytes(bytes.fromhex(raw[2:]), 'little')
    runner = SimpleNamespace(
        endpoints={'reader_a': endpoint}, genesis_hash=lambda: '0x' + LOCAL_GENESIS,
        head_block=lambda: int(chain.call('chain_getHeader', [chain.call('chain_getFinalizedHead', [])])['number'], 16),
        block_hash=lambda height: chain.call('chain_getBlockHash', [height]),
        block_timestamp_ms=timestamp,
    )
    adapter = SimpleNamespace(
        weekly_assignments=True, report_gate_enabled=True, report_reward='bonus-20', netuid=82,
        day_anchor_ts=T0 // 1000,
        report_gate_policy_path=str(template_path), _submitted_record_dir=str(state_root),
        weekly_assignment_signing_key_file=str(_assignment_key(template_path)), runner_chain=runner,
    )
    with patch.dict(os.environ, {'NOAH_REPORT_PYTHON': str(PYTHON)}):
        result = publish_weekly(adapter)
    assert result is not None, 'normal weekly scheduler refused synthetic fixture'
    return result


def test_three_reports_from_one_hotkey_stay_distinct(tmp_path):
    wallets = tmp_path / 'wallets'
    miner = Wallet(name='miner', hotkey='default', path=str(wallets))
    miner.create_hotkey_from_uri('//weekly-miner', use_password=False, overwrite=True)
    publisher = Wallet(name='validator', hotkey='default', path=str(wallets))
    publisher.create_hotkey_from_uri('//weekly-validator', use_password=False, overwrite=True)
    miner_key = miner.hotkey.public_key
    owner = Keypair.create_from_uri('//weekly-owner', crypto_type=1, ss58_format=42).public_key
    chain = Chain(miner_key, owner)
    rpc, _ = _serve(chain)
    endpoint = f'ws://127.0.0.1:{rpc.socket.getsockname()[1]}'
    http = None
    try:
        template = {
            'schema': 'noah.weekly-assignment-template/2',
            'assignment_signer_public_key': sr25519.pair_from_seed(b'A' * 32)[0].hex(),
            'commitment_format': 'context-v1', 'genesis_hash': LOCAL_GENESIS,
            'mining_netuid': 82, 'day_anchor_ts': T0 // 1000, 'effective_day': 0,
            'snapshot_offset_seconds': 24, 'prepare_lead_seconds': 1,
            'commit_seconds': 7200, 'reveal_seconds': 7200,
            'eligibility_policy_version': REPORT_POLICY, 'calculation_version': CALCULATION,
            'scoring_release': 'weekly-local', 'ark_coldkey': '77' * 32,
            'ark_validator_hotkey': publisher.hotkey.public_key.hex(),
        }
        template_path = tmp_path / 'template.json'
        template_path.write_text(json.dumps(template))
        state_root = tmp_path / 'validator-state'
        published = _schedule(template_path, endpoint, chain, state_root)
        assert published['schema'] == 'noah.weekly-assignments/2'
        assert published['week_start'] == '2026-11-15'
        assert published['targets'] == list(TARGETS)
        assert len(published['assignments']) == 1
        out = state_root / 'report-state' / 'weekly-assignments'
        http, url = _http(out)
        state = tmp_path / 'state'
        ini = tmp_path / 'miner.ini'
        ini.write_text(f"""
[miner]
network = local
endpoint = {endpoint}
netuid = 82
miner_hotkey = {miner.hotkey.ss58_address}
coldkey = {Keypair(public_key=owner, crypto_type=1, ss58_format=42).ss58_address}
wallet_name = miner
hotkey_name = default
wallet_path = {wallets}
genesis_hash = 0x{LOCAL_GENESIS}
stake_position = miner
[ark]
ark_coldkey = {Keypair(public_key=bytes.fromhex('77'*32), crypto_type=1, ss58_format=42).ss58_address}
designated_validator_hotkey = {publisher.hotkey.ss58_address}
[deposit]
mode = fixed
amount_noah = 1
[evidence]
file = {tmp_path / 'evidence.json'}
[state]
dir = {state}
[report]
assignments_url = {url}/
assignment_signer_public_key = {template['assignment_signer_public_key']}
""")
        seen = []
        for _ in range(3):
            seen.append(_run(['miner', '--config', str(ini), 'assignments', '--yes']))
        assert sorted(row.get('reported_netuid') for row in seen) == sorted(TARGETS)
        assert all(row.get('commit') == 'finalized' for row in seen)
        chain.reveal_time = T0 + 25000 + 7_200_000 + 1000
        revealed = []
        for _ in range(3):
            revealed.append(_run(['miner', '--config', str(ini), 'assignments', '--yes']))
        assert [row.get('action') for row in revealed] == ['reveal', 'reveal', 'reveal'], revealed
        assert all(row.get('reveal') == 'finalized' for row in revealed)
        hotkeys = tmp_path / 'hotkeys.json'
        hotkeys.write_text(json.dumps([miner.hotkey.ss58_address]))
        netuids = set()
        for path in (state / 'weekly-assignments').glob('*/day-*.json'):
            resolution = json.loads(path.read_text())
            if 'round' not in resolution:
                continue
            round_path = tmp_path / f"round-{resolution['round']['reported_netuid']}.json"
            round_path.write_text(json.dumps(resolution['round']))
            collected = tmp_path / f"collect-{resolution['round']['reported_netuid']}"
            _run(['validator', 'collect', '--round', str(round_path), '--endpoint', endpoint,
                  '--hotkeys', str(hotkeys), '--out', str(collected)])
            evidence = json.loads(next(collected.rglob('evidence.json')).read_text())
            row = next(iter(evidence['evidence'].values()))
            from noah.reports.backing.protocol.preimage import decode
            revealed_payload = decode(bytes.fromhex(row['reveal']['data_hex']))
            assert revealed_payload.netuid == resolution['round']['reported_netuid']
            netuids.add(revealed_payload.netuid)
        assert netuids == set(TARGETS)
        assert chain.quota_used == 909  # Quota units: 3 * (max(100, 79) + 203); 846 payload bytes.
    finally:
        rpc.shutdown()
        if http is not None:
            http.shutdown()


def test_scheduler_catches_up_after_sunday_and_restart_keeps_the_file(tmp_path):
    wallets = tmp_path / 'wallets'
    miner = Wallet(name='miner', hotkey='default', path=str(wallets))
    miner.create_hotkey_from_uri('//weekly-miner', use_password=False, overwrite=True)
    publisher = Wallet(name='validator', hotkey='default', path=str(wallets))
    publisher.create_hotkey_from_uri('//weekly-validator', use_password=False, overwrite=True)
    owner = Keypair.create_from_uri('//weekly-owner', crypto_type=1, ss58_format=42).public_key
    chain = Chain(miner.hotkey.public_key, owner)
    chain.clock_ms = T0 + 3 * 86400 * 1000
    rpc, endpoint = _serve(chain)
    try:
        template = {
            'schema': 'noah.weekly-assignment-template/2',
            'assignment_signer_public_key': sr25519.pair_from_seed(b'A' * 32)[0].hex(),
            'commitment_format': 'context-v1', 'genesis_hash': LOCAL_GENESIS,
            'mining_netuid': 82, 'day_anchor_ts': T0 // 1000, 'effective_day': 0,
            'snapshot_offset_seconds': 24, 'prepare_lead_seconds': 1,
            'commit_seconds': 7200, 'reveal_seconds': 7200,
            'eligibility_policy_version': REPORT_POLICY, 'calculation_version': CALCULATION,
            'scoring_release': 'weekly-local', 'ark_coldkey': '77' * 32,
            'ark_validator_hotkey': publisher.hotkey.public_key.hex(),
        }
        template_path = tmp_path / 'template.json'
        template_path.write_text(json.dumps(template))
        state_root = tmp_path / 'validator-state'
        first = _schedule(template_path, endpoint, chain, state_root)
        assert first['schema'] == 'noah.weekly-assignments/2'
        assert first['week_start'] == '2026-11-15'
        path = state_root / 'report-state/weekly-assignments/2026-11-15.json'
        body = path.read_bytes()
        second = _schedule(template_path, endpoint, chain, state_root)
        assert second == first
        assert path.read_bytes() == body
    finally:
        rpc.shutdown()


def _bonus_case(tmp_path, commits, reveals, *, omit_reveal_netuid=None):
    """Scheduler, miner URL, collection, adjudication, and the V2 bonus function."""
    from noah.core.report_publication import derive_vector
    from noah.reports.assignments import earns_reporting_bonus
    from noah.reports.common import digest
    from noah.reports.gate import eval_day_for_close
    from noah.scoring.reporting_bonus import bonus_pay
    wallets = tmp_path / 'wallets'
    miner = Wallet(name='miner', hotkey='default', path=str(wallets))
    miner.create_hotkey_from_uri('//weekly-miner', use_password=False, overwrite=True)
    publisher = Wallet(name='validator', hotkey='default', path=str(wallets))
    publisher.create_hotkey_from_uri('//weekly-validator', use_password=False, overwrite=True)
    owner = Keypair.create_from_uri('//weekly-owner', crypto_type=1, ss58_format=42)
    quiet_hot = Keypair.create_from_uri('//weekly-quiet', crypto_type=1, ss58_format=42)
    quiet_cold = Keypair.create_from_uri('//weekly-quiet-owner', crypto_type=1, ss58_format=42)
    chain = Chain(miner.hotkey.public_key, owner.public_key, extras=[(quiet_hot.public_key, quiet_cold.public_key)])
    rpc, endpoint = _serve(chain)
    other, other_endpoint = _serve(chain)
    http = None
    try:
        template = {
            'schema': 'noah.weekly-assignment-template/2',
            'assignment_signer_public_key': sr25519.pair_from_seed(b'A' * 32)[0].hex(),
            'commitment_format': 'context-v1', 'genesis_hash': LOCAL_GENESIS,
            'mining_netuid': 82, 'day_anchor_ts': T0 // 1000, 'effective_day': 0,
            'snapshot_offset_seconds': 24, 'prepare_lead_seconds': 1,
            'commit_seconds': 7200, 'reveal_seconds': 7200,
            'eligibility_policy_version': REPORT_POLICY, 'calculation_version': CALCULATION,
            'scoring_release': 'weekly-local', 'ark_coldkey': '77' * 32,
            'ark_validator_hotkey': publisher.hotkey.public_key.hex(),
        }
        template_path = tmp_path / 'template.json'
        template_path.write_text(json.dumps(template))
        state_root = tmp_path / 'validator-state'
        published = _schedule(template_path, endpoint, chain, state_root)
        assert published['schema'] == 'noah.weekly-assignments/2' and len(published['assignments']) == 2
        out = state_root / 'report-state' / 'weekly-assignments'
        http, url = _http(out)
        miner_state = tmp_path / 'miner-state'
        ini = tmp_path / 'miner.ini'
        ini.write_text(f"""
[miner]
network = local
endpoint = {endpoint}
netuid = 82
miner_hotkey = {miner.hotkey.ss58_address}
coldkey = {owner.ss58_address}
wallet_name = miner
hotkey_name = default
wallet_path = {wallets}
genesis_hash = 0x{LOCAL_GENESIS}
stake_position = miner
[ark]
ark_coldkey = {Keypair(public_key=bytes.fromhex('77'*32), crypto_type=1, ss58_format=42).ss58_address}
designated_validator_hotkey = {publisher.hotkey.ss58_address}
[deposit]
mode = fixed
amount_noah = 1
[evidence]
file = {tmp_path / 'evidence.json'}
[state]
dir = {miner_state}
[report]
assignments_url = {url}/
assignment_signer_public_key = {template['assignment_signer_public_key']}
""")
        committed = [_run(['miner', '--config', str(ini), 'assignments', '--yes']) for _ in range(commits)]
        assert len({row.get('reported_netuid') for row in committed}) == commits
        chain.reveal_time = T0 + 25000 + 7_200_000 + 1000
        if omit_reveal_netuid is None:
            revealed = [_run(['miner', '--config', str(ini), 'assignments', '--yes']) for _ in range(reveals)]
        else:
            assert commits == 3 and reveals == 2
            contracts = [json.loads(path.read_text())['round']
                         for path in (miner_state / 'weekly-assignments').glob('*/day-*.json')]
            assert {row['reported_netuid'] for row in contracts} == set(TARGETS)
            revealed = []
            for contract in sorted(contracts, key=lambda row: row['reported_netuid']):
                if contract['reported_netuid'] == omit_reveal_netuid:
                    continue
                path = tmp_path / f"submit-round-{contract['reported_netuid']}.json"
                path.write_text(json.dumps(contract))
                revealed.append(_run(['miner', '--config', str(ini), 'submit', '--round', str(path), '--yes']))
        assert [row.get('action') for row in revealed] == ['reveal'] * reveals
        hotkeys = tmp_path / 'hotkeys.json'
        hotkeys.write_text(json.dumps([miner.hotkey.ss58_address]))
        collected_netuids = set()
        rounds = []
        for path in sorted((miner_state / 'weekly-assignments').glob('*/day-*.json')):
            resolution = json.loads(path.read_text())
            if 'round' not in resolution:
                continue
            contract = resolution['round']
            rounds.append(contract)
            round_path = tmp_path / f"round-{contract['reported_netuid']}.json"
            round_path.write_text(json.dumps(contract))
            collected = tmp_path / f"collect-{contract['reported_netuid']}"
            _run(['validator', 'collect', '--round', str(round_path), '--endpoint', endpoint,
                  '--hotkeys', str(hotkeys), '--out', str(collected)])
            evidence = json.loads(next(collected.rglob('evidence.json')).read_text())
            row = next(iter(evidence['evidence'].values()))
            data = (row.get('reveal') or {}).get('data_hex')
            if data:
                from noah.reports.backing.protocol.preimage import decode
                if decode(bytes.fromhex(data)).netuid == contract['reported_netuid']:
                    collected_netuids.add(contract['reported_netuid'])
        chain.open_adjudication(max(contract['reveal_close_ms'] for contract in rounds) + 1000)
        pin = block_hash(8)[2:]
        seats = []
        for uid, hot, cold in (
            (0, miner.hotkey.public_key, owner.public_key),
            (1, quiet_hot.public_key, quiet_cold.public_key),
        ):
            seats.append({'uid': uid, 'hotkey': hot.hex(), 'coldkey': cold.hex(),
                          'registration_block': 0, 'registration_day': 0,
                          'observed_at': {'block_number': 8, 'block_hash': pin}})
        day = eval_day_for_close(rounds[0]['reveal_close_ms'], template['day_anchor_ts'])
        policy = {'schema': 'noah.report-policy/1', 'effective_day': 0, 'genesis_hash': LOCAL_GENESIS,
                  'mining_netuid': 82, 'eligibility_policy_version': REPORT_POLICY, 'required_rounds': rounds}
        week = json.loads((out / '2026-11-15.json').read_text())
        request = {
            'schema': 'noah.report-adjudication-request/1', 'policy': policy, 'policy_sha256': digest(policy),
            'genesis_hash': LOCAL_GENESIS, 'mining_netuid': 82, 'eval_day': day,
            'day_anchor_ts': template['day_anchor_ts'],
            'evaluation': {'block_number': 8, 'block_hash': pin, 'timestamp_ms': chain.adjudication_time},
            'calculation': {'reference': {'path': 'evidence/' + '1' * 64 + '.json', 'sha256': '1' * 64,
                                          'day': day, 'genesis_hash': LOCAL_GENESIS, 'candidate_sha256': None},
                            'fixture_sha256': '2' * 64},
            'scored_seats': seats, 'assignments': week['assignments'],
        }
        invocation = tmp_path / 'invocation.json'
        invocation.write_text(json.dumps({
            'schema': 'noah.report-adjudication-invocation/1', 'request': request,
            'sources': [{'id': 'reader_a', 'endpoint': endpoint, 'history': None},
                        {'id': 'reader_b', 'endpoint': other_endpoint, 'history': None}],
        }))
        result = _run(['validator', 'adjudicate', '--invocation', str(invocation),
                       '--output', str(tmp_path / 'package')])
        ordinary = 100 * 10**9
        score = {'schema': 'noah-score-output/6', 'evalDay': str(day), 'miners': [
            {'uid': uid, 'coldkey': cold.ss58_address, 'payWeightFp': str(ordinary),
             'scoreFp': str(100 * 10**9), 'multiplierFp': str(10**9), 'doorOpen': True,
             'state': {'maturity': '1000000', 'missed': '0'}}
            for uid, cold in ((0, owner), (1, quiet_cold))
        ]}
        hotkeys = {str(uid): pair.ss58_address for uid, pair in ((0, miner.hotkey), (1, quiet_hot))}
        submission = {'live_uids': [0, 1], 'owner_uids': [], 'fixture_hotkeys': hotkeys, 'chain_hotkeys': dict(hotkeys)}
        vector = derive_vector(score, request, result, result['manifest_sha256'], submission, reward='bonus-20')
        by_sha = {digest(contract): contract['reported_netuid'] for contract in rounds}
        decisions = []
        for decision in result['scored_decisions']:
            decisions.append({
                'uid': decision['uid'], 'outcome': decision['outcome'], 'effect': decision['effect'],
                'bonus': earns_reporting_bonus(decision, request),
                'rounds': [{'netuid': by_sha[row['round_sha256']], 'outcome': row['outcome'],
                            'status': row['status'], 'reason': row['reason']} for row in decision['rounds']],
            })
        pays = {row['uid']: bonus_pay(row, row['uid'] in {item['uid'] for item in decisions if item['bonus']})
                for row in score['miners']}
        return {'quota_used': chain.quota_used, 'collected_netuids': sorted(collected_netuids), 'adjudication_status': result['status'],
                'decisions': decisions, 'ordinary_pay': ordinary, 'pays': pays, 'vector': vector}
    finally:
        rpc.shutdown()
        other.shutdown()
        if http is not None:
            http.shutdown()


def test_three_verified_reports_earn_one_bonus(tmp_path):
    report = _bonus_case(tmp_path, 3, 3)
    print('ALL_THREE ' + json.dumps(report))
    reporter = next(row for row in report['decisions'] if row['uid'] == 0)
    quiet = next(row for row in report['decisions'] if row['uid'] == 1)
    assert report['quota_used'] == 909, report
    assert report['collected_netuids'] == sorted(TARGETS), report
    assert sorted(row['netuid'] for row in reporter['rounds']) == sorted(TARGETS), report
    assert all(row['outcome'] == 'verified' for row in reporter['rounds']), report
    assert reporter['bonus'] is True and quiet['bonus'] is False, report
    assert report['pays'][0] == report['ordinary_pay'] + 20 * 10**9, report
    assert report['pays'][1] == report['ordinary_pay'], report
    assert report['vector'][0] > report['vector'][1], report


def test_two_verified_reports_and_one_missing_earn_no_bonus(tmp_path):
    report = _bonus_case(tmp_path, 2, 2)
    print('TWO_PLUS_MISSING ' + json.dumps(report))
    reporter = next(row for row in report['decisions'] if row['uid'] == 0)
    verified = [row for row in reporter['rounds'] if row['outcome'] == 'verified']
    other = [row for row in reporter['rounds'] if row['outcome'] != 'verified']
    assert len(verified) == 2 and len(other) == 1, report
    assert other[0]['status'] == 'MISSING', report
    assert reporter['bonus'] is False, report
    assert report['pays'][0] == report['ordinary_pay'] == report['pays'][1], report
    assert set(report['vector']) == {0, 1}, report


@pytest.mark.parametrize('omitted', [TARGETS[0], TARGETS[2]])
def test_all_three_commits_one_missing_reveal_preserves_verified_siblings(tmp_path, omitted):
    report = _bonus_case(tmp_path, 3, 2, omit_reveal_netuid=omitted)
    assert report['quota_used'] == 706, report
    reporter = next(row for row in report['decisions'] if row['uid'] == 0)
    verified = [row for row in reporter['rounds'] if row['outcome'] == 'verified']
    missing = [row for row in reporter['rounds'] if row['outcome'] != 'verified']
    assert {row['netuid'] for row in verified} == set(TARGETS) - {omitted}, report
    assert len(missing) == 1 and missing[0]['netuid'] == omitted, report
    assert missing[0]['status'] == 'MISSING' and missing[0]['reason'] == 'reveal', report
    assert reporter['bonus'] is False, report
    assert report['pays'][0] == report['ordinary_pay'] == report['pays'][1], report
    # Unchanged largest-remainder normalization gives UID0 the one odd unit.
    assert report['vector'] == {0: 32768, 1: 32767}, report
