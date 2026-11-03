import copy

import pytest

from noah.core.publish_intent import PublicationStatusUnknown
from noah.research.tests.test_starter_epoch import R, checked, request
from noah.starter.second_opinion import check


@pytest.mark.parametrize("n", [1, 2, 16, 32, 81])
def test_independent_allocator_agrees_for_distinct_and_shared_wallets(n):
    for shared in (False, True):
        raw = request(n)
        if shared:
            for m in raw["fixture"]["miners"][1:]:
                m["coldkey"] = "shared"
            for seat in raw["snapshot"]["seats"][1:]:
                seat["coldkey"] = "shared"
            raw["lifetimeEarnings"] = []
        out = checked(raw)
        check(raw, None, out)


def test_independent_allocator_rejects_vector_tampering():
    raw = request()
    out = checked(raw)
    out["proposedWeightsU16"]["1"] += 1
    out["proposedWeightsU16"]["0"] -= 1
    with pytest.raises(PublicationStatusUnknown):
        check(raw, None, out)


def test_entry_graduation_and_lifetime_wallet_exhaustion():
    raw = request(2)
    for seat in raw["snapshot"]["seats"][1:]:
        seat["spendableEarnedRao"] = 5 * R
    out = checked(raw)
    check(raw, None, out)
    assert out["proposedWeightsU16"]["1"] == 0


@pytest.mark.parametrize("newcomers", [0, 2])
@pytest.mark.parametrize("departed", [1, 3])
def test_historical_unseated_rows_remain_in_score_but_not_publishable_vectors(newcomers, departed):
    raw = request(newcomers)
    baseline = checked(raw)
    for index in range(departed):
        historical = copy.deepcopy(raw["fixture"]["miners"][0])
        historical.update(uid=None, coldkey=f"departed-{index}")
        raw["fixture"]["miners"].append(historical)
    result = checked(raw)
    assert sum(m["uid"] is None for m in result["ordinaryOutput"]["miners"]) == departed
    assert result["proposedWeightsU16"] == baseline["proposedWeightsU16"]
    assert result["ordinaryFallbackWeightsU16"] == baseline["ordinaryFallbackWeightsU16"]
    check(raw, None, result)
    result["ordinaryFallbackWeightsU16"]["null"] = 0
    with pytest.raises(PublicationStatusUnknown):
        check(raw, None, result)
