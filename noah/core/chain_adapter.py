"""Chain-backed daily scoring and weight publication.

The adapter builds daily fixtures from settled chain records, checks source
agreement and history completeness, enforces entry eligibility, and compares
Python and TypeScript scores. Configuration supplies the Ark identity, scoring
profile, holding sample interval, durable history and publication state paths.

Publication uses the configured signing route and retains submission evidence.
An uncertain outcome must be reconciled before another submission. The
``previous_period`` method currently refuses rather than reconstructing a prior
sealed vector, so the period runner reports that reveal check as unverified.
Operators must independently establish the commit and reveal result.
"""

from ..chain.agreeing import AgreeingChain
from ..chain.foreign_value import foreign_deposit_rule

#: The base class noah.core.loop.PERIOD_REFUSALS catches, by name, to treat
#: a genuine per-period refusal as recorded-and-retryable rather than a
#: crash (that tuple's own comment: "the same explicit, by-name discipline
#: every other entry ... already follows, applied once, at the level the
#: class hierarchy actually offers"). CanonicalScoreNotWired/
#: CanonicalScoreUnavailable below are also this, alongside
#: ChainAdapterRefused/SettingsError -- issue #212's own integration gap, a
#: Verifier found live: with no try/except of its own around
#: already_published() (period.py's own step 2), an exception that is only
#: a SettingsError propagates uncaught through run_forever's own loop
#: (which deliberately does not catch-all), crashing the unattended
#: process, rather than degrading the same way every sibling refusal
#: already does.
from ..chain.sources import Refusal, scrub_endpoint_text

#: Used below to floor a freshly-read chain genesis moment to the nearest
#: UTC midnight before it becomes day_anchor_ts -- imported rather than
#: redefined as a literal 86_400 here, so this can never silently drift
#: from noah.chain.store.Store's own identical constant.
from ..chain.ss58 import public_key_of
from ..chain.store import DAY_SECONDS, DayNotScanned, Store

#: The SAME function noah.deposits.entry_gate's scoring gate calls to decide
#: who clears entry (#112's Verifier finding). Aliased, not imported as
#: `entry_deposit_rao`, because that name is already this module's own
#: settings field / constructor parameter / local variable throughout --
#: see build_chain_adapter's own "entry_deposit_rao (#570)" comment below
#: for where this is actually used: to refuse, by name, the moment the
#: legacy NOAH_ENTRY_DEPOSIT_RAO setting disagrees with this, rather than
#: silently building an adapter whose fixture credit can differ from what
#: the gate already cleared.
from ..config.profile import entry_deposit_rao as _profile_entry_deposit_rao
from ..deposits.entry_gate import exclude_unconfirmed_entries
from ..pathtext import plain_path_text
from ..runtime_events import (
    bind as bind_event_context,
)
from ..runtime_events import (
    observe_entry_qualification,
    observe_seats,
    observe_window,
)
from ..scoring.engine import score_fixture

#: #566: reused, unparsed, to validate a real NOAH_DIALS_JSON file the exact
#: same way neurons/miner.py's own _real_dials already does -- see
#: _real_dials below for the ported copy.
from ..scoring.fixture_io import FixtureError, _parse_dials

#: The SAME single canonical NOAH_PARAMETERS_JSON reader
#: EntryDepositAmountMismatched's own cross-check already routes through
#: (there, via noah.config.profile's thin wrapper) -- used directly here
#: because the dials cross-check below (#112, DialsAmountMismatched) needs
#: the full profile (all seven scoring dial fields), not just the one
#: entry-deposit figure noah.config.profile exposes. Calling this directly
#: is the sanctioned route: noah/scoring/tests/test_single_canonical_reader.py
#: only forbids reading the NOAH_PARAMETERS_JSON env var a second time
#: outside noah.scoring.parameters itself, and explicitly names this
#: function as the correct way for another module to reach it.
from ..scoring.parameters import load_parameters as _profile_parameters
from ..scoring.parameters import load_parameters_snapshot
from .commit_ledger import CommitLedger
from .proxy_route import PublishFailureBackoff, identity_from_settings
from .publish import CommitRejected, PublicationRefused, publish_weights, publish_weights_direct
from .publish_hold import PublishHold
from .publish_intent import REASON_PREFIX, PublicationStatusUnknown, PublishIntent
from .publish_intent import _utc_now as _utc_now_iso
from .reader import ChainUnreadable
from .settings import PUBLISH_MODE_DEFAULT, PUBLISH_MODES, SettingsError
from .submitted_record import write_submitted_record
from .twin import require_agreement
from .vector import from_engine_output


def _error_text(error):
    """An error's text for a log or a returned reason: for an OS error that names a file, only the
    system's reason (its full text repeats the typed path); else the text with any URL scrubbed."""
    if isinstance(error, OSError) and error.filename is not None:
        return str(error.strerror or "(details withheld)")
    return scrub_endpoint_text(error)


class ChainAdapterRefused(SettingsError):
    """Base for every refusal this adapter raises on its own account."""


class InvalidPublishMode(ChainAdapterRefused):
    """``publish_mode`` is neither "sealed" nor "direct".

    Ticket #16 (B06), issue #56 comment 5720306237. Checked at construction,
    not at publish time: a bad value here is a configuration mistake, not a
    per-period condition, and catching it before the adapter is even usable
    is cheaper than finding it the first time `run_period` tries to act on
    it.
    """


class SingleSourceChainRefused(ChainAdapterRefused):
    """This adapter was built without a real, dual-sourced runner_chain."""


class SeatRegistryNotWired(ChainAdapterRefused):
    """There is no live reader for {coldkey: {hotkey: uid}} configured on
    this ``ChainAdapter``.

    **Not #193.** An earlier version of this refusal cited #193 for this
    gap; #193 (``gh issue view 193``: "Rework the holdings code built
    against the dead rule") is CLOSED and was about per-seat HOLDING
    AMOUNTS -- #134's own domain, not seat-ownership identity. That was a
    wrong citation, not a description of a different, still-open gap: no
    currently-open issue was found tracking the live seat-registry read
    specifically (searched by title and body via ``gh issue list``), so
    none is invented here in its place.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``.** ``noah.core.chain_adapter.build_chain_adapter``
    -- the one place production code constructs a real ``ChainAdapter`` --
    now always supplies a real ``seats_source`` (``noah.chain.seats.
    current_seats``, composed with the dual-sourced ``runner_chain``), so
    this never fires on a real deployment. It stays a real, tested guard
    against constructing a ``ChainAdapter`` any other way -- directly, or
    through a future second caller -- without one.
    """


class DayWindowNotWired(ChainAdapterRefused):
    """There is no live calendar-day block-range resolver configured on
    this ``ChainAdapter``.

    Genuinely new work as of this wiring (D-139/#436): the only precedent
    in this codebase, ``neurons/miner.py``'s own ``_day_start_block``
    (issue #478), answers an adjacent but different question for a
    different caller (see ``noah.chain.dayblocks``'s own module docstring
    for exactly how) and was never reachable from here. No issue number is
    cited for the gap itself because none tracks it yet.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``.** A real deployment always gets a real
    ``day_window`` from ``build_chain_adapter`` (``noah.chain.dayblocks.
    day_block_range``, composed with the dual-sourced ``runner_chain``), so
    this never fires on a real deployment.
    """


class DayAnchorNotWired(ChainAdapterRefused):
    """There is no ``day_anchor_ts`` configured on this ``ChainAdapter``.

    Found by NOAH-ADMIN/Testnet's own review of this exact wiring: once
    ``seats_source`` and ``day_window`` are both real, they no longer
    shield ``read()`` from reaching the point where ``self.day_anchor_ts``
    is used (``build_fixture(day_epoch_ts=self.day_anchor_ts, ...)``) --
    and a missing anchor there used to surface only as
    ``noah.chain.fixture_build``'s own generic ``MissingFromStore``
    ("the day anchor came in as None, which is not a whole number"), AFTER
    a full day's worth of chain scanning (``run_day``) had already run for
    nothing. Refused here, by name, before any of that work happens --
    matching the identical "cheapest and most likely to be wrong first"
    discipline ``build_chain_adapter``'s own endpoint checks already use.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``.** A real deployment always gets a real,
    freshly chain-read ``day_anchor_ts`` from ``build_chain_adapter``
    (``noah.chain.dayblocks.day_anchor``, floored to the nearest UTC
    midnight the same way ``noah.chain.store.Store`` floors it internally),
    so this never fires on a real deployment.
    """


class CanonicalScoreNotWired(ChainAdapterRefused, Refusal):
    """There is no local canonical-score reader configured on this
    ``ChainAdapter`` (issue #212's own phase-two fix).

    **Also a ``Refusal`` (``noah.chain.sources``), not only a
    ``ChainAdapterRefused``/``SettingsError`` -- a real integration gap an
    independent Verifier found live in this fix's first version.**
    ``noah.core.period.run_period`` calls ``already_published`` at step 2
    with no ``try``/``except`` of its own, and ``noah.core.loop.tick``'s
    own ``except PERIOD_REFUSALS`` clause is what turns a genuine per-
    period refusal into a recorded, retryable outcome rather than an
    uncaught crash of the whole unattended process (``run_forever``'s own
    docstring: this loop deliberately does not catch-all). A plain
    ``ChainAdapterRefused`` matches none of ``PERIOD_REFUSALS``'s named
    members, so it would have propagated straight out of the loop --
    exactly the crash ``PERIOD_REFUSALS`` names ``Refusal`` (rather than
    enumerating every one of the forty-plus existing subclasses one at a
    time) to prevent for this whole family. Inheriting from both keeps
    every existing ``ChainAdapterRefused``/``SettingsError`` catch site
    working unchanged (this is still one, first in the MRO) while also
    making this a period refusal ``tick`` already knows how to handle
    gracefully, with no change needed to ``loop.py``'s own tuple.

    ``already_published``'s phase-two fallback (a candidate ``WeightsSet``
    found well past the ordinary settle-margin window) needs day D's own
    real, independently-known expected vector to tell "day D's own
    genuine, delayed publication" apart from "a DIFFERENT day's own real
    publication that happens to land in the same widened window" -- an
    independent Verifier reproduced exactly that confusion against an
    earlier version of this fix that only checked whether content had
    CHANGED since the day's own baseline, not whether it matched day D
    SPECIFICALLY (a later day, D+2, publishing genuinely different
    content was mistaken for D's own delayed recovery). See
    ``already_published``'s own docstring for the full reasoning.

    Without a way to read day D's own real, already-computed canonical
    score (``persist_score_evidence.write_canonical_score_evidence``'s own
    ``RUNTIME_ROOT/ticket-16/scores/day-<D>.json`` convention), phase two
    has no ground truth to compare against and refuses by name here rather
    than falling back to timing alone, which is exactly the insufficient
    check this fix replaces.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``, or when phase two is actually needed (a
    delayed recovery outside the ordinary settle margin) -- the
    overwhelming common on-time-publish case never reaches phase two at
    all.** A real deployment always gets a real ``canonical_score_for``
    from ``build_chain_adapter`` (reading the same canonical path
    ``persist_score_evidence.py`` already writes), so this never fires on
    a real deployment that has ever actually scored the day being asked
    about.
    """


class CanonicalScoreUnavailable(ChainAdapterRefused, Refusal):
    """Day D's own canonical score file is genuinely not available locally
    -- day D was never scored or attempted on this deployment, so there is
    nothing to compare a widened-window candidate's content against.

    This is not the same finding as ``CanonicalScoreNotWired`` above: the
    CAPABILITY is configured and working, it genuinely answered "day D has
    no local record." Per this fix's own discipline (refuse rather than
    guess, the same reason a failed read raises ``ChainUnreadable`` instead
    of silently reading as "not published"), phase two refuses here rather
    than falling back to comparing timing alone -- a delayed publish for a
    day nobody locally has a record of scoring is a genuinely unusual
    state that deserves a human's attention, not a guess.

    **Also a ``Refusal``, for the identical integration reason
    ``CanonicalScoreNotWired``'s own docstring gives -- and the more
    practically reachable of the two in real production.** Unlike
    ``CanonicalScoreNotWired`` (essentially unreachable once
    ``build_chain_adapter`` has ever wired a real default, the same "only
    reachable bypassing build_chain_adapter" shape ``DayWindowNotWired``
    already has), a fresh or rebuilt instance, a deleted local score file,
    or a day scored by a different validator instance are all real,
    ordinary ways for THIS to fire with a fully-wired, working reader.
    Without this being a ``Refusal``, that real, expected condition would
    crash the unattended validator process outright -- and, since
    ``run_forever``'s own restart-seeding would find the identical missing
    file on every subsequent restart, very plausibly crash-loop on the
    same period until a human manually repairs it, rather than being
    recorded once and retried at the next period boundary the way every
    sibling refusal already degrades.
    """


class ParticipantHoldingsNotWired(ChainAdapterRefused):
    """The holding sample interval is missing or invalid.

    Configure ``NOAH_HOLDING_STRIDE_BLOCKS`` explicitly. The adapter does not
    substitute a sampling interval when constructing daily holding evidence.
    """


class StoreFactoryNotWired(ChainAdapterRefused):
    """There is no live history-store builder configured on this
    ``ChainAdapter`` (#566).

    ``read()`` calls ``self._store_factory()`` to get the
    ``noah.chain.store.Store`` it scans a day's chain facts into
    (``run_day``) and reads seats/holdings/pay back out of
    (``build_fixture``) -- found, by direct reading, to have NO
    corresponding check anywhere in ``read()`` before #566, even though
    every other capability this class defaults to ``None`` already refused
    by name. A ``ChainAdapter`` built with ``store_factory`` left at its
    default would have reached ``self._store_factory()`` -- ``None()`` --
    the moment a real ``holding_stride`` stopped ``ParticipantHoldingsNotWired``
    from firing first: a bare ``TypeError``, not a named refusal, the exact
    "accidental shield" shape this branch's own history already found twice
    before this issue.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``, or via a real deployment where this
    validator's own local history-store path could not be determined.**
    ``build_chain_adapter`` always supplies a real ``store_factory`` --
    ``noah.chain.store.Store.create``, at a path namespaced by netuid and
    Ark address (``_default_validator_history_db_path``, or
    ``NOAH_HISTORY_DB`` if set) -- provided the Ark's own coldkey is known
    (see ``ArkColdkeyNotWired`` below, checked first: the default path is
    namespaced by it). The closure itself still refuses, separately, as
    ``HistoryStoreUnwritable`` if the path it computes cannot actually be
    written to when ``read()`` calls it.
    """


class ArkColdkeyNotWired(ChainAdapterRefused):
    """There is no Ark coldkey configured on this ``ChainAdapter`` (#566).

    ``read()``'s own ``run_day`` call passes ``ark_coldkey=self.ark_coldkey``
    straight through to ``noah.chain.deposits.deposits_in_block``, which
    keys every deposit it extracts off it -- found, by direct reading, to
    have no corresponding check anywhere in ``read()`` before #566, the
    identical "accidental shield" shape ``StoreFactoryNotWired`` above
    describes: a real ``holding_stride`` used to be the ONLY thing standing
    between a real deployment and this ``None`` reaching ``deposits_in_block``
    unexamined.

    **Not a chain fact -- an operator-configured identity, the same one
    ``neurons/miner.py``'s own ``NOAH_ARK_ADDRESS`` (``ARK_ADDRESS_VAR``)
    already is.** No chain read computes which coldkey the Ark is; an
    operator names it, the same physical fact on both the miner and the
    validator side of one deployment, which is why ``build_chain_adapter``
    reads the identical setting name rather than inventing a second,
    separately-configured value for it.

    **Only reachable via a ``ChainAdapter`` built directly, bypassing
    ``build_chain_adapter``, or via a real deployment with
    ``NOAH_ARK_ADDRESS`` still unset.** ``build_chain_adapter`` raises this
    same class directly, by the identical name, when that setting is
    absent.

    **This class proves only that an address was given, never that it is
    the RIGHT one.** ``ChainAdapter.read()`` is what proves the second half,
    AS OF ISSUE #571, fresh on every real call -- see
    ``ArkIdentityNotPublished``/``ArkIdentityMismatch`` below and this
    module's own docstring, "One residual risk this closes only partially",
    now marked closed.
    """


class DesignatedValidatorHotkeyNotWired(ChainAdapterRefused):
    """The designated Ark validator hotkey is missing.

    Configure NOAH_VALIDATOR_HOTKEY for the intended network; no other network's
    identity is substituted. Direct adapter construction also enforces this gate.
    """


class ArkIdentityNotPublished(ChainAdapterRefused):
    """The subnet has published no identity record at all, as of the block
    ``ChainAdapter.read()`` just checked (#571).

    **Not a mismatch, and never reported as one.** A subnet that has not
    spoken has not disagreed with the configured Ark coldkey --
    ``noah.deposits.ark_readers.ark_address_matches_chain_pin`` already
    draws this exact line for ``neurons/miner.py``'s own preflight
    (``PinNotPublished``, that module's own docstring: "reporting that as a
    mismatch would send an operator looking for tampering that did not
    happen"), and this refusal exists so the validator's own check never
    collapses the two into one generic failure either.

    **Fires on every real deployment until the subnet's own identity record
    names an address in ``additional``** (``noah.chain.live.
    LiveRunnerChain.published_ark_pin``'s own docstring for exactly which
    field and why) -- including the practice chain today, which has not
    published one. Configuring the record is an on-chain, operator action
    outside this codebase's own scope; this refusal exists to make its
    absence loud rather than silently trusted.
    """


