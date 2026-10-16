"""Compose and submit sealed or direct weight publications through the SDK.

Retain the distinction between rejection, uncertain send status and verified
inclusion. The publisher and restart logic perform the corresponding chain checks;
a transport acknowledgement alone must not be described as a confirmed result."""

from ..diagnostics import debug

from .settings import SettingsError
from .version import OUR_VERSION_KEY
from .proxy_route import INNER_ERR, INNER_OK, classify_inner
from .wallet import load_hotkey, permitted, permitted_proxy_wrapper

MODULE = "SubtensorModule"
CALL = "commit_timelocked_mechanism_weights"

DIRECT_MODULE = "SubtensorModule"
DIRECT_CALL = "set_mechanism_weights"

# The six values the reveal-round arithmetic needs, and the names they are
# passed to the sealer under.
SCHEDULE = {
    "Tempo": "tempo",
    "RevealPeriodEpochs": "subnet_reveal_period_epochs",
    "LastEpochBlock": "last_epoch_block",
    "PendingEpochAt": "pending_epoch_at",
    "SubnetEpochIndex": "subnet_epoch_index",
    "BlocksSinceLastStep": "blocks_since_last_step",
}


class ScheduleUnreadable(SettingsError):
    """A value the reveal round is computed from could not be read."""


def _block_from_extrinsic_id(extrinsic_id):
    """Parse the including block from the SDK's block-number/index identifier, or return None."""
    if not extrinsic_id:
        return None
    block_part = str(extrinsic_id).split("-", 1)[0]
    try:
        return int(block_part)
    except ValueError:
        return None


def _extrinsic_index_from_id(extrinsic_id):
    """The extrinsic's index inside its block, parsed from the same
    ``"block_number-index"`` string `_block_from_extrinsic_id` reads, or
    ``None`` when absent or malformed (never raises). Recorded in the per-day
    commit ledger when available; nothing depends on it being present."""
    if not extrinsic_id or "-" not in str(extrinsic_id):
        return None
    try:
        return int(str(extrinsic_id).split("-", 1)[1])
    except ValueError:
        return None


