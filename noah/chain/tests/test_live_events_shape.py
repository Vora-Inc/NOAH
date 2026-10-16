"""``LiveRunnerChain.events()``'s ``dict(_event_dict(event), ...)`` cast,
decode-shape focused (issue #538).

Found and flagged, not fixed, while closing #536/#537: the same pattern
class as #532 (``int(_plain(...))``) and #536/#537 (``str(_plain(...))``) --
a bare cast applied straight to ``_plain()``'s output, this time ``dict(...)``
instead of ``int``/``str``. Unlike those three, this one was NOT live-broken:
a real scan of the practice chain (``ws://127.0.0.1:9955``, 2026-08-30, see
``_event_dict``'s own docstring in ``live.py`` for the exact block range and
counts) found every real event already decodes as a genuine ``dict``, so
``_plain(event)`` -- a no-op on something with no ``.value`` attribute -- was
never garbling or crashing on it the way the other three sites' bare casts
did on their own confirmed real shapes.

This file is the hardening issue #538 itself asks for even though nothing
was broken: ``_event_dict`` now unwraps the ONE confirmed shape and refuses
everything else by name, the same discipline ``_mechanism_count_scalar`` and
``_account_id32_address`` (``test_live_epoch_reads.py``) already have tests
for. The real event fixtures below are copied verbatim from that live scan,
not invented, so a reader can see the actual wire shape this guard accepts.
"""

import pytest

from noah.chain.live import _EVENT_RECORD_KEYS, ChainUnavailable, LiveRunnerChain, _event_dict

_HASH = "0x" + "ab" * 32
BLOCK = 51696


class _ScaleValue:
    """Stands in for a ``scalecodec``-style decoded object exposing
    ``.value`` -- see ``test_live_epoch_reads.py``'s identical helper. Real
    events from the installed client (``async_substrate_interface==2.2.1``,
    floored at ``>=2.2.1`` by ``pyproject.toml``) never arrive in this shape
    (its own ``get_events()`` already does the ``.value`` unwrap before this
    project ever sees the list -- see ``_event_dict``'s docstring), so this
    exercises the defensive path only: if a future client version stopped
    doing that unwrap itself, this guard must still cope rather than
    silently break."""

    def __init__(self, value):
        self.value = value


class _Connection:
    """Enough of a raw substrate connection for ``events()``:
    ``get_block_hash`` and ``get_events``. ``events`` maps a block number to
    whatever ``get_events`` should hand back for it."""

    def __init__(self, *, block_hash=_HASH, events=None):
        self._block_hash = block_hash
        self._events = dict(events or {})

    def get_block_hash(self, block):
        return self._block_hash

    def get_events(self, block_hash=None):
        return self._events[BLOCK]


def _chain(**kwargs):
    return LiveRunnerChain(_Connection(**kwargs), netuid=2, endpoint="wss://node.example")


# -- the confirmed real shape, copied verbatim from a live scan -------------
#
# ``ws://127.0.0.1:9955``, block 51696, position 0 -- the actual first
# element ``get_events()`` returned, before this project's own code ever
# touches it. Already a genuine dict; no ``.value`` attribute anywhere in
# it, at any nesting level.

_REAL_BALANCES_ISSUED_EVENT = {
    "phase": "Initialization",
    "extrinsic_idx": None,
    "event": {
        "event_index": "050f",
        "module_id": "Balances",
        "event_id": "Issued",
        "attributes": {"amount": 1000000000},
    },
    "event_index": 5,
    "module_id": "Balances",
    "event_id": "Issued",
    "attributes": {"amount": 1000000000},
    "topics": [],
}

# Same live scan, a second real shape (an extrinsic-triggered event, not an
# Initialization-phase one) -- block 51710, position 1.