class ArkIdentityMismatch(ChainAdapterRefused):
    """The subnet's own published identity record names a DIFFERENT
    ACCOUNT than the one configured on this ``ChainAdapter`` (#571) --
    compared by public key (``noah.chain.ss58.public_key_of``), never by
    raw text: two spellings of the SAME account (a different network
    prefix, a trailing newline) must never raise this.

    **A real disagreement, never softened into a warning.** This is the
    exact failure #571's own issue text names as the dangerous kind: a
    wrong-but-well-formed Ark coldkey makes every real deposit invisible to
    ``run_day`` (it scans for deposits to whatever address it is given),
    producing a clean-looking fixture with silently zero credit for
    everyone. ``ArkColdkeyNotWired`` above already proves SOME address was
    configured; this proves it is the address the subnet itself vouches
    for, not merely a well-formed one.

    **Checked fresh on every real ``read()`` call, not once at
    ``build_chain_adapter`` time.** The published record is itself
    operator-mutable on chain after this validator starts; a check that ran
    once at startup and cached its answer would keep trusting a coldkey the
    subnet had since disavowed. The cost is a few more dual-sourced,
    canary-checked reads per call (``noah.chain.agreeing.AgreeingChain.
    published_ark_pin``, called three times -- see ``_confirm_ark_identity``'s
    own docstring for why) -- the same per-call re-check
    ``noah.chain.emission_state.observe_emission_state`` (#567) already
    pays for the identical reason, immediately adjacent to this one in
    ``read()`` itself.
    """


class ArkIdentityReorged(ChainAdapterRefused):
    """The chain moved between two reads inside one ``_confirm_ark_identity``
    call -- a reorg both sources have already converged on, not a source
    disagreement -- so the converged answer is refused rather than trusted
    stale (#571).

    **Found by real codex adversarial review, 2026-09-01, P1, of the first
    version of this check, which read ``published_ark_pin`` only once.** A
    single ``noah.chain.agreeing.AgreeingChain._agreed`` call is already
    dual-sourced and canary-checked for the one block it asks about -- this
    project's own accepted baseline exposure, "one read's worth" (issue
    #520) -- but that protects the READ itself, not what happens AFTER a
    trusted answer is handed to a caller that then does further chain work
    (``run_day``'s own multi-block scan) on the strength of it. A reorg
    landing strictly between this check's own read and that later work is
    invisible to a single read's own canary. Closed the same way
    ``noah.chain.emission_state.observe_emission_state``'s own
    ``EmissionStateReorged`` already closes the identical composition risk
    for ``first_emission_block`` (#567): the answer this check is about to
    trust is re-derived, fresh, immediately before trusting it, and must
    still agree.

    **The FIRST fix still did not close this, found by a second real codex
    adversarial review the same day, round 2, P1.** That version read
    ``from_block`` AFTER this bracket's own two reads, so the bracket's
    second read was no longer "immediately before trusting" anything --
    codex reproduced, against a real two-source ``AgreeingChain``, a reorg
    landing during that interposed ``from_block`` read going undetected.
    ``_confirm_ark_identity`` now reads ``from_block`` FIRST, alone, before
    this bracket ever runs, so the bracket's own second read is the LAST
    chain work the function does -- see that function's own docstring,
    "The order above is load-bearing."
    """


class ArkIdentityChangedDuringDay(ChainAdapterRefused):
    """The subnet's own published identity record named a REAL, DIFFERENT
    account at the start of the day being scanned than the one it named
    (confirmed twice) at the end of it (#571).

    **Found by real codex adversarial review, 2026-09-01, P1.** A single
    ``to_block`` snapshot cannot tell "this address has always been
    correct" apart from "this address only became correct partway through
    the very day ``run_day`` is about to scan for deposits" -- and the
    latter is exactly the shape a subnet identity record changing (an
    operator correction, or a compromise) mid-day would take. Reading
    ``from_block`` too and requiring it to agree with ``to_block`` (once
    ``to_block`` has itself been reorg-checked -- see ``ArkIdentityReorged``
    above) catches that shape directly, at the cost of one more read per
    call.

    **``None`` at ``from_block`` is deliberately NOT instability.** A
    record with no value at the day's start that first names a real address
    by the day's end is the ordinary, expected shape of the very day an
    Ark's identity is first published on chain -- the exact scenario #571
    itself exists to verify (see this module's own docstring, "AS OF ISSUE
    #571: CLOSED"). Refusing that case here would refuse the one case this
    whole check was built to pass. Only a REAL, different, already-published
    address at ``from_block`` counts as a change worth refusing over -- and
    a record that existed at ``from_block`` but is gone entirely by
    ``to_block`` counts too (a disappearance is exactly as suspicious as a
    change to a different account).

    **Proves the two ENDPOINTS of the scanned day agree, not that every
    block strictly between them does -- named rather than silently
    accepted, found by the same round-2 review that found the ordering
    gap above.** Codex constructed a chain answering the configured
    address at both ``from_block`` and ``to_block`` but a DIFFERENT one at
    an interior block, and confirmed ``_confirm_ark_identity`` accepts it.
    Proving stability across the WHOLE range would mean reading every
    block in it (or a binary-search-narrowed subset), real, scoped design
    work of its own -- genuinely larger than this check, and larger than
    the two-round adversarial-review ceiling this branch already spent
    affords. This closes #571's own stated scenario (a wrong address
    configured before the first real validator run, or from the start) by
    proving the address was not wrong at either boundary of the day being
    judged; it does not, and does not claim to, prove continuous identity
    stability throughout it.
    """


class DialsNotWired(ChainAdapterRefused):
    """The scoring dials file is missing, unreadable or invalid.

    Set ``NOAH_DIALS_JSON`` to a file generated from the selected parameter profile.
    There is no fallback to fixture settings.
    """


class EntryDepositAmountNotWired(ChainAdapterRefused):
    """The entry contribution amount is not configured.

    ``NOAH_ENTRY_DEPOSIT_RAO`` must specify the amount used by the selected profile.
    The amount is not inferred from registration collateral or example fixtures.
    """


class EntryDepositAmountMismatched(ChainAdapterRefused):
    """The configured entry amount disagrees with the selected scoring profile.

    build_chain_adapter refuses this mismatch before returning live connections.
    The legacy environment setting remains supported but cannot override policy.
    """


class DialsAmountMismatched(ChainAdapterRefused):
    """The supplied dials disagree with the selected scoring profile.

    All seven scoring dials are compared with ``NOAH_PARAMETERS_JSON``. Configure
    both inputs from the same profile; disagreement prevents adapter construction.
    """


class HistoryStoreUnwritable(ChainAdapterRefused):
    """This validator's own local history-store path could not actually be
    written to (#566).

    Raised from the real ``store_factory`` closure ``build_chain_adapter``
    builds -- never from ``build_chain_adapter`` itself, because a
    filesystem failure is a fact about the machine THIS RUN happens to be
    on, not about configuration, and ``store_factory`` is called fresh
    inside ``read()`` (once per scored period), not once at startup. Ports
    the identical hardening ``neurons/miner.py``'s own ``build_services``
    already carries, adversarially found necessary there (issue #514):
    ``os.makedirs`` on an unwritable parent, or a path component that is a
    file rather than a directory, raises a bare ``OSError``; ``sqlite3.
    connect`` on a path that is itself unopenable (an existing directory,
    among other causes) raises a bare ``sqlite3.OperationalError`` before
    ``noah.chain.store.Store.create``'s own try/except ever starts. Both
    are caught here and reported by name instead, the same "cheapest and
    most likely to be wrong first" discipline this codebase applies
    throughout ``noah/chain/`` and ``neurons/``.
    """


def require_dual_sourced(runner_chain):
    """`runner_chain` unchanged, or refuse -- the identical check
    `neurons/miner.py`'s own `_require_dual_sourced` already performs for
    its own real chain reads, reused in spirit rather than re-derived: a
    single endpoint has already been observed returning a plausible wrong
    answer for an individual read with no error (this project's own
    recorded history, cited throughout `noah/chain/`), so nothing here may
    trust one.
    """
    if not isinstance(runner_chain, AgreeingChain):
        raise SingleSourceChainRefused(
            "the chain adapter's own runner_chain is not a real, dual-sourced "
            "AgreeingChain (noah.chain.agreeing.AgreeingChain). This project's chain "
            "reads are never single-sourced -- a single endpoint has already been "
            "observed answering a plausible-looking wrong value for an individual read "
            "with no error at all. Configure a second, independent endpoint "
            "(NOAH_ENDPOINT_2, the same variable neurons/miner.py's own sources_agree "
            "check already reads) and build this adapter from two real sources. "
            "Nothing was read."
        )
    return runner_chain


def _confirm_ark_identity(runner_chain, *, netuid, ark_coldkey, from_block, to_block):
    """``ark_coldkey`` unchanged, or refuse -- confirming it against the
    subnet's own published identity record, not merely that it is
    well-formed (#571). Called by ``ChainAdapter.read()`` itself, fresh
    every time, BEFORE ``store_factory()`` runs (a refusal here must leave
    no store behind); see ``ArkIdentityMismatch``'s own docstring for why
    it is not computed once by ``build_chain_adapter`` and cached.

    ``netuid`` is taken from the caller (``ChainAdapter.netuid``) rather
    than read off ``runner_chain`` -- ``AgreeingChain`` wraps one netuid
    per ``LiveRunnerChain`` source internally but exposes no ``netuid``
    attribute of its own, so guessing at one here would either be wrong or
    silently degrade every message this raises.

    **Three real, dual-sourced, canary-checked reads, not one -- found
    insufficient by real codex adversarial review, 2026-09-01 (two P1s), of
    an earlier, single-read version, and REORDERED after a second
    adversarial review the same day (round 2, two more P1s) found the fix
    itself still insufficient.** See ``ArkIdentityReorged`` and
    ``ArkIdentityChangedDuringDay``'s own docstrings for exactly what each
    closes:

    1. ``published_ark_pin(from_block)`` -- the day-stability input, read
       FIRST and ALONE, deliberately before either ``to_block`` read below.
    2. ``published_ark_pin(to_block)``, then read again immediately -- a
       disagreement between the two is a reorg, not a real fact, and
       refuses as ``ArkIdentityReorged`` rather than trusting either
       answer.

    **The order above is load-bearing, not cosmetic.** [codex adversarial
    review, round 2, 2026-09-01, P1] reproduced, against a real two-source
    ``AgreeingChain``, that an EARLIER version of this function -- which
    read ``from_block`` AFTER the ``to_block`` bracket -- accepted a stale
    ``to_block`` answer: the bracket's own second read is only "immediately
    before trusting it" if literally nothing else touches the chain
    between that read and the comparisons that follow. Interposing the
    ``from_block`` read between the bracket's two reads (the first
    version's own bug) reopened exactly the gap the bracket exists to
    close. This version's own last chain read is the ``to_block`` bracket's
    second call; every comparison after it is pure, local computation
    (``public_key_of``, equality), never another read.

    Compared by PUBLIC KEY (``noah.chain.ss58.public_key_of``) THROUGHOUT
    -- ``from_block``'s own answer included, never by raw text -- see
    ``noah/chain/ss58.py``'s own module docstring for the exact failure a
    text compare invites: "every ordinary way of writing the same key down
    produces a mismatch, and a mismatch here is not an error. It is every
    deposit ever made quietly failing to be a deposit." [codex adversarial
    review, round 1, 2026-09-01, P2] found the FIRST fix decoded
    ``to_block``'s own answer correctly but still compared ``from_block``'s
    answer as raw text against it -- the identical defect
    ``noah.deposits.ark_readers.ark_address_matches_chain_pin`` already
    carries for the miner's setting (a pre-existing gap in already-shipped
    code, out of this issue's own scope to fix, flagged separately rather
    than silently perpetuated here). A malformed address on ANY of the
    three sides (``ark_coldkey``, ``from_block``'s answer, ``to_block``'s
    answer) raises ``noah.chain.ss58.NotAnAddress`` directly, unwrapped: a
    more specific and more honest signal than folding it into
    ``ArkIdentityMismatch`` or ``ArkIdentityChangedDuringDay``.
    ``ark_coldkey`` is decoded FIRST of all, before any chain read,
    matching this codebase's own "cheapest and most likely to be wrong
    first" discipline within this function -- a malformed operator setting
    is a local, free check (``ChainAdapter.read()`` itself already reads
    ``block_hash(height)``/``day_window`` before this function is ever
    called, so this is the cheapest-first ordering THIS function controls,
    not a claim about the whole method).

    **What this still does NOT prove, named rather than silently
    accepted: that the record was stable at every block strictly BETWEEN
    ``from_block`` and ``to_block``, only that its two endpoints agree.**
    [codex adversarial review, round 2, 2026-09-01, P1] constructed a chain
    answering the configured address at both endpoints but a DIFFERENT one
    at an interior block, and this function accepts it -- proving the
    endpoints alone is a materially weaker claim than proving the whole
    range. Closing that fully would mean reading every block in the range
    (or a binary-search-narrowed subset of it) rather than its two ends --
    real, scoped design work of its own, genuinely larger than a two-round
    review ceiling affords, not attempted here. This function proves the
    address was NOT wrong at either boundary of the day being scanned,
    which is what closes #571's own stated scenario (a wrong address
    configured from the start, or before the very first real validator
    run); it does not prove continuous identity stability, and does not
    claim to.

    Two distinct refusals for the two outcomes #571's own issue text
    requires stay separate: ``None`` at ``to_block`` (nothing published) is
    ``ArkIdentityNotPublished``, never treated as a mismatch; a real,
    different account is ``ArkIdentityMismatch``.
    """
    ark_key = public_key_of(ark_coldkey)

    # from_block first, alone -- see this function's own docstring, "The
    # order above is load-bearing", for why this must not be interposed
    # between the to_block bracket's own two reads below.
    published_start = runner_chain.published_ark_pin(from_block)
    published_start_key = None if published_start is None else public_key_of(published_start)

    published_end = runner_chain.published_ark_pin(to_block)
    # Re-derived fresh, IMMEDIATELY before trusting it -- nothing else
    # reads the chain between this call and the comparisons below. See
    # ArkIdentityReorged's own docstring, "Reorg safety" (matches
    # noah.chain.emission_state.observe_emission_state's own precedent).
    published_end_reconfirmed = runner_chain.published_ark_pin(to_block)
    if published_end_reconfirmed != published_end:
        raise ArkIdentityReorged(
            f"block {to_block}'s own identity-pallet answer changed from {published_end!r} "
            f"to {published_end_reconfirmed!r} between the first read and a fresh re-check "
            f"taken immediately before trusting it -- the chain reorged mid-read. Refused "
            f"rather than trusted stale; nothing was published. Re-run once the chain has "
            f"settled."
        )

    if published_end_reconfirmed is None:
        if published_start_key is not None:
            raise ArkIdentityChangedDuringDay(
                f"subnet {netuid}'s own published identity record named {published_start!r} "
                f"at block {from_block} (the day's own start) and publishes NOTHING at block "
                f"{to_block} (the day's own end, confirmed twice) -- a real change during the "
                f"exact day being scanned for deposits, not a reorg. Refused rather than "
                f"trusting either answer for the whole day; nothing was published."
            )
        raise ArkIdentityNotPublished(
            f"block {to_block}: subnet {netuid} publishes no identity record, so there is "
            f"no pin to confirm the configured Ark coldkey {ark_coldkey!r} against. This is "
            f"not a mismatch and is not reported as one -- see ArkIdentityNotPublished's "
            f"own docstring. Nothing was published."
        )

    published_end_key = public_key_of(published_end_reconfirmed)

    if published_start_key is not None and published_start_key != published_end_key:
        raise ArkIdentityChangedDuringDay(
            f"subnet {netuid}'s own published identity record named {published_start!r} at "
            f"block {from_block} (the day's own start) and {published_end_reconfirmed!r} at "
            f"block {to_block} (the day's own end, confirmed twice) -- a real change during "
            f"the exact day being scanned for deposits, not a reorg (compared by public key, "
            f"not text). Refused rather than trusting either answer for the whole day; "
            f"nothing was published."
        )

    if published_end_key != ark_key:
        raise ArkIdentityMismatch(
            f"block {to_block}: subnet {netuid}'s own published identity record names "
            f"{published_end_reconfirmed!r}, a different account than the configured Ark "
            f"coldkey {ark_coldkey!r} (compared by public key, not text). A wrong-but-"
            f"well-formed address makes every real deposit invisible to run_day -- see "
            f"ArkIdentityMismatch's own docstring. Nothing was published, and no store was "
            f"scanned."
        )
    return published_end_reconfirmed


