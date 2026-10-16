"""Shared synthetic inputs for public participant tests."""

from noah.chain.holdings import Share
from noah.chain.store import NOT_READ, Store

ANCHOR = 1767571200
DAY = 86400
MINER = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
DEFAULT_HOLD = 400000000000


def a_store(tmp_path):
    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    store.record_emission_start(block=8000000, block_timestamp=ANCHOR, block_hash="0xstart")
    return store


def paid(store, coldkey, day, amount, uid=140, locked=0, unlocked=0, hotkey=HOTKEY):
    """Record one epoch payment and its collateral lock reading for a named wallet and hotkey."""
    block = 8800000 + day * DAY // 12 + uid
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=hotkey,
        uid=uid,
        block_timestamp=ANCHOR + day * DAY + 100,
        amount_rao=amount,
        block=block,
        block_hash=f"0x{day:x}{uid:x}",
    )
    store.record_locked_pay(
        coldkey=coldkey,
        block=block,
        block_timestamp=ANCHOR + day * DAY + 100,
        locked_arrival_rao=locked,
        unlocked_rao=unlocked,
        block_hash=f"0x{day:x}{uid:x}",
    )


def deposited(store, coldkey, day, amount, ordinal=1, hotkey=None, origin=None):
    """Record a deposit: origin names the miner; hotkey names the stake destination."""
    store.record_noah_deposit(
        coldkey=coldkey,
        block_timestamp=ANCHOR + day * DAY + 200,
        amount_rao=amount,
        block=8800000 + day * 100,
        event_ordinal=ordinal,
        extrinsic_index=3,
        block_hash=f"0xd{day:x}",
        carried_by="StakeTransferred",
        hotkey=hotkey,
        origin_hotkey=origin,
    )


def held(store, coldkey, day, amount=DEFAULT_HOLD, hotkey=HOTKEY):
    """Record one hotkey holding with unit shares at a block separate from payment events."""
    held_many(store, coldkey, day, {hotkey: amount})


def held_many(store, coldkey, day, amounts):
    """Record all hotkey holdings in one sample for the same wallet and block."""
    store.record_holding_sample(
        coldkey=coldkey,
        block_timestamp=ANCHOR + day * DAY + 300,
        block=8700000 + day,
        block_hash=f"0xh{day:x}",
        total_rao=sum(amounts.values()),
        locked_rao=NOT_READ,
        available_rao=NOT_READ,
        positions=[
            {
                "hotkey": hotkey,
                "shares": Share(encoding="u64f64", raw=1, exponent=0, source="v1"),
                "denominator": Share(encoding="u64f64", raw=1, exponent=0, source="v1"),
                "total_hotkey_alpha_rao": amount,
            }
            for hotkey, amount in amounts.items()
        ],
    )


def holdings_sampled(store, *days):
    for day in days:
        store.mark_day_sampled(day, stride=1000000, from_block=8700000, to_block=8799999)


__all__ = [
    "ANCHOR",
    "DAY",
    "HOTKEY",
    "MINER",
    "a_store",
    "deposited",
    "held",
    "held_many",
    "holdings_sampled",
    "paid",
]
