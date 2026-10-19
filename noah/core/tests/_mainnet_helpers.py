"""Shared synthetic inputs for public participant tests."""

import json
import re
from pathlib import Path

from noah.core import mainnet as mn
from noah.core.chain import MAINNET

DECIDED_NAME = "mainnet-production-v1"

PROFILE_FILE = next(
    p / "docs/parameters/profiles/mainnet-production-v1.json"
    for p in Path(__file__).resolve().parents
    if (p / "docs/parameters/profiles/mainnet-production-v1.json").is_file()
)


def write_profile(directory, version=DECIDED_NAME, **over):
    name = re.sub("[^A-Za-z0-9]", "_", version) + ".json"
    data = json.loads(PROFILE_FILE.read_text())
    data["profileVersion"] = version
    data.update(over)
    path = Path(directory) / name
    path.write_text(json.dumps(data))
    return str(path)


OWNER_HOTKEY = "5Hn6PHyjx2rLY3EXCP1xK7XHUquRo17fyuyM1HuZrAWb8y3G"

TEST_PROXY = "5ErAdwYih4PrXEHqibWGvw6YVk4iKNgdtbVstLYUNupe9kJ9"


def good_mapping():
    return {
        "NOAH_NETWORK": "finney",
        "NOAH_NETUID": "82",
        "NOAH_MAINNET_ENABLE": MAINNET,
        "NOAH_CHAIN_ENDPOINT": "ws://127.0.0.1:23946",
        "NOAH_ENDPOINT_2": "wss://entrypoint-finney.opentensor.ai:443",
        "NOAH_PUBLISH_MODE": "sealed",
        "NOAH_WALLET_NAME": "noah-scoring",
        "NOAH_WALLET_HOTKEY": "scoring",
        "NOAH_EXPECTED_SIGNER_ADDRESS": OWNER_HOTKEY,
        "NOAH_PROXY_SIGNER_ADDRESS": TEST_PROXY,
        "NOAH_ARK_ADDRESS": mn.ARK_ADDRESS,
        "NOAH_VALIDATOR_HOTKEY": mn.ARK_VALIDATOR_HOTKEY,
        "NOAH_ENTRY_DEPOSIT_RAO": "10000000000",
        "NOAH_LOG_LEVEL": "info",
        "NOAH_SCORE_INTERVAL_BLOCKS": "360",
        "NOAH_WEIGHT_INTERVAL_BLOCKS": "360",
        "NOAH_SETTLE_BLOCKS": "20",
        "NOAH_REQUIRE_TWIN_PROOF": "true",
        "NOAH_HOLDING_STRIDE_BLOCKS": "300",
    }


__all__ = ["TEST_PROXY", "good_mapping", "write_profile"]
