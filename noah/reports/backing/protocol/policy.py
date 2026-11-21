"""Protocol constants. Release identity is supplied by the trusted round."""

import hashlib

MAINNET_GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
ENGINE_PIN = "76f629a3933967170229a732c4b02740be22e5eb"


def release_prefix(release_id: str) -> bytes:
    return hashlib.sha256(release_id.encode("utf-8")).digest()[:16]
