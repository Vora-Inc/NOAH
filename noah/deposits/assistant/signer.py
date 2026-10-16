#!/usr/bin/env python3
"""Host-operated policy signer. Exposes typed miner actions over a per-miner Unix socket.

No keys are imported by preview. The journal directory is shared by
ALL signer workers, including workers serving the same delegate or owner.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import sqlite3
import time

from noah.chain.sources import Refusal
from noah.deposits.assistant.proxy_signing import (
    is_proxy, preflight, proxy_executed, reject, same, secret_backend, signing, uint, unwrap,
)

ERA = 64


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def hash_text(value):
    if isinstance(value, bytes):
        return '0x' + value.hex()
    value = str(value)
    if not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        reject('chain did not provide a usable transaction hash')
    return value.lower()


class Journal:
    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path / 'signer.sqlite', isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    @contextlib.contextmanager
    def locked(self):
        # A shared lock serializes even different delegates spending the same owner
        # balance. It also prevents nonce races across different wallet containers.
        lock_path = self.path / 'execution.lock'
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), 'a') as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        reject('signer busy; no request queued for later execution')
                    time.sleep(0.02)
            yield

    def rows(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM intents')]

    def put(self, row):
        self.db.execute('INSERT OR REPLACE INTO intents VALUES (?, ?)',
                        (row['id'], json.dumps(row, sort_keys=True)))

    def incarnation(self):
        # Persist with the same FULL-synchronous SQLite durability as intents.
        # Recreating the journal creates a different identity, never proof that
        # an intent submitted under the old journal was absent from the chain.
        import uuid
        self.db.execute('CREATE TABLE IF NOT EXISTS journal_identity (id INTEGER PRIMARY KEY, token TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO journal_identity VALUES (1,?)',(uuid.uuid4().hex,))
        return self.db.execute('SELECT token FROM journal_identity WHERE id=1').fetchone()[0]

    def close(self):
        self.db.close()


def load_delegate(config, *, local_key_dir=None):
    """Only called after every public policy/preflight check, immediately before signing."""
    s = signing(config)
    ref = s['signer_ref']
    backend = secret_backend(config)
    if backend == 'bittensor-wallet':
        from bittensor import Wallet
        wallet = Wallet(name=ref.split(':', 1)[1],
                        path=config.wallet_path or os.path.expanduser('~/.bittensor/wallets'))
        if not same(wallet.coldkeypub.ss58_address, s['proxy_address']):
            reject('delegate wallet public address differs from proxy_address')
        key = wallet.coldkey  # Delegate wallet's own prompt, only at signing time.
        if not same(key.ss58_address, s['proxy_address']):
            reject('unlocked delegate public address differs from proxy_address')
        return key
    if backend == 'local-test':
        from noah.core.chain import MAINNET, TESTNET
        if config.network not in ('testnet', 'private-test') or config.genesis_hash in (MAINNET, TESTNET) or not local_key_dir:
            reject('local-test keys require explicit private-test configuration and key directory')
        path = Path(local_key_dir) / (ref.split(':', 1)[1] + '.json')
        if path.is_symlink() or path.stat().st_mode & 0o077:
            reject('local delegate file must be private and not a symlink')
        raw = path.read_text()
    else:
        reject('unsupported secret backend')
    from .strict_json import read_json_text
    secret = read_json_text(raw)
    if (not isinstance(secret, dict) or set(secret) != {'version', 'crypto_type', 'seed_hex'}
            or type(secret['version']) is not int or secret['version'] != 1 or secret['crypto_type'] != 'sr25519'
            or not isinstance(secret['seed_hex'], str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', secret['seed_hex'])):
        reject('invalid delegate secret format')
    from bittensor.sp_core import Keypair
    key = Keypair.create_from_seed(bytes.fromhex(secret['seed_hex'][2:]), crypto_type=1)
    if not same(key.ss58_address, s['proxy_address']):
        reject('delegate secret public address differs from proxy_address')
    return key


class Signer:
    def __init__(self, config, services, policy, journal, key_loader=load_delegate):
        self.config, self.services, self.policy = config, services, policy
        self.journal, self.key_loader = journal, key_loader
        if not is_proxy(config) or config.stake_position != 'miner' or same(signing(config).get('proxy_address'), config.coldkey):
            reject('signer requires an attributed miner-position proxy config')
        expected = {'version', 'policy_id', 'runtime_code_hash', 'spec_version',
                    'max_amount_rao', 'max_total_rao', 'max_fee_rao', 'conversion'}
        if set(policy) - {'purchase', 'max_total_tao_rao'} != expected or type(policy['version']) is not int or policy['version'] != 1:
            reject('invalid signer policy fields')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', policy['policy_id']):
            reject('invalid signer policy id')
        hash_text(policy['runtime_code_hash'])
        for field in ('spec_version', 'max_amount_rao', 'max_total_rao', 'max_fee_rao'):
            uint(policy[field], field, 1)
        from .conversion_config import validate
        self.conversion = validate(policy['conversion'])
        self.purchase = None
        if 'purchase' in policy or 'max_total_tao_rao' in policy:
            if not {'purchase','max_total_tao_rao'} <= policy.keys():
                reject('purchase signer requires settings and a lifetime TAO cap')
            from .purchase import from_mapping, test_gate
            self.purchase = from_mapping(policy['purchase'])
            test_gate(config,self.purchase)
            if not self.purchase.plan_id:
                reject('purchase signer requires an immutable plan_id')
            uint(policy['max_total_tao_rao'],'lifetime purchase TAO limit',1)

    def runtime(self, *, require_context=False):
        conn = self.services.connection
        if str(conn.get_block_hash(0)) != self.config.genesis_hash:
            reject('signer chain genesis differs from fixed policy')
        for head in (conn.get_chain_finalised_head(), conn.get_chain_head()):
            code_hash = conn.rpc_request('state_getStorageHash', ['0x3a636f6465', head])['result']
            version = conn.rpc_request('state_getRuntimeVersion', [head])['result']
            if code_hash != self.policy['runtime_code_hash'] or version['specVersion'] != self.policy['spec_version']:
                reject('runtime changed or unreviewed; review exact proxy call filters before signing')
        if require_context:
            context = getattr(conn, 'runtime', None)
            if (getattr(context, 'runtime_version', None) != version['specVersion']
                    or getattr(context, 'transaction_version', None) != version.get('transactionVersion')):
                reject('SDK signing runtime context differs from reviewed chain runtime')

    def call(self, request):
        from noah.deposits.assistant.session import _read_ceiling, _check_transaction_can_succeed
        from noah.deposits.assistant.assistant_tx import plan_deposit, compose, EARNINGS
        from noah.deposits.disclosures import acknowledge, REQUIRED_DISCLOSURES
        c, conn = self.config, self.services.connection
        from noah.core.chain import MAINNET
        from noah.core.mainnet import ARK_ADDRESS, ARK_VALIDATOR_HOTKEY, MAINNET_NETUID
        if c.genesis_hash == MAINNET and (c.netuid != MAINNET_NETUID or not same(c.ark_coldkey, ARK_ADDRESS) or not same(c.validator_hotkey, ARK_VALIDATOR_HOTKEY)):
            reject('mainnet signer requires the official Ark, validator and NOAH subnet')
        amount = uint(request['amount_rao'], 'amount_rao', 1)
        if amount > self.policy['max_amount_rao']:
            reject('amount exceeds signer transaction limit')
        ceiling = _read_ceiling(c, self.services)
        if amount + c.reserve_rao > ceiling.amount:
            reject('amount exceeds available stake after reserve')
        owner = unwrap(conn.query('SubtensorModule', 'Owner', [c.miner_hotkey]))
        if not same(owner, c.coldkey):
            reject('policy coldkey does not own miner hotkey')
        if request['action'] == 'deposit':
            _check_transaction_can_succeed(c, self.services)
            if c.cap_rao is not None and amount > c.cap_rao:
                reject('amount exceeds configured deposit cap')
            if c.mode in ('custom', 'fixed') and amount != c.amount_rao:
                reject('amount differs from fixed deposit policy')
            if c.mode not in ('custom', 'fixed'):
                reject('signer supports fixed contribution amounts only')
            plan = plan_deposit(EARNINGS, ark_coldkey=c.ark_coldkey, miner_hotkey=c.miner_hotkey,
                validator_hotkey=c.validator_hotkey, netuid=c.netuid, amount_rao=amount,
                ceiling=ceiling, acknowledgment=acknowledge(REQUIRED_DISCLOSURES))
            return compose(conn, plan)
        s = self.conversion
        if request['destination_netuid'] != s['destination_netuid'] or not same(request['destination_hotkey'], s['destination_hotkey']):
            reject('requested conversion destination differs from trusted policy')
        if not s['enabled'] or not same(s['destination_hotkey'], c.miner_hotkey):
            reject('conversion policy is disabled or changes hotkey')
        if s['destination_netuid'] == c.netuid:
            reject('conversion must change subnet')
        from .conversion_config import conversion_amount
        if conversion_amount(c, s, ceiling.amount) != amount:
            reject('conversion amount differs from trusted policy')
        head = conn.get_block_number(conn.get_chain_head())
        age = head - uint(request['quote_block'], 'quote_block')
        if not 0 <= age <= s['quote_max_age_blocks']:
            reject('conversion quote is expired')
        prices = []
        for netuid in (c.netuid, s['destination_netuid']):
            if unwrap(conn.query('SubtensorModule', 'NetworksAdded', [netuid])) is not True or self.services.source.subtoken_enabled(netuid) is not True:
                reject('conversion subnet is absent or disabled')
            prices.append(uint(unwrap(conn.runtime_call('SwapRuntimeApi', 'current_alpha_price', [netuid])), 'price', 1))
        ratio = prices[0] * 1_000_000_000 // prices[1]
        minimum_limit = (ratio * (10000 - s['max_slippage_bp']) + 9999) // 10000
        limit = uint(request['limit_price'], 'limit_price', 1)
        if limit < minimum_limit:
            reject('conversion price limit violates trusted slippage policy')
        return conn.compose_call(call_module='SubtensorModule', call_function='swap_stake_limit',
            call_params=dict(hotkey=c.miner_hotkey, origin_netuid=c.netuid,
                destination_netuid=s['destination_netuid'], alpha_amount=amount,
                limit_price=limit, allow_partial=False))

    def execute(self, request):
        if isinstance(request,dict) and request.get('action') in ('purchase','purchase-noah','purchase-foreign'):
            from .purchase_signer import execute
            return execute(self,request)
        if self.purchase is not None:
            reject('fixed purchase policy accepts only its bound purchase and delivery actions')
        required = {'version', 'action', 'intent_id', 'amount_rao', 'identity', 'operation', 'expires_at'}
        if not isinstance(request, dict):
            reject('request must be an object')
        if request.get('action') == 'convert':
            required |= {'limit_price', 'quote_block', 'destination_netuid', 'destination_hotkey'}
        if set(request) != required or type(request['version']) is not int or request['version'] != 1 or request['action'] not in ('deposit', 'convert') or request.get('operation') not in ('check', 'submit'):
            reject('only typed deposit/conversion requests are supported')
        if not isinstance(request['intent_id'], str) or not re.fullmatch(r'[a-zA-Z0-9:_-]{1,128}', request['intent_id']):
            reject('invalid intent id')
        expires = uint(request['expires_at'], 'expires_at')
        if not time.time() < expires <= time.time() + 180:
            reject('request expired or expiry exceeds three minutes')
        request = {k: v for k, v in request.items() if k != 'expires_at'}
        c, conn, j = self.config, self.services.connection, self.journal
        from noah.deposits.assistant.proxy_signing import request_identity
        if request['identity'] != request_identity(c):
            reject('request identity differs from trusted signer policy')
        s = signing(c)
        identity = [c.genesis_hash, c.coldkey, c.miner_hotkey, self.policy['policy_id']]
        ident = digest([identity, request['intent_id']])
        with j.locked():
            rows = j.rows()
            old = next((r for r in rows if r['id'] == ident), None)
            if old and request['operation'] == 'submit':
                if old['request'] != request:
                    reject('intent id already binds different request')
                return self.reconcile(old)
            if any(r['genesis'] == c.genesis_hash and
                   (same(r['delegate'], s['proxy_address']) or same(r['owner'], c.coldkey)) and
                   r['status'] in ('not_submitted', 'prepared', 'uncertain') for r in rows):
                reject('delegate or owner has an unresolved transaction; reconcile it first')
            # Lifetime cumulative limit; changing labels or policy id cannot reset it.
            spent = sum(r['request']['amount_rao'] for r in rows if
                r['genesis'] == c.genesis_hash and same(r['owner'], c.coldkey) and
                same(r['miner'], c.miner_hotkey) and r['netuid'] == c.netuid and
                r['status'] in ('prepared', 'uncertain', 'finalized') and
                r['request']['action'] not in ('purchase','purchase-foreign'))
            if spent + uint(request['amount_rao'], 'amount', 1) > self.policy['max_total_rao']:
                reject('miner cumulative spending limit exceeded')
            self.runtime()
            inner = self.call(request)
            outer, facts = preflight(conn, c, inner, request['action'])
            if 2 * facts['fee_estimate_rao'] > self.policy['max_fee_rao']:
                reject('fee estimate exceeds signer fee budget')
            if time.time() >= expires:
                reject('request expired during public checks')
            if request['operation'] == 'check':
                return {'status': 'checked', 'runtime_permission_verified': True}
            row = dict(id=ident, request=request, genesis=c.genesis_hash, owner=c.coldkey,
                miner=c.miner_hotkey, netuid=c.netuid, delegate=s['proxy_address'],
                status='prepared', start_block=conn.get_block_number(conn.get_chain_finalised_head()))
            j.put(row)
            try:
                key = self.key_loader(c)
                if not same(key.ss58_address, s['proxy_address']):
                    reject('loaded delegate address mismatch')
            except (Exception, Refusal):
                row['status'] = 'refused'
                j.put(row)
                return self.result(row)
            try:
                if time.time() >= expires:
                    reject('request expired before signing')
                nonce = conn.get_account_next_index(s['proxy_address'])
                self.runtime()
                signer = self
                class GuardedKey:
                    def __getattr__(self, name):
                        return getattr(key, name)
                    def sign(self, payload):
                        if time.time() >= expires:
                            reject('request expired at signature boundary')
                        signer.runtime(require_context=True)
                        return key.sign(payload)
                from noah.deposits.deposit_submission import BytesConvertingSigner
                signed = conn.create_signed_extrinsic(call=outer, keypair=BytesConvertingSigner(GuardedKey()),
                    nonce=nonce, era={'period': ERA, 'current': row['start_block']})
                row.update(extrinsic_hash=hash_text(signed.extrinsic_hash), nonce=nonce)
                j.put(row)  # durable hash BEFORE broadcast; a crash never causes resubmission
                self.runtime(require_context=True)
                if time.time() >= expires:
                    reject('request expired before submission')
                conn.submit_extrinsic(signed, wait_for_inclusion=True, wait_for_finalization=True)
                return self.reconcile(row)
            except (Exception, Refusal):
                row['status'] = 'uncertain'
                j.put(row)
                return self.result(row)

    def result(self, row):
        return {k: row[k] for k in ('status', 'extrinsic_hash', 'block_hash', 'block_number', 'extrinsic_id') if k in row}

    def reconcile(self, row):
        if row.get('request',{}).get('action') in ('purchase','purchase-noah','purchase-foreign'):
            from .purchase_signer import reconcile
            return reconcile(self,row)
        if row['status'] not in ('prepared', 'uncertain'):
            return self.result(row)
        conn = self.services.connection
        if str(conn.get_block_hash(0)) != row['genesis']:
            reject('cannot reconcile on a different chain')
        final = conn.get_block_number(conn.get_chain_finalised_head())
        # Refuse replay even after mortality; expiry is a terminal record for this intent.
        for number in range(row['start_block'], min(final, row['start_block'] + 2 * ERA) + 1):
            block_hash = conn.get_block_hash(number)
            block = conn.get_block(block_hash=block_hash)
            for index, extrinsic in enumerate(block['extrinsics']):
                value = unwrap(extrinsic)
                raw_hash = getattr(extrinsic, 'extrinsic_hash', None) or value.get('extrinsic_hash')
                if raw_hash is None:
                    reject('finalized history contains an extrinsic without a verifiable hash; outcome remains unresolved')
                if hash_text(raw_hash) != row.get('extrinsic_hash'):
                    continue
                events = [unwrap(e) for e in conn.get_events(block_hash=block_hash)]
                events = [e for e in events if e.get('extrinsic_idx') == index]
                system = {(e.get('event', e).get('module_id'), e.get('event', e).get('event_id')) for e in events}
                inner = proxy_executed(events)
                status = 'failed' if ('System', 'ExtrinsicFailed') in system or inner is False else (
                    'finalized' if ('System', 'ExtrinsicSuccess') in system and inner is True else 'uncertain')
                row.update(status=status, block_hash=str(block_hash), block_number=number,
                           extrinsic_id=f'{number}-{index}')
                self.journal.put(row)
                return self.result(row)
        row['status'] = 'failed' if final > row['start_block'] + 2 * ERA else 'uncertain'
        self.journal.put(row)
        return self.result(row)


def ini_policy(path):
    """Manual participant policy. Public values only; no private deployment format."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    expected = {'policy_id', 'runtime_code_hash', 'spec_version', 'max_amount_rao',
                'max_total_rao', 'max_fee_rao'}
    purchase = parser.has_section('purchase')
    if purchase:expected.add('max_total_tao_rao')
    if not parser.has_section('signer') or set(parser['signer']) != expected:
        reject('signer section requires policy_id, runtime_code_hash, spec_version, '
               'max_amount_rao, max_total_rao and max_fee_rao')
    result = dict(parser['signer'])
    for field in ('spec_version', 'max_amount_rao', 'max_total_rao', 'max_fee_rao', *(['max_total_tao_rao'] if purchase else [])):
        try:
            result[field] = int(result[field])
        except ValueError:
            reject(field + ' must be a whole integer')
    from .conversion_config import validate
    if purchase:
        from dataclasses import asdict
        from .purchase import load_settings
        result['purchase']=asdict(load_settings(path))
    return dict(result, version=1, conversion=validate())


