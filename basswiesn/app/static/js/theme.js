/* Browser-local appearance; no backend write or device operation. */
(() => {
  const key = "basswiesn.appearance";
  const allowed = ["system", "light", "dark"];
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try { preference = localStorage.getItem(key) || "system"; } catch { /* storage may be disabled */ }
  if (!allowed.includes(preference)) preference = "system";
  function apply() {
    document.documentElement.dataset.theme = preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.querySelectorAll("[data-theme-select]").forEach(node => { node.value = preference; });
  }
  function localize() {
    const de = document.documentElement.lang === "de";
    const labels = window.BasswiesnI18n?.appearance?.() || (de ? { label: "Darstellung", system: "Wie Gerät", light: "Tag", dark: "Nacht" } : { label: "Appearance", system: "System", light: "Light", dark: "Dark" });
    document.querySelectorAll("[data-theme-label]").forEach(node => { node.textContent = labels.label; });
    document.querySelectorAll("[data-theme-select]").forEach(node => {
      node.setAttribute("aria-label", labels.label);
      for (const option of node.options) option.textContent = labels[option.value];
    });
  }
  apply(); // Before styles and first paint.
  media.addEventListener("change", apply);
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    preference = allowed.includes(event.newValue) ? event.newValue : "system";
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    localize();
    document.querySelectorAll("[data-theme-select]").forEach(node => node.addEventListener("change", () => {
      if (!allowed.includes(node.value)) return;
      preference = node.value;
      try { localStorage.setItem(key, preference); } catch { /* current tab still works */ }
      apply();
    }));
    new MutationObserver(localize).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
  });
})();
