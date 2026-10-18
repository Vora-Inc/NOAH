"""Foreign-token credit, end to end (issue #16).

A deposit in another subnet's token is filed in ``foreign_deposit`` at the ledger
write (never in ``noah_deposit``, which alone feeds entry and compliance), valued at the
spot price of its own block (foreign alpha -> TAO -> NOAH, both prices at that block,
whole rao, floored), fed into the fixture per seat, and credited by the existing engine,
which caps it by the miner's NOAH credit.

Path exercised, none of it hand-made: ``Deposit`` -> ``runner._record`` (the real ledger
write) -> ``Store`` -> the same composition ``ChainAdapter.read`` performs (entry gate,
``_entry_deposits_for``, ``_ExcludingEntryDeposit``) -> ``build_fixture`` ->
``parse_fixture`` -> ``score_fixture``. Prices come from a real ``LiveRunnerChain``
reading a fake node's ``SwapRuntimeApi.current_alpha_price`` at a block hash, so the
price reader, its block pinning and its refusals are the production ones.

Expected values are worked by hand in the tests, in two floored steps, not recomputed
with the code under test.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.deposits import Deposit
from noah.chain.fixture_build import build_fixture
from noah.chain.foreign_value import ForeignDepositUnpriced, foreign_deposit_rule
from noah.chain.live import LiveRunnerChain
from noah.chain.runner import _record
from noah.chain.sources import Refusal
from noah.chain.store import UNNAMED_REASON_PREFIX, DayNotScanned, Store
from noah.chain.tests import test_runner as TR
from noah.chain.holdings import Share
from noah.chain.store import NOT_READ
from noah.chain.valuation import noah_equivalent_rao
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture

ANCHOR = 1767571200
DAY = 86_400
_REPO_ROOT = next(parent for parent in Path(__file__).resolve().parents
                  if (parent / "docs/parameters/profiles/m1-revised-v1.json").is_file())
_PROFILE_PATH = _REPO_ROOT / "docs" / "parameters" / "profiles" / "m1-revised-v1.json"
_PROFILE = json.loads(_PROFILE_PATH.read_text())
DIALS = {
    k: _PROFILE[k]
    for k in (
        "targetFp",
        "harshness",
        "capFp",
        "halfLifeDays",
        "streakCeilingFp",
        "streakRampDays",
        "ongoingHoldRao",
    )
}
N = 10**9
PAY = 100 * N
NEED = int(_PROFILE["targetFp"]) * PAY // 1_000_000_000  # what one day of pay asks to be matched
ENTRY = int(_PROFILE["entryDepositPerHotkeyRao"])

NETUID = 35  # the scored subnet
FOREIGN = 9  # another subnet whose alpha is deposited
COLDKEY = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
HOT_A = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
HOT_B = "5FotherHotkeyotherHotkeyotherHotkeyotherHotkeyoQ"
VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
OTHER_VALIDATOR = "5CS7V5Qh6G1KJPc9Tn58dnUgqf2YddM5v16fmHCp9fgLHroD"

# Spot prices: rao-TAO per alpha, scaled by 1e9. Foreign alpha is worth 0.02 TAO,
# NOAH alpha 0.05 TAO, so one foreign alpha is 0.4 NOAH.
FOREIGN_PRICE = 20_000_000
NOAH_PRICE = 50_000_000


# Synthetic ledger setup is self-contained and needs no live chain.
def paid(store, coldkey, day, amount, uid=140, locked=0, unlocked=0, hotkey=HOT_A):
    block = 8_800_000 + day * DAY // 12 + uid
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


def held_many(store, coldkey, day, amounts):
    store.record_holding_sample(
        coldkey=coldkey,
        block_timestamp=ANCHOR + day * DAY + 300,
        block=8_700_000 + day,
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
        store.mark_day_sampled(day, stride=1_000_000, from_block=8_700_000, to_block=8_799_999)


@pytest.fixture(autouse=True)
def _profile_and_gate(monkeypatch):
    monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(_PROFILE_PATH))
    monkeypatch.setattr("noah.chain.fixture_build.require_whole_history", lambda *a, **k: None)


# ---- a node that answers prices, and only prices -----------------------------


def hash_of(block):
    return "0x" + f"{block:064x}"


class PriceNode:
    """A fake node. ``prices`` maps ``(netuid, block)`` to what ``current_alpha_price``
    answers at that block's hash. Anything absent fails the way a pruned node does.
    Every call is recorded, so a test can prove which block each price was read at."""

    def __init__(self, prices, override=None):
        self.prices = dict(prices)
        self.override = override or {}
        self.calls = []

    def get_block_hash(self, block):
        return hash_of(block)

    def runtime_call(self, api, method, params, block_hash=None):
        assert (api, method) == ("SwapRuntimeApi", "current_alpha_price")
        netuid = params[0][0]
        block = int(block_hash, 16)
        self.calls.append((netuid, block, block_hash))
        key = (netuid, block)
        if key in self.override:
            return self.override[key]
        if key not in self.prices:
            raise RuntimeError("State already discarded for block")
        return _Scale(self.prices[key])

    def close(self):
        pass


class _Scale:
    def __init__(self, value):
        self.value = value


def price_chain(node, name="a"):
    return LiveRunnerChain(node, netuid=NETUID, endpoint=f"wss://{name}.example:443")


def prices_at(block, foreign=FOREIGN_PRICE, noah=NOAH_PRICE):
    return {(FOREIGN, block): foreign, (NETUID, block): noah}


# ---- feeding the ledger through the real write ------------------------------


def put(
    store,
    *,
    day,
    amount,
    token=NETUID,
    block=None,
    ordinal=1,
    origin=HOT_A,
    hotkey=VALIDATOR,
    reason=None,
    coldkey=COLDKEY,
):
    """One deposit through ``runner._record``, the only place the NOAH/foreign split is made."""
    block = 8_800_000 + day * 100 + ordinal if block is None else block
    deposit = Deposit(
        coldkey=coldkey,
        amount_rao=amount,
        token_netuid=token,
        block=block,
        event_ordinal=ordinal,
        extrinsic_index=3,
        block_hash=hash_of(block),
        carried_by="StakeTransferred",
        hotkey=hotkey,
        origin_hotkey=origin,
        owner_at_deposit=coldkey if origin else None,
        unattributed_reason=reason,
    )
    _record(store, deposit, ANCHOR + day * DAY + 200, NETUID)
    return block


def a_store(tmp_path, name="noah.db"):
    store = Store.create(tmp_path / name, day_anchor_ts=ANCHOR)
    store.record_emission_start(
        block=8_000_000, block_timestamp=ANCHOR + DAY + 10, block_hash="0xstart"
    )
    return store


def scan(store, first, last, hotkeys=(HOT_A,)):
    for day in range(first, last + 1):
        store.mark_day_scanned(day, from_block=1, to_block=2)
        held_many(store, COLDKEY, day, {h: 1 for h in hotkeys})
    holdings_sampled(store, *range(first, last + 1))


def compose(store, day, seats, rule, registration_days=None):
    """What ``ChainAdapter.read`` does after the scan, in the same order, with the real
    functions: entry gate, per-seat entry credit, the entry-excluding store, the fixture."""
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    from noah.deposits.entry_gate import exclude_unconfirmed_entries

    seats, _dropped = exclude_unconfirmed_entries(store, seats, as_of_day=day)
    registration_days = registration_days or {COLDKEY: {HOT_A: 1}}
    entry_deposits, entry_days = _entry_deposits_for(store, registration_days, ENTRY, as_of_day=day)
    fixture_store = (
        _ExcludingEntryDeposit(store, entry_deposits, entry_days) if entry_deposits else store
    )
    fixture_registration_days = {c: dict(h) for c, h in registration_days.items()}
    for coldkey, per_hotkey in entry_days.items():
        fixture_registration_days.setdefault(coldkey, {}).update(per_hotkey)
    return build_fixture(
        fixture_store,
        name="foreign-credit",
        eval_day=day,
        day_epoch_ts=ANCHOR,
        dials=DIALS,
        seats=seats,
        foreign_rule=rule,
        entry_deposits=entry_deposits,
        registration_days=fixture_registration_days,
    )


def score(built):
    return score_fixture(parse_fixture(built))


def miner_row(output, uid=140):
    return next(m for m in output["miners"] if m["uid"] == uid)


SEATS = {COLDKEY: {HOT_A: 140}}


def story(tmp_path, *, noah_share=NEED, foreign=None, foreign_kwargs=None, name="noah.db"):
    """The acceptance story. Day 1: the 10 NOAH entry. Day 2: emissions. Day 3: a NOAH
    deposit of a share of them (``noah_share``; 0 for none) and a foreign deposit
    (``foreign`` alpha rao, or none). Returns ``(store, block of the foreign deposit)``."""
    store = a_store(tmp_path, name)
    scan(store, 1, 3)
    for d in (1, 2, 3):
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=140, hotkey=HOT_A)
    put(store, day=1, amount=ENTRY, ordinal=1)
    block = None
    if noah_share:
        put(store, day=3, amount=noah_share, ordinal=1)
    if foreign is not None:
        block = put(
            store, day=3, amount=foreign, token=FOREIGN, ordinal=2, **(foreign_kwargs or {})
        )
    return store, block


def rule_for(block, node=None, **price_kw):
    node = node or PriceNode(prices_at(block, **price_kw))
    return foreign_deposit_rule(price_chain(node), NETUID), node


# ---- 1. the split at the ledger write ----------------------------------------


def test_a_foreign_event_is_filed_as_foreign_and_never_as_noah(tmp_path):
    store = TR.a_store(tmp_path)
    foreign_events = TR.a_deposit(ordinal=145)
    for event in foreign_events:  # the same event shape, but alpha of subnet 9
        attrs = event["attributes"]
        if event["event_id"] == "StakeAdded":
            attrs[4] = FOREIGN
        else:
            attrs[3] = attrs[4] = FOREIGN
    noah_events = TR.a_deposit(xt=9, ordinal=300)
    chain = TR.Chain(deposits={8_800_001: foreign_events, 8_800_002: noah_events})
    TR.run(store, chain, last=8_800_002)

    assert store.noah_deposits(TR.GIVER, 0) == 469_790_773_712  # the NOAH one only
    noah_rows = store._conn.execute("SELECT COUNT(*) FROM noah_deposit").fetchone()[0]
    foreign_rows = store._conn.execute(
        "SELECT amount_rao, token_netuid, block, coldkey, day, hotkey, block_hash "
        "FROM foreign_deposit"
    ).fetchall()
    assert noah_rows == 1
    assert foreign_rows == [
        (469_790_773_712, FOREIGN, 8_800_001, TR.GIVER, 0, TR.HOTKEY, "0x864701")
    ]
    assert store.raw_foreign_deposits(TR.GIVER, 0)[0]["token_netuid"] == FOREIGN


def test_a_foreign_receipt_keeps_the_designated_validator_check(tmp_path):
    store = TR.a_store(tmp_path)
    events = TR.a_deposit(ordinal=145)
    for event in events:
        attrs = event["attributes"]
        if event["event_id"] == "StakeAdded":
            attrs[4] = FOREIGN
        else:
            attrs[3] = attrs[4] = FOREIGN
    chain = TR.Chain(deposits={8_800_001: events})
    TR.run(store, chain, designated_validator_hotkey=OTHER_VALIDATOR)
    (reason,) = store._conn.execute("SELECT unattributed_reason FROM foreign_deposit").fetchone()
    assert "not the designated Ark Validator" in reason
    assert store._conn.execute("SELECT COUNT(*) FROM noah_deposit").fetchone()[0] == 0


# ---- 2. the acceptance story ---------------------------------------------------


def test_acceptance_entry_emissions_noah_share_and_one_percent_foreign(tmp_path):
    """10 NOAH entry, emissions next day, a NOAH deposit of a share (50 NOAH), then about 1%
    in a foreign token. That day's foreign credit is greater than zero, at the spot price at
    the deposit's block.

    1.25 foreign alpha at 0.02 TAO is 0.025 TAO = 25_000_000 rao; at 0.05 TAO per NOAH that
    is 0.5 NOAH = 500_000_000 rao, 1% of the 50 NOAH share."""
    alpha = 1_250_000_000
    store, block = story(tmp_path, foreign=alpha)
    rule, node = rule_for(block)

    built = compose(store, 3, SEATS, rule)
    out = score(built)
    row = miner_row(out)

    day3 = built["miners"][0]["days"]["3"]
    assert day3["foreignDepositNoahEquivRao"] == "500000000"
    assert row["foreignCreditRao"] == "500000000"
    assert int(row["foreignCreditRao"]) > 0
    assert row["noahCreditRao"] == str(NEED)  # day 3's NOAH credit: min(share, prior pay)
    assert out["weightsU16"] == {"140": 65535}
    # Both prices were read once, at exactly the deposit's block hash, and nowhere else.
    assert sorted(node.calls) == sorted(
        [(FOREIGN, block, hash_of(block)), (NETUID, block, hash_of(block))]
    )


def test_the_foreign_value_is_floored_at_each_step(tmp_path):
    """7_777_777_777 foreign alpha: TAO = floor(7_777_777_777 * 0.02) = 155_555_555 (the
    exact 155_555_555.54 loses its fraction), then NOAH = floor(155_555_555 / 0.05) =
    3_111_111_100. One combined step would give 3_111_111_110: the tests pin two floors."""
    store, block = story(tmp_path, foreign=7_777_777_777)
    rule, _ = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == "3111111100"
    assert miner_row(score(built))["foreignCreditRao"] == "3111111100"


def test_a_foreign_deposit_larger_than_the_miners_noah_credit_is_capped(tmp_path):
    """The engine caps foreign credit at the room NOAH credit opened (N - F, F being 0
    here), so the credit equals N: the base is N + F = 2 * F."""
    huge = 10**6 * N
    store, block = story(tmp_path, foreign=huge)
    rule, _ = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    row = miner_row(score(built))
    equivalent = int(built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"])
    credit = int(row["foreignCreditRao"])
    assert equivalent == 10**6 * N * FOREIGN_PRICE // 10**9 * 10**9 // NOAH_PRICE
    assert 0 < credit < equivalent
    assert int(row["baseFp"]) == 2 * credit  # N == F: the room was used up exactly


def test_earlier_noah_history_with_no_new_noah_that_day_still_qualifies(tmp_path):
    """Entry on day 1 stands on its own; a day with only a foreign deposit (no NOAH
    deposit that day) is still credited against the room the entry opened."""
    store, block = story(tmp_path, noah_share=0, foreign=1_250_000_000)
    rule, _ = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["entryDepositRao"] == str(ENTRY)
    day3 = built["miners"][0]["days"]["3"]
    assert day3["noahDepositRao"] == "0"
    assert miner_row(score(built))["foreignCreditRao"] == "500000000"


def test_foreign_only_gets_no_entry_credit_and_no_weight(tmp_path):
    """A wallet that has only ever sent another token has no real NOAH: no entry credit,
    no seat past the gate, no weight, and nothing was priced."""
    store = a_store(tmp_path)
    scan(store, 1, 3)
    for d in (1, 2, 3):
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=140, hotkey=HOT_A)
    block = put(store, day=3, amount=5 * N, token=FOREIGN, ordinal=2)
    rule, node = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert all(m["entryDepositRao"] == "0" for m in built["miners"])
    out = score(built)
    assert out["weightsU16"] == {}
    assert all(m["foreignCreditRao"] == "0" for m in out["miners"])
    assert node.calls == []  # a foreign receipt of a wallet with no seat is never even priced
    assert store.cumulative_noah_deposits_for_hotkey(COLDKEY, HOT_A, 3) == 0


def test_a_miner_with_no_genuine_noah_credit_gets_none_even_beside_one_that_has_it(tmp_path):
    """Two seats on one wallet. HOT_A made the entry; HOT_B only ever sent foreign alpha
    naming itself. HOT_B has no seat past the gate and no credit; HOT_A's is unaffected."""
    store = a_store(tmp_path)
    scan(store, 1, 3, (HOT_A, HOT_B))
    for d in (1, 2, 3):
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=140, hotkey=HOT_A)
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=141, hotkey=HOT_B)
    put(store, day=1, amount=ENTRY, ordinal=1, origin=HOT_A)
    put(store, day=3, amount=NEED, ordinal=1, origin=HOT_A)
    block = put(store, day=3, amount=1_250_000_000, token=FOREIGN, ordinal=2, origin=HOT_B)
    rule, _ = rule_for(block)
    seats = {COLDKEY: {HOT_A: 140, HOT_B: 141}}
    built = compose(store, 3, seats, rule, {COLDKEY: {HOT_A: 1, HOT_B: 1}})
    out = score(built)
    assert {m["uid"] for m in built["miners"]} >= {140}
    assert 141 not in out["weightsU16"]
    assert miner_row(out, 140)["foreignCreditRao"] == "0"  # HOT_B's receipt is not HOT_A's
    assert not any(m["uid"] == 141 and m["foreignCreditRao"] != "0" for m in out["miners"])


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(
            {"hotkey": OTHER_VALIDATOR, "reason": "staked to X, not the designated Ark Validator"},
            id="wrong-validator",
        ),
        pytest.param(
            {"origin": None, "reason": UNNAMED_REASON_PREFIX + " a plain transfer"},
            id="no-miner-named",
        ),
        pytest.param({"origin": None}, id="no-attribution-at-all"),
    ],
)
def test_a_foreign_receipt_that_is_not_attributed_credits_nobody(tmp_path, kwargs):
    store, block = story(tmp_path, foreign=1_250_000_000, foreign_kwargs=kwargs)
    rule, node = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == "0"
    assert miner_row(score(built))["foreignCreditRao"] == "0"
    assert node.calls == []  # nobody earns it, so it is not valued


