"""Independent integer arithmetic cross-check. No SDK or chain access."""

import json
import random
import subprocess

from noah.scoring.pay import pay_weight
from noah.scoring.reporting_bonus import bonus_pay


def test_bonus_matches_independent_javascript_across_integer_rounding_edges():
    rng = random.Random(16011)
    cases = []
    for _ in range(100):
        score = rng.randrange(10**17)
        mult = rng.randrange(2 * 10**9)
        cases.append(
            dict(
                scoreFp=str(score),
                multiplierFp=str(mult),
                payWeightFp=str(pay_weight(score, mult)),
                doorOpen=True,
            )
        )
        cases.append(
            dict(
                payWeightFp=str(rng.randrange(10**16)),
                doorOpen=True,
                state=dict(maturity=str(rng.randrange(1000001)), missed=str(rng.randrange(5))),
            )
        )
    script = """
const fs=require('fs');const rows=JSON.parse(fs.readFileSync(0,'utf8'));
function sqrt(n){
  if(n<2n)return n;
  let low=0n,high=n;
  while(low+1n<high){
    const mid=(low+high)/2n;
    if(mid*mid<=n)low=mid;else high=mid;
  }
  return low;
}
console.log(JSON.stringify(rows.map(r=>{
  if(!r.state)
    return (sqrt(BigInt(r.scoreFp)+20000000000n)*BigInt(r.multiplierFp)/1000000000n)
      .toString();
  const s=r.state,m=BigInt(s.maturity),d=BigInt(s.missed);
  const extra=d===0n?20n*m:d===1n?10n*m:0n;
  return (BigInt(r.payWeightFp)+extra*1000n).toString();
})));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert list(map(int, json.loads(proc.stdout))) == [bonus_pay(row, True) for row in cases]
