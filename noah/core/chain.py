"""Identify a chain by its genesis hash and enforce the configured chain policy.

Network labels and endpoint names are not identity evidence. Unknown or mismatched
genesis values are refused before operations that depend on chain identity."""

from .reader import _transport_failure_types
from .settings import SettingsError

LOCAL = "0x7f0279c35dd5d9f9f0bb7ca10e2fd0c5d405028fc52fe42b026ad94e038a51a5"
TESTNET = "0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105"
MAINNET = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"

KNOWN_CHAINS = {LOCAL: "LOCAL", TESTNET: "TESTNET", MAINNET: "MAINNET"}

# The live network is refused by name UNLESS the caller presents a
# MainnetClearance, which only ``noah.core.mainnet.clear_mainnet`` issues, and
# only after every condition of the explicit mainnet opt-in holds (network,
# genesis-hash switch, decided profile, two independent endpoints both reporting
# the mainnet genesis, netuid 82, designated hotkeys, sealed mode ...; see that
# module). With no clearance the behaviour is exactly the absolute refusal this
# constant used to be. Listed as a name so no caller can target it by changing
# what it asks for.
FORBIDDEN = frozenset({"MAINNET"})

_ISSUER = object()


class MainnetClearance:
    """Proof that every mainnet opt-in condition held. Not constructible by accident:
    only ``_issue_clearance`` (called from ``noah.core.mainnet``) makes one."""

    __slots__ = ("genesis",)

    def __init__(self, genesis, *, _token=None):
        if _token is not _ISSUER:
            raise TypeError("a MainnetClearance is issued by noah.core.mainnet.clear_mainnet only")
        object.__setattr__(self, "genesis", genesis)

    def __setattr__(self, name, value):
        raise AttributeError("a MainnetClearance is immutable")


def _issue_clearance(genesis):
    return MainnetClearance(genesis, _token=_ISSUER)


class ChainNotIdentified(SettingsError):
    """The endpoint could not be identified at all, which is not the same as unknown."""


class WrongChain(SettingsError):
    """The endpoint is a chain, and not the one this validator was told to use."""


def identify(connection):
    """Return the genesis hash and recognized label from the endpoint, or refuse."""
    try:
        genesis = connection.genesis_hash()
    except _transport_failure_types() as error:  # a real failure to identify is a refusal
        raise ChainNotIdentified(
            f"could not identify the chain at all "
            f"({type(error).__name__}; the error text is not shown). "
            f"Refusing rather than continuing against something unidentified."
        ) from None

    if not isinstance(genesis, str) or not genesis.startswith("0x") or len(genesis) != 66:
        raise ChainNotIdentified(
            f"the endpoint answered {genesis!r} when asked for its first block, which is "
            f"not a genesis hash. An empty or malformed answer is refused rather than "
            f"treated as a pass."
        )
    return genesis, KNOWN_CHAINS.get(genesis, "UNKNOWN")


def require_chain(connection, expected, *, local_genesis=None, clearance=None):
    """Refuse unless the connection really is the chain named by `expected`.

    Returns the genesis hash so every run's evidence can carry the identity of
    the chain it acted on.

    ``local_genesis`` lets an operator name the local chain they expect, because
    a local chain gets a new identity every time it is rebuilt and a built-in
    constant would need a code change on every restart. It applies ONLY to
    LOCAL: the live network and the public test network have fixed identities
    and configuration cannot move them.

    The whole risk in that convenience is widening the guard, so three things
    hold regardless of what is configured. The live network is refused by name
    even if somebody configures its fingerprint as their local chain. Asking for
    LOCAL with nothing configured and nothing recorded refuses rather than
    accepting whatever answers. And a configured value is an ADDITIONAL
    permitted identity for LOCAL, never a replacement for any other check.
    """
    if expected not in set(KNOWN_CHAINS.values()):
        raise ValueError(
            f"unknown expectation {expected!r}; use one of {sorted(set(KNOWN_CHAINS.values()))}"
        )
    cleared = (
        isinstance(clearance, MainnetClearance)
        and clearance.genesis == MAINNET
        and expected == "MAINNET"
    )
    if expected in FORBIDDEN and not cleared:
        raise WrongChain(
            f"refusing to target {expected}. Nothing this validator runs may touch the "
            f"live network, so no caller may ask for it, whatever reason is given."
        )

    # A configured local chain is checked for being the live network BEFORE the
    # endpoint is consulted, so a pasted live fingerprint cannot be laundered
    # into a permitted value by the configuration route.
    if local_genesis is not None and KNOWN_CHAINS.get(local_genesis) in FORBIDDEN:
        raise WrongChain(
            f"the configured local chain is {KNOWN_CHAINS[local_genesis]}. Configuration "
            f"cannot make the live network reachable, whatever it is called in a settings "
            f"file. Nothing this validator runs may touch it."
        )

    genesis, label = identify(connection)

    if label in FORBIDDEN and not (cleared and label == expected):
        raise WrongChain(
            f"STOP. This endpoint is {label} (genesis {genesis}), and the validator asked "
            f"for {expected}. Nothing here may touch the live network. Check what is bound "
            f"to that port: a node serving the live network has held the expected local "
            f"port on this machine before, and answered plausibly."
        )
    if expected == "LOCAL" and label == "UNKNOWN":
        if local_genesis is None:
            raise WrongChain(
                f"this endpoint has genesis {genesis}, which is not the local chain this "
                f"validator has recorded, and nothing says which local chain to expect. A "
                f"local chain gets a new identity every rebuild, so state the one you mean "
                f"rather than leaving it open: an unstated expectation must refuse, because "
                f"the alternative is accepting whatever happens to answer."
            )
        if genesis != local_genesis:
            raise WrongChain(
                f"this endpoint has genesis {genesis} and the configured local chain is "
                f"{local_genesis}. A local chain is rebuilt often, so this usually means the "
                f"chain was rebuilt and the setting was not updated. Check which is stale "
                f"before changing either."
            )
        return genesis

    if label == "UNKNOWN":
        raise WrongChain(
            f"this endpoint has genesis {genesis}, which is not a chain this validator "
            f"knows, and {expected} was expected. An unrecognised chain is not a "
            f"permitted chain."
        )
    if label != expected:
        raise WrongChain(
            f"this endpoint is {label} (genesis {genesis}), and the validator asked for {expected}."
        )
    return genesis
