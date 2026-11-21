import json
import random
import subprocess
import unittest
from pathlib import Path

from noah.points.engine import evaluate
from noah.points.run import agreed

R = 10**9


def fixture(histories):
    return dict(
        schema="noah-fixture/6",
        name="points-test",
        dayEpochTs="0",
        evalDay=str(len(histories[0]) - 1),
        dials=dict(
            targetFp="500000000",
            harshness=2,
            capFp="2000000000",
            halfLifeDays="45",
            streakCeilingFp="2000000000",
            streakRampDays="90",
            ongoingHoldRao="0",
        ),
        miners=[
            dict(
                coldkey=f"test-cold-{uid}",
                uid=uid,
                holdRao="0",
                firstActivityDay="0",
                entryDepositRao="0",
                registrationDay="0",
                days={
                    str(day): dict(
                        paidRao=str(e),
                        paidLockedArrivalRao="0",
                        payUnlockedRao="0",
                        noahDepositRao=str(n),
                        foreignDepositNoahEquivRao=str(f),
                        noFault=False,
                    )
                    for day, (e, n, f) in enumerate(hist)
                },
            )
            for uid, hist in enumerate(histories)
        ],
    )


class Engine(unittest.TestCase):
    def test_starter_to_zero_to_catchup_twin(self):
        raw = fixture([[(5 * R, 0, 0), (0, 0, 0), (0, 2500000000, 0), (0, 0, 0)]])
        result = agreed(raw)
        s = result["miners"][0]["state"]
        self.assertEqual((s["points"], s["qualified"], s["streak"]), ("4000000", "1", "0"))

    def test_root_foreign_equivalence(self):
        self.assertEqual(
            evaluate(fixture([[(100 * R, 0, 0), (0, 50 * R, 0)]])),
            evaluate(fixture([[(100 * R, 0, 0), (0, 0, 50 * R)]])),
        )

    def test_all_zero_does_not_invent_weights(self):
        self.assertEqual(agreed(fixture([[(0, 0, 0)]]))["weightsU16"], {})

    def test_missing_day_is_not_miner_miss(self):
        raw = fixture([[(100 * R, 0, 0), (0, 0, 0)]])
        del raw["miners"][0]["days"]["1"]
        with self.assertRaises(ValueError):
            evaluate(raw)

    def test_wrong_target_not_silently_ignored(self):
        raw = fixture([[(0, 0, 0)]])
        raw["dials"]["targetFp"] = "900000000"
        with self.assertRaises(ValueError):
            evaluate(raw)

    def test_randomized_python_typescript_full_state_parity(self):
        rng = random.Random(402040)
        raws = []
        for case in range(120):
            histories = []
            for miner in range(1 + case % 7):
                histories.append(
                    [
                        (
                            rng.choice([0, 1, 5 * R, 100 * R, 10**16]),
                            rng.choice([0, 1, 2500000000, 50 * R, 200 * R]),
                            rng.choice([0, 1, 10 * R]),
                        )
                        for day in range(1 + case % 39)
                    ]
                )
            raws.append(fixture(histories))
        script = Path(__file__).resolve().parents[3] / "verification/noah-ts/src/points.ts"
        js = f"import {{evaluatePoints}} from {json.dumps(script.as_uri())};let s='';for await(const c of process.stdin)s+=c;const a=JSON.parse(s);console.log(JSON.stringify(a.map(x=>evaluatePoints(JSON.stringify(x))),(_,v)=>typeof v==='bigint'?v.toString():v));"
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", js],
            input=json.dumps(raws),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([evaluate(r) for r in raws], json.loads(proc.stdout))


def test_verified_earnings_override_old_no_fault_marker_without_mutating_evidence():
    import copy

    raw = fixture([[(100 * R, 0, 0), (0, 50 * R, 0)]])
    expected = agreed(raw)
    raw["miners"][0]["days"]["0"]["noFault"] = True
    frozen = copy.deepcopy(raw)
    assert agreed(raw) == expected
    assert raw == frozen
