import {FIELDS, DEFAULTS, SCENARIOS, configuration, initial, replay, clockText, pageKind,
  scrollOffset, validateEvent, validateReplay, demoEvents, preferences} from './model.mjs';
import {fixedFrame, drawFixedFrame} from './fixed-layout.mjs';

const $ = id => document.getElementById(id);
const labels = {station:'Sender', artist:'Interpret', title:'Titel', clock:'Uhr', other:'Zusatzinfo'};
let config = configuration();
let presentation = {fields: FIELDS.slice(0,4), order: [...FIELDS], mode:'CUSTOM', concept:false};
let events = [], state = initial(config), running = false, wall = 0, animation = 0;
let projected = {track:'', artist:''}, signature = '', request = 0, controller = null;
let composeStatus = '', lastPaint = 0;
let messageRevision = 0;
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

function message(text, error = false) {
  messageRevision += 1;
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}

function fieldControls() {
  $('fields').replaceChildren();
  presentation.order.forEach((field, i) => {
    const row = document.createElement('div'); row.className = 'field';
    const label = document.createElement('label');
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = presentation.fields.includes(field);
    input.disabled = presentation.mode !== 'CUSTOM'; input.dataset.field = field;
    input.addEventListener('change', () => {
      presentation.fields = input.checked ? [...presentation.fields, field] : presentation.fields.filter(f => f !== field);
      refresh();
    });
    label.append(input, document.createTextNode(labels[field])); row.append(label);
    for (const [delta, symbol, text] of [[-1,'↑','nach oben'],[1,'↓','nach unten']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = symbol;
      button.setAttribute('aria-label', `${labels[field]} ${text}`);
      button.disabled = i + delta < 0 || i + delta >= FIELDS.length || presentation.mode !== 'CUSTOM';
      button.addEventListener('click', () => {
        [presentation.order[i], presentation.order[i+delta]] = [presentation.order[i+delta], presentation.order[i]];
        fieldControls(); refresh();
        $('fields').querySelector(`[data-field="${field}"]`).focus();
      }); row.append(button);
    }
    $('fields').append(row);
  });
}

function setRunning(value) {
  running = value; wall = performance.now(); $('run').textContent = running ? 'Simulation anhalten' : 'Simulation starten';
}

function seek(to) {
  state = replay(config, events, Math.max(0, Math.min(3600000, Math.round(to))));
  $('position').max = String(Math.max(120000, state.now, ...events.map(e=>e.at)));
  $('position').value = state.now; refresh();
}

function dispatch(type, data) {
  try {
    const event = validateEvent({at:state.now, type, data});
    const past = events.filter(e=>e.at <= state.now);
    if (past.length >= 500) throw Error('500 Ereignisse erreicht. Verlauf exportieren und zurücksetzen.');
    events = [...past, event]; seek(state.now); timeline();
  } catch (error) { message(error.message, true); }
}

function timeline() {
  $('timeline').replaceChildren();
  for (const event of events) {
    const line = document.createElement('li');
    line.textContent = `${(event.at/1000).toFixed(1)} s · ${event.type} · ${JSON.stringify(event.data)}`;
    $('timeline').append(line);
  }
}

function chooseScenario(index) {
  setRunning(false);
  const scenario = SCENARIOS[index];
  for (const key of ['source','station','artist','track','other_info']) $(key).value = scenario[key];
  events = demoEvents(scenario).slice(0,3);
  seek(3500); timeline();
}

async function compose() {
  const payload = {station:state.station, metadata:state.metadata, fields:presentation.fields,
    order:presentation.order, mode:presentation.mode, clock:clockText(state)};
  const key = JSON.stringify(payload);
  if (key === signature) return;
  signature = key; const id = ++request, statusRevision = messageRevision;
  controller?.abort(); controller = new AbortController();
  projected = {track:'', artist:''}; composeStatus = 'Text wird lokal berechnet …';
  $('payload').textContent = composeStatus;
  try {
    const response = await fetch('/api/compose', {method:'POST', headers:{'Content-Type':'application/json'},
      body:key, signal:controller.signal, cache:'no-store'});
    if (!response.ok) throw Error(`Lokale Textaufbereitung: HTTP ${response.status}`);
    const result = await response.json();
    if (id !== request) return;
    if (typeof result.track !== 'string' || typeof result.artist !== 'string' || result.contract?.firmware_execution !== false) {
      throw Error('Ungültiger lokaler Textvertrag');
    }
    projected = result; composeStatus = '';
    $('payload').textContent = JSON.stringify({track:result.track, artist:result.artist,
      characters:result.characters, presentation:'provider text; physical layout unverified'}, null, 2);
    // A delayed composition must not erase a newer import/validation outcome.
    if (statusRevision === messageRevision) message('Textvertrag berechnet. Physisches Layout bleibt ein unkalibriertes Modell.');
    draw();
  } catch (error) {
    if (id !== request || error.name === 'AbortError') return;
    signature = ''; composeStatus = 'Lokale Textaufbereitung nicht verfügbar';
    $('payload').textContent = 'Keine aktuelle Textausgabe – es wird kein alter Titel als neuer ausgegeben.';
    if (statusRevision === messageRevision) message(error.message, true);
  }
}

function line(ctx, text, x, y, width, font, elapsed = 0, moving = true) {
  if (width <= 0) return;
  ctx.save(); ctx.font = `${font}px monospace`; ctx.textBaseline = 'top';
  ctx.beginPath(); ctx.rect(x,y,width,font*1.35); ctx.clip();
  const offset = moving && !reducedMotion ? scrollOffset(ctx.measureText(text).width, width, elapsed, config.speed, config.hold) : 0;
  ctx.fillText(text, x + offset, y); ctx.restore();
}

function renderCanvas(id, concept = false) {
  const canvas = $(id), {width:w, height:h, font:f} = config;
  if (concept) {
    const metrics = document.createElement('canvas').getContext('2d');
    const frame = fixedFrame(state, (size, text) => {
      metrics.font = `${size}px Arial, sans-serif`; return metrics.measureText(text).width;
    }, {fields: presentation.fields, reducedMotion});
    drawFixedFrame(canvas, frame);
    return;
  }
  if (canvas.width !== w || canvas.height !== h) { canvas.width=w; canvas.height=h; }
  const ctx = canvas.getContext('2d');
  ctx.fillStyle='#000'; ctx.fillRect(0,0,w,h); ctx.fillStyle='#eef8f6';
  // Virtual time, not the asynchronous HTTP arrival, determines scroll position.
  // Replaying the same input at the same model time produces the same frame.
  const clockAnchor = presentation.mode === 'CUSTOM' && presentation.fields.includes('clock') ? Math.floor(state.now/60000)*60000 : 0;
  const kind = pageKind(state), age = Math.max(0,state.now-Math.max(state.metadataChangedAt,clockAnchor));
  const small = Math.max(8,Math.round(f*.58)), pad = 8;
  if (kind === 'NO_DISPLAY') {
    canvas.setAttribute('aria-label', 'Kein Display für die gewählte Geräteklasse'); return;
  }
  if (kind === 'STANDBY') {
    line(ctx, clockText(state), pad, (h-f*1.7)/2, w-16, Math.round(f*1.7),0,false);
  } else if (kind === 'ERROR') {
    line(ctx,'DIENST NICHT VERFÜGBAR',pad,12,w-16,small,age);
    line(ctx,'Simulierter Fehler',pad,h/2,w-16,f,age);
  } else if (kind === 'VOLUME') {
    line(ctx,'LAUTSTÄRKE · MODELL',pad,10,w-16,small,0,false);
    line(ctx,String(state.volume),pad,28,w-16,f,0,false);
    ctx.strokeStyle='#92a39e'; ctx.strokeRect(pad,h-18,w-16,7);
    ctx.fillRect(pad,h-18,(w-16)*state.volume/100,7);
  } else if (composeStatus && !concept) {
    line(ctx,'TEXT WIRD BERECHNET',pad,12,w-16,small,0,false);
  } else {
    line(ctx,`${state.source} · ${state.state}`,pad,8,w-16,small,0,false);
    line(ctx,projected.track,pad,h*.40,w-16,f,age);
    line(ctx,projected.artist,pad,h-small-8,w-16,small,age);
  }
  canvas.setAttribute('aria-label', `${concept?'Layoutentwurf':'Displaymodell'}: ${kind}, ${projected.track}, ${projected.artist}`);
}

function draw() {
  renderCanvas('display');
  if (presentation.concept) renderCanvas('concept-display',true);
}

function refresh() {
  $('concept-wrap').hidden = !presentation.concept;
  $('display-grid').classList.toggle('compare',presentation.concept);
  $('time-label').textContent = `${(state.now/1000).toFixed(1)} s`;
  $('page-kind').textContent = pageKind(state); $('generation').textContent = state.generation;
  $('pending').textContent = state.pending ? `in ${((state.pending.due-state.now)/1000).toFixed(1)} s` : 'übernommen';
  $('clock').textContent = clockText(state); $('volume').value = state.volume; $('volume-label').textContent=state.volume;
  $('accessible-display').textContent = `${pageKind(state)}. ${projected.track}. ${projected.artist}`;
  void compose(); draw();
}

function download(blob,name) {
  const url = URL.createObjectURL(blob), a=document.createElement('a');
  a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}

for (const [index, scenario] of SCENARIOS.entries()) {
  const option=document.createElement('option'); option.value=index; option.textContent=scenario.name; $('scenario').append(option);
}
$('scenario').addEventListener('change',()=>chooseScenario(Number($('scenario').value)));
$('select-source').addEventListener('click',()=>dispatch('SELECT',{source:$('source').value,station:$('station').value}));
$('metadata').addEventListener('click',()=>dispatch('METADATA',{generation:state.generation,
  track:$('track').value,artist:$('artist').value,other_info:$('other_info').value}));
$('mode').addEventListener('change',()=>{presentation.mode=$('mode').value;fieldControls();refresh();});
$('concept').addEventListener('change',()=>{presentation.concept=$('concept').checked;refresh();});
$('states').addEventListener('click',event=>{if(event.target.dataset.state) dispatch('STATE',{state:event.target.dataset.state});});
$('volume').addEventListener('input',()=>{$('volume-label').textContent=$('volume').value;});
$('volume').addEventListener('change',()=>dispatch('VOLUME',{value:Number($('volume').value)}));
$('run').addEventListener('click',()=>setRunning(!running));
$('step').addEventListener('click',()=>{setRunning(false);seek(state.now+1000);});
$('minute').addEventListener('click',()=>{setRunning(false);seek(state.now+60000);});
$('position').addEventListener('input',()=>{setRunning(false);seek(Number($('position').value));});
$('reset').addEventListener('click',()=>chooseScenario(Number($('scenario').value)));
$('demo').addEventListener('click',()=>{events=demoEvents(SCENARIOS[Number($('scenario').value)]);seek(0);timeline();setRunning(true);});
$('calibrate').addEventListener('click',()=>{
  try {
    const next={};for(const key of Object.keys(DEFAULTS)) next[key]=key==='oled'?$('oled').checked:key==='baseTime'?$('baseTime').value:Number($(key).value);
    config=configuration(next);seek(state.now);message('Modell neu berechnet; keine Hardwarekalibrierung behauptet.');
  } catch(error){message(error.message,true);}
});
$('export').addEventListener('click',()=>{
  const value=validateReplay({schema:1,kind:'DISPLAY_BEHAVIOR_MODEL',config,presentation,events,
    until:Math.max(state.now,...events.map(e=>e.at))});
  download(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}),'display-model-replay.json');
});
$('screenshot').addEventListener('click',()=>$('display').toBlob(blob=>{if(blob)download(blob,'display-model-not-hardware.png');}));
$('import').addEventListener('change',async()=>{
  setRunning(false);
  try {
    const file=$('import').files[0];if(!file)return;
    if(file.size>1048576)throw Error('Datei zu groß (maximal 1 MB)');
    const value=validateReplay(JSON.parse(await file.text()));
    config=value.config;presentation=preferences(value.presentation);events=value.events;
    for(const key of Object.keys(DEFAULTS)) {if(key==='oled')$(key).checked=config[key];else $(key).value=config[key];}
    $('mode').value=presentation.mode;$('concept').checked=presentation.concept;
    fieldControls();seek(value.until);timeline();message('Verlauf lokal geladen. Zum Abspielen Zeitregler zurücksetzen und starten.');
  }catch(error){message(`Import abgelehnt: ${error.message}`,true);}
  finally{$('import').value='';}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)setRunning(false);});
function tick(now) {
  if (running && now-lastPaint>=50) {
    const delta=Math.round(now-wall);wall=now;lastPaint=now;
    seek(state.now+delta);if(state.now>=3600000)setRunning(false);
  }
  animation=requestAnimationFrame(tick);
}
window.addEventListener('pagehide',()=>{setRunning(false);controller?.abort();cancelAnimationFrame(animation);});
fieldControls();chooseScenario(0);animation=requestAnimationFrame(tick);
