"""Map emitted incentive vectors onto validated seat identities.

Require a dense seat map with the declared length and no duplicate hotkeys before
interpreting vector indexes as UIDs. A short map can indicate either changed chain
state or an incomplete read; neither is silently accepted. Decode packed subnet
and mechanism indexes and require all declared mechanisms to be accounted for."""

from .sources import Refusal


class NotAVector(Refusal):
    """A pay vector that is not a list of plain, decoded, non-negative integers."""


class SeatsNotContiguous(Refusal):
    """The seat map is not one hotkey each over 0..n-1, so a position is not a seat."""


class SeatsDoNotMatchVector(Refusal):
    """The chain's own seat count and the length of its pay vector disagree."""


def seats_from_vector(vector, *, hotkey_by_uid, subnetwork_n, what):
    """Turn a positional pay vector into ``{seat: amount}``, or refuse.

    ``subnetwork_n`` is the chain's own declared seat count, read at the same
    block as everything else. It is the outside fact that makes the position
    readable: without it, a vector of length 255 and a map of 255 seats agree
    with each other perfectly while both being one seat short of the truth.
    """
    if isinstance(subnetwork_n, bool) or not isinstance(subnetwork_n, int) or subnetwork_n < 0:
        raise SeatsDoNotMatchVector(
            f"the declared seat count for {what} came back as {subnetwork_n!r}, which is not a "
            f"whole number of seats. It is the only outside check on what a position means, "
            f"so a reader that cannot read it cannot read the vector either."
        )

    if not isinstance(vector, (list, tuple)):
        raise NotAVector(
            f"the {what} vector came back as {vector!r}, which is not a list of amounts. An "
            f"undecoded vector is refused rather than interpreted here, because every way of "
            f"interpreting it produces numbers rather than an error."
        )

    seats = _seat_numbers(hotkey_by_uid, what)
    expected = list(range(subnetwork_n))

    if seats != expected:
        missing = [seat for seat in expected if seat not in set(seats)]
        extra = [seat for seat in seats if seat >= subnetwork_n]
        raise SeatsNotContiguous(
            f"the seat map behind {what} is not one hotkey each over seats 0 to "
            f"{subnetwork_n - 1}. Missing: {missing or 'none'}. Beyond the count: "
            f"{extra or 'none'}. Held {len(seats)} seats for a declared {subnetwork_n}. "
            f"A position in the vector can only be read as a seat number while this map is "
            f"exactly the declared range, because the chain sorts by seat and then drops it. "
            f"With a gap, every amount after the gap belongs to the next participant along "
            f"and nothing about the numbers says so."
        )

    if len(vector) != subnetwork_n:
        raise SeatsDoNotMatchVector(
            f"the {what} vector holds {len(vector)} amounts for a declared {subnetwork_n} "
            f"seats. The chain built this vector from the same map it counts, so a length "
            f"that disagrees means one of the two reads is not describing this block."
        )

    return {seat: _amount(vector[seat], seat, what) for seat in expected}


def _seat_numbers(hotkey_by_uid, what):
    """The seats the map holds, sorted, or a refusal.

    Two seats sharing a hotkey is refused here rather than downstream. The
    runtime builds its epoch map keyed by hotkey (epoch/run_epoch.rs:619-628),
    so a duplicate does not produce two entries, it produces one, and the
    vector comes back a seat short with no gap in the seat map to show for it.
    Registration forbids it (subnets/registration.rs:61-64), which is exactly
    why nothing downstream is watching for it.
    """
    if not isinstance(hotkey_by_uid, dict):
        raise SeatsNotContiguous(
            f"the seat map behind {what} came back as {hotkey_by_uid!r}, which is not a map "
            f"of seats to hotkeys."
        )

    for seat in hotkey_by_uid:
        if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
            raise SeatsNotContiguous(
                f"the seat map behind {what} is keyed by {seat!r}, which is not a seat "
                f"number. A map keyed by text sorts and compares differently from one keyed "
                f"by number, so seat 10 would sit between seat 1 and seat 2."
            )

    hotkeys = list(hotkey_by_uid.values())
    if len(set(hotkeys)) != len(hotkeys):
        shared = sorted(seat for seat, hotkey in hotkey_by_uid.items() if hotkeys.count(hotkey) > 1)
        raise SeatsNotContiguous(
            f"seats {shared} in the map behind {what} share a hotkey. The chain keys its "
            f"epoch map by hotkey, so those seats collapse into one entry and the vector "
            f"arrives short while the seat map still looks complete."
        )

    return sorted(hotkey_by_uid)


