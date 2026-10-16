"""Entry-deposit and ongoing-hold values use the canonical parameter reader.

Each accessor delegates to ``noah.scoring.parameters.load_parameters()``,
which validates the complete profile before returning the requested field.
Fixtures therefore include all required profile fields, matching
``docs/parameters/profiles/m1-revised-v1.json``.

Reader failures are translated from ``ParametersError`` to ``RuntimeError``.
Each call reads and parses the file again; changes between calls are visible
without a cached value.
"""

import json

import pytest

from noah.config.profile import entry_deposit_rao, ongoing_hold_rao

#: A complete, real-shaped profile, matching
#: ``docs/parameters/profiles/m1-revised-v1.json``'s actual field set --
#: ``noah.scoring.parameters.load_parameters()`` requires every one of
#: these fields and rejects any it does not recognize.
_FULL_PROFILE = {
    "profileVersion": "m1-revised-v1",
    "createdAt": "2026-09-20T05:24:42Z",
    "targetFp": "900000000",
    "harshness": 2,
    "capFp": "1000000000",
    "halfLifeDays": "45",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "90",
    "ongoingHoldRao": "0",
    "entryDepositPerHotkeyRao": "10000000000",
}


def _write_parameters(path, **overrides):
    fields = dict(_FULL_PROFILE)
    fields.update(overrides)
    path.write_text(json.dumps(fields))


def _write_parameters_missing(path, *drop_fields, **overrides):
    fields = dict(_FULL_PROFILE)
    fields.update(overrides)
    for field in drop_fields:
        fields.pop(field, None)
    path.write_text(json.dumps(fields))


class TestEntryDepositRao:
    def test_reads_the_real_configured_figure(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=10_000_000_000, ongoingHoldRao=0)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert entry_deposit_rao() == 10_000_000_000

    def test_never_caches_rereads_the_file_every_call(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=10_000_000_000, ongoingHoldRao=0)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert entry_deposit_rao() == 10_000_000_000
        _write_parameters(path, entryDepositPerHotkeyRao=25_000_000_000, ongoingHoldRao=0)
        assert entry_deposit_rao() == 25_000_000_000

    def test_raises_when_the_env_var_is_unset(self, monkeypatch):
        monkeypatch.delenv("NOAH_PARAMETERS_JSON", raising=False)
        with pytest.raises(RuntimeError, match="NOAH_PARAMETERS_JSON is not set"):
            entry_deposit_rao()

    def test_raises_when_the_file_does_not_exist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(tmp_path / "missing.json"))
        with pytest.raises(RuntimeError, match="could not be read"):
            entry_deposit_rao()

    def test_raises_on_malformed_json(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        path.write_text("{not valid json")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="could not be read"):
            entry_deposit_rao()

    def test_raises_when_the_top_level_json_is_not_an_object(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        path.write_text("[1, 2, 3]")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="expected a JSON object"):
            entry_deposit_rao()

    def test_raises_when_an_unknown_field_is_present(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, notARealField="1")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="unknown field"):
            entry_deposit_rao()

    def test_raises_when_the_field_is_missing(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters_missing(path, "entryDepositPerHotkeyRao")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="missing field.*entryDepositPerHotkeyRao"):
            entry_deposit_rao()

    def test_raises_when_the_field_is_a_boolean(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=True)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="boolean"):
            entry_deposit_rao()

    @pytest.mark.parametrize("bad_value", [100.5, None, [100]])
    def test_raises_when_the_field_is_not_an_integer_or_decimal_string(
        self, tmp_path, monkeypatch, bad_value
    ):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=bad_value)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="expected integer or decimal string"):
            entry_deposit_rao()

    @pytest.mark.parametrize("bad_value", ["100.5", "-1", "01", "", "abc", " 100"])
    def test_raises_when_a_string_field_is_not_a_canonical_decimal(
        self, tmp_path, monkeypatch, bad_value
    ):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=bad_value)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="not a canonical decimal string"):
            entry_deposit_rao()

    def test_accepts_a_canonical_decimal_string_field(self, tmp_path, monkeypatch):
        """Deposit and hold values accept canonical decimal strings as well as integers."""
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao="10000000000", ongoingHoldRao="0")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert entry_deposit_rao() == 10_000_000_000
        assert ongoing_hold_rao() == 0

    def test_raises_on_a_negative_figure(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, entryDepositPerHotkeyRao=-1)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="negative"):
            entry_deposit_rao()


class TestOngoingHoldRao:
    def test_reads_the_real_configured_figure(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, ongoingHoldRao=400_000_000_000)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert ongoing_hold_rao() == 400_000_000_000

    def test_a_configured_zero_is_a_valid_figure_not_treated_as_unset(self, tmp_path, monkeypatch):
        """An explicitly configured zero is valid and must not be treated as missing."""
        path = tmp_path / "parameters.json"
        _write_parameters(path, ongoingHoldRao=0)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert ongoing_hold_rao() == 0

    def test_raises_when_the_field_is_missing(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters_missing(path, "ongoingHoldRao")
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        with pytest.raises(RuntimeError, match="missing field.*ongoingHoldRao"):
            ongoing_hold_rao()

    def test_never_caches_rereads_the_file_every_call(self, tmp_path, monkeypatch):
        path = tmp_path / "parameters.json"
        _write_parameters(path, ongoingHoldRao=400_000_000_000)
        monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
        assert ongoing_hold_rao() == 400_000_000_000
        _write_parameters(path, ongoingHoldRao=0)
        assert ongoing_hold_rao() == 0
