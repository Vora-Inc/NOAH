// Independent retained snapshot-to-credit checks. No filesystem or chain reads.
import { createHash } from 'node:crypto';
import { canonicalJson } from './canonical.ts';
import { emaFractionCredit, priceFraction } from './ema_pricing.ts';
const codes: Record<number,string>={483:'0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2',488:'0x3147daab804046184359d8a929cb333d75f490323b9e1327887a172c64eafa94'};
export const EMA_SOURCES=['SubnetMovingPrice'];
const hash=(v:unknown)=>typeof v==='string'&&/^0x[0-9a-f]{64}$/.test(v);
const sha=(v:unknown)=>typeof v==='string'&&/^[0-9a-f]{64}$/.test(v);
function need(ok:unknown):asserts ok {if(!ok)throw new Error('invalid EMA reference proof');}
function keys(v:any,names:string[]){need(v!==null&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).sort().join(',')===[...names].sort().join(','));}
function integer(v:unknown,low=0,high=Number.MAX_SAFE_INTEGER):asserts v is number {need(Number.isSafeInteger(v)&&Number(v)>=low&&Number(v)<=high);}
function little(raw:any,width:number,signed=false):bigint{
 need(typeof raw==='string'&&new RegExp('^0x[0-9a-fA-F]{'+width*2+'}$').test(raw));
 let n=0n;for(let i=width-1;i>=0;i--)n=n*256n+BigInt(parseInt(raw.slice(2+i*2,4+i*2),16));
 return signed&&n>=(1n<<BigInt(width*8-1))?n-(1n<<BigInt(width*8)):n;
}
function generation(v:any,block:number){
 need(Array.isArray(v)&&v.length===4);
 const member=v[0]===null?0n:little(v[0],1),birth=v[1]===null?0n:little(v[1],8),counter=v[2]===null?0n:little(v[2],8),mechanism=v[3]===null?0n:little(v[3],2);
 need(member<=1n&&birth<=BigInt(block)&&counter<(1n<<64n)-1n&&mechanism<=1n);
 return {member:member===1n,birth,counter};
}
export function proofDigest(p:any){return createHash('sha256').update(canonicalJson(p)).digest('hex');}
export function validateEmaProof(p:any):string{
 keys(p,['schema','genesis','native_netuid','asset_netuid','receipt','reference','runtime','parent_runtime','metadata_sha256','receipt_native','receipt_asset','parent_native','parent_asset','method','ema_source','ema_raw','spot_raw','prices','status','reason']);
 need(p.schema==='noah-token-ema-reference/1'&&p.method==='chain-ema-spot-minimum/1'&&EMA_SOURCES.includes(p.ema_source));
 need(hash(p.genesis)&&sha(p.metadata_sha256));integer(p.native_netuid,1,65535);integer(p.asset_netuid,0,65535);need(p.native_netuid!==p.asset_netuid);
 keys(p.receipt,['block','block_hash','parent_hash','timestamp']);integer(p.receipt.block,1);integer(p.receipt.timestamp);need(hash(p.receipt.block_hash)&&hash(p.receipt.parent_hash));
 keys(p.reference,['block','block_hash']);need(p.reference.block===p.receipt.block-1&&p.reference.block_hash===p.receipt.parent_hash);
 for(const runtime of [p.runtime,p.parent_runtime]){keys(runtime,['spec_version','code_hash','transaction_version']);integer(runtime.spec_version);need(Object.hasOwn(codes,runtime.spec_version)&&runtime.code_hash===codes[runtime.spec_version]&&runtime.transaction_version===1);}
 let reason:string|null=null;
 for(const role of ['native','asset']){
  if(role==='asset'&&p.asset_netuid===0){need(p.receipt_asset===null&&p.parent_asset===null);continue;}
  const now=generation(p['receipt_'+role],p.receipt.block),before=generation(p['parent_'+role],p.reference.block);
  need(now.member);
  if(!before.member){reason??='reference-subnet-absent';continue;}
  // A change inside the receipt block is ambiguous, never terminal no-credit.
  need(now.birth===before.birth&&now.counter===before.counter&&now.birth<BigInt(p.receipt.block));
 }
 if(reason!==null){need(p.prices===null&&p.ema_raw===null&&p.spot_raw===null);}
 else{
  keys(p.ema_raw,['native','asset']);keys(p.spot_raw,['native','asset']);keys(p.prices,['ema_asset','ema_native','spot_asset','spot_native']);
  for(const role of ['native','asset']){
   const root=role==='asset'&&p.asset_netuid===0;
   if(root)need(p.ema_raw.asset===null&&p.spot_raw.asset===null);
   for(const raw of [p.ema_raw[role],p.spot_raw[role]])if(raw!==null)need(typeof raw==='string'&&/^0x[0-9a-f]+$/.test(raw));
   const e=root?1n:(p.ema_raw[role]===null?0n:little(p.ema_raw[role],16,true));
   const s=root?1n:little(p.spot_raw[role],8);
   need(e>=0n);
   const [en,ed]=priceFraction(p.prices['ema_'+role]),[sn,sd]=priceFraction(p.prices['spot_'+role]);
   need(en===e&&ed===(root?1n:1n<<32n)&&sn===s&&sd===(root?1n:1000000000n));
   if(e===0n||s===0n)reason??=role==='native'?'native-reference-price-zero':'foreign-reference-price-zero';
  }
 }
 need(p.status===(reason===null?'supported':'unsupported')&&p.reason===reason);
 need(Buffer.byteLength(canonicalJson(p))<65536);
 return proofDigest(p);
}
export function verifyEmaFixture(input:any,rows:any[]):void{
 const boundary=input.pointsPricingBoundary;
 keys(boundary,['anchor','effectiveBlock','policySha256','predecessorPolicySha256','emaSource','genesis','nativeNetuid']);
 keys(boundary.anchor,['block','hash']);integer(boundary.anchor.block,1,Number.MAX_SAFE_INTEGER-1);
 need(hash(boundary.anchor.hash)&&boundary.effectiveBlock===boundary.anchor.block+1&&sha(boundary.policySha256)&&sha(boundary.predecessorPolicySha256)&&EMA_SOURCES.includes(boundary.emaSource));
 need(hash(boundary.genesis));integer(boundary.nativeNetuid,1,65535);
 const proofs=input.pointsPricing;need(proofs!==null&&typeof proofs==='object'&&!Array.isArray(proofs));
 const used=new Set<string>();
 for(const row of rows){
  const pin=row.reference_proof.sha256;
  if(row.block<boundary.effectiveBlock){need(row.reason!=='ema-spot-reference'&&!Object.hasOwn(proofs,pin));continue;}
  need(Object.hasOwn(proofs,pin));const proof=proofs[pin];used.add(pin);
  need(validateEmaProof(proof)===pin&&proof.ema_source===boundary.emaSource);
  need(row.receipt_id.split('/')[0]===proof.genesis&&row.block===proof.receipt.block&&row.block_hash===proof.receipt.block_hash&&row.token_netuid===proof.asset_netuid);
  need(proof.native_netuid===boundary.nativeNetuid&&proof.genesis===boundary.genesis);
  const anchorTs=BigInt(input.dayEpochTs);need(BigInt(proof.receipt.timestamp)>=anchorTs&&Number((BigInt(proof.receipt.timestamp)-anchorTs)/86400n)===row.day);
  if(row.block===boundary.effectiveBlock)need(proof.receipt.parent_hash===boundary.anchor.hash);
  if(proof.status==='supported'){
   need(row.status==='accepted'&&row.reason==='ema-spot-reference');
   const p=proof.prices;need(BigInt(row.credit_rao)===emaFractionCredit(BigInt(row.amount_rao),p.ema_asset,p.ema_native,p.spot_asset,p.spot_native));
  }else need(row.status==='unsupported'&&row.reason===proof.reason&&row.credit_rao==='0');
 }
 need(Object.keys(proofs).every(k=>used.has(k))&&Object.keys(proofs).length===used.size);
}
