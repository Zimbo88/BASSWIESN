import {RASTER, wrapPages, informationPages} from './review-model.mjs';
import {initial, apply, advance, clockText} from './model.mjs';
import {fixedFrame, drawFixedFrame, FIXED_SCENARIOS} from './fixed-layout.mjs';

const $ = id => document.getElementById(id);
let font = 'Arial, sans-serif', loadedFont = null, generation = 0, fontGeneration = 0, referenceGeneration = 0;
let currentPages = [['']], proposedPages = [{kind: 'Keine Metadaten', lines: ['']}];
let currentIndex = 0, pageIndex = 0;
let fixedState = initial({baseTime: '20:15'});
let fixedFrameNow = null;

function fixedOptions() {
  return {notice: $('layout-notice').value, zone: $('layout-zone').value,
    metadataStale: $('metadata-stale').checked,
    standby: {enabled: $('standby-enabled').checked, synchronized: $('standby-synced').checked,
      format: Number($('standby-format').value), brightness: Number($('standby-brightness').value)},
    reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
    fields: $('clock').value ? ['station','title','artist','clock'] : ['station','title','artist']};
}

function renderFixed() {
  const metrics = new Map();
  fixedFrameNow = fixedFrame(fixedState, (size, text) => {
    if (!metrics.has(size)) metrics.set(size, measure(size));
    return metrics.get(size)(text);
  }, fixedOptions());
  drawFixedFrame($('layout'), fixedFrameNow, font);
  $('layout-text').textContent = fixedFrameNow.fullText;
  $('layout-status').textContent = `${fixedFrameNow.kind} · Titel ${fixedFrameNow.titlePage}/${fixedFrameNow.titlePages}` +
    ` · Interpret ${fixedFrameNow.artistPage}/${fixedFrameNow.artistPages} · ${(fixedState.now / 1000).toFixed(0)} s Modellzeit`;
  $('layout').dataset.kind = fixedFrameNow.kind;
  $('layout').dataset.titlePage = fixedFrameNow.titlePage;
  $('layout').dataset.titlePages = fixedFrameNow.titlePages;
  $('layout').dataset.artistPages = fixedFrameNow.artistPages;
}

function context(id) {
  const ctx = $(id).getContext('2d');
  ctx.fillStyle = '#010101'; ctx.fillRect(0, 0, 128, 100);
  ctx.fillStyle = '#e3e3e3'; ctx.font = `${RASTER.font}px ${font}`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  return ctx;
}

function measure(size = RASTER.font) {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = `${size}px ${font}`;
  return text => ctx.measureText(text).width;
}

function drawPage(id, lines) {
  const ctx = context(id);
  ctx.save(); ctx.beginPath(); ctx.rect(4, 43, 120, 49); ctx.clip();
  lines.forEach((line, i) => ctx.fillText(line, 64, RASTER.baseline + i * RASTER.lineHeight));
  ctx.restore();
  $(id).setAttribute('aria-label', lines.join(' / ') || 'Leere Textseite');
}

function render() {
  drawPage('current', currentPages[currentIndex]);
  $('current-text').textContent = `Textseite ${currentIndex + 1}/${currentPages.length}: ${currentPages[currentIndex].join(' / ')}`;
  $('current-next').disabled = currentPages.length <= 1;
  drawPage('pages', proposedPages[pageIndex].lines);
  $('pages-text').textContent = `${proposedPages[pageIndex].kind} · ${pageIndex + 1}/${proposedPages.length}: ${proposedPages[pageIndex].lines.join(' / ')}`;
  $('pages-next').disabled = proposedPages.length <= 1;
  renderFixed();
}

