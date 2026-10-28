"use strict";

const state = { targets: [], errors: {}, settings: {}, backupPlaces: {}, jobs: [], schedule: null, selected: new Set(), expanded: new Set(), logJob: null, logLines: [], showHidden: false, view: "machines" };
const $ = (sel) => document.querySelector(sel);
const renderedHTML = new WeakMap();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PLATFORM = { proxmox: "Proxmox", incus: "Incus", ssh: "Standalone", demo: "Demo" };
const KIND = { host: "Host", lxc: "LXC", vm: "VM", container: "Container" };
const ICON = {
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
  reboot: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
  refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3v8Z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  package: '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="M3.3 7 12 12l8.7-5"/><path d="M12 22V12"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  backup: '<path d="M3 6h18"/><path d="M19 6v14H5V6"/><path d="M8 3h8l1 3H7Z"/><path d="M9 11h6"/>',
  terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
  eye: '<path d="M2.1 12a10.8 10.8 0 0 1 19.8 0 10.8 10.8 0 0 1-19.8 0Z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="m3 3 18 18"/><path d="M10.6 10.6A2 2 0 0 0 13.4 13.4"/><path d="M9.9 4.2A10.7 10.7 0 0 1 21.9 12a10.5 10.5 0 0 1-2.1 3.1"/><path d="M6.2 6.2A10.8 10.8 0 0 0 2.1 12a10.8 10.8 0 0 0 12 7.8"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
  alert: '<path d="M10.3 2.9 1.8 17a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 2.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
};
const icon = (name) => `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;
const plural = (count, one, many) => count === 1 ? one : many;
const kindLabel = (t) => t.kind === "host" && t.platform === "proxmox" ? "Node" : KIND[t.kind] || t.kind;
// Status dot: Proxmox says online/offline for nodes, running/stopped for guests; anything else is grey.
const DOT = { running: "on", online: "on", stopped: "off", offline: "off" };

// Per-browser conveniences only; the hidden machines themselves are kept on the server.
const remember = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private window */ } },
};
state.showHidden = !!remember.get("showHidden");
state.view = remember.get("dashboardView") === "containers" ? "containers" : "machines";
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
  if (!sc?.check_every) return "Automatic checks off";
  const next = sc.next_run ? until(sc.next_run) : "running now";
  const notify = sc.notify_targets ? `notifies ${sc.notify_targets} target${sc.notify_targets > 1 ? "s" : ""}` : "no notifications set up";
  return `<span class="schedule-next" title="Every ${every(sc.check_every)}; ${notify}">${icon("clock")}Next check ${next}</span>`;
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
  const compare = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  for (const g of map.values()) g.rows.sort((a, b) => Number(b.target.kind === "host") - Number(a.target.kind === "host") || compare(a.target.name, b.target.name) || compare(a.target.id, b.target.id));
  const order = { proxmox: 0, incus: 1, ssh: 2, demo: 3 };
  return [...map.values()].sort((a, b) => (order[a.platform] ?? 4) - (order[b.platform] ?? 4) || compare(a.title, b.title) || compare(a.key, b.key));
}

const canUpgrade = (t) => t.updatable && !t.check_only;
const phasedPackages = (check) => check?.packages?.filter((p) => p.phased) || [];
const actionablePackages = (check) => check?.packages?.filter((p) => !p.phased) || [];
const securityPackages = (check) => actionablePackages(check).filter((p) => p.security);
const regularPackages = (check) => actionablePackages(check).filter((p) => !p.security);
const selectable = (v) => canUpgrade(v.target) && actionablePackages(v.check).length > 0 && !v.hidden && !v.blocked;
const canReboot = (t) => canUpgrade(t) && !t.no_reboot;
const BUSY = { upgrade: "updating", check: "checking", reboot: "rebooting", rollback: "rolling back", backup: "backing up", stacks: "checking Docker", "stack-update": "updating Docker" };

function metricButton(v, kind, count) {
  if (!count) return "";
  const id = esc(v.target.id), open = state.expanded.has(v.target.id);
  const label = { security: "security", package: "package", phased: "phased" }[kind];
  const iconName = { security: "shield", package: "package", phased: "clock" }[kind];
  return `<button class="update-metric ${kind}" data-toggle="${id}" data-focus-key="updates:${id}:${kind}" aria-expanded="${open}" aria-controls="packages-${id}" aria-label="${count} ${label} update${count === 1 ? "" : "s"}; ${open ? "hide" : "show"} details">${icon(iconName)}<span>${count}</span></button>`;
}

function updateColumns(v) {
  const t = v.target;
  if (v.active_job) return `<td colspan="3" class="update-summary" data-label="Updates"><span class="spin"></span> ${BUSY[v.active_job.action] || v.active_job.action}…</td>`;
  if (!t.updatable) return `<td colspan="3" class="update-summary muted" data-label="Updates">${esc(t.note || "Unavailable")}</td>`;
  const c = v.check;
  if (!c) return `<td colspan="3" class="update-summary muted" data-label="Updates">Not checked</td>`;
  if (c.error) return `<td colspan="3" class="update-summary" data-label="Updates"><span class="status-inline bad" title="${esc(c.error)}">${icon("alert")}Check failed</span></td>`;
  const security = securityPackages(c).length;
  const regular = regularPackages(c).length;
  const phased = phasedPackages(c).length;
  if (!security && !regular && !phased) return `<td colspan="3" class="update-summary" data-label="Updates"><span class="status-inline ok">${icon("check")}Up to date</span></td>`;
  if (!security && !regular && phased) {
    return `<td colspan="2" class="update-summary" data-label="Updates"><span class="status-inline ok">${icon("check")}Up to date</span></td><td class="update-metric-cell" data-label="Phased">${metricButton(v, "phased", phased)}</td>`;
  }
  return `<td class="update-metric-cell" data-label="Security">${metricButton(v, "security", security)}</td><td class="update-metric-cell" data-label="Package">${metricButton(v, "package", regular)}</td><td class="update-metric-cell" data-label="Phased">${metricButton(v, "phased", phased)}</td>`;
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

function csTip(app) {
  return `Installed by the community-scripts helper script for ${app}. ` + (state.settings.community_scripts_update
    ? `Updating it also runs ${app}'s own update afterwards (like typing "update" in it), which keeps the app working.`
    : `Its own update is turned off (community_scripts_update), so after an OS update run "update" inside it if ${app} misbehaves.`);
}