def _registration_days_for(runner_chain, store, seats, to_block):
    """Real registration day per SEAT, from the chain's own
    ``BlockAtRegistration`` record -- never left to `noah.chain.
    fixture_build.build_fixture`'s own silent default (a coldkey's first
    observed activity, which is a different fact and can silently disagree
    with when a wallet actually registered) (#570).

    `noah.chain.pay._check_registration` already reads this identical map,
    for the identical reason (confirming a paid seat's registration record
    can be trusted): this is that same real, dual-sourced read
    (``runner_chain.registration_blocks``), reused, not a second
    implementation of it.

    Scoped to ``seats`` -- the seats this call is already scoring -- for
    two reasons. First, ``registration_blocks(to_block)`` only answers for a
    UID actually registered AT ``to_block``, so a seat with no live UID is
    not something this read can answer for regardless. Second, a
    registration day this function cannot establish is not defaulted to
    anything here: `_entry_deposits_for` below only ever credits a seat
    this function actually named.

    **Returns ``{coldkey: {hotkey: day}}`` -- genuinely per SEAT, never
    collapsed or excluded by coldkey (#112, D-138 reopened, replacing the
    exclusion this function used to apply here).** D-072 rules the entry
    structure PER SEAT ("each seat maintaining its own 400 after its own
    100 deposit... one holding may never satisfy multiple seats"), and
    D-138's reopening (issue #112) now lets one coldkey legitimately hold up
    to 32 live seats at once -- so a coldkey holding more than one seat is
    no longer a suspicious shape to guard against by leaving it out
    entirely. The real, per-uid ``BlockAtRegistration`` record already
    answers each seat independently (``registration_block_by_uid.get(uid)``
    is looked up once per hotkey's own uid, never merged across a
    coldkey's several seats), so nesting the result by ``{coldkey: {hotkey:
    day}}`` -- the same shape ``seats`` itself already uses, and the shape
    `noah.chain.fixture_build.build_fixture` now accepts for
    ``registration_days``/``entry_deposits`` (same design, landed
    separately in that module) -- is a direct, lossless use of a fact this
    function already had, not a new read and no `min()`/collapsing
    arithmetic to get wrong.

    Before this issue, a coldkey holding more than one live seat was left
    out of the returned mapping ENTIRELY, real codex adversarial review,
    2026-09-01, P1: `build_fixture`'s own schema held exactly one
    ``registrationDay``/``entryDepositRao`` per COLDKEY at the time, copied
    unchanged into every one of that coldkey's rows, so a collapsed or
    duplicated registration day fed straight into `_entry_deposits_for`
    would have credited the SAME entry deposit onto every seat's row. That
    tension is exactly what this reopening (#112) resolves, on both sides:
    each seat now gets its OWN real registration day, and `build_fixture`
    reads it back per seat rather than copying one coldkey-wide value onto
    every row.
    """
    registration_block_by_uid = runner_chain.registration_blocks(to_block)
    registration_days = {}
    for coldkey, hotkey_uids in seats.items():
        per_hotkey = {}
        for hotkey, uid in hotkey_uids.items():
            block = registration_block_by_uid.get(uid)
            if block is None:
                # No real registration record for this seat's uid at
                # to_block -- left out, the same safe default build_fixture
                # already gives any seat this function cannot establish a
                # registration day for.
                continue
            # A registration block never moves, so its day is recorded once and
            # read back from the store afterwards. Asking the chain again would
            # fail for a block older than a pruned node's state (about 90 days
            # on mainnet), and this runs on every read() for every seat.
            day = store.registration_block_day(block)
            if day is None:
                day = store.record_registration_block_day(
                    block, runner_chain.block_timestamp(block)
                )
            per_hotkey[hotkey] = day
        if per_hotkey:
            registration_days[coldkey] = per_hotkey
    return registration_days


def _entry_allocation(store, coldkey, hotkey, amount, *, through_day):
    """Allocate the first ``amount`` eligible NOAH across its actual receipt days.

    Reads use the store's existing per-miner attribution and scan/refusal checks.
    Stop at the threshold; future receipts never affect an earlier evaluation.
    A partial allocation earns no starting credit and is returned as empty.
    """
    first = store.first_activity_day_for_hotkey(coldkey, hotkey)
    if first is None or first > through_day:
        return {}
    remaining = amount
    allocation = {}
    for day in range(first, through_day + 1):
        observed = store.noah_deposits_for_hotkey(coldkey, hotkey, day)
        consumed = min(observed, remaining)
        if consumed:
            allocation[day] = consumed
            remaining -= consumed
        if remaining == 0:
            return allocation
    return {}


def _entry_deposits_for(store, registration_days, entry_deposit_rao, *, as_of_day):
    """Credit one entry amount when this seat's cumulative eligible NOAH reaches it.

    Drew's 2026-10-16 decision: deposits may span UTC days. Credit lands on
    the threshold-crossing day, bounded by the day being evaluated. Return
    per-seat credited amounts and qualification days for the unchanged engine.
    ``_ExcludingEntryDeposit`` removes exactly these tokens from ordinary
    contributions on each original receipt day, including earlier partials.

    Preserve the existing re-registration rule: history predating this seat's
    current registration does not create a second starting credit. Sibling
    seats cannot fund or disqualify each other. Missing scanned history leaves
    the affected seat uncredited; other store refusals propagate. The adapter's
    history-floor and gap guards still apply before scoring/publication.
    """
    if type(as_of_day) is not int or as_of_day < 0:
        raise ValueError("as_of_day must be a nonnegative whole UTC day")
    if type(entry_deposit_rao) is not int or entry_deposit_rao <= 0:
        raise ValueError("entry_deposit_rao must be a positive whole amount")
    credited = {}
    entry_days = {}
    for coldkey, per_hotkey_registration in registration_days.items():
        for hotkey, registration_day in per_hotkey_registration.items():
            if registration_day > as_of_day:
                continue
            first = store.first_activity_day_for_hotkey(coldkey, hotkey)
            if first is not None and first < registration_day:
                continue
            try:
                allocation = _entry_allocation(
                    store, coldkey, hotkey, entry_deposit_rao, through_day=as_of_day
                )
            except DayNotScanned:
                continue
            if allocation:
                credited.setdefault(coldkey, {})[hotkey] = entry_deposit_rao
                entry_days.setdefault(coldkey, {})[hotkey] = max(allocation)
    return credited, entry_days


class _ExcludingEntryDeposit:
    """Read-only view excluding entry tokens from their original receipt days.

    Reconstruct the same bounded allocation used to award entry credit. Both
    wallet and per-seat reads subtract only the tokens consumed for that seat;
    any surplus remains an ordinary contribution. Raw cumulative eligibility,
    registration facts and persisted receipts remain unchanged.
    """

    def __init__(self, store, entry_deposits, entry_days):
        self._store = store
        self._by_coldkey_day = {}
        self._by_seat_day = {}
        for coldkey, per_hotkey in entry_deposits.items():
            for hotkey, amount in per_hotkey.items():
                day = entry_days.get(coldkey, {}).get(hotkey)
                if day is None:
                    raise ChainAdapterRefused("entry credit has no qualification day")
                allocation = _entry_allocation(store, coldkey, hotkey, amount, through_day=day)
                if not allocation or max(allocation) != day or sum(allocation.values()) != amount:
                    raise ChainAdapterRefused("entry credit does not match eligible receipt allocation")
                for receipt_day, consumed in allocation.items():
                    self._by_seat_day[coldkey, hotkey, receipt_day] = consumed
                    key = (coldkey, receipt_day)
                    self._by_coldkey_day[key] = self._by_coldkey_day.get(key, 0) + consumed

    def noah_deposits(self, coldkey, day):
        observed = self._store.noah_deposits(coldkey, day)
        return observed - self._by_coldkey_day.get((coldkey, day), 0)

    def noah_deposits_for_hotkey(self, coldkey, hotkey, day):
        observed = self._store.noah_deposits_for_hotkey(coldkey, hotkey, day)
        return observed - self._by_seat_day.get((coldkey, hotkey, day), 0)

    def __getattr__(self, name):
        return getattr(self._store, name)


