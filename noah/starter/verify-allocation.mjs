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
const reportsEnabled=x.schema==='noah-starter-epoch-preview/5', excluded=new Set();
const exact=(value,names)=>{
 if(!value || Array.isArray(value) || typeof value!=='object'
   || Object.keys(value).sort().join('|')!==names.split(' ').sort().join('|'))throw Error('report fields');
};
const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'
 ?Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])])):value;
if(reportsEnabled){
 exact(x,'schema policyId fixture scoredHotkeys snapshot lifetimeEarnings reportGate');
 if(x.fixture.schema?.startsWith('noah-points-fixture/') || 'pointsRules' in x.fixture)throw Error('Legacy required');
 const g=x.reportGate;
 exact(g,'schema proofSha256 evalDay registrations exclusions'+('bonusUids' in g?' bonusUids':''));
 if('bonusUids' in g && (!Array.isArray(g.bonusUids)||g.exclusions.length||g.bonusUids.some((u,i)=>!Number.isSafeInteger(u)||!miners[u]||(i&&u<=g.bonusUids[i-1]))))throw Error('bonus identities');
 if(g.schema!=='noah-starter-report-exclusions/1'||! /^[0-9a-f]{64}$/.test(g.proofSha256)
   ||typeof g.proofSha256!=='string'||!Number.isSafeInteger(g.evalDay)||g.evalDay<0
   ||B(g.evalDay)!==B(x.fixture.evalDay))throw Error('report context');
 const registrations=new Map();
 for(const name of ['registrations','exclusions']){
  if(!Array.isArray(g[name]))throw Error('report array');
  let last=-1;const hotkeys=new Set();
  for(const row of g[name]){
   exact(row,'uid coldkey hotkey registrationBlock');
   const {uid:u,coldkey:c,hotkey:h,registrationBlock:b}=row;
   if(!Number.isSafeInteger(u)||u<0||u<=last||!Number.isSafeInteger(b)||b<0||b>s.finalizedBlock
     ||typeof c!=='string'||!c||typeof h!=='string'||!h||hotkeys.has(h)
     ||!miners[u]||miners[u].coldkey!==c||x.scoredHotkeys[u]!==h)throw Error('report identity');
   const seat=s.seats.find(t=>t.uid===u);
   if(!seat||seat.coldkey!==c||seat.hotkey!==h)throw Error('report snapshot identity');
   last=u;hotkeys.add(h);
   if(name==='registrations')registrations.set(u,[c,h,b]);
   else{
    if(JSON.stringify(registrations.get(u))!==JSON.stringify([c,h,b]))throw Error('report registration');
    excluded.add(u);
   }
  }
 }
 if(registrations.size!==Object.keys(miners).length)throw Error('report coverage');
 if(r.schema!=='noah-starter-epoch-result/5'
   ||JSON.stringify(canonical(g))!==JSON.stringify(canonical(r.reportGate)))throw Error('report result binding');
}else if('reportGate' in x)throw Error('report schema required');
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
 if(excluded.has(u)){requests[u]=0n;continue;}
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
if(reportsEnabled && 'bonusUids' in x.reportGate){
 const sqrt=n=>{if(n<0n)throw Error('negative score');if(n<2n)return n;let a=n,b=(a+1n)/2n;while(b<a){a=b;b=(a+n/a)/2n;}return a;};
 for(const u of x.reportGate.bonusUids){const o=ordinary[u];if(u in normal&&o.doorOpen&&!s.seats.find(t=>t.uid===u).ownerExcluded)normal[u]=sqrt(B(o.scoreFp)+20000000000n)*B(o.multiplierFp)/1000000000n;}
}
const blocked=Object.keys(proposed).length>0&&!sum(Object.values(normal));
let weights;
if(blocked)weights=Object.fromEntries(Object.keys(ordinary).map(u=>[u,0n]));
else if(Object.keys(proposed).length)weights={...apport(normal,U-sum(Object.values(proposed))),...proposed};
else weights=reportsEnabled&&'bonusUids' in x.reportGate?(sum(Object.values(normal))?apport(normal,U):normal):Object.fromEntries(Object.entries(r.ordinaryOutput.weightsU16).map(([u,v])=>[u,B(v)]));
const fallback=Object.fromEntries(Object.keys(ordinary).map(u=>[u,normal[u]??0n]));
let fw=sum(Object.values(fallback))?apport(fallback,U):fallback;
if(reportsEnabled){
 if(!sum(Object.values(weights)))weights={};
 if(!sum(Object.values(fw)))fw={};
}
function check(expected,actual,label){
 if(Object.keys(expected).length!==Object.keys(actual).length)throw Error(label+' keys');
 for(const [u,v] of Object.entries(expected))if(v!==B(actual[u]))throw Error(label+' '+u);
}
check(weights,r.proposedWeightsU16,'weights');check(fw,r.ordinaryFallbackWeightsU16,'fallback');
for(const [k,t] of Object.entries(terminals))if(r.state.seats[k].terminal!==t)throw Error('terminal state');
process.stdout.write('verified\n');
