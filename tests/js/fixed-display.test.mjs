import test from 'node:test';
import assert from 'node:assert/strict';
import {initial, apply, advance, STATES, SOURCES} from '../../tools/display_emulator_assets/model.mjs';
import {fixedFrame, standbyClock, REGIONS, FIXED_SCENARIOS, NOTICES} from '../../tools/display_emulator_assets/fixed-layout.mjs';

const measure = (size, text) => [...text].length * size * 0.55;
function ready(source = 'INTERNET_RADIO', data = {}) {
  let s = initial({baseTime:'23:59'});
  s = apply(s, {at:0, type:'SELECT', data:{source,station:data.station || 'Example'}});
  s = apply(s, {at:0, type:'METADATA', data:{generation:1, track:data.track ?? 'Song', artist:data.artist ?? 'Artist', other_info:''}});
  return apply(s, {at:2000, type:'STATE', data:{state:'PLAYING'}});
}

test('fixed regions fit the real raster without overlap', () => {
  const regions = Object.values(REGIONS);
  for (const r of regions) {
    assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.width <= 128 && r.y + r.height <= 100);
  }
  for (let i=1; i<regions.length; i++) assert.ok(regions[i-1].y + regions[i-1].height <= regions[i].y);
});
test('source/state/notice/zone matrix never claims hardware success and critical pages win', () => {
  let combinations = 0;
  for (const scenario of FIXED_SCENARIOS) for (const state of STATES) for (const notice of Object.keys(NOTICES)) {
    for (const zone of ['solo','master','member']) {
      const s = apply(ready(scenario.source, scenario), {at:2000,type:'STATE',data:{state}});
      const frame = fixedFrame(s, measure, {notice,zone});
      assert.equal(frame.firmwareExecution, false); assert.equal(frame.hardwareValidated, false);
      assert.ok(frame.title.length <= 2 && frame.artist.length <= 2);
      assert.ok(frame.title.every(t => measure(15,t) <= 120));
      assert.ok(frame.artist.every(t => measure(10,t) <= 120));
      if (['CRITICAL_BATTERY','SETUP','UPDATE'].includes(notice)) assert.equal(frame.kind, notice);
      else assert.equal(frame.kind, state);
      if (['CRITICAL_BATTERY','SETUP','UPDATE','ERROR','STANDBY','STOPPED','BUFFERING'].includes(frame.kind)) {
        assert.equal(frame.artist.length,0);
      }
      combinations++;
    }
  }
  assert.equal(combinations,1170);
});
test('all source classes honestly handle absent metadata, especially AUX', () => {
  for (const source of SOURCES) {
    const frame = fixedFrame(ready(source,{track:'',artist:''}),measure);
    assert.equal(frame.artist.join(''),'');
    assert.equal(frame.title.join(' '), source === 'AUX' ? 'AUX-Eingang' : 'Keine Titeldaten');
    assert.ok(!frame.fullText.includes('Song'));
  }
});
test('long title pages preserve content and keep station/clock regions fixed', () => {
  const title = 'A very long live recording of an orchestra playing in a concert hall';
  let s = ready('DLNA',{track:title});
  const first = fixedFrame(s,measure);
  const parts = [];
  for (let i=0; i<first.titlePages; i++) {
    const f = fixedFrame(advance(s,2000+i*5000),measure);
    assert.equal(f.header,first.header); assert.equal(f.titlePage,i+1);
    parts.push(...f.title);
  }
  assert.equal(parts.join(' '),title);
  assert.equal(fixedFrame(advance(s,60000),measure).clock,'00:00');
});
test('new source clears stale metadata; late previous-session updates cannot leak', () => {
  let s = apply(ready(),{at:3000,type:'SELECT',data:{source:'AUX',station:'AUX'}});
  s = apply(s,{at:3001,type:'METADATA',data:{generation:1,track:'OLD SONG',artist:'OLD ARTIST',other_info:''}});
  s = apply(s,{at:6000,type:'STATE',data:{state:'PLAYING'}});
  const f=fixedFrame(s,measure);
  assert.equal(s.ignored,1); assert.ok(!f.fullText.includes('OLD')); assert.deepEqual(f.title,['AUX-Eingang']);
});
test('volume zero is not mute, overlays expire, error and critical battery are never obscured', () => {
  let s=apply(ready(),{at:3000,type:'VOLUME',data:{value:0}});
  assert.equal(fixedFrame(s,measure).kind,'VOLUME'); assert.equal(s.muted,false);
  s=apply(s,{at:3500,type:'MUTE',data:{value:true}});
  assert.equal(fixedFrame(s,measure).header,'Stumm');
  assert.equal(fixedFrame(advance(s,5500),measure).badge,'Stumm');
  assert.equal(fixedFrame(s,measure,{notice:'CRITICAL_BATTERY'}).kind,'CRITICAL_BATTERY');
  s=apply(s,{at:3600,type:'STATE',data:{state:'ERROR'}});
  assert.equal(fixedFrame(s,measure).kind,'ERROR');
});
test('pause retains the song and never becomes standby; reduced-motion holds pages', () => {
  let s=ready('AIRPLAY',{track:'A very long title that needs more than two large lines to show'});
  s=apply(s,{at:8000,type:'STATE',data:{state:'PAUSED'}});
  assert.equal(fixedFrame(s,measure).kind,'PAUSED'); assert.equal(fixedFrame(s,measure).badge,'Pause');
  assert.equal(fixedFrame(advance(s,50000),measure).titlePage,1);
  s=apply(s,{at:9000,type:'STATE',data:{state:'PLAYING'}});
  assert.equal(fixedFrame(s,measure,{reducedMotion:true}).titlePage,1);
});
test('clock tick does not reset long-title page anchor; duplicate metadata does not reset it either', () => {
  let s=ready('DLNA',{track:'One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen'});
  s=advance(s,59000); const anchor=s.metadataChangedAt;
  s=apply(s,{at:60000,type:'METADATA',data:{generation:1,...s.metadata}});
  assert.equal(s.metadataChangedAt,anchor);
  assert.equal(fixedFrame(s,measure).titlePage,Math.floor((60000-anchor)/5000)%fixedFrame(s,measure).titlePages+1);
});
test('displayless device stays displayless, including critical overlays', () => {
  const s=ready();s.config.oled=false;
  assert.equal(fixedFrame(s,measure,{notice:'CRITICAL_BATTERY'}).kind,'NO_DISPLAY');
  assert.equal(fixedFrame(s,measure).clock,'');
});
test('malformed snapshots/options/metrics cannot invent a supported feature', () => {
  assert.throws(()=>fixedFrame(ready(),measure,{host:'example.invalid'}));
  assert.throws(()=>fixedFrame(ready(),measure,{fields:['title','title']}));
  assert.throws(()=>fixedFrame(ready(),measure,{notice:'REBOOT'}));
  assert.throws(()=>fixedFrame(ready(),()=>NaN));
  assert.throws(()=>fixedFrame({...ready(),volume:101},measure));
  assert.throws(()=>fixedFrame({...ready(),source:'EXECUTE'},measure));
  const invalidClock = ready(); invalidClock.config.baseTime = '24:99';
  assert.throws(()=>fixedFrame(invalidClock,measure));
});
test('independently bounded artist and extra information both remain pageable', () => {
  let s=ready();
  s=apply(s,{at:3000,type:'METADATA',data:{generation:1,track:'Song',artist:'a'.repeat(1024),other_info:'b'.repeat(1024)}});
  s=advance(s,5000);
  const options={fields:['artist','other']};
  const frame=fixedFrame(s,measure,options), parts=[];
  for(let i=0;i<frame.artistPages;i++) parts.push(...fixedFrame(advance(s,5000+i*7000),measure,options).artist);
  assert.equal(parts.join(''),'a'.repeat(1024)+'b'.repeat(1024));
});

