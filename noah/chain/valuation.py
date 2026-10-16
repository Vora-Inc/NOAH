"""Pure TAO valuation of an alpha amount at a spot price."""

PRICE_SCALE = 1_000_000_000  # price is rao-TAO per 1 alpha, scaled by 1e9


def tao_value_rao(alpha_amount_rao, price):
    """Return ``(raw_alpha_rao, tao_rao)``.

    ``tao_rao = alpha_amount_rao * price // 1_000_000_000``: integer math,
    rounded DOWN (floor). The raw amount is returned unchanged beside the
    value; it is never replaced. Non-int (or bool), negative amount, or
    non-positive price raises ValueError.
    """
    for name, v in (("alpha_amount_rao", alpha_amount_rao), ("price", price)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{name} must be an int, got {v!r}")
    if alpha_amount_rao < 0:
        raise ValueError(f"alpha_amount_rao must not be negative, got {alpha_amount_rao}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    return alpha_amount_rao, alpha_amount_rao * price // PRICE_SCALE


def noah_equivalent_rao(foreign_alpha_rao, foreign_price, noah_price):
    """Whole NOAH rao worth the same as ``foreign_alpha_rao`` of another subnet's token.

    Foreign alpha -> TAO at the foreign subnet's spot price, then TAO -> NOAH at the
    scored subnet's spot price, both read at the SAME block. Integer math, floor at
    each step (the first step is :func:`tao_value_rao`; the second inverts it):
    ``noah = tao_rao * 1_000_000_000 // noah_price``. Never rounds up, never a float.
    Same input checks as :func:`tao_value_rao`.
    """
    _, tao_rao = tao_value_rao(foreign_alpha_rao, foreign_price)
    if isinstance(noah_price, bool) or not isinstance(noah_price, int):
        raise ValueError(f"noah_price must be an int, got {noah_price!r}")
    if noah_price <= 0:
        raise ValueError(f"noah_price must be positive, got {noah_price}")
    return tao_rao * PRICE_SCALE // noah_price
