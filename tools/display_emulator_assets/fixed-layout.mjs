// Proposed fixed-region display. Pure projection + canvas adapter; no transport,
// firmware patch, service startup or persistence. Timings/priority are OUR policy.
import {clockText, pageKind, SOURCES, STATES} from './model.mjs';
import {wrapPages, shortLine} from './review-model.mjs';

export const REGIONS = Object.freeze({
  header: Object.freeze({x: 4, y: 3, width: 120, height: 18, font: 10, baseline: 15}),
  title: Object.freeze({x: 4, y: 28, width: 120, height: 36, font: 15, baseline: 43, step: 17}),
  artist: Object.freeze({x: 4, y: 65, width: 120, height: 23, font: 10, baseline: 73, step: 11}),
  footer: Object.freeze({x: 4, y: 88, width: 120, height: 11, font: 10, baseline: 97}),
});
export const SOURCE_LABELS = Object.freeze({INTERNET_RADIO: 'Internetradio', AIRPLAY: 'AirPlay',
  BLUETOOTH: 'Bluetooth', AUX: 'AUX', DLNA: 'DLNA', MUSIC_LIBRARY: 'Musikbibliothek',
  SPOTIFY: 'Spotify'});
export const NOTICES = Object.freeze({NONE: '', LOW_BATTERY: 'Akku niedrig',
  CRITICAL_BATTERY: 'Akku leer', SETUP: 'Einrichtung', UPDATE: 'Aktualisierung'});

function clean(value) {return value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim();}
// Proposed clock policy, NOT native movement/brightness calibration. Clock
// enablement is deliberately independent of the playback field selection.
export function standbyClock(state, provided = {}) {
  const defaults = {enabled: true, synchronized: true, format: 24, brightness: 45};
  if (!provided || typeof provided !== 'object' || Array.isArray(provided) ||
      Object.keys(provided).some(k => !Object.hasOwn(defaults,k))) throw Error('Ungültige Standby-Uhr');
  const options = {...defaults, ...provided};
  if (typeof options.enabled !== 'boolean' || typeof options.synchronized !== 'boolean' ||
      ![12,24].includes(options.format) || !Number.isInteger(options.brightness) ||
      options.brightness < 0 || options.brightness > 100 ||
      !Number.isInteger(state.stateChangedAt) || state.stateChangedAt < 0 || state.stateChangedAt > state.now) {
    throw Error('Ungültige Standby-Uhr');
  }
  const time = clockText(state), hour = Number(time.slice(0,2));
  const label = !options.synchronized ? '--:--' : options.format === 24 ? time : `${hour % 12 || 12}:${time.slice(3)}`;
  const suffix = options.synchronized && options.format === 12 ? (hour < 12 ? 'AM' : 'PM') : '';
  const age = Math.max(0,state.now-state.stateChangedAt);
  // Discrete movement, no animation. Reduced-motion MUST NOT pin an OLED clock.
  // This reduces stationary exposure but is no OLED lifetime guarantee.
  const offsets = [[0,0],[2,0],[2,2],[0,2],[-2,2],[-2,0],[-2,-2],[0,-2],[2,-2]];
  const [dx,dy] = offsets[Math.floor(age/60000) % offsets.length];
  const visible = options.enabled && options.brightness > 0;
  const level = age < 30000 ? options.brightness : Math.min(options.brightness,20);
  return {text: visible ? label : '', suffix: visible ? suffix : '', visible,
    synchronized: options.synchronized, brightness: visible ? level : 0,
    x: 8+dx, y: 27+dy, width: 112, height: 48, baseline: 58+dy,
    status: !visible ? 'Standby · Anzeige aus' : !options.synchronized ? 'Uhrzeit nicht synchronisiert' : `Standby ${label} ${suffix}`.trim()};
}
function validate(state, options) {
  if (!state || !STATES.includes(state.state) || (state.source && !SOURCES.includes(state.source)) ||
      !Number.isInteger(state.now) || state.now < 0 || state.now > 3600000 ||
      !Number.isInteger(state.metadataChangedAt) || state.metadataChangedAt < 0 || state.metadataChangedAt > state.now ||
      !Number.isFinite(state.overlayUntil) || !Number.isInteger(state.volume) || state.volume < 0 || state.volume > 100 ||
      typeof state.config?.oled !== 'boolean' || typeof state.muted !== 'boolean' ||
      typeof state.config.baseTime !== 'string' || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(state.config.baseTime) ||
      [state.station, state.metadata?.track, state.metadata?.artist, state.metadata?.other_info]
        .some(s => typeof s !== 'string' || s.length > 1024)) throw Error('Ungültiger Display-Snapshot');
  const {fields, notice, zone, metadataStale, reducedMotion} = options;
  if (!Array.isArray(fields) || new Set(fields).size !== fields.length ||
      fields.some(f => !['station','title','artist','clock','other'].includes(f)) ||
      !Object.hasOwn(NOTICES, notice) || !['solo','master','member'].includes(zone) ||
      typeof metadataStale !== 'boolean' || typeof reducedMotion !== 'boolean') throw Error('Ungültige Displayoptionen');
}

