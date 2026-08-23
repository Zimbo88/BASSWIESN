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
    const code = error?.code && error.code !== "HTTP_ERROR" ? ` (${error.code})` : "";
    const next = error?.status === 403 ? " Einstellungen und IP Write Guard prüfen."
      : error?.status === 502 || error?.status === 504 ? " Radio und Netzwerk prüfen; danach erneut versuchen."
      : error?.status >= 500 ? " Logs oder Support Bundle prüfen und erneut versuchen." : "";
    showToast(`${context}${code}: ${error?.message || String(error)}${next}`, "error");
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
