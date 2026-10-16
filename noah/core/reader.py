"""Read the validator's registration, publication and weight state.

Bind historical queries to explicit blocks. Preserve the difference between
missing values and failed reads, and between transport failures and programming
errors. Commit and direct-weight scans filter by the configured identity and
subnet; a generic success event from another seat is not publication evidence."""

from ..chain.emissions import netuid_and_mechanism
from .settings import SettingsError


class ChainUnreadable(SettingsError):
    """A read this validator depends on could not be answered."""


def unwrap(result):
    """The value, whether it arrived wrapped or bare.

    Defaults to the result itself. Never to None, and never to an empty list.
    """
    return getattr(result, "value", result)


def _transport_failure_types():
    """Return recognized SDK transport exceptions without swallowing programming errors.

    Missing optional SDK exception classes are handled during discovery; query-time
    exceptions outside this set keep their original classification."""
    try:
        from bittensor.result import ChainError, RpcConnectionError, RpcPolicyError
    except ImportError:
        return ()
    return (ChainError, RpcConnectionError, RpcPolicyError)


def _storage_function_not_found_type():
    """``StorageFunctionNotFound`` itself, for `_owner_hotkey_or_absent`'s
    own except clause -- looked up the same lazy, only-on-failure way
    `_transport_failure_types` is (see that function's docstring for why:
    an ordinary successful read must never pay for, or be able to fail
    because of, an optional import this method does not need to make, and
    for why this catches ``ImportError`` rather than the narrower
    ``ModuleNotFoundError``).
    """
    try:
        from bittensor._transport.errors import StorageFunctionNotFound
    except ImportError:
        return ()
    return StorageFunctionNotFound


