"""Report cutoffs retain millisecond precision through independent readers."""

from types import SimpleNamespace

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.sources import SourceDisagreement
from noah.chain.tests.test_live_epoch_reads import _chain


def test_report_clock_keeps_fractional_second_without_changing_legacy_clock():
    chain = _chain(storage={("Timestamp", "Now"): 1794528180123})
    assert chain.block_timestamp(100) == 1794528180
    assert chain.block_timestamp_ms(100) == 1794528180123


def test_readers_disagreeing_within_one_second_cannot_freeze_report_cutoff():
    def source(milliseconds):
        return SimpleNamespace(block_hash=lambda _block: "0x" + "aa" * 32,
                               block_timestamp_ms=lambda _block: milliseconds)
    chain = AgreeingChain({"a": source(1794528180001), "b": source(1794528180999)},
                         {"a": "http://a.invalid", "b": "http://b.invalid"},
                         canary=lambda *_: 256)
    with pytest.raises(SourceDisagreement):
        chain.block_timestamp_ms(100)
