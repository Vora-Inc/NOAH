"""Conflicting retained responses cannot choose a convenient replay result."""

import pytest

from noah.reports.backing.rpc import result_sha256
from noah.reports.backing.source import ReplayError, ReplaySource

PIN = "0x" + "11" * 32
PARAMS = ["0x1234", PIN]


def receipt(result):
    digest, size = result_sha256(result)
    return {"method": "state_getStorage", "params": PARAMS,
            "result": result, "sha256": digest, "byte_length": size}


@pytest.mark.parametrize("results", [("0x01", "0x02"), ("0x02", "0x01"), (None, "0x01")])
def test_valid_individual_hashes_do_not_hide_conflicting_answers(results):
    with pytest.raises(ReplayError, match="conflicting_response"):
        ReplaySource([receipt(value) for value in results], PIN)


def test_repeated_identical_response_is_replayable():
    source = ReplaySource([receipt("0x01"), receipt("0x01")], PIN)
    assert source.call("state_getStorage", PARAMS) == "0x01"