@contextlib.contextmanager
def shutdown_requests():
    """Signals stop admission; the synchronous active request finishes first."""
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield lambda: stopping
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def serve(signer, path, stopping):
    from .strict_json import read_json_text
    if Path(path).exists():
        reject('socket path already exists; inspect old worker before removing it')

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                if stopping():
                    reject('signer is stopping; no new request accepted')
                raw = self.rfile.readline(8193)
                if len(raw) > 8192 or not raw.endswith(b'\n'):
                    reject('request exceeds limit')
                # A stop can arrive while the client is supplying its request.
                if stopping():
                    reject('signer is stopping; no new request accepted')
                result = signer.execute(read_json_text(raw.decode()))
            except (Exception, Refusal):
                # No external exception text can expose secret/provider data.
                result = {'status': 'uncertain', 'reason': 'request not completed; inspect signer journal and public preflight'}
            self.wfile.write(json.dumps(result).encode() + b'\n')

    # This server is deliberately synchronous. Do not start another request
    # after shutdown, and never interrupt an active signing/submission handler.
    with socketserver.UnixStreamServer(path, Handler, bind_and_activate=False) as server:
        owned = None
        try:
            server.server_bind()
            node = Path(path).lstat()
            owned = (node.st_dev, node.st_ino)
            os.chmod(path, 0o600)
            server.server_activate()
            server.timeout = 0.2
            while not stopping():
                server.handle_request()
        finally:
            if owned is not None:
                try:
                    node = Path(path).lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (node.st_dev, node.st_ino) != owned:
                        reject('signer socket path changed; replacement preserved for inspection')
                    Path(path).unlink()


