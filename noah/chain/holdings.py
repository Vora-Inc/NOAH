"""Value wallet shares without mixing storage encodings.

U64F64 carries a raw integer scaled by 2**64; SafeFloat carries a mantissa and a
decimal exponent. Decode each independently before combining a wallet share with
its pool denominator and total alpha. Migration may leave the numerator and
denominator in different encodings. Preserve explicit storage presence: a default
zero alone does not distinguish an absent key from a present zero position."""

from dataclasses import dataclass

from .sources import Refusal

# normalize caps a mantissa here (share-pool/src/lib.rs:15).
SAFE_FLOAT_MAX_MANTISSA = 10**21
# U64F64 is one u128 of raw bits.
U64F64_BITS = 2**128
U64F64_SCALE = 2**64
# SafeFloat's exponent is an i64.
_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1


class ShareNotRead(Refusal):
    """A share-pool component that cannot be used as read."""


@dataclass(frozen=True)
class Share:
    """One share-pool quantity, kept in the number system it was stored in.

    ``raw`` is the raw fixed-point integer for an old value, or the mantissa
    for a new one. ``exponent`` is zero for an old value and the stored
    base-ten exponent for a new one. Neither is a magnitude on its own.
    """

    encoding: str
    raw: int
    exponent: int
    source: str

    def as_ratio(self):
        """The exact value, as a numerator and denominator of whole numbers.

        Exact rather than approximate because this sits under a payment. The
        two encodings become comparable here and nowhere earlier.
        """
        if self.encoding == "u64f64":
            return self.raw, U64F64_SCALE
        if self.encoding == "safefloat":
            if self.exponent >= 0:
                return self.raw * 10**self.exponent, 1
            return self.raw, 10 ** (-self.exponent)
        raise ShareNotRead(
            f"a share was built with encoding {self.encoding!r}, which names no number "
            f"system this chain stores. There are two, and which one a value came from "
            f"decides what it means."
        )


def from_storage(decoded, key_present):
    """Distinguish an absent raw storage key from a present decoded value.

    Presence is supplied separately because a default decoded zero cannot establish
    whether a key exists."""
    return decoded if key_present else None


def _old_value(bits, what):
    """A raw U64F64 value, or a refusal.

    Accepts the shape the chain's own client hands back, which is a mapping
    carrying one field of raw bits, as well as the bare integer.
    """
    if isinstance(bits, dict):
        if set(bits) != {"bits"}:
            raise ShareNotRead(
                f"{what} came back from the old map as {bits!r}. That map's decoded shape "
                f"carries exactly one field of raw bits, and anything else is a different "
                f"kind of value being read as this one."
            )
        bits = bits["bits"]
    if isinstance(bits, bool) or not isinstance(bits, int):
        raise ShareNotRead(
            f"{what} came back from the old map as {bits!r}. That map holds one raw "
            f"fixed-point integer. A pair of numbers is the new map's shape, and reading "
            f"one as the other is wrong by an arbitrary power of ten."
        )
    if bits < 0:
        raise ShareNotRead(f"{what} from the old map is negative ({bits}).")
    if bits >= U64F64_BITS:
        raise ShareNotRead(
            f"{what} from the old map is {bits}, which is wider than the type holds. It "
            f"did not come from this chain, so it is a misdecode wearing a plausible shape."
        )
    return bits


