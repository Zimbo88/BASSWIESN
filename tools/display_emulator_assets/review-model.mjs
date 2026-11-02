// Offline layout comparison. Raster observed on one SCM ST30; wrapping is an
// approximation, not a port of DirectFB or a claim about other models.
// 12 px with the privately supplied reference font reproduces the three line
// breaks in one observed frame. This is calibration, not exact glyph emulation.
export const RASTER = Object.freeze({width: 128, height: 100, font: 12, textWidth: 120,
  baseline: 57, lineHeight: 13, lines: 3});

export function wrapPages(text, measure, width = 120, linesPerPage = 3) {
  if (typeof text !== 'string' || text.length > 2048 || typeof measure !== 'function' ||
      !Number.isFinite(width) || width < 1 || !Number.isInteger(linesPerPage) || linesPerPage < 1) {
    throw Error('Ungültiges Textmodell');
  }
  const fits = value => {
    const size = measure(value);
    if (!Number.isFinite(size) || size < 0) throw Error('Ungültige Schriftmetrik');
    return size <= width;
  };
  // Keep grapheme clusters intact, including combining accents and emoji.
  const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
  const lines = [];
  let line = '';
  for (const word of text.trim().split(/\s+/u).filter(Boolean)) {
    const joined = line ? `${line} ${word}` : word;
    if (fits(joined)) { line = joined; continue; }
    if (line) { lines.push(line); line = ''; }
    for (const {segment} of segmenter.segment(word)) {
      if (line && !fits(line + segment)) { lines.push(line); line = ''; }
      // A single oversized glyph remains intact; canvas clipping is preferable
      // to silently dropping content or corrupting a Unicode cluster.
      line += segment;
    }
  }
  if (line || !lines.length) lines.push(line);
  const pages = [];
  for (let i = 0; i < lines.length; i += linesPerPage) pages.push(lines.slice(i, i + linesPerPage));
  return pages;
}

export function informationPages(values, measure) {
  const keys = ['station', 'track', 'artist', 'clock'];
  if (!values || keys.some(k => typeof values[k] !== 'string' || values[k].length > 1024)) {
    throw Error('Ungültige Metadaten');
  }
  if (values.clock && !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(values.clock)) throw Error('Ungültige Uhrzeit');
  const groups = [
    ['Titel', values.track], ['Interpret', values.artist],
    ['Sender und Uhr', [values.station, values.clock].filter(Boolean).join(' ')],
  ].filter(([, text]) => text.trim());
  const pages = groups.flatMap(([kind, text]) => wrapPages(text, measure).map(lines => ({kind, lines})));
  return pages.length ? pages : [{kind: 'Keine Metadaten', lines: ['']}];
}

export function shortLine(text, measure, width) {
  if (measure(text) <= width) return text;
  const clusters = [...new Intl.Segmenter(undefined, {granularity: 'grapheme'}).segment(text)].map(s => s.segment);
  while (clusters.length && measure(clusters.join('') + '…') > width) clusters.pop();
  return clusters.join('') + '…';
}
