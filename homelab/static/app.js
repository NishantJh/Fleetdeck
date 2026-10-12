"use strict";

const state = { targets: [], errors: {}, settings: {}, jobs: [], schedule: null, selected: new Set(), expanded: new Set(), logJob: null, logLines: [], showHidden: false };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PLATFORM = { proxmox: "Proxmox", incus: "Incus", ssh: "Standalone", demo: "Demo" };
const KIND = { host: "host", lxc: "LXC", vm: "VM", container: "container" };
// Status dot: Proxmox says online/offline for nodes, running/stopped for guests; anything else is grey.
const DOT = { running: "on", online: "on", stopped: "off", offline: "off" };
// Short state words keep the State column on one line; the full word is the tooltip.
const SHORT = { running: "up", online: "up", stopped: "off", offline: "down" };

// Per-browser conveniences only; the hidden machines themselves are kept on the server.
const remember = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private window */ } },
};
state.showHidden = !!remember.get("showHidden");
// Machines whose "runs this dashboard, hide it?" hint was dismissed.
const dismissed = new Set(remember.get("dismissedSelfHint") || []);
// Hidden machines stay out of the counts, Check all and Update selected, even while shown.
const active = () => state.targets.filter((v) => !v.hidden);

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function until(iso) {
  const s = Math.round((new Date(iso) - Date.now()) / 1000);
  if (s < 60) return "in under a minute";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} d`;
}

function every(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400} d`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  return `${Math.round(seconds / 60)} min`;
}

function scheduleStat() {
  const sc = state.schedule;
  if (!sc?.check_every) return `<div class="stat" title="Set schedule.check_every in config.yaml"><b>off</b><span class="muted">auto-check</span></div>`;
  const next = sc.next_run ? until(sc.next_run) : "running now";
  const notify = sc.notify_targets ? `notifies ${sc.notify_targets} target${sc.notify_targets > 1 ? "s" : ""}` : "no notifications set up";
  return `<div class="stat" title="Every ${every(sc.check_every)}; ${notify}"><b>${next}</b><span class="muted">next auto-check</span></div>`;
}

function groupKey(t) {
  if (t.platform === "ssh") return "ssh";
  return `${t.platform}:${t.source}:${t.parent || t.name}`;
}

function groups() {
  const map = new Map();
  for (const v of state.showHidden ? state.targets : active()) {
    const k = groupKey(v.target);
    if (!map.has(k)) map.set(k, { key: k, platform: v.target.platform, title: v.target.platform === "ssh" ? "Standalone hosts" : v.target.parent || v.target.name, rows: [] });
    map.get(k).rows.push(v);
  }
  for (const g of map.values()) g.rows.sort((a, b) => (a.target.kind === "host" ? -1 : b.target.kind === "host" ? 1 : a.target.name.localeCompare(b.target.name)));
  return [...map.values()];
}

const canUpgrade = (t) => t.updatable && !t.check_only;
const selectable = (v) => canUpgrade(v.target) && !v.hidden;
const canReboot = (t) => canUpgrade(t) && !t.no_reboot;
const BUSY = { upgrade: "updating", check: "checking", reboot: "rebooting", rollback: "rolling back" };

function updatesCell(v) {
  const t = v.target;
  if (v.active_job) return `<span class="spin"></span> ${BUSY[v.active_job.action] || v.active_job.action}…`;
  if (!t.updatable) return `<span class="muted">${esc(t.note || "unavailable")}</span>`;
  const c = v.check;
  if (!c) return `<span class="muted">not checked</span>`;
  if (c.error) return `<span class="bad" title="${esc(c.error)}">error</span>`;
  const n = c.packages.length;
  if (n === 0) return `<span class="ok">up to date</span>`;
  const open = state.expanded.has(t.id);
  const sec = c.packages.filter((p) => p.security).length;
  const secChip = sec ? ` <span class="chip sec" title="From a security repository">${sec} security</span>` : "";
  return `<button class="small" data-toggle="${esc(t.id)}">${n} update${n > 1 ? "s" : ""} ${open ? "▴" : "▾"}</button>${secChip}`;
}