test('standby clock is independent of playback fields and cannot expose stale song text', () => {
  let s=apply(ready(),{at:3000,type:'STATE',data:{state:'STANDBY'}});
  const frame=fixedFrame(s,measure,{fields:[]});
  assert.equal(frame.clock,'23:59');assert.equal(frame.header,'');assert.equal(frame.badge,'');
  assert.deepEqual(frame.title,[]);assert.deepEqual(frame.artist,[]);
  assert.ok(!frame.fullText.includes('Song'));
  assert.equal(fixedFrame(s,measure,{standby:{enabled:false}}).standby.visible,false);
  assert.equal(fixedFrame(s,measure,{standby:{brightness:0}}).clock,'');
  assert.equal(fixedFrame(s,measure,{standby:{synchronized:false}}).clock,'--:--');
  assert.equal(fixedFrame(s,measure,{standby:{enabled:false},notice:'CRITICAL_BATTERY'}).kind,'CRITICAL_BATTERY');
});

test('clock dims once per standby entry; repeated state/metadata does not brighten or pin it', () => {
  let s=apply(ready(),{at:3000,type:'STATE',data:{state:'STANDBY'}});
  assert.equal(standbyClock(s).brightness,45);
  s=advance(s,33000);assert.equal(standbyClock(s).brightness,20);
  s=apply(s,{at:34000,type:'STATE',data:{state:'STANDBY'}});
  assert.equal(s.stateChangedAt,3000);assert.equal(standbyClock(s).brightness,20);
  const first=standbyClock(s);
  s=apply(s,{at:35000,type:'METADATA',data:{generation:1,track:'Late',artist:'Late',other_info:''}});
  s=advance(s,63000);
  assert.notEqual(standbyClock(s).x,first.x);
  assert.equal(fixedFrame(s,measure,{reducedMotion:true}).standby.x,standbyClock(s).x);
  s=apply(s,{at:64000,type:'STATE',data:{state:'PLAYING'}});
  s=apply(s,{at:65000,type:'STATE',data:{state:'STANDBY'}});
  assert.equal(standbyClock(s).brightness,45);
});

test('standby all 1440 minute values, formats, bounded movement and midnight/noon', () => {
  for(let minute=0;minute<1440;minute++) {
    const hh=String(Math.floor(minute/60)).padStart(2,'0'),mm=String(minute%60).padStart(2,'0');
    const s=initial({baseTime:`${hh}:${mm}`});
    assert.equal(standbyClock(s).text,`${hh}:${mm}`);
    assert.equal(standbyClock(s,{format:12}).text,`${Math.floor(minute/60)%12||12}:${mm}`);
    assert.equal(standbyClock(s,{format:12}).suffix,minute<720?'AM':'PM');
  }
  const positions=new Set();
  for(let i=0;i<60;i++) {
    const c=standbyClock(advance(initial(),i*60000));positions.add(`${c.x}:${c.y}`);
    assert.ok(c.x>=4 && c.y>=3 && c.x+c.width<=124 && c.y+c.height<=99);
  }
  assert.equal(positions.size,9);
  assert.equal(standbyClock(advance(initial({baseTime:'23:59'}),60000),{format:12}).text,'12:00');
  for(const bad of [{format:13},{brightness:101},{brightness:NaN},{enabled:1},{synchronized:'yes'},{host:'example.invalid'}]) {
    assert.throws(()=>fixedFrame(ready(),measure,{standby:bad}));
  }
});
