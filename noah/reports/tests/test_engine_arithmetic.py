"""Engine behavior on a scripted chain: arithmetic, locks, and fail-closed gates."""

from noah.reports.backing.constants import ARK_PUBKEY_HEX, PERQUINTILL_ONE
from noah.reports.backing.engine import _compute as compute
from noah.reports.backing.keys import hex_decode
from noah.reports.backing.result import canonical_dumps, percent_string
from noah.reports.backing.scale import Availability, SimSwap
from noah.reports.backing.u64f64 import calculate_price, drop_ppm
from noah.reports.tests.fakes import FakeChain, alpha_storage_key

BLOCK = "0x" + "ab" * 32
QUOTE = PERQUINTILL_ONE // 2
X = Y = 50_000_000_000_000
NETUID = 82


def _chain():
    chain = FakeChain(BLOCK)
    chain.netuid = NETUID
    chain.set_pool(NETUID, alpha_in=X, tao=Y, alpha_out=X * 4, quote=QUOTE)
    return chain


def _add(chain, pubkey: bytes, total: int, locked: int, available: int):
    hot = bytes([pubkey[0] ^ 0x5A]) + pubkey[1:]
    chain.alpha_keys.append("0x" + alpha_storage_key(hot, pubkey, NETUID).hex())
    chain.availability[pubkey] = Availability(total, locked, available)


def _result(chain, number=None):
    result, _meta = compute(chain, NETUID, BLOCK, number)
    canonical_dumps(result)
    return result


def test_output1_uses_available_and_floors_the_drop():
    chain = _chain()
    holder = bytes([0x22]) + bytes(31)
    sold = 2_000_000_000_000
    _add(chain, holder, sold, 0, sold)
    chain.sim = lambda amount: SimSwap(amount // 10, amount, 0, 0, 0, 0)
    result = _result(chain)
    assert result["status"] == "OK"
    price_after = calculate_price(X + sold, Y - sold // 10, QUOTE)
    ppm = drop_ppm(calculate_price(X, Y, QUOTE), price_after)
    assert result["output1"]["drop_ppm"] == ppm
    assert result["output1"]["percent"] == percent_string(ppm)
    assert result["output1"]["alpha_sold_rao"] == sold
    assert result["holder"]["available_rao"] == sold
    assert int(canonical_dumps(result).split('"alpha_required_rao":"')[1].split('"')[0]) % 1_000_000 == 0
    assert result["output2"]["alpha_decimal"].count(".") == 1
    assert len(result["output2"]["alpha_decimal"].split(".")[1]) == 3
    assert result["output2"]["k_minus_1"]["qualifies"] is False
    assert result["output1"]["status"] == "OK"
    text = canonical_dumps(result)
    assert "e+" not in text and "E+" not in text


def test_locked_holder_loses_to_larger_available_and_ark_is_excluded():
    chain = _chain()
    ark = hex_decode(ARK_PUBKEY_HEX)
    locked_holder = bytes([0x30]) + bytes(31)
    seller = bytes([0x11]) + bytes(31)
    _add(chain, ark, 9_000_000_000_000, 0, 9_000_000_000_000)
    _add(chain, locked_holder, 8_000_000_000_000, 5_000_000_000_000, 3_000_000_000_000)
    _add(chain, seller, 4_000_000_000_000, 0, 4_000_000_000_000)
    chain.locks[locked_holder] = 5_000_000_000_000
    result = _result(chain)
    assert result["holder"]["pubkey"] == "0x" + seller.hex()
    assert result["holder"]["available_rao"] == 4_000_000_000_000
    assert result["ark_context"]["present"] is True
    assert "sealed Ark account" in result["ark_context"]["note"]
    assert result["holder"]["pubkey"] != "0x" + ark.hex()


def test_tie_uses_the_lowest_pubkey():
    chain = _chain()
    high = bytes([0x90]) + bytes(31)
    low = bytes([0x10]) + bytes(31)
    _add(chain, high, 1000, 0, 1000)
    _add(chain, low, 1000, 0, 1000)
    result = _result(chain)
    assert result["holder"]["pubkey"] == "0x" + low.hex()
    assert result["holder"]["tie_count"] == 2


def test_ark_absent_is_reported_as_absent():
    chain = _chain()
    _add(chain, bytes([0x44]) + bytes(31), 1000, 0, 1000)
    result = _result(chain)
    assert result["ark_context"]["present"] is False
    assert result["ark_context"]["available_rao"] == 0


def test_invalid_output1_does_not_become_zero_percent_and_thresholds_continue():
    chain = _chain()
    _add(chain, bytes([0x45]) + bytes(31), 5_000_000_000, 0, 5_000_000_000)

    def sim(amount):
        if amount == 5_000_000_000:
            return SimSwap(0, 0, 0, 0, 0, 0)
        fee = amount * 33 // 65535
        return SimSwap(amount // 40, amount - fee, 0, fee, 0, 0)

    chain.sim = sim
    result = _result(chain)
    assert result["status"] == "OK"
    assert result["output1"]["status"] == "UNAVAILABLE"
    assert result["output1"]["reason"] == "all_zero_sim"
    assert "drop_ppm" not in result["output1"]
    assert result["output2"]["status"] == "OK"


def test_collateral_cross_check_uses_available_not_total():
    chain = _chain()
    pubkey = bytes([0x61]) + bytes(31)
    _add(chain, pubkey, 1000, 400, 500)
    chain.locks[pubkey] = 400
    chain.collateral[pubkey] = 100
    from noah.reports.backing.keys import coldkey_miner_collateral_key, hex_encode
    from noah.reports.backing.scale import encode_u64

    chain.storage[hex_encode(coldkey_miner_collateral_key(NETUID, pubkey))] = hex_encode(encode_u64(100))
    result = _result(chain)
    assert result["status"] == "OK"
    assert result["holder"]["available_rao"] == 500
    assert result["holder"]["collateral_derived_rao"] == 100
    assert result["holder"]["total_rao"] == 1000
