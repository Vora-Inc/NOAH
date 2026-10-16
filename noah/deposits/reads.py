"""Read the chain facts required to enforce a deposit ceiling.

Keep provenance and block identity with the observations. Missing or disagreeing
facts must not be treated as a safe available balance."""

from dataclasses import dataclass

from ..chain.reads import KeyRead, NotDecoded, _decoded_bool, confirmed_absence
from ..chain.sources import Refusal


class PositionNotFound(Refusal):
    """No stake position exists for this hotkey, coldkey and subnet."""


class PositionAnswerUnreadable(Refusal):
    """The stake-position answer is neither a live position nor the chain's own
    documented shape for an absent one, so nothing is said about which."""


class AvailabilityNotRead(Refusal):
    """The coldkey-wide availability could not be read, so nothing is known about it."""


class QuestionNotWellFormed(Refusal):
    """Nothing established that the collateral query was built with the right keys."""


@dataclass(frozen=True)
class CollateralRead:
    """A collateral value together with evidence that the query used the correct key shape."""

    present: bool
    question_well_formed: bool


@dataclass(frozen=True)
class CeilingInputs:
    """The three numbers, each one read and checked rather than defaulted."""

    position_stake: int
    position_collateral: int
    coldkey_available: int


def decoded_amount(value, what):
    """A decoded, non-negative integer, or a refusal naming what arrived.

    ``bool`` is rejected explicitly: it passes ``isinstance(v, int)`` in Python,
    and a ``True`` that reached an amount field came from something that was not
    decoding amounts.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotDecoded(
            f"{what} arrived as {value!r}, which is not a decoded integer. A payment path "
            f"does not accept a value still wearing its wire format."
        )
    if value < 0:
        raise NotDecoded(
            f"{what} arrived as {value!r}. Nothing on chain is negative, so a negative here "
            f"means a decode went wrong upstream and the number is not trusted."
        )
    return value


def _absent(read):
    """Whether the collateral row is genuinely absent, or a refusal.

    Accepts either witness. A ``KeyRead`` carries a control row and is handed
    straight to the shared ``confirmed_absence``, so the stronger idiom keeps its
    single implementation. A ``CollateralRead`` carries the wider witness for the
    case a control row cannot exist.
    """
    if isinstance(read, KeyRead):
        return confirmed_absence(read)
    if not isinstance(read, CollateralRead):
        raise NotDecoded(
            f"a collateral answer came in as {read!r}, which carries no witness that its "
            f"question was well formed. An absence means nothing without one."
        )
    _decoded_bool(read.question_well_formed, "whether the collateral query was well formed")
    _decoded_bool(read.present, "whether the collateral row was present")
    if not read.question_well_formed:
        raise QuestionNotWellFormed(
            "nothing established that the collateral query supplied the keys the map "
            "actually takes. A query built with too few keys is accepted and answers 'not "
            "there' without complaining, so an unverified question and a real absence are "
            "the same bytes. The metadata's declared key arity is the witness that works "
            "even when the map is empty, which is the launch case."
        )
    return not read.present


def _is_a_live_position(stake_info):
    """Whether this record describes a seat that exists, by its own flag.

    Named, and called ON ONE LINE, so the mutation sweep can replace that line and
    watch the guard fail. Written inline first, the condition spanned three lines
    after formatting, the sweep could not apply its mutation at all, and it
    reported the guard NOT PROVEN. Which is the sweep working: it said it had
    proved nothing rather than saying the guard held.
    """
    return isinstance(stake_info, dict) and stake_info.get("is_registered") is True


def _is_an_unseated_position(stake_info):
    """Opt-in only: a well-formed record for a hotkey that holds no seat on the subnet.

    ``is_registered`` is an honest ``False`` and ``stake`` is a decoded,
    non-negative integer (zero or not). Anything else -- ``None``, a non-dict, a
    missing or non-bool ``is_registered``, an undecoded or negative ``stake`` --
    is not this shape and keeps its normal refusal. Used only when the caller passes
    ``allow_unregistered_position=True``.
    """
    if not isinstance(stake_info, dict) or stake_info.get("is_registered") is not False:
        return False
    stake = stake_info.get("stake")
    return isinstance(stake, int) and not isinstance(stake, bool) and stake >= 0


def _is_a_well_formed_absence(stake_info):
    """Recognize only the supported explicit absent-position shape.

    Require a dict with is_registered exactly False and stake as the decoded integer
    zero. Missing, undecoded, negative or positive stake is not that evidence; an
    unregistered hotkey can still carry a real stake position."""
    if not isinstance(stake_info, dict) or stake_info.get("is_registered") is not False:
        return False
    stake = stake_info.get("stake")
    return isinstance(stake, int) and not isinstance(stake, bool) and stake == 0


def _field_names(stake_info):
    """The keys of a malformed record, sorted for a stable, readable message."""
    return sorted(repr(key) for key in stake_info)


def _unreadable_position_reason(stake_info):
    """Explain why a reply is neither a supported live position nor explicit absence.

    Distinguish missing records/fields, undecoded types, negative amounts and a
    nonzero position on an unregistered hotkey. These cases do not justify silently
    claiming the wallet has no position."""
    if stake_info is None:
        return (
            "it came back as None, and the runtime call behind it never returns None -- it "
            "returns a fully-populated, zero-filled record instead, measured live on "
            "2026-08-24 (the citation is in ceiling_inputs, below). A None here is a shape the "
            "real chain does not send."
        )
    if not isinstance(stake_info, dict):
        return (
            f"it came back as {stake_info!r}, of type {type(stake_info).__name__}, which is "
            f"not a record at all."
        )
    if "is_registered" not in stake_info:
        return (
            f"it is a record with no `is_registered` field at all -- the fields present are "
            f"[{', '.join(_field_names(stake_info))}]."
        )
    flag = stake_info["is_registered"]
    if not isinstance(flag, bool):
        return (
            f"its `is_registered` field came back as {flag!r}, which is not a decoded true or "
            f"false."
        )
    # Only False reaches past this point: True is a live position and never gets
    # this far, and False with `stake` present, decoded, AND non-negative is the
    # confident absence, also handled before this function is ever called. What
    # remains is False with `stake` missing, undecoded, or negative.
    if "stake" not in stake_info:
        return (
            f"it carries `is_registered: False` but no `stake` field, though the two are "
            f"always encoded together, decoded, on the chain's real record (see "
            f"_is_a_well_formed_absence's own docstring). The fields present are "
            f"[{', '.join(_field_names(stake_info))}]. A record that agrees with the "
            f"documented shape on one key is not proof it agrees on the rest."
        )
    stake = stake_info["stake"]
    if not isinstance(stake, int) or isinstance(stake, bool):
        return (
            f"it carries `is_registered: False` and a `stake` field, but `stake` came back as "
            f"{stake!r}, which is not a decoded integer -- still wearing its wire format the "
            f"same way `decoded_amount` (below) refuses every other amount on this payment "
            f"path. A record that has not finished being decoded is not proof of the chain's "
            f"real answer."
        )
    if stake < 0:
        return (
            f"it carries `is_registered: False` and a decoded `stake` field, but `stake` came "
            f"back as {stake!r}. Nothing on chain is negative -- the same rule "
            f"`decoded_amount` (below) applies to every other figure on this payment path -- "
            f"so a negative here means a decode went wrong upstream, not that a position was "
            f"found and confirmed absent."
        )
    # A decoded, non-negative int reaching here can only be nonzero: exactly
    # zero is the confident absence, handled before this function is ever
    # called.
    return (
        f"it carries `is_registered: False` but a nonzero `stake` of {stake!r}. The chain's "
        f"own documented absence pairs `is_registered: False` with `stake: 0` -- the two "
        f"fields are computed by two independent reads inside the runtime call "
        f"(`get_stake_for_hotkey_and_coldkey_on_subnet` for `stake`, "
        f"`is_hotkey_registered_on_network` for `is_registered`, nothing enforcing they "
        f"agree -- see _is_a_well_formed_absence's own docstring), so a hotkey holding no "
        f"seat here can still hold real staked alpha, left over from before it deregistered "
        f"or staked to it after. That is not the same fact as 'nobody has ever staked to "
        f"this hotkey', and this project has no measured answer yet for what a depositor "
        f"should be told in that case, so it refuses rather than guessing which one is true."
    )


def ceiling_inputs(*, stake_info, availability, collateral, allow_unregistered_position=False):
    """The ceiling's three numbers, or a refusal naming which read failed.

    ``stake_info`` is the decoded ``StakeInfo`` for this exact position. The chain
    reports "no such position" as a ZERO-FILLED RECORD carrying
    ``is_registered: False``, and that is the ONLY shape read as a confident
    absence -- see ``_is_a_well_formed_absence``. A ``stake_info`` that is
    neither that shape nor a live ``is_registered: True`` record -- a missing
    ``is_registered`` key, a value for it that is not a real ``bool``, a bare
    ``None``, or something that is not a record at all -- is a DIFFERENT fact
    from either of those, and is refused under its own name,
    ``PositionAnswerUnreadable``, naming what actually arrived rather than
    being told a seat was never found.

    ``availability`` is the decoded
    entry for this coldkey and subnet, or ``None``. ``collateral`` is a
    ``(KeyRead, raw)`` pair, the read carrying its own control so an absence
    cannot be reported without one.

    ``allow_unregistered_position`` (default off; every caller but the deposit
    assistant's ``stake_position = validator`` shape leaves it off) additionally
    accepts a well-formed ``is_registered: False`` record and reads its ``stake``
    as the position: the designated Ark Validator hotkey may legitimately hold
    stake before it is registered on the subnet. Malformed records still refuse.
    """
    try:
        if not _is_a_live_position(stake_info) and not (
            allow_unregistered_position and _is_an_unseated_position(stake_info)
        ):
            if _is_a_well_formed_absence(stake_info):
                raise PositionNotFound(
                    "the chain has no stake position for this hotkey, coldkey and subnet. That "
                    "is a different fact from a position holding nothing, and it is refused "
                    "rather than read as zero: zero would produce a ceiling of zero and tell "
                    "the depositor they may send nothing, when what is actually wrong is which "
                    "position is being read."
                )
            raise PositionAnswerUnreadable(
                f"the stake-position answer is not a record this project recognises, so "
                f"nothing is said about whether a position exists: "
                f"{_unreadable_position_reason(stake_info)} That is a different fact from 'no "
                f"stake position' and is refused under its own name, so debugging it means "
                f"checking what produced this answer rather than looking for a seat that was "
                f"never the actual problem."
            )
        position_stake = decoded_amount(stake_info.get("stake"), "position stake")
    except Exception as failure:
        raise PositionAnswerUnreadable(
            f"reading this stake-position answer's own shape raised {failure!r} instead of "
            f"answering. That is not the same fact as either a live position or a documented "
            f"absence, and a crash is not evidence for either one, so it is refused under the "
            f"same name any other unrecognisable answer gets."
        ) from failure
    try:
        if not isinstance(availability, dict) or "available" not in availability:
            raise AvailabilityNotRead(
                f"the coldkey-wide availability came back as {availability!r}, with no "
                f"available figure in it. Silence from the map is not an amount, and a shape "
                f"that changed under a runtime upgrade must not read as zero available."
            )
        coldkey_available = decoded_amount(availability.get("available"), "coldkey available")
    except Exception as failure:
        raise AvailabilityNotRead(
            f"reading this coldkey-wide availability answer's own shape raised {failure!r} "
            f"instead of answering. Silence from the map is not an amount, and neither is a "
            f"crash while trying to read one."
        ) from failure

    collateral_read, collateral_raw = collateral
    if _absent(collateral_read):
        owed = 0
    else:
        owed = decoded_amount(collateral_raw, "position collateral")

    return CeilingInputs(
        position_stake=position_stake,
        position_collateral=owed,
        coldkey_available=coldkey_available,
    )
