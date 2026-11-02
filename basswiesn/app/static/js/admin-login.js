(function () {
  const de = navigator.language.startsWith("de");
  const text = de ? {
    title: "Administrator-Anmeldung", account: "Konto", password: "Passwort", submit: "Anmelden",
    help: "Konto lokal mit python -m basswiesn.admin einrichten oder wiederherstellen. Es gibt kein Standardpasswort.",
    failed: "Anmeldung nicht möglich. Konto, Passwort und lokale Einrichtung prüfen.",
    limited: "Zu viele Anmeldeversuche. Bitte mindestens eine Minute warten.",
  } : {
    title: "Administrator sign in", account: "Account", password: "Password", submit: "Sign in",
    help: "Create or recover the account locally with python -m basswiesn.admin. No default password exists.",
    failed: "Sign-in failed. Check the account, password and local configuration.",
    limited: "Too many login attempts. Wait at least one minute.",
  };
  document.documentElement.lang = de ? "de" : "en";
  for (const [id, key] of Object.entries({"login-title": "title", "username-label": "account", "password-label": "password", "login-submit": "submit", "bootstrap-help": "help"})) {
    document.getElementById(id).textContent = text[key];
  }
  document.getElementById("admin-login").addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.getElementById("login-submit");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch("/auth/login", {method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({
          username: document.getElementById("username").value, password: document.getElementById("password").value,
        })});
      document.getElementById("password").value = "";
      if (response.ok) { location.assign("/"); return; }
      document.getElementById("login-status").textContent = response.status === 429 ? text.limited : text.failed;
    } catch {
      document.getElementById("password").value = "";
      document.getElementById("login-status").textContent = text.failed;
    } finally { button.disabled = false; }
  });
}());
