/* Browser-local reading preferences. No API writes, analytics or network. */
(() => {
  "use strict";
  const main=document.querySelector("main"),i=window.BasswiesnI18n;
  if(!main||!i?.word302) return;
  const key="basswiesn.reading",allowed=["standard","large","largest"];
  let preference={size:"standard",contrast:false};
  try {const saved=JSON.parse(localStorage.getItem(key)||"null");if(saved&&allowed.includes(saved.size)&&typeof saved.contrast==="boolean")preference=saved;}catch{/* local only */}
  const w=k=>i.word302(k),panel=document.createElement("details");panel.className="reading-preferences";panel.dataset.authoredCopy="true";
  const heading=document.createElement("summary"),label=document.createElement("label"),caption=document.createElement("span"),select=document.createElement("select");
  select.id="reading-size";for(const value of allowed)select.add(new Option("",value));label.append(caption,select);
  const toggleLabel=document.createElement("label"),toggle=document.createElement("input"),toggleText=document.createElement("span");
  toggle.type="checkbox";toggle.id="reading-contrast";toggleLabel.append(toggle,toggleText);panel.append(heading,label,toggleLabel);
  // Keep these browser-local controls reachable even in Easy mode, where
  // server settings are intentionally absent from the navigation.
  main.append(panel);
  if(!main.id)main.id="basswiesn-main";
  main.tabIndex=-1;
  const skip=document.createElement("a");skip.className="skip-content";skip.href="#"+main.id;skip.dataset.authoredCopy="true";
  skip.addEventListener("click",()=>main.focus());document.body.prepend(skip);
  function labels(){heading.textContent=w("accessibility");caption.textContent=w("text_size");toggleText.textContent=w("contrast");skip.textContent=w("skip");for(const option of select.options)option.textContent=w(option.value);}
  function apply(){document.documentElement.dataset.textSize=preference.size;document.documentElement.dataset.contrast=preference.contrast?"high":"normal";select.value=preference.size;toggle.checked=preference.contrast;}
  function save(){preference={size:select.value,contrast:toggle.checked};apply();try{localStorage.setItem(key,JSON.stringify(preference));}catch{/* current tab remains usable */}}
  select.addEventListener("change",save);toggle.addEventListener("change",save);
  window.addEventListener("storage",e=>{if(e.key!==key)return;try{const v=JSON.parse(e.newValue);if(v&&allowed.includes(v.size)&&typeof v.contrast==="boolean"){preference=v;apply();}}catch{/* ignore invalid data */}});
  new MutationObserver(labels).observe(document.documentElement,{attributes:true,attributeFilter:["lang"]});labels();apply();
})();
