"""Answer chain queries only when independently configured sources agree.

Reads are bound to block identity. Hash spelling is normalized, while state and
event disagreements are refused. Endpoint diversity is a configuration requirement;
different URL strings alone do not prove independently operated infrastructure."""

from .sources import (
    NotEnoughSources,
    Refusal,
    SourceDisagreement,
    agreed,
    check_source_at_block,
    comparable_hash,
    scrub_endpoint_text,
)


class SourceFailed(Refusal):
    """A source raised rather than answering. Never an ordinary exception."""


class MissingEndpoint(Refusal):
    """A source has no genuine declared endpoint. Never guessed or blank."""


class SharedClient(Refusal):
    """Two source names are the same client object. Never two sources."""


def _blank(endpoint):
    """True for anything that is not a real, distinguishing endpoint string."""
    return not isinstance(endpoint, str) or not endpoint.strip()


class AgreeingChain:
    """Every question the runner asks, asked of every source and compared.

    ``sources`` maps a name to a client. ``endpoints`` maps the same names to the
    endpoint each one actually talks to, because two names for one endpoint is
    one source and only the endpoint knows that. Every name in ``sources`` must
    have a genuinely non-blank entry in ``endpoints``: a source built without one
    (absent, ``None``, ``""``, or a whitespace-only string) used to fall back to
    its own name as its identity, which is how two clients on one real node
    could be made to look independent. Missing or blank is refused here instead
    (see ``_blank`` for exactly what counts), and so is handing two
    names the literal same client object -- a label was never what made two
    sources independent, and a shared object cannot be independent of itself no
    matter what two strings ``endpoints`` gives it.

    ``canary`` is how a source proves it is answering honestly about a given
    block. It defaults to asking the client itself, which every client here
    implements; passing one is for a client that cannot.
    """

    def __init__(self, sources, endpoints, canary=None):
        self._sources = dict(sources)
        self._endpoints = dict(endpoints)
        missing = sorted(name for name in self._sources if _blank(self._endpoints.get(name)))
        if missing:
            raise MissingEndpoint(
                f"{', '.join(missing)} named as a source but given no genuine entry in "
                f"endpoints (absent, or a blank value such as None, an empty string, or "
                f"whitespace only). A source with no real declared endpoint used to fall back "
                f"to its own name as its identity -- first by never being in the mapping at "
                f"all, then, once that was closed, by being in it with a value too blank to "
                f"use -- so two clients on the exact same real endpoint could pass as "
                f"independent purely because their dict keys differed. Refused here, at "
                f"construction, so no caller can build an AgreeingChain that could ever be "
                f"fooled that way."
            )
        by_identity = {}
        for name, client in sorted(self._sources.items()):
            by_identity.setdefault(id(client), []).append(name)
        shared = sorted(names for names in by_identity.values() if len(names) > 1)
        if shared:
            names = shared[0]
            raise SharedClient(
                f"{' and '.join(names)} are the literal same client object under two "
                f"different names. Two names and two endpoint strings do not make one "
                f"connection into two sources; whatever fault that one client has, both names "
                f"have it identically, at the identical moment, which is exactly what "
                f"agreement is meant to catch and cannot when there is only one client "
                f"answering both questions."
            )
        self._canary = canary or (lambda client, block: client.canary(block))
        self.checks = 0

    def close(self):
        """Close the underlying connection or connections. Cleanup failures remain visible."""
        errors = []
        for source in self._sources.values():
            try:
                source.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    @property
    def endpoints(self):
        """The source-name-to-endpoint mapping this chain was built with.

        A copy, not the live dict, so a caller cannot mutate what this
        chain actually checks agreement against. A caller that wants to
        name every contributing endpoint in a run's provenance record --
        `noah/chain/runner.py`'s `run_day` takes exactly that as its own
        `sources` argument -- has no single `.endpoint` to read the way a
        single-source chain like `LiveRunnerChain` does, and needs this
        instead.
        """
        return dict(self._endpoints)

    # -- the four questions ------------------------------------------------

    def head_block(self):
        """The lowest head any source can answer about.

        Sources are legitimately a block or two apart, so requiring agreement on
        the head would refuse constantly and for no reason. The lowest is the
        height every source can speak to, and reading past it would be reading
        past what one of them knows.
        """
        heads = self._ask_all("the chain head", lambda client: client.head_block())
        return min(heads.values())

    def block_hash(self, block):
        """Return the block hash only when sources agree on its canonical value."""
        return self._agreed(
            "blockHash", block, lambda client: comparable_hash(client.block_hash(block))
        )

    def genesis_hash(self):
        """Block 0's hash, agreed by every source, with no state canary.

        The canary every other read starts with is a read of the chain's
        timestamp at that block, and block 0 has none: the timestamp is first
        set by block 1's inherent, so the canary reads 0 there and every honest
        source would be refused. A node that keeps only recent state has also
        discarded that state, though it still holds block 0's hash. The hash
        needs no such proof, because it is a header fact that the caller then
        checks against a permanent, known value (:mod:`noah.chain.genesis_times`).

        What this still requires is what agreement always requires: every source
        answers, they resolve to at least two distinct endpoints, and they give
        the same hash after canonicalising its spelling.
        """
        answers = self._ask_all(
            "genesisHash", lambda client: comparable_hash(client.block_hash(0))
        )
        endpoints = {self._endpoints[name] for name in answers}
        if len(endpoints) < 2:
            raise NotEnoughSources(
                f"genesisHash: {len(answers)} source(s) answered but they resolve to "
                f"{len(endpoints)} distinct endpoint(s), and agreement requires at least 2."
            )
        values = list(answers.values())
        if any(value != values[0] for value in values[1:]):
            detail = ", ".join(f"{name}={value!r}" for name, value in sorted(answers.items()))
            raise SourceDisagreement(f"genesisHash: sources disagree: {detail}")
        return values[0]

    def block_timestamp(self, block):
        # zero_is_impossible, because a block timestamp is milliseconds since the
        # epoch and no block we read has one. The canary already refuses a source
        # that zeros wholesale at this block; this is the second layer, for a
        # source honest about the canary and zeroing this field specifically --
        # which is the shape W-08 records live on the secondary endpoint, where
        # pruned state comes back as 0 with no error.
        #
        # blockHash and blockEvents do NOT get it: a hash is not an integer so the
        # check cannot fire on one, and an empty event list is an honest answer
        # for a quiet block.
        return self._agreed(
            "blockTime",
            block,
            lambda client: client.block_timestamp(block),
            zero_is_impossible=True,
        )

    def events(self, block):
        return self._agreed("blockEvents", block, lambda client: client.events(block))

    def extrinsics(self, block):
        """Read this block's decoded extrinsics, preserving the indexes used by events."""
        return self._agreed("blockExtrinsics", block, lambda client: client.extrinsics(block))

    def incentive(self, block):
        return self._agreed("incentive", block, lambda client: client.incentive(block))


    def seat_hotkeys(self, block):
        """The seat map, agreed WHOLE rather than seat by seat.

        Two sources returning the same 255 seats where one also returns a 256th
        agree on every seat they share, and the map's completeness is exactly
        what the pay attribution depends on: a position in the emission vector
        is only a seat number while the map is the declared range.
        """
        return self._agreed("keys", block, lambda client: client.seat_hotkeys(block))

    def registration_blocks(self, block):
        return self._agreed(
            "blockAtRegistration", block, lambda client: client.registration_blocks(block)
        )

    def subnetwork_n(self, block):
        return self._agreed("subnetworkN", block, lambda client: client.subnetwork_n(block))

    def mechanism_count(self, block):
        return self._agreed(
            "mechanismCountCurrent", block, lambda client: client.mechanism_count(block)
        )

    def hotkey_owners(self, hotkeys, block):
        """Who owns each paid hotkey, agreed as one map.

        Asked for a specific list rather than the whole map, because only the
        hotkeys that earned something need an owner, and asking for the rest
        would be one read per seat per epoch for nothing. ``hotkeys`` is
        consumed once into a tuple, matching ``run_day``'s own call shape
        (``chain.hotkey_owners(paid_hotkeys, block)`` at
        ``noah/chain/runner.py:264``) and this tree's ``LiveRunnerChain.hotkey_owners``,
        which also takes an iterable of hotkeys and a block.
        """
        wanted = tuple(hotkeys)
        return self._agreed("owner", block, lambda client: client.hotkey_owners(wanted, block))

    def subnet_owner(self, block):
        return self._agreed("subnetOwner", block, lambda client: client.subnet_owner(block))

    def owner_at(self, *, hotkey, block, block_hash):
        """Read ownership at the caller-specified historical block hash, never at head.

        An unavailable ownership query is a read refusal, not proof of absent ownership."""
        wanted = hotkey
        return self._agreed(
            f"owner:{wanted}",
            block,
            lambda client: client.owner_at(hotkey=wanted, block_hash=block_hash),
        )

    # -- and the four a holding takes ---------------------------------------
    #
    # Added after the six above, and the gap between them is the finding rather
    # than the fix: this class went from answering four of ten to answering ten
    # of fourteen within a day, because holdings arrived after the six landed and
    # nothing declared what the full set was. A test now derives the question
    # list from the runner and checks every client against it, so the next read
    # added fails there rather than at the first block that needs it.

    def staking_hotkeys(self, coldkey, block):
        """Which hotkeys a wallet stakes to, agreed as a whole list.

        Compared entire rather than name by name, for the same reason as the
        seat map: two sources returning the same nine hotkeys where one also
        returns a tenth agree on every name they share, and a holding read from
        the shorter list is short by whatever sits on the hotkey that was
        dropped.
        """
        return self._agreed(
            "stakingHotkeys", block, lambda client: client.staking_hotkeys(coldkey, block)
        )

    def holding_maps_declared(self, block):
        """Which old share maps the runtime declares at this block, agreed.

        Runtime 473 removed ``Alpha``. Each source decides from the chain's
        own metadata at the block's hash whether the old map exists; the two
        must say the same, or this is a ``SourceDisagreement``. Asked before
        every share read so a disagreement about the metadata cannot hide
        behind two share answers that happen to coincide.
        """
        return self._agreed(
            "holdingMapsDeclared", block, lambda client: client.holding_maps_declared(block)
        )

    def alpha_price_at(self, block, netuid, *, expected_block_hash=None):
        """A subnet's spot alpha price at exactly ``block``, only when every source
        gives the same positive whole number (issue #16). A source that cannot read
        it, or disagrees, refuses the read; no other block's price is substituted."""
        return self._agreed(
            f"alphaPrice[{netuid}]",
            block,
            lambda client: client.alpha_price_at(block, netuid=netuid, **(
                {"expected_block_hash": expected_block_hash}
                if expected_block_hash is not None else {}
            )),
            zero_is_impossible=True,
        )

    def alpha_share(self, hotkey, coldkey, block):
        self.holding_maps_declared(block)
        return self._agreed(
            f"alphaShare[{hotkey}]",
            block,
            lambda client: client.alpha_share(hotkey, coldkey, block),
        )

    def hotkey_shares(self, hotkey, block):
        self.holding_maps_declared(block)
        return self._agreed(
            f"totalHotkeyShares[{hotkey}]",
            block,
            lambda client: client.hotkey_shares(hotkey, block),
        )

    def hotkey_alpha(self, hotkey, block):
        return self._agreed(
            f"totalHotkeyAlpha[{hotkey}]",
            block,
            lambda client: client.hotkey_alpha(hotkey, block),
        )

    def coldkey_collateral(self, coldkey, block):
        """A wallet's total locked collateral on this subnet.

        The cheap half of the collateral read: one question per wallet rather
        than one per hotkey. Zero means nothing was captured and nothing was
        released for this wallet, but only once the question has been shown
        well formed, because this map's row is removed when it reaches zero and
        an empty map answers zero to a broken question too.
        """
        return self._agreed(
            f"coldkeyMinerCollateral[{coldkey}]",
            block,
            lambda client: client.coldkey_collateral(coldkey, block),
        )

    def collateral_hotkeys(self, coldkey, block):
        """Which of a wallet's hotkeys carry a collateral row, agreed whole.

        Compared entire rather than name by name, for the same reason as the
        seat map and the staking list: a sum taken over the shorter of two
        lists is short by whatever sat on the hotkey that was dropped, and here
        that shortfall would read as pay the miner never had locked.
        """
        return self._agreed(
            f"coldkeyCollateralHotkeys[{coldkey}]",
            block,
            lambda client: client.collateral_hotkeys(coldkey, block),
        )

    def collateral_arity(self, block):
        """How many keys the chain says each collateral map takes.

        The witness that works on an empty map. Every other check this package
        uses for absence needs a key known to exist, and the collateral maps are
        empty across the live network, so on today's chain there is no such key
        anywhere and the control-key idiom cannot run at all. Metadata can.
        """
        return self._agreed(
            "collateralKeyArity", block, lambda client: client.collateral_arity(block)
        )

    def miner_collateral(self, hotkey, coldkey, block):
        """One position's locked alpha, or None where no row exists.

        Absence is honest here: the map is OptionQuery (lib.rs:3265-3281, spec 450,
        was :3092-3101 at spec 447), so a missing row is a real absence rather than
        a default, and it is passed through as None rather than flattened to zero.
        """
        return self._agreed(
            f"minerCollateral[{hotkey}/{coldkey}]",
            block,
            lambda client: client.miner_collateral(hotkey, coldkey, block),
        )


    def first_emission_block(self, block):
        """Whether the subnet's start call has landed as of ``block``,
        agreed -- see ``LiveRunnerChain.first_emission_block``'s own
        docstring for what this reads and why.

        ``None`` is a genuine, agreeable answer here, not an error:
        ``sources.agreed``'s own ``(type(v), v) != (type(first), first)``
        comparison already treats two sources' ``None`` as agreement
        (``type(None) is type(None)`` and ``None == None``), the identical
        mechanism every other method here relies on for its own honest,
        non-error values -- no special case is needed for it.
        """
        return self._agreed(
            "firstEmissionBlockNumber", block, lambda client: client.first_emission_block(block)
        )


    def published_ark_pin(self, block):
        """Read the Ark address published in the subnet identity at the specified block."""
        return self._agreed(
            "publishedArkPin", block, lambda client: client.published_ark_pin(block)
        )

    # -- how every one of them is answered ---------------------------------

    def _ask_all(self, field, question):
        answers = {}
        for name, client in sorted(self._sources.items()):
            try:
                answers[name] = question(client)
            except Refusal:
                raise
            except Exception as exc:
                raise SourceFailed(
                    f"{name} failed to answer for {field}: {self._failure_text(exc)}. A source that raises is not "
                    f"a source that disagreed, and it is not one that agreed either. An "
                    f"ordinary exception here is the one shape a broad retry loop swallows, "
                    f"which would leave a run believing whatever the other source said."
                ) from None
        return answers

    def _failure_text(self, exc):
        """The exception's class and its text with every endpoint URL scrubbed.

        A source's own exception can echo the keyed URL it dialled. The message
        and the traceback chain (``from None``, so ``__cause__`` and the
        implicit context are not shown) must not carry it.
        """
        return f"{type(exc).__name__}: " + scrub_endpoint_text(exc, *self._endpoints.values())

    def _agreed(self, field, block, question, zero_is_impossible=False):
        """One fact, from every source, at one block, or a refusal.

        The canary comes first. An answer from a source that has not proved
        itself at this block is not evidence, and collecting the answers before
        checking would mean a source's wrong answer had already been compared
        against a right one before anybody asked whether it could be trusted.
        """
        accepted = []
        for name, client in sorted(self._sources.items()):
            try:
                value = self._canary(client, block)
            except Refusal:
                raise
            except Exception as exc:
                raise SourceFailed(
                    f"{name} failed its check at block {block}: {self._failure_text(exc)}."
                ) from None
            # Direct indexing, not `.get(name, name)`: __init__ already refused
            # any source missing from `endpoints`, so a silent fallback here
            # would only ever paper over that guarantee being violated some
            # other way.
            accepted.append(
                check_source_at_block(name, block, value, endpoint=self._endpoints[name])
            )
            self.checks += 1

        answers = self._ask_all(field, question)
        return agreed(field, answers, block, accepted, zero_is_impossible=zero_is_impossible)