function backupCell(t) {
  const b = t.backup;
  if (!b) return "";
  let html;
  if (b.last) {
    const tip = `${new Date(b.last).toLocaleString()} on ${b.storage} · ${b.count} backup${b.count > 1 ? "s" : ""}`;
    html = `<span class="${b.stale ? "warn" : ""}" title="${esc(tip)}">${ago(b.last)}</span>`;
  } else if (b.not_set_up) {
    html = `<span class="muted" title="${esc(b.note)}">not set up ⓘ</span>`;
  } else if (b.note) {
    html = `<span class="muted" title="${esc(b.note)}">?</span>`;
  } else {
    html = `<span class="bad">none</span>`;
  }
  if (b.verified === false) html += ` <span class="chip bad" title="The newest backup failed verification">verify failed</span>`;
  if (b.in_job === false) html += ` <span class="chip warn" title="Not in any scheduled backup job">no job</span>`;
  if (b.last && b.note) html += ` <span class="muted" title="${esc(b.note)}">ⓘ</span>`;
  return html;
}

function rowHtml(v, withBackup) {
  const t = v.target, c = v.check;
  const busy = !!v.active_job;
  const reboot = c?.reboot_required ? `<span class="chip warn" title="Reboot needed">reboot</span>` : "";
  const notes = c?.notes?.length
    ? ` <span class="chip ${c.notes.length > 1 ? "warn" : ""}" title="${esc(c.notes.join("\n"))}">${c.notes.length > 1 ? "repo warning" : "repos"}</span>`
    : "";
  const pending = c && !c.error && c.packages.length > 0;
  const self = v.runs_dashboard ? ` <span class="chip" title="This dashboard runs here">dashboard</span>` : "";
  let html = `<tr class="${t.kind === "host" ? "host" : "guest"}${v.hidden ? " is-hidden" : ""}">
    <td><input type="checkbox" data-select="${esc(t.id)}" ${state.selected.has(t.id) ? "checked" : ""} ${selectable(v) ? "" : "disabled"}></td>
    <td class="name">${esc(t.name)} <span class="chip">${KIND[t.kind] || t.kind}</span>${self}${t.updatable && t.note ? ` <span class="muted" title="${esc(t.note)}">ⓘ</span>` : ""}</td>
    <td class="muted os" data-label="OS">${esc(c?.os || "")}</td>
    <td data-label="State"><span class="state" title="${esc(t.status)}"><span class="dot ${DOT[t.status] || ""}"></span>${esc(SHORT[t.status] || t.status)}</span></td>
    ${withBackup ? `<td data-label="Last backup">${backupCell(t)}</td>` : ""}
    <td data-label="Updates">${updatesCell(v)} ${reboot}${notes}</td>
    <td class="muted" data-label="Checked">${c ? ago(c.checked_at) : ""}</td>
    <td class="actions">
      <button class="small" data-act="check" data-id="${esc(t.id)}" ${t.updatable && !busy ? "" : "disabled"}>Check</button>
      <button class="small ${pending && !t.check_only ? "primary" : ""}" data-act="upgrade" data-id="${esc(t.id)}" ${t.updatable && !t.check_only && !busy ? "" : "disabled"} title="${t.check_only ? esc(t.note) : ""}">Update</button>
      <button class="small" data-act="reboot" data-id="${esc(t.id)}" ${canReboot(t) && !busy ? "" : "disabled"} title="${esc(t.no_reboot || (t.check_only ? t.note : ""))}">Reboot</button>
      <button class="small ghost" data-hide="${v.hidden ? "0" : "1"}" data-id="${esc(t.id)}" title="${v.hidden ? "Show this machine on the dashboard again" : "Hide from the dashboard, Check all and scheduled checks"}">${v.hidden ? "Unhide" : "Hide"}</button>
    </td></tr>`;
  if (pending && state.expanded.has(t.id)) {
    // Security updates first, so they stand out in a long list.
    const pkgs = [...c.packages].sort((a, b) => b.security - a.security);
    const rows = pkgs.map((p) => `<tr><td>${esc(p.name)}${p.security ? ` <span class="chip sec">security</span>` : ""}</td><td>${esc(p.current || "")}</td><td>→ ${esc(p.available)}</td></tr>`).join("");
    html += `<tr class="pkgs"><td></td><td colspan="${withBackup ? 7 : 6}"><table>${rows}</table></td></tr>`;
  }
  return html;
}