def test_a_foreign_receipt_is_credited_to_the_seat_that_it_names_only(tmp_path):
    store = a_store(tmp_path)
    scan(store, 1, 3, (HOT_A, HOT_B))
    for d in (1, 2, 3):
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=140, hotkey=HOT_A)
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=141, hotkey=HOT_B)
    for origin in (HOT_A, HOT_B):
        put(store, day=1, amount=ENTRY, ordinal=1 if origin == HOT_A else 2, origin=origin)
        put(store, day=3, amount=NEED, ordinal=1 if origin == HOT_A else 2, origin=origin)
    block = put(store, day=3, amount=1_250_000_000, token=FOREIGN, ordinal=5, origin=HOT_B)
    rule, _ = rule_for(block)
    built = compose(
        store,
        3,
        {COLDKEY: {HOT_A: 140, HOT_B: 141}},
        rule,
        {COLDKEY: {HOT_A: 1, HOT_B: 1}},
    )
    by_uid = {m["uid"]: m for m in built["miners"]}
    assert by_uid[140]["days"]["3"]["foreignDepositNoahEquivRao"] == "0"
    assert by_uid[141]["days"]["3"]["foreignDepositNoahEquivRao"] == "500000000"


# ---- 3. prices: exact block, one reading each, never a guess ------------------