_REAL_SYSTEM_EXTRINSIC_SUCCESS_EVENT = {
    "phase": "ApplyExtrinsic",
    "extrinsic_idx": 0,
    "event": {
        "event_index": "0000",
        "module_id": "System",
        "event_id": "ExtrinsicSuccess",
        "attributes": {
            "dispatch_info": {
                "weight": {"ref_time": 321973000, "proof_size": 5691},
                "class": "Mandatory",
                "pays_fee": "Yes",
            }
        },
    },
    "event_index": 0,
    "module_id": "System",
    "event_id": "ExtrinsicSuccess",
    "attributes": {
        "dispatch_info": {
            "weight": {"ref_time": 321973000, "proof_size": 5691},
            "class": "Mandatory",
            "pays_fee": "Yes",
        }
    },
    "topics": [],
}


def test_event_dict_accepts_the_confirmed_real_shape_unchanged():
    assert _event_dict(_REAL_BALANCES_ISSUED_EVENT) == _REAL_BALANCES_ISSUED_EVENT


def test_events_reads_the_confirmed_real_shape_through_the_full_chain_call():
    """The actual call shape ``run_day`` uses -- ``chain.events(block)`` --
    not a hand-assembled shortcut straight to the guard."""
    chain = _chain(events={BLOCK: [_REAL_BALANCES_ISSUED_EVENT]})
    result = chain.events(BLOCK)
    assert result == [dict(_REAL_BALANCES_ISSUED_EVENT, ordinal_in_block=0)]


def test_events_gives_each_of_several_real_events_its_own_position():
    chain = _chain(
        events={BLOCK: [_REAL_BALANCES_ISSUED_EVENT, _REAL_SYSTEM_EXTRINSIC_SUCCESS_EVENT]}
    )
    result = chain.events(BLOCK)
    assert result[0]["ordinal_in_block"] == 0
    assert result[0]["module_id"] == "Balances"
    assert result[1]["ordinal_in_block"] == 1
    assert result[1]["module_id"] == "System"


def test_events_on_a_genuine_dict_does_not_mutate_the_original():
    """``dict(_event_dict(event), ordinal_in_block=position)`` must build a
    new dict, not add the key onto the one the connection handed back --
    the same object could be read again (e.g. by a second source in an
    ``AgreeingChain`` comparison) and must not carry a stamp from this
    read."""
    original = dict(_REAL_BALANCES_ISSUED_EVENT)
    chain = _chain(events={BLOCK: [original]})
    chain.events(BLOCK)
    assert "ordinal_in_block" not in original


# -- defensive hardening: a future client that stops unwrapping itself ------


def test_event_dict_unwraps_a_scalecodec_style_wrapper_defensively():
    """Not a shape the installed client (2.2.1) ever produces -- its own
    ``get_events()`` already does this unwrap before this project's code
    runs (see ``_event_dict``'s docstring in ``live.py``) -- but the same
    ``_plain()`` this project's every other decode site relies on is applied
    here too, so a future client version that stopped doing its own
    internal unwrap would still be handled rather than silently broken."""
    wrapped = _ScaleValue(_REAL_BALANCES_ISSUED_EVENT)
    assert _event_dict(wrapped) == _REAL_BALANCES_ISSUED_EVENT


def test_events_unwraps_a_wrapper_around_the_whole_list_too():
    """[codex review round 2, issue #538, 2026-08-30, P2] found that the
    per-event wrapper defense above does not cover a future client that
    wraps the WHOLE event list rather than each element: ``events()``'s own
    ``isinstance(raw, (list, tuple))`` check ran on the unwrapped-only-per-
    element value, so a list-level wrapper was refused before ``_event_dict``
    ever ran, even though the docstring claimed this shape was handled.
    ``events()`` now applies ``_plain`` to the whole ``raw`` value first --
    proven here through the real ``chain.events()`` call, not just a
    unit-level check of the unwrap in isolation."""
    chain = _chain(
        events={
            BLOCK: _ScaleValue([_REAL_BALANCES_ISSUED_EVENT, _REAL_SYSTEM_EXTRINSIC_SUCCESS_EVENT])
        }
    )
    result = chain.events(BLOCK)
    assert result[0]["ordinal_in_block"] == 0
    assert result[0]["module_id"] == "Balances"
    assert result[1]["ordinal_in_block"] == 1
    assert result[1]["module_id"] == "System"


