// Independent integer allocation implementation. No signing or network access.
import fs from 'node:fs';
const {input:x, previous:p, result:r}=JSON.parse(fs.readFileSync(0,'utf8'));
const B=BigInt, A=5000000000n, U=65535n;
const sum=a=>a.reduce((v,w)=>v+w,0n), min=(...a)=>a.reduce((v,w)=>v<w?v:w), max=(a,b)=>a>b?a:b;
const key=(c,h)=>JSON.stringify([c,h]);
const earnedC={},earnedH={},pendingC={},pendingH={},ledger={};
for(const t of x.lifetimeEarnings){
 const c=t.coldkey,h=t.hotkey,e=B(t.earnedRao),q=B(t.pendingExpectedRao);
 earnedC[c]=(earnedC[c]??0n)+e;earnedH[h]=(earnedH[h]??0n)+e;
 pendingC[c]=(pendingC[c]??0n)+q;pendingH[h]=(pendingH[h]??0n)+q;
 ledger[key(c,h)]=t;
}
// Retain departed wallets in the ordinary score/history, but only current seats
// have destinations. A null UID must never become a synthetic "null" weight key.
const ordinary=Object.fromEntries(r.ordinaryOutput.miners.filter(m=>m.uid!=null).map(m=>[m.uid,m]));
const miners=Object.fromEntries(x.fixture.miners.filter(m=>m.uid!=null).map(m=>[m.uid,m]));
const s=x.snapshot, sample=s.miningPoolSample;
const pool=B(sample.amountRao)*B(s.epochSeconds)/B(sample.durationSeconds);
const requests={},groups={},quarantined=new Set();
for(const t of s.seats){
 const l=ledger[key(t.coldkey,t.hotkey)],m=miners[t.uid];
 const closed=sum(Object.values(m.days).map(d=>B(d.paidRao)+B(d.payUnlockedRao??0)));
 if(!l || t.observedBlock!==s.finalizedBlock || !/^\d+$/.test(String(t.spendableEarnedRao))
   || B(t.spendableEarnedRao)<0n || B(t.pendingExpectedRao)<0n || B(t.spendableEarnedRao)<closed
   || l.earnedRao!==t.spendableEarnedRao || l.pendingExpectedRao!==t.pendingExpectedRao
   || typeof t.ownerExcluded!=='boolean') quarantined.add(t.coldkey);
}
const terminals={};
for(const t of s.seats){
 const c=t.coldkey,h=t.hotkey,u=t.uid,m=miners[u],o=ordinary[u];
 if(quarantined.has(c)){requests[u]=0n;continue;}
 const prior=p?.seats?.[key(c,h)]?.terminal??'';
 const exhausted=earnedC[c]>=A||earnedH[h]>=A;
 const terminal=B(m.entryDepositRao??0)>0n||prior==='ordinary'||(B(o.baseFp)>0n&&(exhausted||prior==='exhausted'))
  ?'ordinary':exhausted||prior==='exhausted'?'exhausted':'';
 terminals[key(c,h)]=terminal;
 if(terminal||t.ownerExcluded||!o.doorOpen)continue;
 const remaining=max(0n,min(A-earnedC[c]-pendingC[c],A-earnedH[h]-pendingH[h]));
 const num=min(A*B(s.epochSeconds),remaining*86400n)*U,den=86400n*pool;
 requests[u]=(num+den-1n)/den;
 (groups[c]??={})[u]=requests[u];
}
function apport(values,total){
 const n=sum(Object.values(values));if(!n)throw Error('no positive allocation');
 const out={};for(const [k,v] of Object.entries(values))out[k]=v*total/n;
 const order=Object.keys(values).sort((a,b)=>{
  const ar=values[a]*total%n,br=values[b]*total%n;return ar===br?Number(a)-Number(b):ar>br?-1:1;
 });
 for(const k of order.slice(0,Number(total-sum(Object.values(out)))))out[k]++;
 return out;
}
const wallets={};
for(const [c,vs] of Object.entries(groups)){
 const remaining=max(0n,A-earnedC[c]-pendingC[c]);
 wallets[c]=min(sum(Object.values(vs)),(min(A*B(s.epochSeconds),remaining*86400n)*U+86400n*pool-1n)/(86400n*pool));
}
// Water filling, one weight unit per eligible wallet per round. Deliberately
// differs from the Python implementation; at most 163 rounds are needed.
const shares=Object.fromEntries(Object.keys(wallets).map(c=>[c,0n]));
let budget=163n;
while(budget){let used=false;for(const c of Object.keys(wallets).sort()){
 if(budget&&shares[c]<wallets[c]){shares[c]++;budget--;used=true;}
}if(!used)break;}
const proposed={...requests};
for(const [c,vs] of Object.entries(groups))if(sum(Object.values(vs)))Object.assign(proposed,apport(vs,shares[c]));
const normal={};
for(const [u,o] of Object.entries(ordinary))if(!(u in proposed))normal[u]=B(o.payWeightFp);
const blocked=Object.keys(proposed).length>0&&!sum(Object.values(normal));
let weights;
if(blocked)weights=Object.fromEntries(Object.keys(ordinary).map(u=>[u,0n]));
else if(Object.keys(proposed).length)weights={...apport(normal,U-sum(Object.values(proposed))),...proposed};
else weights=Object.fromEntries(Object.entries(r.ordinaryOutput.weightsU16).map(([u,v])=>[u,B(v)]));
const fallback=Object.fromEntries(Object.keys(ordinary).map(u=>[u,normal[u]??0n]));
const fw=sum(Object.values(fallback))?apport(fallback,U):fallback;
function check(expected,actual,label){
 if(Object.keys(expected).length!==Object.keys(actual).length)throw Error(label+' keys');
 for(const [u,v] of Object.entries(expected))if(v!==B(actual[u]))throw Error(label+' '+u);
}
check(weights,r.proposedWeightsU16,'weights');check(fw,r.ordinaryFallbackWeightsU16,'fallback');
for(const [k,t] of Object.entries(terminals))if(r.state.seats[k].terminal!==t)throw Error('terminal state');
process.stdout.write('verified\n');
