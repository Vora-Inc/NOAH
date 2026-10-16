"""Both real chain readers must price the exact receipt hash, even if they agree otherwise."""
import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.foreign_value import ForeignDepositUnpriced, foreign_deposit_rule
from noah.chain.tests.test_foreign_credit import (
    FOREIGN,
    NETUID,
    N,
    PriceNode,
    hash_of,
    price_chain,
    prices_at,
)


def test_agreeing_prices_at_a_different_hash_are_refused_before_runtime_reads():
    block = 8_800_303
    nodes = [PriceNode(prices_at(block)), PriceNode(prices_at(block))]
    chain = AgreeingChain({"a": price_chain(nodes[0], "a"), "b": price_chain(nodes[1], "b")},
                          {"a": "wss://a.example:443", "b": "wss://b.example:443"})
    rule = foreign_deposit_rule(chain, NETUID)
    with pytest.raises(ForeignDepositUnpriced, match="receipt records"):
        rule(N, FOREIGN, block, hash_of(block + 1))
    assert not any(n.calls for n in nodes)


def test_cached_price_cannot_be_reused_for_another_receipt_hash():
    block = 8_800_303
    node = PriceNode(prices_at(block))
    rule = foreign_deposit_rule(price_chain(node), NETUID)
    assert rule(N, FOREIGN, block, hash_of(block)) == 400_000_000
    assert len(node.calls) == 2
    with pytest.raises(ForeignDepositUnpriced):
        rule(N, FOREIGN, block, hash_of(block + 1))
    assert len(node.calls) == 2


@pytest.mark.parametrize("bad", [None, "", "0x", "0x" + "z" * 64, True])
def test_missing_or_malformed_receipt_hash_never_reads_a_price(bad):
    block = 8_800_303
    node = PriceNode(prices_at(block))
    with pytest.raises(ForeignDepositUnpriced, match="exact block hash"):
        foreign_deposit_rule(price_chain(node), NETUID)(N, FOREIGN, block, bad)
    assert node.calls == []


def test_extreme_positive_spot_is_recorded_without_an_extra_cap():
    block = 8_800_303
    node = PriceNode(prices_at(block, foreign=10**18, noah=1))
    evidence = []
    value = foreign_deposit_rule(price_chain(node), NETUID, evidence=evidence)(
        N, FOREIGN, block, hash_of(block)
    )
    assert value == 10**27
    assert evidence == [{
        "block": block, "block_hash": hash_of(block),
        "token_netuid": FOREIGN, "noah_netuid": NETUID,
        "amount_rao": str(N), "foreign_price_rao": str(10**18),
        "noah_price_rao": "1", "noah_equivalent_rao": str(value),
        "policy": "deposit_block_spot_floor_each_step",
    }]


def test_root_tao_uses_its_exact_block_runtime_price_and_noah_price():
    # Runtime 479 dual-node read at block 175031 returned root price 1e9.
    # Keep reading the API at the receipt hash; do not hardcode a head price.
    block = 175031
    node = PriceNode({(0, block): N, (NETUID, block): 50_000_000})
    value = foreign_deposit_rule(price_chain(node), NETUID)(
        5_000_000, 0, block, hash_of(block)
    )
    assert value == 100_000_000
    assert node.calls == [(0, block, hash_of(block)), (NETUID, block, hash_of(block))]


def test_nonexact_second_conversion_rounds_down_from_chain_prices():
    block = 42
    node = PriceNode(prices_at(block, foreign=20_000_000, noah=30_000_000))
    rule = foreign_deposit_rule(price_chain(node), NETUID)
    # First floor: 20,000,000.14 ->20,000,000 TAO rao.
    # Second floor: 666,666,666.666... ->666,666,666 NOAH rao.
    assert rule(1_000_000_007, FOREIGN, block, hash_of(block)) == 666_666_666
    assert len(node.calls) == 2


@pytest.mark.parametrize("invalid", [0, True])
def test_invalid_price_from_rule_reader_is_named_refusal(invalid):
    class InvalidPriceReader:
        def alpha_price_at(self, block, *, netuid, expected_block_hash):
            return invalid

    with pytest.raises(ForeignDepositUnpriced, match="positive whole number"):
        foreign_deposit_rule(InvalidPriceReader(), NETUID)(N, FOREIGN, 42, hash_of(42))