def test_the_price_is_the_one_at_the_deposit_block_not_a_later_one(tmp_path):
    store, block = story(tmp_path, foreign=1_250_000_000)
    prices = prices_at(block)
    prices.update(prices_at(block + 1, foreign=999 * FOREIGN_PRICE))  # a later, higher price
    prices.update(prices_at(block - 1, foreign=999 * FOREIGN_PRICE))
    rule, node = rule_for(block, PriceNode(prices))
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == "500000000"
    assert {call[1] for call in node.calls} == {block}


def test_two_deposits_in_one_block_read_each_price_once_at_that_block(tmp_path):
    store, block = story(tmp_path, foreign=1_250_000_000)
    put(store, day=3, amount=1_250_000_000, token=FOREIGN, block=block, ordinal=3)
    rule, node = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == "1000000000"
    assert sorted(node.calls) == sorted(
        [(FOREIGN, block, hash_of(block)), (NETUID, block, hash_of(block))]
    )


def test_each_deposit_is_valued_at_its_own_blocks_price(tmp_path):
    store, block = story(tmp_path, foreign=1_250_000_000)
    other = put(store, day=3, amount=1_250_000_000, token=FOREIGN, ordinal=3, block=block + 50)
    prices = prices_at(block)
    prices.update(prices_at(other, foreign=2 * FOREIGN_PRICE))  # twice the price: twice the value
    rule, node = rule_for(block, PriceNode(prices))
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == str(
        500_000_000 + 1_000_000_000
    )
    assert {call[1] for call in node.calls} == {block, other}


