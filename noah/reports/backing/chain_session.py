"""Read one configured chain and submit using the caller-authorized miner hotkey."""

from __future__ import annotations

import hashlib
import re
import time

from .deposit_index import event_id_of, phase_of
from .finality import prove_ancestry
from .submit import Inclusion, require_sr25519, SubmitError, SigningRuntimeRefused


def commitment_info(variant: str, payload: bytes) -> dict:
    """Same `Data` encoding as `scripts/localnet_rehearsal.py`."""
    value = "0x" + payload.hex()
    return {"fields": [[{variant: value}]]}


class SubstrateChain:
    def __init__(self, url: str):
        from .sdk_client import live_sdk

        self.substrate = live_sdk(url)
        try:
            self.substrate.init_runtime()
        except BaseException:
            self.substrate.close()
            raise
        self.search_from_block = None

    def genesis(self) -> str:
        return str(self.substrate.get_block_hash(0)).lower()

    def finalized_time_ms(self) -> int:
        head = self._rpc("chain_getFinalizedHead", [])
        return int(self.substrate.query("Timestamp", "Now", block_hash=head).value)

    def is_registered(self, netuid: int, hotkey_ss58: str) -> bool:
        return self.substrate.query("SubtensorModule", "Uids", [netuid, hotkey_ss58]).value is not None

    def commitment(self, netuid: int, hotkey_ss58: str) -> bytes | None:
        stored = self.substrate.query("Commitments", "CommitmentOf", [netuid, hotkey_ss58]).value
        return context_commitment_of(stored) or raw32_of(stored)

    def commitment_quota(self, netuid, hotkey, required_space):
        """Public finalized quota receipt; no wallet, nonce lookup or transaction."""
        from .commitment_quota import QuotaRulesUnreviewed, read_commitment_quota
        try:
            return read_commitment_quota(self.substrate, mining_netuid=netuid,
                                         hotkey=hotkey, required_space=required_space)
        except QuotaRulesUnreviewed:
            if getattr(self, 'report_proxy', None):
                raise
            # The chain enforces its own quota. Do not guess the rules on an upgrade.
            return None

    def ensure_supported_signing_runtime(self):
        """Fresh finalized runtime check; existing sends remain reconcilable."""
        from types import SimpleNamespace
        from .reads import Reader
        try:
            head = _runtime_hash(self._rpc("chain_getFinalizedHead", []))
            reader = Reader(SimpleNamespace(call=self._rpc), head)
            if not reader.qualified_runtime(reader.spec_version()):
                raise SigningRuntimeRefused("current finalized runtime is not supported for report signing")
        except SigningRuntimeRefused:
            raise
        except Exception as exc:
            raise SigningRuntimeRefused("current finalized signing runtime could not be checked") from exc

    def check_finalized_signing_context(self):
        """Check the finalized context before opening a wallet."""
        try:
            head = _runtime_hash(self._rpc("chain_getFinalizedHead", []))
            self._runtime_version_at(head)
        except Exception as exc:
            raise SigningRuntimeRefused("current finalized signing context could not be read") from exc

    def _runtime_version_at(self, block):
        version = _version_pair(self._rpc("state_getRuntimeVersion", [block]))
        if getattr(self, 'report_proxy', None):
            from types import SimpleNamespace
            from .reads import Reader
            reader = Reader(SimpleNamespace(call=self._rpc), block)
            if not reader.qualified_runtime(version[0]):
                raise SigningRuntimeRefused("report proxy signing requires a reviewed runtime")
        return version

    def _checked_sdk_runtime(self):
        """Check the selected parent version and capture the SDK metadata identity."""
        try:
            block = _runtime_hash(self.substrate.block_hash)
            parent = _runtime_hash(self._rpc("chain_getHeader", [block])["parentHash"])
            pin = block if parent == "0x" + "00" * 32 else parent
            version = self._runtime_version_at(pin)
            identity = _sdk_identity(self.substrate)
            if version != identity[:2]:
                raise SigningRuntimeRefused("SDK signing runtime version differs from its chain context")
            return identity
        except SigningRuntimeRefused:
            raise
        except Exception as exc:
            raise SigningRuntimeRefused("SDK signing runtime could not be checked") from exc

    def sign(self, keypair, netuid: int, variant: str, payload: bytes):
        require_sr25519(keypair)
        self.check_finalized_signing_context()
        call = self.substrate.compose_call(
            "Commitments",
            "set_commitment",
            {"netuid": netuid, "info": commitment_info(variant, payload)},
        )
        proxy = getattr(self, 'report_proxy', None)
        if proxy:
            from .proxy_receipts import public_hex
            if (proxy.get('netuid') != netuid or public_hex(proxy.get('delegate')) != keypair.public_key.hex()
                    or not public_hex(proxy.get('real')) or public_hex(proxy['real']) == keypair.public_key.hex()):
                raise SigningRuntimeRefused("proxy route does not match signer and report")
            call = self.substrate.compose_call('Proxy', 'proxy', {
                'real': proxy['real'], 'force_proxy_type': 'Validate', 'call': call})
            self._proxy_preflight(call, keypair)
        expected = self._checked_sdk_runtime()
        if _metadata_fingerprint(getattr(call, 'metadata', None)) != expected[2]:
            raise SigningRuntimeRefused("composed call metadata differs from SDK signing metadata")
        chain = self

        class GuardedSigner:
            invoked = False

            def __getattr__(self, name):
                return getattr(keypair, name)

            def sign(self, *args, **kwargs):
                if self.invoked:
                    raise SubmitError("multiple signature requests refused")
                if (chain._checked_sdk_runtime() != expected
                        or _metadata_fingerprint(getattr(call, 'metadata', None)) != expected[2]):
                    raise SigningRuntimeRefused("signing runtime or composed metadata changed before signature")
                self.invoked = True
                try:
                    return keypair.sign(*args, **kwargs)
                except SigningRuntimeRefused as exc:
                    # Even a similarly named error from a wallet is no longer
                    # proof of an unsent attempt once signing has begun.
                    raise SubmitError("signer failed after signing began") from exc

        signer = GuardedSigner()
        try:
            extrinsic = self.substrate.create_signed_extrinsic(call=call, keypair=signer)
        except SigningRuntimeRefused as exc:
            if signer.invoked:
                raise SubmitError("SDK failed after signing began") from exc
            raise
        if not signer.invoked:
            raise SigningRuntimeRefused("SDK did not invoke the guarded signer")
        try:
            if (_sdk_identity(self.substrate) != expected
                    or _metadata_fingerprint(getattr(call, 'metadata', None)) != expected[2]):
                raise SubmitError("signing metadata changed after signing began")
        except SigningRuntimeRefused as exc:
            raise SubmitError("signing metadata unavailable after signing began") from exc
        return _as_hex(extrinsic.extrinsic_hash), extrinsic

    def _proxy_preflight(self, call, keypair):
        """No signature: exact zero-delay Validate grant, fees plus 2%, account minimum."""
        from .proxy_receipts import public_hex
        try:
            proxy = self.report_proxy
            head = self._rpc('chain_getFinalizedHead', [])
            grants = self.substrate.query('Proxy', 'Proxies', [proxy['real']], block_hash=head).value
            if not isinstance(grants, (tuple, list)) or len(grants) != 2:
                raise ValueError('invalid delegation response')
            allowed = any(isinstance(r, dict) and public_hex(r.get('delegate')) == keypair.public_key.hex()
                          and r.get('proxy_type') == 'Validate' and type(r.get('delay')) is int and r['delay'] == 0
                          for r in grants[0])
            if not allowed:
                raise ValueError('zero-delay Validate delegation missing')
            from substrateinterface import Keypair
            public = Keypair(ss58_address=proxy['delegate'])
            quote = self.substrate.get_payment_info(call=call, keypair=public)
            fee = quote.get('partialFee', quote.get('partial_fee'))
            data = self.substrate.query('System', 'Account', [proxy['delegate']], block_hash=head).value['data']
            minimum = self.substrate.get_constant('Balances', 'ExistentialDeposit', block_hash=head).value
            frozen = max(data.get(k, 0) for k in ('frozen', 'fee_frozen', 'misc_frozen'))
            if any(type(x) is not int or x < 0 for x in (fee, minimum, frozen, data['free'])):
                raise ValueError('invalid fee or balance')
            if data['free'] - frozen - minimum < (fee * 102 + 99) // 100:
                raise ValueError('delegate balance cannot cover estimated fee plus 2% and account minimum')
        except Exception as exc:
            raise SigningRuntimeRefused('report proxy preflight refused; verify zero-delay Validate grant and delegate fee balance') from exc

    def preflight_proxy_report(self, netuid, variant, payload):
        """Retryable public-only check before opening the wallet or phase journal."""
        from substrateinterface import Keypair
        from .proxy_receipts import public_hex
        proxy = getattr(self, 'report_proxy', None)
        if not proxy:
            return
        if proxy.get('netuid') != netuid or not public_hex(proxy.get('delegate')) or not public_hex(proxy.get('real')):
            raise SigningRuntimeRefused('invalid report proxy route')
        inner = self.substrate.compose_call('Commitments', 'set_commitment',
                                             {'netuid': netuid, 'info': commitment_info(variant, payload)})
        call = self.substrate.compose_call('Proxy', 'proxy', {
            'real': proxy['real'], 'force_proxy_type': 'Validate', 'call': inner})
        self._proxy_preflight(call, Keypair(ss58_address=proxy['delegate']))

    def broadcast(self, handle) -> Inclusion:
        receipt = self.substrate.submit_extrinsic(handle, wait_for_inclusion=True)
        return self._inclusion_from_receipt(receipt)

    def find(self, extrinsic_hash: str) -> Inclusion | None:
        head = self._rpc("chain_getFinalizedHead", [])
        cursor = head
        scanned = 0
        while True:
            header = self.substrate.get_block_header(cursor)["header"]
            number = _number(header["number"])
            if self.search_from_block is not None and number < self.search_from_block:
                return None
            if self.search_from_block is None and scanned >= 64:
                return None
            block = self.substrate.get_block(cursor)
            extrinsics = block["extrinsics"]
            for index, extrinsic in enumerate(extrinsics):
                hashed = getattr(extrinsic, "extrinsic_hash", None)
                if hashed and _as_hex(hashed) == extrinsic_hash.lower():
                    return self._inclusion_from_block(cursor, index, extrinsic_hash)
            if number == 0:
                return None
            cursor = header["parentHash"]
            scanned += 1
        return None

    def finality(self, block_hash: str, block_number: int) -> dict:
        deadline = time.time() + 90
        proof = {"matches": False}

        def header_of(one_hash: str):
            header = self.substrate.get_block_header(one_hash)["header"]
            return _number(header["number"]), header["parentHash"]

        while time.time() < deadline:
            head = self._rpc("chain_getFinalizedHead", [])
            head_number = _number(self.substrate.get_block_header(head)["header"]["number"])
            proof = prove_ancestry(
                head_hash=head,
                head_number=head_number,
                inclusion_hash=block_hash,
                inclusion_number=block_number,
                header_of=header_of,
            )
            if proof["matches"] or head_number >= block_number:
                return proof
            time.sleep(0.2)
        return proof

    def block_events(self, block_hash: str) -> list:
        value = self.substrate.query("System", "Events", block_hash=block_hash).value
        return [_plain_event(item) for item in value or []]

    def _inclusion_from_receipt(self, receipt) -> Inclusion:
        if getattr(self, 'report_proxy', None):
            return self._inclusion_from_block(str(receipt.block_hash), int(receipt.extrinsic_idx),
                                              _as_hex(receipt.extrinsic_hash))
        header = self.substrate.get_block_header(receipt.block_hash)["header"]
        events = [_plain_event(item.value if hasattr(item, "value") else item) for item in receipt.triggered_events]
        success = _succeeded(events)
        return Inclusion(
            extrinsic_hash=_as_hex(receipt.extrinsic_hash),
            block_hash=str(receipt.block_hash),
            block_number=_number(header["number"]),
            extrinsic_index=int(receipt.extrinsic_idx),
            block_timestamp_ms=int(self.substrate.query("Timestamp", "Now", block_hash=receipt.block_hash).value),
            success=success,
            dispatch_error=None if success else _receipt_error_name(receipt),
        )

    def _inclusion_from_block(self, block_hash: str, index: int, extrinsic_hash: str) -> Inclusion:
        header = self.substrate.get_block_header(block_hash)["header"]
        events = [
            event
            for event in self.block_events(block_hash)
            if phase_of(event) == ("ApplyExtrinsic", index)
        ]
        success = _succeeded(events)
        proxy = getattr(self, 'report_proxy', None)
        if proxy:
            from .proxy_receipts import proxy_report, proxy_success, public_hex, arguments
            block = self.substrate.get_block(block_hash=block_hash)
            routed = proxy_report(block['extrinsics'][index].value)
            success = bool(routed and routed[1] == public_hex(proxy['real'])
                           and routed[2] == public_hex(proxy['delegate'])
                           and arguments(routed[0]).get('netuid') == proxy['netuid']
                           and proxy_success(events, index, routed[1], proxy['netuid']))
        return Inclusion(
            extrinsic_hash=extrinsic_hash,
            block_hash=str(block_hash),
            block_number=_number(header["number"]),
            extrinsic_index=index,
            block_timestamp_ms=int(self.substrate.query("Timestamp", "Now", block_hash=block_hash).value),
            success=success,
        )

    def _rpc(self, method: str, params: list):
        return self.substrate.rpc_request(method, params)["result"]


