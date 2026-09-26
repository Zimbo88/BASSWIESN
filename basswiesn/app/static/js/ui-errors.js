(function () {
  function showToast(message, tone = "ok") {
    const toast = document.getElementById("app-toast");
    if (!toast) return;
    toast.textContent = message;
    toast.className = `app-toast ${tone === "bad" ? "error" : tone}`;
    toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 5000);
  }

  function showApiError(error, context = "Aktion fehlgeschlagen") {
    const i=window.BasswiesnI18n;
    const fallback={error_generic:"Action failed",error_forbidden:"Check protection settings. Do not bypass a protected-device warning.",
      error_timeout:"No timely response. Check the recorded state before trying again.",
      error_server:"Inspect the earliest relevant diagnostic event. Do not repeat a write blindly."};
    const word=key=>i?.word302?.(key)||fallback[key];
    const code = error?.code && error.code !== "HTTP_ERROR" ? ` (${error.code})` : "";
    const next = error?.status === 403 ? word("error_forbidden")
      : error?.status === 502 || error?.status === 504 ? word("error_timeout")
      : error?.status >= 500 ? word("error_server") : "";
    const title=context==="Aktion fehlgeschlagen"?word("error_generic"):(i?.dynamic(context)||context);
    showToast(`${title}${code}: ${i?.dynamic(error?.message||String(error))||error?.message||String(error)} ${next}`, "error");
  }

  function setFormBusy(form, busy, label = "Wird ausgeführt") {
    const button = form?.querySelector('button[type="submit"]');
    if (!button) return;
    if (busy) {
      button.dataset.idleText = button.textContent;
      button.textContent = label;
      button.disabled = true;
      button.classList.add("is-busy");
    } else {
      button.textContent = button.dataset.idleText || button.textContent;
      button.disabled = false;
      button.classList.remove("is-busy");
      delete button.dataset.idleText;
    }
  }

  window.BasswiesnUi = { showToast, showApiError, setFormBusy };
}());
