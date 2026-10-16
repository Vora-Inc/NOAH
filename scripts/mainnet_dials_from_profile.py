"""Write the NOAH_DIALS_JSON for a profile: the seven dial values, read from the profile.

    NOAH_PARAMETERS_JSON=<profile> python scripts/mainnet_dials_from_profile.py > dials.json

Derived from the profile so the two files cannot disagree (the validator refuses
a mismatch). Refuses whatever the profile loader refuses, including undecided
markers. Does not judge whether the profile is DECIDED; the mainnet guard does.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from noah.scoring.parameters import ParametersError, load_parameters  # noqa: E402


def dials_from(params):
    h = params.harshness
    return {
        "targetFp": str(params.target_fp),
        "harshness": int(h) if h.denominator == 1 else str(h.numerator / h.denominator),
        "capFp": str(params.cap_fp),
        "halfLifeDays": str(params.half_life_days),
        "streakCeilingFp": str(params.streak_ceiling_fp),
        "streakRampDays": str(params.streak_ramp_days),
        "ongoingHoldRao": str(params.ongoing_hold_rao),
    }


def main():
    try:
        print(json.dumps(dials_from(load_parameters()), indent=2))
    except ParametersError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