class ChainAdapter:
    """The real `chain` object `run_period` and `noah.core.loop.tick` operate
    against, and the same object `noah.core.publish.publish_weights` and
    `noah.core.startup.check_can_publish_yet` sign and submit through.

    ``reader`` is a `noah.core.reader.ChainReader` built from the same
    `bt.subtensor` client `neurons/validator.py:connect()` already opens for
    `gate()`/`survey()`. ``publisher`` is a `noah.core.submitter.Publisher`
    built from the SAME wallet. ``runner_chain`` is a real, dual-sourced
    `noah.chain.agreeing.AgreeingChain` -- see `require_dual_sourced` above,
    which this constructor always calls.

    ``seats_source``, ``day_window``, and ``day_anchor_ts`` all default to
    ``None`` on this constructor, but a real deployment (via
    `build_chain_adapter`) no longer leaves them that way: each is computed
    for real there, from the dual-sourced `runner_chain`, before this
    constructor is ever called (see this module's own docstring, "What
    became real tonight"). ``None`` remains the honest value for a
    ``ChainAdapter`` built directly -- a test proving one specific behaviour
    in isolation, or proving each of `read()`'s own named refusals actually
    fires -- and `read()` still refuses by name rather than proceed whenever
    any of the three is genuinely absent.

    ``holding_stride`` -- along with ``store_factory``, ``ark_coldkey`` and
    ``dials`` -- is computed for real inside `build_chain_adapter` too, AS OF
    ISSUE #566 (see this module's own docstring for the full reasoning
    behind each): `noah.chain.runner._sample_holdings` now has real reorg
    protection, and `build_chain_adapter` reads a real, operator-configured
    stride from the validator's own settings, refusing by name
    (`ParticipantHoldingsNotWired`) when none is configured -- never a
    number invented here, since no ruled sampling cadence exists anywhere
    in this project for one to compute from.

    ``seats_source``, when given, is ``(day, store) -> {coldkey: {hotkey:
    uid}}``, `build_fixture`'s own documented shape --
    `noah.chain.seats.current_seats`, composed with `runner_chain` and the
    day's own recorded range-end block, in production.

    ``day_window`` is ``height -> (day, from_block, to_block)`` -- the
    calendar day `height` falls in, and that day's own true block bounds.
    In production, `noah.chain.dayblocks.day_block_range`, composed with
    `runner_chain` and `day_anchor_ts`; see that module's own docstring for
    why this is genuinely new work rather than a reuse of
    `neurons/miner.py`'s own `_day_start_block` (#478).

    ``day_anchor_ts`` is the chain's own genesis moment, floored to the
    nearest UTC midnight -- the same fact `noah.chain.store.Store`'s own
    ``day_anchor_ts`` is, confirmed by `build_fixture`'s own hard equality
    check between the two (fixture_build.py:156-159). In production,
    `noah.chain.dayblocks.day_anchor`, read fresh from `runner_chain`.
    """

    def __init__(
        self,
        *,
        reader,
        publisher,
        runner_chain,
        netuid,
        uid,
        wallet_hotkey,
        settle_blocks,
        ark_coldkey=None,
        designated_validator_hotkey=None,
        dials=None,
        day_anchor_ts=None,
        seats_source=None,
        holding_stride=None,
        read_collateral=False,
        day_window=None,
        store_factory=None,
        entry_deposit_rao=None,
        may_resume=True,
        publish_mode=PUBLISH_MODE_DEFAULT,
        canonical_score_for=None,
        commit_ledger_path=None,
        publish_intent_path=None,
        publish_hold_path=None,
        publish_backoff_path=None,
        submitted_record_dir=None,
    ):
        if publish_mode not in PUBLISH_MODES:
            raise InvalidPublishMode(
                f"publish_mode must be one of {PUBLISH_MODES}, and it is {publish_mode!r}. "
                f"This is a fixed, one-time configuration choice (NOAH_PUBLISH_MODE) -- never "
                f"a value guessed at, since a validator that guessed here could silently pick "
                f"the wrong one of two genuinely different publish paths."
            )
        # Ticket #16 (B06), issue #56 comment 5720306237. Owned here, the
        # same way self.uid/self.wallet_hotkey already are: which of the two
        # publish paths (self.publish/self.publish_direct, below) run_period
        # actually calls is this adapter's own fixed configuration, not a
        # raw setting run_period reads for itself. Defaults to "sealed" --
        # unchanged behavior for every existing caller that has never heard
        # of this parameter, including every fake/test chain built before it
        # existed.
        self.publish_mode = publish_mode
        # Issue #334: the per-day sealed-commit ledger (noah.core.commit_ledger).
        # None (the default, and every pre-existing caller) means no ledger:
        # sealed already_published behaves exactly as before. Sealed mode only;
        # direct mode never reads or writes it.
        # Issue #291: where `record_submitted` writes day-N.submitted.json. None
        # (default, every pre-existing caller) means nothing is recorded.
        self._submitted_record_dir = submitted_record_dir
        self._agreed_calculation = None
        self._calculation_reference = None
        self._commit_ledger = (
            CommitLedger(commit_ledger_path) if commit_ledger_path is not None else None
        )
        # Issue #291: the "a send may be in flight" marker (noah.core.publish_intent).
        # None (default, every pre-existing caller) means no marker: unchanged behaviour.
        # Sealed mode only.
        self._publish_intent = (
            PublishIntent(publish_intent_path) if publish_intent_path is not None else None
        )
        self._resolving_intent = False
        # Issue #291/#235: a hand-set hold (noah.core.publish_hold); None means no hold file is watched.
        self._publish_hold = PublishHold(publish_hold_path) if publish_hold_path is not None else None
        # Proxy route: a persisted count of consecutive failed sends (each may cost the
        # proxy signer a fee). None means no limit is watched (every pre-existing caller).
        self._failure_backoff = (
            PublishFailureBackoff(publish_backoff_path) if publish_backoff_path is not None else None
        )
        self.reader = reader
        self.publisher = publisher
        self.runner_chain = require_dual_sourced(runner_chain)
        self.netuid = netuid
        self.uid = uid
        self.wallet_hotkey = wallet_hotkey
        self.settle_blocks = settle_blocks
        self.ark_coldkey = ark_coldkey
        # Issue #282 item 4/4a, #302: the designated Ark Validator's own
        # hotkey, threaded through to run_day -> deposits_in_block's own
        # required `designated_validator_hotkey` the identical way
        # ark_coldkey already is -- see DesignatedValidatorHotkeyNotWired's
        # own docstring for the refusal read() raises when this is None.
        self.designated_validator_hotkey = designated_validator_hotkey
        self.dials = dials
        self.day_anchor_ts = day_anchor_ts
        self._seats_source = seats_source
        self._holding_stride = holding_stride
        self._read_collateral = read_collateral
        self._day_window = day_window
        self._store_factory = store_factory
        # already_published's own issue #212 phase-two fix: a callable
        # `day -> noah-score-output/6 dict | None`, day D's own real,
        # locally-computed canonical score if one exists. See
        # CanonicalScoreNotWired's own docstring for what this is for and
        # why phase two cannot work without it.
        self._canonical_score_for = canonical_score_for
        # Threaded straight through to run_day's own may_resume (default
        # True, unchanged behavior for every existing caller). Exists so a
        # day whose store record predates a real fix -- read_collateral
        # flipping True being the first real case, D-143 -- can be forced
        # to actually rescan rather than skipped as already-scanned, going
        # through this exact same read() path (ark identity, emission
        # state, entry deposits, all of it) rather than a bespoke one-off
        # that risks silently diverging from it.
        self._may_resume = may_resume
        # #570: the ruled, flat entry-deposit figure [P-020], operator-
        # configured (NOAH_ENTRY_DEPOSIT_RAO) via build_chain_adapter -- see
        # EntryDepositAmountNotWired's own docstring. None remains the
        # honest value for a ChainAdapter built directly (a test), and
        # read() refuses by name rather than proceed when it is genuinely
        # absent, the identical pattern ark_coldkey/dials/holding_stride
        # above already follow.
        self.entry_deposit_rao = entry_deposit_rao
        # Ticket #16 (B06), removed by #135: read() no longer calls
        # exclude_duplicate_coldkeys (see read()'s own comment at its old
        # call site), so this stays [] permanently now. Kept only because
        # dropped_duplicate_coldkeys() below still reads it, for
        # noah.core.loop.tick's persisted-state field of the same name.
        self._dropped_duplicate_coldkeys = []
        # Ticket #16 (B06), Codex finding F1: the most recent read()'s
        # dropped hotkeys for a coldkey with no confirmed 100-NOAH entry
        # deposit (see read()'s own comment at the exclude_unconfirmed_
        # entries call site). Empty until read() has run once; never
        # accumulated across periods.
        self._dropped_unconfirmed_entry_deposits = []
        # Issue #112 (traced by #12, operator view): the most recent
        # read()'s real, live, on-chain-confirmed Ark identity --
        # _confirm_ark_identity's own return value (the subnet's own
        # published SubnetIdentitiesV3[netuid] record at to_block, already
        # proved to match self.ark_coldkey by public key before read() ever
        # proceeds), captured here rather than discarded so a caller
        # persisting this read's score evidence (scripts/
        # persist_score_evidence.py, via scripts/day_close_pipeline.py) can
        # stamp the REAL live-read address, not merely echo back the
        # configured setting -- the exact distinction #12 needs its own
        # mismatch check to be worth anything. None until read() has run
        # once; never accumulated across periods, matching
        # self._dropped_duplicate_coldkeys/self._dropped_unconfirmed_entry_
        # deposits above.
        self.last_confirmed_ark_pin = None
        # Set by read(): the deposits that credit nobody (rule B, #338), for the
        # day-close report. None until a read has run.
        self.last_unattributed_deposits = None
        # Issue #291: (height, {uid: hotkey}) -- who held each seat in the very
        # seat map the last read(height) SCORED (the day's last scanned block).
        # fixture_hotkeys(height) answers from it, see there.
        self._scored_seat_hotkeys = None

    def close(self):
        """Close both reader and runner connections, even if one close raises.

        Re-raise the first error after giving both connections a chance to close.
        """
        errors = []
        for closer in (self.reader, self.runner_chain):
            try:
                closer.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    # -- the ten run_period asks of a chain -----------------------------

    def pinned_height(self):
        """The settled height to score at, or None if nothing has settled yet.

        `run_period` treats both a raised exception and a returned `None` as
        "nothing to score" (`period.py` step 1); `pinning.pin_height` raises
        `NoSettledPeriod` for the identical fact, so that is caught and
        turned into the `None` `run_period` already knows how to read rather
        than left to propagate as a second, differently-shaped signal for
        the same thing.

        Ticket #16 (B06), 2026-09-15: `tempo` is now pinned to the SAME
        `head` this method just established (`self.reader.tempo(self.netuid,
        block=head)`), rather than letting it implicitly re-resolve
        "current" a second time -- see `ChainReader.tempo`'s own docstring
        for the real chain behavior this closes.
        """
        from .pinning import NoSettledPeriod, pin_height

        head = self.reader.current_block()
        tempo = self.reader.tempo(self.netuid, block=head)
        try:
            return pin_height(head=head, tempo=tempo, settle_blocks=self.settle_blocks)
        except NoSettledPeriod:
            return None

    def already_published(self, height):
        """Whether OUR hotkey has already committed for the period `height`
        falls in.

        Checks every period from the SCORED one through the CURRENT one,
        not the scored period alone. `height` is already settled by
        `settle_blocks` (`pinning.pin_height`'s own guarantee) by the time
        this runs, so the actual commit -- sealed and submitted only after
        the period it scores has ended -- lands in the scored period's own
        block range only in the narrowest case (tempo large relative to
        settle_blocks); ordinarily it lands in a LATER period entirely.
        Checking the scored period alone (this method's own earlier
        version) checked a window the real commit could rarely or never
        actually be in, so it would answer False almost every time --
        found by codex adversarial challenge of that exact commit, and the
        precise failure `run_period`'s own docstring warns about:
        `already_published` is meant to be the CHAIN-authoritative defense
        against re-scoring and re-publishing a period after a crash
        (period.py: "the chain -- not a local file -- says it already
        happened"), and a check that structurally cannot find a real commit
        is not that defense, whatever `noah.core.loop`'s own in-memory
        skip-gate additionally provides.

        `reader.committed_in_period`'s own `hotkey=` filter does the
        comparison; no second one is built here.

        Passes `head` through to every call. The loop's own last period is
        always the CURRENT, still-in-progress one (`current_period` below),
        which has blocks past the live head that have not been produced
        yet -- not blocks that failed to read. Without a head bound,
        `committed_in_period` used to query those non-existent blocks too,
        get the same transport failure a genuinely unreadable block raises,
        and refuse every single time: live-reproduced as a validator stuck
        re-cycling this exact check for over 100 minutes, never reaching a
        score. `head` is read once, here, and reused both to find
        `current_period` and to bound the scan, so the two agree on the same
        chain height rather than risking a block landing between two
        separate reads of it.

        Ticket #16 (B06), 2026-09-15: `head` is now read FIRST (moved ahead
        of `tempo`, which has no dependency requiring the opposite order),
        and `tempo` is pinned to that SAME `head`
        (`self.reader.tempo(self.netuid, block=head)`) rather than
        implicitly re-resolving "current" on its own -- the identical real
        chain behavior `pinned_height`'s own docstring closes for the
        identical reason.

        Direct mode uses the persisted exact transaction and vector, reconciled
        against finalized chain state. Event timing alone cannot identify a day.
        """
        if self.publish_mode == "direct":
            return self._already_published_direct(height)

        head = self.reader.current_block()
        tempo = self.reader.tempo(self.netuid, block=head)

        # Issue #291/#235: a hand-set hold (restore, new machine) refuses before anything else.
        if self._publish_hold is not None:
            self._publish_hold.raise_if_held()
        # Proxy route: tripped failure limit (or inside the spacing window). Checked HERE as well as
        # in publish() so the loop WAITS (this step is before the point of no return) instead of
        # abandoning the day the way a failure at the publish step does.
        if self._failure_backoff is not None:
            self._failure_backoff.raise_if_blocked()
        # Issue #291: an open publish-intent marker means a send may have happened and
        # its outcome was never established (crash, timeout, lost loop.json). Look for
        # that commit on chain; if it is there the marker is cleared and the normal
        # decision below proceeds; if not, STOP (nothing is sent) until a person
        # reconciles. The chain scan below is the only thing that can clear it.
        if self._publish_intent is not None and not self._resolving_intent:
            marker = self._publish_intent.load()
            if marker is not None:
                if marker.get("mode") == "direct":
                    raise PublicationStatusUnknown(
                        "resolve the open direct send before switching to sealed mode"
                    )
                self._resolving_intent = True
                try:
                    found = self.already_published(marker["height"])
                finally:
                    self._resolving_intent = False
                if not found:
                    raise PublicationStatusUnknown(
                        f"{REASON_PREFIX}: a sealed commit for day {marker.get('day')} (height "
                        f"{marker['height']}) was started at {marker.get('at')} and its outcome "
                        f"was never recorded, and the chain scan does not show it. Nothing is "
                        f"sent. Check the chain for that commit, then reconcile the publication marker "
                        f"({plain_path_text(self._publish_intent.path)})."
                    )
                self._publish_intent.clear()

        # Sealed mode (issue #217). The same unit as direct mode, for the same
        # reason: a commit event carries no day, so which day a commit belongs
        # to can only come from WHERE it landed, and the day's own block range
        # -- not tempo-period arithmetic -- says where to look. The earlier
        # scan, `period_of(height)` through `period_of(head)`, answered "has
        # our hotkey committed anything since the start of the tempo period
        # containing `height`", which is not "was THIS day committed":
        #   (a2) day D-1's own commit landing late, inside the scan range of
        #        day D's last-chance tick, made already_published(D) True
        #        though D never committed; run_period raised AlreadyPublished
        #        and the loop skipped day D silently;
        #   (a1) after day D+1 committed, asking about an earlier, unpublished
        #        day D answered True;
        #   and the 358/359 shape (a day shorter than a tempo period), which
        #   cannot occur on mainnet geometry (~7200-block days, tempo 360)
        #   but is closed by the same change.
        if self._day_window is None:
            raise DayWindowNotWired(
                f"height {height}: already_published cannot tell which calendar day's "
                f"commit to look for without a live day-boundary resolver -- the same "
                f"capability direct mode requires (see this method's own docstring, "
                f"'the tempo-PERIOD scan above is the wrong unit'). A tempo-period scan "
                f"here would repeat issue #217's false positives. Nothing was checked."
            )
        day, _from_block, to_block = self._day_window(height)
        # Lower edge: a genuine commit for day D can never land at or before
        # to_block(D) -- day_block_range refuses (DayNotComplete) until the
        # chain has moved into day D+1, and the commit is sealed only after
        # that. Day D's own range, and everything earlier, is never scanned.
        scan_from = to_block + 1
        # Upper edge: a commit for day D lands before day D+1 closes unless it
        # is more than a full day late. When day D+1 is already complete its
        # last block is the bound; when it is not (the normal on-time case:
        # day D just closed) the call below refuses DayNotComplete and the
        # scan runs to head. Anything past to_block(D+1) is read as day D+1's
        # (or later), not day D's.
        #
        # Accepted tradeoff (Task Lead decision, #217): a commit for day D
        # that lands MORE than a full day late is outside this window, so the
        # day could be committed a second time. That duplicate is identical
        # content, deterministic from immutable chain data, and benign; a
        # silently skipped day is not. The bound also keeps each call to about
        # one day of block reads however long an outage lasted (the old scan
        # grew with the outage: 14,760 block reads for a two-day one).
        from ..chain.dayblocks import DayNotComplete

        scan_to = head
        try:
            _next_day, _next_from, next_to = self._day_window(to_block + 1)
            scan_to = min(head, next_to)
        except DayNotComplete:
            pass

        # Issue #334 (residuals R1/R2 of the window scan above): the per-day
        # commit ledger, when this deployment keeps one and the file exists.
        # No ledger file => everything below is skipped and the scan is exactly
        # the one above (documented fallback: restored backup / new machine).
        # A corrupt or newer-version file raises CommitLedgerRefused (a
        # ChainUnreadable: the loop waits, nothing is committed).
        ledger_days = self._commit_ledger.load() if self._commit_ledger is not None else None
        if ledger_days is not None:
            ours = self.wallet_hotkey.ss58_address
            mine = {
                d: e
                for d, e in ledger_days.items()
                if e.get("hotkey") == ours and e.get("netuid") == int(self.netuid)
            }
            entry = mine.get(day)
            if entry is not None:
                # The ledger says day D was committed at `block`. Believe it
                # only if the chain still shows our commit event there (a
                # reorged-out commit must not block a re-commit). A block the
                # node has not reached yet cannot be judged: unreadable, not
                # "absent".
                block = entry["block"]
                if block > head:
                    raise ChainUnreadable(
                        f"the commit ledger says day {day} was committed at block {block}, "
                        f"but the chain head is only {head}; cannot confirm the commit "
                        f"event yet, and nothing is decided from an unconfirmed entry. If this "
                        f"chain was rebuilt or this state directory was reused from another "
                        f"chain, the ledger is stale: it can be moved aside deliberately "
                        f"({plain_path_text(self._commit_ledger.path)}), which falls back to the "
                        f"window scan "
                        f"alone (per-day attribution is lost)."
                    )
                if self.reader.committed_in_block_range(
                    self.netuid, block, block, hotkey=ours, head=head
                ):
                    return True
                # Ledger entry present, chain event absent (reorg): not
                # published by that entry. Fall through to the window scan so
                # a re-commit whose own ledger write was lost is still found;
                # if that finds nothing the day may be committed again.
            # Blocks this ledger attributes to ANOTHER day are not day D's
            # commit (this is what fixes R1: day D-1's catch-up commit landing
            # in day D's window). Unattributed blocks still count.
            other_day_blocks = {e["block"] for d, e in mine.items() if d != day}
            if scan_to < scan_from:
                return False
            blocks = self.reader.commit_blocks_in_block_range(
                self.netuid, scan_from, scan_to, hotkey=ours, head=head
            )
            return any(b not in other_day_blocks for b in blocks)

        if scan_to < scan_from:
            return False
        return bool(
            self.reader.committed_in_block_range(
                self.netuid,
                scan_from,
                scan_to,
                hotkey=self.wallet_hotkey.ss58_address,
                head=head,
            )
        )

    def read(self, height):
        """See this module's own docstring, "What became real tonight, and
        what still refuses loudly rather than pretending". Proves
        the two configured sources genuinely agree on the height about to be
        read -- real dual-sourced chain work, not a stand-in -- then refuses
        by name for whichever capability this ``ChainAdapter`` was built
        without, rather than proceed on a fabricated input.

        **Eight separate checks, not one combined check -- and ordered to
        match the sequence below actually uses each capability, not
        declaration order.** Four of them predate #566 (an earlier version
        folded ``seats_source`` and ``day_window`` into one ``or`` condition
        behind a single message naming only #193 -- so a deployment missing
        ONLY ``day_window`` (or only ``seats_source``) got a message about
        the wrong capability; [codex adversarial review, 2026-09-01, P2]
        then found the resulting four-way split still ordered its checks
        seats/window/anchor/holdings while the code actually used them
        window/holdings/seats/anchor, reordered then to match). **#566 adds
        three more, found by direct reading rather than by review this
        time: ``store_factory``, ``ark_coldkey`` and ``dials`` had NO
        corresponding check at all** -- ``self._store_factory()``,
        ``self.ark_coldkey`` and ``self.dials`` all flowed straight through
        to ``run_day``/``build_fixture`` unexamined, the same "accidental
        shield" shape this branch's own history had already found twice
        before this issue: ``ParticipantHoldingsNotWired`` (the fourth
        check below) was the only thing standing between a real deployment
        and a bare ``TypeError`` (``None()``) or a confusing failure deep
        inside scoring, the moment a real ``holding_stride`` stopped firing
        first. All eight checks below run in the exact order the code two
        paragraphs down actually uses each capability: ``day_window`` (used
        to compute the day's own block range), ``store_factory`` (the store
        is built immediately after), ``ark_coldkey`` and ``holding_stride``
        (both consumed inside the one ``run_day`` call that follows, in the
        order they appear at that call site), ``seats_source`` (used right
        after ``run_day`` returns), ``entry_deposit_rao`` (#570: used by
        ``_entry_deposits_for`` immediately after ``seats_source``
        resolves), then ``day_anchor_ts`` and ``dials`` (both consumed
        inside the one ``build_fixture`` call that follows, in the order
        they appear at that call site).

        **Still eight, not nine, deliberately -- AS OF ISSUE #571.**
        ``ArkIdentityNotPublished``/``ArkIdentityMismatch``/
        ``ArkIdentityReorged``/``ArkIdentityChangedDuringDay`` are named,
        ``ChainAdapterRefused`` refusals matching the eight's own family,
        but none of the four is a NOT-WIRED check: nothing about them is
        "did the operator leave a setting unset" the way the eight above
        all are. They confirm a chain FACT against a setting the eight
        already proved IS wired (``ark_coldkey``), the identical
        distinction this module's own docstring already draws for
        ``observe_emission_state`` ("a missing capability is not the
        failure mode here; a chain read failing on the day is"). All four
        run after all eight, immediately once ``day_window`` resolves --
        BEFORE ``store_factory()`` runs, not merely before ``run_day``
        ([codex adversarial review, 2026-09-01] found an earlier version
        let a refusal here leave an already-created, empty store file on
        disk) -- and need both ``from_block`` and ``to_block``, not
        ``to_block`` alone (see ``_confirm_ark_identity``'s own docstring
        for why).
        """
        # Real, dual-sourced work: AgreeingChain.block_hash raises
        # SourceDisagreement (a Refusal) the moment the two configured
        # sources do not actually agree on this height, exactly the same
        # canary neurons/miner.py's own build_services runs before it lets
        # anything else touch the chain.
        self._calculation_reference = None
        self.runner_chain.block_hash(height)

        if self._day_window is None:
            raise DayWindowNotWired(
                f"height {height}: this deployment has no live calendar-day block-range "
                f"resolver. `read()` cannot translate the scored height into a day's own "
                f"(from_block, to_block) without one, so `run_day` is never called. See "
                f"DayWindowNotWired's own docstring. Nothing was published."
            )

        if self._store_factory is None:
            raise StoreFactoryNotWired(
                f"height {height}: this deployment has no live history-store builder "
                f"configured. `read()` cannot scan the day's chain facts into a "
                f"noah.chain.store.Store without one, so `run_day` is never called. See "
                f"StoreFactoryNotWired's own docstring. Nothing was published."
            )

        if self.ark_coldkey is None:
            raise ArkColdkeyNotWired(
                f"height {height}: this deployment has no Ark coldkey configured. "
                f"noah.chain.deposits.deposits_in_block (called from inside `run_day`) "
                f"keys every deposit it extracts off this value, and this ChainAdapter has "
                f"none configured. See ArkColdkeyNotWired's own docstring. Nothing was "
                f"published."
            )

        if self.designated_validator_hotkey is None:
            raise DesignatedValidatorHotkeyNotWired(
                f"height {height}: this deployment has no designated Ark Validator hotkey "
                f"configured. noah.chain.deposits.deposits_in_block (called from inside "
                f"`run_day`) checks every deposit's own destination hotkey against this "
                f"value before it can qualify for mining credit, and this ChainAdapter has "
                f"none configured. See DesignatedValidatorHotkeyNotWired's own docstring. "
                f"Nothing was published."
            )

        if self._holding_stride is None:
            raise ParticipantHoldingsNotWired(
                f"height {height}: this deployment has no live participant-holdings "
                f"sampling stride configured. noah.chain.live.LiveRunnerChain and "
                f"noah.chain.agreeing.AgreeingChain both implement the four reads a "
                f"holding sample needs (staking_hotkeys/alpha_share/hotkey_shares/"
                f"hotkey_alpha, issue #134's own reads), and noah.chain.runner."
                f"_sample_holdings now has the same reorg protection the rest of that file "
                f"does (issue #566) -- but no stride VALUE is configured: this project has "
                f"no ruled cadence for how often a holding must be sampled, and every "
                f"caller in this codebase's history has passed holding_stride=None. See "
                f"ParticipantHoldingsNotWired's own docstring for why that is a real, "
                f"unsettled policy question rather than a number to invent here. Nothing "
                f"was published."
            )

        if self._seats_source is None:
            raise SeatRegistryNotWired(
                f"height {height}: this deployment has no live seat registry reader. "
                f"noah.chain.fixture_build.build_fixture's own required `seats` argument "
                f"({{coldkey: {{hotkey: uid}}}}) has no live, block-pinned implementation "
                f"configured on this ChainAdapter. See SeatRegistryNotWired's own "
                f"docstring. Nothing was published."
            )

        # #570: checked here, matching this method's own "ordered to the
        # sequence actually uses each capability" discipline -- entry_deposit_rao
        # is used by _entry_deposits_for right after seats_source resolves,
        # before day_anchor_ts/dials are ever reached at the build_fixture
        # call site below.
        if self.entry_deposit_rao is None:
            raise EntryDepositAmountNotWired(
                f"height {height}: this deployment has no ruled entry-deposit amount "
                f"configured. _entry_deposits_for needs it to recognise a coldkey's real "
                f"entry deposit among its ordinary NOAH deposits, and this ChainAdapter has "
                f"none configured. See EntryDepositAmountNotWired's own docstring. Nothing "
                f"was published."
            )

        if self.day_anchor_ts is None:
            raise DayAnchorNotWired(
                f"height {height}: this deployment has no day_anchor_ts. "
                f"noah.chain.fixture_build.build_fixture requires it to match the store's "
                f"own day_anchor_ts exactly (day_epoch_ts != store.day_anchor_ts refuses), "
                f"and this ChainAdapter has none configured. See DayAnchorNotWired's own "
                f"docstring. Nothing was published."
            )

        if self.dials is None:
            raise DialsNotWired(
                f"height {height}: this deployment has no scoring dials configured. "
                f"noah.chain.fixture_build.build_fixture's own required `dials` argument "
                f"has no value configured on this ChainAdapter. See DialsNotWired's own "
                f"docstring. Nothing was published."
            )

        day, from_block, to_block = self._day_window(height)
        observe_window(day=day, height=height, from_block=from_block, to_block=to_block,
                       anchor=self.day_anchor_ts, sources=self.runner_chain.endpoints,
                       netuid=self.netuid, ark_address=self.ark_coldkey,
                       validator_hotkey=self.designated_validator_hotkey)

        # Issue #571: confirm self.ark_coldkey is the address the subnet
        # itself publishes, not merely a well-formed one -- read and
        # compared fresh here, on every real call, BEFORE store_factory()
        # runs (a refusal here must leave no store behind -- [codex
        # adversarial review, 2026-09-01] found an earlier version of this
        # call ran AFTER store creation) and before run_day ever scans a
        # single block for deposits to it. See _confirm_ark_identity's own
        # docstring for why netuid is passed explicitly, why from_block AND
        # to_block are both read (not just one), and ArkIdentityMismatch's
        # own docstring for why this is not computed once at
        # build_chain_adapter time and cached.
        # Captured, not discarded (issue #112): the subnet's own live
        # published address at this exact day's to_block -- see
        # self.last_confirmed_ark_pin's own docstring in __init__.
        self.last_confirmed_ark_pin = _confirm_ark_identity(
            self.runner_chain,
            netuid=self.netuid,
            ark_coldkey=self.ark_coldkey,
            from_block=from_block,
            to_block=to_block,
        )

        from ..chain.emission_state import observe_emission_state
        from ..chain.fixture_build import build_fixture
        from ..chain.runner import run_day

        store = self._store_factory()
        # Issue #567: whether the subnet has begun emitting -- a wholly
        # separate fact from day_anchor_ts above (see this module's own
        # docstring, "A third, related gap") -- is read and recorded here,
        # fresh, on every real call, so `build_fixture` below never reaches
        # its own EmissionStateNotObserved refusal on a real deployment.
        # `observed_block=to_block` ties the "as of" moment exactly to the
        # completed day being scored (see noah.chain.emission_state's own
        # module docstring, "Which block is observed"): once emission has
        # genuinely started, one reading closes the question permanently;
        # until then, calling this again on every later read() call keeps
        # re-recording the absence at a fresher block, which is what keeps
        # a later day from being left unresolved.
        observe_emission_state(self.runner_chain, store, observed_block=to_block)
        run_day(
            store,
            self.runner_chain,
            day=day,
            from_block=from_block,
            to_block=to_block,
            ark_coldkey=self.ark_coldkey,
            designated_validator_hotkey=self.designated_validator_hotkey,
            sources=self.runner_chain.endpoints,
            netuid=self.netuid,
            holding_stride=self._holding_stride,
            read_collateral=self._read_collateral,
            may_resume=self._may_resume,
            # Issue #112-15: this IS the real, live scanning path (this
            # class's own constructor already refuses a `runner_chain` that
            # is not a genuine, dual-sourced `AgreeingChain`, so
            # `chain.extrinsics`/`chain.owner_at` are always available
            # here) -- every real deposit read through it must go through
            # the real batch_all+remark attribution check, not the
            # pre-#112-15 default that left every deposit's origin
            # unattributed.
            attribute_deposits=True,
        )
        seats = self._seats_source(day, store)
        observe_seats(seats, day=day, height=height)
        scored_seat_hotkeys = {
            uid: hotkey for per_hotkey in seats.values() for hotkey, uid in per_hotkey.items()
        }

        # Multiple live seats per coldkey are permitted. Qualification is
        # checked per seat; do not exclude every seat of a shared coldkey.
        # `self._dropped_duplicate_coldkeys` stays permanently `[]` now
        # (nothing populates it any more); `dropped_duplicate_coldkeys()`
        # below and its one real caller (`noah.core.loop.tick`) are
        # unchanged in behavior -- an always-empty list was already exactly
        # what they persisted on every period with no live duplicate, which
        # is now every period.

        # Rule 1, "entry before score" (#7 issuecomment-5623324414; issue
        # #16's own body; historical parameter draft entry_cutoff_rule):
        # no score and no weight for a miner until its 100 NOAH entry
        # deposit is confirmed in the Ark. Codex finding F1 (2026-09-13,
        # this ticket): `noah.deposits.entry_gate.confirm_entry_before_score`
        # was built, correctly reviewed, and unit-tested against `Store` in
        # an earlier round, but nothing in this method ever called it -- a
        # real check with no real caller. Wired here the same structural
        # way `exclude_duplicate_coldkeys` used to be, before #135 removed
        # that call above (see the comment there): on `seats`'s own
        # resolved snapshot (never a second seat read), immediately
        # before anything downstream (`_registration_days_for`,
        # `_entry_deposits_for`, `build_fixture`) ever sees it. `day` is the
        # exact day being scored -- the same value already threaded through
        # `self._seats_source(day, store)` above -- so this always checks
        # cumulative deposits AS OF the period actually being judged, never
        # a stale or cached verdict (closing the spec's own named "epoch/
        # weight-setting lag" failure mode by construction: this call
        # happens fresh on every `read()`, never once and reused).
        #
        # The UID-takeover case needs no special handling here: this checks
        # each coldkey `seats` names against `Store`'s own real deposit
        # ledger, never a uid, so a coldkey that has just taken over a uid a
        # different coldkey previously held is judged purely on its own
        # recorded deposits -- it inherits nothing through the shared uid.
        # See `noah.deposits.entry_gate.exclude_unconfirmed_entries`'s own
        # docstring, and `noah/deposits/tests/test_entry_gate.py::
        # TestExcludeUnconfirmedEntries::test_uid_takeover_...` for the
        # proof.
        # exclude_unconfirmed_entries takes no entry_deposit_rao argument
        # so callers cannot substitute a different policy value. It reads the figure
        # itself, fresh, from noah.config.profile.entry_deposit_rao() --
        # see noah.deposits.entry_gate's own module-level docstring. That
        # is a SEPARATE setting (NOAH_PARAMETERS_JSON) from this class's
        # own self.entry_deposit_rao (NOAH_ENTRY_DEPOSIT_RAO), which
        # _entry_deposits_for below still uses for the fixture's own
        # entryDepositRao credit -- unifying those two sources is not part
        # of this ticket's own scope; see the handback report for that gap.
        seats, self._dropped_unconfirmed_entry_deposits = exclude_unconfirmed_entries(
            store, seats, as_of_day=day
        )

        # #570: registration_days and entry_deposits, wired for real rather
        # than left unpassed -- build_fixture's own silent defaults credit
        # every entry deposit as zero and every registration day as first
        # activity, proven by execution (aecfe7e5) to completely zero a real
        # foreign-matched pay_weight once foreign_rule is wired alone. See
        # _registration_days_for and _entry_deposits_for's own docstrings.
        #
        # #112, D-138 reopened: both are now genuinely per-SEAT, nested
        # {coldkey: {hotkey: value}} -- the same shape `build_fixture` now
        # accepts for these two arguments (same design, landed separately
        # in noah.chain.fixture_build), so they are forwarded below
        # directly, with no coldkey-level collapsing needed.
        registration_days = _registration_days_for(self.runner_chain, store, seats, to_block)
        entry_deposits, entry_days = _entry_deposits_for(
            store, registration_days, self.entry_deposit_rao, as_of_day=day
        )
        observe_entry_qualification(seats, self._dropped_unconfirmed_entry_deposits,
                                    entry_deposits, entry_days)

        # Exclude the exact entry allocation on each original receipt day.
        # The fixture's registrationDay triggers starting credit on the UTC
        # threshold day; actual chain registration facts remain unchanged.
        fixture_store = (
            _ExcludingEntryDeposit(store, entry_deposits, entry_days) if entry_deposits else store
        )
        fixture_registration_days = {
            coldkey: dict(per_hotkey) for coldkey, per_hotkey in registration_days.items()
        }
        for coldkey, per_hotkey_entry_days in entry_days.items():
            fixture_registration_days.setdefault(coldkey, {}).update(per_hotkey_entry_days)

        # ISSUE #16 (foreign-token credit): no longer None. A deposit in another
        # subnet's token is filed in `foreign_deposit` at the ledger write (never in
        # `noah_deposit`, which alone feeds entry and compliance) and valued here at
        # the spot price of its own block: foreign alpha -> TAO -> NOAH, both prices
        # read at that exact block through the dual-sourced chain. A price that cannot
        # be read is the named refusal `ForeignDepositUnpriced`, which stops this
        # day's read: nothing is valued at zero or guessed, and nothing is published
        # from a day with an unvalued deposit. The engine caps the credit by the
        # miner's NOAH credit.
        self._foreign_valuation_evidence = []
        foreign_rule = foreign_deposit_rule(
            self.runner_chain, self.netuid, evidence=self._foreign_valuation_evidence
        )

        # Rule B (#338): deposits that moved alpha into the Ark but credit
        # nobody (no miner hotkey named, or the wrong validator), as of this
        # day. Kept on the adapter for the day-close report only; it is not
        # part of the fixture and changes no score.
        self.last_unattributed_deposits = store.unattributed_deposits(through_day=day)

        # The raw noah-fixture/6 dict, not the parsed Fixture: score() and
        # agree() below each parse it themselves (score() for the Python
        # engine; agree()'s TypeScript side hands the raw dict to the real
        # TypeScript CLI, which does its OWN independent parse -- the whole
        # point of a second, separately-written implementation).
        fixture = build_fixture(
            fixture_store,
            name=f"height-{height}",
            eval_day=day,
            day_epoch_ts=self.day_anchor_ts,
            dials=self.dials,
            seats=seats,
            foreign_rule=foreign_rule,
            entry_deposits=entry_deposits,
            registration_days=fixture_registration_days,
        )
        # Issue #291: remember which hotkey held each uid in the seat map just
        # scored (taken before exclude_unconfirmed_entries, which only removes
        # seats; an unscored seat publishes 0 anyway).
        self._scored_seat_hotkeys = (height, scored_seat_hotkeys)
        return fixture

    def score(self, observations):
        """Never inspected by `run_period` (see `period.py`'s own comment on
        why `agree()`'s output, not this one, is what gets published) --
        called so a scoring failure is reported as scoring failing rather
        than folded into the twin proof's own message.
        """
        from ..scoring.fixture_io import parse_fixture

        return score_fixture(parse_fixture(observations))

    def agree(self, observations):
        """The twin proof, for real: the Python engine and the TypeScript
        engine, run against the SAME raw fixture dict, required to render
        identical canonical bytes (`noah.core.twin.require_agreement`)
        before anything is published. Returns the agreed vector as
        `{uid: weight}` (`noah.core.vector.from_engine_output`),
        reconstructed from whichever rendering `require_agreement` proved
        was agreed on -- not a second, separate call to either engine,
        which is exactly the gap `noah/core/tests/test_integration.py`'s
        own `StandInChain.agree` docstring names and refuses to reopen.

        The TypeScript side's own CLI already writes canonical JSON
        (sorted keys, two-space indent, trailing newline --
        `verification/noah-ts/src/cli.ts`'s own docstring) in the identical
        form `noah.scoring.fixture_io.render_output` produces, so its raw
        output text is compared as-is rather than re-serialized by this
        module a second time.
        """
        import json

        from ..scoring.fixture_io import parse_fixture, render_output
        from .ts_engine import render_with_ts_engine

        def python_engine(raw):
            return render_output(score_fixture(parse_fixture(raw)))

        def typescript_engine(raw):
            return render_with_ts_engine(raw)

        from .calculation_record import digest, encoded

        self._agreed_calculation = None
        agreed_text = require_agreement(observations, first=python_engine, second=typescript_engine)
        self._agreed_calculation = (digest(encoded(observations)), agreed_text)
        return from_engine_output(json.loads(agreed_text))

    def record_calculation(self, height, observations, agreed, vector):
        """Retain exactly the twin-agreed result before the automatic route sends."""
        import json
        import os
        from pathlib import Path

        from .calculation_record import (
            CalculationEvidenceRefused,
            digest,
            encoded,
            retain,
            source_identity,
        )

        self._calculation_reference = None
        if self._submitted_record_dir is None or self._day_window is None:
            raise CalculationEvidenceRefused("automatic publication needs durable evidence storage")
        saved = self._agreed_calculation
        if saved is None or saved[0] != digest(encoded(observations)):
            raise CalculationEvidenceRefused("no exact twin-agreed input/result pair is available")
        if from_engine_output(json.loads(saved[1])) != agreed:
            raise CalculationEvidenceRefused("agreed vector differs from the retained calculation")
        day, first, last = self._day_window(height)
        profile_snapshot = load_parameters_snapshot()
        context = {
            "day": day,
            "height": height,
            "from_block": first,
            "to_block": last,
            "genesis_hash": self.runner_chain.genesis_hash(),
            "netuid": self.netuid,
            "from_block_hash": self.runner_chain.block_hash(first),
            "to_block_hash": self.runner_chain.block_hash(last),
            "day_start_utc_unix": self.day_anchor_ts + day * DAY_SECONDS,
            "day_end_utc_unix": self.day_anchor_ts + (day + 1) * DAY_SECONDS,
            "settle_blocks": self.settle_blocks,
            "source_agreement": "required_by_ingestion",
            "ark_address": self.last_confirmed_ark_pin,
            "designated_validator_hotkey": self.designated_validator_hotkey,
            "validator_hotkey": self.wallet_hotkey.ss58_address,
            "profile_sha256": digest(profile_snapshot.source_bytes),
            "profile": profile_snapshot.profile,
            "profile_capture": "configured_file_at_evidence_time",
            "scoring_dials": self.dials,
            "candidate": source_identity(),
            "foreign_valuations": getattr(self, "_foreign_valuation_evidence", []),
        }
        self._calculation_reference = retain(
            self._submitted_record_dir,
            fixture=observations,
            agreed_text=saved[1],
            vector=vector,
            context=context,
        )
        from ..runtime_events import emit, observe_prepared

        bind_event_context("validator", day=day, genesis_hash=context["genesis_hash"],
                           candidate_sha256=context["candidate"]["source_sha256"])
        bind_event_context("validator", artifact_sha256=self._calculation_reference["sha256"])
        emit("validator", "calculation.recorded", "validator.evidence", outcome="calculated")
        observe_prepared(agreed, vector, self.publish_mode, self.publisher)
        return self._calculation_reference

    def live_uids(self, height):
        return self.reader.live_uids(self.netuid)

    def owner_uids(self):
        return self.reader.owner_uids(self.netuid)

    def fixture_hotkeys(self, height):
        """Who held each seat at the height that was scored -- the dual-
        sourced AgreeingChain's own `seat_hotkeys`, never the single-sourced
        reader (see this module's own "Two sources, never one").

        Issue #291: when `read(height)` ran on this adapter, the answer is the
        seat map that read SCORED (the day's last scanned block), not a chain
        snapshot at `height`. The score of a uid is computed for the hotkey that
        holds it in that map (pay and holdings are keyed by hotkey), so that is
        the hotkey `publishable_vector` must compare with who holds the uid now.
        `height` is the pinned height of the day, which for a catch-up is the
        start of the day: a seat registered later in the day is not in a
        snapshot there and used to be published as 0 despite a confirmed entry.
        Without a read at this height the old snapshot is the answer."""
        scored = self._scored_seat_hotkeys
        if scored is not None and scored[0] == height:
            return dict(scored[1])
        return self.runner_chain.seat_hotkeys(height)

    def chain_hotkeys(self):
        """Who holds each seat NOW, at the freshest height either source
        can answer for."""
        return self.runner_chain.seat_hotkeys(self.runner_chain.head_block())

    def dropped_duplicate_coldkeys(self):
        """Return the compatibility field for dropped duplicate coldkeys.

        The live read path permits multiple mining seats per coldkey and no longer
        populates this list. The accessor preserves the durable state record shape.
        """
        return self._dropped_duplicate_coldkeys

    def dropped_unconfirmed_entry_deposits(self):
        """Ticket #16 (B06), Codex finding F1: the hotkeys excluded by the
        most recent ``read()`` call because their coldkey's 100-NOAH entry
        deposit is not confirmed as of the day being scored (Rule 1,
        "entry before score") -- ``[]`` if none, or if ``read()`` has never
        run. Same shape and same reasoning as
        ``dropped_duplicate_coldkeys`` immediately above."""
        return self._dropped_unconfirmed_entry_deposits

    def publish(self, vector, height):
        if self._publish_hold is not None:
            self._publish_hold.raise_if_held()
        if self._failure_backoff is not None:
            self._failure_backoff.raise_if_blocked()
        intent = self._publish_intent
        if intent is not None:
            # Issue #291: record that a send may be in flight BEFORE sending. If this
            # write fails the exception stops the send (nothing is submitted).
            day = self._day_window(height)[0] if self._day_window is not None else -1
            intent.open(
                day=day, height=height, hotkey=self.wallet_hotkey.ss58_address, netuid=self.netuid,
                calculation_evidence=self._calculation_reference,
            )
        try:
            return publish_weights(
                self, vector=vector, uid=self.uid, netuid=self.netuid, hotkey=self.wallet_hotkey
            )
        except (CommitRejected, PublicationRefused) as error:
            # Nothing was accepted and the outcome is known: the chain definitively refused
            # (CommitRejected) or the publish refused before anything was submitted
            # (PublicationRefused: rate limit, vector check, seal failure). Any other failure
            # (indeterminate submit, read-back error, crash) leaves the marker open.
            if intent is not None:
                intent.clear()
            if self._failure_backoff is not None and isinstance(error, CommitRejected):
                # A definite chain refusal (on the proxy route this includes an inner failure
                # AFTER inclusion, which costs a fee): counted, so repeated failures stop.
                self._failure_backoff.record_failure(str(error))
            raise

    def record_commit(self, height, published):
        """Issue #334: record day -> confirmed commit in the ledger. Called by
        `run_period` only AFTER `publish` returned a confirmed result (never
        before), sealed mode only. NEVER raises: the commit is already on
        chain, so failing the period here would make the loop treat a
        published day as failed. A failed ledger write therefore leaves an
        unattributed commit (the pre-ledger failure mode: today's window scan
        decides) and is reported in the returned dict, which `run_period` puts
        in its result, and logged loudly.

        Residual (documented, not closed): a crash between the chain accepting
        the commit and this write finishing has the same effect."""
        if self._publish_intent is not None:
            # The commit is confirmed (we hold its block): the outcome is known, whether
            # or not the ledger write below succeeds.
            self._publish_intent.clear()
        if self._failure_backoff is not None:
            self._failure_backoff.record_success()
        if self._commit_ledger is None:
            return {"recorded": False, "reason": "no ledger configured"}
        try:
            block = published.get("block")
            if block is None:
                return {
                    "recorded": False,
                    "reason": "the publish result carries no commit block; nothing to attribute",
                }
            if self._day_window is None:
                return {"recorded": False, "reason": "no day-boundary resolver wired"}
            day = self._day_window(height)[0]
            self._commit_ledger.record(
                day=day,
                block=block,
                extrinsic_index=published.get("extrinsic_index"),
                reveal_round=published.get("reveal_round"),
                hotkey=self.wallet_hotkey.ss58_address,
                netuid=self.netuid,
            )
            return {"recorded": True, "day": day, "block": block}
        except Exception as error:  # noqa: BLE001 - see docstring: must not fail a published day
            import logging

            logging.getLogger(__name__).error(
                "COMMIT LEDGER NOT WRITTEN for height %s (%s: %s). The commit is on chain "
                "but unattributed to its day; already_published falls back to the window "
                "scan for it.",
                height,
                type(error).__name__,
                _error_text(error),
            )
            return {"recorded": False, "reason": f"{type(error).__name__}: {_error_text(error)}"}

    def record_submitted(self, height, vector, published):
        """Issue #291: record the vector just submitted (see
        `noah.core.submitted_record`). Called by `run_period` only after the
        publish call returned, either mode. NEVER raises: the weights are
        already on chain, so a failed write must not fail a published day; it
        is logged loudly and reported in the returned dict."""
        if self._submitted_record_dir is None:
            return {"recorded": False, "reason": "no submitted-record directory configured"}
        try:
            if self._day_window is None:
                return {"recorded": False, "reason": "no day-boundary resolver wired"}
            day = self._day_window(height)[0]
            path = write_submitted_record(
                self._submitted_record_dir,
                day=day,
                height=height,
                vector=vector,
                published={**published, "calculation_evidence": self._calculation_reference},
                mode=self.publish_mode,
                hotkey=self.wallet_hotkey.ss58_address,
                netuid=self.netuid,
                uid=self.uid,
                signer=getattr(self.publisher, "signer_address", None),
                route="proxy" if getattr(self.publisher, "commit_identity", None) is not None else "direct-signer",
            )
            return {"recorded": True, "day": day, "path": plain_path_text(path)}
        except Exception as error:  # noqa: BLE001 - see docstring: must not fail a published day
            import logging

            logging.getLogger(__name__).error(
                "SUBMITTED-WEIGHTS RECORD NOT WRITTEN for height %s (%s: %s). The weights are "
                "on chain; the day has no day-N.submitted.json.",
                height,
                type(error).__name__,
                _error_text(error),
            )
            return {"recorded": False, "reason": f"{type(error).__name__}: {_error_text(error)}"}

    def previous_period(self):
        """What the chain holds for the period before this one, so
        `noah.core.landed.check_previous_reveal` (`run_period` step 5) can
        confirm it against the rescale of what WE sealed -- or refuse by
        name, explicitly, rather than let a missing method raise a bare
        `AttributeError` that `check_previous_reveal`'s own `except
        Exception` swallows into a silent, permanent "unverified, continue"
        for every period this adapter ever publishes. Found by codex
        adversarial review of this exact commit: an absent method and a
        deliberate refusal are caught identically by `check_previous_reveal`
        (by design -- a check that cannot run must never halt the
        validator, see that function's own docstring), but only one of them
        is a decision anyone made, tested, or could find again later.

        Genuinely not buildable yet, for the same reason `read()` refuses:
        confirming the previous period needs the vector WE sealed for it,
        and reconstructing that needs the same real, dual-sourced
        holdings/seats pipeline #134/#193 block `read()` on. This is not a
        third, independent gap -- it is the same two, one call site over.
        """
        raise ChainAdapterRefused(
            "the previous period cannot be checked: confirming it needs the vector this "
            "validator itself sealed for it, and reconstructing that needs the same "
            "real, dual-sourced holdings (#134) and seat registration (#193) pipeline "
            "read()'s own two named refusals are gated on. noah.core.landed."
            "check_previous_reveal treats this exactly as a failed read (a chain hiccup, "
            "not a proven fault) and continues UNVERIFIED rather than halting -- by "
            "design, not a gap in that check -- but that design still expects a real "
            "attempt, not a method that quietly does not exist."
        )

    # -- noah.core.loop.tick's own two extra reads -----------------------

    def current_block(self):
        return self.reader.current_block()

    def tempo(self):
        return self.reader.tempo(self.netuid)

    def closed_day_ready(self, day):
        """Cheap scheduling hint; the pipeline still resolves the exact day range.

        Use both readers' finalized history, with the configured block margin
        beyond midnight. This does not scan, select a new scoring height, or
        establish permission to publish. Host wall-clock time is irrelevant.
        """
        if isinstance(day, bool) or not isinstance(day, int) or day < 0:
            raise ChainUnreadable(f"invalid pending day: {day!r}")
        head = self.runner_chain.head_block()
        if isinstance(head, bool) or not isinstance(head, int) or head < 0:
            raise ChainUnreadable(f"invalid finalized head: {head!r}")
        settled = head - self.settle_blocks
        if settled < 1:
            return False
        self.runner_chain.block_hash(settled)
        timestamp = self.runner_chain.block_timestamp(settled)
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise ChainUnreadable(f"invalid finalized block timestamp: {timestamp!r}")
        return timestamp >= self.day_anchor_ts + (day + 1) * DAY_SECONDS

    # -- publish_weights / check_can_publish_yet's own extra five --------

    def last_update(self, uid, netuid):
        return self.reader.last_update(uid, netuid)

    def rate_limit(self, netuid):
        return self.reader.rate_limit(netuid)

    def seal(self, uids, values, hotkey):
        return self.publisher.seal(uids, values, hotkey)

    def submit(self, commit, reveal_round, netuid):
        if self._publish_intent is not None:
            self._identify_send_in_marker(commit, reveal_round, netuid)
        return self.publisher.submit(commit, reveal_round, netuid)

    def _identify_send_in_marker(self, commit, reveal_round, netuid):
        """Issue #291: write into the open marker what a person needs to find this exact
        transaction (signer, call, netuid, sha256 of the sealed bytes, the nonce and head
        read before the send). Real fields only; anything unreadable is ``None``. A failure
        here never blocks the send: the marker is already open, which is the protection."""
        import hashlib
        import logging

        from .submitter import CALL, MODULE

        try:
            # `signer` is the key that signs (the proxy signer on the proxy route; the
            # validator itself otherwise). `commit_identity` is the validator the chain
            # sees as the committer. They are different accounts on the proxy route, and the
            # nonce below belongs to the signer.
            signer = getattr(self.publisher, "signer_address", None) or self.wallet_hotkey.ss58_address
            fields = {
                "signer": signer,
                "commit_identity": self.wallet_hotkey.ss58_address,
                "route": "proxy" if getattr(self.publisher, "commit_identity", None) is not None else "direct-signer",
                "call": f"{MODULE}.{CALL}",
                "netuid": int(netuid),
                "reveal_round": reveal_round,
                "commit_sha256": hashlib.sha256(bytes(commit)).hexdigest()
                if isinstance(commit, (bytes, bytearray))
                else None,
                "sent_at": _utc_now_iso(),
            }
            context = getattr(self.publisher, "send_context", None)
            fields.update(context(signer) if context is not None else {})
            self._publish_intent.annotate(**fields)
        except Exception as error:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "could not add the transaction identity to the publish-intent marker: %s: %s",
                type(error).__name__,
                error,
            )

    def read_map(self, name, netuid):
        return self.publisher.read_map(name, netuid)

    # -- publish_weights_direct's own extra two (ticket #16, B06) --------
    #
    # Ticket #16 (B06), issue #56 comment 5720306237. Delegates exist so
    # publish_weights_direct can be handed a real ChainAdapter exactly the
    # way publish_weights already is above -- neither of these two is ever
    # called from the four delegates above them, on failure or otherwise.

    def submit_direct(self, uids, values, netuid):
        result = self.publisher.submit_direct(uids, values, netuid)
        # Failure to persist the response leaves the original marker intact.
        # Recovery then searches by signer, nonce and complete call.
        self._publish_intent.annotate(result=result)
        return result

    def weights_set_for_seat(self, netuid, uid, from_block, to_block):
        return self.reader.weights_set_for_seat(netuid, uid, from_block, to_block)

    def confirm_direct(self, *, vector, result):
        from .direct import confirm

        marker = self._publish_intent.load()
        if marker is None or marker.get("vector") != {str(k): v for k, v in vector.items()}:
            raise PublicationStatusUnknown("direct publication intent is missing or changed")
        return confirm(self.reader, marker, result, finalized_chain=self.runner_chain)

    def _publication_event(self, marker, outcome, evidence=None):
        from ..runtime_events import emit

        reference = marker.get("calculation_evidence") or {}
        evidence = evidence or {}
        emit(
            "validator", "publication.state", "validator.publication",
            outcome=outcome, day=marker.get("day"), netuid=marker.get("netuid"),
            uid=marker.get("uid"), intent_id=marker.get("intent_id"),
            validator_hotkey=marker.get("hotkey"),
            artifact_sha256=reference.get("sha256"),
            genesis_hash=reference.get("genesis_hash"),
            candidate_sha256=reference.get("candidate_sha256"),
            height=evidence.get("block"), block_hash=evidence.get("block_hash"),
            extrinsic_index=evidence.get("extrinsic_index"),
        )

    def _check_direct_identity(self, marker):
        from .direct import same_address

        if (
            marker.get("mode") != "direct"
            or marker.get("netuid") != self.netuid
            or marker.get("uid") != self.uid
            or marker.get("mecid") != getattr(self.publisher, "mecid", 0)
            or not same_address(marker.get("hotkey"), self.wallet_hotkey.ss58_address)
        ):
            raise PublicationStatusUnknown(
                "publication marker belongs to a different identity or mode"
            )

    def _already_published_direct(self, height):
        import json
        from pathlib import Path

        from .direct import reconcile
        from .submitted_record import submitted_record_path

        if self._publish_hold is not None:
            self._publish_hold.raise_if_held()
        if self._day_window is None:
            raise DayWindowNotWired("direct publication requires the scoring day boundaries")
        if self._publish_intent is None or self._submitted_record_dir is None:
            raise PublicationStatusUnknown(
                "direct publication requires durable intent and submitted records"
            )
        day, _, to_block = self._day_window(height)
        marker = self._publish_intent.load()
        if marker is not None:
            self._check_direct_identity(marker)
            try:
                evidence = reconcile(self.reader, marker, finalized_chain=self.runner_chain)
                self._save_direct_confirmation(marker, evidence)
                self._publication_event(marker, "reconciled", evidence)
            except Exception as error:
                raise PublicationStatusUnknown(
                    f"{REASON_PREFIX}: direct send remains unresolved "
                    f"({type(error).__name__}: {error})"
                ) from error
            self._publish_intent.clear()
            if self._failure_backoff is not None:
                self._failure_backoff.record_success()
        path = submitted_record_path(self._submitted_record_dir, day)
        missing = object()
        try:
            record = json.loads(Path(path).read_text())
        except FileNotFoundError:
            record = missing
        except (ValueError, OSError) as error:
            raise PublicationStatusUnknown("direct submitted record is unreadable") from error
        if record is not missing:
            if not isinstance(record, dict):
                raise PublicationStatusUnknown("direct submitted record is not a mapping")
            marker = record.get("direct_intent")
            if not isinstance(marker, dict) or marker.get("day") != day:
                raise PublicationStatusUnknown(
                    "legacy direct record needs exact transaction reconciliation"
                )
            self._check_direct_identity(marker)
            reference = marker.get("calculation_evidence")
            if reference is not None:
                from .calculation_record import verify

                try:
                    verify(self._submitted_record_dir, reference)
                except Exception as error:
                    raise PublicationStatusUnknown(
                        "confirmed publication has missing or invalid calculation evidence"
                    ) from error
            reconcile(self.reader, marker, finalized_chain=self.runner_chain)
            return True
        # An old event cannot tell which day's vector it applied. Preserve old
        # deployments by holding for reconciliation rather than silently
        # skipping a day or resending it. No local record + no events means no
        # evidence of a previous direct publication for the closed day.
        head = self.reader.current_block()
        if head > to_block:
            # LastUpdate is only a candidate location, never proof that this
            # day was published. A real matching event anywhere in this same
            # history range already requires the refusal below. Check that
            # candidate first so an old, missing receipt (notably short Day 0)
            # does not force a genesis-to-head scan on every process restart.
            # A negative, unusable or unreadable hint cannot permit a send:
            # only the unchanged complete scan can establish absence.
            observed_event = False
            try:
                hint = self.reader.last_update(self.uid, self.netuid)
                if type(hint) is int and to_block < hint <= head:
                    observed_event = self.reader.weights_set_for_seat(
                        self.netuid, self.uid, hint, hint
                    ) is True
            except Exception:  # Optional hint; retain the authoritative scan.
                pass
            if observed_event:
                raise PublicationStatusUnknown(
                    "unattributed direct publication history; reconcile before sending"
                )
            events = self.reader.weights_set_blocks_for_seat(
                self.netuid, self.uid, to_block + 1, head
            )
            if events is None or events:
                raise PublicationStatusUnknown(
                    "unattributed direct publication history; reconcile before sending"
                )
        if self._failure_backoff is not None:
            self._failure_backoff.raise_if_blocked()
        return False

    def _save_direct_confirmation(self, marker, evidence):
        reference = marker.get("calculation_evidence")
        if reference is not None:
            from .calculation_record import verify

            manifest = verify(self._submitted_record_dir, reference)
            if (manifest["day"] != marker["day"]
                    or manifest["publishable_weights_u16"] != marker["vector"]):
                raise PublicationStatusUnknown(
                    "calculation evidence differs from the direct intent"
                )
        result = {
            "block": evidence["block"],
            "extrinsic_index": evidence["extrinsic_index"],
            "block_hash": evidence["block_hash"],
        }
        marker = {**marker, "result": result}
        write_submitted_record(
            self._submitted_record_dir,
            day=marker["day"],
            height=marker["height"],
            vector={int(k): v for k, v in marker["vector"].items()},
            published={**result, "direct_intent": marker, "applied_weights_readback": evidence,
                       "calculation_evidence": reference},
            mode="direct",
            hotkey=marker["hotkey"],
            netuid=marker["netuid"],
            uid=marker["uid"],
            signer=marker["signer"],
            route=marker["route"],
        )

    def publish_direct(self, vector, height):
        """Persist the complete direct intent before sending, with restart guards."""
        import uuid

        from .publish import _validated_vector

        _validated_vector(vector)
        if self._already_published_direct(height):
            raise PublicationRefused("this day's direct publication is already confirmed")
        day = self._day_window(height)[0]
        signer = self.publisher.signer_address
        context = self.publisher.send_context(signer)
        if (
            type(context.get("head_at_send")) is not int
            or type(context.get("account_nonce_at_head")) is not int
            or not signer
        ):
            raise PublicationRefused(
                "direct send requires a readable signer nonce and head for recovery"
            )
        marker = dict(
            intent_id=str(uuid.uuid4()),
            day=day,
            height=height,
            hotkey=self.wallet_hotkey.ss58_address,
            netuid=self.netuid,
            mode="direct",
            uid=self.uid,
            mecid=self.publisher.mecid,
            version_key=self.publisher.version_key,
            signer=signer,
            route="proxy" if self.publisher.commit_identity is not None else "direct-signer",
            vector={str(uid): value for uid, value in vector.items()},
            calculation_evidence=self._calculation_reference,
            **context,
        )
        # One fsynced write, including the vector and transaction context. A failed
        # write stops the send. Never overwrite an unresolved marker.
        self._publish_intent.open(**marker, exclusive=True)
        self._publication_event(marker, "submitting")
        try:
            published = publish_weights_direct(
                self, vector=vector, uid=self.uid, netuid=self.netuid, hotkey=self.wallet_hotkey
            )
        except (CommitRejected, PublicationRefused) as error:
            self._publication_event(
                marker, "rejected" if isinstance(error, CommitRejected) else "refused"
            )
            self._publish_intent.clear()
            if self._failure_backoff is not None and isinstance(error, CommitRejected):
                self._failure_backoff.record_failure(str(error))
            raise
        except Exception:
            self._publication_event(marker, "uncertain")
            raise
        # Retain the marker if this durable record fails. A restart will verify
        # the exact same transaction and write the record without sending again.
        try:
            self._save_direct_confirmation(marker, published["applied_weights_readback"])
        except Exception:
            self._publication_event(marker, "uncertain")
            raise
        self._publication_event(marker, "finalized", published["applied_weights_readback"])
        self._publish_intent.clear()
        if self._failure_backoff is not None:
            self._failure_backoff.record_success()
        published["calculation_evidence"] = self._calculation_reference
        published["direct_intent"] = {
            **marker,
            "result": {
                "block": published["block"],
                "extrinsic_index": published["extrinsic_index"],
                "block_hash": published["block_hash"],
            },
        }
        return published