function render() {
  $("#errors").innerHTML = Object.entries(state.errors)
    .map(([p, e]) => `<div class="error-banner"><b>${esc(p)}</b>: ${esc(e)}</div>`).join("");

  const updatable = active().filter((v) => v.target.updatable);
  const pending = updatable.filter((v) => v.check && !v.check.error && v.check.packages.length);
  const pkgCount = pending.reduce((n, v) => n + v.check.packages.length, 0);
  const secMachines = pending.filter((v) => v.check.packages.some((p) => p.security)).length;
  const reboot = updatable.filter((v) => v.check?.reboot_required).length;
  const unchecked = updatable.filter((v) => !v.check).length;
  // Leave out guests whose backups couldn't be read (a note but no date): unknown isn't "fine".
  const withBackups = active().filter((v) => v.target.backup && (v.target.backup.last || !v.target.backup.note));
  const staleBackups = withBackups.filter((v) => v.target.backup.stale || v.target.backup.verified === false).length;
  $("#summary").innerHTML = `
    <div class="stat"><b>${updatable.length}</b><span class="muted">machines</span></div>
    <div class="stat"><b class="${pending.length ? "warn" : "ok"}">${pending.length}</b><span class="muted">need updates (${pkgCount} pkgs)</span></div>
    <div class="stat"><b class="${secMachines ? "bad" : ""}">${secMachines}</b><span class="muted">have security updates</span></div>
    <div class="stat"><b class="${reboot ? "warn" : ""}">${reboot}</b><span class="muted">need a reboot</span></div>
    <div class="stat"><b>${unchecked}</b><span class="muted">not checked yet</span></div>
    ${withBackups.length ? `<div class="stat"><b class="${staleBackups ? "warn" : "ok"}">${staleBackups}</b><span class="muted">machines with old or no backups</span></div>` : ""}
    ${scheduleStat()}`;
  $("#test-notify").hidden = !state.schedule?.notify_targets;

  const hiddenCount = state.targets.length - active().length;
  const toggle = $("#show-hidden");
  toggle.hidden = hiddenCount === 0;
  toggle.textContent = `Show hidden (${hiddenCount})`;
  toggle.setAttribute("aria-pressed", String(state.showHidden));
  const hint = active().find((v) => v.runs_dashboard && !dismissed.has(v.target.id));
  $("#self-hint").innerHTML = hint
    ? `<div class="hint-banner"><span><b>${esc(hint.target.name)}</b> looks like the machine this dashboard runs on. Hide it, so it isn't updated or rebooted from here by accident?</span>
       <span><button class="small primary" data-hide="1" data-id="${esc(hint.target.id)}">Hide it</button> <button class="small" data-dismiss="${esc(hint.target.id)}">Keep it</button></span></div>`
    : "";

  $("#groups").innerHTML = groups().map((g) => {
    const withBackup = g.rows.some((v) => v.target.backup);
    return `
    <div class="group">
      <div class="group-head"><span class="chip">${PLATFORM[g.platform] || g.platform}</span> ${esc(g.title)}</div>
      <div class="group-scroll"><table>
        <thead><tr><th></th><th>Name</th><th>OS</th><th>State</th>${withBackup ? "<th>Last backup</th>" : ""}<th>Updates</th><th>Checked</th><th></th></tr></thead>
        <tbody>${g.rows.map((v) => rowHtml(v, withBackup)).join("")}</tbody>
      </table></div>
    </div>`;
  }).join("") || `<p class="muted">No targets yet. Add Proxmox, Incus or SSH hosts to config.yaml.</p>`;

  const btn = $("#update-selected");
  btn.disabled = state.selected.size === 0;
  btn.textContent = state.selected.size ? `Update selected (${state.selected.size})` : "Update selected";

  $("#jobs").innerHTML = state.jobs.map((j) => {
    const cls = j.status === "failed" ? "bad" : j.status === "succeeded" ? "ok" : "";
    const icon = j.status === "running" || j.status === "queued" ? `<span class="spin"></span>` : "";
    return `<li data-job="${j.id}" class="${state.logJob === j.id ? "sel" : ""}">
      <span>${icon} ${esc(j.target_name)} · ${j.action}</span><span class="${cls}">${j.status}</span></li>`;
  }).join("") || `<li class="muted">Nothing yet</li>`;
}

