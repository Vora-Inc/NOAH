"""Explicit mainnet startup clearance for the configured deployment.

Require the mainnet genesis opt-in, subnet and parameter profile, distinct read
endpoints, designated Ark identities, explicit publication mode and the restricted proxy
route. This implementation also pins the scoring identity to SIGNER_HOTKEY; it
does not currently support arbitrary independent mainnet validator identities.
Public receiving/delegation identities and the validator's signing identity serve
different purposes and must not be conflated."""

from __future__ import annotations

import dataclasses

from ..chain.sources import canonical_endpoint, scrub_endpoint_text
from ..chain.ss58 import NotAnAddress, public_key_of
from .chain import MAINNET, ChainNotIdentified, MainnetClearance, WrongChain, identify, _issue_clearance
from .settings import SettingsError

MAINNET_NETUID = 82

ARK_ADDRESS = "5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3"
ARK_VALIDATOR_HOTKEY = "5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV"
SIGNER_HOTKEY = "5Hn6PHyjx2rLY3EXCP1xK7XHUquRo17fyuyM1HuZrAWb8y3G"
ARK_VALIDATOR_MULTISIG = "5Gj3HZJdLC1hASrn32NR4mttimJ8UPb63ftJATUraNMuWohX"
OWNER_MULTISIG = "5GGrRdUatgDLiJxnytdWfYCwsAb1Cyo4U5Dw1HmLiAcbWj88"

DEV_ADDRESSES = {
    "Alice": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "Bob": "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "Charlie": "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y",
    "Dave": "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy",
    "Eve": "5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw",
    "Ferdie": "5CiPPseXPECbkjWCa6MnjNokrgYjMqmKndv2rSnekmSK2DjL",
}

PLACEHOLDER = "REPLACE-ME"
PROFILE_PREFIX = "mainnet-production-"


class WrongSigner(SettingsError):
    """The loaded signing hotkey is not the configured public address."""


class MainnetRefused(WrongChain):
    """Mainnet was asked for and one or more required conditions do not hold."""


