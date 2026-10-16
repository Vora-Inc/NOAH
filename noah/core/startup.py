"""Validator startup checks with specific refusal reasons.

Check declared dependency constraints, subnet existence, registration, validator
permit, publication readiness, required historical reads and sealed-mode settings.
A successful probe shows only that the queried history is readable; whole-history
requirements are enforced separately when constructing scoring fixtures."""

import re
import sys
from importlib import metadata
from pathlib import Path

from .reader import ChainUnreadable
from .settings import SettingsError

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

CHAIN_LIBRARIES = ("async-substrate-interface", "bittensor", "bittensor-core")

_FALLBACK_SPECIFIERS = {
    "async-substrate-interface": ">=2.2.1",
    "bittensor": ">=11.1.0,<12.0.0",
    "bittensor-core": ">=0.1.3",
}


class SubnetDoesNotExist(SettingsError):
    """The subnet this validator was pointed at has never been created."""


class NoValidatorPermit(SettingsError):
    """This seat may commit and its weights will be thrown away at reveal."""


class NotRegistered(SettingsError):
    """The signing hotkey holds no seat on the subnet."""


class CannotPublishYet(SettingsError):
    """The chain will not accept weights from this seat yet. Waiting, not broken."""


class NotArchiveCapable(SettingsError):
    """The node cannot answer reads deep enough for scoring to replay history."""


class SealedPathUnavailable(SettingsError):
    """The subnet is not running the sealed publication path the design requires."""


class DirectPathMisconfigured(SettingsError):
    """This validator is set to publish directly, but the subnet still has sealed publication on."""


class UnsupportedLibraryVersion(SettingsError):
    """A chain library is missing, or fails the constraint pyproject.toml declares for it."""


def check_subnet_exists(chain, netuid):
    """Refuse a subnet number that names nothing, before anything else is read.

    This runs first because almost every other read would succeed. Per-subnet
    settings are stored with defaults, so a subnet that has never existed hands
    back a complete and ordinary configuration: the same rate limit, the same
    activity factor, and sealed publication reading as ENABLED. Every later
    check passes on those values.

    What does not pass is the seat check, since a subnet with no seats has none
    for us either. So a mistyped subnet number currently refuses for the wrong
    reason, and the refusal tells an operator to go and register on a subnet
    that is not there. Being refused is not the problem; being told the wrong
    thing to do about it is.
    """
    if not chain.subnet_exists(netuid):
        raise SubnetDoesNotExist(
            f"subnet {netuid} does not exist on this chain. This is checked before anything "
            f"else because almost nothing else would notice: most per-subnet settings answer "
            f"from a default, so a subnet that was never created reports a complete and "
            f"ordinary configuration, sealed publication included. Check the subnet number "
            f"before registering anything, because the seat check would otherwise tell you to "
            f"register on a subnet that is not there."
        )


def check_registered(chain, hotkey, netuid):
    """Return the seat this hotkey holds, or refuse."""
    seat = chain.seat_of(hotkey, netuid)
    # Seat zero is a real seat. Testing truthiness here would refuse a validator
    # holding the first seat, which is the one the canonical validator is most
    # likely to hold.
    if seat is None:
        raise NotRegistered(
            f"the hotkey {hotkey} holds no seat on subnet {netuid}, so nothing it "
            f"publishes would be counted. Register it on that subnet, or point this "
            f"validator at the subnet where it is already registered."
        )
    return int(seat)


def check_can_publish_yet(chain, uid, netuid):
    """Return blocks still to wait, refusing when that is more than zero.

    Zero means the validator may publish now. The refusal is deliberately worded
    as waiting rather than failing, because this fires on a correctly built
    validator every time it is freshly registered.
    """
    current = int(chain.current_block())
    last_update = int(chain.last_update(uid, netuid))
    limit = int(chain.rate_limit(netuid))
    waited = current - last_update
    remaining = limit - waited
    if remaining > 0:
        raise CannotPublishYet(
            f"this seat may not publish for another {remaining} blocks. The chain's "
            f"limit is {limit} blocks between publications, and the clock starts when a "
            f"seat is REGISTERED rather than when it first publishes, so a newly "
            f"registered validator waits out the full period before its first one. "
            f"Wait, then start. This is not a fault and nothing needs changing."
        )
    return 0


