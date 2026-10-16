"""Strict nested-call test double; does not prove real SCALE encoding."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ComposedCall:
    original: object


class ComposesCalls:
    def compose(self, call):
        assert call.module == "SubtensorModule"
        return ComposedCall(call)


def inner_call(wrapper):
    inner = wrapper.params["call"]
    assert isinstance(inner, ComposedCall), "nested generated Call was not composed"
    return inner.original
