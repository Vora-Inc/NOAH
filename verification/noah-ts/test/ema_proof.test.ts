import test from 'node:test';
import assert from 'node:assert/strict';
import { validateEmaProof,verifyEmaFixture,proofDigest } from '../src/ema_proof.ts';
const H='0x'+'a'.repeat(64), P='0x'+'b'.repeat(64), G='0x'+'c'.repeat(64);
function raw(n:bigint,width:number){return '0x'+Array.from({length:width},(_,i)=>Number((n>>BigInt(i*8))&255n).toString(16).padStart(2,'0')).join('');}
function proof():any{
 const marker=['0x01',raw(10n,8),raw(1n,8),'0x0100'];
 const runtime={spec_version:483,transaction_version:1,code_hash:'0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2'};
 return {schema:'noah-token-ema-reference/1',method:'chain-ema-spot-minimum/1',ema_source:'SubnetMovingPrice',genesis:G,native_netuid:2,asset_netuid:3,receipt:{block:100,block_hash:H,parent_hash:P,timestamp:86400},reference:{block:99,block_hash:P},runtime,parent_runtime:{...runtime},metadata_sha256:'d'.repeat(64),receipt_native:[...marker],receipt_asset:[...marker],parent_native:[...marker],parent_asset:[...marker],ema_raw:{native:raw(1n<<30n,16),asset:raw(1n<<31n,16)},spot_raw:{native:raw(250000000n,8),asset:raw(500000000n,8)},prices:{ema_native:{numerator:String(1n<<30n),denominator:String(1n<<32n)},ema_asset:{numerator:String(1n<<31n),denominator:String(1n<<32n)},spot_native:{numerator:'250000000',denominator:'1000000000'},spot_asset:{numerator:'500000000',denominator:'1000000000'}},status:'supported',reason:null};
}
function fixture(p:any){const pin=proofDigest(p);return {dayEpochTs:'0',pointsPricing:{[pin]:p},pointsPricingBoundary:{anchor:{block:99,hash:P},effectiveBlock:100,policySha256:'e'.repeat(64),predecessorPolicySha256:'f'.repeat(64),emaSource:'SubnetMovingPrice',genesis:G,nativeNetuid:2}};}
function row(p:any):any{const pin=proofDigest(p);return {receipt_id:G+'/'+H+'/5',block:100,block_hash:H,day:1,event_ordinal:5,token_netuid:3, amount_rao:'25000000000',credit_rao:'50000000000',status:'accepted',reason:'ema-spot-reference',reference_proof:{path:'points-reference-proofs/'+pin+'.json',sha256:pin}};}
test('raw authenticated snapshot yields independently recomputed credit',()=>{const p=proof();assert.equal(validateEmaProof(p),proofDigest(p));verifyEmaFixture(fixture(p),[row(p)]);});
test('price representation, method, raw negative/zero, runtime and parent tampering refuse',()=>{
 const mutations=[(p:any)=>p.prices.ema_asset.numerator='999',(p:any)=>p.ema_source='SubnetFastMovingPrice',(p:any)=>p.ema_raw.asset=raw((1n<<128n)-1n,16),(p:any)=>p.spot_raw.asset=raw(0n,8),(p:any)=>p.reference.block=98,(p:any)=>p.reference.block_hash=H,(p:any)=>p.runtime.code_hash=H,(p:any)=>p.metadata_sha256='bad',(p:any)=>p.extra=true,(p:any)=>p.receipt_asset[1]=raw(100n,8),(p:any)=>p.parent_asset[2]=raw(2n,8)];
 for(const mutate of mutations){const p=proof();mutate(p);assert.throws(()=>validateEmaProof(p));}
});
test('missing slow EMA is authenticated zero, but does not become positive credit',()=>{
 const p=proof();p.ema_raw.asset=null;p.prices.ema_asset.numerator='0';p.status='unsupported';p.reason='foreign-reference-price-zero';validateEmaProof(p);
 const r=row(p);Object.assign(r,{status:'unsupported',reason:p.reason,credit_rao:'0'});verifyEmaFixture(fixture(p),[r]);r.credit_rao='1';assert.throws(()=>verifyEmaFixture(fixture(p),[r]));
});
test('parent absence is terminal, same-block present reuse remains uncertain',()=>{
 const p=proof();p.parent_asset=[null,null,null,null];p.receipt_asset[1]=raw(100n,8);p.ema_raw=p.spot_raw=p.prices=null;p.status='unsupported';p.reason='reference-subnet-absent';validateEmaProof(p);
 p.receipt_native[0]=null;assert.throws(()=>validateEmaProof(p));
});
test('native absence cannot hide invalid asset state',()=>{const p=proof();p.parent_native=[null,null,null,null];p.receipt_asset[0]=null;p.ema_raw=p.spot_raw=p.prices=null;p.status='unsupported';p.reason='reference-subnet-absent';assert.throws(()=>validateEmaProof(p));});
test('root uses unit numerators, no nonexistent root raw EMA',()=>{const p=proof();p.asset_netuid=0;p.receipt_asset=p.parent_asset=null;p.ema_raw.asset=p.spot_raw.asset=null;for(const k of ['ema_asset','spot_asset'])p.prices[k]={numerator:'1',denominator:'1'};validateEmaProof(p);p.prices.ema_asset.numerator='2';assert.throws(()=>validateEmaProof(p));});
test('fixture binds denominator, chain, parent boundary, row credit and date',()=>{
 const p=proof();for(const mutate of [(f:any)=>f.pointsPricingBoundary.nativeNetuid=82,(f:any)=>f.pointsPricingBoundary.genesis=H,(f:any)=>f.pointsPricingBoundary.anchor.hash=H,(f:any)=>f.pointsPricingBoundary.effectiveBlock=101,(f:any)=>f.pointsPricingBoundary.emaSource='SubnetFastMovingPrice',(f:any)=>f.dayEpochTs='86400']){const f=fixture(p);mutate(f);assert.throws(()=>verifyEmaFixture(f,[row(p)]));}
 for(const mutate of [(r:any)=>r.credit_rao='50000000001',(r:any)=>r.token_netuid=4, (r:any)=>r.block_hash=P,(r:any)=>r.status='unsupported',(r:any)=>r.reason='daily-reference']){const r=row(p);mutate(r);assert.throws(()=>verifyEmaFixture(fixture(p),[r]));}
});
test('late-discovered preboundary receipt keeps old rule; no unused proofs',()=>{const p=proof(),f=fixture(p),r=row(p);f.pointsPricing={};r.block=98;r.reason='daily-reference';verifyEmaFixture(f,[r]);r.reason='ema-spot-reference';assert.throws(()=>verifyEmaFixture(f,[r]));assert.throws(()=>verifyEmaFixture(fixture(p),[]));});

test('unreviewed EMA runtime486 refuses even with known lifecycle code',()=>{const p=proof();p.parent_runtime={spec_version:486,transaction_version:1,code_hash:'0x49781ac4cfda0ac6cf6e53cfceea7c728394d9c28da7881f47e3fbfbc239800f'};assert.throws(()=>validateEmaProof(p));});