def check_has_validator_permit(chain, uid, netuid):
    """Refuse a seat whose weights the chain will not accept.

    ## Why this is not obvious and why it is dangerous

    The permit is NOT checked when a commit is made. `internal_commit_timelocked_weights`
    checks four things and the permit is not among them: that sealed publication
    is on, that the version is right, that the hotkey is registered, and the rate
    limit. The permit is checked later, in `internal_set_weights`
    (`subnets/weights.rs:776`), which runs at REVEAL.

    So a validator with no permit does all of this successfully:

        commits, and the extrinsic succeeds
        reads the commit back off the chain and finds it
        has its activity stamped, because activity is stamped at commit

    and then the reveal fails an epoch later with NeuronNoValidatorPermit and no
    weights are ever set. It stays counted as active, its publication read-back
    passes, and it sets nothing, every period, forever.

    That is the failure this whole component is built to make impossible: a
    validator that looks healthy on every axis and is doing nothing.

    Runtime 475 keeps the owner exemption for Yuma. Null instead requires a
    validator permit even for the owner, checked by ensure_null_weight_permit
    and check_validator_permit in subnets/weights.rs. Read the selected
    consensus before applying the exemption. An unreadable mode must refuse.
    """
    if chain.has_validator_permit(netuid, uid):
        return

    if chain.epoch_consensus(netuid) != "Null" and chain.owner_uid(netuid) == uid:
        return  # the owner's seat is exempt in the runtime itself

    raise NoValidatorPermit(
        f"seat {uid} holds no validator permit on subnet {netuid}, so the chain will not "
        f"accept its weights. Null requires a permit even for the owner. "
        f"This refuses BEFORE publishing rather than after, because the "
        f"failure would otherwise arrive an epoch late and look like something else: the "
        f"permit is not checked when a commit is made, only when it is opened. A validator "
        f"without one commits successfully, finds its own commit on a read-back, and has its "
        f"activity stamped, and then the reveal is thrown away and no weights are ever set. "
        f"It reads as healthy on every check there is while doing nothing at all. Acquire a "
        f"permit, which on this network means enough stake to be in the validator set, or "
        f"run on a subnet where this seat has one."
    )


def check_reads_history(chain, depth):
    """Probe a historical read at the required depth.

    Failure refuses startup. Success does not establish complete local history or
    availability of every older block needed after a long outage."""
    try:
        answer = chain.historical_probe(depth)
    except ChainUnreadable as error:
        raise NotArchiveCapable(
            f"this node could not answer a read {depth} blocks back "
            f"({type(error).__name__}; the error text is not shown). "
            f"Scoring reads the newest completed days "
            f"from the chain, so the node must keep at least {depth} blocks of state. "
            f"Point the validator at a node that does."
        ) from None

    if not answer:
        raise NotArchiveCapable(
            f"this node answered a read {depth} blocks back with {answer!r}, which is not "
            f"an answer. A node keeping only recent state returns an empty or zero value "
            f"at depth rather than an error, so the score would come back plausible and "
            f"wrong instead of refused. Point the validator at a node that keeps at least "
            f"{depth} blocks of state."
        )
    return None


