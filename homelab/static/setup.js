"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const HINTS = {
  proxmox: "On Connect the dashboard creates its own API user, role and token on this node and pins its certificate. For a cluster, add one node and run the script on every node; Connect checks the others. Put the VMID of the LXC running this dashboard under protect: in the config editor below.",
  incus: "On Connect the dashboard turns on the HTTPS API (port 8443, or the next free one; on this address only) if it's off and trusts its own client certificate for the existing projects. Works for LXD too.",
  host: "A Raspberry Pi, a VPS or any other Linux machine. If its firewall only accepts SSH from some addresses, allow the dashboard's (for a VPS: your home's public IP). Run the script before pressing Connect: failed logins can get the dashboard banned by fail2ban or CrowdSec.",
};
const DRAFTS = "homelab-setup-drafts";
const ALLOW = "homelab-setup-allow-from";

// The one-time link from the logs carries the setup token in the URL fragment (never sent to the server).
const hashToken = new URLSearchParams(location.hash.slice(1)).get("token");
if (hashToken) {
  sessionStorage.setItem("setup-token", hashToken);
  history.replaceState(null, "", location.pathname);
}
const token = sessionStorage.getItem("setup-token");
let state = null;

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Setup-Token"] = token;
  const r = await fetch(path, { ...opts, headers });
  const text = await r.text();
  if (!r.ok) {
    let msg = text;
    try { msg = JSON.parse(text).detail ?? text; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return text ? JSON.parse(text) : null;
}

function notice(msg, kind = "error") {
  $("#notice").innerHTML = "";
  if (!msg) return;
  const div = document.createElement("div");
  div.className = kind === "error" ? "error-banner" : "info-banner";
  div.textContent = msg;
  $("#notice").append(div);
}

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Plain-HTTP pages have no clipboard API; fall back to selecting the text.
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const was = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => (button.textContent = was), 1500);
}

function machineOf(card) {
  return {
    kind: $(".f-kind", card).value,
    name: $(".f-name", card).value.trim(),
    address: $(".f-address", card).value.trim(),
    port: Number($(".f-port", card).value) || 22,
    tunnel: $(".f-kind", card).value === "host" && $(".f-tunnel", card).checked,
    udp_port: Number($(".f-udp", card).value) || 51821,
  };
}

function saveDrafts() {
  const drafts = [...document.querySelectorAll(".card")].filter((c) => !c.dataset.saved && !c.dataset.removed).map(machineOf);
  save(DRAFTS, drafts);
}

function setStatus(card, text, cls = "") {
  const s = $(".status", card);
  s.textContent = text;
  s.className = `status ${cls}`;
}

const TUNNEL_HINT = " Via tunnel: the dashboard dials out a WireGuard link to this machine on the UDP port shown (it must be free there; Pangolin uses 51820), so it works behind CGNAT and when the machine only accepts SSH from your PC. The script sets up the machine's end; open that UDP port in its provider's firewall.";

function addCard(m = { kind: "host", name: "", address: "", port: 22 }, saved = false) {
  const card = $("#card-tpl").content.firstElementChild.cloneNode(true);
  $(".f-kind", card).value = m.kind;
  $(".f-name", card).value = m.name;
  $(".f-address", card).value = m.address;
  $(".f-port", card).value = m.port;
  $(".f-tunnel", card).checked = !!m.tunnel;
  $(".f-udp", card).value = m.udp_port || 51821;
  if (saved) {
    card.dataset.saved = m.name;
    // A saved machine is identified by its name; change anything else by removing and re-adding it.
    for (const f of card.querySelectorAll("select, input")) f.disabled = true;
    $(".b-connect", card).textContent = "Reconnect";
    const running = state.running.includes(m.name);
    setStatus(card, running ? "connected" : "connected, not applied yet", running ? "ok" : "warn");
  }
  const hint = () => {
    const host = $(".f-kind", card).value === "host";
    const tunnel = host && $(".f-tunnel", card).checked;
    $(".tun", card).hidden = !host;
    $(".f-udp", card).hidden = !tunnel;
    $(".f-address", card).placeholder = tunnel ? "its public IP or host name" : "IP address or host name";
    $(".hint", card).textContent = HINTS[$(".f-kind", card).value] + (tunnel ? TUNNEL_HINT : "");
  };
  hint();
  let regen;
  card.addEventListener("input", () => {
    hint();
    saveDrafts();
    // Once typing pauses: a tunnel's script carries keys made for that name.
    clearTimeout(regen);
    if (!$(".script", card).hidden) regen = setTimeout(() => showScript(card), 700);
  });
  $(".b-script", card).onclick = () => ($(".script", card).hidden ? showScript(card) : ($(".script", card).hidden = true));
  $(".b-copy", card).onclick = (e) => copy($(".s-code", card).textContent, e.currentTarget);
  $(".b-copy-dl", card).onclick = (e) => copy($(".s-dl", card).textContent, e.currentTarget);
  $(".b-close", card).onclick = () => card.remove();
  $(".b-connect", card).onclick = () => connect(card);
  $(".b-trust", card).onclick = () => connect(card, card.dataset.fingerprint);
  $(".b-remove", card).onclick = () => remove(card);
  $("#cards").append(card);
  return card;
}

