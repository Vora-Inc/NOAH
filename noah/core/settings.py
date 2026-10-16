"""Reading the validator's settings, and refusing the ones that only look fine.

Settings are read once, at startup, before anything connects to a chain. The
job here is not to be helpful about bad input. It is to stop the validator
starting at all when a setting is wrong, because every alternative to stopping
is worse: a validator that guesses publishes weights, and a published weight
vector cannot be taken back.

Three refusals carry most of the weight, and none of them is about a value
being absent. Absence is easy. These are about values that a lenient reader
would accept and a human would misread.

**The subnet number has no safe default.** Zero is not a placeholder on this
chain, it is the root subnet. Substituting it for an unset value would aim a
weight vector at an entirely different subnet, and nothing about the run would
look unusual. So an unset subnet number stops the validator by name.

**A safety flag written in good faith can read as off.** The twin-proof gate is
what stops the validator publishing when the two scoring implementations
disagree. Compared with the string "true", a file saying `True` reads as false
and the gate is gone, with the operator believing they switched it on. Only
known spellings are accepted, in any case, and anything else is refused rather
than treated as off.

**A typo in a setting's NAME is the same hazard as a typo in its value.** A line
reading `NOAH_REQUIRE_TWIN_PROF=false` sets nothing, leaves the real gate at
whatever it would otherwise be, and produces a run identical to one where the
line is absent. Unknown settings beginning with our own prefix are refused, with
a suggestion of the name that was probably meant.

No key material passes through here, ever. These settings hold the NAME of a
wallet and the NAME of a hotkey inside it, which is all that is needed to ask
the wallet software to sign. A mnemonic, a seed or a private key never appears
in this module, in the example file, or in any log line the validator writes.
"""

import difflib
import os
import re
import stat
from dataclasses import dataclass, fields
from typing import Optional

PREFIX = "NOAH_"

NETWORKS = ("finney", "test", "local")
LOG_LEVELS = ("debug", "info", "warning", "error")

# Sealed publication is the default. Direct publication is an explicit mode
# for a chain configured without commit/reveal; startup policy gates still apply.
PUBLISH_MODES = ("sealed", "direct")
PUBLISH_MODE_DEFAULT = "sealed"

# Only these two, in any case. Accepting "yes", "on" or "1" as well would be
# leniency bought for nothing: it widens what the file can say without making
# any wrong value safe, and this module's whole argument is that a settings
# reader should refuse what it was not given rather than interpret it.
TRUE_SPELLINGS = ("true",)
FALSE_SPELLINGS = ("false",)

# Digits and nothing else. Python's int() would read "2_0" as 20 and "  20  "
# as 20, neither of which a person scanning the file would see, so the shape is
# checked before the conversion.
WHOLE_NUMBER = re.compile(r"^[0-9]+$")