async function compare() {
  const ticket = ++generation;
  const candidate = Object.fromEntries(['station', 'artist', 'track', 'clock'].map(key => [key, $(key).value]));
  $('status').textContent = 'Lokaler Vergleich …';
  try {
    const response = await fetch('/api/compose', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({station: candidate.station, metadata: {artist: candidate.artist, track: candidate.track},
        clock: candidate.clock, mode: 'CUSTOM', fields: ['station', 'artist', 'title', 'clock'],
        order: ['station', 'artist', 'title', 'clock', 'other']})});
    if (!response.ok) throw Error('Eingaben ungültig oder lokale Vorschau nicht erreichbar.');
    const result = await response.json();
    if (ticket !== generation) return;
    const measured = measure();
    const current = wrapPages(result.track, measured);
    const proposed = informationPages(candidate, measured);
    currentPages = current; proposedPages = proposed;
    // The bounded replay has a one-hour horizon. Start a fresh model rather
    // than leaving a half-applied metadata event at the end of that horizon.
    if (fixedState.now + fixedState.config.coalesce > 3600000) {
      fixedState = initial({baseTime: candidate.clock || '00:00'});
      $('layout-volume').value = '5'; $('layout-mute').checked = false;
    }
    const source = $('layout-source').value;
    if (source !== fixedState.source || candidate.station !== fixedState.station) {
      fixedState = apply(fixedState, {at: fixedState.now, type: 'SELECT', data: {source, station: candidate.station}});
    }
    fixedState.config.baseTime = candidate.clock || '00:00';
    fixedState = apply(fixedState, {at: fixedState.now, type: 'METADATA', data: {
      generation: fixedState.generation, track: candidate.track, artist: candidate.artist, other_info: ''}});
    fixedState = advance(fixedState, fixedState.now + fixedState.config.coalesce);
    fixedState = apply(fixedState, {at: fixedState.now, type: 'STATE', data: {state: $('layout-state').value}});
    currentIndex = 0; pageIndex = 0; render();
    $('status').textContent = 'Vorschau aktualisiert. Kein Radio wurde kontaktiert oder verändert.';
  } catch (error) {
    if (ticket === generation) $('status').textContent = String(error.message);
  }
}