def _default_validator_history_db_path(netuid, ark_address):
    """Where this validator's own local history store lives when
    ``NOAH_HISTORY_DB`` is not set (#566).

    Ported from (and namespaced the identical way as) ``neurons/miner.py``'s
    own ``_default_history_db_path`` -- netuid AND Ark address, not just the
    chain endpoint, so switching ``NOAH_NETUID`` or pointing this validator
    at a different Ark cannot silently reopen a DIFFERENT store under the
    same default path (that function's own docstring has the full history
    of why). NOT reused directly across the ``neurons``/``noah`` audit
    boundary (``noah/audit/tests/test_signers.py``), the same reasoning
    ``noah.chain.sources.canonical_endpoint`` already documents for its own
    port of ``neurons/miner.py``'s ``_canonical_endpoint``.

    **Under a DIFFERENT base directory than the miner's own default**
    (``~/.noah-validator/...``, not ``~/.noah-miner/...``), deliberately: a
    miner and a validator running on the same machine, for the same Ark,
    read and write different things into their own local history over
    time, and defaulting them to the SAME sqlite file would mean two
    independent processes writing to one file with no coordination between
    them, silently, unless an operator went looking. An operator who
    genuinely wants one shared store for both still can, by setting
    ``NOAH_HISTORY_DB`` explicitly to the same path for both processes --
    naming it IS the acknowledgment, the identical philosophy
    ``neurons/miner.py``'s own ``_history_db_path`` already applies to its
    legacy-path migration check.

    No legacy-path migration check here, unlike the miner's own function:
    this validator had no history store before #566 wired ``store_factory``
    for the first time, so there is no earlier default path any store could
    already exist at.

    **Named rather than closed: this path is not namespaced by chain
    identity, only by netuid and Ark address, so two DIFFERENT chains
    sharing both (a public network and a local rebuild reusing the same
    netuid/Ark for testing, for example) share the same default path too.**
    Found by real codex adversarial review of this exact commit
    (2026-09-01); ``neurons/miner.py``'s own ``_default_history_db_path``
    has the identical limitation, unaddressed, for the identical reason.
    Partially, not fully, mitigated by an existing check: ``noah.chain.
    store.Store.create``'s own identity check (``_anchor_mismatch``, keyed
    on ``day_anchor_ts``, itself derived from the chain's own genesis
    block) refuses, by name, the moment a path is reopened under an anchor
    that actually DIFFERS from the one already stored there -- catching the
    ORDINARY case (two chains with different genesis moments). Real codex
    adversarial review of this exact docstring (round 2, 2026-09-01) found
    the claim as first written ("not silently unsafe") too broad: two
    DIFFERENT chains whose genesis moments happen to floor to the SAME UTC
    day (not a contrived coincidence -- a local chain rebuilt the same day
    as a real one it shares a netuid/Ark with for testing, for example) produce
    the SAME stored anchor, and ``Store.create`` cannot tell that apart
    from a genuine reopen of its own prior store; the full silent-collision
    risk this section opens with remains real for exactly that case.
    Closing this at the path-naming level too (rather than relying on the
    Store's own after-the-fact, anchor-day-collision-shaped check) would
    mean deciding what "chain identity" means for this purpose -- the
    genesis hash, the network name, something else -- a real design
    question, not a wiring fix, and out of this issue's own scope.
    """
    import os

    default_state_dir = os.path.expanduser("~/.noah-validator")
    return os.path.join(default_state_dir, f"netuid-{netuid}", f"ark-{ark_address}", "history.db")


