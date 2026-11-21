"""UTC round windows. B is chosen and announced before any commitment."""

from __future__ import annotations

from dataclasses import dataclass

HOUR_MS = 3_600_000
COMMIT_WINDOW_MS = 2 * HOUR_MS
REVEAL_WINDOW_MS = 2 * HOUR_MS


@dataclass(frozen=True)
class RoundWindows:
    block_timestamp_ms: int
    announced_at_ms: int
    commit_window_ms: int = COMMIT_WINDOW_MS
    reveal_window_ms: int = REVEAL_WINDOW_MS

    @property
    def commit_open_ms(self) -> int:
        return self.announced_at_ms

    @property
    def commit_close_ms(self) -> int:
        return self.block_timestamp_ms + self.commit_window_ms

    @property
    def reveal_open_ms(self) -> int:
        return self.commit_close_ms

    @property
    def reveal_close_ms(self) -> int:
        return self.commit_close_ms + self.reveal_window_ms

    def announcement_too_late(self) -> bool:
        return self.announced_at_ms >= self.commit_close_ms


def classify_commit(timestamp_ms: int, windows: RoundWindows) -> str:
    if timestamp_ms < windows.commit_open_ms:
        return "EARLY_COMMIT"
    if timestamp_ms >= windows.commit_close_ms:
        return "LATE_COMMIT"
    return "COMMIT_OK"


def classify_reveal(timestamp_ms: int, windows: RoundWindows, *, committed_ok: bool) -> str:
    if not committed_ok:
        return "MISSING_COMMIT"
    if timestamp_ms < windows.reveal_open_ms:
        return "EARLY_REVEAL"
    if timestamp_ms >= windows.reveal_close_ms:
        return "LATE_REVEAL"
    return "REVEAL_OK"


def select_first_finalized_at_or_after(
    blocks: list[tuple[int, int, str]],
    *,
    midnight_ms: int,
    finalized_number: int,
) -> tuple[int, int, str] | None:
    """Smallest finalized number whose timestamp is at or after midnight.

    `blocks` is `(number, timestamp_ms, hash)`. Numbers above `finalized_number`
    are ignored. This does not read the chain head for data.
    """
    eligible = [
        item
        for item in blocks
        if item[0] <= finalized_number and item[1] >= midnight_ms
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda item: item[0])
