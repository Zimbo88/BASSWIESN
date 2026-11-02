import test from 'node:test';
import assert from 'node:assert/strict';
import {RASTER, wrapPages, informationPages, shortLine} from '../../tools/display_emulator_assets/review-model.mjs';

const measure = value => [...value].length;
test('observed raster is not the arbitrary free-model size', () => {
  assert.equal(RASTER.width, 128); assert.equal(RASTER.height, 100);
});
test('bounded wrapping retains every word across pages', () => {
  const text = 'one two three four five six seven eight';
  const pages = wrapPages(text, measure, 9, 2);
  assert.ok(pages.length > 1);
  assert.equal(pages.flat().join(' '), text);
  assert.ok(pages.every(page => page.length <= 2));
  assert.ok(pages.flat().every(line => measure(line) <= 9));
});
test('oversized words split without dropping characters or grapheme clusters', () => {
  assert.equal(wrapPages('abcdefghijk', measure, 3).flat().join(''), 'abcdefghijk');
  const accent = 'e\u0301';
  const pages = wrapPages(accent.repeat(8), measure, 5);
  assert.equal(pages.flat().join(''), accent.repeat(8));
  assert.ok(pages.flat().every(line => !line.startsWith('\u0301')));
});
test('proposal separates fields, skips missing data and keeps station and clock', () => {
  const pages = informationPages({station:'Radio',artist:'Artist',track:'Song',clock:'20:15'}, measure);
  assert.deepEqual(pages.map(p => p.kind), ['Titel','Interpret','Sender und Uhr']);
  assert.deepEqual(pages.map(p => p.lines.join(' ')), ['Song','Artist','Radio 20:15']);
  assert.equal(informationPages({station:'Radio',artist:'',track:'',clock:''}, measure).length, 1);
  assert.equal(informationPages({station:'',artist:'',track:'',clock:''}, measure)[0].kind, 'Keine Metadaten');
});
test('invalid data fails closed; markup remains literal text', () => {
  assert.throws(() => wrapPages('x'.repeat(2049), measure));
  assert.throws(() => wrapPages('test', () => NaN));
  assert.throws(() => wrapPages('test', measure, 0));
  assert.throws(() => wrapPages('test', measure, 5, 0));
  assert.throws(() => informationPages({station:'',artist:'',track:'',clock:'25:99'}, measure));
  assert.equal(wrapPages('<script>x</script>', measure).flat().join(''), '<script>x</script>');
});
test('layout-study ellipsis is explicit, not an unmarked truncation', () => {
  assert.equal(shortLine('short', measure, 7), 'short');
  assert.equal(shortLine('a very long title', measure, 7), 'a very…');
});