const dockerSettings = (v) => v.docker_settings || { mode: "managed", manager_name: "Dockhand", manager_url: null };
const dockerEnabled = (v) => !!state.settings.docker_enabled && ["managed", "monitor"].includes(dockerSettings(v).mode);
const hasDocker = (v) => !!state.settings.docker_enabled && (v.check?.docker_installed === true || (v.stacks && v.stacks.docker !== "none") || dockerSettings(v).mode !== "managed");
const dockerHosts = () => active().filter(hasDocker).sort((a, b) => a.target.name.localeCompare(b.target.name, undefined, { numeric: true }) || a.target.id.localeCompare(b.target.id));
const dockerBehind = (v) => dockerEnabled(v) ? (v.stacks?.stacks || []).filter((s) => !s.error && s.update_available && s.state !== "stopped").length : 0;
const dockerErrors = (v) => !dockerEnabled(v) ? 0 : v.stacks?.error && v.stacks.docker !== "none" ? 1 : (v.stacks?.stacks || []).filter((s) => s.error).length;
function managerLink(v, label) {
  const url = dockerSettings(v).manager_url;
  // Treat stored values as untrusted too; never turn a javascript: URL into a link.
  try { if (url && ["http:", "https:"].includes(new URL(url).protocol)) return `<a class="docker-status-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`; } catch { /* invalid old setting */ }
  return `<button class="docker-status-link" data-docker-settings="${esc(v.target.id)}">${esc(label)}</button>`;
}

function dockerCell(v) {
  if (!hasDocker(v)) return "";
  const policy = dockerSettings(v);
  if (policy.mode === "disabled") return `<span class="muted">disabled</span>`;
  if (policy.mode === "external") return managerLink(v, `Managed by ${policy.manager_name}`);
  const r = v.stacks;
  let label = "Not checked", cls = "muted";
  if (v.active_job?.action === "stacks") label = "Fetching image updates…";
  else if (r?.error) { label = r.docker === "none" ? "Not installed" : "Check failed"; cls = r.docker === "none" ? "muted" : "bad"; }
  else if (r) { const n = dockerBehind(v); label = n ? `${n} stack update${n === 1 ? "" : "s"}` : dockerErrors(v) ? "Status unknown" : "Up to date"; cls = n ? "warn" : dockerErrors(v) ? "bad" : "ok"; }
  return `<button class="docker-status-link ${cls}" data-open-docker="${esc(v.target.id)}" data-focus-key="docker:${esc(v.target.id)}">${label} →</button>${dockerErrors(v) ? `<div class="cell-detail bad">${dockerErrors(v)} check failure(s)</div>` : ""}${r ? `<div class="cell-detail muted">${ago(r.checked_at)}</div>` : ""}`;
}