async function load(refresh = false) {
  try {
    const data = await api(`/api/targets${refresh ? "?refresh=true" : ""}`);
    state.targets = data.targets;
    state.errors = data.errors;
    state.settings = data.settings || {};
    state.jobs = await api("/api/jobs");
    state.schedule = await api("/api/schedule");
    // Drop selections for targets that disappeared or can no longer be upgraded.
    const ok = new Set(state.targets.filter(selectable).map((v) => v.target.id));
    for (const id of state.selected) if (!ok.has(id)) state.selected.delete(id);
  } catch (e) {
    state.errors = { dashboard: e.message };
  }
  render();
}

async function act(action, ids, { rebootIfRequired = false, snapshotFirst = false } = {}) {
  if (!ids.length) return;
  let res;
  try {
    const body = { action, target_ids: ids, reboot_if_required: rebootIfRequired, snapshot_first: snapshotFirst };
    res = await api("/api/actions", { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    alert(`Could not start ${action}: ${e.message}`);
    return;
  }
  const rejected = Object.entries(res.rejected);
  // The server's reason already names the target; fall back to the id for unknown ones.
  if (rejected.length) alert(rejected.map(([id, why]) => (why === "unknown target" ? `${id}: ${why}` : why)).join("\n"));
  if (res.jobs.length === 1) {
    state.logJob = res.jobs[0].id;
    state.logLines = [];
  }
  schedule(200);
}

const CONFIRM = {
  upgrade: {
    title: "Update these machines?",
    text: "Runs a full OS upgrade (apt dist-upgrade, dnf upgrade, apk upgrade or pacman -Syu).",
    ok: "Update",
  },
  reboot: {
    title: "Reboot these machines?",
    text: "Reboots now and waits for each machine to come back, then re-checks it. Services on it will be down meanwhile.",
    ok: "Reboot",
  },
};

function confirmAction(action, ids) {
  const byId = new Map(state.targets.map((v) => [v.target.id, v]));
  const c = CONFIRM[action];
  $("#confirm-title").textContent = c.title;
  $("#confirm-text").textContent = c.text;
  $("#confirm-ok").textContent = c.ok;
  $("#confirm-reboot-row").hidden = action !== "upgrade";
  $("#confirm-reboot").checked = false;
  $("#confirm-list").innerHTML = ids.map((id) => {
    const v = byId.get(id);
    const n = v?.check?.packages?.length;
    let detail = n != null ? `${n} package(s)` : "not checked";
    if (action === "reboot") {
      detail = v?.check?.reboot_required ? "reboot needed" : "";
      const t = v?.target;
      const guests = t?.kind === "host"
        ? state.targets.filter((g) => g.target.parent === t.name && g.target.source === t.source && g.target.status === "running").length : 0;
      // Waits for running guest jobs first, but the guests themselves go down with it.
      if (guests) detail += `${detail ? " · " : ""}and every guest running on it (${guests})`;
    }
    if (action === "upgrade" && v?.target.no_reboot) detail += ` · won't reboot: ${v.target.no_reboot}`;
    return `<li>${esc(v?.target.name || id)} <span class="muted">${esc(detail)}</span></li>`;
  }).join("");
  const snapshottable = action === "upgrade" && ids.some((id) => byId.get(id)?.target.can_snapshot);
  $("#confirm-snapshot-row").hidden = !snapshottable;
  $("#confirm-snapshot").checked = snapshottable && !!state.settings.snapshot_before_upgrade;
  const dlg = $("#confirm");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "ok") return;
    if (action === "upgrade") state.selected.clear();
    const upgrade = action === "upgrade";
    act(action, ids, {
      rebootIfRequired: upgrade && $("#confirm-reboot").checked,
      snapshotFirst: upgrade && $("#confirm-snapshot").checked,
    });
  }, { once: true });
}

