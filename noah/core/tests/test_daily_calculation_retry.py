"""Same closed-day retries retain identical financial inputs across attempt heights."""
import copy
import contextvars
from pathlib import Path

import pytest

from ...chain.store import Store
from ...chain.tests._agreeing_helpers import (
    ARK, EPOCH, SEAT0_COLD, SEAT0_HOT, SEAT1_COLD, SEAT1_HOT,
    VALIDATOR_HOTKEY, a_gated_chain, pay_event,
)
from ..calculation_record import CalculationEvidenceRefused, verify
from ._adapter_helpers import DIALS, FLOORED_ANCHOR, FakeReader, an_adapter


@pytest.fixture(autouse=True)
def selected_profile(monkeypatch):
    service = Path(__file__).resolve().parents[3]
    relative = Path('docs/parameters/profiles/m1-revised-v1.json')
    profile = service / relative  # Self-contained public layout.
    if not profile.is_file():
        profile = service.parents[1] / relative  # Private monorepo layout.
    monkeypatch.setenv('NOAH_PARAMETERS_JSON', str(profile))
    monkeypatch.delenv('NOAH_EVENT_LOG', raising=False)


def retry_adapter(tmp_path):
    chain = a_gated_chain(events={EPOCH: [pay_event([700, 300])]},
                         head=EPOCH + 200, first_emission_block=EPOCH - 2)
    adapter, _, _ = an_adapter(
        reader=FakeReader(current_block=EPOCH + 200, tempo=1000,
                          committed=set(), live_uids=[0, 1], owner_uids=set()),
        runner_chain=chain,
        seats_source=lambda day, store: {SEAT0_COLD: {SEAT0_HOT: 0}, SEAT1_COLD: {SEAT1_HOT: 1}},
        holding_stride=1, day_window=lambda height: (0, EPOCH - 2, EPOCH + 2),
        ark_coldkey=ARK, designated_validator_hotkey=VALIDATOR_HOTKEY, dials=DIALS,
        day_anchor_ts=FLOORED_ANCHOR, read_collateral=True,
        store_factory=lambda: Store.create(tmp_path / 'history.db', day_anchor_ts=FLOORED_ANCHOR),
        submitted_record_dir=tmp_path / 'scores', settle_blocks=1,
        entry_deposit_rao=100_000_000_000,
    )
    return adapter


def calculate(adapter, height):
    def run():
        fixture = adapter.read(height)
        vector = adapter.agree(fixture)  # real independent Python and TypeScript engines
        reference = adapter.record_calculation(height, fixture, vector, vector)
        return fixture, vector, reference
    return contextvars.copy_context().run(run)


def test_same_day_retry_at_new_height_preserves_canonical_inputs_and_score(tmp_path):
    adapter = retry_adapter(tmp_path)
    first, vector, ref = calculate(adapter, EPOCH + 100)
    paths = [tmp_path / 'scores' / name for name in ('day-0.fixture.json', 'day-0.json')]
    original = [p.read_bytes() for p in paths]
    second, next_vector, next_ref = calculate(adapter, EPOCH + 101)
    assert first == second
    assert vector == next_vector
    assert [p.read_bytes() for p in paths] == original
    assert verify(tmp_path / 'scores', ref)['height'] == EPOCH + 100
    assert verify(tmp_path / 'scores', next_ref)['height'] == EPOCH + 101
    assert ref != next_ref  # actual attempt provenance stays separate


def test_changed_financial_input_on_retry_still_refuses_without_overwrite(tmp_path):
    adapter = retry_adapter(tmp_path)
    calculate(adapter, EPOCH + 100)
    paths = [tmp_path / 'scores' / name for name in ('day-0.fixture.json', 'day-0.json')]
    original = [p.read_bytes() for p in paths]
    fixture = copy.deepcopy(adapter.read(EPOCH + 101))
    fixture['dials']['harshness'] += 1
    vector = adapter.agree(fixture)
    with pytest.raises(CalculationEvidenceRefused, match='differs'):
        contextvars.copy_context().run(
            adapter.record_calculation, EPOCH + 101, fixture, vector, vector
        )
    assert [p.read_bytes() for p in paths] == original
