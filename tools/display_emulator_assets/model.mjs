// Deterministic BEHAVIOR MODEL. Timings/priority are adjustable hypotheses,
// not a port of Bose's renderer. No DOM, I/O, timers, URLs or hardware actions.
export const FIELDS = ['station', 'artist', 'title', 'clock', 'other'];
export const DEFAULTS = Object.freeze({width: 320, height: 112, font: 20, speed: 24,
  hold: 1800, overlay: 2000, coalesce: 2000, baseTime: '20:15', oled: true});
export const STATES = ['PLAYING', 'PAUSED', 'BUFFERING', 'STOPPED', 'STANDBY', 'ERROR'];
// Display-input classes only, not an assertion of media-provider support.
export const SOURCES = ['INTERNET_RADIO', 'AIRPLAY', 'BLUETOOTH', 'AUX', 'DLNA', 'MUSIC_LIBRARY', 'SPOTIFY'];
const META = ['track', 'artist', 'other_info'];
export const EMPTY = Object.freeze({track: '', artist: '', other_info: ''});
const exactKeys = (v, keys) => v && typeof v === 'object' && !Array.isArray(v) &&
  Object.keys(v).every(k => keys.includes(k));
const boundedText = v => typeof v === 'string' && v.length <= 1024;
const int = (v, min, max) => Number.isInteger(v) && v >= min && v <= max;

export function configuration(value = {}) {
  if (!exactKeys(value, Object.keys(DEFAULTS))) throw Error('Unbekannte Modellparameter');
  const c = {...DEFAULTS, ...value};
  for (const [k, lo, hi] of [['width',128,640], ['height',64,256], ['font',8,36],
    ['speed',0,120], ['hold',0,5000], ['overlay',0,10000], ['coalesce',0,5000]]) {
    if (!int(c[k], lo, hi)) throw Error(`Ungültiger Modellparameter: ${k}`);
  }
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(c.baseTime) || typeof c.oled !== 'boolean') {
    throw Error('Ungültige Modelluhr / Displayklasse');
  }
  return c;
}

export function validateEvent(event) {
  if (!exactKeys(event, ['at', 'type', 'data']) || !int(event.at, 0, 3600000)) {
    throw Error('Ungültiger Ereigniszeitpunkt');
  }
  const d = event.data;
  if (event.type === 'SELECT') {
    if (!exactKeys(d, ['source', 'station']) || !SOURCES.includes(d.source) || !boundedText(d.station)) {
      throw Error('Ungültige simulierte Quelle');
    }
  } else if (event.type === 'METADATA') {
    if (!exactKeys(d, ['generation', ...META]) || !int(d.generation, 1, 500) ||
        !META.every(k => boundedText(d[k]))) throw Error('Ungültige Metadaten');
  } else if (event.type === 'STATE') {
    if (!exactKeys(d, ['state']) || !STATES.includes(d.state)) throw Error('Ungültiger Modellzustand');
  } else if (event.type === 'VOLUME') {
    if (!exactKeys(d, ['value']) || !int(d.value, 0, 100)) throw Error('Ungültige Modelllautstärke');
  } else if (event.type === 'MUTE') {
    if (!exactKeys(d, ['value']) || typeof d.value !== 'boolean') throw Error('Ungültiger Stummzustand');
  } else {
    throw Error('Unbekanntes Ereignis – keine Geräteaktionen erlaubt');
  }
  return structuredClone(event);
}

export function initial(config = {}) {
  return {config: configuration(config), now: 0, state: 'STANDBY', stateChangedAt: 0, source: '', station: '',
    generation: 0, metadata: {...EMPTY}, pending: null, volume: 5, muted: false, overlayUntil: 0,
    metadataChangedAt: 0, ignored: 0};
}

export function advance(state, to) {
  if (!int(to, state.now, 3600000)) throw Error('Modellzeit muss monoton sein (maximal 1 Stunde)');
  const next = structuredClone(state);
  if (next.pending && next.pending.due <= to) {
    if (next.pending.generation === next.generation) {
      next.metadata = next.pending.metadata;
      next.metadataChangedAt = next.pending.due;
    }
    next.pending = null;
  }
  next.now = to;
  return next;
}

export function apply(state, raw) {
  const event = validateEvent(raw);
  const next = advance(state, event.at);
  const d = event.data;
  if (event.type === 'SELECT') {
    next.generation += 1;
    next.station = d.station;
    next.source = d.source;
    next.metadata = {...EMPTY};
    next.pending = null;
    next.state = 'BUFFERING';
    next.stateChangedAt = next.now;
    next.overlayUntil = 0;
    next.metadataChangedAt = next.now;
  } else if (event.type === 'METADATA') {
    if (d.generation !== next.generation) { next.ignored += 1; return next; }
    const metadata = Object.fromEntries(META.map(k => [k, d[k]]));
    const latest = next.pending?.metadata ?? next.metadata;
    if (JSON.stringify(metadata) === JSON.stringify(latest)) return next;
    // A fixed first-event window avoids starving updates during frequent input.
    // 2 s is a static coalescing clue; exact native scheduling is unverified.
    next.pending = {metadata, generation: next.generation,
      due: next.pending?.due ?? next.now + next.config.coalesce};
  } else if (event.type === 'STATE') {
    if (next.state !== d.state) next.stateChangedAt = next.now;
    next.state = d.state;
    if (d.state === 'STANDBY') next.overlayUntil = 0;
  } else if (event.type === 'VOLUME') {
    next.volume = d.value;
    next.overlayUntil = next.now + next.config.overlay;
  } else if (event.type === 'MUTE') {
    next.muted = d.value;
    next.overlayUntil = next.now + next.config.overlay;
  }
  return advance(next, next.now);
}