def redact_endpoint(url):
    """An endpoint URL safe to print: scheme, host and port only.

    Hosted RPC providers put the API key in the userinfo, the path or the query
    (``wss://user:pw@host``, ``wss://host/v2/<key>``, ``?apikey=...``). Messages here
    reach logs and issue comments, so anything past the host is replaced.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
        host = parts.hostname
        if not parts.scheme or not host:
            return "<unparseable endpoint>"
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        hidden = "/<hidden>" if (parts.path not in ("", "/") or parts.query or parts.username) else ""
        return f"{parts.scheme}://{host}{port}{hidden}"
    except ValueError:
        return "<unparseable endpoint>"


def _scrub(text, endpoint):
    """``text`` with the raw endpoint string replaced by its redacted form."""
    redacted = str(text).replace(str(endpoint), redact_endpoint(endpoint))
    return scrub_endpoint_text(redacted, endpoint)


class RpcGenesis:
    """Ask a configured endpoint for chain_getBlockHash(0) to verify genesis identity."""

    def __init__(self, endpoint, timeout=15):
        self.endpoint = endpoint
        self.timeout = timeout

    def genesis_hash(self):
        import json

        try:
            from websockets.sync.client import connect

            with connect(self.endpoint, open_timeout=self.timeout, close_timeout=5) as ws:
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "chain_getBlockHash", "params": [0]}))
                reply = json.loads(ws.recv(timeout=self.timeout))
        except Exception as error:  # any failure to ask is "could not identify", never a pass
            raise ChainNotIdentified(
                f"could not ask {redact_endpoint(self.endpoint)} for its genesis "
                f"({type(error).__name__}: {_scrub(error, self.endpoint)})."
            ) from None
        return reply.get("result")


def _same_key(a, b):
    try:
        return public_key_of(a) == public_key_of(b)
    except (NotAnAddress, TypeError, ValueError):
        return a == b


def _is_dev(address):
    return [name for name, dev in DEV_ADDRESSES.items() if _same_key(address, dev)]


def _profile_problems(settings, env):
    from ..scoring.parameters import ParametersError, load_parameters

    try:
        params = load_parameters(env)
    except ParametersError as error:
        return [f"the parameters are not a loadable, decided profile: {error}"], None
    version = params.profile_version
    problems = []
    if not version.startswith(PROFILE_PREFIX):
        problems.append(
            f"the parameters profile {version!r} is not a mainnet profile "
            f"(profileVersion must start with {PROFILE_PREFIX!r}); the private-test profile "
            f"cannot run mainnet."
        )
    return problems, params


def setting_problems(settings, env=None):
    """Every unmet condition that needs no connection. A list of plain sentences."""
    problems = []
    if settings.network != "finney":
        problems.append(f"NOAH_NETWORK is {settings.network!r}, not 'finney'.")
    if settings.mainnet_enable != MAINNET:
        problems.append(
            "NOAH_MAINNET_ENABLE is not set to mainnet's exact genesis hash "
            f"({MAINNET}); it is a different value (not shown). This is the deliberate switch: "
            "a boolean could be copied between env files by accident, this hash cannot."
        )
    if settings.netuid != MAINNET_NETUID:
        problems.append(f"NOAH_NETUID is {settings.netuid}, not {MAINNET_NETUID}.")

    profile_problems, params = _profile_problems(settings, env)
    problems.extend(profile_problems)

    if not settings.chain_endpoint:
        problems.append("NOAH_CHAIN_ENDPOINT is not set (mainnet needs two named endpoints).")
    if not settings.second_chain_endpoint:
        problems.append("NOAH_ENDPOINT_2 is not set (mainnet needs two named endpoints).")
    if settings.chain_endpoint and settings.second_chain_endpoint:
        if canonical_endpoint(settings.chain_endpoint) == canonical_endpoint(
            settings.second_chain_endpoint
        ):
            problems.append(
                "NOAH_CHAIN_ENDPOINT and NOAH_ENDPOINT_2 are the same endpoint under two "
                "spellings; they must be two independent nodes."
            )

    # Designated Ark Validator hotkey.
    vh = settings.designated_validator_hotkey
    if not vh:
        problems.append("NOAH_VALIDATOR_HOTKEY is not set.")
    else:
        if not _same_key(vh, ARK_VALIDATOR_HOTKEY):
            problems.append(
                f"NOAH_VALIDATOR_HOTKEY is not the designated Ark Validator hotkey "
                f"{ARK_VALIDATOR_HOTKEY}."
            )
        for name in _is_dev(vh):
            problems.append(f"NOAH_VALIDATOR_HOTKEY is the well-known dev address //{name}.")
        for label, other in (
            ("the scoring/commit hotkey", SIGNER_HOTKEY),
            ("the Ark Validator multisig coldkey", ARK_VALIDATOR_MULTISIG),
            ("the SN82 owner coldkey", OWNER_MULTISIG),
            ("the Ark receiving coldkey", ARK_ADDRESS),
        ):
            if _same_key(vh, other):
                problems.append(f"NOAH_VALIDATOR_HOTKEY equals {label}; it must be distinct.")

    # Ark address and entry deposit.
    ark = settings.ark_address
    if not ark:
        problems.append("NOAH_ARK_ADDRESS is not set.")
    else:
        if not _same_key(ark, ARK_ADDRESS):
            problems.append(f"NOAH_ARK_ADDRESS is not the Ark receiving coldkey {ARK_ADDRESS}.")
        for name in _is_dev(ark):
            problems.append(f"NOAH_ARK_ADDRESS is the well-known dev address //{name}.")
    if settings.entry_deposit_rao is None:
        problems.append("NOAH_ENTRY_DEPOSIT_RAO is not set.")
    elif params is not None and settings.entry_deposit_rao != params.entry_deposit_per_hotkey_rao:
        problems.append(
            f"NOAH_ENTRY_DEPOSIT_RAO is {settings.entry_deposit_rao} but the profile's "
            f"entryDepositPerHotkeyRao is {params.entry_deposit_per_hotkey_rao}."
        )

    signer = settings.expected_signer_address
    if not signer:
        problems.append("NOAH_EXPECTED_SIGNER_ADDRESS is not set.")
    else:
        if not _same_key(signer, SIGNER_HOTKEY):
            problems.append(
                f"NOAH_EXPECTED_SIGNER_ADDRESS is not the scoring/commit hotkey "
                f"{SIGNER_HOTKEY}."
            )
        for name in _is_dev(signer):
            problems.append(f"NOAH_EXPECTED_SIGNER_ADDRESS is the well-known dev address //{name}.")
    proxy_signer = settings.proxy_signer_address
    if not proxy_signer:
        problems.append(
            "NOAH_PROXY_SIGNER_ADDRESS is not set. Mainnet publishes through the proxy route "
            "(the validator's own key is never on the server)."
        )
    else:
        if signer and _same_key(proxy_signer, signer):
            problems.append("NOAH_PROXY_SIGNER_ADDRESS equals NOAH_EXPECTED_SIGNER_ADDRESS; they must differ.")
        if _same_key(proxy_signer, SIGNER_HOTKEY):
            problems.append("NOAH_PROXY_SIGNER_ADDRESS is the validator's own hotkey; it must be a dedicated key.")
        for name in _is_dev(proxy_signer):
            problems.append(f"NOAH_PROXY_SIGNER_ADDRESS is the well-known dev address //{name}.")

    for field in dataclasses.fields(settings):
        value = getattr(settings, field.name)
        if isinstance(value, str) and PLACEHOLDER in value:
            problems.append(f"a setting ({field.name}) still holds the placeholder {PLACEHOLDER!r}.")
    return problems


def endpoint_problems(answers):
    """``answers``: list of (setting name, endpoint, genesis-or-None, error-or-None)."""
    problems = []
    distinct = {canonical_endpoint(ep) for _, ep, _, _ in answers}
    if len(answers) >= 2 and len(distinct) < 2:
        problems.append("fewer than two independent endpoints were reached.")
    for name, endpoint, genesis, error in answers:
        if error is not None:
            problems.append(f"{name} ({redact_endpoint(endpoint)}) could not be identified: {_scrub(error, endpoint)}")
        elif genesis != MAINNET:
            problems.append(
                f"{name} ({redact_endpoint(endpoint)}) reports genesis {genesis}, not mainnet's {MAINNET}."
            )
    return problems


def refusal(problems):
    return MainnetRefused(
        f"MAINNET REFUSED. {len(problems)} required condition(s) not met, so this "
        f"validator will not start on mainnet:\n  - " + "\n  - ".join(problems)
    )


def clear_mainnet(settings, connections, env=None):
    """Return a :class:`MainnetClearance` or raise :class:`MainnetRefused` naming every gap.

    ``connections``: list of (setting name, endpoint, connection) for NOAH_CHAIN_ENDPOINT,
    NOAH_ENDPOINT_2 and, when set, NOAH_SUBMIT_ENDPOINT. Each is asked for its genesis.
    """
    problems = setting_problems(settings, env)
    answers = []
    for name, endpoint, connection in connections:
        try:
            genesis, _ = identify(connection)
            answers.append((name, endpoint, genesis, None))
        except ChainNotIdentified as error:
            answers.append((name, endpoint, None, error))
    if len({n for n, _, _, _ in answers} & {"NOAH_CHAIN_ENDPOINT", "NOAH_ENDPOINT_2"}) < 2:
        problems.append("both NOAH_CHAIN_ENDPOINT and NOAH_ENDPOINT_2 must be asked for their genesis.")
    problems.extend(endpoint_problems(answers))
    if problems:
        raise refusal(problems)
    return _issue_clearance(MAINNET)


def check_signer_hotkey(settings, loaded_hotkey_ss58):
    """Require the loaded signer to match the configured signing role.

    In proxy mode the signer must differ from the commit identity. Mainnet additionally
    pins that commit identity to SIGNER_HOTKEY and requires the proxy route."""
    from .proxy_route import check_proxy_signing_identity, proxy_mode

    expected = settings.expected_signer_address
    expected = expected if isinstance(expected, str) and expected else None
    if proxy_mode(settings):
        try:
            check_proxy_signing_identity(settings, loaded_hotkey_ss58)
        except SettingsError as error:
            if settings.network == "finney":
                raise MainnetRefused(str(error)) from error
            raise
        if settings.network == "finney":
            if not expected or not _same_key(expected, SIGNER_HOTKEY):
                raise MainnetRefused(
                    f"NOAH_EXPECTED_SIGNER_ADDRESS (the value is not shown) is not the address "
                    f"mainnet publishes as, {SIGNER_HOTKEY} (the scoring/commit hotkey). "
                    f"Nothing was signed."
                )
            proxy = settings.proxy_signer_address
            if _is_dev(proxy) or _same_key(proxy, SIGNER_HOTKEY):
                raise MainnetRefused(
                    "NOAH_PROXY_SIGNER_ADDRESS is a well-known dev address or the "
                    "validator itself. Nothing was signed."
                )
        return
    if settings.network == "finney":
        raise MainnetRefused(
            f"mainnet publishes through the proxy route only: set NOAH_PROXY_SIGNER_ADDRESS to "
            f"the dedicated signer's public address. The validator's own key {SIGNER_HOTKEY} is "
            f"never loaded on a server (the loaded hotkey here is {loaded_hotkey_ss58}). "
            f"Nothing was signed."
        )
    elif expected and not _same_key(loaded_hotkey_ss58, expected):
        raise WrongSigner(
            f"the loaded wallet hotkey is {loaded_hotkey_ss58}, but NOAH_EXPECTED_SIGNER_ADDRESS "
            f"is a different address (the value is not shown). Nothing was signed."
        )


__all__ = [
    "MainnetClearance",
    "MainnetRefused",
    "SettingsError",
    "clear_mainnet",
    "check_signer_hotkey",
    "WrongSigner",
    "refusal",
    "setting_problems",
    "endpoint_problems",
]