def _new_value(parts, what):
    """A SafeFloat mantissa and exponent, or a refusal."""
    if isinstance(parts, dict):
        if set(parts) != {"mantissa", "exponent"}:
            raise ShareNotRead(
                f"{what} came back from the new map as {parts!r}. That map's decoded shape "
                f"carries a mantissa and an exponent and nothing else. A mantissa without "
                f"its exponent carries no magnitude at all."
            )
        parts = (parts["mantissa"], parts["exponent"])
    if not isinstance(parts, (tuple, list)) or len(parts) != 2:
        raise ShareNotRead(
            f"{what} came back from the new map as {parts!r}. That map holds a mantissa "
            f"and an exponent together. A mantissa on its own carries no magnitude at all, "
            f"because every nonzero one is normalised into the same narrow band, so a "
            f"value without its exponent is not an approximate size, it is no size."
        )
    mantissa, exponent = parts
    for value, name in ((mantissa, "mantissa"), (exponent, "exponent")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ShareNotRead(f"{what}: the {name} came back as {value!r}, not a whole number.")
    if mantissa < 0:
        raise ShareNotRead(f"{what}: the mantissa is negative ({mantissa}).")
    if mantissa > SAFE_FLOAT_MAX_MANTISSA:
        raise ShareNotRead(
            f"{what}: the mantissa is {mantissa}, above the cap this chain normalises to. "
            f"It did not come from here."
        )
    if not (_I64_MIN <= exponent <= _I64_MAX):
        raise ShareNotRead(f"{what}: the exponent {exponent} is outside what the type holds.")
    return mantissa, exponent


def _resolve(v1_bits, v2_parts, what):
    """The old map if its key exists, else the new one, else nothing."""
    if v1_bits is not None:
        return Share(
            encoding="u64f64",
            raw=_old_value(v1_bits, what),
            exponent=0,
            source="v1",
        )
    if v2_parts is not None:
        mantissa, exponent = _new_value(v2_parts, what)
        return Share(encoding="safefloat", raw=mantissa, exponent=exponent, source="v2")
    return None


def resolve_share(*, v1_bits, v2_parts):
    """Resolve a wallet share with its encoding preserved, refusing conflicting values."""
    share = _resolve(v1_bits, v2_parts, "the wallet's shares")
    return (
        share
        if share is not None
        else Share(encoding="safefloat", raw=0, exponent=0, source="absent")
    )


def resolve_denominator(*, v1_bits, v2_parts, share_present=True):
    """Resolve total pool shares with explicit encoding and presence requirements."""
    denominator = _resolve(v1_bits, v2_parts, "the hotkey's total shares")
    if denominator is None:
        if share_present:
            raise ShareNotRead(
                "the hotkey's total shares are absent from both maps while the wallet holds "
                "a share of it. Shares outstanding against a pool with no total cannot be "
                "honest, so this is a read that did not happen rather than a hotkey holding "
                "nothing."
            )
        return None
    if denominator.raw == 0:
        raise ShareNotRead(
            f"the hotkey's total shares read as zero in the {denominator.source} map. A "
            f"wallet's holding is a fraction of that total, so a total of zero has no "
            f"answer, and a zero beside any outstanding share cannot be honest."
        )
    return denominator


@dataclass(frozen=True)
class Position:
    """One hotkey's contribution to a wallet's holding, with its components.

    The components are kept beside the answer rather than discarded, because a
    published figure has to be traceable to the reads behind it, and because a
    total with nothing under it is a wallet credited on nobody's word.
    """

    hotkey: str
    alpha_rao: int
    share_numerator: int
    share_denominator: int
    pool_alpha_rao: int


@dataclass(frozen=True)
class Holding:
    """What a wallet holds on one subnet, and every position behind it."""

    total_rao: int
    positions: tuple


def positions_for_coldkey(positions):
    """Value each hotkey position separately using its own pool and share denominator."""
    rows = []
    for entry in positions:
        share = entry["share"]
        denominator = entry["denominator"]
        pool_alpha = _whole_alpha(entry["pool_alpha_rao"], entry["hotkey"])

        share_num, share_den = share.as_ratio()
        denom_num, denom_den = denominator.as_ratio()
        if denom_num == 0:
            raise ShareNotRead(
                f"the hotkey {entry['hotkey']} has a total of zero shares. A wallet's "
                f"holding is a fraction of that total, so a zero has no answer to give."
            )

        if share_num == 0:
            alpha = 0
        else:
            if pool_alpha == 0:
                raise ShareNotRead(
                    f"the pool on hotkey {entry['hotkey']} holds no alpha while the wallet "
                    f"holds a share of it. A share is a fraction of the pool, so a pool of "
                    f"nothing beside an outstanding share cannot be honest, and reading it "
                    f"as nothing is the silent zero in the direction that costs a "
                    f"participant their gate."
                )
            # pool * (share_num/share_den) / (denom_num/denom_den), floored.
            alpha = (pool_alpha * share_num * denom_den) // (share_den * denom_num)

        rows.append(
            Position(
                hotkey=entry["hotkey"],
                alpha_rao=alpha,
                share_numerator=share_num,
                share_denominator=share_den,
                pool_alpha_rao=pool_alpha,
            )
        )

    # Sorted so two runs over the same wallet produce the same rows whatever
    # order the hotkeys arrived in. The store keys on the wallet and the block.
    rows.sort(key=lambda row: row.hotkey)
    return tuple(rows)


def holding_for_coldkey(positions):
    """Sum the coldkey's validated positions on one subnet.

    An absent share can represent zero; malformed or unknown storage cannot."""
    rows = positions_for_coldkey(positions)
    total = sum(row.alpha_rao for row in rows)
    return Holding(total_rao=total, positions=rows)


def _whole_alpha(value, hotkey):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShareNotRead(
            f"the pool alpha on hotkey {hotkey} came back as {value!r}, which is not a "
            f"decoded amount of alpha."
        )
    return value