KEYS = (
    "NOAH_WALLET_NAME",
    "NOAH_WALLET_HOTKEY",
    "NOAH_NETWORK",
    "NOAH_NETUID",
    "NOAH_CHAIN_ENDPOINT",
    "NOAH_LOG_LEVEL",
    "NOAH_SCORE_INTERVAL_BLOCKS",
    "NOAH_WEIGHT_INTERVAL_BLOCKS",
    "NOAH_SETTLE_BLOCKS",
    "NOAH_REQUIRE_TWIN_PROOF",
    "NOAH_LOCAL_GENESIS",
    # The second, independent chain endpoint the real chain adapter
    # (noah/core/chain_adapter.py, #436) needs to build a dual-sourced
    # AgreeingChain. The identical name neurons/miner.py's own
    # SECOND_ENDPOINT_VAR already reads for the census's sources_agree
    # check -- this is the same physical second connection, not a second,
    # separately-configured value for it. Optional here: unset means "no
    # second source configured", and the adapter refuses to build a
    # single-sourced read path rather than silently accepting one.
    "NOAH_ENDPOINT_2",
    # Optional: the path of a file holding the value NOAH_ENDPOINT_2 would
    # carry, for an endpoint whose URL contains an access key. Set this or
    # NOAH_ENDPOINT_2, never both.
    "NOAH_ENDPOINT_2_FILE",
    # The four settings issue #566 wires into build_chain_adapter, each
    # optional here for the identical reason NOAH_ENDPOINT_2 is: the gate,
    # a dry run and survey() all need to work with none of these set, and
    # build_chain_adapter is where an absence becomes a real, named refusal
    # (ChainAdapterRefused, itself a SettingsError) rather than a startup
    # crash. All four reuse an EXISTING name from neurons/miner.py wherever
    # one already exists for the identical physical fact, rather than
    # inventing a second, separately-configured value for it -- the same
    # reasoning NOAH_ENDPOINT_2 above already states.
    #
    # The Ark's own coldkey -- neurons/miner.py's ARK_ADDRESS_VAR, unchanged.
    "NOAH_ARK_ADDRESS",
    # The designated Ark Validator's own hotkey (issue #282 item 4/4a,
    # #302) -- the SAME name scripts/daily_deposit_service.py's own
    # _resolve_validator_hotkey already reads for the identical physical
    # fact on the writer/submission side. Optional here, like
    # NOAH_ARK_ADDRESS: noah.core.chain_adapter.build_chain_adapter fails
    # closed by name when a mainnet deployment has none configured, rather
    # than this module refusing a legitimate local/test run that never
    # touches deposits.
    "NOAH_VALIDATOR_HOTKEY",
    # A JSON file carrying this subnet's real scoring dials -- neurons/
    # miner.py's DIALS_JSON_VAR, unchanged. See noah.core.chain_adapter's
    # own _real_dials (ported from, and kept in sync with, miner.py's
    # function of the same name -- see that function's own docstring for
    # why it is ported rather than imported) for exactly what shape this
    # file must be and where its ruled values come from
    # (the selected parameter profile).
    "NOAH_DIALS_JSON",
    # The settled M1-revised parameters profile (ticket #112): a JSON file
    # carrying the entry-deposit threshold and ongoing seat-holding floor,
    # among other scoring dials -- noah.scoring.parameters.load_parameters(),
    # the SINGLE canonical reader of this file, reads it directly via
    # os.environ, never through this Settings mapping (see that module's own
    # docstring, and noah.config.profile.entry_deposit_rao()/
    # ongoing_hold_rao(), which delegate to it and are what
    # noah.deposits.entry_gate/noah.deposits.d143/noah.core.chain_adapter's
    # own EntryDepositAmountMismatched cross-check actually call). Listed
    # here only so a real, documented line in .env.validator is never
    # refused as an unknown/typo'd NOAH_ setting -- the identical reason
    # NOAH_RUNTIME_ROOT below is listed but not a Settings dataclass field.
    "NOAH_PARAMETERS_JSON",
    # Where this validator's own local history store lives. Reuses
    # neurons/miner.py's own HISTORY_DB_VAR name for the override, but NOT
    # its default path: the validator gets its own default, under a
    # different base directory, so a miner and a validator running on the
    # same machine for the same Ark do not silently open and write the
    # SAME sqlite file from two independent processes unless an operator
    # explicitly names one path for both (see
    # noah.core.chain_adapter._default_validator_history_db_path's own
    # docstring).
    "NOAH_HISTORY_DB",
    # How many blocks apart _sample_holdings's own stride reads a wallet's
    # position. NOT a chain fact and not read from parameters.yaml: no
    # ruled value exists for it anywhere in this project, and
    # noah/chain/store.py's own module docstring says so directly --
    # "[the holding requirement,] over what stretch of time, is not
    # settled." An operator-configured value, refused when absent rather
    # than defaulted to a number invented here -- see
    # noah.core.chain_adapter.HoldingStrideNotConfigured's own docstring.
    "NOAH_HOLDING_STRIDE_BLOCKS",
    # The ruled, flat entry-deposit amount [P-020: "500 NOAH held to
    # register; 100 of it becomes the first Ark deposit"], in rao. The
    # identical "not a chain fact, not hardcoded, not read from
    # parameters.yaml (a different repository from the one this code ships
    # in)" reasoning NOAH_DIALS_JSON's own comment above already gives --
    # noah.core.chain_adapter._entry_deposits_for uses it to recognise
    # which of a coldkey's real, observed NOAH deposits IS its entry
    # deposit, never to invent one that was not observed (#570). Optional
    # here for the identical reason every #566 setting above is: the gate,
    # a dry run and survey() all need to work with it unset, and
    # build_chain_adapter is where an absence becomes a real, named
    # refusal (EntryDepositAmountNotWired) rather than a startup crash.
    "NOAH_ENTRY_DEPOSIT_RAO",
    # The evidence writer reads this override directly from the environment.
    # Configure durable storage explicitly rather than relying on checkout depth.
    "NOAH_RUNTIME_ROOT",
    # Optional application event sink, read directly from the process environment.
    "NOAH_EVENT_LOG",
    # Issue #334: where the per-day sealed-commit ledger lives (default:
    # commit-ledger.json beside loop.json). Known here, not a Settings field;
    # read via os.environ.get by neurons/validator.py, like NOAH_RUNTIME_ROOT.
    "NOAH_COMMIT_LEDGER_PATH",
    # Ticket #16 (B06), issue #56 comment 5720306237: which publish path
    # run_period uses -- "sealed" (default, unchanged) or "direct" (this
    # explicit non-production mode; see PUBLISH_MODES above). Optional here for the identical reason NOAH_HISTORY_DB is:
    # unset means "sealed", the existing default behavior, never a startup
    # refusal. A REQUIRED, explicit choice would defeat the point -- every
    # existing deployment that has never heard of this setting must keep
    # publishing exactly as it does today.
    "NOAH_PUBLISH_MODE",
    # Mainnet only (ticket #293 mainnet startup). Unset on every other network.
    # NOAH_MAINNET_ENABLE: must equal mainnet's exact genesis hash to lift the
    # chain guard's refusal (noah.core.mainnet). NOAH_SUBMIT_ENDPOINT: an
    # optional separate endpoint the signed commit is SUBMITTED through, when the
    # read endpoints do not accept author_submitExtrinsic. NOAH_EXPECTED_SIGNER_ADDRESS:
    # the public ss58 the loaded wallet hotkey must equal (never key material).
    "NOAH_MAINNET_ENABLE",
    "NOAH_SUBMIT_ENDPOINT",
    "NOAH_EXPECTED_SIGNER_ADDRESS",
    # Proxy route (noah.core.proxy_route): the public address of the DEDICATED proxy signer
    # whose key the unit loads. When set, NOAH_EXPECTED_SIGNER_ADDRESS is the validator's own
    # (commit identity) address and its key is never loaded. Public addresses only.
    "NOAH_PROXY_SIGNER_ADDRESS",
)