export function fixedFrame(state, measure, provided = {}) {
  const optionKeys = ['fields','notice','zone','metadataStale','reducedMotion','standby'];
  if (!provided || typeof provided !== 'object' || Array.isArray(provided) ||
      Object.keys(provided).some(k => !optionKeys.includes(k)) || typeof measure !== 'function') {
    throw Error('Ungültige Displayoptionen');
  }
  const options = {fields: ['station','title','artist','clock'], notice: 'NONE', zone: 'solo',
    metadataStale: false, reducedMotion: false, standby: {}, ...provided};
  validate(state, options);
  const standby = standbyClock(state,options.standby);
  const measured = size => text => {
    const width = measure(size, text);
    if (!Number.isFinite(width) || width < 0) throw Error('Ungültige Schriftmetrik');
    return width;
  };
  const selected = new Set(options.fields);
  const sourceLabel = SOURCE_LABELS[state.source] || 'Keine Quelle';
  const station = selected.has('station') ? clean(state.station) || sourceLabel : '';
  const track = selected.has('title') ? clean(state.metadata.track) : '';
  const artist = selected.has('artist') ? clean(state.metadata.artist) : '';
  const other = selected.has('other') ? clean(state.metadata.other_info) : '';
  const clock = selected.has('clock') ? clockText(state) : '';
  const fullText = [station, track, artist, other, clock].filter(Boolean).join(' / ');
  let page = pageKind(state);
  // This priority is a conservative PROPOSED renderer policy, not recovered
  // native behavior. Critical/system pages must never be obscured by song data.
  if (state.config.oled && ['CRITICAL_BATTERY','SETUP','UPDATE'].includes(options.notice)) page = options.notice;
  const frame = {kind: page, width: 128, height: 100, header: '', title: [], artist: [],
    clock, badge: '', titlePage: 1, titlePages: 1, artistPage: 1, artistPages: 1,
    fullText, volume: null, muted: state.muted, stale: options.metadataStale,
    hardwareValidated: false, firmwareExecution: false, policy: 'FIXED_LAYOUT_PROTOTYPE'};
  if (page === 'NO_DISPLAY') return {...frame, clock: '', fullText: 'Gerät ohne Display'};
  frame.header = shortLine(station, measured(10), 120);
  if (page === 'STANDBY') return {...frame, header: '', badge: '', clock: standby.text, standby, fullText: standby.status};
  if (page === 'VOLUME') return {...frame, header: state.muted ? 'Stumm' : 'Lautstärke',
    title: [String(state.volume)], volume: state.volume, badge: state.muted ? 'Stumm' : '',
    fullText: `Lautstärke ${state.volume}${state.muted ? ', stumm' : ''}`};
  if (['CRITICAL_BATTERY','SETUP','UPDATE','ERROR'].includes(page)) {
    const text = page === 'ERROR' ? 'Dienst nicht verfügbar' : NOTICES[page];
    return {...frame, header: page === 'ERROR' ? 'Fehler' : 'System',
      title: wrapPages(text, measured(15), 120, 2)[0], fullText: text, clock: ''};
  }
  if (page === 'BUFFERING' || page === 'STOPPED') return {...frame,
    title: page === 'BUFFERING' ? ['Verbinden …'] : ['Gestoppt'],
    fullText: `${station} / ${page === 'BUFFERING' ? 'Verbindung wird aufgebaut' : 'Gestoppt'} / ${clock}`};
  const fallback = selected.has('title') ? (state.source === 'AUX' ? 'AUX-Eingang' : 'Keine Titeldaten') : '';
  const titlePages = wrapPages(track || fallback, measured(15), 120, 2);
  const details = [artist, other].filter(Boolean);
  const artistPages = details.length ? details.flatMap(text => wrapPages(text, measured(10), 120, 2)) : [['']];
  const age = options.reducedMotion || page === 'PAUSED' ? 0 : Math.max(0, state.now - state.metadataChangedAt);
  const titleIndex = Math.floor(age / 5000) % titlePages.length;
  const artistIndex = Math.floor(age / 7000) % artistPages.length;
  frame.title = titlePages[titleIndex]; frame.artist = artistPages[artistIndex];
  frame.fullText = [station, track || fallback, artist, other, clock].filter(Boolean).join(' / ');
  frame.titlePage = titleIndex + 1; frame.titlePages = titlePages.length;
  frame.artistPage = artistIndex + 1; frame.artistPages = artistPages.length;
  const badge = options.notice === 'LOW_BATTERY' ? 'Akku niedrig' : state.muted ? 'Stumm' :
    page === 'PAUSED' ? 'Pause' : options.metadataStale ? 'Daten alt' :
      options.zone !== 'solo' ? 'Gruppe' : titlePages.length > 1 ? `${titleIndex + 1}/${titlePages.length}` : '';
  frame.badge = shortLine(badge, measured(9), clock ? 66 : 120);
  return frame;
}

