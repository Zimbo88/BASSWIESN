import test from 'node:test';
import assert from 'node:assert/strict';
import {initial, apply, advance, clockText, pageKind, scrollOffset, configuration,
  demoEvents, replay, validateReplay, FIELDS} from '../../tools/display_emulator_assets/model.mjs';

const selected = () => apply(initial(), {at:0,type:'SELECT',data:{source:'INTERNET_RADIO',station:'Example'}});
const meta = (at, track, generation=1) => ({at,type:'METADATA',data:{generation,track,artist:'Artist',other_info:''}});
const stateEvent = (at,state) => ({at,type:'STATE',data:{state}});

test('coalescing holds text until deadline, latest update wins without starvation',()=>{
  let s=apply(selected(),meta(0,'First'));
  s=apply(s,meta(1900,'Second'));
  assert.equal(advance(s,1999).metadata.track,'');
  assert.equal(advance(s,2000).metadata.track,'Second');
  assert.equal(advance(s,2000).metadataChangedAt,2000);
});
test('duplicate metadata does not restart the coalescing/scroll anchor',()=>{
  let s=advance(apply(selected(),meta(0,'First')),2000);
  s=apply(s,meta(3000,'First'));
  assert.equal(s.pending,null);assert.equal(s.metadataChangedAt,2000);
});
test('empty snapshot clears old fields rather than inventing a title',()=>{
  let s=advance(apply(selected(),meta(0,'First')),2000);
  s=apply(s,{at:2000,type:'METADATA',data:{generation:1,track:'',artist:'',other_info:''}});
  assert.deepEqual(advance(s,4000).metadata,{track:'',artist:'',other_info:''});
});
test('source changes clear pending metadata and reject a stale-session update',()=>{
  let s=apply(selected(),meta(0,'Old'));
  s=apply(s,{at:500,type:'SELECT',data:{source:'AIRPLAY',station:'New'}});
  s=apply(s,meta(700,'Late old title',1));
  s=advance(s,4000);
  assert.equal(s.metadata.track,'');assert.equal(s.pending,null);assert.equal(s.ignored,1);
});
test('pause/resume is not standby, and neither clears song metadata',()=>{
  let s=advance(apply(selected(),meta(0,'Song')),2000);
  s=apply(s,stateEvent(2500,'PAUSED'));assert.equal(pageKind(s),'PAUSED');
  s=apply(s,stateEvent(14000,'PLAYING'));assert.equal(pageKind(s),'PLAYING');assert.equal(s.metadata.track,'Song');
});
test('volume expires exactly; error priority is explicit model policy',()=>{
  let s=apply(selected(),stateEvent(0,'PLAYING'));
  s=apply(s,{at:100,type:'VOLUME',data:{value:0}});
  assert.equal(pageKind(advance(s,2099)),'VOLUME');assert.equal(pageKind(advance(s,2100)),'PLAYING');
  assert.equal(pageKind(apply(s,stateEvent(200,'ERROR'))),'ERROR');
  assert.equal(pageKind(apply(s,stateEvent(200,'STANDBY'))),'STANDBY');
});
test('non-display profile cannot suddenly acquire a simulated OLED',()=>{
  assert.equal(pageKind(initial({oled:false})),'NO_DISPLAY');
});
test('clock rolls over midnight based on deterministic model time',()=>{
  let s=initial({baseTime:'23:59'});assert.equal(clockText(s),'23:59');
  s=advance(s,60000);assert.equal(clockText(s),'00:00');
});
test('backward time is rejected; replay provides deterministic seeking',()=>{
  assert.throws(()=>advance(advance(initial(),1000),999));
  const events=demoEvents();assert.deepEqual(replay({},events,50000),replay({},events,50000));
  assert.equal(replay({},events,1000).metadata.track,'');
});
test('scroll is bounded, begins at start, pauses at both ends and returns',()=>{
  assert.equal(scrollOffset(200,100,1000,20,2000),0);
  assert.equal(scrollOffset(200,100,4500,20,2000),-50);
  assert.equal(scrollOffset(200,100,8000,20,2000),-100);
  assert.equal(scrollOffset(200,100,14000,20,2000),0);
  assert.equal(scrollOffset(50,100,9999,20,2000),0);
  assert.equal(scrollOffset(200,100,9999,0,2000),0);
  for(let i=0;i<30000;i+=17){const x=scrollOffset(200,100,i,20,2000);assert.ok(x>=-100 && x<=0);}
});
test('extreme/malformed calibration is rejected instead of allocating huge canvas',()=>{
  for(const c of [{width:999999},{font:NaN},{oled:1},{speed:-1},{baseTime:'25:00'},{unknown:1}]) assert.throws(()=>configuration(c));
});
test('replay import is bounded and rejects commands, URLs, unknown fields, wrong order',()=>{
  const valid={schema:1,kind:'DISPLAY_BEHAVIOR_MODEL',config:{},presentation:{fields:[...FIELDS],order:[...FIELDS],mode:'CUSTOM',concept:false},events:demoEvents(),until:60000};
  assert.equal(validateReplay(valid).events.length,11);
  for(const value of [null,{}, {...valid,until:Infinity}, {...valid,host:'example.invalid'},
    {...valid,events:[{at:0,type:'REBOOT',data:{}}]}, {...valid,events:[...demoEvents()].reverse()},
    {...valid,events:Array(501).fill(demoEvents()[0])}, {...valid,events:[meta(0,'x'.repeat(1025))]},
    {...valid,presentation:{...valid.presentation,order:['title','title']}},
    {...valid,events:[{...demoEvents()[0],data:{...demoEvents()[0].data,url:'http://example.invalid'}}]}]) {
    assert.throws(()=>validateReplay(value));
  }
});
test('JSON roundtrip preserves every state at the same virtual time',()=>{
  const original={schema:1,kind:'DISPLAY_BEHAVIOR_MODEL',config:{coalesce:1000},
    presentation:{fields:['title','clock'],order:[...FIELDS],mode:'CUSTOM',concept:false},events:demoEvents(),until:60000};
  const value=validateReplay(JSON.parse(JSON.stringify(original)));
  for(const t of [0,999,1000,5000,9000,28000,42000,60000]) {
    assert.deepEqual(replay(original.config,original.events,t),replay(value.config,value.events,t));
  }
});