// Shows a script: a short download command when there is one, else the whole script in one line.
function setScript(card, label, r) {
  $(".s-label", card).textContent = label;
  $(".s-code", card).textContent = r ? r.script : "";
  $(".s-source", card).textContent = r ? r.source : "";
  // Runs only a download whose sha256 matches, so nobody on the way can swap the script. Inside sh -c
  // with the URL quoted, so it works the same in fish, csh and zsh (an IPv6 [address] isn't a glob).
  const dl = r && r.download
    ? ` sh -c 'curl -fsSLo "$1" "$2" && echo "$3  $1" | sha256sum -c - >/dev/null && sh "$1"' _ ~/homelab-setup.sh '${location.origin}${r.download.path}' ${r.download.sha256}`
    : "";
  const local = /^(localhost|127\.|\[::1\])/.test(location.host);
  $(".s-dl-note", card).textContent = local
    ? `This page is open as ${location.host}, which other machines can't reach: open it by this host's LAN address, or use the whole script below.`
    : location.protocol === "https:" ? "If the dashboard's HTTPS certificate is self-signed, curl refuses it there: use the whole script below." : "";
  $(".s-dl", card).textContent = dl;
  $(".dl", card).hidden = !dl;
  $(".full", card).open = !!r && !dl;
  $(".s-full-label", card).textContent = dl
    ? "The whole script in one line, if the machine can't reach this page"
    : "Paste this into a shell on the machine";
}

let scriptSeq = 0;
async function showScript(card) {
  const m = machineOf(card);
  $(".script", card).hidden = false;
  const label = `On ${m.name || m.address || "the machine"}, in any shell, as root or as a user with sudo, doas or the root password (it asks if needed). It checks first and stops before changing anything if something's in the way.`;
  if (!m.name || !m.address) {
    setScript(card, "Fill in a name and an address first.", null);
    return;
  }
  const seq = ++scriptSeq;
  try {
    const r = await api("/api/setup/script", { method: "POST", body: JSON.stringify({ ...m, allow_from: $("#allow-from").value }) });
    if (seq === scriptSeq) setScript(card, label, r);
  } catch (e) {
    if (seq === scriptSeq) setScript(card, `Can't make the script: ${e.message}`, null);
  }
}

async function connect(card, trustFingerprint = null) {
  const m = machineOf(card);
  const log = $(".connect-log", card);
  log.hidden = false;
  log.textContent = `Connecting to ${m.address}:${m.port} ...`;
  $(".key-changed", card).hidden = true;
  setStatus(card, "connecting", "muted");
  for (const b of card.querySelectorAll("button")) b.disabled = true;
  try {
    const r = await api("/api/setup/connect", { method: "POST", body: JSON.stringify({ ...m, trust_fingerprint: trustFingerprint }) });
    log.textContent = r.log.join("\n");
    if (r.ok) {
      setStatus(card, "connected, not applied yet", "warn");
      if (!card.dataset.saved) {
        card.dataset.saved = m.name;
        for (const f of card.querySelectorAll("select, input")) f.disabled = true;
        $(".b-connect", card).textContent = "Reconnect";
        $(".script", card).hidden = true;
        saveDrafts();
      }
      showApply(true);
    } else {
      const ask = r.needs_confirm || r.host_key_changed;
      setStatus(card, r.needs_confirm ? "confirm the host key" : "not connected", r.needs_confirm ? "warn" : "bad");
      card.dataset.fingerprint = r.fingerprint || "";
      $(".b-trust", card).textContent = r.host_key_changed ? "Trust the new host key" : "It matches, connect";
      $(".key-changed", card).hidden = !ask;
    }
  } catch (e) {
    log.textContent = e.message;
    setStatus(card, "not connected", "bad");
  } finally {
    for (const b of card.querySelectorAll("button")) b.disabled = false;
  }
}