# -- everything that is NOT the confirmed shape refuses, by name ------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="empty_list"),
        pytest.param(["module_id", "event_id"], id="list_of_strings"),
        pytest.param((1, 2, 3), id="bare_tuple"),
        pytest.param("Balances.Issued", id="bare_string"),
        pytest.param(5, id="bare_int"),
        pytest.param(None, id="bare_none"),
        pytest.param(True, id="bare_bool"),
        pytest.param({"module_id", "event_id"}, id="a_set_not_a_dict"),
    ],
)
def test_event_dict_refuses_every_shape_that_is_not_a_genuine_dict(raw):
    """The issue's own warning, the same one #532 and #536/#537 were each
    found by heeding: a shape this project has not confirmed real must
    refuse loudly rather than be guessed at. None of these is the confirmed
    dict shape, even after ``_plain`` (which only ever unwraps a ``.value``
    attribute -- none of these has one)."""
    with pytest.raises(ValueError, match="not the genuine dict"):
        _event_dict(raw)


def test_event_dict_refusal_names_the_bad_shape_and_what_to_check():
    with pytest.raises(ValueError, match=r"decoded as \[\]"):
        _event_dict([])
    with pytest.raises(
        ValueError, match=r"check whether the installed async_substrate_interface version"
    ):
        _event_dict([])


def test_events_refuses_a_malformed_event_through_the_full_chain_too():
    """Same malformed shape, reached the real way -- through ``events()``,
    which must convert the guard's ``ValueError`` into the ``ChainUnavailable``
    every other failed read on this class already raises (via ``_ask``),
    not let a bare ``ValueError`` escape uncaught."""
    chain = _chain(events={BLOCK: [_REAL_BALANCES_ISSUED_EVENT, "not an event"]})
    with pytest.raises(ChainUnavailable, match="not the genuine dict"):
        chain.events(BLOCK)


def test_a_dict_subclass_is_not_silently_trusted():
    """[same discipline as ``_is_plain_mechid_int``/``_is_plain_byte``]
    ``type(value) is dict`` rather than ``isinstance`` on purpose: a dict
    subclass could override ``__getitem__``/iteration to lie about its own
    contents. Nothing in this project's real decode path has ever produced
    one -- the live scan found only the builtin ``dict`` -- but refusing it
    here costs nothing and removes the question, the same reasoning
    ``_is_plain_mechid_int`` and ``_is_plain_byte`` already document.

    Scope note [codex adversarial challenge, issue #538, 2026-08-30, P2]:
    this proves a dict subclass with NO ``.value`` attribute is refused.
    A dict subclass that ALSO carries a ``.value`` attribute would be
    unwrapped by ``_plain`` before this check ever runs -- not a gap this
    function introduces (every guard in this file shares ``_plain``'s same
    unwrap-first contract), and not a shape any real decode has ever
    produced. See ``_event_dict``'s own docstring in ``live.py`` for why
    that is out of this guard's scope rather than silently accepted."""

    class _LyingDict(dict):
        pass

    lying = _LyingDict(_REAL_BALANCES_ISSUED_EVENT)
    with pytest.raises(ValueError, match="not the genuine dict"):
        _event_dict(lying)


# -- the confirmed shape needs the confirmed FIELDS, not just any dict ------
#
# [codex adversarial challenge, issue #538, 2026-08-30, P1] proved the first
# version of this guard (type(plain) is dict, nothing else) let an empty {}
# straight through: dict({}, ordinal_in_block=0) succeeds, and the resulting
# {'ordinal_in_block': 0} reaches deposits.py/emissions.py's own .get(...)
# reads as an event with every field absent -- which those two modules SKIP
# rather than refuse (deposits.py:249, emissions.py:290), silently dropping
# a block's real deposit or pay event instead of loudly refusing the read.


def test_event_dict_refuses_an_empty_dict_the_p1_finding_reproduced():
    """The exact repro from the adversarial challenge: an empty dict passed
    the old type-only check and was silently treated as a real, contentless
    event two modules downstream."""
    with pytest.raises(ValueError, match="missing"):
        _event_dict({})


