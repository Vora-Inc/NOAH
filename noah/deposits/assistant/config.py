"""The miner's readable settings file (INI). No cron, no secrets.

The file names a wallet and hotkey by NAME or ADDRESS only. Any key that looks
like key material is refused on load, so a recovery phrase pasted here cannot be
read, logged or reused.
"""

import configparser
import math
import os
from dataclasses import dataclass

from ...chain.ss58 import NotAnAddress, public_key_of
from ...pathtext import plain_path_text
from .amounts import MODES, SUGGESTED_PERCENT, DepositRefused, parse_noah

_SECRETISH = ("mnemonic", "seed", "secret", "private", "password", "phrase", "recovery")

WRAPPED_DEPOSIT_NOTICE = (
    "A transfer made with transfer_stake_and_hotkey is attributed to your miner hotkey "
    "from the transfer itself, even when it is sent inside a proxy, multisig or batch. A plain "
    "transfer_stake is attributed only through a remark in a top-level batch_all; a plain "
    "transfer_stake with no remark, or one whose remark sits inside a proxy, multisig, "
    "Utility.batch or another wrapper where it is not read, names no miner hotkey, so the "
    "deposit is recorded as unattributed, with a reason, and credits nobody (the chain "
    "itself shows no error). This helper sends NOAH directly, using "
    "transfer_stake_and_hotkey (stake_position = miner) or a top-level batch_all that "
    "carries the remark (stake_position = validator), with optional proxy signing only for the miner position. The entry "
    "deposit must name your miner hotkey the first time; an unnamed attempt is recorded as "
    "unattributed and does not count as your entry."
)

STAKE_POSITION_HELP = (
    "stake_position = miner: your NOAH is staked to your own miner hotkey (where mining "
    "earnings land); the helper moves it to the Ark and re-delegates it to the Ark Validator in "
    "one step.\n"
    "stake_position = validator: your NOAH is already staked to the designated Ark Validator, and "
    "you own the miner hotkey with the same coldkey; the helper moves it to the Ark and names "
    "your miner hotkey in a note so the deposit is attributed to you."
)

EXAMPLE = """\
# Noah deposit assistant settings. Addresses and names only, never recovery words.
# Keep every comment on its own line; text after a value is read as part of the value.
#
# A transfer made with transfer_stake_and_hotkey is attributed to your miner hotkey from the
# transfer itself, even when it is sent inside a proxy, multisig or batch. A plain transfer_stake is
# attributed only through a remark in a top-level batch_all; a plain transfer_stake with no
# remark, or one whose remark sits inside a proxy, multisig, Utility.batch or another wrapper
# where it is not read, names no miner hotkey, so the deposit is recorded as unattributed, with
# a reason, and credits nobody (the chain itself shows no error). This helper sends NOAH
# directly, using transfer_stake_and_hotkey (stake_position = miner) or a top-level batch_all
# that carries the remark (stake_position = validator), with optional proxy signing only for the miner position. The entry
# deposit must name your miner hotkey the first time; an unnamed attempt is recorded as
# unattributed and does not count as your entry.

[miner]
network = private-test
# The chain node this tool talks to, as ws://host:port. The default ws://127.0.0.1:9944 means a
# node running on THIS computer. Point it at a node you run or trust: it supplies every number the
# tool reads, and the tool will sign what you approve based on those numbers.
# If it points at the wrong network, set genesis_hash below and the tool refuses to run.
endpoint = ws://127.0.0.1:9944
netuid = 2
# Your registered miner hotkey address.
miner_hotkey = MINER_HOTKEY_ADDRESS
# Your coldkey address (it owns the stake and signs).
coldkey = COLDKEY_ADDRESS
wallet_name = my-wallet
hotkey_name = my-miner-hotkey
# Optional: only if your wallets are NOT in the usual bittensor folder (~/.bittensor/wallets).
# wallet_path = /path/to/your/wallets
# Optional: the chain's block-0 hash. If set, the tool refuses to run on any other chain.
# genesis_hash = 0x...
# Where your NOAH is staked, and so which of the two direct forms is used:
#   miner (default): your NOAH is staked to your own miner hotkey, where mining earnings land.
#     The helper sends one transfer_stake_and_hotkey to the Ark and re-delegates it to the
#     Ark Validator in the same step. Choose this for ordinary earnings.
#   validator: your NOAH is already staked to the designated Ark Validator, and you own the
#     miner hotkey with the same coldkey. The helper sends a top-level batch_all with the Ark
#     transfer and a note naming your miner hotkey. Choose this only in that situation.
stake_position = miner

[ark]
# The official Ark.
ark_coldkey = ARK_ADDRESS
# The designated Ark Validator. Required. This is not your miner hotkey.
designated_validator_hotkey = ARK_VALIDATOR_HOTKEY_ADDRESS

[deposit]
# mode: custom | fixed | percent | maintenance
mode = percent
percent = 50
# amount_noah = 5
# cap_noah = 20
# minimum_noah = 0.5
# Kept back for fees and protected holdings.
reserve_noah = 1

[evidence]
# A JSON file of finalized earnings and credited contributions.
file = ./evidence.json

[state]
dir = ./noah-deposit-state
"""