export function pageKind(state) {
  if (!state.config.oled) return 'NO_DISPLAY';
  if (state.state === 'ERROR') return 'ERROR';
  if (state.overlayUntil > state.now) return 'VOLUME';
  return state.state;
}

export function clockText(state) {
  const [hours, minutes] = state.config.baseTime.split(':').map(Number);
  const m = (hours * 60 + minutes + Math.floor(state.now / 60000)) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2,'0')}:${String(m % 60).padStart(2,'0')}`;
}

export function scrollOffset(textWidth, viewport, elapsed, speed, hold) {
  const extra = Math.max(0, textWidth - viewport);
  if (!extra || speed <= 0 || elapsed < 0) return 0;
  const travel = extra / speed * 1000;
  const phase = elapsed % (2 * travel + 2 * hold);
  if (phase < hold) return 0;
  if (phase < hold + travel) return -(phase - hold) / travel * extra;
  if (phase < 2 * hold + travel) return -extra;
  return -extra * (1 - (phase - 2 * hold - travel) / travel);
}

export function preferences(value) {
  if (!exactKeys(value, ['fields', 'order', 'mode', 'concept'])) throw Error('Ungültige Darstellung');
  const {fields, order, mode, concept} = value;
  if (!Array.isArray(fields) || fields.length > 5 || !fields.every(f => FIELDS.includes(f)) ||
      new Set(fields).size !== fields.length || !Array.isArray(order) || order.length !== 5 ||
      new Set(order).size !== 5 || !order.every(f => FIELDS.includes(f)) ||
      !['CUSTOM', 'STATION', 'TRACK_ARTIST'].includes(mode) || typeof concept !== 'boolean') {
    throw Error('Ungültige Feldauswahl');
  }
  return structuredClone(value);
}

export function validateReplay(value) {
  if (!exactKeys(value, ['schema', 'kind', 'config', 'presentation', 'events', 'until']) ||
      value.schema !== 1 || value.kind !== 'DISPLAY_BEHAVIOR_MODEL' ||
      !Array.isArray(value.events) || value.events.length > 500 || !int(value.until, 0, 3600000)) {
    throw Error('Kein gültiger Display-Modellverlauf (max. 500 Ereignisse / 1 Stunde)');
  }
  const config = configuration(value.config);
  const presentation = preferences(value.presentation);
  const events = value.events.map(validateEvent);
  let previous = 0;
  for (const event of events) {
    if (event.at < previous || event.at > value.until) throw Error('Ereignisse nicht chronologisch');
    previous = event.at;
  }
  return {schema: 1, kind: 'DISPLAY_BEHAVIOR_MODEL', config, presentation, events, until: value.until};
}

export function replay(config, events, until) {
  let result = initial(config);
  for (const event of events) {
    if (event.at > until) break;
    result = apply(result, event);
  }
  return advance(result, until);
}

export const SCENARIOS = [
  {name: 'Radio · vollständige Songdaten', source: 'INTERNET_RADIO', station: 'Beispielradio',
    track: 'Unterwegs nach Hause', artist: 'Aurora Ensemble', other_info: ''},
  {name: 'Radio · lange Texte', source: 'INTERNET_RADIO', station: 'Beispielradio Niederbayern / Oberpfalz',
    track: 'Unterwegs nach Hause – die lange akustische Live-Version aus dem Abendprogramm',
    artist: 'Aurora Ensemble mit dem Nachtorchester', other_info: 'Abendmagazin mit Nachrichten und Wetter'},
  {name: 'Radio · Titel ohne Interpret', source: 'INTERNET_RADIO', station: 'Beispielradio',
    track: 'Unterwegs nach Hause', artist: '', other_info: ''},
  {name: 'Radio · keine Songdaten', source: 'INTERNET_RADIO', station: 'Beispielradio',
    track: '', artist: '', other_info: ''},
  {name: 'AirPlay · synthetische Songdaten', source: 'AIRPLAY', station: 'Apple-Sender (Simulation)',
    track: 'Kleine Schritte', artist: 'Das Beispielquartett', other_info: ''},
  {name: 'Zeichensatz · nur Browsernachweis', source: 'INTERNET_RADIO', station: 'Beispielradio',
    track: 'Über den Fluss / 夜の音楽', artist: 'Émilie & Gäste', other_info: ''},
  {name: 'Zeichenbudget · kurzer Sender, langer Titel', source: 'INTERNET_RADIO', station: 'FM',
    track: 'Ein sehr langer, tatsächlich gelieferter Titel '.repeat(6), artist: 'A', other_info: ''},
  {name: 'Text ist kein HTML', source: 'INTERNET_RADIO', station: 'Beispielradio',
    track: 'A & B <Live>', artist: '<img src=x onerror=alert(1)>', other_info: ''},
];

export function demoEvents(scenario = SCENARIOS[0]) {
  const {name: _name, source, station, ...meta} = scenario;
  return [
    {at: 0, type: 'SELECT', data: {source, station}},
    {at: 0, type: 'METADATA', data: {generation: 1, ...meta}},
    {at: 2500, type: 'STATE', data: {state: 'PLAYING'}},
    {at: 5000, type: 'VOLUME', data: {value: 5}},
    {at: 9000, type: 'STATE', data: {state: 'PAUSED'}},
    {at: 14000, type: 'STATE', data: {state: 'PLAYING'}},
    {at: 18000, type: 'METADATA', data: {generation: 1, ...meta, track: 'Ein neuer Titel'}},
    {at: 28000, type: 'STATE', data: {state: 'ERROR'}},
    {at: 33000, type: 'STATE', data: {state: 'PLAYING'}},
    {at: 41000, type: 'STATE', data: {state: 'STANDBY'}},
    {at: 47000, type: 'STATE', data: {state: 'PLAYING'}},
  ];
}