def _amount(value, seat, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAVector(
            f"the {what} for seat {seat} came back as {value!r}, which is not a decoded "
            f"amount. Reaching attribution undecoded is refused rather than parsed here, "
            f"because a wrong parse pays somebody the wrong number instead of failing."
        )
    if value < 0:
        raise NotAVector(f"the {what} for seat {seat} is negative ({value}).")
    return value


# The stride the runtime packs a mechanism into (subnets/mechanism.rs:28), with
# the packing itself at mechanism.rs:35-40 as
# ``storage_index = netuid + mech_id * GLOBAL_MAX_SUBNET_COUNT``.
GLOBAL_MAX_SUBNET_COUNT = 4096

PAY_EVENT = "IncentiveAlphaEmittedToMiners"


class NotAMechanismIndex(Refusal):
    """A pay event whose subnet field is not a packed index at all."""


class MechanismsMissing(Refusal):
    """The mechanisms that paid this block and the ones that were read disagree."""


def netuid_and_mechanism(storage_index):
    """Unpack a pay event's subnet field into ``(netuid, mechanism)``.

    The field is not a netuid. It is ``netuid + mechanism * 4096``. For the
    main mechanism those are the same number, which is the whole danger: a
    reader that compares this field against its own subnet number works
    perfectly for as long as the subnet runs one mechanism, and the day a
    second one is added it silently discards that mechanism's pay as belonging
    to some other subnet. The total stays a real, well-formed number.
    """
    if isinstance(storage_index, bool) or not isinstance(storage_index, int):
        raise NotAMechanismIndex(
            f"a pay event's subnet field came back as {storage_index!r}, which is not a "
            f"whole number. It carries both the subnet and the mechanism, so a reader that "
            f"cannot unpack it cannot tell whose pay this is."
        )
    if storage_index < 0:
        raise NotAMechanismIndex(f"a pay event's subnet field is negative ({storage_index}).")
    return storage_index % GLOBAL_MAX_SUBNET_COUNT, storage_index // GLOBAL_MAX_SUBNET_COUNT


def pay_event_present(events, *, netuid):
    """Whether this block paid our subnet at all.

    Cheap, and separate from the reading, so a block that is not an epoch block
    costs no extra chain reads. Every block in a day runs through here and only
    a handful are epoch blocks.
    """
    for record in _records(events):
        index = _pay_index(record)
        if index is not None and netuid_and_mechanism(index)[0] == netuid:
            return True
    return False


def epoch_pay_vectors(events, *, netuid, mechanism_count, hotkey_by_uid, subnetwork_n, block):
    """Every mechanism's pay vector at one epoch block, or a refusal.

    Returns ``{mechanism: {seat: amount}}``, unsummed. Summing belongs to
    attribution, which knows which seats the runtime then withholds from.

    ``mechanism_count`` is read at this same block rather than once per day,
    because a subnet's mechanism count is settable and a day that begins with
    one and ends with two would otherwise lose every later epoch's second half.
    """
    if isinstance(mechanism_count, bool) or not isinstance(mechanism_count, int):
        raise MechanismsMissing(
            f"the mechanism count at block {block} came back as {mechanism_count!r}, which "
            f"is not a whole number."
        )
    if mechanism_count < 1:
        raise MechanismsMissing(
            f"the mechanism count at block {block} came back as {mechanism_count}. Every "
            f"subnet runs at least one mechanism, so this is a read that failed rather than "
            f"a subnet that pays through none."
        )

    vectors = {}
    for record in _records(events):
        index = _pay_index(record)
        if index is None:
            continue
        their_netuid, mechanism = netuid_and_mechanism(index)
        if their_netuid != netuid:
            continue
        if mechanism in vectors:
            raise MechanismsMissing(
                f"mechanism {mechanism} paid twice in block {block}. Which of the two is "
                f"this epoch's pay is not written on the chain, and choosing one would be a "
                f"guess about what everybody earned."
            )
        vectors[mechanism] = seats_from_vector(
            record["attributes"].get("emissions"),
            hotkey_by_uid=hotkey_by_uid,
            subnetwork_n=subnetwork_n,
            what=f"pay from mechanism {mechanism} at block {block}",
        )

    if not vectors:
        return {}

    expected = set(range(mechanism_count))
    if set(vectors) != expected:
        raise MechanismsMissing(
            f"block {block} paid through mechanisms {sorted(vectors)} while the subnet "
            f"declares {mechanism_count} of them, so the expected set is "
            f"{sorted(expected)}. A mechanism that paid and was not read understates every "
            f"wallet in the day by exactly what it paid, and the shortfall is a valid "
            f"number that nothing downstream can question."
        )

    return vectors


def _records(events):
    if not isinstance(events, (list, tuple)):
        raise NotAVector(f"a block's events came in as {events!r}, which is not a list of events.")
    return events


def _pay_index(record):
    """The packed subnet field of a pay event, or None if this is not one."""
    if not isinstance(record, dict):
        raise NotAVector(f"an event came in as {record!r}, which is not an event record.")
    if record.get("module_id") != "SubtensorModule" or record.get("event_id") != PAY_EVENT:
        return None
    attributes = record.get("attributes")
    if not isinstance(attributes, dict) or "netuid" not in attributes:
        raise NotAMechanismIndex(
            f"a pay event arrived without a subnet field: {record!r}. An event whose shape "
            f"changed is refused rather than skipped, because skipping it would drop real "
            f"pay and look like a block that paid nothing."
        )
    return attributes["netuid"]
