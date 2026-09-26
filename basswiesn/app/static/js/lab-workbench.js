/* LAB workbench: local cache only until an explicitly confirmed replay. */
(() => {
  "use strict";
  const panel = document.getElementById("lab-workbench"), api = window.BasswiesnApi;
  if (!panel || !api) return;
  panel.dataset.authoredCopy = "true";
  const words = {
    en: {
      title:"Listening workbench", intro:"LAB · Software-tested; real-radio acceptance is pending. These tools use saved BASSWIESN data, not new radio queries.",
      open:"Open workbench", refresh:"Refresh saved data", device:"Radio", empty:"No unprotected radios saved.",
      busy:"Working…", ready:"Saved data loaded. No radio contacted.", failed:"Could not complete the operation.",
      lab:"Switch to LAB to use this workbench.", diagnosis:"Playback diagnosis", cause:"Cause not established",
      caution:"Observations are clues, not proof of a cause or audible output. There is no automatic retry or reboot.",
      noClues:"No specific warning in the available records. This does not prove the radio is healthy.",
      timeline:"Event window", hours:"Look back", hour:"hours", noEvents:"No recorded events in this window.",
      truncated:"Limited to the newest records; older entries are not included.",
      metadata:"Metadata origin and freshness", metadataNote:"Stored values, not a live display check. A matching source does not verify the track's session.",
      station_name:"Station", track:"Title", artist:"Artist", album:"Album", genre:"Genre", provenance:"Origin", age:"Age (seconds)",
      artwork:"Artwork reference saved", yes:"Yes", no:"No", unknown:"Unknown", observed:"Observed at",
      recent:"Recently listened", noRecent:"No confirmed listening history.", replay:"Play again at volume 1",
      replayAsk:"Start this station on the selected radio at volume 1? This is a real playback action. The original source and volume are not restored automatically.",
      replaySent:"Playback request completed. Review the readback below; audible output is not verified.",
      replayUnavailable:"No current playable station mapping", details:"Readback details", shortcut:"Remote shortcut",
      qr:"Create QR code", qrNote:"Contains this server address and a stable radio ID, no administrator code. Keep it private. Opening the remote can read the radio.",
      qrAlt:"QR code linking to the selected radio remote", openRemote:"Open remote", copyLink:"Copy link", copied:"Link copied.",
      clipboardFailed:"Copy the displayed link manually.", snapshots:"Compare saved states", label:"Snapshot label (optional)",
      capture:"Save current cached state", compare:"Compare with current cache", download:"Download JSON", remove:"Delete snapshot",
      removeAsk:"Delete this local snapshot? The radio is not changed.", noSnapshots:"No snapshots saved. Maximum 10 per radio.",
      snapshotNote:"These snapshots contain only a small diagnostic subset. They are not hardware backups or restore certificates. Times and data age are excluded from comparisons.",
      noDiff:"No content differences in this cached subset. This is not proof of an exact radio restore.", field:"Field", before:"Before", after:"After",
      export:"Export listening times", days:"Days", csv:"Download CSV", csvNote:"UTC; estimated, confirmed history intervals only. No URLs or device IDs. Names may be personal; review before sharing.",
      formats:"Saved stream formats", formatNote:"Saved format hints, not stream probes or hardware playback tests. No conversion is started.",
      noFormats:"No stations saved.", format:"Format", assessment:"Assessment", auto:"Refresh this cache view every 10 seconds while visible",
      snapshotSaved:"Cached snapshot saved locally. No radio contacted.",
      SNAPSHOT_LIMIT:"Snapshot limit reached. Delete an old snapshot first.", SNAPSHOT_INTEGRITY:"Snapshot integrity check failed. It was not compared or exported.",
      STATION_CHANGED:"Station mapping changed. Refresh and confirm again.", STATION_UNAVAILABLE:"The station no longer has a current replay mapping.",
      LAB_MODE_REQUIRED:"LAB mode is required. No operation started.", audio_safety_locked:"Playback is safety-locked. Complete the existing safety check before trying again.",
      CACHED_OFFLINE:"The last saved connection observation was offline.", SOURCE_INVALID:"The saved source was invalid.", STREAM_NOT_ALIVE:"The saved stream observation was not alive.",
      PLAYBACK_ATTENTION:"The saved playback state needs attention.", STOP_OBSERVED:"A stopped/standby state was saved; the reason is unknown.",
      PROVIDER_ATTENTION:"A provider warning was saved; it may be unrelated to current playback.", REPORTING_ONLY:"Reporting needs attention. This alone does not prove an audio failure.",
      TIMER_CONFIGURED:"An inactivity deadline was saved. This does not prove the timer stopped playback.", METADATA_ONLY:"Metadata is stale or belongs to another source; audio may still work.",
      RECENT:"Recent observation", STALE:"Old observation", UNKNOWN:"Unknown", NOT_OBSERVED:"Not observed", SELECTION_MISMATCH:"Different source",
      DIRECT_CANDIDATE:"MP3/AAC candidate; audio unverified", HLS_REQUIRES_ADAPTATION:"HLS: server adaptation needed for this path", FORMAT_UNVERIFIED:"Format not verified"
    },
    de: {
      title:"Hör-Werkstatt", intro:"LAB · Softwaregeprüft; die Abnahme am echten Radio steht aus. Diese Werkzeuge nutzen gespeicherte BASSWIESN-Daten, keine neuen Radioabfragen.",
      open:"Werkstatt öffnen", refresh:"Gespeicherte Daten aktualisieren", device:"Radio", empty:"Keine ungeschützten Radios gespeichert.",
      busy:"Wird bearbeitet …", ready:"Gespeicherte Daten geladen. Kein Radio kontaktiert.", failed:"Der Vorgang konnte nicht abgeschlossen werden.",
      lab:"Für diese Werkstatt in LAB wechseln.", diagnosis:"Wiedergabediagnose", cause:"Ursache nicht belegt",
      caution:"Beobachtungen sind Hinweise, kein Beweis für eine Ursache oder hörbaren Ton. Kein automatischer Wiederholungsversuch oder Neustart.",
      noClues:"Kein konkreter Warnhinweis in den vorhandenen Daten. Das beweist nicht, dass das Radio fehlerfrei ist.",
      timeline:"Ereignisfenster", hours:"Rückblick", hour:"Stunden", noEvents:"Keine gespeicherten Ereignisse in diesem Zeitraum.",
      truncated:"Auf die neuesten Einträge begrenzt; ältere Daten fehlen.",
      metadata:"Metadaten-Herkunft und -Alter", metadataNote:"Gespeicherte Werte, keine Live-Displayprüfung. Eine passende Quelle beweist nicht die Sitzung des Titels.",
      station_name:"Sender", track:"Titel", artist:"Interpret", album:"Album", genre:"Genre", provenance:"Herkunft", age:"Alter (Sekunden)",
      artwork:"Cover-Verweis gespeichert", yes:"Ja", no:"Nein", unknown:"Unbekannt", observed:"Beobachtet am",
      recent:"Zuletzt gehört", noRecent:"Noch kein bestätigter Hörverlauf.", replay:"Erneut mit Lautstärke 1 starten",
      replayAsk:"Diesen Sender auf dem gewählten Radio mit Lautstärke 1 starten? Das ist eine echte Wiedergabeaktion. Die vorherige Quelle und Lautstärke werden nicht automatisch wiederhergestellt.",
      replaySent:"Wiedergabeanfrage abgeschlossen. Rückprüfung unten beachten; hörbarer Ton ist nicht bestätigt.",
      replayUnavailable:"Keine aktuelle abspielbare Senderzuordnung", details:"Rückprüfung im Detail", shortcut:"Fernbedienungs-Direktzugang",
      qr:"QR-Code erzeugen", qrNote:"Enthält diese Serveradresse und die stabile Radio-ID, keinen Administratorcode. Privat halten. Das Öffnen der Fernbedienung kann das Radio abfragen.",
      qrAlt:"QR-Code zur Fernbedienung des ausgewählten Radios", openRemote:"Fernbedienung öffnen", copyLink:"Link kopieren", copied:"Link kopiert.",
      clipboardFailed:"Bitte den angezeigten Link manuell kopieren.", snapshots:"Gespeicherte Zustände vergleichen", label:"Name der Momentaufnahme (optional)",
      capture:"Aktuellen Cache-Zustand speichern", compare:"Mit aktuellem Cache vergleichen", download:"JSON herunterladen", remove:"Momentaufnahme löschen",
      removeAsk:"Diese lokale Momentaufnahme löschen? Das Radio wird nicht verändert.", noSnapshots:"Keine Momentaufnahmen gespeichert. Maximal 10 je Radio.",
      snapshotNote:"Diese Aufnahmen enthalten nur einen kleinen Diagnoseausschnitt. Keine Hardware-Backups oder Restore-Nachweise. Zeitstempel und Datenalter werden beim Vergleich ausgenommen.",
      noDiff:"Keine inhaltlichen Unterschiede in diesem Cache-Ausschnitt. Das beweist keinen exakten Radio-Restore.", field:"Feld", before:"Vorher", after:"Nachher",
      export:"Hörzeiten exportieren", days:"Tage", csv:"CSV herunterladen", csvNote:"UTC; nur geschätzte, bestätigte Hörintervalle. Keine URLs oder Geräte-IDs. Namen können persönlich sein; vor Weitergabe prüfen.",
      formats:"Gespeicherte Streamformate", formatNote:"Gespeicherte Formathinweise, keine Streamprüfung oder Hardware-Wiedergabetests. Es startet keine Umwandlung.",
      noFormats:"Keine Sender gespeichert.", format:"Format", assessment:"Bewertung", auto:"Diese Cache-Ansicht alle 10 Sekunden aktualisieren, solange sichtbar",
      snapshotSaved:"Cache-Momentaufnahme lokal gespeichert. Kein Radio kontaktiert.",
      SNAPSHOT_LIMIT:"Aufnahmegrenze erreicht. Zuerst eine alte Momentaufnahme löschen.", SNAPSHOT_INTEGRITY:"Integritätsprüfung der Aufnahme fehlgeschlagen. Kein Vergleich oder Export.",
      STATION_CHANGED:"Senderzuordnung verändert. Bitte aktualisieren und neu bestätigen.", STATION_UNAVAILABLE:"Der Sender hat keine aktuelle Zuordnung zum erneuten Start.",
      LAB_MODE_REQUIRED:"LAB-Modus erforderlich. Kein Vorgang gestartet.", audio_safety_locked:"Wiedergabe sicherheitsgesperrt. Zuerst die vorhandene Sicherheitsprüfung abschließen.",
      CACHED_OFFLINE:"Die letzte gespeicherte Verbindungsbeobachtung war offline.", SOURCE_INVALID:"Die gespeicherte Quelle war ungültig.", STREAM_NOT_ALIVE:"Der gespeicherte Stream wurde als nicht aktiv beobachtet.",
      PLAYBACK_ATTENTION:"Der gespeicherte Wiedergabestatus benötigt Aufmerksamkeit.", STOP_OBSERVED:"Stopp oder Standby gespeichert; der Grund ist unbekannt.",
      PROVIDER_ATTENTION:"Provider-Warnung gespeichert; sie kann unabhängig von der aktuellen Wiedergabe sein.", REPORTING_ONLY:"Reporting benötigt Aufmerksamkeit. Allein kein Nachweis eines Audiofehlers.",
      TIMER_CONFIGURED:"Inaktivitätsfrist gespeichert. Das beweist keinen Wiedergabestopp durch den Timer.", METADATA_ONLY:"Metadaten veraltet oder von einer anderen Quelle; Audio kann trotzdem funktionieren.",
      RECENT:"Jüngere Beobachtung", STALE:"Ältere Beobachtung", UNKNOWN:"Unbekannt", NOT_OBSERVED:"Nicht beobachtet", SELECTION_MISMATCH:"Andere Quelle",
      DIRECT_CANDIDATE:"MP3-/AAC-Kandidat; Ton unbestätigt", HLS_REQUIRES_ADAPTATION:"HLS: Serveranpassung für diesen Weg nötig", FORMAT_UNVERIFIED:"Format nicht bestätigt"
    }
  };
  const s = {opened:false, busy:false, devices:[], device:"", data:null, snapshots:[], formats:null,
    qr:null, diff:null, replay:null, message:"", hours:24, days:7, label:"", auto:false};
  const t = () => window.BasswiesnI18n.scoped(words);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const active = () => document.body.classList.contains("lab-mode");
  const base = "/api/lab/workbench";
  const route = () => base + "/devices/" + encodeURIComponent(s.device);
  const date = value => value ? new Date(value).toLocaleString(document.documentElement.lang || "en") : t().unknown;
  const label = key => t()[key] || key;
  const button = (action, title, extra="") => '<button class="command" type="button" data-workbench="'+action+'" '+extra+' '+(s.busy?"disabled":"")+'>'+esc(title)+"</button>";
  const paragraph = value => '<p class="muted-copy">'+esc(value)+"</p>";
  function render() {
    const w=t(), disabled=s.busy?"disabled":"", snap=s.data?.snapshot, meta=snap?.metadata;
    panel.innerHTML='<h3>'+w.title+'</h3>'+paragraph(w.intro)
      +button("load", s.opened?w.refresh:w.open)
      +'<p id="workbench-status" role="status" aria-live="polite">'+esc(s.busy?w.busy:s.message)+"</p>";
    if(!s.opened) return;
    panel.innerHTML+='<label>'+w.device+'<select id="workbench-device" '+disabled+'><option value="">—</option>'
      +s.devices.map(d=>'<option value="'+esc(d.id)+'" '+(d.id===s.device?"selected":"")+'>'+esc(d.name || d.model || "Radio")+'</option>').join("")+"</select></label>";
    if(!s.devices.length) panel.innerHTML+=paragraph(w.empty);
    if(s.data && s.device) {
      panel.innerHTML+='<label class="toggle-line"><input type="checkbox" id="workbench-auto" '+(s.auto?"checked":"")+'>'+w.auto+'</label>'
        +'<div class="workbench-grid"><section><h4>'+w.diagnosis+'</h4><strong>'+w.cause+'</strong>'+paragraph(w.caution)
        +(s.data.diagnosis.observations.map(o=>'<p>'+esc(label(o.code))+' <small>'+esc(label(o.freshness)+" · "+date(o.observed_at))+'</small></p>').join("") || paragraph(w.noClues))
        +'</section><section><h4>'+w.metadata+'</h4>'+paragraph(w.metadataNote)+'<p>'+esc(label(meta.state))+'</p><dl>'
        +Object.entries(meta.fields).map(([k,v])=>'<dt>'+esc(label(k))+'</dt><dd>'+esc(v||"—")+'</dd>').join("")
        +'<dt>'+w.provenance+'</dt><dd>'+esc(meta.provenance)+'</dd><dt>'+w.age+'</dt><dd>'+esc(meta.age_seconds??w.unknown)+'</dd>'
        +'<dt>'+w.artwork+'</dt><dd>'+(meta.artwork_present?w.yes:w.no)+'</dd></dl></section></div>'
        +'<details><summary>'+w.timeline+'</summary><label>'+w.hours+'<select id="workbench-hours" '+disabled+'>'
        +[1,6,24,168].map(n=>'<option '+(s.hours===n?"selected":"")+' value="'+n+'">'+n+' '+w.hour+'</option>').join("")+'</select></label>'
        +(s.data.timeline.items.map(e=>'<p><time>'+esc(date(e.at))+'</time> · <code>'+esc(e.domain+" / "+e.code)+'</code> · '+esc(e.severity)+'</p>').join("")||paragraph(w.noEvents))
        +(s.data.timeline.truncated?paragraph(w.truncated):"")+"</details>"
        +'<h4>'+w.recent+'</h4><div id="workbench-recents">'
        +(s.data.recent.items.map((item,index)=>'<div class="event-row"><div><strong>'+esc(item.name)+'</strong><small>'+esc(date(item.last_played_at))+'</small></div>'
          +(item.replayable?button("play",w.replay,'data-index="'+index+'"'):esc(w.replayUnavailable))+"</div>").join("")||paragraph(w.noRecent))+"</div>"
        +(s.data.recent.truncated?paragraph(w.truncated):"")
        +(s.replay?'<details><summary>'+w.details+'</summary><pre>'+esc(JSON.stringify(s.replay,null,2))+'</pre></details>':"")
        +'<details><summary>'+w.shortcut+'</summary>'+paragraph(w.qrNote)+button("qr",w.qr)
        +(s.qr?'<p><img class="workbench-qr" alt="'+w.qrAlt+'" src="data:image/svg+xml;charset=utf-8,'+encodeURIComponent(s.qr.svg)+'"></p>'
          +'<a href="'+esc(s.qr.url)+'" target="_blank" rel="noopener noreferrer">'+w.openRemote+'</a><p class="workbench-link">'+esc(s.qr.url)+'</p>'+button("copy",w.copyLink):"")+"</details>"
        +'<details id="workbench-snapshots"><summary>'+w.snapshots+'</summary>'+paragraph(w.snapshotNote)
        +'<label>'+w.label+'<input id="workbench-label" maxlength="60" value="'+esc(s.label)+'" '+disabled+'></label>'+button("capture",w.capture)
        +(s.snapshots.map(item=>'<div class="event-row"><div><strong>'+esc(item.label||date(item.captured_at))+'</strong><small>'+esc(date(item.captured_at))+'</small></div><div class="inline-actions">'
          +button("compare",w.compare,'data-snapshot="'+item.id+'"')+button("download",w.download,'data-snapshot="'+item.id+'"')
          +button("remove",w.remove,'data-snapshot="'+item.id+'"')+'</div></div>').join("")||paragraph(w.noSnapshots))
        +(s.diff?'<p>'+esc(date(s.diff.before_at)+" → "+date(s.diff.after_at))+'</p>'+(s.diff.changes.length?
          '<div class="workbench-table"><table><thead><tr><th>'+w.field+'</th><th>'+w.before+'</th><th>'+w.after+'</th></tr></thead><tbody>'
          +s.diff.changes.map(d=>'<tr><td>'+esc(d.field)+'</td><td>'+esc(JSON.stringify(d.before))+'</td><td>'+esc(JSON.stringify(d.after))+'</td></tr>').join("")+'</tbody></table></div>':paragraph(w.noDiff)):"")+"</details>"
        +'<details><summary>'+w.export+'</summary>'+paragraph(w.csvNote)+'<label>'+w.days+'<select id="workbench-days">'
        +[1,7,30,365].map(n=>'<option '+(s.days===n?"selected":"")+'>'+n+'</option>').join("")+'</select></label>'+button("csv",w.csv)+"</details>";
    }
    if(s.formats) panel.innerHTML+='<details><summary>'+w.formats+'</summary>'+paragraph(w.formatNote)
      +'<div class="workbench-table"><table><thead><tr><th>'+w.station_name+'</th><th>'+w.format+'</th><th>'+w.assessment+'</th></tr></thead><tbody>'
      +s.formats.items.map(row=>'<tr><td>'+esc(row.name)+'</td><td>'+esc(row.format)+'</td><td>'+esc(label(row.assessment))+'</td></tr>').join("")
      +'</tbody></table></div>'+(s.formats.items.length?"":paragraph(w.noFormats))+(s.formats.truncated?paragraph(w.truncated):"")+"</details>";
  }
  function redraw() {
    const open=[...panel.querySelectorAll("details[open]")].map(el=>el.querySelector("summary")?.textContent);
    render();
    panel.querySelectorAll("details").forEach(el=>{if(open.includes(el.querySelector("summary")?.textContent)) el.open=true;});
  }
  async function run(fn, {silent=false}={}) {
    if(s.busy) return;
    if(!active()) {s.message=t().lab;redraw();return;}
    s.label=panel.querySelector("#workbench-label")?.value??s.label;
    s.busy=true;
    if(!silent) {s.message="";redraw();}
    try {await fn();}
    catch(error) {
      s.auto=false;
      s.message=t()[error.payload?.detail?.code] || t().failed;
    } finally {s.busy=false;redraw();}
  }
  async function data() {
    if(!s.device) return;
    const id=s.device;
    const [overview,snapshots]=await Promise.all([api.getJson(route()+"/overview?hours="+s.hours),api.getJson(route()+"/snapshots")]);
    if(id===s.device && active()) {s.data=overview;s.snapshots=snapshots.items;}
  }
  async function download(url, name) {
    const response=await fetch(url);
    if(!response.ok) throw new Error("Download failed");
    const blob=await response.blob(), href=URL.createObjectURL(blob), link=document.createElement("a");
    link.href=href;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);
  }
  panel.addEventListener("click",event=>{
    const b=event.target.closest("[data-workbench]");
    if(!b || b.disabled || s.busy) return;
    const action=b.dataset.workbench, item=s.data?.recent.items[Number(b.dataset.index)];
    if(action==="play" && (!item || !window.confirm(t().replayAsk))) return;
    if(action==="remove" && !window.confirm(t().removeAsk)) return;
    run(async()=>{
      if(action==="load") {
        const [devices,formats]=await Promise.all([api.getJson(base+"/devices"),api.getJson(base+"/formats")]);
        s.devices=devices.devices;s.formats=formats;s.opened=true;
        if(!s.devices.some(d=>d.id===s.device)) {s.device=s.devices[0]?.id||"";s.data=null;s.qr=null;s.diff=null;s.replay=null;s.snapshots=[];}
        await data();s.message=t().ready;
      }
      if(action==="qr") s.qr=await api.postJson(route()+"/qr",{origin:location.origin});
      if(action==="copy") {
        try {await navigator.clipboard.writeText(s.qr.url);s.message=t().copied;}
        catch {s.message=t().clipboardFailed;}
      }
      if(action==="play") {s.auto=false;s.replay=await api.postJson(route()+"/recent/"+item.history_id+"/play",{approve:true,revision:item.revision,safe_volume:1});await data();s.message=t().replaySent;}
      if(action==="capture") {await api.postJson(route()+"/snapshots",{label:s.label});s.label="";await data();s.message=t().snapshotSaved;}
      if(action==="compare") s.diff=await api.getJson(route()+"/snapshots/"+b.dataset.snapshot+"/compare");
      if(action==="download") await download(route()+"/snapshots/"+b.dataset.snapshot+"/download","basswiesn-cached-state.json");
      if(action==="remove") {await api.deleteJson(route()+"/snapshots/"+b.dataset.snapshot);s.diff=null;await data();}
      if(action==="csv") await download(route()+"/listening.csv?days="+s.days,"basswiesn-listening.csv");
    });
  });
  panel.addEventListener("change",event=>{
    if(event.target.id==="workbench-days") s.days=Number(event.target.value);
    if(event.target.id==="workbench-auto") s.auto=event.target.checked;
    if(event.target.id==="workbench-hours") {s.hours=Number(event.target.value);run(data);}
    if(event.target.id==="workbench-device") {
      s.device=event.target.value;s.data=null;s.qr=null;s.diff=null;s.replay=null;s.snapshots=[];s.auto=false;run(data);
    }
  });
  // This is browser-to-server cache refresh, not radio subscription/polling.
  // Hidden/background tabs and non-LAB modes do not refresh.
  let timer=null;
  function startTimer() {
    if(timer!==null) return;
    timer=setInterval(()=>{
      const editing=panel.contains(document.activeElement) && document.activeElement.matches('input:not([type=checkbox]),select,textarea');
      if(s.auto && !s.busy && !editing && active() && !document.hidden && panel.getClientRects().length) run(data,{silent:true});
    },10000);
  }
  window.addEventListener("pagehide",()=>{clearInterval(timer);timer=null;s.auto=false;});
  window.addEventListener("pageshow",()=>{startTimer();redraw();});
  startTimer();
  new MutationObserver(()=>redraw()).observe(document.documentElement,{attributes:true,attributeFilter:["lang"]});
  new MutationObserver(()=>{if(!active()) {s.auto=false;s.qr=null;}}).observe(document.body,{attributes:true,attributeFilter:["class"]});
  render();
})();
