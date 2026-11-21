"""Credit actual received assets at the published daily reference, without haircuts."""

from .pricing import Unpriced, daily_average_credit


def credit(*, receipt_id, amount_rao, netuid, timestamp, intervals, native_netuid=82):
    if type(amount_rao) is not int or amount_rao <= 0 or type(netuid) is not int or netuid < 0:
        return dict(
            receipt_id=receipt_id, status="rejected", reason="invalid-receipt", credit_rao=0
        )
    try:
        value = daily_average_credit(
            amount_rao, netuid, timestamp, intervals, native_netuid=native_netuid
        )
    except (Unpriced, ValueError, TypeError, AttributeError):
        return dict(
            receipt_id=receipt_id,
            status="pending",
            reason="reference-data-unavailable",
            credit_rao=0,
        )
    return dict(
        receipt_id=receipt_id, status="accepted", reason="daily-reference", credit_rao=value
    )