def test_events_refuses_an_empty_dict_through_the_full_chain_too():
    chain = _chain(events={BLOCK: [{}]})
    with pytest.raises(ChainUnavailable, match="missing"):
        chain.events(BLOCK)


@pytest.mark.parametrize("missing_key", sorted(_EVENT_RECORD_KEYS))
def test_event_dict_refuses_a_dict_missing_any_one_confirmed_field(missing_key):
    """Every one of the eight confirmed ``EventRecord`` fields is load-
    bearing: dropping any single one, not just going all the way to an
    empty dict, must still refuse -- a partially-truncated record is exactly
    as unconfirmed a shape as a fully empty one."""
    incomplete = {k: v for k, v in _REAL_BALANCES_ISSUED_EVENT.items() if k != missing_key}
    with pytest.raises(ValueError) as exc_info:
        _event_dict(incomplete)
    assert "missing" in str(exc_info.value)
    assert missing_key in str(exc_info.value)


def test_event_dict_accepts_a_dict_with_confirmed_keys_plus_an_extra_one():
    """The check is a SUPERSET test (``_EVENT_RECORD_KEYS <= plain.keys()``),
    not an exact match -- a future client adding one more benign metadata
    field alongside the eight confirmed ones must not be refused just for
    carrying more than expected; missing a confirmed field is the dangerous
    case, not carrying an extra one."""
    extra = dict(_REAL_BALANCES_ISSUED_EVENT, a_new_field="something benign")
    assert _event_dict(extra) == extra


# -- present-but-worthless values need refusing too, not just absent keys ---
#
# [codex adversarial challenge round 2, issue #538, 2026-08-30, P1] proved
# the key-presence check alone was not enough: a dict carrying all eight
# confirmed keys, every value None, still passed. deposits.py's and
# emissions.py's own `record.get("module_id") != "SubtensorModule"` checks
# then treat a None module_id the same as an honest unrelated module --
# silently skipped, not refused. Direct reproduction before this fix:
# _pay_index() returned None (not NotAMechanismIndex) for the all-None dict.


def test_event_dict_refuses_the_all_none_shape_the_round_2_p1_reproduced():
    """The exact repro: every confirmed key present, every value None."""
    all_none = dict.fromkeys(_EVENT_RECORD_KEYS)
    with pytest.raises(ValueError, match="module_id"):
        _event_dict(all_none)


def test_events_refuses_the_all_none_shape_through_the_full_chain_too():
    chain = _chain(events={BLOCK: [dict.fromkeys(_EVENT_RECORD_KEYS)]})
    with pytest.raises(ChainUnavailable, match="module_id"):
        chain.events(BLOCK)


def test_emissions_pay_index_does_not_silently_swallow_the_all_none_shape():
    """The actual exploit chain the challenge traced, closed at its source:
    before this fix, _event_dict's output for the all-None dict reached
    emissions.py's _pay_index() as None != 'SubtensorModule', returning
    None (an honest 'not a pay event') rather than the refusal a corrupted
    record deserves. Now the corrupted shape never reaches _pay_index at
    all -- events() refuses it first."""
    from noah.chain.emissions import _pay_index

    all_none = dict.fromkeys(_EVENT_RECORD_KEYS)
    # _pay_index on its own still can't tell a corrupted record from an
    # honest unrelated one -- that was never its job. The fix is upstream:
    # events() must never hand it this shape in the first place.
    assert _pay_index(all_none) is None
    chain = _chain(events={BLOCK: [all_none]})
    with pytest.raises(ChainUnavailable, match="module_id"):
        chain.events(BLOCK)