def _unpriced_cases():
    return [
        pytest.param({}, id="no-price-at-all"),
        pytest.param("only-noah", id="foreign-price-missing"),
        pytest.param("only-foreign", id="noah-price-missing"),
        pytest.param("zero", id="zero"),
        pytest.param("none", id="none"),
        pytest.param("bool", id="bool"),
        pytest.param("negative", id="negative"),
    ]


@pytest.mark.parametrize("case", _unpriced_cases())
def test_a_price_that_cannot_be_read_is_a_named_refusal_and_the_deposit_is_kept(tmp_path, case):
    """The chosen behaviour: a named refusal (``ForeignDepositUnpriced``). The day is not
    scored, nothing is valued at zero or at another block's price, and the deposit stays
    recorded so the next attempt, once the price at that block can be read, includes it."""
    store, block = story(tmp_path, foreign=1_250_000_000)
    full = prices_at(block)
    node = (
        {
            "only-noah": PriceNode({(NETUID, block): NOAH_PRICE}),
            "only-foreign": PriceNode({(FOREIGN, block): FOREIGN_PRICE}),
        }.get(case)
        if isinstance(case, str)
        else PriceNode(case)
    )
    if isinstance(case, str) and case in ("zero", "none", "bool", "negative"):
        bad = {"zero": _Scale(0), "none": None, "bool": _Scale(True), "negative": _Scale(-5)}[case]
        node = PriceNode(full, override={(FOREIGN, block): bad})
    rule, _ = rule_for(block, node)
    with pytest.raises(ForeignDepositUnpriced) as refused:
        compose(store, 3, SEATS, rule)
    assert str(block) in str(refused.value)
    # Not dropped, not zeroed:
    assert store.raw_foreign_deposits(COLDKEY, 3)[0]["amount_rao"] == 1_250_000_000
    # And a later reading of the same block, once the node can answer, values it in full.
    rule_ok, _ = rule_for(block)
    assert (
        compose(store, 3, SEATS, rule_ok)["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"]
        == "500000000"
    )