def check_commit_reveal_enabled(chain, netuid, publish_mode="sealed"):
    """Refuse a subnet whose sealed-publication setting does not match how this validator publishes.

    `publish_mode` follows the same "chosen once, never guessed at" rule as
    `noah.core.period`'s own `publish_mode` branch: it defaults to "sealed", today's
    only behavior, so every existing caller (this function had no such parameter
    before) keeps refusing exactly as it always did, unchanged.

    In "sealed" mode this requires `CommitRevealWeightsEnabled == True` -- unchanged
    from before direct publishing existed, and this path must never change behavior
    for a validator still on sealed mode, since that is every other deployment.

    In "direct" mode this requires the opposite, `CommitRevealWeightsEnabled ==
    False`. Direct publishing needs sealing off: the sealed path itself hard-fails
    at the transaction-validity layer while commit-reveal is on, so a validator
    configured for direct mode while the chain still has it on is a real
    misconfiguration -- the wrong on-chain state for the mode this validator was
    actually told to run -- not something to tolerate silently. That is refused
    under its own name, `DirectPathMisconfigured`, rather than reusing
    `SealedPathUnavailable`'s wording, because the two failures are not the same
    claim: "sealed" mode raises this when the sealed path is unavailable, "direct"
    mode raises it when the chain is not configured the way THIS mode needs.
    """
    enabled = chain.commit_reveal(netuid)
    if publish_mode == "direct":
        if enabled is not False:
            raise DirectPathMisconfigured(
                f"subnet {netuid} reports its sealed publication setting as {enabled!r}, and "
                f"this validator is configured with NOAH_PUBLISH_MODE=direct, which needs "
                f"sealed publication OFF. The sealed path hard-fails at the "
                f"transaction-validity layer while commit-reveal is on, so direct publishing "
                f"cannot work against this subnet as it stands. Either turn "
                f"CommitRevealWeightsEnabled off for this subnet, or switch this validator "
                f"back to NOAH_PUBLISH_MODE=sealed."
            )
        return None
    if enabled is not True:
        raise SealedPathUnavailable(
            f"subnet {netuid} reports its sealed publication setting as {enabled!r}, and "
            f"this validator publishes only by the sealed path. Anything else would put "
            f"the day's numbers in the open before they take effect, which is the thing "
            f"sealing exists to prevent."
        )
    return None


def _tomllib():
    """Return the available TOML parser for this interpreter, or None."""
    try:
        import tomllib

        return tomllib
    except ImportError:
        return None


# A sentinel `_declared_specifier` returns instead of `None` when this
# interpreter has no TOML parser to read pyproject.toml with -- distinct
# from `None`, which still means "tomllib works fine, but this name is not
# declared in pyproject.toml at all," a real bug in this file or the
# manifest that `check_library_versions` raises loudly for. Conflating the
# two would make a below-floor interpreter (which triggers the sentinel for
# every name, every time) look like that bug instead of the ordinary,
# expected case this whole fallback path exists for.
_TOMLLIB_UNAVAILABLE = object()


# Every comparison operator a PEP 508 specifier can use, longest first so a
# stripping or matching pass never mistakes "<=" for "<" plus a stray "=".
# Shared by `_declared_specifier` (stripping a line to its bare name) and
# `_clauses` (splitting a specifier into evaluable pieces), rather than each
# keeping its own copy of the same seven strings to drift apart silently.
_SPECIFIER_OPERATORS = (">=", "<=", "==", "!=", "~=", ">", "<")

_EXTRAS_RE = re.compile(r"\[[^\]]*\]")


def _declared_specifier(name):
    """The version specifier pyproject.toml declares for one dependency.

    ``"bittensor"`` -> ``">=11.1.0,<12.0.0"``. ``None`` if that name is not
    declared at all. `_TOMLLIB_UNAVAILABLE` if this interpreter has no TOML
    parser to read pyproject.toml with in the first place -- see `_tomllib`.

    This is `scripts/declared_matches_installed.py`'s own `declared()` run in
    reverse. That function strips a dependency line down to its bare name
    ("cyscale>=0.8" -> "cyscale") to build a set of names; this keeps the part
    `declared()` throws away, using the identical strip -- same separators,
    same order, same case-and-underscore normalisation -- so a name matches
    here exactly when it would match there, and the piece this check actually
    needs (the full constraint) is read out of the manifest rather than typed
    a second time.
    """
    tomllib = _tomllib()
    if tomllib is None:
        return _TOMLLIB_UNAVAILABLE
    data = tomllib.loads(_PYPROJECT.read_text())
    project = data.get("project", {})
    lines = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        lines.extend(extra)
    target = name.strip().lower().replace("_", "-")
    for line in lines:
        clause = _EXTRAS_RE.sub("", line.split(";")[0])
        bare = clause
        for sep in _SPECIFIER_OPERATORS:
            bare = bare.split(sep)[0]
        if bare.strip().lower().replace("_", "-") != target:
            continue
        return clause.strip()[len(bare.strip()) :].strip()
    return None


_COMPARATORS = {
    ">=": lambda installed, required: installed >= required,
    "<=": lambda installed, required: installed <= required,
    "==": lambda installed, required: installed == required,
    ">": lambda installed, required: installed > required,
    "<": lambda installed, required: installed < required,
}