$('compare').addEventListener('click', compare);
$('current-next').addEventListener('click', () => {currentIndex = (currentIndex + 1) % currentPages.length; render();});
$('pages-next').addEventListener('click', () => {pageIndex = (pageIndex + 1) % proposedPages.length; render();});
for (const [index, scenario] of FIXED_SCENARIOS.entries()) {
  const option = document.createElement('option'); option.value = String(index); option.textContent = scenario.label;
  $('layout-scenario').append(option);
}
$('layout-scenario').addEventListener('change', () => {
  if ($('layout-scenario').value === '') return;
  const scenario = FIXED_SCENARIOS[Number($('layout-scenario').value)];
  for (const key of ['station','artist','track']) $(key).value = scenario[key];
  $('layout-source').value = scenario.source; $('layout-state').value = 'PLAYING';
  $('layout-zone').value = scenario.zone || 'solo'; $('layout-notice').value = 'NONE';
  $('metadata-stale').checked = false; $('layout-mute').checked = false;
  $('layout-volume').value = '5';
  fixedState = initial({baseTime: $('clock').value || '00:00'});
  void compare();
});
$('layout-source').addEventListener('change', () => {
  // Explicit source change cannot reuse a previous source's song accidentally.
  $('track').value = ''; $('artist').value = ''; $('station').value = $('layout-source').selectedOptions[0].textContent;
  fixedState = apply(fixedState, {at: fixedState.now, type: 'SELECT', data: {
    source: $('layout-source').value, station: $('station').value}});
  renderFixed();
  void compare();
});
$('layout-state').addEventListener('change', () => {
  fixedState = apply(fixedState, {at: fixedState.now, type: 'STATE', data: {state: $('layout-state').value}});
  renderFixed();
});
for (const id of ['layout-notice','layout-zone','metadata-stale']) $(id).addEventListener('change', renderFixed);
for (const id of ['standby-enabled','standby-synced','standby-format']) $(id).addEventListener('change', renderFixed);
$('standby-brightness').addEventListener('input',renderFixed);
$('layout-volume').addEventListener('input', () => {
  fixedState = apply(fixedState, {at: fixedState.now, type: 'VOLUME', data: {value: Number($('layout-volume').value)}});
  renderFixed();
});
$('layout-mute').addEventListener('change', () => {
  fixedState = apply(fixedState, {at: fixedState.now, type: 'MUTE', data: {value: $('layout-mute').checked}});
  renderFixed();
});
for (const [id, step] of [['layout-step',5000],['layout-minute',60000]]) $(id).addEventListener('click', () => {
  fixedState = advance(fixedState, Math.min(3600000, fixedState.now + step));
  renderFixed();
});
$('layout-export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify({schema: 1, kind: 'FIXED_LAYOUT_PROTOTYPE',
    firmwareExecution: false, hardwareValidated: false, state: fixedState, options: fixedOptions(), frame: fixedFrameNow,
    modelClock: clockText(fixedState)}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = 'fixed-display-model.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$('font-file').addEventListener('change', async () => {
  const ticket = ++fontGeneration;
  try {
    const file = $('font-file').files[0];
    if (!file) return;
    if (file.size < 12 || file.size > 2 * 1024 * 1024) throw Error('Schrift muss kleiner als 2 MB sein.');
    const buffer = await file.arrayBuffer();
    const magic = new DataView(buffer).getUint32(0);
    if (![0x00010000, 0x4f54544f, 0x74727565].includes(magic)) throw Error('Keine unterstützte TTF/OTF-Datei.');
    const face = await new FontFace('PrivateDisplayReference', buffer).load();
    if (ticket !== fontGeneration) return;
    if (loadedFont) document.fonts.delete(loadedFont);
    document.fonts.add(face); loadedFont = face; font = 'PrivateDisplayReference';
    $('font-status').textContent = 'Eigene Schrift nur im Browser geladen. Rasterung weiterhin nicht identisch mit DirectFB belegt.';
    await compare();
  } catch (error) {
    if (ticket === fontGeneration) $('font-status').textContent = `Schrift nicht geladen: ${error.message}`;
  }
});
$('reference').addEventListener('change', async () => {
  const ticket = ++referenceGeneration;
  try {
    const file = $('reference').files[0];
    if (!file) return;
    if (file.size > 256 * 1024) throw Error('Datei ist zu groß.');
    const bytes = new Uint8Array(await file.arrayBuffer());
    const ctx = $('reference-display').getContext('2d');
    if (file.name.endsWith('.gray8') && bytes.length === 12800) {
      const pixels = ctx.createImageData(128, 100);
      bytes.forEach((v, i) => {pixels.data.set([v, v, v, 255], i * 4);});
      if (ticket !== referenceGeneration) return;
      ctx.putImageData(pixels, 0, 0);
    } else {
      const header = new DataView(bytes.buffer);
      if (bytes.length < 33 || header.getUint32(0) !== 0x89504e47 || header.getUint32(4) !== 0x0d0a1a0a ||
          header.getUint32(8) !== 13 || header.getUint32(12) !== 0x49484452 ||
          header.getUint32(16) !== 128 || header.getUint32(20) !== 100) throw Error('PNG muss 128 × 100 Pixel haben.');
      const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
      try {
        if (ticket !== referenceGeneration) return;
        if (bitmap.width !== 128 || bitmap.height !== 100) throw Error('Falsche Bildgröße.');
        ctx.clearRect(0, 0, 128, 100); ctx.drawImage(bitmap, 0, 0);
      } finally {bitmap.close();}
    }
    $('reference-status').textContent = 'Originalbild lokal geladen · unveränderte 128 × 100 Pixel · kein Upload.';
  } catch (error) {
    if (ticket === referenceGeneration) $('reference-status').textContent = `Bild nicht geladen: ${error.message}`;
  }
});
compare();
