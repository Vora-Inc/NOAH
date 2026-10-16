"""What to deposit: four amount modes as pure calculations.

Every function here takes plain numbers and returns a plain result or refuses
with a plain-English reason. Nothing reads a chain, a file or a clock. A missing
input is a refusal, never a zero: a blank earnings figure must not become "owe
nothing", and a blank balance must not become "send everything".

The 10 NOAH entry contribution is a separate, starting credit. It is never an
input to any calculation here; ``Evidence.credited_contributions_rao`` is the
ongoing, post-entry credited total only.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ...chain.sources import Refusal

RAO_PER_NOAH = 1_000_000_000

CUSTOM = "custom"
FIXED = "fixed"
PERCENT = "percent"
MAINTENANCE = "maintenance"
MODES = (CUSTOM, FIXED, PERCENT, MAINTENANCE)

REQUIRED_CONTRIBUTION_BP = 5000
SUGGESTED_PERCENT = 50

READY = "ready"
PARTIAL = "partial"
NOTHING_DUE = "nothing_due"
BELOW_MINIMUM = "below_minimum"


class DepositRefused(Refusal):
    """The assistant will not propose a deposit, and says why in plain words."""

    def __init__(self, message, code="refused"):
        super().__init__(message)
        self.code = code


def parse_noah(text, what):
    """NOAH text such as ``"12.5"`` to whole rao, exactly, or a refusal."""
    if text is None or (isinstance(text, str) and not text.strip()):
        raise DepositRefused(f"{what} is blank. Nothing is assumed for a blank amount.", "blank")
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise DepositRefused(f"{what} ({text!r}) is not a number.", "not_a_number") from None
    if not value.is_finite() or value < 0:
        raise DepositRefused(f"{what} ({text!r}) must be zero or more.", "negative")
    rao = value * RAO_PER_NOAH
    if rao != rao.to_integral_value():
        raise DepositRefused(f"{what} ({text!r}) has more than 9 decimal places.", "precision")
    return int(rao)


def format_noah(rao):
    """Whole rao as NOAH text with trailing zeros trimmed."""
    whole, frac = divmod(rao, RAO_PER_NOAH)
    text = f"{whole}"
    if frac:
        text += "." + f"{frac:09d}".rstrip("0")
    return f"{text} NOAH"


def _whole(value, what, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise DepositRefused(
            f"{what} is not available, so no amount is calculated. A missing figure is never "
            f"treated as zero.",
            "missing_evidence",
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise DepositRefused(
            f"{what} came back as {value!r}, not a whole number of rao.", "bad_input"
        )
    if value < 0:
        raise DepositRefused(f"{what} is negative ({value}).", "bad_input")
    return value


@dataclass(frozen=True)
class Evidence:
    """Facts about earnings and credit, with their provenance.

    ``None`` means "not known". ``history_complete`` must be explicitly True for
    any earnings-based mode. All amounts are rao.
    """

    through_day: str = None
    history_complete: bool = False
    source: str = "unknown"
    # Earnings finalized in the period being measured (percent mode).
    period_start_day: str = None
    period_eligible_earnings_rao: int = None
    # Cumulative post-entry eligible earnings and qualifying credit (maintenance).
    cumulative_eligible_earnings_rao: int = None
    credited_contributions_rao: int = None
    # Informational only; never used in any calculation.
    entry_contribution_rao: int = None


@dataclass(frozen=True)
class Limits:
    available_rao: int = None  # what the chain says may leave the position now
    reserve_rao: int = 0  # kept back for fees / protected holdings
    cap_rao: int = None  # optional per-transaction cap
    minimum_rao: int = 0  # smallest deposit worth sending


@dataclass(frozen=True)
class Proposal:
    status: str
    mode: str
    amount_rao: int  # 0 unless READY or PARTIAL
    wanted_rao: int  # what the mode calculated before limits
    explanation: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    limited_by: str = None
    shortfall_rao: int = 0
    pending_rao: int = 0  # already sent and final, awaiting credit; counted against what is due


def _require_history(evidence, what):
    if not isinstance(evidence, Evidence):
        raise DepositRefused("No earnings evidence was supplied.", "missing_evidence")
    if evidence.history_complete is not True:
        raise DepositRefused(
            f"The earnings history behind {what} is not confirmed complete "
            f"(source: {evidence.source}). The assistant refuses rather than treat unknown "
            f"history as zero. Fix or refresh the evidence and preview again.",
            "incomplete_history",
        )
    if not evidence.through_day:
        raise DepositRefused(
            "The evidence does not say which completed day it runs through.", "missing_evidence"
        )


def wanted_amount(mode, *, amount_rao=None, percent_bp=None, evidence=None, pending_rao=0):
    """The amount the mode asks for before any limit, plus explanation lines.

    Returns ``(wanted_rao, explanation, warnings)``.
    """
    if mode not in MODES:
        raise DepositRefused(f"Unknown deposit mode {mode!r}; choose one of {', '.join(MODES)}.")
    warnings = []
    if mode in (CUSTOM, FIXED):
        amount = _whole(amount_rao, "The amount you chose")
        if amount == 0:
            raise DepositRefused(
                "The amount you chose is zero, so there is nothing to send.", "zero"
            )
        label = "one-time custom amount" if mode == CUSTOM else "fixed amount per run"
        return amount, [f"Your {label}: {format_noah(amount)}."], warnings

    if mode == PERCENT:
        bp = _whole(percent_bp, "The percentage you chose")
        if bp == 0 or bp > 10_000:
            raise DepositRefused(
                "The percentage must be above 0% and at most 100% of eligible earnings.",
                "bad_percent",
            )
        _require_history(evidence, "the percentage calculation")
        earned = _whole(evidence.period_eligible_earnings_rao, "Eligible earnings for the period")
        calculated = earned * bp // 10_000  # rounds down
        wanted = max(0, calculated - pending_rao)
        expl = [
            f"{bp / 100:g}% of {format_noah(earned)} eligible earnings finalized through "
            f"{evidence.through_day} (period starting {evidence.period_start_day or 'unknown'}) "
            f"= {format_noah(calculated)}.",
        ]
        if pending_rao:
            expl.append(
                f"Already sent for this period and awaiting credit: {format_noah(pending_rao)}. "
                f"Still to send: {format_noah(wanted)}."
            )
        if bp < REQUIRED_CONTRIBUTION_BP:
            warnings.append(
                f"{bp / 100:g}% is below the subnet's 50% requirement. Choosing less does not "
                f"lower the requirement and may reduce your score."
            )
        return wanted, expl, warnings

    # MAINTENANCE
    _require_history(evidence, "contribution maintenance")
    earned = _whole(evidence.cumulative_eligible_earnings_rao, "Cumulative eligible earnings")
    credited = _whole(
        evidence.credited_contributions_rao, "Already-credited qualifying contributions"
    )
    required = -(-earned * REQUIRED_CONTRIBUTION_BP // 10_000)  # round up
    wanted = max(0, required - credited - pending_rao)
    expl = [
        f"Required so far: 50% of {format_noah(earned)} earnings through {evidence.through_day} "
        f"= {format_noah(required)}.",
        f"Already credited as qualifying: {format_noah(credited)} "
        f"(the 10 NOAH entry contribution is counted separately and is not included).",
    ]
    if pending_rao:
        expl.append(
            f"Already sent and final, awaiting credit: {format_noah(pending_rao)} "
            f"(counted so it is not paid twice)."
        )
    expl.append(f"Still needed: {format_noah(wanted)}.")
    return wanted, expl, warnings


def propose(mode, *, limits, amount_rao=None, percent_bp=None, evidence=None, pending_rao=0):
    """The proposed deposit after limits, or a refusal.

    Explicit amounts (custom, fixed) that do not fit the limits are refused, not
    shrunk: quietly sending less than the miner typed would be a surprise.
    Calculated amounts (percent, maintenance) that do not fit are shrunk, marked
    PARTIAL, and the shortfall is stated.
    """
    if not isinstance(limits, Limits):
        raise DepositRefused("No spending limits were supplied.", "missing_limits")
    available = _whole(limits.available_rao, "The amount available to deposit")
    reserve = _whole(limits.reserve_rao, "The reserve")
    minimum = _whole(limits.minimum_rao, "The minimum useful deposit")
    cap = _whole(limits.cap_rao, "The per-transaction cap", allow_none=True)

    wanted, expl, warnings = wanted_amount(
        mode,
        amount_rao=amount_rao,
        percent_bp=percent_bp,
        evidence=evidence,
        pending_rao=pending_rao,
    )

    if wanted == 0:
        return Proposal(
            NOTHING_DUE, mode, 0, 0, expl + ["Nothing is due, so nothing will be sent."], warnings
        )

    spendable = available - reserve
    if spendable <= 0:
        raise DepositRefused(
            f"Nothing can be sent: {format_noah(available)} is available and "
            f"{format_noah(reserve)} is held back as your reserve.",
            "insufficient_funds",
        )

    amount, limited_by = wanted, None
    if cap is not None and amount > cap:
        amount, limited_by = cap, "your per-transaction cap of " + format_noah(cap)
    if amount > spendable:
        amount = spendable
        limited_by = (
            f"available funds ({format_noah(available)} minus the {format_noah(reserve)} reserve)"
        )

    if amount < wanted and mode in (CUSTOM, FIXED):
        raise DepositRefused(
            f"You asked to send {format_noah(wanted)} but {limited_by} allows only "
            f"{format_noah(amount)}. Nothing was shrunk silently; change the amount or the limit.",
            "above_limit",
        )

    if amount < minimum:
        return Proposal(
            BELOW_MINIMUM,
            mode,
            0,
            wanted,
            expl
            + [
                f"{format_noah(amount)} is below your minimum useful deposit of "
                f"{format_noah(minimum)}, so nothing will be sent."
            ],
            warnings,
            limited_by,
            wanted - amount,
        )

    if amount < wanted:
        warnings.append(
            f"Limited by {limited_by}: sending {format_noah(amount)} of the {format_noah(wanted)} "
            f"calculated. {format_noah(wanted - amount)} stays unmet and the goal is not reached."
        )
        return Proposal(PARTIAL, mode, amount, wanted, expl, warnings, limited_by, wanted - amount)
    return Proposal(READY, mode, amount, wanted, expl, warnings, pending_rao=pending_rao)
