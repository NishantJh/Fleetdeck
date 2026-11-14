"use strict";

// The Services tab. Read-only catalogue from /api/services; the only write is hide/unhide.
const SERVICE_CATEGORY_ORDER = [
  "Media",
  "Home automation",
  "Network",
  "Storage & backup",
  "Monitoring",
  "Security & identity",
  "Development",
  "Productivity",
  "Tools",
  "Virtualization",
  "Other",
  "Support",
];

function servicesEnabled() {
  return !!(state.services && state.services.enabled);
}

let serviceSearchReady = false;

function serviceMatches(service) {
  if (service.hidden && !state.showHiddenServices) return false;
  if (!service.ui && !service.hidden && !state.showSupportServices) return false;
  const query = (state.serviceQuery || "").trim().toLowerCase();
  if (!query) return true;
  return [service.name, service.machine, service.stack, service.image, service.category]
    .some((value) => (value || "").toLowerCase().includes(query));
}

const CATEGORY_ICONS = {
  "Media": '<path d="M5 4h14v16H5z"/><path d="m10 9 5 3-5 3z"/>',
  "Home automation": '<path d="M3 11 12 3l9 8"/><path d="M5 10v10h14V10"/>',
  "Network": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  "Storage & backup": '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  "Monitoring": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  "Security & identity": '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>',
  "Development": '<path d="m8 8-5 4 5 4M16 8l5 4-5 4M14 5l-4 14"/>',
  "Productivity": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  "Tools": '<path d="M14 6a4 4 0 0 0-5 5L3 17l4 4 6-6a4 4 0 0 0 5-5l-3 3-3-1-1-3z"/>',
  "Virtualization": '<rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/>',
};
const CATEGORY_DEFAULT_ICON = '<rect x="4" y="4" width="16" height="16" rx="3"/>';

function categoryIcon(name) {
  return `<svg class="cat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${CATEGORY_ICONS[name] || CATEGORY_DEFAULT_ICON}</svg>`;
}