async function remove(card) {
  const name = card.dataset.saved;
  if (name) {
    if (!confirm(`Remove ${name} from the dashboard? Nothing is changed on the machine itself; you'll get a script that undoes the setup there.`)) return;
    let undo;
    try {
      const path = `/api/setup/machines/${encodeURIComponent(name)}`;
      undo = await api(`${path}/uninstall`);
      await api(path, { method: "DELETE" });
      showApply(true);
    } catch (e) {
      notice(`Can't remove ${name}: ${e.message}`);
      return;
    }
    // Keep the card to show the undo script; it goes on Close.
    for (const b of card.querySelectorAll(".card-head button")) b.hidden = true;
    $(".hint", card).hidden = true;
    $(".connect-log", card).hidden = true;
    setStatus(card, "removed", "muted");
    $(".script", card).hidden = false;
    setScript(card, `Removed from the dashboard. To undo the setup on ${name} (its homelab user, sudo rule, API token or trust, tunnel), run this there:`, undo);
    $(".removed", card).hidden = false;
    delete card.dataset.saved;
    card.dataset.removed = "1";
    return;
  }
  card.remove();
  saveDrafts();
}

function showApply(pending) {
  $("#apply").hidden = !pending;
  if (pending && !state.can_restart) {
    $("#apply-text").textContent = "Changes are saved. Restart the container to start using them.";
    $("#apply-btn").hidden = true;
  }
}

async function waitAndGo(check, url) {
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      if (await check()) break;
    } catch {}
  }
  location.href = url;
}

$("#admin-form").onsubmit = async (e) => {
  e.preventDefault();
  const password = $("#admin-pass").value;
  if (password !== $("#admin-pass2").value) return notice("The two passwords differ.");
  try {
    const r = await api("/api/setup/admin", { method: "POST", body: JSON.stringify({ username: $("#admin-user").value, password }) });
    notice(r.restarting ? "Login created. Restarting; log in with it when your browser asks." : "Login created. Restart the container, then open /setup again.", "info");
    $("#admin").hidden = true;
    // The restarted app asks for the new login, so the page can only be reloaded, not polled.
    if (r.restarting) setTimeout(() => (location.href = "/setup"), 2500);
  } catch (err) {
    notice(err.message);
  }
};

$("#add-machine").onclick = () => {
  const card = addCard();
  $(".f-name", card).focus();
  saveDrafts();
};

$("#allow-from").addEventListener("input", (e) => {
  save(ALLOW, e.target.value);
  for (const card of document.querySelectorAll(".card")) if (!$(".script", card).hidden) showScript(card);
});

$("#apply-btn").onclick = async (e) => {
  e.currentTarget.disabled = true;
  try {
    await api("/api/setup/apply", { method: "POST" });
    notice("Restarting with the new machines ...", "info");
    await waitAndGo(async () => !(await api("/api/setup")).restart_pending, "/");
  } catch (err) {
    notice(err.message);
    e.currentTarget.disabled = false;
  }
};

$("#advanced").addEventListener("toggle", async (e) => {
  if (!e.target.open) return;
  try {
    const r = await api("/api/setup/config");
    $("#config-text").value = r.yaml;
    $("#config-text").readOnly = !r.writable;
    $("#config-save").disabled = !r.writable;
  } catch (err) {
    $("#config-status").textContent = err.message;
  }
});

$("#config-save").onclick = async () => {
  const status = $("#config-status");
  status.textContent = "Saving ...";
  status.className = "muted";
  try {
    await api("/api/setup/config", { method: "PUT", body: JSON.stringify({ yaml: $("#config-text").value }) });
    status.textContent = "Saved.";
    status.className = "ok";
    showApply(true);
    await render();
  } catch (err) {
    status.textContent = err.message;
    status.className = "bad";
  }
};

async function render() {
  state = await api("/api/setup");
  $("#to-dashboard").hidden = state.first_run;
  if (!state.writable) {
    notice(`${state.config_path} is read-only here (an older install's config folder), so machines can't be added from this page. Move to the single data volume (README) to use it.`);
  }
  $("#admin").hidden = !state.first_run || !state.writable;
  const ready = !state.first_run && state.writable;
  $("#machines").hidden = !ready;
  $("#advanced").hidden = state.first_run;
  showApply(state.restart_pending);
  if (!ready) return;
  $("#cards").innerHTML = "";
  for (const m of state.machines) addCard(m, true);
  const saved = new Set(state.machines.map((m) => m.name));
  const drafts = load(DRAFTS, []).filter((d) => !saved.has(d.name));
  for (const d of drafts) addCard(d);
  if (!state.machines.length && !drafts.length) addCard();
}

$("#allow-from").value = load(ALLOW, "");
render().catch((e) => {
  if (e.message.includes("setup link")) {
    notice("Open this page with the link printed in the container's logs (it looks like http://<host>:8080/setup#token=...).");
  } else {
    notice(e.message);
  }
});
