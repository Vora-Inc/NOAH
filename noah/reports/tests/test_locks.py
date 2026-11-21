"""Lock, collateral, tie, and Ark selection checks from plan section 5.C."""

from noah.reports.backing.constants import ARK_NOTE, ARK_PUBKEY_HEX, PERQUINTILL_ONE
from noah.reports.backing.engine import _compute as compute
from noah.reports.backing.keys import coldkey_miner_collateral_key, hex_decode, hex_encode
from noah.reports.backing.scale import Availability, encode_u64
from noah.reports.tests.fakes import FakeChain, alpha_storage_key

BLOCK = "0x" + "ab" * 32
NETUID = 82


def _chain():
    chain = FakeChain(BLOCK)
    chain.netuid = NETUID
    chain.set_pool(
        NETUID,
        alpha_in=50_000_000_000_000,
        tao=50_000_000_000_000,
        alpha_out=200_000_000_000_000,
        quote=PERQUINTILL_ONE // 2,
    )
    return chain


def _add(chain, pubkey: bytes, total: int, locked: int, available: int):
    hot = bytes([pubkey[0] ^ 0x5A]) + pubkey[1:]
    chain.alpha_keys.append("0x" + alpha_storage_key(hot, pubkey, NETUID).hex())
    chain.availability[pubkey] = Availability(total, locked, available)


def test_largest_total_loses_when_it_is_locked():
    chain = _chain()
    locked = bytes([0x30]) + bytes(31)
    seller = bytes([0x11]) + bytes(31)
    _add(chain, locked, 8_000, 5_000, 3_000)
    _add(chain, seller, 4_000, 0, 4_000)
    chain.locks[locked] = 5_000
    result, _meta = compute(chain, NETUID, BLOCK, None)
    assert result["holder"]["pubkey"] == "0x" + seller.hex()
    assert result["output1"]["alpha_sold_rao"] == 4_000


def test_ark_excluded_even_with_the_largest_available():
    chain = _chain()
    ark = hex_decode(ARK_PUBKEY_HEX)
    other = bytes([0x12]) + bytes(31)
    _add(chain, ark, 9_000, 0, 9_000)
    _add(chain, other, 1_000, 0, 1_000)
    result, _meta = compute(chain, NETUID, BLOCK, None)
    assert result["holder"]["pubkey"] == "0x" + other.hex()
    assert result["ark_context"]["note"] == ARK_NOTE
    assert result["ark_context"]["available_rao"] == 9_000


def test_tie_names_the_lowest_pubkey():
    chain = _chain()
    high = bytes([0x90]) + bytes(31)
    low = bytes([0x10]) + bytes(31)
    _add(chain, high, 1000, 0, 1000)
    _add(chain, low, 1000, 0, 1000)
    named, _meta = compute(chain, NETUID, BLOCK, None)
    chain.alpha_keys.reverse()
    reversed_result, _meta2 = compute(chain, NETUID, BLOCK, None)
    assert named["holder"]["pubkey"] == reversed_result["holder"]["pubkey"] == "0x" + low.hex()
    assert named["output1"]["drop_ppm"] == reversed_result["output1"]["drop_ppm"]


def test_collateral_cross_check():
    chain = _chain()
    pubkey = bytes([0x61]) + bytes(31)
    _add(chain, pubkey, 1000, 400, 500)
    chain.locks[pubkey] = 400
    chain.storage[hex_encode(coldkey_miner_collateral_key(NETUID, pubkey))] = hex_encode(encode_u64(100))
    result, _meta = compute(chain, NETUID, BLOCK, None)
    assert result["status"] == "OK"
    assert result["holder"]["available_rao"] == 500
    assert result["holder"]["total_rao"] == 1000