@pytest.mark.parametrize(
    "module_id,event_id",
    [
        pytest.param(None, "Issued", id="module_id_none"),
        pytest.param("Balances", None, id="event_id_none"),
        pytest.param("", "Issued", id="module_id_empty_string"),
        pytest.param("Balances", "", id="event_id_empty_string"),
        pytest.param(5, "Issued", id="module_id_not_a_string"),
        pytest.param(b"Balances", "Issued", id="module_id_bytes_not_str"),
    ],
)
def test_event_dict_refuses_every_dishonest_module_or_event_id(module_id, event_id):
    """Not just None -- an empty string, a non-string, or bytes are equally
    unconfirmed shapes for a field that structurally can only ever be a
    real SCALE enum variant name (a non-empty string), never any of these."""
    malformed = dict(_REAL_BALANCES_ISSUED_EVENT, module_id=module_id, event_id=event_id)
    with pytest.raises(ValueError, match="module_id"):
        _event_dict(malformed)


def test_event_dict_still_accepts_the_confirmed_real_shape_with_the_new_check():
    """The tightened guard must not regress the two real fixtures every
    other test in this file already relies on."""
    assert _event_dict(_REAL_BALANCES_ISSUED_EVENT) == _REAL_BALANCES_ISSUED_EVENT
    assert _event_dict(_REAL_SYSTEM_EXTRINSIC_SUCCESS_EVENT) == _REAL_SYSTEM_EXTRINSIC_SUCCESS_EVENT


def test_event_dict_still_accepts_a_none_extrinsic_idx_the_honest_initialization_shape():
    """extrinsic_idx=None is legitimately honest for an Initialization-phase
    event (this file's own _REAL_BALANCES_ISSUED_EVENT fixture) -- the new
    module_id/event_id check must not have been generalized into refusing
    every None value, only the two fields that can never honestly be one."""
    assert _REAL_BALANCES_ISSUED_EVENT["extrinsic_idx"] is None
    assert _event_dict(_REAL_BALANCES_ISSUED_EVENT)["extrinsic_idx"] is None


# -- an empty list is "no answer": re-asked, then refused (ticket #399) -----


class _ScriptedConnection:
    """``get_events`` replays ``replies`` in order; counts both questions."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.hash_calls = 0
        self.event_calls = 0

    def get_block_hash(self, block):
        self.hash_calls += 1
        return _HASH

    def get_events(self, block_hash=None):
        self.event_calls += 1
        return self._replies[min(self.event_calls, len(self._replies)) - 1]


@pytest.fixture
def pauses(monkeypatch):
    seen = []
    monkeypatch.setattr("noah.chain.live._pause", seen.append)
    return seen


def _scripted(replies):
    connection = _ScriptedConnection(replies)
    return connection, LiveRunnerChain(connection, netuid=2, endpoint="wss://node.example")


def test_empty_then_real_list_is_returned_after_one_pause(pauses):
    connection, chain = _scripted([[], [_REAL_BALANCES_ISSUED_EVENT]])
    result = chain.events(BLOCK)
    assert result == [dict(_REAL_BALANCES_ISSUED_EVENT, ordinal_in_block=0)]
    assert connection.event_calls == 2
    assert len(pauses) == 1


def test_always_empty_refuses_naming_block_and_attempts(pauses):
    connection, chain = _scripted([[]])
    with pytest.raises(ChainUnavailable) as refused:
        chain.events(BLOCK)
    text = str(refused.value)
    assert str(BLOCK) in text
    assert "4 attempts" in text
    assert "length 0" in text
    assert "always has events" in text
    assert connection.event_calls == 4
    assert len(pauses) == 3


def test_normal_list_is_unchanged_and_asked_once(pauses):
    connection, chain = _scripted([[_REAL_BALANCES_ISSUED_EVENT, _REAL_BALANCES_ISSUED_EVENT]])
    result = chain.events(BLOCK)
    assert [e["ordinal_in_block"] for e in result] == [0, 1]
    assert connection.event_calls == 1
    assert pauses == []


def test_non_list_is_still_refused_without_retry(pauses):
    connection, chain = _scripted([None])
    with pytest.raises(ChainUnavailable, match="not a list of events"):
        chain.events(BLOCK)
    assert connection.event_calls == 1
    assert pauses == []


def test_block_hash_is_derived_once_across_retries(pauses):
    connection, chain = _scripted([[], [], [_REAL_BALANCES_ISSUED_EVENT]])
    chain.events(BLOCK)
    assert connection.event_calls == 3
    assert connection.hash_calls == 1