def close_resources(services, journal):
    """Attempt every close and expose failure without provider/key exception text."""
    resources = [('journal', journal)]
    if services is not None:
        resources += [('ledger', services.ledger), ('connection', services.connection)]
    failed = []
    for name, resource in resources:
        if resource is not None:
            try:
                resource.close()
            except (Exception, KeyboardInterrupt):
                failed.append(name)
    if failed:
        reject('signer cleanup failed for ' + ', '.join(failed) + '; inspect its journal before retrying')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='trusted host-owned deposit INI')
    parser.add_argument('--policy', help='trusted signer policy JSON; otherwise use the INI signer section')
    parser.add_argument('--journal', default=os.path.expanduser('~/.noah/signer-state'),
                        help='same durable directory for ALL workers; default ~/.noah/signer-state')
    parser.add_argument('--local-key-directory', help='disposable private-test keys only')
    parser.add_argument('--reconcile', action='store_true', help='read chain and journal only; never loads keys')
    args = parser.parse_args(argv)
    from noah.deposits.assistant.config import load_config
    from noah.deposits.assistant.cli import build_services
    from .strict_json import read_json
    config = load_config(args.config)
    services = journal = None
    with shutdown_requests() as stopping:
        try:
            services = build_services(config)
            journal = Journal(args.journal)
            signer = Signer(config, services, read_json(args.policy) if args.policy else ini_policy(args.config), journal,
                            lambda c: load_delegate(c, local_key_dir=args.local_key_directory))
            if args.reconcile:
                with journal.locked():
                    for row in journal.rows():
                        if stopping():
                            break
                        if row['genesis'] == config.genesis_hash:
                            print(json.dumps(signer.reconcile(row)))
            elif not stopping():
                serve(signer, signing(config)['signer_socket'], stopping)
        except KeyboardInterrupt:
            return 130
        except Refusal:
            raise
        except Exception:
            reject('signer stopped unexpectedly; inspect its journal before retrying')
        finally:
            close_resources(services, journal)
    return 0


if __name__ == '__main__':
    from noah.deposits.assistant.cli import finish
    try:
        result = main()
    except Refusal as error:
        print(f'Stopped.\n  Why: {error}')
        result = 2
    finish(result)