def _clauses(specifier):
    """Split a comma-separated specifier into ``(operator, version)`` pairs.

    ``">=11.1.0,<12.0.0"`` -> ``[(">=", "11.1.0"), ("<", "12.0.0")]``. An
    unrecognised comparison comes back as ``(None, clause)`` rather than being
    dropped, so a specifier shape this function cannot evaluate is visible to
    its caller instead of silently treated as satisfied.
    """
    parsed = []
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        for op in _SPECIFIER_OPERATORS:
            if clause.startswith(op):
                parsed.append((op, clause[len(op) :].strip()))
                break
        else:
            parsed.append((None, clause))
    return parsed


def _release_tuple(text):
    """Parse a plain dotted-integer release string; do not guess at prerelease ordering."""
    segments = text.strip().split(".")
    if not segments or not all(segment.isdigit() for segment in segments):
        return None
    return tuple(int(segment) for segment in segments)


def _same_length(left, right):
    """`left` and `right`, padded on the right with zeros to equal length."""
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)), right + (0,) * (length - len(right))


def check_library_versions(libraries=CHAIN_LIBRARIES):
    """Enforce the dependency constraints declared by the distributed pyproject.toml."""
    problems = []
    for name in libraries:
        specifier = _declared_specifier(name)

        if specifier is _TOMLLIB_UNAVAILABLE:
            specifier = _FALLBACK_SPECIFIERS.get(name)
            if specifier is None:
                # Same shape as the "not declared" bug below, one level
                # earlier: a library this check names has no bundled
                # fallback either, and there is no parser to fall back to
                # reading pyproject.toml with. Not something an operator on
                # this interpreter can fix by installing anything.
                raise UnsupportedLibraryVersion(
                    f"{name!r} is named in check_library_versions's own CHAIN_LIBRARIES and "
                    f"has no entry in _FALLBACK_SPECIFIERS, and this interpreter has no TOML "
                    f"parser (no tomllib -- Python 3.11+ only) to read pyproject.toml with "
                    f"directly either. Add a fallback specifier for {name!r}; this is not "
                    f"something an operator can act on."
                )
            origin = (
                f"a bundled fallback constraint ({name}{specifier}), used because this "
                f"interpreter has no TOML parser to read pyproject.toml with directly "
                f"(no tomllib -- Python 3.11+ only)"
            )
        elif specifier is None:
            # A library this check names and pyproject.toml no longer declares
            # is a bug in this file or in the manifest, not a fact about the
            # running environment -- fail loudly rather than silently skipping
            # the one thing this check exists to do.
            raise UnsupportedLibraryVersion(
                f"{name!r} is named in check_library_versions's own CHAIN_LIBRARIES and "
                f"pyproject.toml no longer declares it as a dependency at all. Fix "
                f"whichever of the two fell behind the other; this is not something an "
                f"operator can act on."
            )
        else:
            origin = f"pyproject.toml ({name}{specifier})"

        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name} is NOT INSTALLED. Required by {origin}.")
            continue

        installed_release = _release_tuple(installed)
        for op, required in _clauses(specifier):
            required_release = _release_tuple(required)
            if op not in _COMPARATORS or installed_release is None or required_release is None:
                problems.append(
                    f"{name} {installed} is installed. Required by {origin}. This check "
                    f"cannot confidently evaluate that version against that constraint (a "
                    f"comparison it does not parse, or a version -- installed or required "
                    f"-- that is not a plain dotted-integer release), so it refuses rather "
                    f"than guess."
                )
                break
            if not _COMPARATORS[op](*_same_length(installed_release, required_release)):
                problems.append(
                    f"{name} {installed} is installed, and does not satisfy the "
                    f"constraint declared by {origin}."
                )
                break

    if not problems:
        return None

    raise UnsupportedLibraryVersion(
        "this interpreter does not meet the chain library versions pyproject.toml "
        "declares, and this validator refuses to run against one that is unverified "
        "rather than proceed and decode the chain differently without saying so:\n  "
        + "\n  ".join(problems)
        + f"\n  interpreter: {sys.executable}\n"
        "  Reinstall with scripts/install.sh, and check that this really is the "
        "interpreter it built rather than a bare python3 or a different one on PATH."
    )
