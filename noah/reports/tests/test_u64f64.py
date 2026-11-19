import json
from pathlib import Path

import pytest

from noah.reports.backing.constants import PERQUINTILL_ONE
from noah.reports.backing.u64f64 import calculate_price, drop_ppm, price_1e9, safe_div, saturating_mul

VECTORS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "oracle-vectors.json"
ORACLE = Path(__file__).resolve().parents[1] / "oracle" / "target" / "release" / "backing-form-oracle"


def test_drop_ppm_floors():
    assert drop_ppm(1000, 333) == 667_000
    assert drop_ppm(3, 1) == 666_666


def test_safe_div_zero_and_overflow_are_zero():
    assert safe_div(1 << 64, 0) == 0
    huge = (1 << 128) - 1
    assert safe_div(huge, 1) == 0
    assert saturating_mul(huge, huge) == (1 << 128) - 1


def test_sn82_sketch_matches_runtime_price_scale():
    quote = int("06f05b597227b973", 16)
    bits = calculate_price(69_342_974_999_283, 2_246_289_429_597, quote)
    assert price_1e9(bits) == 32_393_900


def test_half_weight_price_is_reserve_ratio():
    bits = calculate_price(4, 2, PERQUINTILL_ONE // 2)
    assert price_1e9(bits) == 500_000_000


def test_oracle_vectors_match_python():
    if not VECTORS.exists():
        pytest.skip("oracle vectors are generated after the Rust oracle build")
    document = json.loads(VECTORS.read_text())
    assert len(document["vectors"]) >= 200
    for vector, expected in zip(document["vectors"], document["results"], strict=True):
        if vector["op"] == "price":
            bits = calculate_price(int(vector["x"]), int(vector["y"]), int(vector["quote"]))
            assert str(bits) == expected["bits"]
            assert str(price_1e9(bits)) == expected["price_1e9"]
        elif vector["op"] == "div":
            assert str(safe_div(int(vector["lhs"]), int(vector["rhs"]))) == expected["bits"]
        else:
            assert str(saturating_mul(int(vector["lhs"]), int(vector["rhs"]))) == expected["bits"]
