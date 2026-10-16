"""Determine whether stored evidence excuses a day without spendable earnings.

Use historical emission-start evidence and explicit stored declarations. A zero-pay
day alone does not establish no fault. Historical reads must refer to the subnet
instance being scored; reusing a subnet number does not make the new instance's
start authoritative for old history. This module consumes declarations and does
not decide who may issue them."""

from .sources import Refusal


class EmissionStateNotObserved(Refusal):
    """Nobody recorded whether the subnet was emitting in a day being judged."""


def no_fault_for(store, coldkey, day):
    """Whether this participant is excused for this day.

    In an emitting day, being paid settles it. The fixture rejects a no-fault
    day alongside nonzero pay outright, so the combination must not be
    producible here and left for a caller to notice.
    """
    emitting = store.subnet_was_emitting(day)
    if emitting is None:
        raise EmissionStateNotObserved(
            f"nobody has recorded when the subnet began emitting, so day {day} cannot be "
            f"judged. Read the subnet's first emission block and record it: one reading "
            f"answers every day. Guessing it was emitting costs somebody their streak, and "
            f"guessing the other way hands everybody a free pass, so neither is done."
        )

    if not emitting:
        # Before the start call the subnet is filtered out before any emission
        # is allocated to it at all (coinbase/subnet_emissions.rs:22-31), so
        # there should be no pay to have missed, and consulting the pay figure
        # here would refuse over an unscanned day the chain has settled.
        #
        # But if a pay row exists anyway, the two records disagree, and pay
        # wins. The fixture rejects a no-fault day alongside nonzero pay
        # outright, so producing that pair here would make the whole fixture
        # unreadable rather than one day wrong. Asking whether a row exists is
        # a question about this store, not about the chain, so it needs no scan.
        return not store.pay_recorded(coldkey, day)

    if store.attributed_pay(coldkey, day) > 0:
        return False

    return store.no_fault_declaration(day) is not None