def test_an_unpriced_deposit_refuses_for_a_later_day_too(tmp_path):
    """A refused price is not forgotten when the day moves on: the deposit is still
    inside the decay, so every later day refuses too until the price at its block is read."""
    store, block = story(tmp_path, foreign=1_250_000_000)
    scan(store, 4, 4)
    paid(store, COLDKEY, 4, PAY, uid=140, hotkey=HOT_A)
    rule, _ = rule_for(block, PriceNode({}))
    with pytest.raises(ForeignDepositUnpriced):
        compose(store, 4, SEATS, rule)


def test_the_refusal_does_not_leak_the_endpoint(tmp_path):
    store, block = story(tmp_path, foreign=1_250_000_000)
    node = PriceNode({})
    chain = LiveRunnerChain(node, netuid=NETUID, endpoint="wss://secret-host.example/KEYVALUE123")
    rule = foreign_deposit_rule(chain, NETUID)
    with pytest.raises(ForeignDepositUnpriced) as refused:
        compose(store, 3, SEATS, rule)
    assert "KEYVALUE123" not in str(refused.value)


def test_a_foreign_row_in_the_scored_token_is_refused_not_valued(tmp_path):
    rule, _ = rule_for(8_800_300)
    with pytest.raises(ForeignDepositUnpriced):
        rule(5 * N, NETUID, 8_800_300, hash_of(8_800_300))


def test_agreeing_chain_refuses_when_the_two_sources_disagree_on_a_price(tmp_path):
    block = 8_800_303
    one = price_chain(PriceNode(prices_at(block)), "a")
    two = price_chain(PriceNode(prices_at(block, foreign=FOREIGN_PRICE + 1)), "b")
    chain = AgreeingChain(
        {"a": one, "b": two}, {"a": "wss://a.example:443", "b": "wss://b.example:443"}
    )
    assert chain.alpha_price_at(block, NETUID) == NOAH_PRICE  # they agree here
    rule = foreign_deposit_rule(chain, NETUID)
    with pytest.raises(ForeignDepositUnpriced) as refused:
        rule(N, FOREIGN, block, hash_of(block))
    assert "disagree" in str(refused.value)


def test_agreeing_chain_refuses_when_one_source_cannot_read_the_price():
    block = 8_800_303
    one = price_chain(PriceNode(prices_at(block)), "a")
    two = price_chain(PriceNode({}), "b")
    chain = AgreeingChain(
        {"a": one, "b": two}, {"a": "wss://a.example:443", "b": "wss://b.example:443"}
    )
    rule = foreign_deposit_rule(chain, NETUID)
    with pytest.raises(ForeignDepositUnpriced):
        rule(N, FOREIGN, block, hash_of(block))


def test_agreeing_chain_values_a_deposit_when_both_sources_agree():
    block = 8_800_303
    one = price_chain(PriceNode(prices_at(block)), "a")
    two = price_chain(PriceNode(prices_at(block)), "b")
    chain = AgreeingChain(
        {"a": one, "b": two}, {"a": "wss://a.example:443", "b": "wss://b.example:443"}
    )
    rule = foreign_deposit_rule(chain, NETUID)
    assert rule(1_250_000_000, FOREIGN, block, hash_of(block)) == 500_000_000


