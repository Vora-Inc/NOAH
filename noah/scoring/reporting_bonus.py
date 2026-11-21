"""Optional daily work reward, separate from the financial scoring ledger.

Callers must authenticate the day's report and miner incarnation before setting
awarded=True. Nothing is carried forward into earnings, deposits or last_compliant.
"""

from .pay import pay_weight

BONUS_FP = 20 * 10**9
REWARD = "bonus-20"


def bonus_pay(row, awarded):
    original = int(row["payWeightFp"])
    if not awarded or row.get("doorOpen") is not True:
        return original
    state = row.get("state")
    if state is not None:
        # Points use 1e6 internally and 1e9 on the output wire. Floor at the
        # same internal precision as the points engine, before wire conversion.
        maturity, missed = int(state["maturity"]), int(state["missed"])
        if not 0 <= maturity <= 10**6 or missed < 0:
            raise ValueError("invalid reporting bonus maturity/debt state")
        bonus = 20 * maturity
        bonus = bonus if missed == 0 else bonus // 2 if missed == 1 else 0
        return original + bonus * 1000
    return pay_weight(int(row["scoreFp"]) + BONUS_FP, int(row["multiplierFp"]))