@dataclass(frozen=True)
class AssistantConfig:
    network: str
    endpoint: str
    netuid: int
    miner_hotkey: str
    coldkey: str
    wallet_name: str
    hotkey_name: str
    ark_coldkey: str
    validator_hotkey: str
    mode: str
    percent_bp: int
    amount_rao: int
    cap_rao: int
    minimum_rao: int
    reserve_rao: int
    evidence_file: str
    state_dir: str
    stake_position: str  # "miner" (earnings on the miner hotkey) or "validator"
    genesis_hash: str = None
    wallet_path: str = None
    signing: dict = None


def _address(value, what):
    if not value or not value.strip():
        raise DepositRefused(f"{what} is missing from the settings file.", "config")
    try:
        public_key_of(value.strip())
    except NotAnAddress as exc:
        raise DepositRefused(f"{what} is not a valid chain address: {exc}", "config") from None
    return value.strip()


def _known_designated_validators(network, environ):
    """(label, address) pairs the repo or environment already carries for the designated
    Ark Validator. Nothing is hard-coded here: the mainnet value is read from the repo's own
    mainnet identity module, and the other from the validator's launch environment."""
    known = []
    env_value = environ.get("NOAH_VALIDATOR_HOTKEY")
    if env_value:
        known.append(("the NOAH_VALIDATOR_HOTKEY environment setting", env_value))
    if (network or "").strip().lower() in ("mainnet", "finney"):
        from ...core.mainnet import ARK_VALIDATOR_HOTKEY

        known.append(("the mainnet launch configuration", ARK_VALIDATOR_HOTKEY))
    return known


def _check_designated_validator(validator, network, environ):
    """Refuse a validator hotkey that differs from the designated Ark Validator on record.

    A deposit staked to any other validator is a real Ark receipt that earns no mining credit,
    and the read-back would agree with whatever was typed, so the typed value is checked here.
    """
    for label, expected in _known_designated_validators(network, environ):
        try:
            same = public_key_of(expected) == public_key_of(validator)
        except NotAnAddress:
            continue
        if not same:
            raise DepositRefused(
                f"The designated Ark Validator hotkey in your settings ({validator}) is not the "
                f"designated Ark Validator on record in {label} ({expected}). Deposits must be "
                f"delegated to the designated Ark Validator; a deposit to any other validator "
                f"earns no mining credit. Nothing was sent. Correct designated_validator_hotkey "
                f"and run again.",
                "wrong_validator",
            )