def test_noah_equivalent_rao_is_integer_floor_math():
    assert noah_equivalent_rao(7_777_777_777, FOREIGN_PRICE, NOAH_PRICE) == 3_111_111_100
    assert noah_equivalent_rao(0, FOREIGN_PRICE, NOAH_PRICE) == 0
    assert noah_equivalent_rao(1, FOREIGN_PRICE, NOAH_PRICE) == 0  # a dust amount floors to zero
    for bad in (0, -1, True, 1.5, None):
        with pytest.raises(ValueError):
            noah_equivalent_rao(N, FOREIGN_PRICE, bad)


# ---- 4. rescan: a store scanned by the older reader is not trusted -----------


def an_old_store(tmp_path):
    """A store as the pre-#16 code left it: schema 17, an eight-column foreign_deposit,
    no rescan table, and EVERY deposit (the foreign alpha included) filed as NOAH."""
    store = a_store(tmp_path, "old.db")
    scan(store, 1, 3)
    for d in (1, 2, 3):
        paid(store, COLDKEY, d, PAY if d >= 2 else 0, uid=140, hotkey=HOT_A)
    put(store, day=1, amount=ENTRY, ordinal=1)
    put(store, day=3, amount=NEED, ordinal=1)
    store.close()
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE foreign_deposit")
    conn.execute(
        "CREATE TABLE foreign_deposit (block INTEGER NOT NULL, event_ordinal INTEGER NOT NULL, "
        "extrinsic_index INTEGER NOT NULL, coldkey TEXT NOT NULL, day INTEGER NOT NULL, "
        "amount_rao INTEGER NOT NULL, token_netuid INTEGER NOT NULL, block_hash TEXT NOT NULL, "
        "PRIMARY KEY (block, event_ordinal))"
    )
    conn.execute("DROP TABLE day_needs_rescan")
    # the old reader's misfiling: foreign alpha of subnet 9, recorded as a NOAH deposit
    conn.execute(
        "INSERT INTO noah_deposit (block, event_ordinal, extrinsic_index, coldkey, day, "
        "amount_rao, block_hash, carried_by, hotkey, owner_at_deposit, origin_hotkey) "
        "VALUES (8800302, 2, 3, ?, 3, ?, ?, 'StakeTransferred', ?, ?, ?)",
        (COLDKEY, 1_250_000_000, hash_of(8800302), VALIDATOR, COLDKEY, HOT_A),
    )
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()
    return path


def test_a_store_scanned_by_the_old_reader_is_refused_until_rescanned(tmp_path):
    path = an_old_store(tmp_path)
    store = Store.open(path, day_anchor_ts=ANCHOR)  # migrate, mark stale, delete nothing
    for day in (1, 2, 3):
        assert not store.day_was_scanned(day)
    with pytest.raises(DayNotScanned):
        store.noah_deposits(COLDKEY, 3)
    with pytest.raises(DayNotScanned):
        store.noah_deposits_for_hotkey(COLDKEY, HOT_A, 3)
    with pytest.raises(DayNotScanned):
        store.foreign_deposit_equivalent_for_hotkey(COLDKEY, HOT_A, 3, lambda *a: 0)
    with pytest.raises(DayNotScanned):  # and a fixture is never built over it
        compose(store, 3, SEATS, None)
    # The old rows are still there, untrusted, not deleted.
    assert store._conn.execute("SELECT COUNT(*) FROM noah_deposit").fetchone()[0] == 3


def test_a_short_rescan_cannot_clear_a_marked_day(tmp_path):
    path = an_old_store(tmp_path)
    store = Store.open(path, day_anchor_ts=ANCHOR)
    # The prior scan covers blocks 1..2. Seeing no events in block 1 alone
    # cannot establish the contents of the old whole range.
    with pytest.raises(Refusal):
        store.mark_day_scanned(3, from_block=1, to_block=1, observed_receipts=set())
    assert not store.day_was_scanned(3)
    assert store._conn.execute("SELECT COUNT(*) FROM noah_deposit").fetchone()[0] == 3


def test_a_new_store_and_an_already_migrated_one_are_not_marked(tmp_path):
    store = a_store(tmp_path)
    scan(store, 1, 2)
    path = tmp_path / "noah.db"
    store.close()
    again = Store.create(path, day_anchor_ts=ANCHOR)
    assert again.day_was_scanned(1) and again.day_was_scanned(2)


def test_a_same_event_filed_as_noah_in_a_trusted_day_is_not_silently_moved(tmp_path):
    from noah.chain.store import ObservationChanged

    store = a_store(tmp_path)
    scan(store, 3, 3)
    put(store, day=3, amount=NEED, ordinal=1, block=8_800_300)
    with pytest.raises(ObservationChanged):
        put(store, day=3, amount=NEED, ordinal=1, block=8_800_300, token=FOREIGN)


def test_no_refusal_lets_a_run_continue():
    assert issubclass(ForeignDepositUnpriced, Refusal)


