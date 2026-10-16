"""Predict the fixed-point transform applied to a published weight vector.

The modeled runtime scales its largest weight to 65,535. At maximum weight 32,768
or below it multiplies before division; above that threshold it divides first to
avoid signed fixed-point overflow. The order affects rounding. Compare stored
weights with this prediction, not directly with the input vector, and verify the
model against the runtime version used by the deployment."""

from dataclasses import dataclass

# The runtime's fixed-point type is I32F32: a signed number carried as an integer
# scaled by 2^32. Everything below works on that raw integer, so this is ordinary
# integer arithmetic with no floating point anywhere.
FRACTION = 32
ONE = 1 << FRACTION
RAW_LIMIT = (1 << 63) - 1

U16_MAX = 65535
THRESHOLD = 32768


class EmittedVectorRefused(ValueError):
    """The vector handed in is not one this validator could have emitted."""


@dataclass(frozen=True)
class Prediction:
    """What the chain will hold, and which of the two formulas produced it."""

    stored: tuple
    divides_first: bool
    largest: int


def _fixed(whole):
    return whole << FRACTION


def _multiply(a, b):
    # Saturating, exactly as the runtime saturates. Worth being precise about when
    # this fires: never, on any vector this validator emits. Below the threshold
    # the product cannot reach the limit, and above it the runtime divides first
    # so the product is small. The clamp exists because it is the reason the
    # threshold is there at all, and the only way to show that is to compute the
    # formula the runtime declined to use and watch it overflow. A test does
    # exactly that. Widening the integers here would hide it.
    return min((a * b) >> FRACTION, RAW_LIMIT)


def _divide(a, b):
    # The runtime's safe division answers zero rather than failing.
    return 0 if b == 0 else (a << FRACTION) // b


def _round_half_up(value):
    return ((value + (ONE >> 1)) >> FRACTION) << FRACTION


def _to_u16(value):
    return max(0, min(U16_MAX, value >> FRACTION))


def predict_stored(emitted):
    """Predict the vector the chain will store, given the one we publish.

    Takes the weight values in the order they are submitted, zeros included, and
    returns them in the same order. Position matters: the runtime zips the
    rescaled values back against the seat numbers, so dropping a zero here would
    silently shift every seat after it.
    """
    values = list(emitted)
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool):
            raise EmittedVectorRefused(
                f"weight {value!r} is not an integer. The chain takes u16 values and this "
                f"predicts what it does with them, so a non-integer here means the caller "
                f"has already lost the arithmetic."
            )
        if not 0 <= value <= U16_MAX:
            raise EmittedVectorRefused(
                f"weight {value} is outside 0 to {U16_MAX}. The chain could not have "
                f"accepted this vector, so predicting what it would store is meaningless."
            )

    if not values:
        raise EmittedVectorRefused(
            "an empty vector. The runtime has a branch for this that normalises by the sum "
            "rather than the maximum, and we never emit one, so rather than model a path "
            "this validator cannot reach, this refuses."
        )

    fixed = [_fixed(v) for v in values]
    largest = max(fixed)
    scale = _fixed(U16_MAX)

    # An all-zero vector stays all-zero, which the activity keeper depends on: it
    # publishes zeroes to stay audible, and a chain that turned those into
    # anything else would be paying seats the keeper meant to pay nothing.
    #
    # The runtime writes that as its own branch. This does not, because here it
    # would be a branch that cannot change an answer. Division by zero happens
    # only when the largest weight is zero, which means every weight is zero,
    # which means every numerator is zero too. Both routes give zero. A mutation
    # sweep found the separate branch unfalsifiable, which is what redundant code
    # looks like from the outside, so it is gone and the property is held by a
    # test instead.
    if largest > _fixed(THRESHOLD):
        ratio = _divide(scale, largest)
        return Prediction(
            stored=tuple(_to_u16(_round_half_up(_multiply(e, ratio))) for e in fixed),
            divides_first=True,
            largest=largest >> FRACTION,
        )

    return Prediction(
        stored=tuple(_to_u16(_round_half_up(_divide(_multiply(e, scale), largest))) for e in fixed),
        divides_first=False,
        largest=largest >> FRACTION,
    )