class SettingsError(Exception):
    """Something about the settings stops the validator starting."""


class MissingSetting(SettingsError):
    """A setting the validator cannot invent a value for."""


class BadSetting(SettingsError):
    """A setting whose value is present and cannot be used."""


class UnknownSetting(SettingsError):
    """A setting name the validator does not know, usually a typo."""


def _shown(value):
    """What an error message says in place of an operator-typed value: nothing of it.

    A value typed into the wrong setting may be an endpoint URL with an access key, in any shape
    (bad scheme, no scheme, spaces, a bare key), so a refusal names the setting and the reason only.
    """
    return "(the value is not shown)"


def _shown_name(key):
    return key if re.fullmatch(r"[A-Za-z0-9_]+", key) else "(a name that is not shown)"


def parse_env_file(text):
    """Read a settings file into a plain mapping.

    Deliberately small. It understands comments, blank lines, an `export`
    prefix and surrounding quotes, and it refuses anything else rather than
    inventing a meaning for it.
    """
    parsed = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise BadSetting(
                f"line {number} of the settings file has no '=' in it (the line is not shown). "
                f"Every setting is written as NAME=value."
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in parsed:
            raise BadSetting(
                f"{_shown_name(key)} is set twice in the settings file, and line {number} is the "
                f"second. Taking the last one silently is how a file can say one thing "
                f"at the top and mean another at the bottom. Delete one of them."
            )
        parsed[key] = value
    return parsed


def _present(mapping, key):
    value = mapping.get(key, "").strip()
    if not value:
        raise MissingSetting(
            f"{key} is not set, and the validator will not start without it. "
            f"See .env.validator.example for what it means."
        )
    return value


MAX_ENDPOINT_FILE_BYTES = 64 * 1024


def _second_endpoint(mapping):
    """The second chain endpoint, from NOAH_ENDPOINT_2 or the file named by NOAH_ENDPOINT_2_FILE.

    The file's content may hold an access key, so no message here ever
    includes any of it: only the setting names, the path and the kind of failure.
    """
    direct = mapping.get("NOAH_ENDPOINT_2", "").strip()
    path = mapping.get("NOAH_ENDPOINT_2_FILE", "").strip()
    if not path:
        return direct
    if direct:
        raise BadSetting(
            "NOAH_ENDPOINT_2 and NOAH_ENDPOINT_2_FILE are both set. Set only one: "
            "the second endpoint comes from either the setting or the file, never both."
        )

    def refuse(reason):
        # The path is operator-typed and may be the URL itself (in any shape), so it is repeated
        # only when it is plainly an absolute filesystem path.
        from ..scoring.parameters import plain_path_text

        shown = plain_path_text(path)
        return BadSetting(f"NOAH_ENDPOINT_2_FILE names {shown}, which {reason}.")

    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            # A FIFO or a device would block or never end: only a regular file is read.
            raise IsADirectoryError
        if info.st_size > MAX_ENDPOINT_FILE_BYTES:
            raise refuse(f"is larger than {MAX_ENDPOINT_FILE_BYTES} bytes (too large)") from None
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ENDPOINT_FILE_BYTES + 1)
    except FileNotFoundError:
        raise refuse("does not exist (missing)") from None
    except (IsADirectoryError, NotADirectoryError):
        raise refuse("is not a file") from None
    except PermissionError:
        raise refuse("cannot be read (unreadable: permission denied)") from None
    except (OSError, ValueError):
        raise refuse("cannot be read (unreadable)") from None
    if len(raw) > MAX_ENDPOINT_FILE_BYTES:
        raise refuse(f"is larger than {MAX_ENDPOINT_FILE_BYTES} bytes (too large)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise refuse("is not valid UTF-8 text (not text)") from None
    value = text.strip()
    if not value:
        raise refuse("is empty")
    return value


def _whole_number(mapping, key, *, minimum):
    value = _present(mapping, key)
    if not WHOLE_NUMBER.match(value):
        raise BadSetting(
            f"{key} must be a whole number written in digits, and it is {_shown(value)}. "
            f"Decimal points, underscores and signs are refused rather than rounded "
            f"or reinterpreted."
        )
    number = int(value)
    if number < minimum:
        raise BadSetting(f"{key} must be at least {minimum}, and it is {number}.")
    return number


def _one_of(mapping, key, allowed):
    value = _present(mapping, key).lower()
    if value not in allowed:
        raise BadSetting(f"{key} must be one of {', '.join(allowed)}, and it is {_shown(value)}.")
    return value


def _one_of_or_default(mapping, key, allowed, default):
    """Like ``_one_of``, except unset means ``default`` rather than refusing
    to start.

    ``_one_of``'s callers (``NOAH_NETWORK``, ``NOAH_LOG_LEVEL``) have no safe
    unset meaning, so absence is refused exactly like a bad value. This
    setting is the opposite case: unset MUST mean the literal behavior this
    validator already has today (see PUBLISH_MODE_DEFAULT's own comment), so
    absence is not an error -- but a PRESENT, misspelled value still is,
    identically to ``_one_of``. Silently reading a typo as the default would
    be the exact hazard this module's own docstring names for
    NOAH_REQUIRE_TWIN_PROOF: a setting that looks configured and is not.
    """
    raw = mapping.get(key, "").strip()
    if not raw:
        return default
    value = raw.lower()
    if value not in allowed:
        raise BadSetting(f"{key} must be one of {', '.join(allowed)}, and it is {_shown(value)}.")
    return value


def _flag(mapping, key):
    value = _present(mapping, key).lower()
    if value in TRUE_SPELLINGS:
        return True
    if value in FALSE_SPELLINGS:
        return False
    raise BadSetting(
        f"{key} must be true or false, and it is {_shown(value)}. It is not guessed at, "
        f"because a safety gate that reads an unrecognised value as false would "
        f"switch itself off while the person who wrote the line believed they had "
        f"switched it on. Accepted: {', '.join(TRUE_SPELLINGS + FALSE_SPELLINGS)}."
    )


def _refuse_unknown(mapping):
    for key in mapping:
        if not key.startswith(PREFIX) or key in KEYS:
            continue
        suggestions = difflib.get_close_matches(key, KEYS, n=1, cutoff=0.6)
        hint = f" Did you mean {suggestions[0]}?" if suggestions else ""
        raise UnknownSetting(
            f"{_shown_name(key)} is not a setting this validator knows.{hint} A misspelled name "
            f"sets nothing and leaves the real setting at whatever it would otherwise "
            f"be, so it is refused rather than ignored."
        )


@dataclass(frozen=True)
class Settings:
    """Everything the validator needs to know before it connects to anything."""

    wallet_name: str
    wallet_hotkey: str
    network: str
    netuid: int
    chain_endpoint: Optional[str]
    second_chain_endpoint: Optional[str]
    log_level: str
    score_interval_blocks: int
    weight_interval_blocks: int
    settle_blocks: int
    require_twin_proof: bool
    local_genesis: Optional[str]
    ark_address: Optional[str]
    designated_validator_hotkey: Optional[str]
    dials_json: Optional[str]
    history_db: Optional[str]
    holding_stride_blocks: Optional[int]
    entry_deposit_rao: Optional[int]
    publish_mode: str
    mainnet_enable: Optional[str] = None
    submit_endpoint: Optional[str] = None
    expected_signer_address: Optional[str] = None
    proxy_signer_address: Optional[str] = None

    def __repr__(self):
        # Endpoint fields show their label only: a hosted RPC URL may carry an API key.
        from ..chain.sources import endpoint_label

        shown = ", ".join(
            f"{f.name}="
            + (
                repr(endpoint_label(getattr(self, f.name)))
                if f.name.endswith("endpoint") and getattr(self, f.name)
                else repr(getattr(self, f.name))
            )
            for f in fields(self)
        )
        return f"Settings({shown})"

    @classmethod
    def from_mapping(cls, mapping):
        """Validate a mapping of settings, refusing anything not understood."""
        # Unknown names first: a typo reported as a typo is more use than the
        # same typo reported as the correct setting being absent.
        _refuse_unknown(mapping)

        if not mapping.get("NOAH_NETUID", "").strip():
            raise MissingSetting(
                "NOAH_NETUID is not set. There is no default: subnet 0 is the chain's "
                "root subnet, not an unset value, so guessing it would aim this "
                "validator's weights at the wrong subnet entirely and the run would "
                "look ordinary. Set it to the subnet this validator runs on."
            )

        endpoint = mapping.get("NOAH_CHAIN_ENDPOINT", "").strip()
        second_endpoint = _second_endpoint(mapping)

        stride_raw = mapping.get("NOAH_HOLDING_STRIDE_BLOCKS", "").strip()
        if stride_raw and not WHOLE_NUMBER.match(stride_raw):
            raise BadSetting(
                f"NOAH_HOLDING_STRIDE_BLOCKS must be a whole number written in digits, and "
                f"it is {_shown(stride_raw)}. Decimal points, underscores and signs are refused "
                f"rather than rounded or reinterpreted."
            )
        holding_stride_blocks = int(stride_raw) if stride_raw else None
        if holding_stride_blocks is not None and holding_stride_blocks < 1:
            raise BadSetting(
                f"NOAH_HOLDING_STRIDE_BLOCKS must be at least 1, and it is {holding_stride_blocks}."
            )

        entry_deposit_raw = mapping.get("NOAH_ENTRY_DEPOSIT_RAO", "").strip()
        if entry_deposit_raw and not WHOLE_NUMBER.match(entry_deposit_raw):
            raise BadSetting(
                f"NOAH_ENTRY_DEPOSIT_RAO must be a whole number written in digits, and it "
                f"is {_shown(entry_deposit_raw)}. Decimal points, underscores and signs are "
                f"refused rather than rounded or reinterpreted."
            )
        entry_deposit_rao = int(entry_deposit_raw) if entry_deposit_raw else None
        if entry_deposit_rao is not None and entry_deposit_rao < 1:
            raise BadSetting(
                f"NOAH_ENTRY_DEPOSIT_RAO must be at least 1, and it is {entry_deposit_rao}."
            )

        return cls(
            wallet_name=_present(mapping, "NOAH_WALLET_NAME"),
            wallet_hotkey=_present(mapping, "NOAH_WALLET_HOTKEY"),
            network=_one_of(mapping, "NOAH_NETWORK", NETWORKS),
            netuid=_whole_number(mapping, "NOAH_NETUID", minimum=0),
            chain_endpoint=endpoint or None,
            second_chain_endpoint=second_endpoint or None,
            log_level=_one_of(mapping, "NOAH_LOG_LEVEL", LOG_LEVELS),
            score_interval_blocks=_whole_number(mapping, "NOAH_SCORE_INTERVAL_BLOCKS", minimum=1),
            weight_interval_blocks=_whole_number(mapping, "NOAH_WEIGHT_INTERVAL_BLOCKS", minimum=1),
            settle_blocks=_whole_number(mapping, "NOAH_SETTLE_BLOCKS", minimum=0),
            require_twin_proof=_flag(mapping, "NOAH_REQUIRE_TWIN_PROOF"),
            # Optional, and only meaningful for a local chain, which gets a new
            # identity every rebuild. Empty means "use the recorded one".
            local_genesis=mapping.get("NOAH_LOCAL_GENESIS", "").strip() or None,
            ark_address=mapping.get("NOAH_ARK_ADDRESS", "").strip() or None,
            # Issue #282 item 4/4a, #302: the SAME real address
            # scripts/daily_deposit_service.py's own _resolve_validator_hotkey
            # already reads under this identical name for the WRITER side
            # (submission always targets it by construction) -- one name for
            # one physical fact, read here for the READER/scoring side's own
            # qualification check (noah.chain.deposits.deposits_in_block's
            # designated_validator_hotkey). Optional here, like ark_address:
            # environment-specific, and a mainnet deployment with this unset
            # fails closed where it is actually consumed
            # (noah.core.chain_adapter.build_chain_adapter), never here --
            # this module reads settings, it does not know which network a
            # given call is for beyond `network` itself, and local/test
            # tooling that never touches deposits legitimately has no need
            # to set it.
            designated_validator_hotkey=mapping.get("NOAH_VALIDATOR_HOTKEY", "").strip() or None,
            dials_json=mapping.get("NOAH_DIALS_JSON", "").strip() or None,
            history_db=mapping.get("NOAH_HISTORY_DB", "").strip() or None,
            holding_stride_blocks=holding_stride_blocks,
            entry_deposit_rao=entry_deposit_rao,
            publish_mode=_one_of_or_default(
                mapping, "NOAH_PUBLISH_MODE", PUBLISH_MODES, PUBLISH_MODE_DEFAULT
            ),
            mainnet_enable=mapping.get("NOAH_MAINNET_ENABLE", "").strip() or None,
            submit_endpoint=mapping.get("NOAH_SUBMIT_ENDPOINT", "").strip() or None,
            expected_signer_address=mapping.get("NOAH_EXPECTED_SIGNER_ADDRESS", "").strip()
            or None,
            proxy_signer_address=mapping.get("NOAH_PROXY_SIGNER_ADDRESS", "").strip() or None,
        )