function stackPanel(v) {
  const id = esc(v.target.id), r = v.stacks, busy = !!v.active_job;
  const policy = dockerSettings(v), managed = policy.mode === "managed";
  const prune = !!remember.get("pruneImages");
  let body;
  if (policy.mode === "disabled") body = `<p class="muted">Docker checks and deployments are disabled. OS updates remain independent.</p>`;
  else if (policy.mode === "external") body = `<p class="muted">Docker checks and deployments are handled by ${esc(policy.manager_name)}. Fleetdeck continues to manage OS updates.</p><p>${managerLink(v, `Open ${policy.manager_name}`)}</p>`;
  else if (!r) body = `<p class="muted">Image updates have not been checked. Fetch image updates to download newer images and compare them with the running containers. Nothing is restarted.</p>`;
  else if (r.error) body = `<p class="${r.docker === "none" ? "muted" : "bad"}">${esc(r.error)}</p>`;
  else if (!r.stacks.length) body = `<p class="muted">No Docker Compose stacks found on this machine.</p>`;
  else {
    const rows = r.stacks.map((s) => {
      const behind = s.services.filter((x) => x.outdated && s.state !== "stopped").map((x) => x.name);
      let status;
      if (s.error) status = `<span class="bad" title="${esc(s.error)}">unknown · check failed</span><div class="cell-detail">${esc(s.error)}</div>`;
      else if (s.state === "stopped") status = `<span class="muted">stopped</span>`;
      else if (behind.length) status = `<span class="warn" title="${esc(behind.join(", "))}">update available</span>`;
      else status = `<span class="ok">up to date</span>`;
      const up = s.state === "partial" ? ` <span class="chip warn" title="Services that are stopped stay stopped">partly running</span>` : "";
      const self = s.is_dashboard ? ` <span class="chip" title="This dashboard runs in this stack. Updating it would stop the update itself: run docker compose pull && docker compose up -d in a terminal.">dashboard</span>` : "";
      const can = managed && canUpgrade(v.target) && !v.blocked && !busy && !s.error && s.state !== "stopped" && !s.is_dashboard && r.compose !== "none";
      const btn = managed && behind.length && !s.is_dashboard ? `<button class="small primary" data-stack="${esc(s.name)}" data-id="${id}" ${can ? "" : "disabled"} title="${esc(v.blocked || s.error || "Download images and recreate running services")}">Update stack</button>` : s.is_dashboard ? `<span class="muted">update externally</span>` : "";
      return `<tr><td class="name">${esc(s.name)}${self}${up}</td><td class="muted" data-label="Services">${s.services.map((x) => esc(x.name)).join(", ")}</td><td data-label="Image status">${status}</td><td class="actions">${btn}</td></tr>`;
    }).join("");
    body = `<div class="group-scroll"><table><thead><tr><th>Stack</th><th>Services</th><th>Image status</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  return `<div class="group docker-host" id="docker-${id}"><div class="group-head"><span class="chip">Docker</span>${esc(v.target.name)}<span class="host-mode muted">${esc({ managed: "Managed by Fleetdeck", monitor: "Monitor only", external: `Managed by ${policy.manager_name}`, disabled: "Disabled" }[policy.mode])}</span><button class="small" data-docker-settings="${id}">Settings</button></div>
    ${dockerEnabled(v) ? `<div class="docker-head"><span class="muted">${r ? `Checked ${ago(r.checked_at)}` : "Not checked"}</span>
      <button class="small" data-act="stacks" data-id="${id}" ${!busy && canUpgrade(v.target) ? "" : "disabled"}>Fetch image updates</button>
      ${managed ? `<label class="muted" title="Runs docker image prune: removes all unused, untagged images on this machine, not only this stack's"><input type="checkbox" data-prune ${prune ? "checked" : ""}> remove unused, untagged images after updating</label>` : `<span class="chip">Stack deployments disabled</span>`}</div><p class="docker-check-note muted">Fetching downloads images. Updating a stack recreates its running services; OS packages are separate.</p>` : ""}
    ${body}</div>`;
}

function rowHtml(v, withBackup, relation = {}) {
  const t = v.target, c = v.check;
  const busy = !!v.active_job;
  const pending = c && !c.error && actionablePackages(c).length > 0;
  const self = v.runs_dashboard ? ` <span class="chip" title="This dashboard runs here">dashboard</span>` : "";
  const cs = c?.community_script
    ? ` <span class="chip" title="${esc(csTip(c.community_script))}">helper script</span>` : "";
  const note = t.updatable && t.note
    ? ` <span class="machine-note warn" title="${esc(t.note)}" aria-label="${esc(t.note)}">${icon("alert")}</span>` : "";
  const repoNote = c?.notes?.length
    ? `<span class="repo-note ${c.notes.length > 1 ? "warn" : "muted"}" title="${esc(c.notes.join("\n"))}" aria-label="Repository information">${icon("alert")}</span>` : "";
  const rebootTitle = t.no_reboot || v.blocked || "Reboot needed";
  const reboot = c?.reboot_required === true
    ? canReboot(t)
      ? `<button class="small reboot-action" data-act="reboot" data-id="${esc(t.id)}" data-focus-key="reboot:${esc(t.id)}" ${!busy && !v.blocked ? "" : "disabled"} title="${esc(v.blocked || "Reboot required — reboot this machine")}">${icon("reboot")}<span>Reboot</span></button>`
      : `<span class="reboot-status required" title="${esc(rebootTitle)}">${icon("reboot")}Required</span>`
    : c && c.reboot_required === false ? "" : `<span class="muted">Unknown</span>`;
  const updateAction = pending && canUpgrade(t)
    ? `<button class="small primary" data-act="upgrade" data-id="${esc(t.id)}" data-focus-key="upgrade:${esc(t.id)}" ${!busy && !v.blocked ? "" : "disabled"} title="${esc(v.blocked || "Install available OS packages")}">${icon("download")}<span>Update</span></button>` : "";
  const checkAction = t.updatable && (c?.error || !c)
    ? `<button class="small" data-act="check" data-id="${esc(t.id)}" data-focus-key="check:${esc(t.id)}" ${busy ? "disabled" : ""}>${icon("refresh")}<span>${c?.error ? "Retry check" : "Check"}</span></button>` : "";
  const rowClasses = [t.kind === "host" ? "host" : "guest", "machine-row"];
  if (relation.hasGuests) rowClasses.push("has-guests");
  if (relation.firstGuest) rowClasses.push("first-guest");
  if (relation.lastGuest) rowClasses.push("last-guest");
  if (v.hidden) rowClasses.push("is-hidden");
  if (state.selected.has(t.id)) rowClasses.push("is-selected");
  let html = `<tr class="${rowClasses.join(" ")}" data-machine-row="${esc(t.id)}">
    <td><input type="checkbox" aria-label="Select ${esc(t.name)}" data-select="${esc(t.id)}" data-focus-key="select:${esc(t.id)}" ${state.selected.has(t.id) ? "checked" : ""} ${selectable(v) ? "" : "disabled"}></td>
    <td class="name machine-cell" data-label="Machine"><span class="machine-main"><span class="state" title="${esc(t.status)}"><span class="dot ${DOT[t.status] || ""}"></span></span><span class="machine-name">${esc(t.name)}</span></span>${self}${cs}${note}</td>
    <td data-label="Type"><span class="chip type-chip">${esc(kindLabel(t))}</span></td>
    <td class="os-cell" data-label="OS">${esc(c?.os || t.status)}</td>
    ${withBackup ? `<td data-label="Last backup">${backupCell(t)}</td>` : ""}
    ${updateColumns(v)}
    <td data-label="Reboot">${reboot}</td>
    <td class="last-check" data-label="Last check"><span>${c ? ago(c.checked_at) : "Never"}</span>${repoNote}</td>
    ${state.settings.docker_enabled ? `<td data-label="Containers">${dockerCell(v)}</td>` : ""}
    <td class="actions" data-label="Actions">${updateAction}${checkAction}</td>
    <td class="more-cell"><button class="small ghost more-actions" data-machine-menu="${esc(t.id)}" data-focus-key="menu:${esc(t.id)}" aria-label="More actions for ${esc(t.name)}" aria-controls="machine-actions" aria-expanded="false">⋯</button></td>
  </tr>`;
  if (c?.packages?.length && state.expanded.has(t.id)) {
    // Security updates first, so they stand out in a long list.
    const rank = (p) => p.phased ? 2 : p.security ? 0 : 1;
    const pkgs = [...c.packages].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
    const rows = pkgs.map((p) => {
      const kind = p.phased ? "phased" : p.security ? "security" : "package";
      const label = p.phased ? "Phased" : p.security ? "Security" : "Package";
      const iconName = p.phased ? "clock" : p.security ? "shield" : "package";
      return `<tr><td><span class="package-type ${kind}">${icon(iconName)}${label}</span></td><td class="package-name">${esc(p.name)}</td><td class="package-version">${esc(p.current || "—")}</td><td class="package-version">${esc(p.available)}</td></tr>`;
    }).join("");
    const detailClasses = ["pkgs"];
    if (state.selected.has(t.id)) detailClasses.push("is-selected");
    if (relation.hasGuests || (t.kind !== "host" && !relation.lastGuest)) detailClasses.push("tree-continues");
    html += `<tr class="${detailClasses.join(" ")}" id="packages-${esc(t.id)}"><td></td><td colspan="${10 + Number(!!withBackup) + Number(!!state.settings.docker_enabled)}"><div class="packages-panel"><table class="packages-table"><thead><tr><th>Type</th><th>Package</th><th>Installed</th><th>Available</th></tr></thead><tbody>${rows}</tbody></table></div></td></tr>`;
  }
  return html;
}

function render() {
  if (!state.settings.docker_enabled) state.view = "machines";
  const focused = document.activeElement?.dataset?.focusKey;
  // Avoid replacing live controls on every poll when their rendered data did not change.
  const html = (selector, content) => {
    const el = $(selector);
    // Compare our templates, not serialized DOM (browsers normalize boolean attributes, etc.).
    if (renderedHTML.get(el) !== content) { el.innerHTML = content; renderedHTML.set(el, content); }
  };
  $("#errors").innerHTML = Object.entries(state.errors)
    .map(([p, e]) => `<div class="error-banner"><b>${esc(p)}</b>: ${esc(e)}</div>`).join("");

  const updatable = active().filter((v) => v.target.updatable);
  const pending = updatable.filter((v) => v.check && !v.check.error && actionablePackages(v.check).length);
  const secMachines = pending.filter((v) => securityPackages(v.check).length).length;
  const reboot = updatable.filter((v) => v.check?.reboot_required).length;
  const hosts = dockerHosts();
  const images = hosts.reduce((n, v) => n + dockerBehind(v), 0);
  const imageErrors = hosts.reduce((n, v) => n + dockerErrors(v), 0);
  const imageUnchecked = hosts.filter((v) => dockerEnabled(v) && !v.stacks).length;
  html("#summary", `
    <div class="stat"><b class="${pending.length ? "warn" : "ok"}">${pending.length}</b><span>${plural(pending.length, "Machine needing updates", "Machines needing updates")}</span></div>
    <div class="stat"><b class="${secMachines ? "bad" : "ok"}">${secMachines}</b><span>${plural(secMachines, "Machine with security updates", "Machines with security updates")}</span></div>
    <div class="stat"><b class="${reboot ? "reboot" : "ok"}">${reboot}</b><span>${plural(reboot, "Machine requiring reboot", "Machines requiring reboot")}</span></div>
    ${state.settings.docker_enabled ? `<div class="stat"><b class="${images ? "warn" : imageErrors ? "bad" : "ok"}">${images}</b><span>${plural(images, "Stack with an image update", "Stacks with image updates")}${imageUnchecked ? ` · ${imageUnchecked} unchecked` : ""}</span></div>` : ""}`);
  $("#overview-count").textContent = `${active().length} ${plural(active().length, "machine", "machines")}`;
  $("#schedule-status").innerHTML = scheduleStat();
  $("#test-notify").hidden = !state.schedule?.notify_targets;

  const hiddenCount = state.targets.length - active().length;
  const toggle = $("#show-hidden");
  toggle.hidden = hiddenCount === 0;
  toggle.textContent = `Show hidden (${hiddenCount})`;
  toggle.setAttribute("aria-pressed", String(state.showHidden));
  html("#groups", groups().map((g) => {
    const withBackup = g.rows.some((v) => v.target.backup);
    const guestIndexes = g.rows.map((v, index) => v.target.kind === "host" ? -1 : index).filter((index) => index >= 0);
    const firstGuest = guestIndexes[0], lastGuest = guestIndexes.at(-1);
    return `
    <div class="group">
      <div class="group-head"><span class="chip">${PLATFORM[g.platform] || g.platform}</span> ${esc(g.title)}</div>
      <div class="group-scroll"><table class="machine-table" aria-label="${esc(g.title)} machines">
        <thead><tr><th></th><th>Machine</th><th>Type</th><th>OS</th>${withBackup ? "<th>Last backup</th>" : ""}<th class="update-heading">Security</th><th class="update-heading">Package</th><th class="update-heading">Phased</th><th>Reboot</th><th>Last check</th>${state.settings.docker_enabled ? "<th>Containers</th>" : ""}<th>Actions</th><th>More</th></tr></thead>
        <tbody>${g.rows.map((v, index) => rowHtml(v, withBackup, {
          hasGuests: v.target.kind === "host" && guestIndexes.length > 0,
          firstGuest: index === firstGuest,
          lastGuest: index === lastGuest,
        })).join("")}</tbody>
      </table></div>
    </div>`;
  }).join("") || `<div class="empty"><h3>No machines yet</h3><p class="muted">Add a Proxmox node, an Incus server or any Linux host, and it shows up here with its updates, backups and state.</p><a class="button primary" href="/setup">Add machines</a></div>`);
  const unknown = active().filter((v) => v.target.updatable && v.check?.docker_installed == null && !v.stacks).length;
  html("#containers", state.settings.docker_enabled ? `${hosts.some((v) => dockerEnabled(v) && canUpgrade(v.target)) ? `<div class="containers-toolbar"><span class="muted">OS packages and container deployments are managed separately.</span><button class="small" data-fetch-docker>Fetch image updates</button></div>` : ""}${hosts.map(stackPanel).join("") || `<div class="empty"><h3>No Docker hosts discovered</h3><p class="muted">Update checks also detect Docker without downloading images.</p></div>`}${unknown ? `<p class="muted discovery-note">Docker capability is not known on ${unknown} machine(s). <button class="small" data-discover-docker>Check updates to discover hosts</button></p>` : ""}` : "");
  $(".view-tabs").hidden = !state.settings.docker_enabled;
  $("#groups").hidden = state.view !== "machines";
  $("#containers").hidden = state.view !== "containers";
  $("#docker-host-count").textContent = hosts.length;
  document.querySelectorAll("[data-view]").forEach((el) => el.setAttribute("aria-pressed", String(el.dataset.view === state.view)));

  const btn = $("#update-selected");
  btn.disabled = state.selected.size === 0;
  btn.textContent = `Update selected (${state.selected.size})`;
  $("#bulk-toolbar").hidden = state.selected.size === 0 || state.view !== "machines";
  $("#selection-count").textContent = `${state.selected.size} ${plural(state.selected.size, "machine", "machines")} selected`;
  document.querySelectorAll("[data-machine-menu]").forEach((el) => el.setAttribute("aria-expanded", String(el.dataset.machineMenu === machineMenuId)));

  $("#jobs").innerHTML = state.jobs.map((j) => {
    const cls = j.status === "failed" ? "bad" : j.status === "succeeded" ? "ok" : "";
    const icon = j.status === "running" || j.status === "queued" ? `<span class="spin"></span>` : "";
    return `<li data-job="${j.id}" class="${state.logJob === j.id ? "sel" : ""}">
      <span>${icon} ${esc(j.target_name)} · ${j.action}</span><span class="${cls}">${j.status}</span></li>`;
  }).join("") || `<li class="muted">No jobs yet. Checks, updates, reboots and backups you start show up here.</li>`;
  if (focused && document.activeElement === document.body) {
    [...document.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focused)?.focus({ preventScroll: true });
  }
}

function setView(view, hostId = null) {
  state.view = state.settings.docker_enabled && view === "containers" ? "containers" : "machines";
  remember.set("dashboardView", state.view);
  closeMachineMenu();
  render();
  if (hostId) document.getElementById(`docker-${hostId}`)?.scrollIntoView({ block: "start" });
}

let machineMenuId = null;
function closeMachineMenu(restoreFocus = false) {
  $("#machine-actions").hidden = true;
  if (machineMenuId) {
    const trigger = [...document.querySelectorAll("[data-machine-menu]")].find((el) => el.dataset.machineMenu === machineMenuId);
    trigger?.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger?.focus();
  }
  machineMenuId = null;
}

function openMachineMenu(id, trigger) {
  const open = machineMenuId === id;
  closeMachineMenu();
  if (open) return;
  const v = state.targets.find((item) => item.target.id === id);
  if (!v) return;
  const t = v.target, busy = !!v.active_job;
  const action = (kind, label, iconName, enabled, reason = "") => `<button data-act="${kind}" data-id="${esc(id)}" ${enabled ? "" : "disabled"} title="${esc(reason)}">${icon(iconName)}${label}</button>`;
  const menu = $("#machine-actions");
  menu.innerHTML = `<div class="muted menu-caption">${esc(t.name)}</div>` +
    action("check", "Check updates", "refresh", t.updatable && !busy) +
    (v.check?.reboot_required === true ? "" : action("reboot", "Reboot", "reboot", canReboot(t) && !busy && !v.blocked, t.no_reboot || v.blocked || t.note)) +
    (t.can_backup ? action("backup", "Back up configuration", "backup", !busy) : "") +
    (hasDocker(v) ? `<button data-docker-settings="${esc(id)}">${icon("settings")}Docker management</button>` : "") +
    (state.console?.enabled && !["stopped", "offline"].includes(t.status) ? `<button data-run="${esc(id)}">${icon("terminal")}Run command</button>` : "") +
    `<button data-hide="${v.hidden ? "0" : "1"}" data-id="${esc(id)}">${icon(v.hidden ? "eye" : "eyeOff")}${v.hidden ? "Unhide machine" : "Hide machine"}</button>`;
  machineMenuId = id;
  trigger.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, rect.bottom + menu.offsetHeight < innerHeight ? rect.bottom + 6 : rect.top - menu.offsetHeight - 6)}px`;
  menu.querySelector("button:not(:disabled)")?.focus();
}

function dockerModeFields() {
  const external = $("#docker-mode").value === "external";
  $("#docker-external-fields").hidden = !external;
  $("#docker-manager-url").disabled = !external;
  $("#docker-manager-name").disabled = !external;
}

function openDockerSettings(id) {
  const v = state.targets.find((item) => item.target.id === id);
  if (!v) return;
  closeMachineMenu();
  const policy = dockerSettings(v), dlg = $("#docker-settings-dialog");
  dlg.dataset.targetId = id;
  $("#docker-settings-title").textContent = `Docker management · ${v.target.name}`;
  $("#docker-mode").value = policy.mode;
  $("#docker-manager-name").value = policy.manager_name;
  $("#docker-manager-url").value = policy.manager_url || "";
  $("#docker-settings-error").textContent = "";
  dockerModeFields();
  dlg.showModal();
}

async function load(refresh = false) {
  try {
    const data = await api(`/api/targets${refresh ? "?refresh=true" : ""}`);
    state.targets = data.targets;
    state.errors = data.errors;
    state.settings = data.settings || {};
    state.backupPlaces = data.backup_places || {};
    state.backupFolder = data.backup_folder || null;
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

async function act(action, ids, { rebootIfRequired = false, snapshotFirst = false, destination = null, stack = null, prune = false } = {}) {
  if (!ids.length) return;
  let res;
  try {
    const body = { action, target_ids: ids, reboot_if_required: rebootIfRequired, snapshot_first: snapshotFirst, destination, stack, prune };
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
  backup: {
    title: "Back up these machines?",
    text: "Saves what is needed to set the machine up again, not its guests or instances. Proxmox: the node's config (to Proxmox Backup Server if there is one). Incus: its settings. Other machines: an archive of /etc and a few more config folders. Nothing is stopped or changed.",
    ok: "Back up",
  },
  stacks: {
    title: "Check Docker on these machines?",
    text: "Downloads (pulls) newer images for every running Docker Compose stack on each machine, which can use a lot of bandwidth and disk space. Nothing is restarted. Machines where Docker was found missing are left out.",
    ok: "Check Docker",
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
  const placeRow = $("#confirm-place-row");
  placeRow.hidden = action !== "backup";
  if (action === "backup") {
    const saved = state.backupPlaces[ids[0]] || "";
    const onDashboard = saved === "dashboard";
    $("#confirm-place-default").checked = !saved;
    $("#confirm-place-dashboard").checked = onDashboard;
    $("#confirm-place-dashboard-path").textContent = state.backupFolder || "its backup folder";
    $("#confirm-place-custom").checked = !!saved && !onDashboard;
    $("#confirm-place-path").value = onDashboard ? "" : saved;
    $("#confirm-place-remember").checked = !!saved;
  }
  $("#confirm-list").innerHTML = ids.map((id) => {
    const v = byId.get(id);
    const n = v?.check ? actionablePackages(v.check).length : null;
    const phased = phasedPackages(v?.check).length;
    let detail = n != null ? `${n} package${n === 1 ? "" : "s"}${phased ? ` · ${phased} phased, waiting` : ""}` : "not checked";
    if (action === "backup" || action === "stacks") detail = "";
    if (action === "reboot") {
      detail = v?.check?.reboot_required ? "reboot needed" : "";
      const t = v?.target;
      const guests = t?.kind === "host"
        ? state.targets.filter((g) => g.target.parent === t.name && g.target.source === t.source && g.target.status === "running").length : 0;
      // Waits for running guest jobs first, but the guests themselves go down with it.
      if (guests) detail += `${detail ? " · " : ""}and every guest running on it (${guests})`;
    }
    if (action === "upgrade" && v?.target.no_reboot) detail += ` · won't reboot: ${v.target.no_reboot}`;
    const app = v?.check?.community_script;
    if (action === "upgrade" && app) {
      detail += state.settings.community_scripts_update
        ? ` · then runs ${app}'s own update (helper script)`
        : ` · ${app}'s own update is off: run "update" in it if it misbehaves`;
    }
    return `<li>${esc(v?.target.name || id)} <span class="muted">${esc(detail)}</span></li>`;
  }).join("");
  const snapshottable = action === "upgrade" && ids.some((id) => byId.get(id)?.target.can_snapshot);
  $("#confirm-snapshot-row").hidden = !snapshottable;
  $("#confirm-snapshot").checked = snapshottable && !!state.settings.snapshot_before_upgrade;
  const dlg = $("#confirm");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue !== "ok") return;
    if (action === "upgrade") state.selected.clear();
    const upgrade = action === "upgrade";
    let destination = null;
    if (action === "backup") {
      destination = $("#confirm-place-dashboard").checked ? "dashboard"
        : $("#confirm-place-custom").checked ? $("#confirm-place-path").value.trim() : null;
      if ($("#confirm-place-custom").checked && !destination) { alert("Enter the folder to back up to, or choose the default place."); return; }
      if ($("#confirm-place-remember").checked) {
        // Choosing the default place and remembering it clears an earlier choice.
        try {
          const r = await api("/api/backup-places", { method: "PUT", body: JSON.stringify({ target_id: ids[0], path: destination }) });
          state.backupPlaces = r.backup_places;
        } catch (e) { alert(`Could not save the backup folder: ${e.message}`); return; }
      }
    }
    act(action, ids, {
      destination,
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
  const el = e.target.closest("[data-act],[data-toggle],[data-job],[data-hide],[data-stack]");
  if (!el || el.disabled) return;
  closeMachineMenu();
  $("#dashboard-menu").open = false;
  if (el.dataset.stack) {
    const id = el.dataset.id, name = el.dataset.stack;
    const host = state.targets.find((v) => v.target.id === id)?.target.name || id;
    if (!confirm(`Update ${name} on ${host}?\n\nPulls newer images and restarts the containers that changed. Stopped services stay stopped.`)) return;
    act("stack-update", [id], { stack: name, prune: !!remember.get("pruneImages") });
  } else if (el.dataset.act === "docker") {
    setView("containers", el.dataset.id);
  } else if (el.dataset.act === "stacks") {
    confirmAction("stacks", [el.dataset.id]);
  } else if (el.dataset.hide) {
    setHidden(el.dataset.id, el.dataset.hide === "1");
  } else if (el.dataset.toggle) {
    const id = el.dataset.toggle;
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    render();
  } else if (el.dataset.job) {
    showLog(el.dataset.job);
  } else if (el.dataset.act === "check") {
    act("check", [el.dataset.id]);
  } else if (el.dataset.act === "upgrade" || el.dataset.act === "reboot" || el.dataset.act === "backup") {
    confirmAction(el.dataset.act, [el.dataset.id]);
  }
});

document.addEventListener("click", (e) => {
  if (!e.target.closest("#dashboard-menu") || e.target.closest(".dashboard-menu-items button,.dashboard-menu-items a")) $("#dashboard-menu").open = false;
  const el = e.target.closest("[data-machine-menu],[data-open-docker],[data-docker-settings],[data-view],[data-fetch-docker],[data-discover-docker]");
  if (el?.dataset.machineMenu) { openMachineMenu(el.dataset.machineMenu, el); return; }
  if (!e.target.closest("#machine-actions")) closeMachineMenu();
  if (!el || el.disabled) return;
  if (el.dataset.view) setView(el.dataset.view);
  else if (el.dataset.openDocker) setView("containers", el.dataset.openDocker);
  else if (el.dataset.dockerSettings) openDockerSettings(el.dataset.dockerSettings);
  else if (el.hasAttribute("data-fetch-docker")) {
    const ids = dockerHosts().filter((v) => dockerEnabled(v) && canUpgrade(v.target) && !v.active_job).map((v) => v.target.id);
    if (ids.length) confirmAction("stacks", ids);
  } else if (el.hasAttribute("data-discover-docker")) {
    act("check", active().filter((v) => v.target.updatable && !v.active_job).map((v) => v.target.id));
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMachineMenu(true); $("#dashboard-menu").open = false; }
});
window.addEventListener("resize", () => closeMachineMenu());
window.addEventListener("scroll", () => closeMachineMenu(), true);
$("#clear-selected").onclick = () => { state.selected.clear(); render(); };
$("#docker-mode").onchange = dockerModeFields;
$("#docker-settings-close").onclick = () => $("#docker-settings-dialog").close();
$("#docker-settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const dlg = $("#docker-settings-dialog"), id = dlg.dataset.targetId;
  const body = { target_id: id, mode: $("#docker-mode").value, manager_name: $("#docker-manager-name").value.trim() || "Dockhand", manager_url: $("#docker-manager-url").value.trim() || null };
  $("#docker-settings-save").disabled = true;
  $("#docker-settings-error").textContent = "";
  try {
    const res = await api("/api/docker-settings", { method: "PUT", body: JSON.stringify(body) });
    const view = state.targets.find((v) => v.target.id === id);
    if (view) view.docker_settings = res.docker_settings;
    dlg.close();
    render();
  } catch (err) {
    $("#docker-settings-error").textContent = err.message;
  } finally {
    $("#docker-settings-save").disabled = false;
  }
};

document.addEventListener("change", (e) => {
  if (e.target.matches("[data-prune]")) { remember.set("pruneImages", e.target.checked); return; }
  const id = e.target.dataset?.select;
  if (!id) return;
  e.target.checked ? state.selected.add(id) : state.selected.delete(id);
  render();
});

$("#confirm-place-path").addEventListener("input", () => { $("#confirm-place-custom").checked = true; });
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
