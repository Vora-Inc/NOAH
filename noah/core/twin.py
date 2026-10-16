"""Require the Python and TypeScript scoring results to agree before publication.

Both engines use the same fixture. Canonical serialization makes the comparison
exact. Agreement tests implementation consistency, not the correctness of chain
inputs or the economic policy encoded in the profile."""

from .settings import SettingsError


class TwinProofFailed(SettingsError):
    """The two engines produced different bytes. Nothing is published."""


class SecondOpinionUnavailable(SettingsError):
    """There is no usable second opinion, so there is no proof to publish on."""


def _first_difference(left, right):
    for offset, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return offset
    return min(len(left), len(right))


def require_agreement(fixture, *, first, second):
    """Return the agreed output, or refuse.

    ``first`` and ``second`` each take the fixture and return the rendered
    output as text. Both are called, always, and neither is treated as the
    reference.
    """
    try:
        one = first(fixture)
    except Exception as error:
        raise SecondOpinionUnavailable(
            f"the primary engine could not score this period "
            f"({type(error).__name__}: {error}). Nothing is published."
        ) from error

    try:
        two = second(fixture)
    except Exception as error:
        raise SecondOpinionUnavailable(
            f"the second engine could not be run ({type(error).__name__}: {error}), so this "
            f"period has only one opinion behind it. That is refused rather than published: "
            f"a validator down to one engine has lost the protection this gate exists to "
            f"give, and publishing anyway would look identical to publishing with it."
        ) from error

    if not one or not two:
        raise SecondOpinionUnavailable(
            f"an engine produced no output at all (primary: {len(one or '')} bytes, second: "
            f"{len(two or '')} bytes). Two empty outputs would compare equal and prove "
            f"nothing, so an empty result refuses rather than agreeing."
        )

    if one != two:
        offset = _first_difference(one, two)
        raise TwinProofFailed(
            f"the two engines disagree, first at byte {offset}. Neither is treated as "
            f"correct, because nobody knows yet which one is wrong, and publishing either "
            f"would put numbers on chain that half of this validator's own arithmetic "
            f"rejects. Nothing is published. Compare the two outputs and fix whichever "
            f"turns out to depart from the written rules."
        )

    return one