function tileHue(name) {
  let h = 0;
  for (const ch of name || "?") h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

function serviceCard(service) {
  const links = service.links || [];
  const primary = links[0];
  const letter = (service.name || "?").slice(0, 1).toUpperCase();
  const where = [service.machine, service.stack].filter(Boolean).join(" · ");
  const chips = links.slice(1).map((link) => `<a class="chip service-link" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer" title="${esc(link.url)}">${esc(link.label || link.kind)}</a>`).join("");
  const inner = `<span class="tile-icon" style="--h:${tileHue(service.name)}" aria-hidden="true">${esc(letter)}<span class="dot ${DOT[service.state] || ""}" title="${esc(service.state || "link")}"></span></span>
      <span class="tile-text"><span class="tile-name">${esc(service.name)}</span><span class="tile-sub muted">${esc(where || (service.link_only ? "link" : ""))}</span></span>`;
  const main = primary
    ? `<a class="tile-main" href="${esc(primary.url)}" target="_blank" rel="noopener noreferrer" title="${esc(primary.url)}">${inner}</a>`
    : `<div class="tile-main no-link" title="Add a fleetdeck.url label or a services.links entry">${inner}</div>`;
  const action = service.manual
    ? `<button class="small" data-remove-link="${esc(service.id.split("|")[1])}">Remove</button>`
    : service.hidden
      ? `<button class="small" data-unhide-service="${esc(service.id)}">Unhide</button>`
      : `<button class="small" data-hide-service="${esc(service.id)}">Hide</button>`;
  const badge = service.link_only && !service.manual && service.machine ? `<span class="chip tile-badge" title="Listed only: never updated or monitored from here">link only</span>` : "";
  return `<article class="service-tile${service.hidden ? " is-hidden-service" : ""}${service.state && !["running", "restarting"].includes(service.state) && !service.link_only ? " is-stopped" : ""}" data-service-id="${esc(service.id)}">
    ${main}
    <div class="tile-foot">${badge}${chips}<span class="tile-actions">${action}</span></div>
  </article>`;
}

function renderServices() {
  const el = $("#services");
  if (!servicesEnabled()) {
    el.hidden = true;
    return;
  }
  const all = state.services.services || [];
  if (state.view === "services" && !state.sourceScanStarted && (state.services.sources || []).some((x) => !x.scanned)) {
    state.sourceScanStarted = true; // once per page load; the button scans again
    serviceCall("/api/services/scan", { method: "POST" });
  }
  const shown = all.filter(serviceMatches);
  const hidden = all.filter((service) => service.hidden).length;
  const support = all.filter((service) => !service.ui && !service.hidden).length;
  const query = (state.serviceQuery || "").trim();
  const groups = {};
  for (const service of shown) (groups[service.category] ||= []).push(service);
  const order = SERVICE_CATEGORY_ORDER.concat(Object.keys(groups).filter((name) => !SERVICE_CATEGORY_ORDER.includes(name)));
  const present = order.filter((name) => groups[name]?.length);
  const chips = present.length > 1
    ? `<div class="service-chips" role="group" aria-label="Groups"><button class="chip-btn" data-service-cat="" aria-pressed="${!state.serviceCat}">All</button>${present.map((name) => `<button class="chip-btn" data-service-cat="${esc(name)}" aria-pressed="${state.serviceCat === name}">${esc(name)}</button>`).join("")}</div>`
    : "";
  const visible = state.serviceCat ? present.filter((name) => name === state.serviceCat) : present;
  const sections = visible.map((name) => `<section class="service-category">
      <h2>${categoryIcon(name)}${esc(name)} <span class="muted">${groups[name].length}</span></h2>
      <div class="service-grid${state.serviceDense ? " dense" : ""}">${groups[name].map(serviceCard).join("")}</div>
    </section>`).join("");
  const empty = query
    ? `<div class="empty"><h3>No services match “${esc(query)}”</h3><p class="muted">Clear the search to show all services.</p><button class="small" data-clear-service-search>Clear search</button></div>`
    : support
      ? `<div class="empty"><h3>No services with a UI found</h3><p class="muted">${support} support container${support === 1 ? " is" : "s are"} hidden. Show them if one has a UI, or add a <code>fleetdeck.url</code> label.</p><button class="small" data-toggle-support-services>Show support containers (${support})</button></div>`
      : `<div class="empty"><h3>No services yet</h3><p class="muted">Run a container check to discover services, add a services-only source in the config, or use Add link.</p></div>`;
  const checked = state.services.checked_at ? `Last scan ${ago(state.services.checked_at)}` : "No Docker scan yet";
  const sources = state.services.sources || [];
  const failed = sources.filter((source) => source.error);
  const sourceNote = failed.length ? `<p class="muted service-source-error">${failed.map((source) => `${esc(source.name)}: ${esc(source.error)}`).join(" · ")}</p>` : "";
  const form = state.serviceAdding ? `<form class="service-add" data-add-link-form>
      <input name="name" placeholder="Name" required maxlength="80" aria-label="Name">
      <input name="url" type="url" placeholder="https://router.lan or http://192.168.1.1:8080" required maxlength="2048" aria-label="Address">
      <input name="category" placeholder="Group (e.g. Network)" maxlength="40" list="service-groups" aria-label="Group">
      <input name="machine" placeholder="Machine (optional)" maxlength="80" aria-label="Machine">
      <datalist id="service-groups">${SERVICE_CATEGORY_ORDER.map((name) => `<option value="${esc(name)}">`).join("")}</datalist>
      <button class="small primary" type="submit">Add</button><button class="small" type="button" data-add-link-cancel>Cancel</button>
    </form>` : "";
  const markup = `<div class="services-toolbar">
      <input id="service-search" name="fleetdeck-service-filter" type="search" placeholder="Search services" value="${esc(state.serviceQuery || "")}" autocomplete="off" autocapitalize="off" spellcheck="false" data-1p-ignore="true" data-lpignore="true" aria-label="Search services">
      ${query ? `<button class="small" data-clear-service-search>Clear search</button>` : ""}
      <span class="muted">${esc(checked)}</span>
      <button class="small" data-add-link>Add link</button>
      ${sources.length ? `<button class="small" data-scan-sources title="Read the container list of ${sources.length} other machine(s). Read-only.">Scan other machines</button>` : ""}
      <button class="small" data-refresh-services title="Read the last Docker scan again; run Check container updates to refresh it">Reload</button>
      <button class="small" data-toggle-dense aria-pressed="${!!state.serviceDense}">${state.serviceDense ? "Comfortable" : "Compact"}</button>
      ${hidden ? `<button class="small" data-toggle-hidden-services aria-pressed="${state.showHiddenServices}">${state.showHiddenServices ? "Hide hidden" : `Show hidden (${hidden})`}</button>` : ""}
      ${support ? `<button class="small" data-toggle-support-services aria-pressed="${state.showSupportServices}">${state.showSupportServices ? "Hide support containers" : `Show support containers (${support})`}</button>` : ""}
    </div>${form}${sourceNote}${chips}${sections || empty}`;
  if (el.innerHTML !== markup) el.innerHTML = markup;
  if (!serviceSearchReady) {
    serviceSearchReady = true;
    state.serviceQuery = "";
    const input = $("#service-search");
    if (input) input.value = "";
  }
}

async function serviceCall(path, options) {
  try {
    state.services = await api(path, options);
    state.errors = {};
  } catch (error) {
    state.errors = { services: error.message };
  }
  render();
}

async function setServiceHidden(id, hidden) {
  try {
    await api("/api/services/hidden", { method: "POST", body: JSON.stringify({ service_ids: [id], hidden }) });
    await load();
  } catch (error) {
    state.errors = { services: error.message };
    render();
  }
}

document.addEventListener("input", (event) => {
  if (event.target.id !== "service-search") return;
  state.serviceQuery = event.target.value;
  renderServices();
  const input = $("#service-search");
  if (input) {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
});

document.addEventListener("submit", (event) => {
  if (!event.target.matches("[data-add-link-form]")) return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  state.serviceAdding = false;
  serviceCall("/api/services/links", { method: "POST", body: JSON.stringify({ ...data, category: data.category || "Other" }) });
});

document.addEventListener("click", (event) => {
  const act = event.target.closest("[data-add-link],[data-add-link-cancel],[data-remove-link],[data-scan-sources],[data-toggle-dense],[data-service-cat]");
  if (act) {
    const d = act.dataset;
    if (d.addLink !== undefined || d.addLinkCancel !== undefined) {
      state.serviceAdding = d.addLink !== undefined;
      return renderServices();
    }
    if (d.removeLink) {
      if (!confirm("Remove this link card? Nothing on the machine changes.")) return;
      return serviceCall(`/api/services/links/${encodeURIComponent(d.removeLink)}`, { method: "DELETE" });
    }
    if (d.scanSources !== undefined) return serviceCall("/api/services/scan?force=true", { method: "POST" });
    if (d.toggleDense !== undefined) {
      state.serviceDense = !state.serviceDense;
      remember.set("serviceDense", state.serviceDense);
      return renderServices();
    }
    if (d.serviceCat !== undefined) {
      state.serviceCat = d.serviceCat;
      return renderServices();
    }
  }
  const el = event.target.closest("[data-hide-service],[data-unhide-service],[data-toggle-hidden-services],[data-toggle-support-services],[data-refresh-services],[data-clear-service-search]");
  if (!el) return;
  if (el.dataset.clearServiceSearch !== undefined) {
    state.serviceQuery = "";
    render();
    $("#service-search")?.focus();
    return;
  }
  if (el.dataset.hideService) return setServiceHidden(el.dataset.hideService, true);
  if (el.dataset.unhideService) return setServiceHidden(el.dataset.unhideService, false);
  if (el.dataset.toggleHiddenServices !== undefined) {
    state.showHiddenServices = !state.showHiddenServices;
    remember.set("showHiddenServices", state.showHiddenServices);
    return render();
  }
  if (el.dataset.toggleSupportServices !== undefined) {
    state.showSupportServices = !state.showSupportServices;
    remember.set("showSupportServices", state.showSupportServices);
    return render();
  }
  if (el.dataset.refreshServices !== undefined) load(true);
});
