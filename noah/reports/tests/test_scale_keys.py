from noah.reports.backing.keys import (
    alpha_v2_prefix,
    hex_decode,
    parse_alpha_v2_key,
    ss58_decode,
    ss58_encode,
    twox128,
    xxh64,
)
from noah.reports.backing.scale import (
    decode_sim,
    decode_u16,
    encode_sim_args,
    encode_u16,
)


def test_xxh64_and_system_account_prefix():
    assert f"{xxh64(b''):016x}" == "ef46db3751d8e999"
    assert f"{xxh64(b'abc'):016x}" == "44bc2cf5ad770999"
    prefix = twox128(b"System") + twox128(b"Account")
    assert prefix.hex() == "26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9"


def test_netuid_82_scale_argument_is_little_endian():
    assert encode_u16(82).hex() == "5200"
    assert encode_sim_args(82, 1).hex() == "52000100000000000000"


def test_sim_roundtrip_and_ark_ss58():
    from noah.reports.backing.constants import ARK_PUBKEY_HEX, ARK_SS58

    raw = bytes.fromhex("010000000000000002000000000000000300000000000000040000000000000005000000000000000600000000000000")
    sim = decode_sim(raw)
    assert sim.as_tuple() == (1, 2, 3, 4, 5, 6)
    pubkey = hex_decode(ARK_PUBKEY_HEX)
    assert ss58_encode(pubkey) == ARK_SS58
    assert ss58_decode(ARK_SS58) == pubkey


def test_alpha_v2_key_parses_netuid_from_the_tail():
    from noah.reports.backing.keys import blake2_128_concat, netuid_le

    hot = bytes(range(32))
    cold = bytes(range(32, 64))
    key = alpha_v2_prefix() + blake2_128_concat(hot) + blake2_128_concat(cold) + netuid_le(82)
    assert parse_alpha_v2_key(key) == (hot, cold, 82)
    assert decode_u16(encode_u16(82)) == 82
