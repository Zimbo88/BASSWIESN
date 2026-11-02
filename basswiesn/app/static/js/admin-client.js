/* Explicit transport helper: no global fetch monkeypatch, no credential storage. */
(function () {
  let sessionRead = null;
  let leaving = false;
  const reads = new Set();
  function stopReads() {
    leaving = true;
    for (const controller of reads) controller.abort();
    reads.clear();
  }
  window.addEventListener("beforeunload", stopReads);
  window.addEventListener("pagehide", stopReads);
  window.addEventListener("pageshow", () => { leaving = false; });
  async function currentSession() {
    if (!sessionRead) {
      sessionRead = fetch("/auth/session", {cache: "no-store", credentials: "same-origin"})
        .then(async response => {
          if (!response.ok) throw new Error("Administrator session unavailable; sign in again.");
          return response.json();
        }).catch(error => { sessionRead = null; throw error; });
    }
    return sessionRead;
  }

  async function adminFetch(input, options = {}) {
    if (leaving) throw new DOMException("Page is leaving", "AbortError");
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = String(options.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    let requestOptions = options;
    // Never leak the synchronizer token to a provider or another listener.
    if (url.origin === location.origin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      const session = await currentSession();
      if (session.enabled) {
        const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
        headers.set("X-BASSWIESN-CSRF", session.csrf_token);
        requestOptions = {...options, headers, credentials: "same-origin"};
      }
    }
    // Login/session lookup may have completed after navigation began. A write
    // not yet sent must not start now; an already-sent write is never aborted.
    if (leaving) throw new DOMException("Page is leaving", "AbortError");
    const readController = ["GET", "HEAD"].includes(method) ? new AbortController() : null;
    const upstreamSignal = options.signal || (input instanceof Request ? input.signal : null);
    const cancelRead = () => readController?.abort();
    if (readController) {
      reads.add(readController);
      if (upstreamSignal?.aborted) readController.abort();
      else upstreamSignal?.addEventListener("abort", cancelRead, {once: true});
      requestOptions = {...requestOptions, signal: readController.signal};
    }
    try {
      const response = await fetch(input, requestOptions);
      if ([401, 403].includes(response.status)) sessionRead = null;
      // In particular, never replay a failed or uncertain mutation after login.
      return response;
    } finally {
      if (readController) reads.delete(readController);
      upstreamSignal?.removeEventListener("abort", cancelRead);
    }
  }

  window.BasswiesnAuth = {fetch: adminFetch, currentSession};
  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const session = await currentSession();
      if (!session.enabled) return;
      const tokens = document.createElement("a");
      tokens.href = "/auth/automation";
      tokens.id = "admin-tokens-link";
      tokens.textContent = navigator.language.startsWith("de") ? "Automatisierungszugriff" : "Automation access";
      if (location.pathname !== "/auth/automation") {
        (document.querySelector("#view-system-settings") || document.querySelector("main") || document.body).append(tokens);
      }
      const logout = document.createElement("button");
      logout.type = "button";
      logout.id = "admin-logout";
      logout.textContent = navigator.language.startsWith("de") ? "Abmelden" : "Sign out";
      logout.addEventListener("click", async () => {
        logout.disabled = true;
        try {
          const response = await adminFetch("/auth/logout", {method: "POST"});
          if (!response.ok) throw new Error("Logout could not be confirmed");
          sessionRead = null;
          stopReads();
          location.assign("/auth/login");
        } catch {
          logout.disabled = false;
          logout.textContent = navigator.language.startsWith("de") ? "Abmeldung nicht bestätigt – erneut versuchen" : "Sign-out not confirmed – try again";
        }
      });
      (document.querySelector(".topbar") || document.querySelector("main") || document.body).append(logout);
    } catch {
      // No automatic redirect while a write may still be in flight. A normal
      // navigation goes to login; API clients receive the original auth error.
    }
  });
}());
