/* Synthetic LAB practice and app-only profiles. No radio IDs or radio APIs. */
(() => {
  "use strict";
  const panel=document.querySelector("#lab-learning");
  if(!panel) return;
  panel.dataset.authoredCopy="true";
  const base="/api/lab/learning", keys=["station","artist","title","clock","other"];
  const w=key=>window.BasswiesnI18n.word302(key);
  const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const active=()=>document.body.classList.contains("lab-mode");
  const s={open:false,busy:false,scenarios:[],scenario:"network_loss",step:0,result:null,profiles:[],
    fields:["station","artist","title","clock"],order:[...keys],name:"",preview:null,sample:"normal",message:""};
  const button=(id,key,disabled=false,extra="")=>`<button id="${id}" type="button" class="command" ${disabled||s.busy?"disabled":""} ${extra}>${esc(w(key))}</button>`;
  const layout=()=>({fields:s.order.filter(k=>s.fields.includes(k)),field_order:[...s.order]});
  async function request(path,body,method="POST") {
    const response=await fetch(base+path,body===undefined?{cache:"no-store"}:{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok) throw Object.assign(new Error("request failed"),{code:data?.detail?.code});
    return data;
  }
  function render() {
    const focus=panel.contains(document.activeElement)?document.activeElement.id:null;
    panel.setAttribute("aria-busy",String(s.busy));
    panel.innerHTML=`<h3>${esc(w("learning"))}</h3><p>${esc(w("inert"))}</p>${button("learning-open","open")}
      <p id="learning-status" role="status" aria-live="polite">${esc(w(s.busy?"busy":s.message))}</p>
      <details><summary>${esc(w("meaning"))}</summary><p>${esc(w("help_learning"))}</p></details>`;
    if(!s.open) return;
    const result=s.result;
    panel.innerHTML+=`<div class="learning-grid"><section><h4>${esc(w("simulator"))}</h4>
      <strong class="simulation-badge">${esc(w("simulated"))}</strong>
      <label>${esc(w("scenario"))}<select id="learning-scenario" ${s.busy?"disabled":""}>${s.scenarios.map(row=>`<option value="${esc(row.id)}" ${row.id===s.scenario?"selected":""}>${esc(w(row.id))}</option>`).join("")}</select></label>
      <div class="button-row">${button("learning-reset","reset")}${button("learning-next","next",s.step>=3||!result)}</div>
      <div id="learning-result" role="status" aria-live="polite">${result?`<h5>${esc(w("observed"))} ${result.step+1} / ${result.steps}</h5><p>${esc(w(result.phase))}</p>
      ${result.diagnosis.observations.map(o=>`<details><summary>${esc(w("meaning"))} · ${esc(o.layer)}</summary><p>${esc(w(o.meaning==="stopped"?"stopped_meaning":o.meaning))}</p><h5>${esc(w("next_step"))}</h5><p>${esc(w(o.next_step))}</p><p>${esc(w(o.not_proven))}</p></details>`).join("")}
      ${result.zone?`<p>${esc(w("join_unknown"))}</p>`:""}<p>${esc(w("first_event"))}</p>`:""}</div>
      </section><section><h4>${esc(w("profiles"))}</h4><p>${esc(w("profile_note"))}</p>
      <label>${esc(w("profile_name"))}<input id="learning-name" maxlength="40" value="${esc(s.name)}" ${s.busy?"disabled":""}></label>
      <fieldset ${s.busy?"disabled":""}><legend>${esc(w("fields"))}</legend>${s.order.map((key,index)=>`<div class="profile-field"><label><input id="learning-field-${key}" type="checkbox" data-field="${key}" ${s.fields.includes(key)?"checked":""}>${esc(w(key))}</label><div>${button("learning-up-"+key,"up",index===0,`data-move="-1" data-field="${key}" aria-label="${esc(w(key)+": "+w("up"))}"`)}${button("learning-down-"+key,"down",index===4,`data-move="1" data-field="${key}" aria-label="${esc(w(key)+": "+w("down"))}"`)}</div></div>`).join("")}</fieldset>
      <label>${esc(w("preview"))}<select id="learning-sample" ${s.busy?"disabled":""}>${["normal","missing","long"].map(k=>`<option value="${k}" ${k===s.sample?"selected":""}>${esc(w(k))}</option>`).join("")}</select></label>
      <div class="button-row">${button("learning-preview","preview")}${button("learning-save","save")}</div>
      <p>${esc(w("preview_note"))}</p><output id="learning-preview-output" aria-live="polite">${esc(s.preview?.title??"")}</output>
      <h5>${esc(w("saved"))}</h5>${s.profiles.length?s.profiles.map((p,index)=>`<div class="event-row"><strong>${esc(p.name)}</strong><div class="button-row">${button("learning-load-"+index,"load",false,`data-profile="${index}" data-action="load"`)}${button("learning-remove-"+index,"remove",false,`data-profile="${index}" data-action="remove"`)}</div></div>`).join(""):`<p>${esc(w("no_profiles"))}</p>`}
      </section></div>`;
    if(focus) document.getElementById(focus)?.focus();
  }
  async function run(action) {
    if(s.busy||!active()) return;
    s.busy=true; s.message=""; render();
    try {await action();}
    catch(error) {s.message=window.Basswiesn302Catalogs.en[error.code]?error.code:"failed";}
    finally {s.busy=false; if(!active()) {s.open=false;s.result=null;} render();}
  }
  async function simulation() {s.result=await request("/simulator",{scenario:s.scenario,step:s.step});}
  async function profiles() {s.profiles=(await request("/display-profiles")).items;}
  panel.addEventListener("input",e=>{if(e.target.id==="learning-name")s.name=e.target.value;});
  panel.addEventListener("change",e=>{
    if(e.target.id==="learning-scenario") {s.scenario=e.target.value;s.step=0;run(simulation);}
    if(e.target.id==="learning-sample") {s.sample=e.target.value;s.preview=null;render();}
    if(e.target.matches('input[data-field]')) {const key=e.target.dataset.field;s.fields=s.order.filter(k=>k===key?e.target.checked:s.fields.includes(k));s.preview=null;render();}
  });
  panel.addEventListener("click",e=>{
    const b=e.target.closest("button");
    if(!b||b.disabled||s.busy||!active()) return;
    if(b.dataset.move) {
      const index=s.order.indexOf(b.dataset.field),next=index+Number(b.dataset.move);
      if(index<0||next<0||next>=s.order.length) return;
      [s.order[index],s.order[next]]=[s.order[next],s.order[index]];s.preview=null;render();
      const moved=document.getElementById(b.id);(moved?.disabled?document.getElementById("learning-field-"+b.dataset.field):moved)?.focus();return;
    }
    if(b.dataset.action==="load") {
      const p=s.profiles[Number(b.dataset.profile)];if(!p)return;
      s.name=p.name;s.fields=[...p.fields];s.order=[...p.field_order];s.preview=null;render();return;
    }
    if(b.dataset.action==="remove"&&!window.confirm(w("delete_question"))) return;
    run(async()=>{
      if(b.id==="learning-open") {s.scenarios=(await request("/simulator")).scenarios;await profiles();s.open=true;await simulation();}
      if(b.id==="learning-reset") {s.step=0;await simulation();}
      if(b.id==="learning-next") {s.step=Math.min(3,s.step+1);await simulation();}
      if(b.id==="learning-preview") s.preview=await request("/display-preview",{...layout(),scenario:s.sample});
      if(b.id==="learning-save") {
        if(!s.name.trim()) {s.message="profile_name";return;}
        await request("/display-profiles",{name:s.name.trim(),...layout()});await profiles();s.message="saved_ok";
      }
      if(b.dataset.action==="remove") {const p=s.profiles[Number(b.dataset.profile)];await request("/display-profiles/"+p.id,{revision:p.revision},"DELETE");await profiles();s.message="deleted";}
    });
  });
  new MutationObserver(render).observe(document.documentElement,{attributes:true,attributeFilter:["lang"]});
  new MutationObserver(()=>{if(!active()){s.open=false;s.result=null;render();}}).observe(document.body,{attributes:true,attributeFilter:["class"]});
  render();
})();
