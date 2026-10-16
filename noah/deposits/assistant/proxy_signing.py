"""Public proxy configuration and constrained, secret-free client operations.

Only the separately operated signer resolves signer_ref. No generic payload API.
"""
import json
import re
import socket
import time

from .amounts import DepositRefused
from ...chain.sources import Refusal
from ...chain.ss58 import public_key_of


def reject(message):
    raise DepositRefused(message, 'proxy_signing')


def uint(value, name, minimum=0):
    if type(value) is not int or not minimum <= value < 2**64:
        reject(name + ' must be a bounded whole integer')
    return value


def same(a, b):
    try:
        return public_key_of(a) == public_key_of(b)
    except (Exception, Refusal):
        return False


def validate_signing(raw=None):
    if raw is None:
        return {'mode': 'direct'}
    if not isinstance(raw, dict):
        reject('signing must be an object')
    if raw.get('mode') == 'direct':
        if set(raw) != {'mode'}:
            reject('direct signing accepts only mode')
        return dict(raw)
    required = {'mode', 'proxy_address', 'proxy_type', 'delay_blocks', 'signer_ref', 'signer_socket'}
    if raw.get('mode') != 'proxy' or not required <= set(raw) or set(raw) - required - {'conversion_proxy_type', 'purchase_proxy_type', 'secret_type'}:
        reject('invalid proxy signing fields')
    try:
        public_key_of(raw['proxy_address'])
    except (Exception, Refusal):
        reject('proxy_address must be a valid public address')
    if raw['proxy_type'] != 'Transfer':
        reject('deposit proxy_type must be Transfer; broader permissions are not substituted')
    if raw.get('conversion_proxy_type') not in (None, 'Staking'):
        reject('conversion_proxy_type must be Staking when configured')
    if raw.get('purchase_proxy_type') not in (None, 'Staking'):
        reject('purchase_proxy_type must be Staking when configured')
    if type(raw['delay_blocks']) is not int or raw['delay_blocks'] != 0:
        reject('only delay_blocks=0 is supported; delayed proxies require an announce lifecycle')
    ref = raw['signer_ref']
    if not isinstance(ref, str):
        reject('signer_ref must be a local wallet or test reference')
    # Default to a local wallet unless a test-only reference is explicit.
    backend = raw.get('secret_type', 'local-test' if ref.startswith('local-test:') else 'bittensor-wallet')
    if backend == 'local-test':
        if not re.fullmatch(r'local-test:[a-zA-Z0-9_-]+', ref):
            reject('local-test requires a named local-test reference')
    elif backend == 'bittensor-wallet':
        if not re.fullmatch(r'wallet:[a-zA-Z0-9_-]{1,64}', ref):
            reject('bittensor-wallet requires wallet:NAME')
    else:
        reject('unsupported secret_type; supported backends: bittensor-wallet, local-test')
    path = raw['signer_socket']
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9/_.-]+', path) or '..' in path.split('/'):
        reject('signer_socket must be an absolute Unix socket path')
    return dict(raw)


def secret_backend(config):
    s = signing(config)
    if s['mode'] != 'proxy':
        reject('secret lookup requires proxy signing mode')
    return s.get('secret_type', 'local-test' if s['signer_ref'].startswith('local-test:') else 'bittensor-wallet')


def signing(config):
    return validate_signing(getattr(config, 'signing', None))


def is_proxy(config):
    return signing(config)['mode'] == 'proxy'


def unwrap(value):
    return getattr(value, 'value', value)


def action_proxy_type(settings, action):
    return settings.get({'deposit':'proxy_type', 'convert':'conversion_proxy_type',
                         'purchase':'purchase_proxy_type'}.get(action, 'unsupported_action'))


def wrap(conn, config, inner, action):
    s = signing(config)
    if config.stake_position != 'miner':
        reject('proxy signing requires stake_position=miner; validator remark attribution is unsupported')
    proxy_type = action_proxy_type(s, action)
    if not proxy_type:
        reject('no proxy delegation configured for this action')
    return conn.compose_call(call_module='Proxy', call_function='proxy', call_params={
        'real': config.coldkey, 'force_proxy_type': proxy_type, 'call': inner})


