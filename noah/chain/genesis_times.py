"""Known block-one timestamps for chains with a fixed genesis identity.

These constants supply a day anchor when old chain state is unavailable. Match the
genesis identity before using a timestamp; never infer it from an endpoint name."""

MAINNET_GENESIS_HASH = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
MAINNET_BLOCK_ONE_TIME = 1679338080

BLOCK_ONE_TIME_BY_GENESIS_HASH = {
    MAINNET_GENESIS_HASH: MAINNET_BLOCK_ONE_TIME,
}


def fixed_block_one_time(genesis_hash):
    """The verified block-1 time for this genesis hash, or ``None`` if the chain
    is not one whose time is fixed here."""
    if not isinstance(genesis_hash, str):
        return None
    return BLOCK_ONE_TIME_BY_GENESIS_HASH.get(genesis_hash.strip().lower())
