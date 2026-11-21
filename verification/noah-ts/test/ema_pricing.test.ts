import test from 'node:test';
import assert from 'node:assert/strict';
import { emaCredit } from '../src/ema_pricing.ts';
const R = 1_000_000_000n;

test('stable, crash, pump, and native denominator movement use complete ratios', () => {
  for (const [spotAsset, spotNative, expected] of [[10n,5n,50n],[1n,5n,5n],[20n,5n,50n],[10n,25n,10n],[10n,2n,50n]]) {
    assert.equal(emaCredit(25n*R,10n,5n,spotAsset,spotNative),expected*R);
  }
});
test('independently minimizing numerator and denominator is not the rule', () => {
  assert.equal(emaCredit(25n,10n,5n,1n,1n),25n);
  assert.notEqual(emaCredit(25n,10n,5n,1n,1n),25n*1n/5n);
});
test('root numerator is exactly one unit in each price scale', () => {
  assert.equal(emaCredit(5n*R/2n,100n,5n,1000n,50n),50n*R);
});
test('identical native ratios credit received units one-for-one at any prices', () => {
  assert.equal(emaCredit(50n*R,7n,7n,123n,123n),50n*R);
});
test('single final floor preserves credit lost by intermediate TAO rounding', () => {
  assert.equal(emaCredit(1n,2n,3n,9n,10n),0n);
  assert.equal(emaCredit(2n,2n,3n,9n,10n),1n);
  assert.equal(emaCredit(1n,1n,1n,1n,1n),1n);
});
test('prices can have distinct EMA versus spot scales without losing exactness', () => {
  assert.equal(emaCredit(11n,2n**100n,3n*2n**100n,1000n,2000n),3n);
});
test('large integers never pass through Number or floating-point arithmetic', () => {
  const q=2n**256n-1n;
  assert.equal(emaCredit(q,3n,2n,7n,4n),q*3n/2n);
});
test('same-reference splitting cannot increase credit', () => {
  for (let a=0n;a<30n;a++) for (let b=0n;b<30n;b++) {
    assert(emaCredit(a,7n,13n,3n,4n)+emaCredit(b,7n,13n,3n,4n)<=emaCredit(a+b,7n,13n,3n,4n));
  }
});
test('exact cap obeys both rational upper bounds', () => {
  for(let ea=1n;ea<6n;ea++)for(let en=1n;en<6n;en++)for(let pa=1n;pa<6n;pa++)for(let pn=1n;pn<6n;pn++){
    const value=emaCredit(29n,ea,en,pa,pn);
    assert(value*en<=29n*ea);assert(value*pn<=29n*pa);
    const selected=ea*pn<=pa*en?[ea,en]:[pa,pn];
    assert((value+1n)*selected[1]>29n*selected[0]);
  }
});
test('zero, negative, missing and number prices refuse rather than fabricate credit', () => {
  for(const bad of [0n,-1n,undefined,null,1,NaN,'1'])for(let index=0;index<4;index++){
    const values:any[]=[1n,1n,1n,1n];values[index]=bad;
    assert.throws(()=>emaCredit(1n,values[0],values[1],values[2],values[3]));
  }
  for(const bad of [-1n,1,undefined])assert.throws(()=>emaCredit(bad as bigint,1n,1n,1n,1n));
});

test('four fractional prices cancel different encodings before the final floor', async () => {
  const {emaFractionCredit}=await import('../src/ema_pricing.ts');
  const fraction=(n:string,d:string)=>({numerator:n,denominator:d});
  assert.equal(emaFractionCredit(25n*R,fraction('1','10'),fraction('5','100'),fraction('10','100'),fraction('1','20')),50n*R);
  assert.equal(emaFractionCredit(25n*R,fraction('1','10'),fraction('5','100'),fraction('1','100'),fraction('1','20')),5n*R);
  for(const bad of [{numerator:'01',denominator:'1'},{numerator:'-1',denominator:'1'},{numerator:'1',denominator:'0'},{numerator:1,denominator:'1'},{numerator:'1',denominator:'1',extra:true}]) {
    assert.throws(()=>emaFractionCredit(1n,bad,fraction('1','1'),fraction('1','1'),fraction('1','1')));
  }
});
