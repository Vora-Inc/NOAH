"""Read sealed commits from the expected runtime storage map.

An empty TimelockedWeightCommits read is cross-checked against legacy commit maps.
Conflicting evidence is refused, since a clean empty reply from the wrong map
would otherwise look like proof that no commit exists."""

from .settings import SettingsError

# Verified-source, spec 447 and unchanged at 448.
LIVE_MAP = "TimelockedWeightCommits"

# Present on the runtime, queryable, and not where commits are filed.
LEGACY_MAPS = ("CRV3WeightCommitsV2", "CRV3WeightCommits", "WeightCommits")


class CommitStorageUnreadable(SettingsError):
    """The commit storage could not be read, so nothing is concluded from it."""


class CommitsFiledElsewhere(SettingsError):
    """A map we do not read is holding commits, so our location is in doubt."""


def read_stored_commits(chain, netuid):
    """Every sealed commit this runtime is holding for a subnet.

    Returns a list, which is empty only when the emptiness has been checked
    against the maps a wrong reader would land on.
    """
    try:
        live = list(chain.read_map(LIVE_MAP, netuid))
    except Exception as error:
        raise CommitStorageUnreadable(
            f"the commit storage {LIVE_MAP} could not be read "
            f"({type(error).__name__}: {error}). Nothing is concluded from this: an "
            f"unreadable map and a map holding no commits are different facts, and only "
            f"one of them means a publication is missing."
        ) from error

    if live:
        return live

    # Empty. Before that is reported as "no commits", find out whether it is
    # this map that is empty or this reader that is lost.
    holding = {}
    unreadable = {}
    for name in LEGACY_MAPS:
        try:
            other = list(chain.read_map(name, netuid))
        except Exception as error:
            unreadable[name] = f"{type(error).__name__}: {error}"
            continue
        if other:
            holding[name] = len(other)

    if holding:
        where = ", ".join(f"{name} ({count})" for name, count in sorted(holding.items()))
        raise CommitsFiledElsewhere(
            f"{LIVE_MAP} is empty and {where} is not. Both of these are real storage on "
            f"this runtime and both answer a query without complaining, so an empty answer "
            f"from one of them is indistinguishable from a chain holding no commits. This "
            f"refuses rather than reporting no commits, because the two explanations need "
            f"different people: either the runtime has moved commits since this reader was "
            f"pinned, or this reader is pointed at the wrong map. Do not conclude a "
            f"publication failed until that is settled."
        )

    if unreadable and len(unreadable) == len(LEGACY_MAPS):
        raise CommitStorageUnreadable(
            f"{LIVE_MAP} read as empty and none of the maps a wrong reader would land on "
            f"could be read to check that ({'; '.join(sorted(unreadable))}). The empty "
            f"answer is therefore unconfirmed, and an unconfirmed empty is the shape this "
            f"whole check exists to refuse."
        )

    return []
