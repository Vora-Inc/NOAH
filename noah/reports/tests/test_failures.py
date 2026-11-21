from noah.reports.backing.constants import PERQUINTILL_ONE
from noah.reports.backing.engine import _compute as compute
from noah.reports.backing.scale import Availability
from noah.reports.tests.fakes import FakeChain, alpha_storage_key

BLOCK = "0x" + "cd" * 32
NETUID = 82


def _ready():
    chain = FakeChain(BLOCK)
    chain.netuid = NETUID
    chain.set_pool(
        NETUID,
        alpha_in=20_000_000_000,
        tao=20_000_000_000,
        alpha_out=40_000_000_000,
        quote=PERQUINTILL_ONE // 2,
    )
    pubkey = bytes([7]) + bytes(31)
    hot = bytes([9]) + bytes(31)
    chain.alpha_keys.append("0x" + alpha_storage_key(hot, pubkey, NETUID).hex())
    chain.availability[pubkey] = Availability(1000, 0, 1000)
    return chain


def _reason(chain, **kwargs):
    result, _meta = compute(chain, NETUID, kwargs.get("block", BLOCK), kwargs.get("number"))
    return result["status"], result["reason"], result


def test_stable_mechanism_has_no_numbers_and_does_not_scan():
    chain = _ready()
    chain.set_pool(NETUID, alpha_in=20_000_000_000, tao=20_000_000_000, alpha_out=1, quote=PERQUINTILL_ONE // 2, mechanism=0)
    status, reason, result = _reason(chain)
    assert status == "UNSUPPORTED" and reason == "stable_mechanism"
    assert "output1" not in result
    assert not any(method == "state_getKeysPaged" for method, _params in chain.calls)


def test_each_pool_gate():
    chain = _ready()
    from noah.reports.backing.keys import hex_encode, subnet_state_key

    del chain.storage[hex_encode(subnet_state_key(NETUID))]
    assert _reason(chain)[:2] == ("UNAVAILABLE", "subnet_not_found")

    chain = _ready()
    chain.storage[hex_encode(subnet_state_key(NETUID))] = "0x02"
    assert _reason(chain)[:2] == ("UNAVAILABLE", "subnet_dissolving")

    chain = _ready()
    chain.storage[hex_encode(subnet_state_key(NETUID))] = "0x03"
    assert _reason(chain)[:2] == ("UNAVAILABLE", "subnet_dissolving")

    chain = _ready()
    from noah.reports.backing.keys import subtoken_enabled_key

    chain.storage[hex_encode(subtoken_enabled_key(NETUID))] = "0x00"
    assert _reason(chain)[:2] == ("UNAVAILABLE", "subtoken_disabled")

    chain = _ready()
    from noah.reports.backing.keys import pal_swap_initialized_key

    chain.storage[hex_encode(pal_swap_initialized_key(NETUID))] = "0x00"
    assert _reason(chain)[:2] == ("UNAVAILABLE", "pool_uninitialized")

    chain = _ready()
    chain.set_pool(NETUID, alpha_in=10, tao=20_000_000_000, alpha_out=1, quote=PERQUINTILL_ONE // 2)
    assert _reason(chain)[:2] == ("UNAVAILABLE", "reserves_too_low")

    chain = _ready()
    chain.set_pool(NETUID, alpha_in=20_000_000_000, tao=20_000_000_000, alpha_out=1, quote=PERQUINTILL_ONE)
    assert _reason(chain)[:2] == ("UNAVAILABLE", "zero_price")


def test_block_and_spec_gates():
    chain = _ready()
    chain.canonical = "0x" + "11" * 32
    assert _reason(chain)[:2] == ("UNAVAILABLE", "block_not_canonical")

    chain = _ready()
    chain.finalized = "0x" + "22" * 32
    chain.finalized_number = 50
    chain.number = 80
    assert _reason(chain)[:2] == ("UNAVAILABLE", "block_above_finalized")

    chain = _ready()
    assert _reason(chain, number=99)[:2] == ("UNAVAILABLE", "block_number_mismatch")

    chain = _ready()
    chain.spec = 489
    assert _reason(chain)[:2] == ("UNSUPPORTED", "runtime_spec_not_pinned")


def test_transport_failure_stays_on_the_pinned_block():
    chain = _ready()
    chain.fail = "state_getStorage"
    status, reason, _result = _reason(chain)
    assert status == "UNAVAILABLE" and reason == "rpc_transport"
    for method, params in chain.calls:
        if method.startswith("state_"):
            assert params[-1] == BLOCK


def test_inconsistent_availability_fails_closed():
    chain = _ready()
    pubkey = bytes([7]) + bytes(31)
    chain.availability[pubkey] = Availability(100, 0, 150)
    assert _reason(chain)[:2] == ("UNAVAILABLE", "availability_inconsistent")


def test_lock_mismatch_fails_closed():
    chain = _ready()
    pubkey = bytes([7]) + bytes(31)
    chain.availability[pubkey] = Availability(1000, 100, 900)
    chain.locks[pubkey] = 50
    assert _reason(chain)[:2] == ("UNAVAILABLE", "lock_cross_check_failed")
