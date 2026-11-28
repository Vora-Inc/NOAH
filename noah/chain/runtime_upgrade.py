"""Read-only upgrade detection, plus atomic installation of signed approvals."""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .runtime_compatibility import (
    APPROVALS_ENV, CAPABILITIES, COMMIT, HASH, KEY_ENV, MAX_BYTES, SCHEMA, lookup, read, verify,
)
from .sources import QuietArgumentParser, Refusal
from .token_context import explicit_metadata, rpc

UPSTREAM = 'https://api.github.com/repos/RaoFoundation/subtensor/releases/latest'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url, limit):
    if urlsplit(url).scheme != 'https':
        raise ValueError('runtime approval downloads require HTTPS')
    with build_opener(NoRedirect).open(url, timeout=20) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('download exceeds size limit')
    return raw


def install(destination, data, key):
    """Verify the candidate before writing. Serialize concurrent installers."""
    import fcntl

    destination = Path(destination)
    updated = verify(data, key)
    lock = destination.with_name(destination.name + '.lock')
    with lock.open('a+b') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if destination.exists():
            previous = read(destination, key)
            for identity, old in previous.items():
                new = updated.get(identity)
                if new is None or (old.code, old.transaction, old.metadata, old.source) != (
                    new.code, new.transaction, new.metadata, new.source
                ) or not old.capabilities <= new.capabilities:
                    raise ValueError('approval update removes or changes an existing qualification')
            if previous == updated:
                return len(updated)
        fd, path = tempfile.mkstemp(prefix=destination.name + '.', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(path).unlink(missing_ok=True)
    return len(updated)


def finalized(connection):
    head = rpc(connection, 'chain_getFinalizedHead', [])
    if not isinstance(head, str) or not HASH.fullmatch(head):
        raise ValueError('invalid finalized head')
    return int(rpc(connection, 'chain_getHeader', [head])['number'], 16)


def read_identity(connection, height):
    block = rpc(connection, 'chain_getBlockHash', [height])
    genesis = rpc(connection, 'chain_getBlockHash', [0])
    runtime = rpc(connection, 'state_getRuntimeVersion', [block])
    code = rpc(connection, 'state_getStorageHash', ['0x3a636f6465', block])
    if not all(isinstance(value, str) and HASH.fullmatch(value) for value in (block, genesis, code)):
        raise ValueError('invalid finalized runtime identity')
    spec, transaction = runtime['specVersion'], runtime['transactionVersion']
    if any(type(version) is not int or not 0 <= version < 2**32 for version in (spec, transaction)):
        raise ValueError('invalid runtime versions')
    metadata = hashlib.sha256(explicit_metadata(connection, block)).hexdigest()
    return {'genesis': genesis, 'height': height, 'block': block,
            'spec': spec, 'transaction': transaction, 'code': code, 'metadata': metadata}


def independent_connections(stack, primary, secondary):
    from .read_connection import ReadSubstrateInterface
    from .sources import canonical_endpoint

    if not primary or not secondary or canonical_endpoint(primary) == canonical_endpoint(secondary):
        raise ValueError('two independent readers are required')
    return [stack.enter_context(closing(ReadSubstrateInterface(url=url, auto_discover=False)))
            for url in (primary, secondary)]


def paired_identity(connections, expected_genesis, height):
    left, right = (read_identity(connection, height) for connection in connections)
    if left != right or left['genesis'] != expected_genesis:
        raise ValueError('paired finalized runtime identities diverge')
    return left


def observe(primary, secondary, expected_genesis):
    with ExitStack() as stack:
        connections = independent_connections(stack, primary, secondary)
        return paired_identity(connections, expected_genesis,
                               min(finalized(connection) for connection in connections))


def rehearsal_observation(connection, identity, netuid, asset, hotkey):
    """Exercise the deployed proof readers and construct the actual unsigned call layouts."""
    from ..reports.backing.chain_session import commitment_info
    from ..reports.backing.commitment_quota import read_commitment_quota, required_space_for_payload
    from .burn_context import read as burn
    from .ema_context import read as ema

    block, at = identity['height'], identity['block']
    if not 0 < netuid < 65536 or not 0 < asset < 65536 or asset == netuid:
        raise ValueError('invalid qualification subnet or reference asset')
    runtime = ema(connection, block, at, [netuid, asset], {}, 'SubnetMovingPrice')
    burn_proof = burn(lambda m, params: rpc(connection, m, params), block, at, netuid)
    forms = [('Raw32', bytes(32)), ('BigRaw', bytes(79))]
    calls, quota = {}, {}
    for variant, payload in forms:
        calls[variant] = connection.compose_call(
            'Commitments', 'set_commitment', {'netuid': netuid, 'info': commitment_info(variant, payload)},
            block_hash=at,
        ).data.to_hex()
        quota[variant] = read_commitment_quota(
            connection, mining_netuid=netuid, hotkey=hotkey,
            required_space=required_space_for_payload(payload), at=at,
        )
    from ..core.submitter import DIRECT_CALL, DIRECT_MODULE
    calls['weights'] = connection.compose_call(
        DIRECT_MODULE, DIRECT_CALL, {'netuid': netuid, 'mecid': 0,
                                    'dests': [0], 'weights': [65535], 'version_key': 0},
        block_hash=at,
    ).data.to_hex()
    return {'runtime': runtime, 'burn': burn_proof, 'unsigned_calls': calls, 'quota': quota}


@contextmanager
def _unsigned_rehearsal_registry(identity, source):
    """Give only the unsigned rehearsal process temporary proof-reader trust."""
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa

    if not COMMIT.fullmatch(source):
        raise ValueError('rehearsal source must be a full lowercase commit SHA')
    authority = ECC.generate(curve='Ed25519')
    key = authority.public_key().export_key(format='raw').hex()
    fields = [str(identity[name]) for name in ('genesis', 'spec', 'transaction', 'code', 'metadata')]
    row = '\t'.join([*fields, source, 'burn,ema,reports,token']).encode('ascii')
    payload = SCHEMA + b'\n' + row + b'\n'
    signature = eddsa.new(authority, 'rfc8032').sign(payload).hex().encode('ascii')
    previous = {name: os.environ.get(name) for name in (APPROVALS_ENV, KEY_ENV)}
    with tempfile.TemporaryDirectory(prefix='noah-unsigned-rehearsal-') as directory:
        path = Path(directory) / 'approvals'
        install(path, signature + b'\n' + payload, key)
        os.environ.update({APPROVALS_ENV: str(path), KEY_ENV: key})
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def rehearse(settings, genesis, at, wasm, netuid, asset, hotkey, *, source=None):
    with ExitStack() as stack:
        connections = independent_connections(stack, settings.chain_endpoint, settings.second_chain_endpoint)
        if type(at) is not int or at <= 1 or at > min(finalized(item) for item in connections):
            raise ValueError('upgrade boundary is not finalized on both readers')
        before = paired_identity(connections, genesis, at - 1)
        after = paired_identity(connections, genesis, at)
        if before['code'] == after['code']:
            raise ValueError('selected pin does not cross a runtime upgrade')
        verify_wasm(wasm, after)
        # The SDK can select parent metadata at the upgrade block. Test both
        # signed payload layouts from the first child block using the new parent.
        if at + 1 > min(finalized(item) for item in connections):
            raise ValueError('first child block is not finalized')
        probe = paired_identity(connections, genesis, at + 1)
        if any(probe[name] != after[name] for name in ('spec', 'transaction', 'code', 'metadata')):
            raise ValueError('runtime changed at the first child block')
        approved = approved_capabilities(probe)
        if source is not None:
            stack.enter_context(_unsigned_rehearsal_registry(probe, source))
        left, right = (rehearsal_observation(item, probe, netuid, asset, hotkey)
                       for item in connections)
        if left != right:
            raise ValueError('candidate capability probes diverge between readers')
    return {'status': 'paired_unsigned_rehearsal', 'before': before, 'activation': after,
            'probe': probe, 'capabilities': approved, 'rehearsal_source': source,
            'observations_sha256': hashlib.sha256(json.dumps(left, sort_keys=True).encode()).hexdigest()}


def approved_capabilities(identity):
    from .burn_context import REVIEWED
    from .ema_context import EMA_CODES
    from .token_context import bundled_identity

    genesis, spec, transaction, code, metadata = (
        identity[key] for key in ('genesis', 'spec', 'transaction', 'code', 'metadata')
    )
    bundled = set()
    if bundled_identity(spec, transaction, code):
        bundled.add('token')
        if EMA_CODES.get(spec) == code:
            bundled.add('ema')
        if REVIEWED.get(spec) == (code, metadata):
            bundled.add('burn')
        if spec in (494, 495):
            bundled.update(('reports', 'weights'))
    return sorted(
        capability for capability in CAPABILITIES
        if capability in bundled or (
            (approval := lookup(genesis, spec, transaction, code, capability)) is not None
            and approval.metadata == metadata
        )
    )


def verify_wasm(path, identity):
    digest = hashlib.blake2b(digest_size=32)
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if '0x' + digest.hexdigest() != identity['code']:
        raise ValueError('candidate WASM differs from the exact on-chain code hash')


def main(argv=None):
    parser = QuietArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    watch = sub.add_parser('observe')
    watch.add_argument('--env', required=True)
    watch.add_argument('--require', choices=sorted(CAPABILITIES), action='append', default=[])
    watch.add_argument('--spec', type=int)
    watch.add_argument('--wasm')
    watch.add_argument('--upstream', action='store_true')
    replay = sub.add_parser('rehearse')
    replay.add_argument('--env', required=True)
    replay.add_argument('--activation-block', required=True, type=int)
    replay.add_argument('--wasm', required=True)
    replay.add_argument('--source', help='full source commit for an unsigned probe of an unapproved runtime')
    replay.add_argument('--netuid', required=True, type=int)
    replay.add_argument('--asset', required=True, type=int)
    replay.add_argument('--hotkey', required=True, help='public quota account used for the unsigned probe')
    update = sub.add_parser('install')
    update.add_argument('--path', required=True)
    update.add_argument('--key', required=True)
    source = update.add_mutually_exclusive_group(required=True)
    source.add_argument('--file')
    source.add_argument('--url')
    args = parser.parse_args(argv)
    try:
        if args.command == 'install':
            if args.file:
                with Path(args.file).open('rb') as stream:
                    raw = stream.read(MAX_BYTES + 1)
            else:
                raw = download(args.url, MAX_BYTES)
            print(json.dumps({'status': 'installed', 'entries': install(args.path, raw, args.key)}))
            return 0
        from ..core.settings import Settings, parse_env_file

        values = parse_env_file(Path(args.env).read_text())
        for name in (APPROVALS_ENV, KEY_ENV):
            if name in values:
                os.environ[name] = values[name]
        settings = Settings.from_mapping(values)
        genesis = settings.mainnet_enable if settings.network == 'finney' else settings.local_genesis
        if args.command == 'rehearse':
            result = rehearse(settings, genesis, args.activation_block, args.wasm,
                              args.netuid, args.asset, args.hotkey, source=args.source)
            print(json.dumps(result, sort_keys=True))
            return 0
        identity = observe(settings.chain_endpoint, settings.second_chain_endpoint, genesis)
        if args.wasm:
            verify_wasm(args.wasm, identity)
        approved = approved_capabilities(identity)
        output = {**identity, 'approved': approved, 'missing': sorted(set(args.require) - set(approved))}
        if args.upstream:
            tag = json.loads(download(UPSTREAM, 512 * 1024))['tag_name']
            match = re.fullmatch(r'v([0-9]{1,10})', tag)
            if match is None:
                raise ValueError('upstream release has no recognized runtime version')
            latest = int(match[1])
            key, path = os.environ.get(KEY_ENV), os.environ.get(APPROVALS_ENV)
            entries = read(path, key) if key and path and Path(path).exists() else {}
            upcoming = entries.get((identity['genesis'], latest))
            output['upstream_spec'] = latest
            output['upcoming_missing'] = sorted(set(args.require) - upcoming.capabilities) if upcoming else args.require
            if latest <= identity['spec']:
                output['upcoming_missing'] = []
        if args.spec is not None and identity['spec'] != args.spec:
            output['spec_mismatch'] = True
        blocked = output['missing'] or output.get('upcoming_missing') or output.get('spec_mismatch')
        print(json.dumps({**output, 'status': 'action_required' if blocked else 'qualified'}, sort_keys=True))
        return 2 if blocked else 0
    except (Exception, Refusal) as error:
        print(json.dumps({'status': 'read_or_verification_failed', 'error_type': type(error).__name__}),
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
