// Independent integer-only 40/20/40 scorer.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { loadFixture, parseStrict } from './fixture.ts';
import { quantizeWeights } from './pay.ts';
import { verifyEmaFixture } from './ema_proof.ts';
import { canonicalJson } from './canonical.ts';
const Q=1000000n;
const min=(a:bigint,b:bigint)=>a<b?a:b;
const max=(a:bigint,b:bigint)=>a>b?a:b;
function receipts(rows:any,ema=false):any[]{
 const names=['receipt_id','coldkey','origin_hotkey','destination_hotkey','block','block_hash','day','event_ordinal','extrinsic_index','token_netuid','amount_rao','status','reason','credit_rao','reference_proof'].sort().join(',');
 const reasons=['reference-subnet-absent','reference-token-incarnation-differs','native-reference-price-zero','foreign-reference-price-zero'];
 const seen=new Set<string>();
 if(!Array.isArray(rows))throw new Error('receipt dispositions must be a list');
 for(const r of rows){
  if(r===null||typeof r!=='object'||Array.isArray(r)||Object.keys(r).sort().join(',')!==names)throw new Error('unexpected receipt fields');
  for(const k of ['block','day','event_ordinal','extrinsic_index','token_netuid'])if(!Number.isSafeInteger(r[k])||r[k]<0)throw new Error('invalid receipt integer');
  for(const k of ['coldkey','origin_hotkey','destination_hotkey'])if(typeof r[k]!=='string'||! /^[1-9A-HJ-NP-Za-km-z]{47,49}$/.test(r[k]))throw new Error('invalid receipt identity');
  if(typeof r.block_hash!=='string'||! /^0x[0-9a-f]{64}$/.test(r.block_hash)||typeof r.receipt_id!=='string'||!new RegExp('^0x[0-9a-f]{64}/'+r.block_hash+'/'+r.event_ordinal+'$').test(r.receipt_id)||seen.has(r.receipt_id))throw new Error('invalid or duplicate receipt');
  seen.add(r.receipt_id);
  for(const k of ['amount_rao','credit_rao'])if(typeof r[k]!=='string'||! /^(0|[1-9][0-9]*)$/.test(r[k]))throw new Error('invalid receipt amount');
  if(BigInt(r.amount_rao)<=0n)throw new Error('empty receipt');
  if(r.status==='accepted'){if(r.reason!=='daily-reference'&&!(ema&&r.reason==='ema-spot-reference'))throw new Error('invalid accepted disposition');}
  else if(r.status==='unsupported'){if(!reasons.includes(r.reason)||r.credit_rao!=='0')throw new Error('invalid unsupported disposition');}
  else throw new Error('pending receipt cannot settle');
  const p=r.reference_proof;
  if(p===null||typeof p!=='object'||Array.isArray(p)||Object.keys(p).sort().join(',')!=='path,sha256'||typeof p.sha256!=='string'||! /^[0-9a-f]{64}$/.test(p.sha256)||p.path!=='points-reference-proofs/'+p.sha256+'.json')throw new Error('invalid proof pin');
 }
 return rows;
}
function reconcileReceipts(input:any, rows:any[]):void{
 const hotkeys=input.pointsHotkeys;
 if(hotkeys===null||typeof hotkeys!=='object'||Array.isArray(hotkeys))throw new Error('missing hotkey map');
 const expected=input.miners.filter((m:any)=>m.uid!==null).map((m:any)=>String(m.uid)).sort();
 if(!expected.every((k:string)=>Object.hasOwn(hotkeys,k))||Object.keys(hotkeys).some(k=>! /^(0|[1-9][0-9]*)$/.test(k)||!Number.isSafeInteger(Number(k)))||Object.values(hotkeys).some(v=>typeof v!=='string'||! /^[1-9A-HJ-NP-Za-km-z]{47,49}$/.test(v))||new Set(Object.values(hotkeys)).size!==Object.keys(hotkeys).length)throw new Error('hotkey roster differs');
 const owners=input.pointsOwners;
 if(owners===null||typeof owners!=='object'||Array.isArray(owners)||Object.keys(owners).sort().join(',')!==Object.keys(hotkeys).sort().join(',')||Object.values(owners).some(v=>typeof v!=='string'||! /^[1-9A-HJ-NP-Za-km-z]{47,49}$/.test(v)))throw new Error('owner roster differs');
 const active=new Map<string,number>(),vacated=new Map<string,number>();
 input.miners.forEach((m:any,i:number)=>{
  if(m.uid===null){if(vacated.has(m.coldkey))throw new Error('ambiguous vacated row');vacated.set(m.coldkey,i);}
  else {const h=hotkeys[String(m.uid)];if(owners[String(m.uid)]!==m.coldkey)throw new Error('fixture owner differs');if(typeof h!=='string'||! /^[1-9A-HJ-NP-Za-km-z]{47,49}$/.test(h))throw new Error('invalid hotkey');active.set(JSON.stringify([m.coldkey,h]),i);}
 });
 const cutoff=input.pointsMigration?.cutoffDay??Number(input.evalDay),totals=new Map<string,bigint>();
 for(const r of rows){
  let index=active.get(JSON.stringify([r.coldkey,r.origin_hotkey]));
  if(index===undefined){if(Object.entries(hotkeys).some(([uid,h])=>h===r.origin_hotkey&&owners[uid]===r.coldkey))throw new Error('excluded current receipt');index=vacated.get(r.coldkey);}
  if(index===undefined||r.day<=cutoff||r.day>Number(input.evalDay)||!Object.hasOwn(input.miners[index].days,String(r.day)))throw new Error('receipt has no prospective row');
  const key = JSON.stringify([index,r.day]); totals.set(key,(totals.get(key)??0n)+BigInt(r.credit_rao));
 }
 input.miners.forEach((m:any,i:number)=>{for(const [day,value] of Object.entries(m.days) as [string,any][]){
  if(Number(day)>cutoff&&BigInt(value.foreignDepositNoahEquivRao)!==(totals.get(JSON.stringify([i,Number(day)]))??0n))throw new Error('foreign total differs from dispositions');
 }});
}
export function evaluatePoints(text:string){
 const input=parseStrict(text) as Record<string, any>;
 const ordinary=Object.hasOwn(input,'pointsRules');
 if(ordinary&&input.schema!=='noah-points-fixture/3')throw new Error('ordinary onboarding requires points fixture schema /3');
 const rules={base:20,contribution:40,target:50,bonusAt:75,maturityStart:1,maturityDays:90,maturityCurve:2,consistency:40,consistencyDays:30,consistencyCurve:2,debtKeep:50,debtZero:2};
 if(ordinary&&canonicalJson(input.pointsRules)!==canonicalJson(rules))throw new Error('unsupported ordinary onboarding rules');
 const excluded=ordinary?input.pointsExcludedUids:[];
 if(!Array.isArray(excluded)||excluded.some((u:any)=>!Number.isSafeInteger(u)||u<0)||new Set(excluded).size!==excluded.length||excluded.some((u:number,i:number)=>i>0&&u<=excluded[i-1]))throw new Error('invalid owner exclusions');
 if(!ordinary&&Object.hasOwn(input,'pointsExcludedUids'))throw new Error('owner exclusions require ordinary onboarding');
 const clean={...input};delete clean.pointsRules;delete clean.pointsExcludedUids;delete clean.pointsMigration;delete clean.pointsReceipts;delete clean.pointsHotkeys;delete clean.pointsOwners;
 const ema=input.schema==='noah-points-fixture/3';
 const amended=ema||input.schema==='noah-points-fixture/2';
 if(!ema&&['pointsPricing','pointsPricingBoundary'].some(k=>Object.hasOwn(input,k)))throw new Error('old policy cannot carry EMA pricing');
 delete clean.pointsPricing;delete clean.pointsPricingBoundary;
 if(!amended&&['pointsReceipts','pointsHotkeys','pointsOwners'].some(k=>Object.hasOwn(input,k)))throw new Error('old policy cannot carry amended dispositions');
 const dispositions=amended?receipts(input.pointsReceipts,ema):null;
 if(ema)verifyEmaFixture(input,dispositions!);
 if(amended)clean.schema='noah-points-fixture/1';
 if(amended)reconcileReceipts(input,dispositions!);
 const f=loadFixture(JSON.stringify(clean),(amended||input.schema==='noah-points-fixture/1')?'noah-points-fixture/1':'noah-fixture/6',true);
 if(f.dials.targetFp!==500000000n||f.dials.ongoingHoldRao!==0n)throw new Error('points candidate requires 50% and no holding gate');
 if(f.evalDay>BigInt(Number.MAX_SAFE_INTEGER))throw new Error('unsafe day');
 const migration=input.pointsMigration;
 const balances=new Map<number,bigint>();let cutoff=-2n;
 if(migration!==undefined){
  const keys=(x:unknown,names:string[])=>typeof x==='object'&&x!==null&&!Array.isArray(x)&&Object.keys(x).sort().join(',')===names.sort().join(',');
  if((!amended&&input.schema!=='noah-points-fixture/1')||!keys(migration,['policySha256','cutoffDay','balances'])||!Number.isSafeInteger(migration.cutoffDay)||migration.cutoffDay<0||BigInt(migration.cutoffDay)>f.evalDay||typeof migration.policySha256!=='string'||! /^[0-9a-f]{64}$/.test(migration.policySha256)||!Array.isArray(migration.balances))throw new Error('invalid points migration');
  cutoff=BigInt(migration.cutoffDay);
  for(const row of migration.balances){
   if(!keys(row,['uid','coldkey','balanceRao'])||!Number.isSafeInteger(row.uid)||balances.has(row.uid)||typeof row.balanceRao!=='string'||! /^(0|-?[1-9][0-9]*)$/.test(row.balanceRao))throw new Error('invalid points migration row');
   const miner=f.miners.find(m=>m.uid===row.uid);
   if(!miner||row.coldkey!==miner.coldkey||miner.firstActivityDay>cutoff)throw new Error('points migration identity differs');
   balances.set(row.uid,BigInt(row.balanceRao));
  }
 }
 const scores=new Map<number,bigint>();
 const miners=f.miners.filter(m=>m.uid!==null).map(m=>{
  let earned=0n,contributed=0n,balance=0n,streak=0n,qualified=0n,missed=0n,points=0n,last=0n,bonus=0n,consistency=0n,maturity=0n;
  if(ordinary){points=200000n;last=points;maturity=10000n;}
  for(let day=m.firstActivityDay;day<=f.evalDay;day++){
   if(day===cutoff+1n&&balances.has(m.uid!))balance=balances.get(m.uid!)!;
   const before=m.days.get(day-1n),today=m.days.get(day)!;
   const pay=before?before.paidRao+before.payUnlockedRao:0n;
   const credit=today.noahDepositRao+today.foreignDepositNoahEquivRao,need=pay/2n,oldBalance=balance;
   earned+=pay;contributed+=credit;balance+=credit-need;
   if(balance<0n){missed++;streak=0n;points=missed===1n?last/2n:0n;bonus=0n;consistency=0n;continue;}
   missed=0n;
   if(need>0n||(qualified===0n&&oldBalance<0n&&earned>0n))qualified++;
   if(oldBalance<0n)streak=need>0n&&credit>=need?1n:0n;else if(need>0n)streak=credit>=need?streak+1n:0n;
   bonus=earned>0n?min(20n*Q,max(0n,80n*Q*contributed/earned-40n*Q)):0n;
   consistency=40n*Q*min(streak,30n)**2n/900n;
   maturity=qualified===0n?0n:100000n+900000n*min(qualified-1n,29n)/29n;
   if(ordinary){bonus=earned>0n?min(40n*Q,max(0n,160n*Q*contributed/earned-80n*Q)):0n;maturity=10000n+990000n*min(max(qualified-1n,0n),89n)**2n/7921n;}
   points=((ordinary?20n:40n)*Q+bonus+consistency)*maturity/Q;last=points;
  }
  if(f.evalDay===cutoff&&balances.has(m.uid!))balance=balances.get(m.uid!)!;
  if(excluded.includes(m.uid)){points=0n;last=0n;}
  scores.set(m.uid!,points);
  return {uid:m.uid!,coldkey:m.coldkey,scoreFp:points*1000n,baseFp:points*1000n,payWeightFp:points*1000n,pciFp:earned>0n?contributed*1000000000n/earned:0n,doorOpen:true,state:{earned,contributed,balance,streak,qualified,missed,points,last_compliant:last,contribution:bonus,consistency,baseline:(ordinary?20n:40n)*Q,maturity}};
 }).sort((a,b)=>a.uid-b.uid);
 return {schema:ema?'noah-points-output/3':amended?'noah-points-output/2':'noah-points-output/1',...(amended?{receiptDispositions:dispositions}:{}),...(ema?{pricingReferences:input.pointsPricing,pricingBoundary:input.pointsPricingBoundary}:{}),policy:ordinary?'noah-points-204040-90d-no-pool/5':ema?'noah-points-402040-30d/4':amended?'noah-points-402040-30d/3':'noah-points-402040-30d/2',fixture:f.name,evalDay:Number(f.evalDay),miners,weightsU16:Object.fromEntries(quantizeWeights(scores).weights)};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
 try{process.stdout.write(canonicalJson(evaluatePoints(readFileSync(process.argv[2],'utf8'))));}
 catch(e){console.error(String(e));process.exitCode=1;}
}
