"use strict";
// The login page: posts the username and password once, the server answers with a session cookie.
(() => {
  const $ = (sel) => document.querySelector(sel);
  const form = $("#login-form"), user = $("#login-user"), pass = $("#login-pass"), submit = $("#login-submit");
  const error = $("#login-error");

  // Only a path on this site; the server checks it too.
  const wanted = new URLSearchParams(location.search).get("next") || "/";
  const next = wanted.startsWith("/") && !wanted.startsWith("//") && !wanted.includes("\\") ? wanted : "/";

  let unblockAt = 0, timer = null;

  const showError = (text) => {
    error.textContent = text;
    error.hidden = false;
    form.classList.remove("shake");
    void form.offsetWidth; // restart the animation
    form.classList.add("shake");
  };
  const busy = (on) => {
    submit.disabled = on || Date.now() < unblockAt;
    submit.classList.toggle("busy", on);
    form.setAttribute("aria-busy", String(on));
  };

  // Locked out after too many wrong passwords: count down on the button, then let them try again.
  const lockFor = (seconds) => {
    unblockAt = Date.now() + seconds * 1000;
    clearInterval(timer);
    const tick = () => {
      const left = Math.ceil((unblockAt - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(timer);
        submit.disabled = false;
        $(".login-label").textContent = "Sign in";
        return;
      }
      submit.disabled = true;
      $(".login-label").textContent = `Try again in ${left >= 60 ? `${Math.ceil(left / 60)} min` : `${left}s`}`;
    };
    tick();
    timer = setInterval(tick, 1000);
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    user.classList.toggle("invalid", !user.value.trim());
    pass.classList.toggle("invalid", !pass.value);
    if (!user.value.trim() || !pass.value) {
      showError("Enter your username and password.");
      (user.value.trim() ? pass : user).focus();
      return;
    }
    error.hidden = true;
    busy(true);
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user.value.trim(), password: pass.value, remember: $("#login-remember").checked }),
      });
      if (r.ok) {
        submit.classList.add("done");
        $(".login-label").textContent = "Signed in";
        location.replace(next);
        return;
      }
      const body = await r.json().catch(() => ({}));
      busy(false);
      if (r.status === 429) {
        showError(body.detail || "Too many failed attempts. Try again later.");
        lockFor(body.retry_after || Number(r.headers.get("Retry-After")) || 300);
      } else if (r.status === 401) {
        showError(body.detail || "Wrong username or password.");
        pass.classList.add("invalid");
        pass.select();
      } else {
        showError(`Could not sign in (${r.status}). Try again.`);
      }
    } catch {
      busy(false);
      showError("Could not reach the dashboard. Check your connection and try again.");
    }
  });

  for (const input of [user, pass]) input.addEventListener("input", () => input.classList.remove("invalid"));

  const reveal = $("#login-reveal");
  reveal.addEventListener("click", () => {
    const show = pass.type === "password";
    pass.type = show ? "text" : "password";
    reveal.setAttribute("aria-pressed", String(show));
    reveal.title = reveal.ariaLabel = show ? "Hide password" : "Show password";
    pass.focus();
  });

  const caps = $("#login-caps");
  const capsCheck = (e) => { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); };
  pass.addEventListener("keydown", capsCheck);
  pass.addEventListener("keyup", capsCheck);
  pass.addEventListener("blur", () => (caps.hidden = true));

  // A browser that filled the username in already: start on the password.
  if (user.value) pass.focus();
})();