async function showLog(jobId) {
  if (state.logJob !== jobId) { state.logJob = jobId; state.logLines = []; }
  let job;
  try {
    job = await api(`/api/jobs/${jobId}?since=${state.logLines.length}`);
  } catch (e) {
    $("#log-title").textContent = `Could not load job output: ${e.message}`;
    return;
  }
  // A click and the poller can fetch at the same time: drop stale answers and splice by
  // offset so lines are never appended twice.
  if (state.logJob !== jobId) return;
  state.logLines.splice(job.log_offset, Infinity, ...job.log);
  $("#log-title").textContent = `${job.target_name} · ${job.action} · ${job.status}`;
  const rb = $("#rollback");
  rb.hidden = !(job.action === "upgrade" && job.snapshot && (job.status === "succeeded" || job.status === "failed"));
  // Not while the machine has a job queued or running (e.g. the rollback just started).
  rb.disabled = state.targets.some((v) => v.target.id === job.target_id && v.active_job);
  rb.dataset.rollbackJob = job.id;
  rb.dataset.snapshot = job.snapshot || "";
  rb.dataset.name = job.target_name;
  const pre = $("#log");
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = state.logLines.join("\n");
  if (stick) pre.scrollTop = pre.scrollHeight;
  render();
}

async function setHidden(id, hidden) {
  try {
    await api("/api/hidden", { method: "POST", body: JSON.stringify({ target_ids: [id], hidden }) });
  } catch (e) {
    alert(`Could not ${hidden ? "hide" : "unhide"} it: ${e.message}`);
    return;
  }
  for (const v of state.targets) if (v.target.id === id) v.hidden = hidden;
  if (hidden) state.selected.delete(id);
  render();
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act],[data-toggle],[data-job],[data-hide],[data-dismiss]");
  if (!el) return;
  if (el.dataset.hide) {
    setHidden(el.dataset.id, el.dataset.hide === "1");
  } else if (el.dataset.dismiss) {
    dismissed.add(el.dataset.dismiss);
    remember.set("dismissedSelfHint", [...dismissed]);
    render();
  } else if (el.dataset.toggle) {
    const id = el.dataset.toggle;
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    render();
  } else if (el.dataset.job) {
    showLog(el.dataset.job);
  } else if (el.dataset.act === "check") {
    act("check", [el.dataset.id]);
  } else if (el.dataset.act === "upgrade" || el.dataset.act === "reboot") {
    confirmAction(el.dataset.act, [el.dataset.id]);
  }
});

document.addEventListener("change", (e) => {
  const id = e.target.dataset?.select;
  if (!id) return;
  e.target.checked ? state.selected.add(id) : state.selected.delete(id);
  render();
});

$("#refresh").onclick = () => load(true);
$("#show-hidden").onclick = () => {
  state.showHidden = !state.showHidden;
  remember.set("showHidden", state.showHidden);
  render();
};
$("#test-notify").onclick = async () => {
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    const failed = Object.entries(r.errors);
    alert(failed.length ? `Sent to ${r.sent}. Failed:\n${failed.map(([n, e]) => `${n}: ${e}`).join("\n")}` : `Sent to ${r.sent} target(s).`);
  } catch (e) {
    alert(`Could not send: ${e.message}`);
  }
};
$("#rollback").onclick = async (e) => {
  const { rollbackJob: job, snapshot, name } = e.currentTarget.dataset;
  if (!confirm(`Roll ${name} back to snapshot ${snapshot}? Everything changed on it since the snapshot is lost.`)) return;
  try {
    const j = await api(`/api/jobs/${job}/rollback`, { method: "POST" });
    state.logJob = j.id;
    state.logLines = [];
    schedule(200);
  } catch (err) {
    alert(`Could not roll back: ${err.message}`);
  }
};
// Backup: an encrypted file of the config, keys and history; restoring one restarts the app.
const backup = { data: null };
$("#backup").onclick = () => {
  for (const id of ["backup-pass", "backup-pass2", "restore-file", "restore-pass"]) $(`#${id}`).value = "";
  for (const id of ["backup-msg", "restore-msg"]) $(`#${id}`).textContent = "";
  resetRestore();
  $("#backup-dialog").showModal();
};

function resetRestore() {
  backup.data = null;
  $("#restore-preview").hidden = true;
  $("#restore-ack").checked = false;
  $("#restore-go").hidden = true;
  $("#restore-go").disabled = true;
  $("#restore-check").hidden = false;
}

function say(id, text, bad = false) {
  const el = $(`#${id}`);
  el.textContent = text;
  el.className = bad ? "bad-text" : "muted";
}