def preflight(conn, config, inner, action):
    """Read current delegation and estimated delegate fees. Never opens a wallet."""
    s = signing(config)
    if not config.genesis_hash or str(conn.get_block_hash(0)) != config.genesis_hash:
        reject('proxy signing requires the correct pinned genesis')
    outer = wrap(conn, config, inner, action)
    proxy_type = action_proxy_type(s, action)
    rows = unwrap(conn.query('Proxy', 'Proxies', [config.coldkey]))
    if not isinstance(rows, (list, tuple)) or len(rows) != 2 or not isinstance(rows[0], (list, tuple)):
        reject('proxy delegation is unreadable')
    matches = [r for r in rows[0] if same(r.get('delegate'), s['proxy_address']) and r.get('proxy_type') == proxy_type]
    if not any(type(r.get('delay')) is int and r['delay'] == 0 for r in matches):
        reject('required zero-delay proxy delegation is missing, revoked or delayed')
    # Construct only a public key object. get_payment_info uses it for the fee estimate.
    from bittensor.sp_core import Keypair
    public = Keypair(ss58_address=s['proxy_address'])
    fee = unwrap(conn.get_payment_info(call=outer, keypair=public))
    fee = uint(fee.get('partial_fee', fee.get('partialFee')), 'fee')
    data = unwrap(conn.query('System', 'Account', [s['proxy_address']]))['data']
    frozen = max(uint(data.get(k, 0), k) for k in ('frozen', 'fee_frozen', 'misc_frozen'))
    existential = uint(unwrap(conn.get_constant('Balances', 'ExistentialDeposit')), 'existential deposit')
    if uint(data['free'], 'delegate free balance') - frozen - existential < 2 * fee:
        reject('delegate TAO balance cannot cover fee with 2x headroom and existential deposit')
    return outer, {'proxy_address': s['proxy_address'], 'real': config.coldkey,
                   'proxy_type': proxy_type, 'fee_estimate_rao': fee,
                   'runtime_permission_verified': False}


def request_identity(config):
    return {'genesis': config.genesis_hash, 'owner': config.coldkey,
            'miner': config.miner_hotkey, 'netuid': config.netuid,
            'delegate': signing(config)['proxy_address'], 'ark': config.ark_coldkey,
            'validator': config.validator_hotkey, 'stake_position': config.stake_position,
            'reserve_rao': config.reserve_rao, 'cap_rao': config.cap_rao}


def request(config, *, action, intent_id, amount_rao, limit_price=None, quote_block=None, operation="submit", destination_netuid=None, destination_hotkey=None, journal_id=None, reservation_token=None, cancellation=None):
    """The socket is bound to a trusted per-miner policy. Caller supplies no destinations."""
    payload = {'version': 1, 'expires_at': int(time.time()) + 120, 'operation': operation, 'action': action, 'intent_id': intent_id, 'amount_rao': amount_rao, 'identity': request_identity(config)}
    if action in ('purchase','purchase-noah','purchase-foreign') and operation in ('submit','status','recover-pre-submit'):
        payload.update(journal_id=journal_id,reservation_token=reservation_token)
    if action == 'purchase' and operation not in ('cancel-check','cancel'):
        payload.update(limit_price=limit_price, quote_block=quote_block)
    if operation in ('cancel-check','cancel'):
        payload['config_binding'] = (cancellation or {}).get('config_binding')
    if operation == 'cancel':
        for field in ('journal_id','policy_fingerprint','cancellation_token'):
            payload[field] = (cancellation or {}).get(field)
    if action == 'convert':
        payload.update(limit_price=limit_price, quote_block=quote_block,
                       destination_netuid=destination_netuid, destination_hotkey=destination_hotkey)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(180)
        sock.connect(signing(config)['signer_socket'])
        sock.sendall(json.dumps(payload, separators=(',', ':')).encode() + b'\n')
        with sock.makefile('rb') as stream:
            raw = stream.readline(65537)
    if len(raw) > 65536:
        reject('signer response exceeded limit')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('status') not in ('checked', 'not_submitted', 'finalized', 'failed', 'uncertain', 'refused', 'cancel_ready', 'cancelled'):
        reject('signer response is invalid; inspect signer journal before retry')
    return result


def proxy_executed(events):
    """Outer ExtrinsicSuccess does not establish success of the proxied call."""
    results = []
    for raw in events:
        event = unwrap(raw)
        e = event.get('event', event)
        if e.get('module_id') == 'Proxy' and e.get('event_id') == 'ProxyExecuted':
            attrs = e.get('attributes', {})
            result = attrs.get('result') if isinstance(attrs, dict) else (attrs[0] if len(attrs) == 1 else None)
            results.append(result)
    if len(results) != 1:
        return None
    result = results[0]
    if isinstance(result, dict) and set(result) == {'Ok'}:
        return True
    if isinstance(result, dict) and set(result) == {'Err'}:
        return False
    return None