def test_mainnet_profile_ten_noah_entry_and_one_percent_foreign(tmp_path, monkeypatch):
    """The user's exact 10-NOAH story, under the real mainnet economic profile."""
    import sys
    module = sys.modules[__name__]
    path = _REPO_ROOT / "docs/parameters/profiles/mainnet-production-v1.json"
    profile = json.loads(path.read_text())
    monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
    monkeypatch.setattr(module, "ENTRY", int(profile["entryDepositPerHotkeyRao"]))
    monkeypatch.setattr(module, "DIALS", {key: profile[key] for key in DIALS})
    assert ENTRY == 10 * N
    store, block = story(tmp_path, noah_share=50 * N, foreign=1_250_000_000)
    rule, node = rule_for(block)
    built = compose(store, 3, SEATS, rule)
    assert built["miners"][0]["entryDepositRao"] == str(10 * N)
    assert miner_row(score(built))["foreignCreditRao"] == "500000000"
    assert sorted(node.calls) == sorted([
        (FOREIGN, block, hash_of(block)), (NETUID, block, hash_of(block)),
    ])


@pytest.mark.parametrize("column", ["origin_hotkey", "owner_at_deposit", "hotkey", "block_timestamp"])
def test_incomplete_foreign_metadata_cannot_feed_credit(tmp_path, column):
    store, block = story(tmp_path, foreign=1_250_000_000)
    # Model a legacy/incomplete receipt. A destination validator is never
    # accepted as a substitute for an explicitly attributed miner.
    store._conn.execute(f"UPDATE foreign_deposit SET {column} = NULL")
    store._conn.commit()
    rule, node = rule_for(block)
    assert store.foreign_deposit_equivalent_for_hotkey(COLDKEY, HOT_A, 3, rule) == 0
    assert node.calls == []
    assert store._conn.execute("SELECT COUNT(*) FROM foreign_deposit").fetchone()[0] == 1


@pytest.mark.parametrize('profile_name', ['mainnet-production-v1'])
@pytest.mark.parametrize('first,second,surplus', [
    (3 * N, 7 * N, 0), (5 * N, 5 * N, 0), (5 * N, 7 * N, 2 * N),
    (30_000_000, 10 * N, 30_000_000),
])
def test_split_entry_crossing_day_scores_once_and_unlocks_foreign(
    tmp_path, monkeypatch, profile_name, first, second, surplus
):
    """User-approved cumulative entry, through production ledger/fixture/engines.

    No prior earnings. Entry tokens cannot also enter the ordinary deposit leg.
    A stored later receipt cannot qualify the miner when evaluating day1.
    """
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    from noah.core.ts_engine import render_with_ts_engine
    from noah.scoring.fixture_io import render_output

    profile_path = _REPO_ROOT / 'docs/parameters/profiles' / (profile_name + '.json')
    profile = json.loads(profile_path.read_text())
    monkeypatch.setitem(globals(), 'ENTRY', int(profile['entryDepositPerHotkeyRao']))
    monkeypatch.setenv('NOAH_PARAMETERS_JSON', str(profile_path))
    monkeypatch.setitem(globals(), 'DIALS', {key: profile[key] for key in DIALS})
    store = a_store(tmp_path)
    scan(store, 1, 3)
    put(store, day=1, amount=first, ordinal=1)
    put(store, day=2, amount=second, ordinal=1)
    block = put(store, day=2, amount=250_000_000, token=FOREIGN, ordinal=2)
    rule, node = rule_for(block)
    regs = {COLDKEY: {HOT_A: 1}}
    assert _entry_deposits_for(store, regs, ENTRY, as_of_day=1) == ({}, {})
    early = compose(store, 1, SEATS, rule, regs)
    assert score(early)['weightsU16'] == {}
    assert node.calls == []

    credited, days = _entry_deposits_for(store, regs, ENTRY, as_of_day=2)
    assert credited == {COLDKEY: {HOT_A: 10 * N}}
    assert days == {COLDKEY: {HOT_A: 2}}
    wrapped = _ExcludingEntryDeposit(store, credited, days)
    for read in (lambda d: wrapped.noah_deposits(COLDKEY, d),
                 lambda d: wrapped.noah_deposits_for_hotkey(COLDKEY, HOT_A, d)):
        assert read(1) == 0
        assert read(2) == surplus
        assert read(1) + read(2) + ENTRY == first + second
    assert wrapped.cumulative_noah_deposits_for_hotkey(COLDKEY, HOT_A, 2) == first + second
    assert store.noah_deposits(COLDKEY, 1) == first  # persisted receipts unchanged
    built = compose(store, 2, SEATS, rule, regs)
    miner = built['miners'][0]
    assert miner['registrationDay'] == '2'
    assert miner['entryDepositRao'] == str(ENTRY)
    assert miner['days']['1']['noahDepositRao'] == '0'
    assert miner['days']['2']['noahDepositRao'] == str(surplus)
    result = score(built)
    assert miner_row(result)['noahCreditRao'] == str(ENTRY)
    assert miner_row(result)['foreignCreditRao'] == '100000000'
    assert result['weightsU16'] == {'140': 65535}
    assert render_with_ts_engine(built) == render_output(result)
    assert set(node.calls) == {(FOREIGN, block, hash_of(block)), (NETUID, block, hash_of(block))}
    before_restart = render_output(result)
    path = tmp_path / 'noah.db'
    store.close()
    reopened = Store.open(path, day_anchor_ts=ANCHOR)
    try:
        assert render_output(score(compose(reopened, 2, SEATS, rule, regs))) == before_restart
        next_day = compose(reopened, 3, SEATS, rule, regs)
        assert next_day['miners'][0]['registrationDay'] == '2'
        assert int(miner_row(score(next_day))['noahCreditRao']) < ENTRY  # decays, no new entry
        assert render_with_ts_engine(next_day) == render_output(score(next_day))
    finally:
        reopened.close()


