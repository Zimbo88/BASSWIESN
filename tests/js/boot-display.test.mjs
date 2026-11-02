import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {BOOT, composeBoot, progressPreview, wordmarkPixels} from '../../tools/display_emulator_assets/boot-layout.mjs';

const mark=JSON.parse(readFileSync(new URL('../../tools/display_emulator_assets/boot-mark.json',import.meta.url)));

test('all 200 progress frames match native ARM/VFP observations, not an invented percentage',()=>{
  const hash=createHash('sha256');
  for (const fullRange of [false,true]) for (let progress=1;progress<=100;progress++) {
    hash.update(progressPreview(progress,fullRange));
  }
  // Derived hash only: no firmware bytes. Exact-image private native harness
  // recorded PROGRESS 1..100 followed by PROGRESS_100 1..100, each 12800 bytes.
  assert.equal(hash.digest('hex'),'26fff5aa2e3cfbb49a44ebe70b56830de02e20ec40053edeb65e80963c5e4c8b');
});

test('logo stays strictly above status and cannot mutate any other pixel',()=>{
  const mask=new Set(wordmarkPixels(mark));
  assert.equal(mask.size,596);
  for(const index of mask) assert.ok(Math.floor(index/128)<32);
  for(const full of [false,true]) for(let value=1;value<=100;value++) {
    const frame=progressPreview(value,full),before=frame.slice(),result=composeBoot(frame,mark);
    assert.deepEqual(frame,before,'input must not mutate');
    assert.deepEqual(result.slice(40*128,60*128),frame.slice(40*128,60*128));
    for(let i=0;i<BOOT.bytes;i++) assert.equal(result[i],mask.has(i)?255:frame[i]);
    assert.deepEqual(composeBoot(result,mark),result);
  }
});

test('arbitrary existing frame is preserved outside the wordmark mask',()=>{
  const frame=Uint8Array.from({length:BOOT.bytes},(_,i)=>(i*17)%256);
  const result=composeBoot(frame,mark),mask=new Set(wordmarkPixels(mark));
  for(let i=0;i<BOOT.bytes;i++) if(!mask.has(i)) assert.equal(result[i],frame[i]);
});

test('typed-array subclasses cannot turn composition into input mutation',()=>{
  const frame=Buffer.alloc(BOOT.bytes,73),before=Buffer.from(frame);
  const result=composeBoot(frame,mark);
  assert.deepEqual(frame,before);
  assert.notEqual(result[13*128+13],73);
});

test('zero does not create a fake blank frame or a new boot event',()=>{
  assert.equal(progressPreview(0),null);
  assert.equal(progressPreview(0,true),null);
});

test('bounded inputs reject invalid sizes, progress and wordmark geometry',()=>{
  for(const value of [-1,101,Infinity,NaN,1.2,'5',null]) assert.throws(()=>progressPreview(value));
  for(const value of [1,'false',null,{}]) assert.throws(()=>progressPreview(50,value));
  for(const frame of [[],new Uint8Array(12799),new Uint8Array(12801),new Uint16Array(12800)]) {
    assert.throws(()=>composeBoot(frame,mark));
  }
  for(const change of [{schema:2},{text:'<script>'},{origin:[9,35]},{shear:[0]},
    {glyphs:{...mark.glyphs,B:['11111']}},{shear:[-1,0,0,0,0,0,0]}]) {
    const frame=new Uint8Array(BOOT.bytes);
    assert.throws(()=>composeBoot(frame,{...mark,...change}));
    assert.ok(frame.every(value=>value===0));
  }
});