def _real_dials(settings):
    """Read and validate the seven dials in ``settings.dials_json``.

    Use ``scripts/mainnet_dials_from_profile.py`` with the selected parameter
    profile to generate this file. Missing or malformed settings are refused;
    fixture defaults are never substituted.
    """
    import json

    path = settings.dials_json
    if not path:
        raise DialsNotWired(
            "NOAH_DIALS_JSON must name a JSON file carrying this subnet's real scoring "
            "dials before a real ChainAdapter can be built. There is no default. "
            "Generate the file with scripts/mainnet_dials_from_profile.py using the "
            "selected scoring profile. Required fields: targetFp, harshness, capFp, "
            "halfLifeDays, streakCeilingFp, streakRampDays, ongoingHoldRao. Nothing was read."
        )
    try:
        with open(path) as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as failure:
        raise DialsNotWired(
            f"NOAH_DIALS_JSON names {plain_path_text(path)}, and it could not be read as the "
            f"dials JSON this wiring needs ({type(failure).__name__}). Nothing was built."
        ) from None
    try:
        _parse_dials(raw, "NOAH_DIALS_JSON")
    except FixtureError as failure:
        raise DialsNotWired(
            f"NOAH_DIALS_JSON names {plain_path_text(path)}, and its contents are not a valid "
            f"dials record: {scrub_endpoint_text(failure)}. Nothing was built."
        ) from None
    # The raw, unparsed dict, matching every other real caller of
    # build_fixture's own dials= argument (noah.chain.fixture_build.
    # build_fixture stores it as-is; score()/agree() each parse it
    # themselves) -- _parse_dials above is run once here only to VALIDATE
    # the file before this adapter is built, the identical "cheapest and
    # most likely to be wrong first" discipline every other check in this
    # function already applies, not because a parsed Dials object is what
    # ChainAdapter.dials is meant to hold.
    return raw


