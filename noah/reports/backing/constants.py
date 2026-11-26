"""Pins and numeric limits from the slice-1 plan."""

SCHEMA_ID = "bfpoc-calc-v1"
VERSION = "0.1.0"

# Evidence/admissibility revision; the three committed integer answers and
# signed calculation identity are unchanged. Legacy is retained replay only.
COVERAGE_LEGACY = "alpha-out-v1"
COVERAGE_STAKED_V2 = "total-alpha-staked-v2"
STAKED_COVERAGE_SPECS = frozenset({483, 492, 493, 494, 495, 497})

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

# Runtime 490 historically admits by spec. Other supported runtimes require a code pin.
SUPPORTED_SPECS = frozenset({490})
SUBTENSOR_PIN = "d967d13588834f4f5aaa8ef4c8fc1a865d581462"
SUBTENSOR_491_PIN = "be3a11b1a5300bb58d6fbad3c629e4a926bb2f44"
SUBTENSOR_492_PIN = "541a50d38764acdce23578bd8ba54f80156ffb2a"
SUBTENSOR_493_PIN = "8b407e40988ea7a251abb6dbc07a9ab7d602b740"
SUBTENSOR_494_PIN = "a68c6e8c65411a842fbf69269dc385aa13041391"
SUBTENSOR_495_PIN = "e536e6bf4bfa58f1ec4506dff0147e1f9b1c1900"
SUBTENSOR_497_PIN = "24e1fc7b12ea7ce9d390925dac602ba961997824"

# Reviewed runtime 483 adapter, restricted to the official release code.
# Other chains/code with that spec number are not implicitly compatible.
RUNTIME_483_CODE_HASH = "0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2"
# Official v491 WASM matched by both Mainnet readers. Relevant runtime API,
# storage and arithmetic contracts were reviewed against the retained v490 pin.
RUNTIME_491_CODE_HASH = "0x4201278656a17aff924ca24123c8e5866d569a84a7150bb0372bc7078fc6173c"
# Official v492 WASM matched both Mainnet readers and the reviewed v491
# report storage, runtime API and call shapes.
RUNTIME_492_CODE_HASH = "0x22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e"
# Official v493 preserves the reviewed report storage, Swap/Commitments
# arithmetic and calls; exact live WASM independently matched the release.
RUNTIME_493_CODE_HASH = "0x51adea39b16a2b9d60b58d430246d49312f1111b74607edc1166e47a73de4591"
RUNTIME_494_CODE_HASH = "0x1735bc231789bead3ab3edfa583f3c9a95aaf7643adcc906ea224a7761a303ca"
# Official v495 tag e536e6bf. Live Finney code read at the finalized tip on 2026-11-24.
RUNTIME_495_CODE_HASH = "0x7f4f60f27f1cbdb92a8413d8280217789229f54189e48a501ada2d25f4c7edf7"
# Official v497 WASM matches the finalized Finney snapshot. The v495-to-v497
# runtime changes preserve the Commitments call and report storage/API contracts.
RUNTIME_497_CODE_HASH = "0x1ef826aebed0d77300b7da09d4e3b23ad507317d72c8c01d827537643dbf2c15"
RUNTIME_CODE_PINS = {
    483: RUNTIME_483_CODE_HASH,
    491: RUNTIME_491_CODE_HASH,
    492: RUNTIME_492_CODE_HASH,
    493: RUNTIME_493_CODE_HASH,
    494: RUNTIME_494_CODE_HASH,
    495: RUNTIME_495_CODE_HASH,
    497: RUNTIME_497_CODE_HASH,
}

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
