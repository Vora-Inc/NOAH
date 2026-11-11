"""Prospective pricing candidate, separate from preserved historical receipts.

Pure arithmetic on externally authenticated price intervals. It neither reads
chain state nor proves liquidity or price provenance. The reference is the previous completed UTC day. There is no market-movement
veto, sale simulation, liquidity threshold or deposit-size restriction.
"""

from dataclasses import dataclass

from .ledger import R, natural


class Unpriced(ValueError):
    pass


@dataclass(frozen=True)
class PriceInterval:
    start: int
    end: int
    noah_price_rao: int
    asset_price_rao: int


def daily_average_credit(
    amount_rao,
    asset_netuid,
    deposit_timestamp,
    intervals,
    *,
    native_netuid=82,
):
    """Ratio of previous completed UTC day's time-weighted average prices.

    Intervals must exactly cover that day with no gaps or overlaps. Prices are
    piecewise constant over each provided interval. Integer math, two floors.
    Actual received tokens are converted once at this fixed daily reference.
    """
    for name, value in [
        ("amount", amount_rao),
        ("asset", asset_netuid),
        ("timestamp", deposit_timestamp),
        ("native subnet", native_netuid),
    ]:
        natural(value, name)
    if asset_netuid == native_netuid:
        return amount_rao
    if deposit_timestamp < 86400:
        raise Unpriced("no prior full UTC day")
    end = deposit_timestamp // 86400 * 86400
    start = end - 86400
    cursor = start
    weighted_noah = weighted_asset = 0
    for r in intervals:
        for name, value in [
            ("start", r.start),
            ("end", r.end),
            ("NOAH price", r.noah_price_rao),
            ("asset price", r.asset_price_rao),
        ]:
            natural(value, name)
        if r.start != cursor or r.end <= r.start or r.end > end:
            raise Unpriced("reference day has a gap, overlap, unordered or future interval")
        if not r.noah_price_rao or not r.asset_price_rao:
            raise Unpriced("zero reference price")
        if asset_netuid == 0 and r.asset_price_rao != R:
            raise Unpriced("root TAO must value at one TAO")
        weighted_noah += r.noah_price_rao * (r.end - r.start)
        weighted_asset += r.asset_price_rao * (r.end - r.start)
        cursor = r.end
    if cursor != end:
        raise Unpriced("incomplete prior UTC day")
    noah_price = weighted_noah // 86400
    asset_price = weighted_asset // 86400
    if not noah_price or not asset_price:
        raise Unpriced("average rounded to zero")
    return (amount_rao * asset_price // R) * R // noah_price


@dataclass(frozen=True)
class RationalPrice:
    """Exact TAO per token; decoding/provenance belong to the reference reader.

    Keep the runtime fixed-point denominator instead of rounding prices into
    rao before conversion. This type does not establish market eligibility.
    """

    numerator: int
    denominator: int

    def positive(self):
        for name, value in (("price numerator", self.numerator),
                            ("price denominator", self.denominator)):
            natural(value, name)
            if value == 0:
                raise Unpriced("zero EMA/spot reference component")
        return self


def ema_spot_credit(
    amount_rao,
    asset_netuid,
    *,
    ema_asset=None,
    ema_noah=None,
    spot_asset=None,
    spot_noah=None,
    native_netuid=82,
):
    """Policy /4 arithmetic: q * min(E_asset/E_noah, P_asset/P_noah).

    All amounts use the same 1e9 token subdivision. Compare whole exact ratios
    by cross multiplication, then floor ONLY the final NOAH-unit result. Root
    TAO uses the literal 1/1 numerator reference and needs no alpha EMA. Native
    repayment is identity and bypasses all market inputs. Missing/zero/invalid
    references raise; this pure function never chooses a terminal disposition.
    The caller must authenticate both prices at the receipt block's parent and
    prove current incarnation before using the result.
    """
    for name, value in (("amount", amount_rao), ("asset", asset_netuid),
                        ("native subnet", native_netuid)):
        natural(value, name)
    if not 0 <= asset_netuid < 65536 or not 0 < native_netuid < 65536:
        raise ValueError("invalid subnet identity")
    if asset_netuid == native_netuid:
        return amount_rao
    if asset_netuid == 0:
        # A caller cannot accidentally substitute a root alpha price.
        if any(value is not None and value != RationalPrice(1, 1)
               for value in (ema_asset, spot_asset)):
            raise Unpriced("root reference must be exactly one TAO")
        ema_asset = spot_asset = RationalPrice(1, 1)
    prices = (ema_asset, ema_noah, spot_asset, spot_noah)
    if any(type(value) is not RationalPrice for value in prices):
        raise Unpriced("complete rational EMA/spot reference required")
    for value in prices:
        value.positive()
    ema_num = ema_asset.numerator * ema_noah.denominator
    ema_den = ema_asset.denominator * ema_noah.numerator
    spot_num = spot_asset.numerator * spot_noah.denominator
    spot_den = spot_asset.denominator * spot_noah.numerator
    if ema_num * spot_den <= spot_num * ema_den:
        return amount_rao * ema_num // ema_den
    return amount_rao * spot_num // spot_den
