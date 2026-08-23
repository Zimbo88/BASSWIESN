(function () {
  function errorMessage(payload, response) {
    return payload?.error?.message
      || payload?.detail?.message
      || payload?.detail?.error
      || (typeof payload?.detail === "string" ? payload.detail : "")
      || `${response.status} ${response.statusText}`;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    const raw = await response.text();
    let payload = null;
    if (raw) {
      try { payload = JSON.parse(raw); } catch { payload = null; }
    }
    if (!response.ok) {
      const base = payload ? errorMessage(payload, response) : (raw || `${response.status} ${response.statusText}`);
      const error = new Error(`${base} · Endpoint: ${url}`);
      error.code = payload?.error?.code || "HTTP_ERROR";
      error.status = response.status;
      error.endpoint = url;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  window.BasswiesnApi = {
    getJson: (url, options = {}) => requestJson(url, options),
    postJson: (url, data) => requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),
    putJson: (url, data) => requestJson(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),
    deleteJson: (url) => requestJson(url, { method: "DELETE" }),
  };
}());