$("#backup-download").onclick = async () => {
  const pass = $("#backup-pass").value;
  if (pass.length < 12) return say("backup-msg", "Use a passphrase of at least 12 characters.", true);
  if (pass !== $("#backup-pass2").value) return say("backup-msg", "The two passphrases don't match.", true);
  say("backup-msg", "Encrypting…");
  try {
    const r = await fetch("/api/backup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passphrase: pass }) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "homelab-backup.hlmbackup";
    const url = URL.createObjectURL(await r.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    say("backup-msg", `Downloaded ${name}. Keep it somewhere safe, away from this server, and remember the passphrase.`);
  } catch (e) {
    say("backup-msg", `Could not make the backup: ${e.message}`, true);
  }
};

const readBase64 = (file) => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(String(fr.result).split(",", 2)[1] || "");
  fr.onerror = () => reject(fr.error);
  fr.readAsDataURL(file);
});

$("#restore-file").onchange = resetRestore;
$("#restore-pass").oninput = resetRestore;
$("#restore-ack").onchange = (e) => { $("#restore-go").disabled = !e.target.checked; };

$("#restore-check").onclick = async () => {
  const file = $("#restore-file").files[0];
  if (!file) return say("restore-msg", "Choose a backup file first.", true);
  if (!$("#restore-pass").value) return say("restore-msg", "Enter the backup's passphrase.", true);
  say("restore-msg", "Opening the backup…");
  try {
    backup.data = await readBase64(file);
    const r = await api("/api/backup/restore", { method: "POST", body: JSON.stringify({ passphrase: $("#restore-pass").value, data: backup.data }) });
    const b = r.backup;
    const made = b.created_at ? new Date(b.created_at).toLocaleString() : "an unknown date";
    $("#restore-summary").textContent = `Made ${made}. Login: ${b.username || "none"}. ${b.machines} machine${b.machines === 1 ? "" : "s"} in the config` +
      `${b.history ? ", with job history" : ""}. ${b.files.length} file${b.files.length === 1 ? "" : "s"}.`;
    $("#restore-preview").hidden = false;
    $("#restore-check").hidden = true;
    $("#restore-go").hidden = false;
    say("restore-msg", "");
  } catch (e) {
    backup.data = null;
    say("restore-msg", e.message.replace(/^400 /, "").replace(/^\{"detail":"(.*)"\}$/, "$1"), true);
  }
};

$("#restore-go").onclick = async () => {
  if (!backup.data || !$("#restore-ack").checked) return;
  $("#restore-go").disabled = true;
  say("restore-msg", "Restoring…");
  try {
    const r = await api("/api/backup/restore", { method: "POST", body: JSON.stringify({ passphrase: $("#restore-pass").value, data: backup.data, confirm: true }) });
    if (!r.restarting) return say("restore-msg", "Restored. Restart the container to use it.");
    say("restore-msg", "Restored. Restarting with it; this page reloads when it's back (log in with the backup's login).");
    await new Promise((ok) => setTimeout(ok, 1500));
    for (let i = 0; i < 60; i++) {
      // Any answer means it is back; 401 if the restored login differs from the one in this browser.
      try { await fetch("/api/schedule"); break; } catch { await new Promise((ok) => setTimeout(ok, 1000)); }
    }
    location.reload();
  } catch (e) {
    $("#restore-go").disabled = false;
    say("restore-msg", `Could not restore: ${e.message}`, true);
  }
};

$("#check-all").onclick = () => act("check", active().filter((v) => v.target.updatable).map((v) => v.target.id));
$("#update-selected").onclick = () => confirmAction("upgrade", [...state.selected]);

// Poll fast while anything is running, slowly otherwise.
let timer;
function schedule(ms) { clearTimeout(timer); timer = setTimeout(tick, ms); }
async function tick() {
  try {
    await load();
    if (state.logJob && state.jobs.some((j) => j.id === state.logJob)) await showLog(state.logJob);
  } catch (e) {
    console.error(e);
  } finally {
    // Always keep polling, even if one request failed (e.g. a job evicted from history).
    const busy = state.jobs.some((j) => j.status === "running" || j.status === "queued");
    schedule(busy ? 1500 : 15000);
  }
}
tick();
