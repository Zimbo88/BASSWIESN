/* Explicit media-server browser. Loading this widget never contacts a NAS/radio. */
(() => {
  "use strict";
  const panel = document.getElementById("dlna-library-panel");
  if (!panel) return;
  const api = window.BasswiesnApi;
  const copy = {
    en: {title:"DLNA media library", enable:"Enable media library", address:"Server description URL",
      connect:"Connect this server", setup:"Add a media server", browse:"Open library", server:"Media server", root:"Root", back:"Back",
      next:"Next page", open:"Open folder", add:"Add to stations", remove:"Forget server",
      note:"Enter the HTTP device-description URL from your media server. No network scan or radio action.",
      help:"LAB preview: browse folders and add an MP3/AAC track to Stations. Radio-audio acceptance is still pending; presets are unchanged.",
      empty:"No media servers connected.", emptyFolder:"This folder is empty.", busy:"Working…",
      noMore:"No more entries.", unknownTotal:"The server has not reported a total. Use Next page to check for more entries.",
      pageLimit:"The browsing limit has been reached. Open a more specific folder.",
      ready:"Ready.", added:"Track saved in Stations. No radio was started.", unsupported:"Format not supported",
      confirm:"Forget this server? Imported stations remain saved but stop working. Remove them or import the tracks again after reconnecting.",
      error:"The operation could not be completed.", identity:"Server identity changed. Check the address before reconnecting.",
      disabled:"Enable the library first.", blocked:"This address is blocked. Use an approved media server, not a radio.",
      transport:"The server did not return a valid response. Check its address and UPnP settings."},
    de: {title:"DLNA-Musikbibliothek", enable:"Musikbibliothek aktivieren", address:"Beschreibungs-URL des Servers",
      connect:"Diesen Server verbinden", setup:"Medienserver hinzufügen", browse:"Bibliothek öffnen", server:"Medienserver", root:"Startordner", back:"Zurück",
      next:"Nächste Seite", open:"Ordner öffnen", add:"Zur Senderliste hinzufügen", remove:"Server vergessen",
      note:"Trage die HTTP-Gerätebeschreibungs-URL deines Medienservers ein. Keine Netzwerksuche oder Radioaktion.",
      help:"LAB-Vorschau: Ordner durchsuchen und MP3-/AAC-Titel zur Senderliste hinzufügen. Die Audioabnahme am Radio steht noch aus; Presets bleiben unverändert.",
      empty:"Noch kein Medienserver verbunden.", emptyFolder:"Dieser Ordner ist leer.", busy:"Wird bearbeitet …",
      noMore:"Keine weiteren Einträge.", unknownTotal:"Der Server meldet keine Gesamtzahl. Mit „Nächste Seite“ nach weiteren Einträgen suchen.",
      pageLimit:"Die Grenze für das Blättern ist erreicht. Bitte einen gezielteren Ordner öffnen.",
      ready:"Bereit.", added:"Titel in der Senderliste gespeichert. Kein Radio wurde gestartet.", unsupported:"Format nicht unterstützt",
      confirm:"Server vergessen? Importierte Sender bleiben gespeichert, sind aber nicht mehr abspielbar. Entferne sie oder importiere die Titel nach dem erneuten Verbinden noch einmal.",
      error:"Der Vorgang konnte nicht abgeschlossen werden.", identity:"Serveridentität hat sich geändert. Adresse vor dem erneuten Verbinden prüfen.",
      disabled:"Bitte zuerst die Bibliothek aktivieren.", blocked:"Diese Adresse ist gesperrt. Einen freigegebenen Medienserver verwenden, kein Radio.",
      transport:"Keine gültige Serverantwort. Adresse und UPnP-Einstellungen prüfen."}
  };
  const state = {enabled:false, servers:[], server:"", object:"0", parents:[], page:null, busy:false, message:"", url:""};
  const labels = () => copy[document.documentElement.lang] || copy.en;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const route = () => "/api/dlna/servers/" + encodeURIComponent(state.server);
  function render() {
    const t=labels(), disabled=state.busy || !state.enabled;
    panel.innerHTML = '<h3>'+t.title+'</h3><p class="muted-copy">'+t.help+'</p>'
      +'<label class="toggle-line"><input id="dlna-enabled" type="checkbox" '+(state.enabled?"checked ":"")+(state.busy?"disabled":"")+">"+t.enable+"</label>"
      +'<details id="dlna-server-setup" '+(!state.servers.length?"open":"")+'><summary>'+t.setup+'</summary><form id="dlna-connect-form" class="settings-form"><label>'+t.address
      +'<input id="dlna-description-url" type="url" maxlength="2048" placeholder="http://192.0.2.42:8200/rootDesc.xml" value="'+esc(state.url)+'" required '+(disabled?"disabled":"")+"></label>"
      +'<p class="muted-copy">'+t.note+'</p><button class="command primary" type="submit" '+(disabled?"disabled":"")+">"+t.connect+"</button></form></details>"
      +'<div class="inline-actions"><label>'+t.server+'<select id="dlna-server-select" '+(disabled?"disabled":"")+'>'
      +state.servers.map(s=>'<option value="'+esc(s.id)+'" '+(s.id===state.server?"selected":"")+">"+esc(s.name)+"</option>").join("")
      +'</select></label><button class="command" data-dlna="browse" '+(disabled||!state.server?"disabled":"")+">"+t.browse+"</button>"
      +'<button class="command" data-dlna="forget" '+(state.busy||!state.server?"disabled":"")+">"+t.remove+"</button></div>"
      +'<p id="dlna-status" role="status" aria-live="polite">'+esc(state.busy?t.busy:state.message||(!state.servers.length?t.empty:t.ready))+"</p>"
      +'<div class="inline-actions"><button class="command" data-dlna="back" '+(disabled||!state.parents.length?"disabled":"")+">"+t.back+"</button>"
      +'<button class="command" data-dlna="root" '+(disabled||!state.server?"disabled":"")+">"+t.root+"</button></div>"
      +'<div class="event-list" id="dlna-items">'+(state.page?.items || []).map((item,index)=>'<div class="event-row dlna-item"><div><strong>'+esc(item.title)
        +'</strong><small>'+esc([item.artist,item.album,...item.formats].filter(Boolean).join(" · "))+'</small></div><button class="command" data-dlna="'
        +(item.kind==="container"?"open":"import")+'" data-item="'+index+'" '+(disabled || item.kind!=="container"&&!item.importable?"disabled":"")+">"
        +(item.kind==="container"?t.open:item.importable?t.add:t.unsupported)+"</button></div>").join("")
      +(state.page && !state.page.items.length?'<p class="empty">'+(state.page.start?t.noMore:t.emptyFolder)+"</p>":"")+"</div>"
      +(state.page?.pagination_limited?'<p class="muted-copy">'+t.pageLimit+'</p>':
        state.page?.total===null?'<p class="muted-copy">'+t.unknownTotal+'</p>':"")
      +'<button class="command" data-dlna="next" '+(disabled||state.page?.next_start==null?"disabled":"")+">"+t.next+"</button>";
  }
  async function run(action) {
    if(state.busy) return;
    state.url=panel.querySelector("#dlna-description-url")?.value || state.url;
    state.busy=true; state.message=""; render();
    try { await action(); }
    catch(error) {
      const code=error.payload?.detail?.code;
      state.message=labels()[code==="SERVER_IDENTITY_CHANGED"?"identity":code==="FEATURE_DISABLED"?"disabled":
        ["PROTECTED_TARGET","RADIO_TARGET","INVALID_ENDPOINT","CROSS_ORIGIN"].includes(code)?"blocked":
        ["TRANSPORT_ERROR","HTTP_ERROR","SOAP_FAULT","INVALID_RESPONSE","CONTENT_DIRECTORY_NOT_UNIQUE"].includes(code)?"transport":"error"];
    }
    finally { state.busy=false; render(); }
  }
  async function load() {
    const data=await api.getJson("/api/dlna/servers");
    state.enabled=data.enabled; state.servers=data.servers;
    if(!state.servers.some(s=>s.id===state.server)) state.server=state.servers[0]?.id || "";
  }
  async function browse(object="0", start=0) {
    const page=await api.postJson(route()+"/browse",{object_id:object,start,count:50,expected_update_id:start?state.page?.update_id:null});
    state.object=object; state.page=page;
  }
  panel.addEventListener("change", event=>{
    if(event.target.id==="dlna-enabled") {
      const value=event.target.checked;
      run(async()=>{const result=await api.postJson("/api/dlna/settings",{enabled:value});state.enabled=result.enabled;});
    }
    if(event.target.id==="dlna-server-select") {
      state.server=event.target.value; state.object="0"; state.parents=[]; state.page=null; render();
    }
  });
  panel.addEventListener("submit", event=>{
    if(event.target.id!=="dlna-connect-form") return;
    event.preventDefault();
    const description_url=panel.querySelector("#dlna-description-url").value;
    run(async()=>{
      const server=await api.postJson("/api/dlna/servers",{description_url,approve:true});
      state.server=server.id; await load(); state.parents=[]; state.page=null; await browse();
    });
  });
  panel.addEventListener("click", event=>{
    const button=event.target.closest("[data-dlna]");
    if(!button || button.disabled) return;
    const action=button.dataset.dlna, item=state.page?.items[Number(button.dataset.item)];
    if(action==="forget" && !window.confirm(labels().confirm)) return;
    run(async()=>{
      if(action==="forget") {await api.deleteJson(route());state.page=null;state.parents=[];await load();}
      if(action==="browse" || action==="root") {await browse();state.parents=[];}
      if(action==="open") {const parent=state.object;await browse(item.id);state.parents.push(parent);}
      if(action==="back") {await browse(state.parents[state.parents.length-1]);state.parents.pop();}
      if(action==="next") await browse(state.object,state.page.next_start);
      if(action==="import") {
        await api.postJson(route()+"/items",{object_id:item.id,approve:true});
        if(typeof window.refreshStations==="function") await window.refreshStations();
        state.message=labels().added;
      }
    });
  });
  new MutationObserver(()=>{state.message="";render();}).observe(document.documentElement,{attributes:true,attributeFilter:["lang"]});
  document.getElementById("reload-media")?.addEventListener("click",()=>run(load));
  render(); run(load);
})();