def _receipt_error_name(receipt):
    try:
        error = receipt.error_message
    except Exception:
        # Decoding the optional label cannot change the observed failure event.
        return None
    name = error.get("name") if isinstance(error, dict) else None
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else None


def _runtime_hash(value):
    if type(value) is not str or re.fullmatch(r'0x[0-9a-f]{64}', value) is None:
        raise SigningRuntimeRefused("invalid signing runtime block identity")
    return value


def _metadata_fingerprint(metadata):
    try:
        raw = bytes(metadata.data.data)
        if not raw:
            raise ValueError('empty metadata')
        return hashlib.sha256(raw).hexdigest()
    except Exception as exc:
        raise SigningRuntimeRefused("signing metadata unavailable") from exc


def _version_pair(version):
    if not isinstance(version, dict):
        raise SigningRuntimeRefused("invalid runtime version")
    spec, transaction = version.get("specVersion"), version.get("transactionVersion")
    if type(spec) is not int or type(transaction) is not int or min(spec, transaction) < 0:
        raise SigningRuntimeRefused("invalid runtime version")
    return spec, transaction


def _sdk_identity(substrate):
    spec = getattr(substrate, 'runtime_version', None)
    transaction = getattr(substrate, 'transaction_version', None)
    if type(spec) is not int or type(transaction) is not int or min(spec, transaction) < 0:
        raise SigningRuntimeRefused("invalid SDK signing runtime version")
    return spec, transaction, _metadata_fingerprint(getattr(substrate, 'metadata', None))


