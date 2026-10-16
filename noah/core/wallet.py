"""Load the configured signer and restrict which calls it may sign.

The validator uses the configured hotkey or dedicated proxy signer. Restricted
wallet wrappers enforce the permitted publication call shape. Loading a signer is
separate from reading chain state; never include its recovery material in logs,
configuration examples or release artifacts."""

from .settings import SettingsError

# Every instruction this validator may ever compose. Two entries now, and a
# test holds the list to exactly this known pair (`test_wallet.py`'s own
# TheAllowlist.test_the_allowlist_holds_exactly_the_known_two_entries), so
# growing it further stays the deliberate act somebody has to argue for --
# see this module's own docstring, above, for the argument already made for
# the second one.
PERMITTED_CALLS = frozenset(
    {
        ("SubtensorModule", "commit_timelocked_mechanism_weights"),
        # The direct, open, non-timelocked equivalent -- the mechanism-aware
        # "plain set_weights" this validator's own reveal-round math already
        # keys everything to (mecid, not the legacy whole-subnet call of the
        # same shape without one). Composed ONLY by
        # `Publisher.submit_direct`, never by `Publisher.submit`. See this
        # module's own docstring, "The direct weight path, now permitted".
        ("SubtensorModule", "set_mechanism_weights"),
    }
)


class SigningKeyRefused(SettingsError):
    """The signing key could not be loaded, so nothing is signed."""


class CallNotPermitted(SettingsError):
    """An instruction outside the short list this key is allowed to compose."""


def load_hotkey(wallet):
    """Return the signing key from a wallet, or refuse.

    Never quotes the wallet back in a refusal: the object may hold key material
    and a refusal usually ends up in a log.
    """
    name = getattr(wallet, "name", "the configured wallet")
    try:
        hotkey = wallet.hotkey
    except Exception as error:
        raise SigningKeyRefused(
            f"the wallet {name!r} would not give up its signing key "
            f"({type(error).__name__}). Nothing is signed. Check the wallet exists "
            f"and is readable by the account running this validator."
        ) from error

    if hotkey is None:
        raise SigningKeyRefused(
            f"the wallet {name!r} has no hotkey, which is the key that signs on the "
            f"network. Nothing is signed."
        )
    return hotkey


def permitted(module, call, arguments=None):
    """Refuse any instruction outside the allowlist, by name.

    ``arguments`` is accepted so callers can pass a whole instruction without
    unpacking it, and is deliberately never included in the refusal: an
    instruction's arguments carry addresses and amounts, and a refusal usually
    ends up in a log.
    """
    if (module, call) in PERMITTED_CALLS:
        return
    allowed = ", ".join(f"{m}.{c}" for m, c in sorted(PERMITTED_CALLS))
    raise CallNotPermitted(
        f"this validator may not compose {module}.{call}. It is allowed exactly one "
        f"instruction, {allowed}, and everything else is refused rather than checked "
        f"against a list of things to avoid. If this call is genuinely needed, the "
        f"allowlist is the place to argue for it, in the open."
    )


# The ONE wrapper the proxy publication route may compose (noah/core/proxy_route.py). It is
# not a member of PERMITTED_CALLS above: a free entry would let any permitted-looking call
# be wrapped around anything. It is allowed only as the OUTER call of an inner call that is
# itself in PERMITTED_CALLS, and only for the configured commit identity as ``real``.
# ``Proxy.add_proxy``, ``remove_proxy``, ``proxy_announced``, ``create_pure`` and every other
# call stay refused by name.
PROXY_WRAPPER = ("Proxy", "proxy")


def permitted_proxy_wrapper(real, expected_real, inner_module, inner_call):
    """Refuse a proxy wrapper unless it wraps exactly one allowlisted inner call on behalf
    of exactly the commit identity. ``real`` and ``expected_real`` are compared as
    addresses by public key when possible (text otherwise)."""
    from ..chain.ss58 import NotAnAddress, public_key_of

    permitted(inner_module, inner_call)
    try:
        same = public_key_of(real) == public_key_of(expected_real)
    except (NotAnAddress, TypeError, ValueError):
        same = real == expected_real
    if not same:
        raise CallNotPermitted(
            "this validator may wrap a commit in Proxy.proxy only on behalf of its own "
            "commit identity; the wrapper named a different account. Nothing is signed."
        )

