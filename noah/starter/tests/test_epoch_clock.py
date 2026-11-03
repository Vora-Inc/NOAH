import pytest

from noah.research.tests.test_starter_epoch import request, sync
from noah.starter.allocation import preview


def request_live():
    raw = request()
    raw["schema"] = "noah-starter-epoch-preview/4"
    snap = raw["snapshot"]
    del snap["epochZeroTimestamp"]
    snap.update(
        epoch=900,
        nextEpochBlock=1261,
        weightEffectiveEpoch=1261,
        pendingThroughEpoch=1260,
        epochStartTimestamp=snap["finalizedTimestamp"] - 100,
    )
    return raw


def test_real_epoch_block_clock_allows_variable_block_times():
    raw = request_live()
    first = preview(raw)
    snap = raw["snapshot"]
    snap.update(
        epoch=1261,
        nextEpochBlock=1622,
        finalizedBlock=1300,
        finalizedHash="next-block",
        epochStartTimestamp=snap["finalizedTimestamp"] + 4700,
        finalizedTimestamp=snap["finalizedTimestamp"] + 5000,
        weightEffectiveEpoch=1622,
        pendingThroughEpoch=1621,
    )
    for row in snap["seats"]:
        row["observedBlock"] = 1300
    snap["miningPoolSample"]["endTimestamp"] = snap["finalizedTimestamp"]
    sync(raw)
    second = preview(raw, first["state"])
    assert second["validUntilBlock"] == 1622
    assert second["allocatedStarterWeightU16"] == first["allocatedStarterWeightU16"]


@pytest.mark.parametrize("block", [899, 1261, 2000])
def test_outside_observed_epoch_refused(block):
    raw = request_live()
    raw["snapshot"]["finalizedBlock"] = block
    with pytest.raises(ValueError, match="outside the observed epoch"):
        preview(raw)
