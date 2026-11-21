"""Split one finished miner vector. This does not score and does not read a chain.

``0`` returns the same object. ``9800`` assigns 64,224 of 65,535 units to one
destination. Each miner with a positive pre-burn weight keeps at least one
submitted unit, paid from the remaining 1,311, and the leftover units follow
the miners' pre-burn weights. An empty or all-zero vector is returned
unchanged, so no burn-only vector is created. Sending nothing leaves whatever
weights are already on chain.
"""

U16_MAX = 65535
OFF = 0
ENABLED = 9800


class BurnAllocationRefused(ValueError):
    """The split is not the approved 0 or 9800 allocation."""


def allocate(vector, bps, destination_uid=None):
    """Return the publication vector. ``bps == 0`` returns ``vector`` itself."""
    if bps not in (OFF, ENABLED):
        raise BurnAllocationRefused("burn allocation accepts only 0 or 9800 basis points")
    if type(vector) is not dict:
        raise BurnAllocationRefused("burn allocation requires a uid map")
    if bps == OFF or not any(vector.values()):
        return vector
    if type(destination_uid) is not int or isinstance(destination_uid, bool) or not 0 <= destination_uid <= U16_MAX:
        raise BurnAllocationRefused("burn destination uid is missing")
    _u16_map(vector)
    if vector.get(destination_uid, 0):
        raise BurnAllocationRefused("burn destination already carries miner weight")
    positive = {uid: weight for uid, weight in vector.items() if weight}
    if not positive:
        return vector
    burn_units = U16_MAX * ENABLED // 10000
    miner_units = U16_MAX - burn_units
    if len(positive) > miner_units:
        raise BurnAllocationRefused(
            "miner allocation cannot cover one submitted unit for each positive miner"
        )
    # One reserved unit each, then largest-remainder on what the 2% has left.
    distributable = miner_units - len(positive)
    total = sum(positive.values())
    order = sorted(positive)
    base = {}
    remainder = {}
    for uid in order:
        product = positive[uid] * distributable
        base[uid] = product // total
        remainder[uid] = product % total
    residual = distributable - sum(base.values())
    for uid in sorted(order, key=lambda item: (-remainder[item], item))[:residual]:
        base[uid] += 1
    published = {uid: 0 for uid in vector}
    for uid, weight in base.items():
        published[uid] = weight + 1
    published[destination_uid] = burn_units
    if sum(published.values()) != U16_MAX:
        raise BurnAllocationRefused("burn split does not sum to 65535")
    return published


def require_chain_limits(vector, *, max_weight_limit, min_allowed_weights, epoch_consensus, recycle_or_burn):
    """Refuse a configuration that would change the intended ratio on chain.

    The runtime max-upscales before applying ``MaxWeightsLimit``. The comparison
    uses that stored vector, from the same prediction finalized readback uses.
    """
    if epoch_consensus != "Yuma":
        raise BurnAllocationRefused("burn allocation requires Yuma consensus")
    if recycle_or_burn != "Burn":
        raise BurnAllocationRefused("burn allocation requires explicit or proven effective Burn")
    if type(max_weight_limit) is not int or isinstance(max_weight_limit, bool) or not 0 <= max_weight_limit <= U16_MAX:
        raise BurnAllocationRefused("MaxWeightsLimit is not a u16")
    if type(min_allowed_weights) is not int or isinstance(min_allowed_weights, bool) or min_allowed_weights < 0:
        raise BurnAllocationRefused("min allowed weights is not a count")
    _u16_map(vector)
    if len(vector) < min_allowed_weights:
        raise BurnAllocationRefused("vector is shorter than min allowed weights")
    if not vector:
        return
    from .rescale import predict_stored
    stored = predict_stored([vector[uid] for uid in sorted(vector)]).stored
    total = sum(stored)
    if total and max(stored) * U16_MAX > max_weight_limit * total:
        raise BurnAllocationRefused("allocation exceeds MaxWeightsLimit")


def modeled_incentive(rows, stakes, *, kappa_num=1, kappa_den=2):
    """Stake-weighted clipped incentive. A model, not a chain observation.

    Each row is normalized by its own sum. Consensus for a uid is the highest
    row weight supported by at least ``kappa`` of stake. Incentive is the
    stake-weighted clipped weight. Bonds are not applied.
    """
    if len(rows) != len(stakes) or not rows:
        raise BurnAllocationRefused("modeled rows and stakes differ")
    if type(kappa_num) is not int or type(kappa_den) is not int or kappa_den <= 0 or not 0 <= kappa_num <= kappa_den:
        raise BurnAllocationRefused("modeled kappa is not a fraction")
    normalized = []
    for row, stake in zip(rows, stakes):
        if type(stake) is not int or isinstance(stake, bool) or stake <= 0:
            raise BurnAllocationRefused("modeled stake must be a positive integer")
        _u16_map(row)
        total = sum(row.values())
        if total <= 0:
            raise BurnAllocationRefused("modeled row has no weight")
        normalized.append({uid: weight * 10**18 // total for uid, weight in row.items()})
    uids = sorted({uid for row in normalized for uid in row})
    total_stake = sum(stakes)
    needed = (total_stake * kappa_num + kappa_den - 1) // kappa_den
    consensus = {}
    for uid in uids:
        levels = sorted({row.get(uid, 0) for row in normalized}, reverse=True)
        consensus[uid] = 0
        for level in levels:
            support = sum(stake for row, stake in zip(normalized, stakes) if row.get(uid, 0) >= level)
            if support >= needed:
                consensus[uid] = level
                break
    clipped = [{uid: min(row.get(uid, 0), consensus[uid]) for uid in uids} for row in normalized]
    ranks = {uid: sum(row[uid] * stake for row, stake in zip(clipped, stakes)) for uid in uids}
    return ranks


def _u16_map(vector):
    for uid, weight in vector.items():
        if type(uid) is not int or isinstance(uid, bool) or not 0 <= uid <= U16_MAX:
            raise BurnAllocationRefused("burn uid is not a u16")
        if type(weight) is not int or isinstance(weight, bool) or not 0 <= weight <= U16_MAX:
            raise BurnAllocationRefused("burn weight is not a u16")
