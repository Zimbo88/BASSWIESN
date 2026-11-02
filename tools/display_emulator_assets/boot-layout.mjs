/** Authored boot composition, NOT a framebuffer driver or firmware patch.
 * Keep the native progress frame byte-exact outside the wordmark mask.
 * The preview progress formula is derived from static firmware analysis;
 * only the separate private ARM harness actually executes that code.
 */
export const BOOT = Object.freeze({width:128, height:100, bytes:12800,
  status:Object.freeze({x:17,y:40,width:94,height:20}),
  interior:Object.freeze({x:19,y:42,width:90,height:16})});

export function wordmarkPixels(mark) {
  if (mark?.schema !== 1 || mark.text !== 'BASSWIESN' || mark.scale !== 2 || mark.gap !== 2 ||
      JSON.stringify(mark.origin) !== '[9,13]' || !Array.isArray(mark.shear) || mark.shear.length !== 7) {
    throw new Error('Unsupported boot wordmark');
  }
  const pixels = new Set();
  let x = mark.origin[0];
  for (const letter of mark.text) {
    const rows = mark.glyphs?.[letter];
    if (!Array.isArray(rows) || rows.length !== 7 || !/^[01]{3,7}$/.test(rows[0]) ||
        rows.some(row => typeof row !== 'string' || row.length !== rows[0].length || !/^[01]+$/.test(row))) {
      throw new Error('Invalid glyph');
    }
    rows.forEach((row,y) => {
      const skew = mark.shear[y];
      if (!Number.isInteger(skew) || skew < 0 || skew > 4) throw new Error('Invalid shear');
      [...row].forEach((value,column) => {
        if (value !== '1') return;
        for (let dy=0;dy<2;dy++) for (let dx=0;dx<2;dx++) {
          const px=x+column*2+skew+dx, py=mark.origin[1]+y*2+dy;
          if (px<4 || px>=124 || py<4 || py>=32) throw new Error('Wordmark crosses reserved bounds');
          pixels.add(py*128+px);
        }
      });
    });
    x += rows[0].length*2+mark.gap;
  }
  return [...pixels].sort((a,b)=>a-b);
}

export function composeBoot(frame, mark) {
  if (!(frame instanceof Uint8Array) || frame.length !== BOOT.bytes) throw new Error('Expected 128 × 100 gray8');
  const mask = wordmarkPixels(mark); // Validate completely before changing anything.
  const result = new Uint8Array(frame);
  for (const index of mask) result[index] = 255;
  return result;
}

export function progressPreview(value, fullRange=false) {
  if (!Number.isInteger(value) || value < 0 || value > 100) throw new Error('Progress must be 0–100');
  if (typeof fullRange !== 'boolean') throw new Error('Invalid progress path');
  // Native zero/negative input does NOT write a new frame. Never invent one.
  if (value === 0) return null;
  const frame = new Uint8Array(BOOT.bytes);
  const multiply = Math.fround(Math.fround(value)*Math.fround(fullRange ? 0.85 : 0.7));
  const effective = Math.min(100,Math.fround(Math.fround(15+multiply)+1));
  const filled = Math.trunc(Math.fround(effective*Math.fround(0.9)));
  for (let y=40;y<60;y++) frame.fill(255,y*128+17,y*128+111);
  for (let y=42;y<58;y++) frame.fill(0,y*128+19+filled,y*128+109);
  return frame;
}
