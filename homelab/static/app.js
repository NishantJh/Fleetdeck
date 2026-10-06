"use strict";

const state = { targets: [], errors: {}, jobs: [], schedule: null, selected: new Set(), expanded: new Set(), logJob: null, logLines: [] };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PLATFORM = { proxmox: "Proxmox", incus: "Incus", ssh: "Standalone", demo: "Demo" };
const KIND = { host: "host", lxc: "LXC", vm: "VM", container: "container" };

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
  for (const v of state.targets) {
    const k = groupKey(v.target);
    if (!map.has(k)) map.set(k, { key: k, platform: v.target.platform, title: v.target.platform === "ssh" ? "Standalone hosts" : v.target.parent || v.target.name, rows: [] });
    map.get(k).rows.push(v);
  }
  for (const g of map.values()) g.rows.sort((a, b) => (a.target.kind === "host" ? -1 : b.target.kind === "host" ? 1 : a.target.name.localeCompare(b.target.name)));
  return [...map.values()];
}

const canUpgrade = (t) => t.updatable && !t.check_only;
const canReboot = (t) => canUpgrade(t) && !t.no_reboot;
const BUSY = { upgrade: "updating", check: "checking", reboot: "rebooting" };

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
  return `<button class="small" data-toggle="${esc(t.id)}">${n} update${n > 1 ? "s" : ""} ${open ? "▴" : "▾"}</button>`;
}

function rowHtml(v) {
  const t = v.target, c = v.check;
  const busy = !!v.active_job;
  const reboot = c?.reboot_required ? `<span class="warn">reboot needed</span>` : "";
  const notes = c?.notes?.length
    ? ` <span class="chip ${c.notes.length > 1 ? "warn" : ""}" title="${esc(c.notes.join("\n"))}">${c.notes.length > 1 ? "repo warning" : "repos"}</span>`
    : "";
  const pending = c && !c.error && c.packages.length > 0;
  let html = `<tr class="${t.kind === "host" ? "host" : "guest"}">
    <td><input type="checkbox" data-select="${esc(t.id)}" ${state.selected.has(t.id) ? "checked" : ""} ${canUpgrade(t) ? "" : "disabled"}></td>
    <td class="name">${esc(t.name)} <span class="chip">${KIND[t.kind] || t.kind}</span>${t.updatable && t.note ? ` <span class="muted" title="${esc(t.note)}">ⓘ</span>` : ""}</td>
    <td class="muted">${esc(c?.os || "")}</td>
    <td>${esc(t.status)}</td>
    <td>${updatesCell(v)} ${reboot}${notes}</td>
    <td class="muted">${c ? ago(c.checked_at) : ""}</td>
    <td>
      <button class="small" data-act="check" data-id="${esc(t.id)}" ${t.updatable && !busy ? "" : "disabled"}>Check</button>
      <button class="small ${pending && !t.check_only ? "primary" : ""}" data-act="upgrade" data-id="${esc(t.id)}" ${t.updatable && !t.check_only && !busy ? "" : "disabled"} title="${t.check_only ? esc(t.note) : ""}">Update</button>
      <button class="small" data-act="reboot" data-id="${esc(t.id)}" ${canReboot(t) && !busy ? "" : "disabled"} title="${esc(t.no_reboot || (t.check_only ? t.note : ""))}">Reboot</button>
    </td></tr>`;
  if (pending && state.expanded.has(t.id)) {
    const rows = c.packages.map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.current || "")}</td><td>→ ${esc(p.available)}</td></tr>`).join("");
    html += `<tr class="pkgs"><td></td><td colspan="6"><table>${rows}</table></td></tr>`;
  }
  return html;
}

function render() {
  $("#errors").innerHTML = Object.entries(state.errors)
    .map(([p, e]) => `<div class="error-banner"><b>${esc(p)}</b>: ${esc(e)}</div>`).join("");

  const updatable = state.targets.filter((v) => v.target.updatable);
  const pending = updatable.filter((v) => v.check && !v.check.error && v.check.packages.length);
  const pkgCount = pending.reduce((n, v) => n + v.check.packages.length, 0);
  const reboot = updatable.filter((v) => v.check?.reboot_required).length;
  const unchecked = updatable.filter((v) => !v.check).length;
  $("#summary").innerHTML = `
    <div class="stat"><b>${updatable.length}</b><span class="muted">machines</span></div>
    <div class="stat"><b class="${pending.length ? "warn" : "ok"}">${pending.length}</b><span class="muted">need updates (${pkgCount} pkgs)</span></div>
    <div class="stat"><b class="${reboot ? "warn" : ""}">${reboot}</b><span class="muted">need a reboot</span></div>
    <div class="stat"><b>${unchecked}</b><span class="muted">not checked yet</span></div>
    ${scheduleStat()}`;
  $("#test-notify").hidden = !state.schedule?.notify_targets;

  $("#groups").innerHTML = groups().map((g) => `
    <div class="group">
      <div class="group-head"><span class="chip">${PLATFORM[g.platform] || g.platform}</span> ${esc(g.title)}</div>
      <table>
        <thead><tr><th></th><th>Name</th><th>OS</th><th>State</th><th>Updates</th><th>Checked</th><th></th></tr></thead>
        <tbody>${g.rows.map(rowHtml).join("")}</tbody>
      </table>
    </div>`).join("") || `<p class="muted">No targets yet. Add Proxmox, Incus or SSH hosts to config.yaml.</p>`;

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
    state.jobs = await api("/api/jobs");
    state.schedule = await api("/api/schedule");
    // Drop selections for targets that disappeared or can no longer be upgraded.
    const selectable = new Set(state.targets.filter((v) => canUpgrade(v.target)).map((v) => v.target.id));
    for (const id of state.selected) if (!selectable.has(id)) state.selected.delete(id);
  } catch (e) {
    state.errors = { dashboard: e.message };
  }
  render();
}

async function act(action, ids, rebootIfRequired = false) {
  if (!ids.length) return;
  let res;
  try {
    const body = { action, target_ids: ids, reboot_if_required: rebootIfRequired };
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
  const dlg = $("#confirm");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "ok") return;
    if (action === "upgrade") state.selected.clear();
    act(action, ids, action === "upgrade" && $("#confirm-reboot").checked);
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
  const pre = $("#log");
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = state.logLines.join("\n");
  if (stick) pre.scrollTop = pre.scrollHeight;
  render();
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act],[data-toggle],[data-job]");
  if (!el) return;
  if (el.dataset.toggle) {
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
$("#test-notify").onclick = async () => {
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    const failed = Object.entries(r.errors);
    alert(failed.length ? `Sent to ${r.sent}. Failed:\n${failed.map(([n, e]) => `${n}: ${e}`).join("\n")}` : `Sent to ${r.sent} target(s).`);
  } catch (e) {
    alert(`Could not send: ${e.message}`);
  }
};
$("#check-all").onclick = () => act("check", state.targets.filter((v) => v.target.updatable).map((v) => v.target.id));
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
