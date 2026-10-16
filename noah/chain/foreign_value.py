"""The valuation rule for a deposit made in another subnet's token (issue #16).

Policy (``docs/CONTRIBUTIONS.md``): a foreign deposit is worth its spot price at the
block it entered the Ark. Not an average, not the lower of two prices. Foreign alpha is
valued in TAO at the foreign subnet's price at that block, then in NOAH at the scored
subnet's price at the SAME block, in whole rao, rounded down, by integer math only.

When a price cannot be read the answer is :class:`ForeignDepositUnpriced`, a named
refusal. Nothing is substituted: not zero, not the head price, not a neighbouring
block's price, not the other source's price when the two disagree. The refusal leaves
the deposit where it is (it is already recorded in ``foreign_deposit``) and stops the
day being scored, so no weight is published from a guess; the next attempt reads it
again. A missing or invalid price is no answer, never a value.
"""

from .sources import Refusal
from .valuation import noah_equivalent_rao


class ForeignDepositUnpriced(Refusal):
    """A foreign deposit could not be valued at its own block, so its day cannot be scored.

    The deposit stays recorded. Nothing is credited, dropped or invented in its place."""


def foreign_deposit_rule(chain, noah_netuid, *, evidence=None):
    """Return ``rule(amount_rao, token_netuid, block, block_hash) -> NOAH rao``.

    ``chain`` answers ``alpha_price_at(block, netuid=...)`` (the agreeing, dual-sourced
    chain in production). Each (subnet, block) price is read once per rule and kept, so
    the two prices behind one deposit and every deposit in the same block are valued
    from one reading each.
    """
    prices = {}

    def price(netuid, block, block_hash):
        key = (netuid, block, block_hash)
        if key not in prices:
            try:
                value = chain.alpha_price_at(
                    block, netuid=netuid, expected_block_hash=block_hash
                )
            except Refusal as refusal:
                raise ForeignDepositUnpriced(
                    f"the alpha price of subnet {netuid} at block {block} could not be read "
                    f"({type(refusal).__name__}: {refusal}). The foreign deposit made at that "
                    f"block stays recorded, is not valued at zero or at any other price, and "
                    f"its day is not scored until the price at exactly that block can be read."
                ) from None
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ForeignDepositUnpriced(
                    f"the alpha price of subnet {netuid} at block {block} came back as "
                    f"{value!r}, not a positive whole number. No other price is used."
                )
            prices[key] = value
        return prices[key]

    def rule(amount_rao, token_netuid, block, block_hash):
        if (
            not isinstance(block_hash, str) or len(block_hash) != 66
            or not block_hash.startswith("0x")
            or any(c not in "0123456789abcdefABCDEF" for c in block_hash[2:])
        ):
            raise ForeignDepositUnpriced(
                f"foreign receipt at block {block} has no valid exact block hash. "
                "No price is read."
            )
        if token_netuid == noah_netuid:
            raise ForeignDepositUnpriced(
                f"a deposit of subnet {token_netuid} at block {block} was filed as foreign, "
                f"but that is the scored subnet's own token. The ledger is inconsistent; "
                f"nothing is valued."
            )
        foreign_price = price(token_netuid, block, block_hash)
        noah_price = price(noah_netuid, block, block_hash)
        equivalent = noah_equivalent_rao(amount_rao, foreign_price, noah_price)
        if evidence is not None:
            evidence.append({
                "block": block, "block_hash": block_hash,
                "token_netuid": token_netuid, "noah_netuid": noah_netuid,
                "amount_rao": str(amount_rao),
                "foreign_price_rao": str(foreign_price),
                "noah_price_rao": str(noah_price),
                "noah_equivalent_rao": str(equivalent),
                "policy": "deposit_block_spot_floor_each_step",
            })
        return equivalent

    return rule
