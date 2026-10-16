"""Shared synthetic inputs for public participant tests."""


class FakeOwnerSource:
    """Answers ``owner_at`` from a canned ``{(hotkey, block_hash): owner}``
    map -- identical shape to ``test_owner_entitlement.py``'s own
    ``FakeSource``, reused here rather than re-invented."""

    def __init__(self, answers):
        self._answers = answers
        self.asked = []

    def owner_at(self, *, hotkey, block_hash):
        self.asked.append((hotkey, block_hash))
        key = (hotkey, block_hash)
        if key not in self._answers:
            raise AssertionError(f"FakeOwnerSource was never told the answer for {key}")
        return self._answers[key]


__all__ = ["FakeOwnerSource"]