export function drawFixedFrame(canvas, frame, font = 'Arial, sans-serif') {
  canvas.width = 128; canvas.height = 100;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = frame.kind === 'NO_DISPLAY' ? '#000' : '#010101'; ctx.fillRect(0, 0, 128, 100);
  canvas.setAttribute('aria-label', `${frame.kind}: ${frame.fullText}`);
  if (frame.kind === 'NO_DISPLAY') return;
  function lines(region, content, size = region.font) {
    ctx.save(); ctx.beginPath(); ctx.rect(region.x, region.y, region.width, region.height); ctx.clip();
    ctx.fillStyle = frame.stale && ['PLAYING','PAUSED'].includes(frame.kind) ? '#bcbcbc' : '#e3e3e3';
    ctx.font = `${size}px ${font}`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    content.forEach((text, i) => ctx.fillText(text, region.x + region.width / 2, region.baseline + i * (region.step || 0)));
    ctx.restore();
  }
  if (frame.kind === 'STANDBY') {
    const clock=frame.standby;
    if (!clock.visible) return;
    ctx.save(); ctx.beginPath();ctx.rect(clock.x,clock.y,clock.width,clock.height);ctx.clip();
    ctx.fillStyle=`rgb(${Math.round(clock.brightness*2.27)} ${Math.round(clock.brightness*2.27)} ${Math.round(clock.brightness*2.27)})`;
    ctx.textAlign='center';ctx.textBaseline='alphabetic';
    // Fit actual font metrics, not character count. This is browser rendering,
    // not proof that the native font factory uses these same size units.
    let size=30;
    do {ctx.font=`${size}px ${font}`;if(ctx.measureText(clock.text).width<=clock.width || size===10)break;size--;}
    while(size>=10);
    ctx.fillText(clock.text,clock.x+clock.width/2,clock.baseline);
    ctx.font=`9px ${font}`;
    ctx.fillText(clock.suffix,clock.x+clock.width/2,clock.baseline+13);
    ctx.restore();
    return;
  } else {
    lines(REGIONS.header, [frame.header]);
    ctx.fillStyle = '#414141'; ctx.fillRect(12, 23, 104, 1);
    lines(REGIONS.title, frame.title);
    if (frame.volume === null) lines(REGIONS.artist, frame.artist);
    else {
      ctx.strokeStyle = '#a0a0a0'; ctx.strokeRect(8.5, 72.5, 111, 7);
      ctx.fillStyle = '#e3e3e3'; ctx.fillRect(10, 74, 108 * frame.volume / 100, 4);
    }
  }
  if (frame.kind !== 'STANDBY') lines({...REGIONS.footer, x: 78, width: 46}, [frame.clock]);
  lines({...REGIONS.footer, x: 4, width: frame.clock && frame.kind !== 'STANDBY' ? 66 : 120, font: 9}, [frame.badge]);
}

export const FIXED_SCENARIOS = [
  {label: 'Radio / Preset · Sender und Song', source: 'INTERNET_RADIO', station: 'Beispielradio', track: 'Unterwegs nach Hause', artist: 'Aurora Ensemble'},
  {label: 'Radio · keine Songdaten', source: 'INTERNET_RADIO', station: 'Nachrichtenradio', track: '', artist: ''},
  {label: 'AirPlay · synthetische Metadaten', source: 'AIRPLAY', station: 'AirPlay', track: 'Kleine Schritte', artist: 'Das Beispielquartett'},
  {label: 'AirPlay · Metadaten fehlen', source: 'AIRPLAY', station: 'AirPlay', track: '', artist: ''},
  {label: 'Bluetooth · Songdaten geliefert', source: 'BLUETOOTH', station: 'Bluetooth', track: 'Später am Abend', artist: 'Aurora Ensemble'},
  {label: 'Bluetooth · ohne Songdaten', source: 'BLUETOOTH', station: 'Bluetooth', track: '', artist: ''},
  {label: 'AUX · kein Metadatenkanal', source: 'AUX', station: 'AUX', track: '', artist: ''},
  {label: 'DLNA / NAS · langer Titel', source: 'DLNA', station: 'Musikbibliothek', track: 'Sinfonie in vier Sätzen – Liveaufnahme aus dem großen Konzertsaal', artist: 'Das Beispielorchester und seine Gäste'},
  {label: 'Lokale Bibliothek · Titel ohne Interpret', source: 'MUSIC_LIBRARY', station: 'Musikbibliothek', track: 'Eigenes Musikstück', artist: ''},
  {label: 'Spotify · simulierte Daten, keine Integration', source: 'SPOTIFY', station: 'Spotify', track: 'Morgenlicht', artist: 'Aurora Ensemble'},
  {label: 'Zone · Mitglied mit übernommenen Daten', source: 'INTERNET_RADIO', station: 'Beispielradio', track: 'Unterwegs nach Hause', artist: 'Aurora Ensemble', zone: 'member'},
  {label: 'Zeichensätze / lange Namen', source: 'DLNA', station: 'Beispielradio', track: 'Über den Fluss / 夜の音楽 / موسيقى', artist: 'Émilie & Gäste – Abendorchester'},
  {label: 'Eingabe bleibt Text, kein HTML', source: 'INTERNET_RADIO', station: 'Beispielradio', track: '<img src=x onerror=alert(1)>', artist: 'A & B <Live>'},
];