def context_commitment_of(stored):
    """Read only an unambiguous context envelope from the stored Data fields."""
    from .protocol.context_commitment import decode_commitment
    found = []
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'BigRaw' and isinstance(item, str) and item.startswith('0x'):
                    try:
                        raw = bytes.fromhex(item[2:])
                        decode_commitment(raw)
                        found.append(raw)
                    except ValueError:
                        pass
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(stored)
    return found[0] if len(found) == 1 else None


def raw32_of(stored) -> bytes | None:
    if isinstance(stored, (bytes, bytearray)) and len(stored) == 32:
        return bytes(stored)
    if isinstance(stored, str) and stored.startswith("0x") and len(stored) == 66:
        return bytes.fromhex(stored[2:])
    if isinstance(stored, dict):
        for value in stored.values():
            found = raw32_of(value)
            if found is not None:
                return found
    if isinstance(stored, list):
        for value in stored:
            found = raw32_of(value)
            if found is not None:
                return found
    return None


def _plain_event(item) -> dict:
    if hasattr(item, "value"):
        item = item.value
    if not isinstance(item, dict):
        return {}
    return item


def _succeeded(events: list[dict]) -> bool:
    names = {event_id_of(event) for event in events}
    return "ExtrinsicSuccess" in names and "ExtrinsicFailed" not in names


def _as_hex(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + value.hex()
    text = str(value).lower()
    return text if text.startswith("0x") else "0x" + text


def _number(value) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)