class ChainReader:
    """The six reads, against a real node.

    Takes an already-connected client rather than connecting itself, so a caller
    decides what it is talking to and this cannot quietly reach a chain nobody
    asked for.
    """

    MODULE = "SubtensorModule"

    def __init__(self, client):
        self.client = client

    def close(self):
        """Close the underlying connection or connections. Cleanup failures remain visible."""
        self.client.close()

    def _query(self, item, params=None, block=None):
        try:
            return unwrap(self.client.query((self.MODULE, item), params, block=block))
        except _transport_failure_types() as error:
            raise ChainUnreadable(
                f"could not read {item} from the chain "
                f"({type(error).__name__}; the error text is not shown). "
                f"Nothing is assumed from a failed read: this validator refuses rather than "
                f"treating an unanswered question as a permitting answer."
            ) from None

    def _query_map(self, item, params=None):
        try:
            return self.client.query_map((self.MODULE, item), params)
        except _transport_failure_types() as error:
            raise ChainUnreadable(
                f"could not read {item} from the chain "
                f"({type(error).__name__}; the error text is not shown). "
                f"Nothing is assumed from a failed read: this validator refuses rather than "
                f"treating an unanswered question as a permitting answer."
            ) from None

    def current_block(self):
        try:
            return int(self.client.block)
        except _transport_failure_types() as error:
            raise ChainUnreadable(
                f"could not read the chain's height "
                f"({type(error).__name__}; the error text is not shown)."
            ) from None

    def seat_of(self, hotkey, netuid):
        """The seat this hotkey holds, or None if it holds none.

        None means "no seat", which is a real answer. It is returned only after
        the seat map has been read successfully, so it is never confused with a
        read that failed; that raises instead.
        """
        rows = self._query_map("Keys", [netuid])
        for uid, holder in rows:
            if str(unwrap(holder)) == str(hotkey):
                return int(unwrap(uid))
        return None

    def last_update(self, uid, netuid):
        stamps = [int(x) for x in self._query("LastUpdate", [netuid])]
        if uid >= len(stamps):
            raise ChainUnreadable(
                f"seat {uid} is past the end of this subnet's activity list, which holds "
                f"{len(stamps)}. Either the seat was read from a different subnet or the "
                f"list was read from a different one."
            )
        return stamps[uid]

    def rate_limit(self, netuid):
        return int(self._query("WeightsSetRateLimit", [netuid]))

    def commit_reveal(self, netuid):
        # Not `bool(...)`. `check_commit_reveal_enabled` downstream checks
        # `enabled is not True`, the same identity check `subnet_exists` above
        # uses and for the same reason: every wire form of "not decoded" is a
        # non-empty string or a raw 1, and all of those are truthy. `bool(...)`
        # here would turn every one of them into True, which is exactly the
        # value that check is built to refuse -- coercing here would make that
        # downstream check unable to see anything but a real True or a real
        # False ever again.
        return self._query("CommitRevealWeightsEnabled", [netuid])

    def tempo(self, netuid, block=None):
        """Read subnet tempo at the specified block, or current state when no block is given."""
        return int(self._query("Tempo", [netuid], block=block))

    def activity_cutoff_factor_milli(self, netuid):
        return int(self._query("ActivityCutoffFactorMilli", [netuid]))

    def historical_probe(self, depth):
        """Whether this node can still answer a read `depth` blocks back.

        A pruned node answers events from long ago and refuses state, so the
        probe asks for STATE at depth rather than for a block. Asking the
        cheaper question is how a node that cannot serve history passes.

        Returns the query's own answer rather than a hardcoded True, because
        `check_reads_history` downstream already refuses a falsy answer
        (`if not answer`) on the understanding that a pruned node can answer
        with None or a default zero INSTEAD of raising -- that is the whole
        reason this probe exists rather than just trying and catching. A
        hardcoded True here would mean that call always completed without an
        exception, which throws away the one piece of information the caller's
        check needs and answers True for a node that structurally cannot serve
        the read.
        """
        target = self.current_block() - int(depth)
        if target < 1:
            return True  # the chain is younger than the depth asked for
        try:
            return unwrap(self.client.query((self.MODULE, "Tempo"), [0], block=target))
        except _transport_failure_types():
            return False

    def live_uids(self, netuid):
        """Every seat currently registered on the subnet, in order."""
        rows = self._query_map("Keys", [netuid])
        return sorted(int(unwrap(uid)) for uid, _ in rows)

    def _owner_hotkey_or_absent(self, netuid):
        """Read the subnet owner hotkey, distinguishing absent metadata from transport failure."""
        try:
            return str(self._query("SubnetOwnerHotkey", [netuid]))
        except _storage_function_not_found_type():
            return None

    def owner_uids(self, netuid):
        """Identify owner-controlled seats through both coldkey ownership and owner hotkey."""
        owner_coldkey = str(self._query("SubnetOwner", [netuid]))
        owner_hotkey = self._owner_hotkey_or_absent(netuid)

        owned = set()
        for uid, holder in self._query_map("Keys", [netuid]):
            seat, hotkey = int(unwrap(uid)), str(unwrap(holder))
            if owner_hotkey and hotkey == owner_hotkey:
                owned.add(seat)
                continue
            coldkey = str(self._query("Owner", [hotkey]))
            if coldkey == owner_coldkey:
                owned.add(seat)
        return owned

    def committed_in_period(self, netuid, period, tempo, hotkey=None, head=None):
        """Return identities with commit events in the requested period and subnet.

        Inspect events because revealed commits may already have left temporary storage.
        Decode packed mechanism indexes, bound the scan by head when supplied, and refuse
        an incomplete negative result. Another validator's commit remains distinguishable
        from this validator's publication."""
        return self._commit_authors_in_blocks(
            netuid,
            period * tempo,
            (period + 1) * tempo - 1,
            hotkey=hotkey,
            head=head,
            what=f"period {period}",
            span_noun="period",
        )

    def committed_in_block_range(self, netuid, from_block, to_block, hotkey=None, head=None):
        """Scan an inclusive block range for commits from the requested identity and subnet.

        Bound reads by head and refuse unreadable evidence; another identity's commit is
        not confirmation of this validator's publication."""
        return self._commit_authors_in_blocks(
            netuid,
            int(from_block),
            int(to_block),
            hotkey=hotkey,
            head=head,
            what=f"block range {int(from_block)} to {int(to_block)}",
            span_noun="range",
        )

    def commit_blocks_in_block_range(self, netuid, from_block, to_block, hotkey, head=None):
        """The sorted block numbers, inclusive range, at which `hotkey` has a
        commit event on this netuid. Same scan, same `head` bound, same
        `ChainUnreadable` rules as `committed_in_block_range` (they share one
        implementation); returns WHERE, not just whether, so a caller holding
        a record of which day each of its own commits belongs to (the
        per-day commit ledger, `noah.core.commit_ledger`) can set aside the
        blocks that are provably another day's. `hotkey` is required: an
        unfiltered list of blocks is not a question this method answers."""
        return self._commit_authors_in_blocks(
            netuid,
            int(from_block),
            int(to_block),
            hotkey=hotkey,
            head=head,
            what=f"block range {int(from_block)} to {int(to_block)}",
            span_noun="range",
            want_blocks=True,
        )

    def _commit_authors_in_blocks(
        self, netuid, first, last, hotkey, head, what, span_noun, want_blocks=False
    ):
        COMMIT_EVENTS = ("WeightsCommitted", "TimelockedWeightsCommitted")
        scan_last = last if head is None else min(last, int(head))
        who = set()
        authored_blocks = set()
        events_seen = 0
        unread_blocks = []
        for block in range(first, scan_last + 1):
            try:
                raw = unwrap(self.client.query(("System", "Events"), None, block=block))
            except _transport_failure_types() as error:
                unread_blocks.append((block, error))
                continue
            if not isinstance(raw, list):
                # This module's own contract is that unwrap() always yields a
                # plain list here. A `None` or otherwise malformed answer that
                # did not raise is not a documented "this block had none"; a
                # single endpoint has been measured returning a SHORTENED
                # event list without ever raising (chain/agreeing.py), so a
                # non-list answer is treated exactly like a failed read.
                unread_blocks.append(
                    (block, ValueError(f"answered with {raw!r}, not a list of events"))
                )
                continue
            events = raw
            events_seen += len(events)
            for event in events:
                inner = event.get("event") if isinstance(event, dict) else None
                if not inner or inner.get("event_id") not in COMMIT_EVENTS:
                    continue
                attributes = list(inner.get("attributes") or [])
                if len(attributes) >= 2 and netuid_and_mechanism(attributes[1])[0] == int(netuid):
                    who.add(str(attributes[0]))
                    if hotkey is not None and str(attributes[0]) == hotkey:
                        authored_blocks.add(block)
        if unread_blocks:
            bad_block, error = unread_blocks[0]
            in_progress = (
                f" ({span_noun} runs to {last}; {last - scan_last} block(s) past the live head "
                f"{head} were not scanned, not counted here)"
                if head is not None and scan_last < last
                else ""
            )
            raise ChainUnreadable(
                f"{len(unread_blocks)} of {scan_last - first + 1} block(s) in {what} "
                f"({first} to {scan_last}{in_progress}) could not be read, starting at block "
                f"{bad_block} ({type(error).__name__}: {error}). The rest of the scanned range "
                f"answered, so this is not a period nobody published in; it was read "
                f"INCOMPLETELY, and the commit that matters most may be sitting in exactly the "
                f"block that failed. A partial read is refused rather than reported as a "
                f"smaller sample."
            )
        # A period entirely past the live head has nothing to scan yet -- not
        # an incomplete read of a producing chain, just a period that has not
        # started. Only a range that was actually attempted can be judged
        # suspicious for answering with zero events.
        if scan_last >= first and events_seen == 0:
            raise ChainUnreadable(
                f"read every block of {what} from {first} to {scan_last} and saw no "
                f"events at all. A producing chain always has some, so this is a reader that "
                f"reached nothing rather than a period nobody published in. Reporting the "
                f"second from the first is how a healthy publication gets called missing."
            )
        if want_blocks:
            return sorted(authored_blocks)
        return {h for h in who if hotkey is None or h == hotkey}

    def subnet_exists(self, netuid):
        """Whether this subnet has been created on this chain."""
        return self._query("NetworksAdded", [netuid]) is True

    def weights_set_for_seat(self, netuid, uid, from_block, to_block):
        """Check for this seat's WeightsSet event in the requested subnet and block range.

        A positive observed event is evidence of existence. A negative result requires a
        complete readable range; missing events or failed blocks are not proof of absence.
        Stored-content comparison is a separate requirement for uncertain-send recovery."""
        seen, unread_blocks, matches = self._scan_weights_set(
            netuid, uid, from_block, to_block, first_only=True
        )
        if matches:
            return True
        if unread_blocks:
            bad_block, error = unread_blocks[0]
            raise ChainUnreadable(
                f"{len(unread_blocks)} of {to_block - from_block + 1} block(s) between "
                f"{from_block} and {to_block} could not be read, starting at block "
                f"{bad_block} ({type(error).__name__}: {error}). The rest of the window "
                f"answered, so this is not an empty window; it was read INCOMPLETELY, and "
                f"the application that matters may be sitting in exactly the block that "
                f"failed."
            )
        if seen == 0:
            return None
        return False

    def _scan_weights_set(self, netuid, uid, from_block, to_block, *, first_only):
        """Scan this seat's WeightsSet events, stopping early only when requested."""
        seen = 0
        unread_blocks = []
        matches = []
        for block in range(from_block, to_block + 1):
            try:
                raw = unwrap(self.client.query(("System", "Events"), None, block=block))
            except _transport_failure_types() as error:
                unread_blocks.append((block, error))
                continue
            if not isinstance(raw, list):
                # See committed_in_period's identical check: a non-list answer
                # that did not raise is not documented as "no events here",
                # and a single endpoint returning a shortened list without
                # raising is measured, not hypothetical (chain/agreeing.py).
                unread_blocks.append(
                    (block, ValueError(f"answered with {raw!r}, not a list of events"))
                )
                continue
            events = raw
            seen += len(events)
            for event in events:
                inner = event.get("event") if isinstance(event, dict) else None
                if not inner or inner.get("event_id") != "WeightsSet":
                    continue
                attributes = list(inner.get("attributes") or [])
                if (
                    len(attributes) >= 2
                    and netuid_and_mechanism(attributes[0])[0] == int(netuid)
                    and int(attributes[1]) == int(uid)
                ):
                    matches.append(block)
                    break
            if first_only and matches:
                break
        return seen, unread_blocks, matches

    def weights_set_blocks_for_seat(self, netuid, uid, from_block, to_block):
        """Return all matching WeightsSet block numbers in the inclusive range.

        Each candidate may require stored-content comparison; existence alone cannot
        identify which uncertain vector a later transaction applied."""
        seen, unread_blocks, matches = self._scan_weights_set(
            netuid, uid, from_block, to_block, first_only=False
        )
        if unread_blocks:
            bad_block, error = unread_blocks[0]
            raise ChainUnreadable(
                f"{len(unread_blocks)} of {to_block - from_block + 1} block(s) between "
                f"{from_block} and {to_block} could not be read, starting at block "
                f"{bad_block} ({type(error).__name__}: {error}). {len(matches)} real "
                f"match(es) were found in the part that WAS read, but this scan collects "
                f"every landing block rather than stopping at the first, and the day "
                f"actually being checked may be sitting in exactly the block that failed."
            )
        if seen == 0:
            return None
        return matches

    def weights_content_for_seat(self, netuid, uid, block):
        """Read this seat's decoded stored (uid, weight) pairs at the specified block."""
        value = self._query("Weights", [netuid, uid], block=block)
        if not isinstance(value, list):
            raise ChainUnreadable(
                f"Weights[{netuid}][{uid}] at block {block} decoded to {value!r}, not a "
                f"list of (uid, weight) pairs. An answer that arrives undecoded is exactly "
                f"the false-permitting shape this module refuses to trust (see "
                f"subnet_exists's/weights_storage_nonempty_for_seat's own docstrings for "
                f"the identical reasoning); this is reported as an unreadable storage item "
                f"rather than treated as empty."
            )
        return value

    def weights_storage_nonempty_for_seat(self, netuid, uid, block):
        """Check whether this seat's decoded weights row is genuinely nonempty.

        A present storage key containing an empty vector is not publication evidence."""
        value = self._query("Weights", [netuid, uid], block=block)
        if not isinstance(value, list):
            raise ChainUnreadable(
                f"Weights[{netuid}][{uid}] at block {block} decoded to {value!r}, not a "
                f"list of (uid, weight) pairs. An answer that arrives undecoded is exactly "
                f"the false-permitting shape this module refuses to trust (see "
                f"subnet_exists's own docstring for the identical reasoning applied to a "
                f"different storage item); this is reported as an unreadable storage item "
                f"rather than treated as empty."
            )
        return len(value) > 0

    def reveal_of_commit(self, netuid, hotkey, commit_block, rate_limit):
        """Find the matching hotkey/subnet reveal within the commit's search window.

        Decode packed mechanism indexes. A missing match is not proof of failure before
        the window closes, and an incomplete negative scan is refused. The caller must
        interpret the result alongside the current head and publication identity."""
        unread_blocks = []
        for block in range(commit_block, commit_block + int(rate_limit)):
            try:
                raw = unwrap(self.client.query(("System", "Events"), None, block=block))
            except _transport_failure_types() as error:
                unread_blocks.append((block, error))
                continue
            if not isinstance(raw, list):
                # See committed_in_period's identical check: a non-list answer
                # that did not raise is not documented as "no events here",
                # and a single endpoint returning a shortened list without
                # raising is measured, not hypothetical (chain/agreeing.py).
                unread_blocks.append(
                    (block, ValueError(f"answered with {raw!r}, not a list of events"))
                )
                continue
            events = raw
            for event in events:
                inner = event.get("event") if isinstance(event, dict) else None
                if not inner or inner.get("event_id") != "TimelockedWeightsRevealed":
                    continue
                attributes = list(inner.get("attributes") or [])
                if (
                    len(attributes) >= 2
                    and netuid_and_mechanism(attributes[0])[0] == int(netuid)
                    and str(attributes[1]) == str(hotkey)
                ):
                    return block
        if unread_blocks:
            bad_block, error = unread_blocks[0]
            raise ChainUnreadable(
                f"{len(unread_blocks)} of {int(rate_limit)} block(s) between {commit_block} "
                f"and {commit_block + int(rate_limit) - 1} could not be read, starting at "
                f"block {bad_block} ({type(error).__name__}: {error}). The rest of the "
                f"window answered, so this is not an unrevealed commit; it was read "
                f"INCOMPLETELY, and the reveal that matters may be sitting in exactly the "
                f"block that failed."
            )
        return None

    def epoch_consensus(self, netuid, block=None):
        """Read the selected consensus, refusing unknown or unreadable values.

        Runtimes predating SubnetEpochConsensus use Yuma; only metadata proving
        the storage item absent allows that compatibility fallback.
        """
        try:
            value = self._query("SubnetEpochConsensus", [netuid], block=block)
        except _storage_function_not_found_type():
            return "Yuma"
        if isinstance(value, dict) and len(value) == 1:
            value = next(iter(value))
        if value not in ("Null", "Yuma"):
            raise ChainUnreadable("SubnetEpochConsensus is not a decoded Null or Yuma value")
        return value

    def has_validator_permit(self, netuid, uid):
        """Whether this seat's weights will actually be accepted.

        A seat without a permit may still COMMIT. The permit is checked at
        reveal, not at commit, so the failure arrives an epoch late and wearing
        the wrong clothes.
        """
        permits = self._query("ValidatorPermit", [netuid])
        permits = list(permits) if permits is not None else []
        if uid >= len(permits):
            raise ChainUnreadable(
                f"seat {uid} is past the end of this subnet's permit list, which holds "
                f"{len(permits)}. The seat and the list were read from different subnets."
            )
        return permits[uid] is True

    def owner_uid(self, netuid):
        """The seat the subnet owner's hotkey holds, or None.

        That seat may set weights whatever its permit says
        (`subnets/weights.rs:1205-1210`), so it is the one exemption to the
        permit rule and has to be read rather than assumed absent. "Assumed
        absent" applies only to a runtime with no such storage item at all;
        a read that merely failed refuses instead of handing back None, or
        this seat's exemption could be missed on a transient failure that has
        nothing to do with whether the seat exists.
        """
        owner_hotkey = self._owner_hotkey_or_absent(netuid)
        if owner_hotkey is None:
            return None
        for uid, holder in self._query_map("Keys", [netuid]):
            if str(unwrap(holder)) == owner_hotkey:
                return int(unwrap(uid))
        return None


class Connection:
    """A client wearing the shape the chain guard expects.

    The guard asks a connection for `genesis_hash()`. The client this project
    runs offers the same fact under a different name and shape, so the adapting
    happens here rather than by widening the guard.

    That direction matters. The guard is what refuses to run against the live
    network, and its tests pin exactly how it asks. Loosening it to accept
    several shapes would mean a client answering none of them reads as a chain
    that could not be identified, which is a refusal, or worse reads as a match.
    Wiring belongs in the wiring.
    """

    def __init__(self, client):
        self.client = client

    def genesis_hash(self):
        """The genesis block's hash, or None if the node answered with nothing."""
        info = self.client.block_info(0)
        if info is None:
            return None
        return str(info.hash)
