"""Block-wide deposit index. The payload field stays a u16.

The backing round contract calls this integer
`block_wide_event_index`. It is the position in the block event list, and the
event's phase must be ApplyExtrinsic of the deposit extrinsic. The count inside
that extrinsic alone is a different number.
"""

from __future__ import annotations


def phase_of(event: dict) -> tuple[str, int | None]:
    raw = event.get("phase")
    if isinstance(raw, str) and raw in {"ApplyExtrinsic", "apply_extrinsic"}:
        index = event.get("extrinsic_idx", event.get("extrinsic_index"))
        if index is None:
            return "other", None
        return "ApplyExtrinsic", int(index)
    if isinstance(raw, dict):
        for name in ("ApplyExtrinsic", "apply_extrinsic"):
            if name in raw:
                return "ApplyExtrinsic", int(raw[name])
    if isinstance(raw, (list, tuple)) and raw and raw[0] in {"ApplyExtrinsic", "apply_extrinsic"}:
        return "ApplyExtrinsic", int(raw[1])
    return "other", None


def event_id_of(event: dict) -> str | None:
    if "event_id" in event:
        return event["event_id"]
    inner = event.get("event")
    if isinstance(inner, dict):
        return inner.get("event_id")
    return None


def locate(events: list[dict], extrinsic_index: int, event_id: str) -> tuple[int, int] | None:
    """Return `(block_wide_index, transaction_local_index)` for the first match."""
    local = 0
    for block_index, event in enumerate(events):
        kind, index = phase_of(event)
        if kind != "ApplyExtrinsic" or index != extrinsic_index:
            continue
        if event_id_of(event) == event_id:
            return block_index, local
        local += 1
    return None
