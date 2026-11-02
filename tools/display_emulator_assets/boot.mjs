import {BOOT, composeBoot, progressPreview} from './boot-layout.mjs';

const status=document.querySelector('#boot-status');
const input=document.querySelector('#progress');
const kind=document.querySelector('#progress-kind');
function draw(id, frame) {
  const context=document.querySelector(id).getContext('2d');
  const image=context.createImageData(BOOT.width,BOOT.height);
  for (let i=0;i<BOOT.bytes;i++) {
    image.data[i*4]=image.data[i*4+1]=image.data[i*4+2]=frame?.[i] ?? 0;
    image.data[i*4+3]=255;
  }
  context.putImageData(image,0,0);
}
try {
  const response=await fetch('/boot-mark.json',{cache:'no-store',credentials:'omit'});
  if (!response.ok) throw new Error('Wordmark unavailable');
  const mark=await response.json();
  function update() {
    const frame=progressPreview(Number(input.value),kind.value==='full');
    document.querySelector('#progress-value').value=input.value;
    // A native no-write leaves the previous framebuffer alone, including any
    // prior logo. This also avoids animating a fictitious restart at value zero.
    if (frame === null) {
      status.textContent='Wert 0: Originalprogramm schreibt kein neues Bild. Letzte Vorschau bleibt stehen.';
      return;
    }
    draw('#boot-original',frame);
    draw('#boot-proposed',composeBoot(frame,mark));
    status.textContent='Vorschau aktualisiert. Statusfeld unverändert; kein Gerät wurde angesprochen.';
  }
  input.addEventListener('input',update);
  kind.addEventListener('change',update);
  update();
} catch {
  status.textContent='Bootvorschau konnte nicht geladen werden. Kein Gerätezugriff und keine Änderung.';
  input.disabled=true;kind.disabled=true;
}