class Publisher:
    """Seals and submits, and reads back what it put there."""

    def __init__(
        self,
        client,
        wallet,
        *,
        netuid,
        mecid=0,
        commit_reveal_version=4,
        version_key=OUR_VERSION_KEY,
        submit_client=None,
        commit_identity=None,
    ):
        self.client = client
        # Proxy route (noah.core.proxy_route): when set, the sealed commit is wrapped in
        # Proxy.proxy(real=<this identity>, ...) and the signer (self.wallet) is the
        # dedicated proxy signer, not the validator. None means the signer IS the validator.
        self.commit_identity = commit_identity
        self.submit_client = submit_client if submit_client is not None else client
        self.wallet = load_hotkey(wallet) if hasattr(wallet, "hotkey") else wallet
        self.netuid = netuid
        self.mecid = mecid
        self.commit_reveal_version = commit_reveal_version
        # Derived from the scoring contract rather than typed, so a scoring
        # change announces itself instead of two validators quietly publishing
        # different numbers under the same version.
        self.version_key = version_key

    @property
    def signer_address(self):
        """The ss58 address of the key that signs (the proxy signer on the proxy route)."""
        return getattr(self.wallet, "ss58_address", None)

    def _schedule(self, block):
        """The six values, all read at the SAME block.

        The pinning is the point, not the form it takes. This client pins by
        block number where the async one pins by block hash; either way the six
        must not straddle an epoch boundary.
        """
        values = {}
        for item, name in SCHEDULE.items():
            raw = self.client.query((MODULE, item), [self.netuid], block=block)
            raw = getattr(raw, "value", raw)
            if raw is None:
                raise ScheduleUnreadable(
                    f"{item} could not be read at the pinned block, so the reveal round "
                    f"cannot be computed. This refuses rather than treating it as zero: zero "
                    f"is a legitimate answer for several of these, so an unread one spelled "
                    f"the same way produces arithmetic that is sound and wrong, and the "
                    f"failure shows up an epoch later as a commit nobody opens."
                )
            values[name] = int(raw)
        return values

    def seal(self, uids, values, hotkey):
        """Seal exactly these integers for the round the chain will open them at."""
        import bittensor_core as core

        block = int(self.client.block)
        schedule = self._schedule(block)

        commit, reveal_round = core.get_encrypted_commit_v2(
            uids=list(uids),
            weights=list(values),
            hotkey=bytes(hotkey.public_key),
            version_key=self.version_key,
            current_block=block,
            block_time=float(self.client.block_time()),
            **schedule,
        )
        return commit, reveal_round

    def send_context(self, address):
        """What a person needs to find this exact extrinsic later, read just before the
        send. Each value is best effort and ``None`` when it cannot be read; this never
        raises. ``account_nonce_at_head`` is ``System.Account`` nonce at the head, which
        does not count extrinsics still waiting in the pool. ``era_period`` is
        bittensor's default mortality, ASSUMED because ``submit_call`` is not given
        another. It is not read from the signed extrinsic, so it is a recorded
        assumption, not proof that the extrinsic cannot still land.
        ``submit_call`` returns only after inclusion and exposes no extrinsic hash
        before that, so none is recorded."""
        context = {"account_nonce_at_head": None, "head_at_send": None, "era_period": None}
        try:
            account = self.client.query(("System", "Account"), [address])
            account = getattr(account, "value", account)
            context["account_nonce_at_head"] = int(account["nonce"])
        except Exception:  # noqa: BLE001
            pass
        try:
            context["head_at_send"] = int(self.client.block)
        except Exception:  # noqa: BLE001
            pass
        try:
            from bittensor.settings import DEFAULT_ERA_PERIOD

            context["era_period"] = int(DEFAULT_ERA_PERIOD)
        except Exception:  # noqa: BLE001
            pass
        return context

    def submit(self, commit, reveal_round, netuid):
        """Compose the one permitted call and submit it."""
        from bittensor._generated import calls

        # Asked before anything is built, so a refusal costs nothing and no
        # half-formed extrinsic exists to be sent by mistake.
        permitted(MODULE, CALL)

        call = getattr(calls.SubtensorModule, CALL)(
            netuid=netuid,
            mecid=self.mecid,
            commit=commit,
            reveal_round=reveal_round,
            commit_reveal_version=self.commit_reveal_version,
        )
        if self.commit_identity is not None:
            # Nested calls must be composed with the submission endpoint's
            # metadata. The SDK cannot encode a generated Call tuple as RuntimeCall.
            debug(
                "composing proxy inner call=%s netuid=%d mecid=%d", CALL, netuid, self.mecid
            )
            inner = self.submit_client.compose(call)
            call = calls.Proxy.proxy(
                real=self.commit_identity.ss58_address, force_proxy_type=None, call=inner
            )
            # Checked on the composed call: its `real` must be the independently configured
            # commit identity, and its inner call must be allowlisted.
            permitted_proxy_wrapper(
                call.params["real"], self.commit_identity.ss58_address, MODULE, CALL
            )
        try:
            # The call itself, not composed bytes. `compose` returns CallBytes,
            # which this rejects, and the rejection reads as a type error rather
            # than as anything about the chain.
            result = self.submit_client.submit_call(call, self.wallet)
        except Exception as error:
            return {
                "error": f"{type(error).__name__}: {error}",
                "block": None,
                "indeterminate": True,
            }

        error = getattr(result, "error", None)
        if error:
            message = getattr(result, "message", "") or ""
            return {"error": f"{error} {message}".strip(), "block": None}
        extrinsic_id = getattr(result, "extrinsic_id", None)
        outcome = {
            "error": None,
            "block": _block_from_extrinsic_id(extrinsic_id),
            "extrinsic_index": _extrinsic_index_from_id(extrinsic_id),
        }
        if self.commit_identity is not None:
            # The outer extrinsic succeeding says nothing about the wrapped commit: Proxy.proxy
            # reports the inner result only in its ProxyExecuted event. Read it ourselves rather
            # than rely on the library to convert it (not verified for submit_call).
            status, inner_error = classify_inner(getattr(result, "events", None))
            if status == INNER_ERR:
                # Included and charged, commit NOT made: a definite refusal that cost a fee.
                return {
                    "error": f"the proxied commit failed inside Proxy.proxy: {inner_error}",
                    "block": outcome["block"],
                    "inner_failed": True,
                    "fee_spent": True,
                }
            if status != INNER_OK:
                return {
                    "error": "the extrinsic was included but its Proxy.ProxyExecuted result could not "
                    "be read from the returned events, so whether the wrapped commit happened is unknown",
                    "block": outcome["block"],
                    "indeterminate": True,
                }
        return outcome

    def submit_direct(self, uids, values, netuid):
        """Compose and submit the open weight-setting call through the supplied client."""
        from bittensor._generated import calls

        # Asked before anything is built, identically to submit() above.
        permitted(DIRECT_MODULE, DIRECT_CALL)

        call = getattr(calls.SubtensorModule, DIRECT_CALL)(
            netuid=netuid,
            mecid=self.mecid,
            dests=list(uids),
            weights=list(values),
            version_key=self.version_key,
        )
        if self.commit_identity is not None:
            # Compose only the inner call; submit_call still takes a generated
            # outer Call and performs signing/submission itself.
            debug(
                "composing proxy inner call=%s netuid=%d mecid=%d seats=%d",
                DIRECT_CALL, netuid, self.mecid, len(uids),
            )
            inner = self.submit_client.compose(call)
            call = calls.Proxy.proxy(
                real=self.commit_identity.ss58_address, force_proxy_type=None, call=inner
            )
            permitted_proxy_wrapper(
                call.params["real"], self.commit_identity.ss58_address, DIRECT_MODULE, DIRECT_CALL
            )
        try:
            # Inclusion and finalization are requested explicitly. A lost response may
            # follow a successful send, so it must leave the durable intent open.
            result = self.submit_client.submit_call(
                call, self.wallet, wait_for_inclusion=True, wait_for_finalization=True
            )
        except Exception as error:
            return {
                "error": f"{type(error).__name__}: {error}",
                "block": None,
                "indeterminate": True,
            }

        extrinsic_id = getattr(result, "extrinsic_id", None)
        outcome = {
            "error": None,
            "block": _block_from_extrinsic_id(extrinsic_id),
            "extrinsic_index": _extrinsic_index_from_id(extrinsic_id),
            "block_hash": getattr(result, "block_hash", None),
        }
        error = getattr(result, "error", None)
        if error or getattr(result, "success", True) is not True:
            outcome["error"] = (
                f"{error or 'unsuccessful submission'} {getattr(result, 'message', '')}".strip()
            )
            # Without an inclusion reference even SDK error results may represent
            # transport uncertainty. Only an included dispatch failure is definite.
            outcome["indeterminate"] = outcome["block"] is None
            return outcome
        if self.commit_identity is not None:
            status, inner_error = classify_inner(getattr(result, "events", None))
            if status == INNER_ERR:
                outcome.update(
                    error=f"the proxied weights failed: {inner_error}",
                    inner_failed=True,
                    fee_spent=True,
                )
            elif status != INNER_OK:
                outcome.update(error="Proxy.ProxyExecuted result is unreadable", indeterminate=True)
        return outcome

    def read_map(self, name, netuid):
        """Every commit a storage map holds, for the read-back after publishing.

        The ciphertext of each entry, not the row's key and not the row's raw
        value either. ``read_stored_commits`` compares what this returns
        against the exact bytes we just sealed (``commit in stored``), and
        neither a storage key nor a whole row's value is that.

        A row's key is whatever the runtime indexes the row by, not the
        commit -- returning keys means the read-back can never find the
        commit it is looking for, no matter how healthy the publication
        actually was.

        A row's VALUE is a QUEUE, not a single commit either: verified
        against a real chain in ``verification/chain_commit_spike.py``'s own
        ``find_commit()``, each row of ``TimelockedWeightCommits`` (and the
        legacy maps read_stored_commits also consults) holds a list of
        ``(hotkey, commit_block, ciphertext, reveal_round)`` entries, because
        more than one commit can land in the same row. Returning the queue
        wholesale compares our sealed bytes against a list of 4-tuples, which
        can never match either -- the same NotOnChain failure this method
        exists to avoid, one layer deeper. So every row is flattened and only
        each entry's ciphertext survives.

        Unwrapped and normalized the same way that same verification script's
        own ``as_bytes`` does: a row's value, and a ciphertext within it, can
        each arrive plain or wrapped under ``.value`` the way ``_schedule``
        above already has to handle, and a storage blob itself can arrive as
        a hex string, a raw byte sequence, or a list of ints -- none of which
        compares equal to the ``bytes`` object ``core.get_encrypted_commit_v2``
        handed us to seal, even when every byte agrees.
        """
        commits = []
        for _, row in self.client.query_map((MODULE, name), [netuid]):
            row = getattr(row, "value", row)
            for entry in row or []:
                commits.append(_as_bytes(getattr(entry[2], "value", entry[2])))
        return commits


def _as_bytes(value):
    """A storage blob, normalized to ``bytes``.

    Matches ``verification/chain_commit_spike.py``'s own ``as_bytes`` exactly:
    a blob can arrive as a hex string, a raw byte sequence, or a list of
    decoded ints, and only one of those three is directly comparable to the
    ``bytes`` this validator sealed and submitted.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    return bytes(bytearray(int(x) for x in value))