#: (the dial's own NOAH_DIALS_JSON/live-dials field name, the shared
#: attribute name noah.scoring.fixture_io.Dials and
#: noah.scoring.parameters.Parameters both use for it) -- one entry per
#: dial DialsAmountMismatched's own docstring cross-checks between the two.
#: entry_deposit_rao/entryDepositPerHotkeyRao is deliberately not here:
#: that figure already has its own real cross-check
#: (EntryDepositAmountMismatched, immediately above in build_chain_adapter),
#: is not one of the seven fields _parse_dials validates, and is not a
#: field either NOAH_DIALS_JSON or noah.scoring.fixture_io.Dials carries
#: at all.
_DIALS_PROFILE_FIELDS = (
    ("targetFp", "target_fp"),
    ("harshness", "harshness"),
    ("capFp", "cap_fp"),
    ("halfLifeDays", "half_life_days"),
    ("streakCeilingFp", "streak_ceiling_fp"),
    ("streakRampDays", "streak_ramp_days"),
    ("ongoingHoldRao", "ongoing_hold_rao"),
)


def _dials_profile_disagreements(dials_raw, profile):
    """Every one of the seven scoring dials on which ``dials_raw`` (the raw
    ``NOAH_DIALS_JSON`` dict ``_real_dials`` already validated) and
    ``profile`` (a ``noah.scoring.parameters.Parameters``, read from
    ``NOAH_PARAMETERS_JSON``) disagree, as ``(json_field_name, dials_value,
    profile_value)`` tuples, in ``_DIALS_PROFILE_FIELDS`` order -- see
    ``DialsAmountMismatched``'s own docstring for why re-parsing
    ``dials_raw`` via ``_parse_dials`` here (rather than comparing the raw
    JSON encodings directly) is what makes the ``harshness``
    ``int``-vs-``Fraction`` comparison, and every other field's
    canonical-decimal-string-vs-plain-int encoding, safe as a plain
    ``==``: both ``_parse_dials`` and ``noah.scoring.parameters.
    parse_parameters`` already normalise their own side to one canonical
    Python type before this function ever compares them.
    """
    parsed_dials = _parse_dials(dials_raw, "NOAH_DIALS_JSON")
    mismatches = []
    for json_name, attr in _DIALS_PROFILE_FIELDS:
        dials_value = getattr(parsed_dials, attr)
        profile_value = getattr(profile, attr)
        if dials_value != profile_value:
            mismatches.append((json_name, dials_value, profile_value))
    return mismatches


def _default_canonical_score_path(day):
    """Return the canonical score path for eval_day.

    NOAH_RUNTIME_ROOT overrides the legacy checkout-depth-derived default. The
    ticket-16/scores directory name is retained for existing evidence consumers.
    """
    import os
    import pathlib

    override = os.environ.get("NOAH_RUNTIME_ROOT", "").strip()
    if override:
        runtime_root = pathlib.Path(override)
    else:
        # chain_adapter.py sits at <checkout>/services/noah/noah/core/
        # chain_adapter.py -- parents[2] is <checkout>/services/noah,
        # the identical depth persist_score_evidence.py's own
        # _SOFTWARE_NOAH_ROOT (scripts/persist_score_evidence.py,
        # parents[1]) resolves to, from a different starting file.
        software_noah_root = pathlib.Path(__file__).resolve().parents[2]
        repo_root = software_noah_root.parent.parent
        runtime_root = repo_root.parent.parent / "runtime"
    return runtime_root / "ticket-16" / "scores" / f"day-{day}.json"


def _default_canonical_score_reader():
    """A real ``canonical_score_for`` closure reading day D's own
    canonical score from ``_default_canonical_score_path(day)`` -- issue
    #212's phase-two fix.

    Returns the parsed ``noah-score-output/6`` dict when the file exists
    and parses as JSON, or ``None`` when it is simply absent (day D was
    never scored/attempted locally -- ``already_published``'s own
    ``CanonicalScoreUnavailable`` is what turns that ``None`` into a
    loud, named refusal rather than a silent wrong answer, so this stays
    a plain, honest "found it or didn't" rather than refusing itself). A
    file that EXISTS but fails to parse as JSON is a different, more
    concerning fact than "absent" -- a real file this deployment itself
    wrote is corrupt -- so that propagates as an ordinary exception rather
    than being folded into the same "no record" case an absent file is.
    """

    def canonical_score_for(day):
        import json

        path = _default_canonical_score_path(day)
        if not path.exists():
            return None
        with open(path) as handle:
            return json.load(handle)

    return canonical_score_for