def test_partial_entry_is_per_seat_and_preserves_reregistration_guard(tmp_path):
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    store = a_store(tmp_path)
    scan(store, 1, 3, hotkeys=(HOT_A, HOT_B))
    put(store, day=1, amount=3 * N, ordinal=1)
    put(store, day=2, amount=7 * N, ordinal=1)
    put(store, day=2, amount=5 * N, ordinal=2, origin=HOT_B)
    regs = {COLDKEY: {HOT_A: 1, HOT_B: 2}}
    credited, days = _entry_deposits_for(store, regs, ENTRY, as_of_day=3)
    assert credited == {COLDKEY: {HOT_A: ENTRY}}
    wrapped = _ExcludingEntryDeposit(store, credited, days)
    assert wrapped.noah_deposits(COLDKEY, 2) == 5 * N
    assert wrapped.noah_deposits_for_hotkey(COLDKEY, HOT_A, 2) == 0
    assert wrapped.noah_deposits_for_hotkey(COLDKEY, HOT_B, 2) == 5 * N
    assert _entry_deposits_for(store, {COLDKEY: {HOT_A: 3}}, ENTRY, as_of_day=3) == ({}, {})


@pytest.mark.parametrize('invalid', ['wrong-validator', 'unnamed', 'foreign'])
def test_partial_entry_cannot_be_completed_by_ineligible_receipts(tmp_path, invalid):
    from noah.core.chain_adapter import _entry_deposits_for
    store = a_store(tmp_path)
    scan(store, 1, 2)
    put(store, day=1, amount=3 * N, ordinal=1)
    kwargs = {'token': FOREIGN} if invalid == 'foreign' else (
        {'hotkey': OTHER_VALIDATOR, 'reason': 'not the designated Ark Validator'}
        if invalid == 'wrong-validator' else {'origin': None, 'reason': UNNAMED_REASON_PREFIX + 'missing'}
    )
    put(store, day=2, amount=7 * N, ordinal=1, **kwargs)
    assert _entry_deposits_for(store, {COLDKEY: {HOT_A: 1}}, ENTRY, as_of_day=2) == ({}, {})
    assert score(compose(store, 2, SEATS, None))['weightsU16'] == {}


def test_partial_entry_does_not_cross_an_unscanned_gap(tmp_path):
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    store = a_store(tmp_path)
    scan(store, 1, 1)
    scan(store, 3, 3)
    put(store, day=1, amount=3 * N, ordinal=1)
    put(store, day=3, amount=7 * N, ordinal=1)
    regs = {COLDKEY: {HOT_A: 1}}
    assert _entry_deposits_for(store, regs, ENTRY, as_of_day=3) == ({}, {})
    with pytest.raises(DayNotScanned):
        _ExcludingEntryDeposit(store, {COLDKEY: {HOT_A: ENTRY}}, {COLDKEY: {HOT_A: 3}})
    scan(store, 2, 2)
    assert _entry_deposits_for(store, regs, ENTRY, as_of_day=3) == (
        {COLDKEY: {HOT_A: ENTRY}}, {COLDKEY: {HOT_A: 3}})


def test_same_day_partial_entry_and_surplus_remain_single_counted(tmp_path):
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    store = a_store(tmp_path)
    scan(store, 1, 1)
    put(store, day=1, amount=3 * N, ordinal=1)
    put(store, day=1, amount=8 * N, ordinal=2)
    credited, days = _entry_deposits_for(store, {COLDKEY: {HOT_A: 1}}, ENTRY, as_of_day=1)
    assert credited == {COLDKEY: {HOT_A: ENTRY}}
    assert days == {COLDKEY: {HOT_A: 1}}
    wrapped = _ExcludingEntryDeposit(store, credited, days)
    assert wrapped.noah_deposits_for_hotkey(COLDKEY, HOT_A, 1) == N
    assert wrapped.noah_deposits(COLDKEY, 1) == N
    assert miner_row(score(compose(store, 1, SEATS, None)))['noahCreditRao'] == str(ENTRY)


def test_stale_partial_receipt_day_cannot_supply_entry_credit(tmp_path):
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    store = a_store(tmp_path)
    scan(store, 1, 2)
    put(store, day=1, amount=3 * N, ordinal=1)
    put(store, day=2, amount=7 * N, ordinal=1)
    store._conn.execute('INSERT INTO day_needs_rescan(day) VALUES (1)')
    regs = {COLDKEY: {HOT_A: 1}}
    assert _entry_deposits_for(store, regs, ENTRY, as_of_day=2) == ({}, {})
    with pytest.raises(DayNotScanned):
        _ExcludingEntryDeposit(store, {COLDKEY: {HOT_A: ENTRY}}, {COLDKEY: {HOT_A: 2}})
