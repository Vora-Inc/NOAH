"""Pins and numeric limits from the slice-1 plan."""

SCHEMA_ID = "bfpoc-calc-v1"
VERSION = "0.1.0"

# Public Ark coldkey. SS58 prefix 42. Not a secret.
ARK_SS58 = "5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3"
ARK_PUBKEY_HEX = "0x64257dfaf373a54793ed9f72be4df3e74044322c653fad72448252c5f5f52ec9"

RAO_STEP = 1_000_000
RAO_PER_ALPHA = 1_000_000_000
DROP_PPM_SCALE = 1_000_000
PERCENT_DIVISOR = 10_000

# Qualifying tests on raw U64F64 bits: P_after * den <= P_B * num.
THRESHOLD_10 = (9, 10)  # 10% drop
THRESHOLD_20 = (4, 5)  # 20% drop

SUPPORTED_SPECS = frozenset({490})
SUBTENSOR_PIN = "d967d13588834f4f5aaa8ef4c8fc1a865d581462"

# Reviewed runtime 483 adapter, restricted to the official release code.
# Other chains/code with that spec number are not implicitly compatible.
RUNTIME_483_CODE_HASH = "0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2"

MINIMUM_RESERVE = 1_000_000
MAX_INPUT_MULTIPLIER = 1000
PERQUINTILL_ONE = 1_000_000_000_000_000_000
FEE_DENOMINATOR = 65_535

MECHANISM_DYNAMIC = 1
SUBNET_STATE_PENDING_DISSOLUTION = 2
SUBNET_STATE_DISSOLVING = 3

AVAILABILITY_BATCH = 64
KEY_PAGE_SIZE = 1000
SIM_BUDGET_PER_THRESHOLD = 96

RPC_TIMEOUT_S = 30.0
RPC_RETRY_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0)
RUN_BUDGET_S = 20 * 60

SS58_PREFIX = 42

ARK_NOTE = (
    "The runtime `available` figure only says the chain's unstake check "
    "(`ensure_available_to_unstake`) would not block this amount. It does not "
    "show that anyone can, may or will authorize a sale from the sealed Ark account. "
    "The Ark is excluded from Output 1 by NOAH policy, not by a chain lock."
)
ARK_LABEL = (
    "Ark: technically sellable under the runtime availability rule "
    "(shown for context; excluded from Output 1 by NOAH policy)"
)
USER_LEG_NOTE = "user leg only; excludes the block-author fee settlement"

# Runtime API and storage names at subtensor d967d135.
PALLET_SUBTENSOR = "SubtensorModule"
PALLET_SWAP = "Swap"
PALLET_TIMESTAMP = "Timestamp"

RPC_ALLOWLIST = frozenset(
    {
        "chain_getFinalizedHead",
        "chain_getBlock",
        "chain_getBlockHash",
        "chain_getHeader",
        "state_getRuntimeVersion",
        "state_getStorage",
        "state_getStorageHash",
        "state_getKeysPaged",
        "state_call",
    }
)
STATE_METHODS = frozenset(
    {
        "state_getRuntimeVersion",
        "state_getStorage",
        "state_getStorageHash",
        "state_getKeysPaged",
        "state_call",
    }
)