def load_config(path, environ=None):
    environ = os.environ if environ is None else environ
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    if not parser.read(path):
        raise DepositRefused(f"Settings file {plain_path_text(path)} could not be read.", "config")
    for section in parser.sections():
        for key in parser[section]:
            # These two fields identify a backend/region, never secret contents.
            # validate_signing below restricts their values to supported metadata.
            public_signer_metadata = section == 'signing' and key in ('secret_type',)
            if not public_signer_metadata and any(word in key.lower() for word in _SECRETISH):
                raise DepositRefused(
                    f"The settings file has a field named {key!r}. This tool never stores or "
                    f"reads key material. Remove it and keep recovery words out of every file.",
                    "secret_in_config",
                )

    def get(section, key, default=None):
        return parser.get(section, key, fallback=default)

    validator = get("ark", "designated_validator_hotkey") or environ.get("NOAH_VALIDATOR_HOTKEY")
    if not validator:
        raise DepositRefused(
            "The designated Ark Validator hotkey is not set (settings file or "
            "NOAH_VALIDATOR_HOTKEY). Without it the tool cannot tell whether a deposit would "
            "count, so it refuses to run.",
            "no_validator",
        )
    validator = _address(validator, "The designated Ark Validator hotkey")
    miner_hotkey = _address(get("miner", "miner_hotkey"), "The miner hotkey")
    _check_designated_validator(validator, get("miner", "network", "unspecified"), environ)
    if public_key_of(miner_hotkey) == public_key_of(validator):
        raise DepositRefused(
            "The miner hotkey and the designated Ark Validator hotkey are the same address. They "
            "are different things; check both.",
            "config",
        )
    mode = (get("deposit", "mode") or "").strip().lower()
    if mode not in MODES:
        raise DepositRefused(f"deposit mode must be one of {', '.join(MODES)}.", "config")

    def noah(key):
        raw = get("deposit", key)
        return None if raw is None or not raw.strip() else parse_noah(raw, key)

    percent_bp = None
    raw_percent = get("deposit", "percent")
    if raw_percent is not None and raw_percent.strip():
        try:
            value = float(raw_percent)
            if not math.isfinite(value):
                raise ValueError
            percent_bp = int(round(value * 100))
        except (ValueError, OverflowError):
            raise DepositRefused("deposit percent is not a usable number.", "config") from None
    try:
        netuid = int(get("miner", "netuid"))
    except (TypeError, ValueError):
        raise DepositRefused("netuid is missing or not a whole number.", "config") from None
    position = (get("miner", "stake_position", "miner") or "miner").strip().lower()
    if position not in ("miner", "validator"):
        raise DepositRefused("stake_position must be 'miner' or 'validator'.", "config")
    from .proxy_signing import validate_signing
    raw_signing = dict(parser['signing']) if parser.has_section('signing') else None
    if raw_signing and 'delay_blocks' in raw_signing:
        try:
            raw_signing['delay_blocks'] = int(raw_signing['delay_blocks'])
        except ValueError:
            raise DepositRefused('delay_blocks must be an integer', 'config') from None
    signing = validate_signing(raw_signing)
    if signing['mode'] == 'proxy':
        from .proxy_signing import same
        if same(signing['proxy_address'], get('miner', 'coldkey')):
            raise DepositRefused('proxy delegate must differ from real coldkey', 'config')
    if signing['mode'] == 'proxy' and position != 'miner':
        raise DepositRefused('proxy requires stake_position=miner for attribution', 'config')
    return AssistantConfig(
        signing=signing,
        network=get("miner", "network", "unspecified"),
        endpoint=get("miner", "endpoint"),
        netuid=netuid,
        miner_hotkey=miner_hotkey,
        coldkey=_address(get("miner", "coldkey"), "The coldkey address"),
        wallet_name=get("miner", "wallet_name"),
        hotkey_name=get("miner", "hotkey_name"),
        ark_coldkey=_address(get("ark", "ark_coldkey"), "The Ark address"),
        validator_hotkey=validator,
        mode=mode,
        percent_bp=percent_bp,
        amount_rao=noah("amount_noah"),
        cap_rao=noah("cap_noah"),
        minimum_rao=noah("minimum_noah") or 0,
        reserve_rao=noah("reserve_noah") or 0,
        evidence_file=get("evidence", "file"),
        state_dir=get("state", "dir", "./noah-deposit-state"),
        stake_position=position,
        genesis_hash=(get("miner", "genesis_hash") or None),
        wallet_path=(get("miner", "wallet_path") or None),
    )


__all__ = [
    "AssistantConfig",
    "load_config",
    "EXAMPLE",
    "SUGGESTED_PERCENT",
    "WRAPPED_DEPOSIT_NOTICE",
    "STAKE_POSITION_HELP",
]
