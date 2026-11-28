"""One explicit Validate proxy around one report; no recursive wrapper trust."""

from .deposit_index import phase_of
from .keys import ss58_decode


def public_hex(value):
    try:
        if isinstance(value, dict) and set(value) == {"Id"}:
            value = value["Id"]
        if isinstance(value, str) and value.startswith("0x"):
            raw = bytes.fromhex(value[2:])
        else:
            raw = ss58_decode(value)
        return raw.hex() if len(raw) == 32 else None
    except (ValueError, TypeError, KeyError):
        return None


def arguments(call):
    args = call.get("call_args")
    if isinstance(args, dict):
        return args
    if isinstance(args, list) and all(
        isinstance(x, dict) and "name" in x and "value" in x for x in args
    ):
        if len({x["name"] for x in args}) == len(args):
            return {x["name"]: x["value"] for x in args}
    return {}


def proxy_report(value):
    """Return (inner call, effective hotkey, outer signer), or reject the shape."""
    outer = value.get("call") or {}
    if (outer.get("call_module"), outer.get("call_function")) != ("Proxy", "proxy"):
        return None
    args = arguments(outer)
    if set(args) != {"real", "force_proxy_type", "call"} or args["force_proxy_type"] != "Validate":
        return None
    inner = args["call"]
    if not isinstance(inner, dict) or (inner.get("call_module"), inner.get("call_function")) != (
        "Commitments",
        "set_commitment",
    ):
        return None
    params = arguments(inner)
    if set(params) != {"netuid", "info"} or type(params["netuid"]) is not int:
        return None
    real, delegate = public_hex(args["real"]), public_hex(value.get("address"))
    if not real or not delegate or real == delegate:
        return None
    return inner, real, delegate


def _attributes(event):
    attrs = event.get("attributes")
    if isinstance(attrs, dict):
        return attrs
    if isinstance(attrs, list) and all(
        isinstance(x, dict) and "name" in x and "value" in x for x in attrs
    ):
        if len({x["name"] for x in attrs}) == len(attrs):
            return {x["name"]: x["value"] for x in attrs}
    return {}


def proxy_success(events, index, real, netuid):
    """Bind inner success and the emitted hotkey/netuid to this extrinsic only."""
    selected = [e.get("event", e) for e in events if phase_of(e) == ("ApplyExtrinsic", index)]

    def named(module, name):
        return [e for e in selected if e.get("module_id") == module and e.get("event_id") == name]

    executed, commitments = named("Proxy", "ProxyExecuted"), named("Commitments", "Commitment")
    if (
        len(executed) != 1
        or len(commitments) != 1
        or len(named("System", "ExtrinsicSuccess")) != 1
        or named("System", "ExtrinsicFailed")
    ):
        return False
    result = _attributes(executed[0]).get("result")
    attrs = _attributes(commitments[0])
    return (
        isinstance(result, dict)
        and set(result) == {"Ok"}
        and result["Ok"] in ([], (), None)
        and type(attrs.get("netuid")) is int
        and attrs["netuid"] == netuid
        and public_hex(attrs.get("who")) == real
    )
