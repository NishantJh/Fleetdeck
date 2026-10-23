"use strict";
// Shared by the dashboard, the setup page and the login page: remembered light/dark choice, a toggle button,
// the logo and the Sign out button.
(() => {
  const KEY = "homelab-theme";
  const root = document.documentElement;
  const read = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const dark = () => root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  const SIGN_OUT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>';
  const LOGO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/></svg>';
  const saved = read();
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;

  // Over plain HTTP from another machine the password and the session cookie cross the network in clear text.
  const local = /^(localhost|127\.0\.0\.1|\[::1\]|.*\.localhost)$/.test(location.hostname);
  const plainHttp = location.protocol === "http:" && !local;

  addEventListener("DOMContentLoaded", () => {
    const main = document.querySelector("main");
    if (plainHttp && main) {
      const warn = document.createElement("div");
      warn.className = "warn-banner";
      warn.setAttribute("role", "alert");
      warn.textContent = "This page is open over plain HTTP, so the password and everything on it can be read on the network. " +
        "Put the dashboard behind HTTPS (see \"Put it behind HTTPS\" in the README).";
      main.prepend(warn);
    }
    const h1 = document.querySelector("header h1");
    if (h1) {
      const brand = document.createElement("div");
      brand.className = "brand";
      brand.innerHTML = `<span class="logo">${LOGO}</span>`;
      h1.replaceWith(brand);
      brand.append(h1);
    }
    const bar = document.querySelector("header .toolbar");
    if (!bar) return;
    const btn = document.createElement("button");
    btn.className = "icon";
    btn.type = "button";
    const sync = () => { btn.innerHTML = dark() ? SUN : MOON; btn.title = btn.ariaLabel = dark() ? "Switch to light theme" : "Switch to dark theme"; };
    btn.onclick = () => {
      root.dataset.theme = dark() ? "light" : "dark";
      try { localStorage.setItem(KEY, root.dataset.theme); } catch { /* private mode: just don't remember it */ }
      sync();
    };
    sync();
    bar.append(btn);
    if (location.pathname !== "/login") addSignOut(bar);
  });

  // Only when the dashboard has a login (and this browser is in it).
  async function addSignOut(bar) {
    let session;
    try { session = await (await fetch("/api/session")).json(); } catch { return; }
    if (!session?.login) return;
    const out = document.createElement("button");
    out.className = "icon";
    out.type = "button";
    out.innerHTML = SIGN_OUT;
    out.title = out.ariaLabel = `Sign out (${session.username})`;
    out.onclick = async () => {
      try { await fetch("/api/logout", { method: "POST" }); } finally { location.href = "/login"; }
    };
    bar.append(out);
  }
})();