def build_chain_adapter(
    *,
    client,
    settings,
    reader,
    wallet,
    uid,
    seats_source=None,
    holding_stride=None,
    day_window=None,
    ark_coldkey=None,
    designated_validator_hotkey=None,
    dials=None,
    day_anchor_ts=None,
    read_collateral=False,
    store_factory=None,
    entry_deposit_rao=None,
    may_resume=True,
    publish_mode=None,
    canonical_score_for=None,
    commit_ledger_path=None,
    publish_intent_path=None,
    publish_hold_path=None,
    publish_backoff_path=None,
    submit_client=None,
    submitted_record_dir=None,
):
    """Build the validator's live ChainAdapter with two independent read sources.

    The bittensor client backs the chain reader and signing. Two separate
    SubstrateInterface connections back AgreeingChain; NOAH_ENDPOINT_2 is required.
    Missing optional injected capabilities are derived from chain state or selected
    settings. After both reader connections are created, later construction
    failures close both.

    read_collateral defaults to False. Enabling it requires the target runtime to
    expose the collateral storage read by LiveRunnerChain; an unsupported map causes
    a scan refusal. With it disabled, locked/unlocked pay attribution is limited.
    Check actual runtime metadata before changing this option.

    An explicit publish_mode wins; otherwise settings selects the mode, defaulting
    to sealed. may_resume defaults to True for normal unattended operation. False
    requests a deliberate re-scan and should only be used with a reviewed day and
    preserved evidence.
    """
    # Checked first, before any connection is opened: SubstrateInterface
    # connects eagerly in its own constructor, so a run with no second
    # endpoint configured would otherwise pay for (and could hang on) a
    # real network connection to the FIRST source only to be refused one
    # line later. Matches neurons/miner.py's own build_services ordering
    # ("cheapest and most likely to be wrong first"). This IS the refusal
    # -- raised directly, by name, rather than deferred to
    # require_dual_sourced by constructing a single-source runner_chain and
    # letting ChainAdapter's own constructor catch it; deferring would mean
    # opening the primary connection for nothing.
    if not settings.second_chain_endpoint:
        raise SingleSourceChainRefused(
            "NOAH_ENDPOINT_2 is not set. This project's chain reads are never "
            "single-sourced (see require_dual_sourced's own docstring) -- a single "
            "endpoint has already been observed answering a plausible-looking wrong "
            "value for an individual read with no error. Configure a second, "
            "independent chain endpoint before this adapter can be built. Nothing "
            "was read, and no connection was opened."
        )

    from async_substrate_interface import SubstrateInterface

    from ..chain.agreeing import AgreeingChain
    from ..chain.live import LiveRunnerChain
    from .submitter import Publisher

    primary_endpoint = settings.chain_endpoint or getattr(client, "endpoint", None)
    if not primary_endpoint:
        raise SingleSourceChainRefused(
            "the primary connection's endpoint URL could not be determined (neither "
            "NOAH_CHAIN_ENDPOINT nor the connected client's own resolved endpoint is "
            "set), so a LiveRunnerChain cannot be built from it. Nothing was read."
        )

    # Canonicalized before AgreeingChain's own distinct-endpoint check ever
    # compares them -- noah.chain.sources.canonical_endpoint, ported from
    # (and kept in exact sync with) neurons/miner.py's own
    # _canonical_endpoint rather than imported across the neurons/noah audit
    # boundary noah/audit/tests/test_signers.py enforces (see
    # canonical_endpoint's own docstring for why it lives in noah/chain/,
    # not a straight `from neurons.miner import ...`). Found by codex
    # adversarial review of this exact commit: an operator who spells
    # NOAH_CHAIN_ENDPOINT and NOAH_ENDPOINT_2 as two different-looking
    # spellings of the SAME real node (ws://node vs ws://node:80, an IPv6
    # literal in two forms, a Unicode hostname and its punycode form) used
    # to pass AgreeingChain's construction-time distinct-source check on the
    # raw strings alone -- agreement theatre through a URL alias, precisely
    # the bug neurons/miner.py's own _dual_sourced_runner_chain already had
    # to close once (issue #511) and this file reopened by not reusing that
    # fix. LiveRunnerChain.endpoint itself still carries each source's own
    # literal, operator-typed string (unchanged below): error messages
    # should say what the operator wrote, not a normalized form they never
    # entered.
    from ..chain.sources import canonical_endpoint, connect_scrubbed

    # Reads use explicit RPC/storage calls; dynamic discovery is not required.
    # Once both connections exist, the following guarded construction closes
    # both on a later failure. Successful construction transfers ownership to
    # the adapter, whose caller must close it.
    primary = LiveRunnerChain(
        connect_scrubbed(
            lambda: SubstrateInterface(url=primary_endpoint, auto_discover=False),
            primary_endpoint,
            ChainAdapterRefused,
        ),
        netuid=settings.netuid,
        endpoint=primary_endpoint,
    )
    second = LiveRunnerChain(
        connect_scrubbed(
            lambda: SubstrateInterface(url=settings.second_chain_endpoint, auto_discover=False),
            settings.second_chain_endpoint,
            ChainAdapterRefused,
        ),
        netuid=settings.netuid,
        endpoint=settings.second_chain_endpoint,
    )
    succeeded = False
    try:
        runner_chain = AgreeingChain(
            sources={"primary": primary, "secondary": second},
            endpoints={
                "primary": canonical_endpoint(primary_endpoint),
                "secondary": canonical_endpoint(settings.second_chain_endpoint),
            },
        )

        # seats_source, day_window, and day_anchor_ts are wired here, for real,
        # from the dual-sourced runner_chain just built above -- reading and
        # computing each one, never handing a new DEFAULT VALUE to ChainAdapter's
        # own constructor arguments (this module's own docstring, "test-only
        # capability injection points... any future production wiring... belongs
        # INSIDE build_chain_adapter"). Each is computed only when the caller did
        # not already supply one, matching every other optional argument this
        # function already forwards unchanged when given -- so a test may still
        # override any of the three directly.
        #
        # day_anchor_ts first: day_window's own real implementation needs it, and
        # it is cheap (one already-dual-sourced, canary-checked block read).
        # Floored to the nearest UTC midnight the moment it is read, matching
        # exactly what noah.chain.store.Store does internally to whatever anchor
        # it is given (Store.create/Store.open) -- so this value and whatever
        # store_factory's own store ends up storing agree by construction, not
        # by both sides happening to floor the same raw input the same way.
        # (noah.chain.fixture_build.build_fixture's own day_epoch_ts !=
        # store.day_anchor_ts check depends on exactly this equality holding.)
        if day_anchor_ts is None:
            from ..chain.dayblocks import day_anchor as _read_day_anchor

            day_anchor_ts = (_read_day_anchor(runner_chain) // DAY_SECONDS) * DAY_SECONDS

        if day_window is None:
            from ..chain.dayblocks import day_block_range

            def day_window(height):
                return day_block_range(runner_chain, day_anchor_ts, height)

        if seats_source is None:
            from ..chain.seats import current_seats

            def seats_source(day, store):
                # read()'s own run_day call always runs, and completes, before
                # seats_source is ever invoked (noah.chain.runner.run_day marks
                # a day scanned unconditionally on a normal return, whether that
                # is a fresh scan or a may_resume skip of an already-scanned
                # day), so store.scanned_range(day) is always populated here --
                # its own to_block is the exact range-end block
                # tools/chain-extract/extract.py's own ported logic (see
                # noah.chain.seats's own module docstring) reads current seats
                # at.
                to_block = store.scanned_range(day)[1]
                return current_seats(runner_chain, to_block)

        # #566: ark_coldkey, store_factory, holding_stride and dials, each
        # investigated on its own merits rather than assumed to need the same
        # treatment (see this module's own docstring, "AS OF ISSUE #566" for
        # the full reasoning behind each). None of these four is a chain fact
        # the way seats_source/day_window/day_anchor_ts are, so none of them is
        # computed FROM runner_chain the way those three are above -- each
        # reads a real, operator-configured value instead, and refuses by name
        # when that configuration is absent, rather than defaulting to a value
        # invented here.
        #
        # ark_coldkey first: not a chain read, an operator-configured identity
        # -- the same NOAH_ARK_ADDRESS neurons/miner.py's own ARK_ADDRESS_VAR
        # already is (see ArkColdkeyNotWired's own docstring). Read before
        # store_factory below because the validator's own default history-db
        # path is namespaced by it, the same way neurons/miner.py's own
        # _default_history_db_path already namespaces the miner's.
        if ark_coldkey is None:
            if not settings.ark_address:
                raise ArkColdkeyNotWired(
                    "NOAH_ARK_ADDRESS is not set. noah.chain.deposits.deposits_in_block keys "
                    "every deposit it extracts off the Ark's own coldkey, and there is no "
                    "default: guessing it would attribute deposits to the wrong address "
                    "entirely. Configure it -- the identical setting neurons/miner.py's own "
                    "preflight already requires -- before this adapter can be built. Nothing "
                    "was read."
                )
            ark_coldkey = settings.ark_address

        # designated_validator_hotkey (issue #282 item 4/4a, #302): the
        # same treatment as ark_coldkey immediately above -- an operator-
        # configured identity, never a chain read, and never defaulted to
        # a value invented here. This is where a real mainnet deployment
        # with no NOAH_VALIDATOR_HOTKEY set fails closed: there is no
        # fallback to another environment's configured address, only this
        # refusal. See DesignatedValidatorHotkeyNotWired's own docstring.
        if designated_validator_hotkey is None:
            if not settings.designated_validator_hotkey:
                raise DesignatedValidatorHotkeyNotWired(
                    "NOAH_VALIDATOR_HOTKEY is not set. noah.chain.deposits.deposits_in_block "
                    "checks every deposit's own destination hotkey against it before the "
                    "deposit can qualify for mining credit, and there is no default: guessing "
                    "it would either check nothing or check against the wrong validator "
                    "entirely, silently crediting or discrediting real mining. Configure it -- "
                    "the identical setting scripts/daily_deposit_service.py's own "
                    "_resolve_validator_hotkey already requires for the writer side -- before "
                    "this adapter can be built. Nothing was read."
                )
            designated_validator_hotkey = settings.designated_validator_hotkey

        # store_factory: a real noah.chain.store.Store.create closure, at a
        # path namespaced by netuid and Ark coldkey (mirroring neurons/
        # miner.py's own _default_history_db_path, ported rather than imported
        # across the neurons/noah audit boundary the same way canonical_endpoint
        # already was -- see _default_validator_history_db_path's own
        # docstring), unless NOAH_HISTORY_DB names an explicit path. Deferred:
        # the closure itself does the real os.makedirs/Store.create work, and
        # refuses as HistoryStoreUnwritable, only when read() actually calls it
        # -- store_factory is a per-call builder (this module's own docstring,
        # "built fresh, per call"), not a one-time-at-startup connection, so a
        # filesystem failure belongs to whichever call hits it, not to this
        # function.
        if store_factory is None:
            history_db = settings.history_db or _default_validator_history_db_path(
                settings.netuid, ark_coldkey
            )

            def store_factory():
                import os
                import sqlite3

                try:
                    os.makedirs(os.path.dirname(history_db) or ".", exist_ok=True)
                except OSError as failure:
                    raise HistoryStoreUnwritable(
                        f"the history store's directory for {plain_path_text(history_db)} could "
                        f"not be created ({type(failure).__name__}). Nothing was scanned and "
                        f"nothing was built."
                    ) from None
                try:
                    return Store.create(history_db, day_anchor_ts=day_anchor_ts)
                except sqlite3.Error as failure:
                    raise HistoryStoreUnwritable(
                        f"the history store at {plain_path_text(history_db)} could not be opened "
                        f"({type(failure).__name__}). Nothing was scanned and nothing was built."
                    ) from None

        # holding_stride: NOT computed here and NOT a chain fact -- an
        # operator-configured cadence, because no ruled value for it exists
        # anywhere in this project (see ParticipantHoldingsNotWired's own
        # docstring, quoting noah/chain/store.py's own module docstring: "over
        # what stretch of time" a holding must be held to count "is not
        # settled").
        if holding_stride is None:
            if not settings.holding_stride_blocks:
                raise ParticipantHoldingsNotWired(
                    "NOAH_HOLDING_STRIDE_BLOCKS is not set. noah.chain.runner._sample_holdings "
                    "now has real reorg protection (issue #566), but no ruled sampling cadence "
                    "exists anywhere in this project for it to use -- see "
                    "ParticipantHoldingsNotWired's own docstring. Configure a real value before "
                    "this adapter can be built; nothing was read."
                )
            holding_stride = settings.holding_stride_blocks

        # entry_deposit_rao (#570): the identical "not a chain fact, not
        # hardcoded here" reasoning holding_stride above already gives --
        # P-020's flat entry-deposit figure is ruled in a registry this code
        # does not ship with, so it is read from a real, operator-configured
        # setting, refusing by name when absent, rather than a number invented
        # here. Checked here, before dials, mirroring holding_stride's own
        # position immediately above -- see EntryDepositAmountNotWired's own
        # docstring.
        #
        # #112, real Verifier finding: this setting (NOAH_ENTRY_DEPOSIT_RAO)
        # and noah.deposits.entry_gate's own figure (noah.config.profile.
        # entry_deposit_rao(), read from NOAH_PARAMETERS_JSON) are two
        # independent, operator-set values for the SAME real figure [P-020],
        # with no code-level guarantee they agree. Cross-checked here, the
        # moment the legacy setting is actually about to be used -- not when
        # a caller supplies entry_deposit_rao directly, the test-only
        # capability-injection point this function's own docstring already
        # names -- so a caller-supplied test value never needs a matching
        # NOAH_PARAMETERS_JSON file. See EntryDepositAmountMismatched's own
        # docstring for the exact hazard this closes.
        if entry_deposit_rao is None:
            if not settings.entry_deposit_rao:
                raise EntryDepositAmountNotWired(
                    "NOAH_ENTRY_DEPOSIT_RAO is not set. _entry_deposits_for needs the ruled, "
                    "flat entry-deposit figure [P-020] to recognise a coldkey's real entry "
                    "deposit among its ordinary NOAH deposits, and no ruled value for it exists "
                    "anywhere in this project for it to invent -- see "
                    "EntryDepositAmountNotWired's own docstring. Configure a real value before "
                    "this adapter can be built; nothing was read."
                )
            profile_entry_deposit_rao = _profile_entry_deposit_rao()
            if settings.entry_deposit_rao != profile_entry_deposit_rao:
                raise EntryDepositAmountMismatched(
                    f"NOAH_ENTRY_DEPOSIT_RAO is {settings.entry_deposit_rao}, but "
                    f"noah.config.profile.entry_deposit_rao() (NOAH_PARAMETERS_JSON's own "
                    f"entryDepositPerHotkeyRao field, the PROFILE's threshold -- not the "
                    f"identically-spelled but unrelated per-miner entryDepositRao field in a "
                    f"scored fixture/output) is {profile_entry_deposit_rao} -- the SAME figure "
                    f"noah.deposits.entry_gate's scoring gate already uses to decide who clears "
                    f"entry. These must agree: see EntryDepositAmountMismatched's own docstring "
                    f"for the concrete miscredit this disagreement would cause. Nothing was "
                    f"read; set NOAH_ENTRY_DEPOSIT_RAO to match NOAH_PARAMETERS_JSON, or correct "
                    f"whichever one is wrong."
                )
            entry_deposit_rao = settings.entry_deposit_rao

        # dials: NOT a chain read and NOT hardcoded here -- see DialsNotWired's
        # own docstring for why a copy of the ruled parameter-registry numbers
        # living in this file would repeat the exact DEFAULT_DIALS mistake
        # neurons/miner.py's own _real_dials already found and closed. Ported
        # from that function (kept in exact sync with it) rather than imported
        # across the neurons/noah audit boundary.
        #
        # #112: NOAH_DIALS_JSON (read above by _real_dials) and
        # NOAH_PARAMETERS_JSON's settled profile are two independent,
        # operator-set files with no code-level guarantee any of their seven
        # overlapping dial fields agree -- the identical "two independent
        # config values, no cross-check" hazard EntryDepositAmountMismatched
        # already closes for entry_deposit_rao, extended here to every other
        # dial. Cross-checked the moment the legacy file is actually read --
        # not when a caller supplies dials= directly, the test-only
        # capability-injection point this function's own docstring names --
        # so a caller-supplied test fixture never needs a matching
        # NOAH_PARAMETERS_JSON file. See DialsAmountMismatched's own
        # docstring for the exact hazard this closes.
        if dials is None:
            dials = _real_dials(settings)
            profile = _profile_parameters()
            mismatches = _dials_profile_disagreements(dials, profile)
            if mismatches:
                detail = "; ".join(
                    f"{json_name} (NOAH_DIALS_JSON={dials_value!r}, "
                    f"NOAH_PARAMETERS_JSON={profile_value!r})"
                    for json_name, dials_value, profile_value in mismatches
                )
                raise DialsAmountMismatched(
                    f"NOAH_DIALS_JSON disagrees with NOAH_PARAMETERS_JSON's settled profile "
                    f"(noah.scoring.parameters.load_parameters()) on {len(mismatches)} of the "
                    f"seven scoring dial field(s): {detail}. These must agree: see "
                    f"DialsAmountMismatched's own docstring for the concrete scoring-policy "
                    f"disagreement this would cause. Nothing was read; correct whichever file "
                    f"is wrong."
                )

        # publish_mode: NOT refused when absent from settings, unlike every
        # capability immediately above -- see this function's own docstring,
        # "publish_mode", for why getattr with a "sealed" fallback is the
        # correct read here rather than settings.publish_mode directly.
        if publish_mode is None:
            publish_mode = getattr(settings, "publish_mode", PUBLISH_MODE_DEFAULT)

        # canonical_score_for: already_published's own issue #212 phase-two
        # fix -- a real reader of day D's own locally-persisted canonical
        # score, at the SAME RUNTIME_ROOT/ticket-16/scores/day-<D>.json
        # convention scripts/persist_score_evidence.py already writes (see
        # _default_canonical_score_path's own docstring for why this is
        # re-derived rather than imported across the noah/scripts
        # boundary). No refusal when absent, unlike every OTHER capability
        # above: the overwhelming common case (an on-time publish) never
        # needs this at all -- already_published's own phase one answers it
        # without ever reaching phase two -- so requiring it here would
        # refuse to build an adapter over a capability most calls never
        # touch. CanonicalScoreNotWired is where an absence becomes a real,
        # named refusal, the moment phase two is actually reached.
        if canonical_score_for is None:
            canonical_score_for = _default_canonical_score_reader()

        # Proxy route: the validator identity is the configured public address and the
        # loaded wallet only signs (noah.core.proxy_route). Off, both are the wallet's hotkey.
        commit_identity = identity_from_settings(settings)
        if commit_identity is not None and publish_backoff_path is None:
            from .proxy_route import ProxyRouteRefused

            raise ProxyRouteRefused(
                "the proxy route is on but no publish_backoff_path was given, so the failed-send "
                "limit would be silently off. Nothing is built."
            )
        publisher = Publisher(
            client,
            wallet,
            netuid=settings.netuid,
            submit_client=submit_client,
            commit_identity=commit_identity,
        )

        result = ChainAdapter(
            commit_ledger_path=commit_ledger_path,
            publish_intent_path=publish_intent_path,
            publish_hold_path=publish_hold_path,
            # Only the proxy route counts failed sends (each can cost the signer a fee); every other
            # signing route retains its existing behavior.
            publish_backoff_path=publish_backoff_path if commit_identity is not None else None,
            submitted_record_dir=submitted_record_dir,
            reader=reader,
            publisher=publisher,
            runner_chain=runner_chain,
            netuid=settings.netuid,
            uid=uid,
            wallet_hotkey=commit_identity if commit_identity is not None else wallet.hotkey,
            settle_blocks=settings.settle_blocks,
            ark_coldkey=ark_coldkey,
            designated_validator_hotkey=designated_validator_hotkey,
            dials=dials,
            day_anchor_ts=day_anchor_ts,
            seats_source=seats_source,
            holding_stride=holding_stride,
            read_collateral=read_collateral,
            day_window=day_window,
            store_factory=store_factory,
            entry_deposit_rao=entry_deposit_rao,
            may_resume=may_resume,
            publish_mode=publish_mode,
            canonical_score_for=canonical_score_for,
        )
        succeeded = True
        return result
    finally:
        if not succeeded:
            # Ticket #16 (B06): nothing else references primary/second at
            # this point if a step above raised -- close them directly so a
            # failed build never leaks two already-open connections. Both
            # are attempted even if one itself raises while closing.
            for connection in (primary, second):
                try:
                    connection.close()
                except Exception:
                    pass
